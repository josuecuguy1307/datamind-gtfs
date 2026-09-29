"""
Stage D — Corridor ∩ Stop DB Intersection

Given a Valhalla corridor (GeoJSON LineString), finds all DB stops near the
corridor using progressive buffer widening. Returns candidates ordered by
path_fraction (position along the corridor).
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List, Optional, Set

from datamind_console.db.db import db_conn, fetch_all
from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    CorridorIntersectionResult,
    CorridorStopCandidate,
)
from datamind_console.phases.phase3_routes.stop_grounding.geography_guardrails import (
    distance_to_bbox_m,
    locality_consistency_score,
    point_in_bbox,
)
from datamind_console.phases.phase3_routes.stop_grounding.catalogs import get_config_section
from datamind_core.province_config import (
    DEFAULT_PROVINCE,
    get_operator_keywords,
    get_province,
)

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Jurisdiction inference for stop_source classification
#
# Province-agnostic by design:
#   * operator_keywords for each province live in
#     ``workspace/config/supported_provinces.json``.
#   * region-specific bbox heuristics (e.g. the historical Rumiñahui
#     fallback) are still loaded from ``stop_grounding_config.json``
#     for Sample Region retro-compat. For any province that doesn't have
#     its own fallback, unknown stops simply return ``None`` instead
#     of crashing.
# ---------------------------------------------------------------------------

_jurisdiction_cfg = get_config_section("jurisdiction")

# Historical Sample Region-only fallback bbox — kept for retro-compat.
# Loaded from stop_grounding_config.json. A future province-specific
# equivalent should live in its own catalog / config entry.
_ruminahui_raw = _jurisdiction_cfg.get("ruminahui_bbox", {})
_SAMPLE_REGION_RUMINAHUI_BBOX = {
    "south": _ruminahui_raw.get("south", -0.395),
    "north": _ruminahui_raw.get("north", -0.285),
    "west": _ruminahui_raw.get("west", -78.48),
    "east": _ruminahui_raw.get("east", -78.39),
}


def _operator_matches(operator: str, province: str) -> bool:
    operator_lower = (operator or "").strip().lower()
    if not operator_lower:
        return False
    keywords = get_operator_keywords(province)
    if not keywords:
        return False
    return any(kw in operator_lower for kw in keywords)


def infer_stop_source(
    *,
    lat: float,
    lon: float,
    ref: str,
    operator: str,
    province: Optional[str] = None,
) -> Optional[str]:
    """
    Infer stop_source jurisdiction code for a stop.

    Province-aware:
      * ``province`` defaults to Sample Region for retro-compat when the
        caller doesn't pass one. In that case behavior is byte-identical
        to the pre-generalization implementation.
      * For any other province, the function reads
        ``operator_keywords`` and ``default_jurisdiction_codes`` from
        :mod:`datamind_core.province_config` and returns the *primary*
        declared jurisdiction when the operator matches, otherwise
        ``None``.

    Rules (Sample Region):
      * ``ref`` starts with "Q" or operator matches Sample Region operator
        keywords → "DMQ".
      * Stop inside the historical Rumiñahui bbox → "ANT".
      * Otherwise → ``None``.
    """
    province_key = (province or DEFAULT_PROVINCE).strip().lower() or DEFAULT_PROVINCE
    province_entry = get_province(province_key)
    default_codes = province_entry.get("default_jurisdiction_codes") or []

    # Primary jurisdiction code: first entry in default_jurisdiction_codes,
    # with Sample Region falling through to the historical "DMQ" literal to
    # guarantee byte-identical outputs for existing runs even if the
    # config file is missing.
    if default_codes:
        primary_code = str(default_codes[0])
    elif province_key == DEFAULT_PROVINCE:
        primary_code = "DMQ"
    else:
        primary_code = None

    ref_upper = (ref or "").strip().upper()

    # Sample Region-specific ref-prefix heuristic (Quito stops use "Q-…").
    if province_key == DEFAULT_PROVINCE and ref_upper.startswith("Q") and primary_code:
        return primary_code

    if _operator_matches(operator, province_key) and primary_code:
        return primary_code

    # Sample Region-specific Rumiñahui bbox fallback → "ANT" historical code.
    if province_key == DEFAULT_PROVINCE:
        if (_SAMPLE_REGION_RUMINAHUI_BBOX["south"] <= lat <= _SAMPLE_REGION_RUMINAHUI_BBOX["north"]
                and _SAMPLE_REGION_RUMINAHUI_BBOX["west"] <= lon <= _SAMPLE_REGION_RUMINAHUI_BBOX["east"]):
            # Use the second declared jurisdiction code if present
            # (Sample Region has ["DMQ", "ANT_SAMPLE_REGION"] but historical
            # output is just "ANT"), otherwise fall back to "ANT".
            return "ANT"

    return None


# ---------------------------------------------------------------------------
# Core PostGIS intersection query
# ---------------------------------------------------------------------------

_CORRIDOR_INTERSECTION_SQL = """
SELECT
    n.node_id::text    AS stop_id,
    COALESCE(
        NULLIF(BTRIM(p.canonical_name), ''),
        NULLIF(BTRIM(n.name), ''),
        NULLIF(BTRIM(n.ref), ''),
        'stop_' || LEFT(n.node_id::text, 8)
    ) AS stop_name,
    n.operator,
    p.region AS locality,
    p.place_id::text   AS place_id,
    n.ref,
    ST_Y(n.geom)       AS lat,
    ST_X(n.geom)       AS lon,
    COALESCE(route_stats.route_count, 0) AS route_count,
    ST_Distance(
        n.geom::geography,
        corridor.geom::geography
    ) AS distance_m,
    ST_LineLocatePoint(
        corridor.geom,
        n.geom::geometry
    ) AS path_fraction
FROM geo_prod.node_place_map m
JOIN node_prod.nodes n ON n.node_id = m.node_id
JOIN geo_prod.places p ON p.place_id = m.place_id
CROSS JOIN LATERAL (
        SELECT count(*) AS route_count
        FROM route_prod.routes r
        WHERE r.stop_node_ids IS NOT NULL
          AND n.node_id = ANY(r.stop_node_ids)
) route_stats
CROSS JOIN (SELECT ST_GeomFromGeoJSON(%(corridor_geojson)s) AS geom) corridor
WHERE n.node_type = 'STOP'
  AND p.status = 'active'
  AND (
      %(bbox_south)s IS NULL
      OR (
          ST_Y(n.geom) BETWEEN %(bbox_south)s AND %(bbox_north)s
          AND ST_X(n.geom) BETWEEN %(bbox_west)s AND %(bbox_east)s
      )
  )
  AND ST_DWithin(
      n.geom::geography,
      corridor.geom::geography,
      %(buffer_m)s
  )
ORDER BY path_fraction
"""


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2.0 * r * math.asin(math.sqrt(a))


def _compute_avg_gap_m(candidates: List[CorridorStopCandidate]) -> float:
    if len(candidates) < 2:
        return float("inf")
    total = 0.0
    for i in range(len(candidates) - 1):
        total += _haversine_m(
            candidates[i].lon, candidates[i].lat,
            candidates[i + 1].lon, candidates[i + 1].lat,
        )
    return total / (len(candidates) - 1)


_corridor_cfg = get_config_section("corridor")
_buffer_cfg = _corridor_cfg.get("buffer_passes", {})
_SHORT_ROUTE_KM = _buffer_cfg.get("short_route_km", 12)
_SHORT_ROUTE_BUFFERS = _buffer_cfg.get("short_route_buffers", [50, 100, 150])
_MEDIUM_ROUTE_KM = _buffer_cfg.get("medium_route_km", 25)
_MEDIUM_ROUTE_BUFFERS = _buffer_cfg.get("medium_route_buffers", [50, 100, 200])
_LONG_ROUTE_BUFFERS = _buffer_cfg.get("long_route_buffers", [50, 100, 200, 400])
_SPARSE_SEGMENT_GAP_M = _corridor_cfg.get("sparse_segment_gap_m", 1000)
_SPARSE_SEGMENT_GAP_FRACTION = _corridor_cfg.get("sparse_segment_gap_fraction", 0.15)
_MIN_DENSITY_GAP_M_DEFAULT = _corridor_cfg.get("min_density_gap_m", 500.0)


def _get_buffer_passes(corridor_length_km: float) -> List[int]:
    """Cap buffer widening by corridor length to reduce noise on short routes."""
    if corridor_length_km > 0 and corridor_length_km < _SHORT_ROUTE_KM:
        return list(_SHORT_ROUTE_BUFFERS)
    elif corridor_length_km > 0 and corridor_length_km < _MEDIUM_ROUTE_KM:
        return list(_MEDIUM_ROUTE_BUFFERS)
    else:
        return list(_LONG_ROUTE_BUFFERS)


def intersect_corridor_with_stops(
    corridor_geojson: Dict[str, Any],
    *,
    operator_id: Optional[int] = None,
    cooperative_id: Optional[int] = None,
    operator_name: Optional[str] = None,
    cooperative_name: Optional[str] = None,
    locality_hints: Optional[List[str]] = None,
    expected_envelope: Optional[Dict[str, Any]] = None,
    known_anchor_ids: Optional[Set[str]] = None,
    known_intermediate_ids: Optional[Set[str]] = None,
    buffer_passes: Optional[List[int]] = None,
    min_density_gap_m: float = _MIN_DENSITY_GAP_M_DEFAULT,
    corridor_length_km: float = 0.0,
    jurisdiction: str = "both",
    conn=None,
    province: Optional[str] = None,
) -> CorridorIntersectionResult:
    """
    Intersect a Valhalla corridor with the DB stop universe using
    progressive buffer widening.

    Returns all candidate stops ordered by path_fraction (0→1).
    """
    if buffer_passes is None:
        buffer_passes = _get_buffer_passes(corridor_length_km)

    known_anchors = known_anchor_ids or set()
    known_intermediates = known_intermediate_ids or set()
    corridor_json_str = json.dumps(corridor_geojson)
    bbox = dict((expected_envelope or {}).get("bbox") or {})
    operator_hint = str(operator_name or cooperative_name or "").strip().lower()
    locality_values = [
        str(value).strip().lower()
        for value in list(locality_hints or []) + list((expected_envelope or {}).get("expected_localities") or [])
        if str(value or "").strip()
    ]

    all_candidates: List[CorridorStopCandidate] = []
    seen_ids: Set[str] = set()
    final_buffer = buffer_passes[0]

    def _do_query(connection) -> CorridorIntersectionResult:
        nonlocal all_candidates, seen_ids, final_buffer

        for buffer_m in buffer_passes:
            final_buffer = buffer_m
            try:
                rows = fetch_all(
                    connection,
                    _CORRIDOR_INTERSECTION_SQL,
                    {
                        "corridor_geojson": corridor_json_str,
                        "buffer_m": buffer_m,
                        "bbox_south": bbox.get("south"),
                        "bbox_west": bbox.get("west"),
                        "bbox_north": bbox.get("north"),
                        "bbox_east": bbox.get("east"),
                    },
                )
            except Exception as exc:
                _LOG.error("Corridor intersection query failed at buffer %dm: %s", buffer_m, exc)
                continue

            excluded_by_jurisdiction = 0
            new_candidates = []
            for row in rows:
                sid = str(row["stop_id"])
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)

                # operator_name from query is text; compare with known operator
                row_operator = str(row.get("operator") or "").strip().lower()
                row_locality = str(row.get("locality") or "").strip().lower()

                # Infer stop_source and apply jurisdiction filter
                stop_source = infer_stop_source(
                    lat=float(row.get("lat") or 0),
                    lon=float(row.get("lon") or 0),
                    ref=str(row.get("ref") or ""),
                    operator=str(row.get("operator") or ""),
                )
                if jurisdiction != "both" and stop_source is not None and stop_source != jurisdiction:
                    excluded_by_jurisdiction += 1
                    continue

                candidate = CorridorStopCandidate(
                    stop_id=sid,
                    stop_name=str(row.get("stop_name") or ""),
                    locality=str(row.get("locality") or ""),
                    lat=float(row.get("lat") or 0),
                    lon=float(row.get("lon") or 0),
                    distance_to_corridor_m=float(row.get("distance_m") or 0),
                    path_fraction=float(row.get("path_fraction") or 0),
                    discovery_buffer_m=buffer_m,
                    is_known_anchor=(sid in known_anchors),
                    is_known_intermediate=(sid in known_intermediates),
                    operator_match=bool(
                        operator_hint and row_operator and (operator_hint in row_operator or row_operator in operator_hint)
                    ),
                    cooperative_match=bool(
                        operator_hint and row_operator and (operator_hint in row_operator or row_operator in operator_hint)
                    ),
                    locality_match=any(
                        value and row_locality and (value in row_locality or row_locality in value)
                        for value in locality_values
                    ),
                    place_id=row.get("place_id"),
                    ref=row.get("ref"),
                    in_expected_geography=(
                        point_in_bbox(
                            float(row.get("lon") or 0),
                            float(row.get("lat") or 0),
                            bbox,
                        )
                        if bbox
                        else True
                    ),
                    distance_to_envelope_m=(
                        distance_to_bbox_m(
                            float(row.get("lon") or 0),
                            float(row.get("lat") or 0),
                            bbox,
                        )
                        if bbox
                        else 0.0
                    ),
                    locality_consistency_score=locality_consistency_score(
                        locality=str(row.get("locality") or ""),
                        stop_name=str(row.get("stop_name") or ""),
                        envelope=expected_envelope,
                        province=province,
                    ),
                    stop_usage_frequency=float(row.get("route_count") or 0.0),
                    stop_source=stop_source,
                )
                new_candidates.append(candidate)

            if excluded_by_jurisdiction > 0:
                _LOG.info(
                    "Jurisdiction filter (%s): excluded %d stops at buffer=%dm",
                    jurisdiction, excluded_by_jurisdiction, buffer_m,
                )

            all_candidates.extend(new_candidates)
            all_candidates.sort(key=lambda c: c.path_fraction)

            # Check density — stop widening if sufficient
            if all_candidates:
                avg_gap = _compute_avg_gap_m(all_candidates)
                if avg_gap < min_density_gap_m:
                    _LOG.info(
                        "Corridor intersection: %d candidates at buffer=%dm, avg_gap=%.0fm — sufficient",
                        len(all_candidates), buffer_m, avg_gap,
                    )
                    break
                _LOG.info(
                    "Corridor intersection: %d candidates at buffer=%dm, avg_gap=%.0fm — widening",
                    len(all_candidates), buffer_m, avg_gap,
                )

        # Compute sparse segments
        sparse_segments = []
        if len(all_candidates) >= 2:
            for i in range(len(all_candidates) - 1):
                gap_m = _haversine_m(
                    all_candidates[i].lon, all_candidates[i].lat,
                    all_candidates[i + 1].lon, all_candidates[i + 1].lat,
                )
                gap_frac = all_candidates[i + 1].path_fraction - all_candidates[i].path_fraction
                if gap_m > _SPARSE_SEGMENT_GAP_M or gap_frac > _SPARSE_SEGMENT_GAP_FRACTION:
                    sparse_segments.append({
                        "after_stop_id": all_candidates[i].stop_id,
                        "before_stop_id": all_candidates[i + 1].stop_id,
                        "gap_m": round(gap_m, 1),
                        "gap_fraction": round(gap_frac, 4),
                    })

        # Coverage density (stops per km)
        total_length_km = 0.0
        if len(all_candidates) >= 2:
            for i in range(len(all_candidates) - 1):
                total_length_km += _haversine_m(
                    all_candidates[i].lon, all_candidates[i].lat,
                    all_candidates[i + 1].lon, all_candidates[i + 1].lat,
                ) / 1000.0
        density = len(all_candidates) / total_length_km if total_length_km > 0 else 0.0

        notes = (
            f"{len(all_candidates)} candidates found, buffer={final_buffer}m, "
            f"density={density:.1f} stops/km, {len(sparse_segments)} sparse segment(s)"
        )
        if expected_envelope:
            in_bounds = sum(1 for cand in all_candidates if cand.in_expected_geography)
            notes += f", in_bounds={in_bounds}/{len(all_candidates)}"

        return CorridorIntersectionResult(
            ordered_candidates=all_candidates,
            total_candidates_found=len(all_candidates),
            buffer_used_m=final_buffer,
            coverage_density=density,
            sparse_segments=sparse_segments,
            intersection_notes=notes,
        )

    def _run(connection):
        nonlocal jurisdiction, all_candidates, seen_ids, final_buffer
        result = _do_query(connection)
        if result.total_candidates_found == 0 and jurisdiction != "both":
            _LOG.warning(
                "Jurisdiction fallback: 0 candidates with jurisdiction=%s — relaxing to 'both' and retrying",
                jurisdiction,
            )
            jurisdiction = "both"
            all_candidates = []
            seen_ids = set()
            final_buffer = buffer_passes[0]
            result = _do_query(connection)
        return result

    if conn is not None:
        return _run(conn)

    with db_conn(readonly=True) as connection:
        return _run(connection)
