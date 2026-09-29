"""
osm_route_fill — gap detection + POI-anchored fill for OSM-sourced routes.

Separate pipeline from stages 3a-3d (per hades-path-aware-synthesis §6).
The OSM relation polyline is already authoritative; we only add synthetics
where a large along-polyline gap exists and Overpass yields a nearby POI.

Stage 4 (pure synthesis) is deliberately off-limits here: inventing stops
along a polyline we trust would manufacture authority we have not earned.

Thresholds (§6):
- gap_min_m_urban = 600; rural (interparroquial or route >15km) = 1200
- along-path separation between synthetics in same gap ≥ 200 m
- cap: ≤ 1 synthetic per 400 m of gap length
- POI-to-path distance ≤ 80 m (same ceiling as stage 3a)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from datamind_console.common import (
    review_queue_writer as rw,
    synthesis_events as se,
)
from datamind_console.phases.phase3_routes.synthesis import (
    path_inference as pi,
    poi_matcher as pm,
)

log = logging.getLogger(__name__)

SKILL_ID = "hades-osm-route-node-fill"
STAGE_OSM_ROUTE_FILL = "osm_route_fill"
SOURCE_TYPE_POI = "poi_anchored_path_projected"

GAP_THRESHOLD_URBAN_M = 600.0
GAP_THRESHOLD_RURAL_M = 1200.0
MIN_SEPARATION_IN_GAP_M = 200.0
CAP_PER_GAP_METRES = 400.0  # 1 synthetic per 400 m
POI_PATH_DISTANCE_MAX_M = pm.POI_PATH_DISTANCE_MAX_M  # 80 m


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gap:
    idx: int
    start_coords: tuple[float, float]
    end_coords: tuple[float, float]
    length_m: float
    start_index: int  # polyline vertex index (inclusive)
    end_index: int    # polyline vertex index (inclusive)


@dataclass(frozen=True)
class FilledStop:
    node_id: str
    osm_id: int
    lat: float
    lon: float
    poi: pm.POIMatch
    gap_idx: int
    review_path: Optional[Path] = None


@dataclass
class FillReport:
    gaps: list[Gap] = field(default_factory=list)
    filled: list[FilledStop] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # {gap_idx, reason}


# ---------------------------------------------------------------------------
# gap detection
# ---------------------------------------------------------------------------


def _nearest_vertex_idx(target: tuple[float, float], pts: Sequence[tuple[float, float]]) -> int:
    best_i, best_d = 0, math.inf
    for i, p in enumerate(pts):
        d = pi.haversine_m(target, p)
        if d < best_d:
            best_d, best_i = d, i
    return best_i


def _along_path_distance(
    pts: Sequence[tuple[float, float]], i: int, j: int
) -> float:
    if i > j:
        i, j = j, i
    total = 0.0
    for k in range(i, j):
        total += pi.haversine_m(pts[k], pts[k + 1])
    return total


def detect_gaps(
    polyline: str,
    stop_coords: Sequence[tuple[float, float]],
    *,
    gap_threshold_m: float = GAP_THRESHOLD_URBAN_M,
) -> list[Gap]:
    """Walk consecutive stop pairs in sequence order; flag pairs whose
    along-polyline separation exceeds ``gap_threshold_m``."""
    if len(stop_coords) < 2:
        return []
    pts = pi.decode_polyline(polyline)
    if len(pts) < 2:
        return []
    gaps: list[Gap] = []
    for idx, (a, b) in enumerate(zip(stop_coords, stop_coords[1:])):
        i = _nearest_vertex_idx(a, pts)
        j = _nearest_vertex_idx(b, pts)
        length_m = _along_path_distance(pts, i, j)
        if length_m > gap_threshold_m:
            gaps.append(Gap(
                idx=idx,
                start_coords=a, end_coords=b,
                length_m=length_m,
                start_index=min(i, j), end_index=max(i, j),
            ))
    return gaps


def is_rural_route(
    *,
    operator_type: Optional[str],
    termini: Sequence[tuple[float, float]],
) -> bool:
    if operator_type and "interparroquial" in operator_type.lower():
        return True
    if len(termini) >= 2:
        span_m = pi.haversine_m(termini[0], termini[-1])
        if span_m > 15_000.0:
            return True
    return False


def gap_threshold_for(
    *,
    operator_type: Optional[str],
    termini: Sequence[tuple[float, float]],
) -> float:
    return (
        GAP_THRESHOLD_RURAL_M if is_rural_route(
            operator_type=operator_type, termini=termini,
        ) else GAP_THRESHOLD_URBAN_M
    )


# ---------------------------------------------------------------------------
# fill_gap — per-gap POI-anchored synthesis
# ---------------------------------------------------------------------------


def _gap_bbox(
    pts: Sequence[tuple[float, float]], gap: Gap, pad_deg: float = 0.002
) -> tuple[float, float, float, float]:
    seg = pts[gap.start_index : gap.end_index + 1]
    if not seg:
        seg = [gap.start_coords, gap.end_coords]
    lats = [p[0] for p in seg]
    lons = [p[1] for p in seg]
    return (
        min(lats) - pad_deg, min(lons) - pad_deg,
        max(lats) + pad_deg, max(lons) + pad_deg,
    )


def _gap_cap(gap: Gap) -> int:
    """≤ 1 synthetic per 400 m of gap length (§6)."""
    return max(1, int(gap.length_m // CAP_PER_GAP_METRES))


def _candidates_in_gap(
    polyline: str,
    gap: Gap,
    *,
    bbox: tuple[float, float, float, float],
    http_post: Optional[Callable] = None,
) -> list[pm.POIMatch]:
    # osm_route_fill uses name="" to match any POI — we filter by class + path distance only.
    # The Overpass regex "" matches all names, but our module requires a name to build the
    # regex. Instead we iterate a small synthetic query per tag family.
    out: list[pm.POIMatch] = []
    # one query per tag family with wildcard name
    try:
        found = pm.query_pois_by_name(
            ".*", bbox,
            tag_filters=pm.OSM_FILL_TAG_FILTERS,
            http_post=http_post,
        )
    except pm.POIMatcherError as e:
        log.warning("overpass failed in gap %d: %s", gap.idx, e)
        return []
    for poi in found:
        proj = pi.project_point_to_polyline(
            poi.centroid[0], poi.centroid[1], polyline,
        )
        if proj.distance_m > POI_PATH_DISTANCE_MAX_M:
            continue
        out.append(pm.POIMatch(
            osm_id=poi.osm_id,
            osm_type=poi.osm_type,
            name=poi.name,
            centroid=poi.centroid,
            tags=poi.tags,
            class_name=poi.class_name,
            class_priority=poi.class_priority,
            poi_to_path_distance_m=proj.distance_m,
            entrance_nodes=poi.entrance_nodes,
        ))
    return out


def _select_with_separation(
    ranked: Sequence[pm.POIMatch], polyline: str, gap: Gap, max_n: int,
) -> list[pm.POIMatch]:
    """Pick up to ``max_n`` POIs ensuring ≥ 200 m along-path separation."""
    picks: list[pm.POIMatch] = []
    for poi in ranked:
        proj = pi.project_point_to_polyline(poi.centroid[0], poi.centroid[1], polyline)
        ok = True
        for p in picks:
            pproj = pi.project_point_to_polyline(p.centroid[0], p.centroid[1], polyline)
            d = pi.haversine_m((proj.lat, proj.lon), (pproj.lat, pproj.lon))
            if d < MIN_SEPARATION_IN_GAP_M:
                ok = False
                break
        if ok:
            picks.append(poi)
            if len(picks) >= max_n:
                break
    return picks


# ---------------------------------------------------------------------------
# DB write helpers (mirror core._insert_node + event + review)
# ---------------------------------------------------------------------------


def _allocate_synthetic_osm_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT nextval('node_prod.synthetic_osm_id_seq')")
        row = cur.fetchone()
    if not row:
        raise RuntimeError("synthetic_osm_id_seq returned no row")
    return int(row[0] if isinstance(row, (tuple, list)) else row.get("nextval"))


def _insert_osm_fill_node(
    conn, *,
    osm_id: int,
    lat: float, lon: float,
    source: str,
    source_type: str,
    poi_osm_id: int,
    poi_to_path_distance_m: float,
    osm_route_fill_context: str,
) -> str:
    """Insert a synthetic node for an OSM-route gap fill via the
    universal stop-quality treater.

    Pre-migration: a raw INSERT that referenced non-existent columns
    (lat, lon, route_id, anchor_name, poi_osm_id). Post: routes through
    ``treat_stop(operation='synthetic_insert')`` so the new node gets
    the contextual-name cascade, a ``geo_prod.places`` row + mapping,
    and an audit row.

    Returns the new node_id (UUID as str). The caller no longer pre-
    allocates the node_id — the treater generates it and we return it
    for use in downstream synthesis-event logging.
    """
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput,
        treat_stop,
    )

    extras = {
        "osm_id": osm_id,
        "source": source,
        "source_type": source_type,
        "synthetic_confidence": "medium",
        "poi_anchor_osm_id": poi_osm_id,
        "poi_to_path_distance_m": poi_to_path_distance_m,
        "osm_route_fill_context": osm_route_fill_context,
    }
    res = treat_stop(
        StopTreatmentInput(
            operation="synthetic_insert",
            caller="osm_route_fill.gap_fill",
            proposed_name="",        # cascade derives a name
            proposed_lat=float(lat),
            proposed_lon=float(lon),
            extras=extras,
        ),
        conn,
    )
    if not res.success:
        raise RuntimeError(f"osm_route_fill treatment failed: {res.error}")
    return str(res.node_id)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def fill_osm_route_gaps(
    *,
    conn,
    route_id: str,
    route_code: str,
    unit: str,
    province: str,
    osm_relation_id: int,
    polyline: str,
    stop_coords: Sequence[tuple[float, float]],
    termini: Sequence[tuple[float, float]],
    operator_type: Optional[str] = None,
    review_root: Path,
    now: Optional[datetime] = None,
    http_post: Optional[Callable] = None,
) -> FillReport:
    """Detect gaps, try to fill each with POI-anchored synthetics.

    Returns a :class:`FillReport` describing what was filled vs skipped.
    No stage-4 synthesis ever fires here — gaps with zero POI evidence
    stay sparse.
    """
    now = now or datetime.now(timezone.utc)
    threshold = gap_threshold_for(operator_type=operator_type, termini=termini)
    gaps = detect_gaps(polyline, stop_coords, gap_threshold_m=threshold)
    report = FillReport(gaps=list(gaps))
    if not gaps:
        return report

    pts = pi.decode_polyline(polyline)
    for gap in gaps:
        bbox = _gap_bbox(pts, gap)
        candidates = _candidates_in_gap(
            polyline, gap, bbox=bbox, http_post=http_post,
        )
        if not candidates:
            report.skipped.append({"gap_idx": gap.idx, "reason": "no_poi_evidence"})
            continue
        ranked = pm.rank_for_gap_fill(candidates)
        picks = _select_with_separation(ranked, polyline, gap, _gap_cap(gap))
        if not picks:
            report.skipped.append({"gap_idx": gap.idx, "reason": "all_candidates_too_close"})
            continue

        for poi in picks:
            proj = pi.project_point_to_polyline(
                poi.centroid[0], poi.centroid[1], polyline,
            )
            osm_id = _allocate_synthetic_osm_id(conn)
            source = f"osm_route_fill:{osm_relation_id}:{gap.idx}"
            context = f"relation_{osm_relation_id}_gap_{gap.idx}"

            node_id = _insert_osm_fill_node(
                conn,
                osm_id=osm_id,
                lat=proj.lat, lon=proj.lon,
                source=source,
                source_type=SOURCE_TYPE_POI,
                poi_osm_id=poi.osm_id,
                poi_to_path_distance_m=proj.distance_m,
                osm_route_fill_context=context,
            )
            se.log_synthesis_event(
                conn,
                node_id=node_id,
                route_id=route_id,
                unit=unit, province=province,
                stage=STAGE_OSM_ROUTE_FILL,
                anchor_name=poi.name,
                final_coords=(proj.lat, proj.lon),
                triggered_by_skill=SKILL_ID,
            )
            if hasattr(conn, "commit"):
                conn.commit()

            review_path: Optional[Path] = None
            try:
                review_path = rw.write_synthetic_review(
                    review_root=Path(review_root),
                    node_id=node_id,
                    osm_id=osm_id,
                    stage=STAGE_OSM_ROUTE_FILL,
                    source_type=SOURCE_TYPE_POI,
                    source=source,
                    unit=unit,
                    province=province,
                    route_code=route_code,
                    route_id=route_id,
                    anchor_name=poi.name,
                    synthetic_confidence="medium",
                    final_coords=(proj.lat, proj.lon),
                    poi_to_path_distance_m=proj.distance_m,
                    osm_route_fill_context=context,
                    cap_weight_consumed=0.0,
                    triggered_by_skill=SKILL_ID,
                    research_output_file=None,
                    evidence_markdown=(
                        f"- POI class: {poi.class_name}\n"
                        f"- POI name: {poi.name}\n"
                        f"- distance to path: {proj.distance_m:.1f}m\n"
                        f"- gap length: {gap.length_m:.0f}m\n"
                    ),
                    why_synthesis_fired=(
                        f"OSM relation {osm_relation_id} gap {gap.idx} "
                        f"(length {gap.length_m:.0f}m) filled by POI at "
                        f"{proj.distance_m:.0f}m from path.\n"
                    ),
                    now=now,
                )
            except Exception as e:
                log.error("review write failed node=%s: %s", node_id, e)

            report.filled.append(FilledStop(
                node_id=node_id,
                osm_id=osm_id,
                lat=proj.lat, lon=proj.lon,
                poi=poi,
                gap_idx=gap.idx,
                review_path=review_path,
            ))
    return report
