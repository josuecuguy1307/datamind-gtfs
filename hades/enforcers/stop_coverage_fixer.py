"""HADES Stop Coverage Fixer — produces v2 stop list that closes gaps.

Called by the re-entry worker in enhance mode. Given a v1 stop list and
its :class:`StopCoverageReport`, the Fixer walks every gap and INSERTS a
new stop chosen via a strict priority cascade:

  1. **Tier 1 / Tier 2** — already-resolved candidates from the report.
     The enforcer's cross-route-borrow and Overpass-POI resolvers populate
     ``gap.resolution.candidate_coord`` when a candidate was found; the
     Fixer simply inserts those.
  2. **DR landmark** — operator-curated deep-research responses, keyed by
     ``gap_idx`` in the ``dr_landmarks`` dict. Only consumed when the
     enforcer's tier 1+2 passes left the gap prepared-but-unresolved.
  3. **Synthetic fill** — last resort. For gaps with no DR response, the
     Fixer uses the enforcer's prepared ``synthetic_prepared`` midpoint as
     the v2 stop. Confidence is inherited from the enforcer prep.

After assembling the v2 stop list, the Fixer re-runs the enforcer on v2
and emits the candidate ONLY if its report is strictly better than v1:

    v2_strictly_better iff (
        rank(v2.classification) < rank(v1.classification)
        OR (
            rank(v2.classification) == rank(v1.classification)
            AND v2.n_gaps_unresolved < v1.n_gaps_unresolved
        )
    )

Classification ladder: good < acceptable < degraded < unroutable.

The Fixer is stateless and never writes to the DB — callers own the
approval_queue candidate and the ``fix_reports`` audit entry. Every
insertion is recorded in ``stops_added`` so the fix manifest captures
which tier / DR response / synthetic fill produced each new stop.

The Fixer does NOT call the resolvers itself. Callers construct the
enforcer (optionally with resolvers) before calling ``fix()`` — the
Fixer reuses the same enforcer for the post-fix report so downstream
tier counts (resolved vs. prepared) are consistent across v1 and v2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Sequence

from hades.enforcers.stop_coverage_enforcer import (
    DEFAULT_THRESHOLDS,
    Gap,
    StopCoverageEnforcer,
    StopCoverageReport,
    StopCoverageThresholds,
    ZONE_DEFAULTS,
    cumulative_m,
    project_point_to_polyline,
    haversine_m,
)


# Promotion strictness — the gate between v2 candidate and v2 proposal.
#
# ``strict`` (default) — v2 must strictly beat v1: either better classification
#   bucket, or same bucket with fewer unresolved gaps. Matches the original
#   Prompt 8 contract.
#
# ``relaxed`` — v2 is promoted whenever classification did not worsen AND at
#   least one Fixer fill was applied. The operator judges quality in the
#   approval queue. Use when the strict gate is silently dropping "meh-but-
#   not-worse" candidates the operator would rather see than lose.
#
# Unknown values raise in ``fix()``.
StrictnessMode = Literal["strict", "relaxed"]


# ---------------------------------------------------------------------------
# Classification ladder.
# ---------------------------------------------------------------------------

_CLASSIFICATION_RANK = {
    "good": 0,
    "acceptable": 1,
    "ship_pending_dr": 2,
    "degraded_minor": 3,
    "degraded": 4,
    "unroutable": 5,
}


# ---------------------------------------------------------------------------
# Outlier detection.
# ---------------------------------------------------------------------------

def _distance_to_polyline_m(
    lat: float, lon: float, coords: Sequence[tuple[float, float]]
) -> float:
    """Approximate closest-point distance from (lat, lon) to the polyline.

    Uses haversine to each vertex; that's a lower-bound on segment
    distance but tight enough for outlier flagging (coarse threshold).
    """
    best = float("inf")
    for clon, clat in coords:
        d = haversine_m(float(lat), float(lon), float(clat), float(clon))
        if d < best:
            best = d
    return best


def _infer_zone(route_length_m: float, thresholds: StopCoverageThresholds) -> str:
    if route_length_m <= thresholds.urban_dense_max_length_m:
        return "urban_dense"
    if route_length_m <= thresholds.urban_peripheral_max_length_m:
        return "urban_peripheral"
    if route_length_m <= thresholds.rural_max_length_m:
        return "rural"
    return "interprovincial"


def _distance_to_polyline_segment_m(
    lat: float, lon: float, coords: Sequence[tuple[float, float]]
) -> float:
    """Approximate closest-point distance — reuse the vertex-min heuristic."""
    best = float("inf")
    for clon, clat in coords:
        d = haversine_m(float(lat), float(lon), float(clat), float(clon))
        if d < best:
            best = d
    return best


def rebuild_from_scratch(
    *,
    coords: Sequence[tuple[float, float]],  # (lon, lat)
    thresholds: StopCoverageThresholds = DEFAULT_THRESHOLDS,
    dr_landmarks: Optional[dict[int, dict[str, Any]]] = None,
    type1_anchors: Optional[list[dict[str, Any]]] = None,
    cross_route_resolver: Optional[Any] = None,  # CrossRouteResolverFn
    overpass_resolver: Optional[Any] = None,     # OverpassResolverFn
    on_corridor_m: float = 60.0,
    min_separation_m: float = 40.0,
) -> tuple[list[tuple[float, float]], list[str], dict[str, Any]]:
    """Build a fresh stop list from the polyline + all available anchor pools.

    Algorithm:

    1. Infer zone from polyline length → target spacing = zone.good_gap_m.
    2. Build candidate pool from all sources, annotate each with
       ``projected_cum_m`` and ``dist_to_corridor_m``.
    3. Drop anything not on-corridor (> ``on_corridor_m``).
    4. Sort candidates by ``projected_cum_m``.
    5. Walk the polyline in target-spacing steps. At each step, pick the
       closest-by-cum_m *unused* candidate within ±target/2. If none:
       use the polyline vertex at that position as a synthetic fallback.
    6. Post-process: dedup anything <min_separation_m apart (prefer
       higher-tier source).
    7. Ensure start + end endpoints are covered.

    Returns ``(stops_latlon, stop_ids, audit)``. Stops are (lat, lon)
    to match the enforcer's expected shape. IDs encode the source:
    ``scratch_<i>_<source>``.
    """
    if len(coords) < 2:
        return [], [], {"reason": "polyline_too_short"}

    cum = cumulative_m(coords)
    total_len = cum[-1] if cum else 0.0
    zone = _infer_zone(total_len, thresholds)
    zone_t = thresholds.zone_to_thresholds.get(zone)
    target = zone_t.good_gap_m if zone_t else 800.0

    source_rank = {
        "type1_grounding": 0,  # highest — human-validated termini + anchors
        "type2_dr":        1,  # DR landmarks validated
        "cross_route":     2,  # node_prod existing stops
        "osm_poi":         3,  # Overpass bus_stop/platform
        "synthetic":       4,  # polyline vertex fallback
    }

    # Build candidate pool
    candidates: list[dict[str, Any]] = []

    if type1_anchors:
        for a in type1_anchors:
            lat, lon = a.get("lat"), a.get("lon")
            if lat is None or lon is None: continue
            candidates.append({
                "lat": float(lat), "lon": float(lon),
                "source": "type1_grounding",
                "name": a.get("name"),
                "seq_idx": a.get("sequence_index"),
                "conf": 0.9,
            })

    if dr_landmarks:
        for gi, lm in dr_landmarks.items():
            lat, lon = lm.get("lat"), lm.get("lon")
            if lat is None or lon is None: continue
            candidates.append({
                "lat": float(lat), "lon": float(lon),
                "source": "type2_dr",
                "name": lm.get("landmark_name"),
                "gap_idx": gi,
                "conf": float(lm.get("final_confidence") or 0.6),
            })

    if cross_route_resolver is not None:
        step = max(target / 2.0, 300.0)
        pos = 0.0
        exclude: list[str] = []
        while pos <= total_len:
            idx = _idx_at_cum(cum, pos)
            if 0 <= idx < len(coords):
                plon, plat = coords[idx]
                try:
                    rows = cross_route_resolver(
                        lat=float(plat), lon=float(plon),
                        buffer_m=thresholds.borrow_buffer_m,
                        route_corridor_coords=list(coords),
                        corridor_buffer_m=thresholds.corridor_buffer_m,
                        exclude_stop_ids=exclude,
                    ) or []
                except Exception:
                    rows = []
                for r in rows:
                    candidates.append({
                        "lat": float(r["lat"]), "lon": float(r["lon"]),
                        "source": "cross_route",
                        "name": r.get("name"),
                        "node_id": r.get("node_id"),
                        "conf": 0.75,
                    })
            pos += step

    if overpass_resolver is not None:
        step = max(target, 400.0)
        pos = 0.0
        while pos <= total_len:
            idx = _idx_at_cum(cum, pos)
            if 0 <= idx < len(coords):
                plon, plat = coords[idx]
                try:
                    pois = overpass_resolver(
                        float(plat), float(plon), float(thresholds.overpass_buffer_m)
                    ) or []
                except Exception:
                    pois = []
                for p in pois:
                    candidates.append({
                        "lat": float(p["lat"]), "lon": float(p["lon"]),
                        "source": "osm_poi",
                        "name": p.get("name"),
                        "osm_id": p.get("osm_id"),
                        "conf": 0.65,
                    })
            pos += step

    # Annotate + filter on-corridor
    annotated: list[dict[str, Any]] = []
    for c in candidates:
        d_corr = _distance_to_polyline_segment_m(c["lat"], c["lon"], coords)
        if d_corr > on_corridor_m:
            continue
        proj, _ = project_point_to_polyline(c["lat"], c["lon"], coords, cum)
        c["proj_cum_m"] = proj
        c["d_corr_m"] = d_corr
        annotated.append(c)
    annotated.sort(key=lambda c: (c["proj_cum_m"], source_rank.get(c["source"], 99)))

    # Walk polyline in target-spacing steps; pick closest unused candidate
    chosen: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    pos = 0.0
    while pos <= total_len + 1:
        best: Optional[dict[str, Any]] = None
        best_i: int = -1
        best_score: Optional[tuple[int, float, float]] = None
        for i, c in enumerate(annotated):
            if i in used_ids:
                continue
            if abs(c["proj_cum_m"] - pos) > target / 2:
                continue
            # score: prefer higher-tier (lower rank), then closer to pos, then on-corridor
            score = (source_rank.get(c["source"], 99), abs(c["proj_cum_m"] - pos), c["d_corr_m"])
            if best_score is None or score < best_score:
                best_score, best, best_i = score, c, i
        if best is not None:
            chosen.append(best)
            used_ids.add(best_i)
            pos = best["proj_cum_m"] + target
        else:
            # No candidate in window — synthesize a vertex point
            idx = _idx_at_cum(cum, pos)
            if 0 <= idx < len(coords):
                plon, plat = coords[idx]
                chosen.append({
                    "lat": float(plat), "lon": float(plon),
                    "source": "synthetic",
                    "proj_cum_m": cum[idx] if idx < len(cum) else pos,
                    "d_corr_m": 0.0,
                    "conf": 0.3,
                })
            pos += target

    # Ensure endpoints are included
    if chosen:
        if chosen[0]["proj_cum_m"] > target / 3:
            chosen.insert(0, {
                "lat": float(coords[0][1]), "lon": float(coords[0][0]),
                "source": "synthetic_start", "proj_cum_m": 0.0, "d_corr_m": 0.0, "conf": 0.3,
            })
        if chosen[-1]["proj_cum_m"] < total_len - target / 3:
            chosen.append({
                "lat": float(coords[-1][1]), "lon": float(coords[-1][0]),
                "source": "synthetic_end", "proj_cum_m": total_len, "d_corr_m": 0.0, "conf": 0.3,
            })

    # Dedupe <min_separation_m
    chosen.sort(key=lambda s: s["proj_cum_m"])
    dedup: list[dict[str, Any]] = []
    for s in chosen:
        if dedup and abs(s["proj_cum_m"] - dedup[-1]["proj_cum_m"]) < min_separation_m:
            # Keep higher-rank (lower rank number)
            prev_rank = source_rank.get(dedup[-1]["source"], 99)
            cur_rank = source_rank.get(s["source"], 99)
            if cur_rank < prev_rank:
                dedup[-1] = s
            continue
        dedup.append(s)

    stops_latlon = [(s["lat"], s["lon"]) for s in dedup]
    stop_ids = [f"scratch_{i:03d}_{s['source']}" for i, s in enumerate(dedup)]

    audit = {
        "zone": zone,
        "target_spacing_m": target,
        "polyline_length_m": round(total_len, 1),
        "n_stops_built": len(dedup),
        "source_counts": {
            src: sum(1 for s in dedup if s["source"] == src)
            for src in ("type1_grounding","type2_dr","cross_route","osm_poi","synthetic","synthetic_start","synthetic_end")
        },
        "total_candidates_seen": len(candidates),
        "on_corridor_candidates": len(annotated),
    }
    return stops_latlon, stop_ids, audit


def _idx_at_cum(cum: list[float], target_m: float) -> int:
    """Return the polyline vertex index whose cum_m is closest to target."""
    if not cum:
        return -1
    if target_m <= cum[0]:
        return 0
    if target_m >= cum[-1]:
        return len(cum) - 1
    # linear scan is fine for O(n) polylines we see
    for i in range(1, len(cum)):
        if cum[i] >= target_m:
            # pick the closer of (i-1, i)
            return i - 1 if (target_m - cum[i-1]) < (cum[i] - target_m) else i
    return len(cum) - 1


def _clean_outlier_stops(
    *,
    stops_list: list[tuple[float, float]],
    ids_list: list[str],
    coords: Sequence[tuple[float, float]],
    dr_landmarks: Optional[dict[int, dict[str, Any]]] = None,
    outlier_threshold_m: float = 120.0,
    fallback_radius_m: float = 250.0,
    on_corridor_buffer_m: float = 60.0,
) -> tuple[
    list[tuple[float, float]],
    list[str],
    dict[str, Any],
]:
    """Scan v1 stops; for any stop whose distance to the polyline exceeds
    ``outlier_threshold_m`` (i.e. literally off the route's trace), try to
    replace with a nearby DR landmark that is on-corridor. If no valid
    replacement exists, drop the stop.

    Returns ``(cleaned_stops, cleaned_ids, audit)`` where ``audit`` is
    ``{"dropped": [...], "replaced": [...], "kept": N}``.
    """
    if not stops_list or len(coords) < 2:
        return list(stops_list), list(ids_list), {"dropped": [], "replaced": [], "kept": len(stops_list)}

    # Candidate pool for replacements = all DR landmarks for this route
    # (Type 2 validated). Each has {lat, lon, landmark_name?, ...}.
    candidates: list[dict[str, Any]] = []
    if dr_landmarks:
        for gap_idx, lm in dr_landmarks.items():
            if lm.get("lat") is None or lm.get("lon") is None:
                continue
            candidates.append({
                "lat": float(lm["lat"]), "lon": float(lm["lon"]),
                "source": "dr_landmark", "gap_idx": gap_idx,
                "name": lm.get("landmark_name"),
            })

    dropped: list[dict[str, Any]] = []
    replaced: list[dict[str, Any]] = []
    cleaned_stops: list[tuple[float, float]] = []
    cleaned_ids: list[str] = []

    for i, (lat, lon) in enumerate(stops_list):
        sid = ids_list[i] if i < len(ids_list) else f"v1_{i}"
        d = _distance_to_polyline_m(lat, lon, coords)
        if d <= outlier_threshold_m:
            cleaned_stops.append((lat, lon))
            cleaned_ids.append(sid)
            continue

        # Outlier — attempt replacement.
        # Best candidate = DR landmark within fallback_radius_m of the outlier
        # AND within on_corridor_buffer_m of the polyline.
        best = None
        best_score = float("inf")
        for c in candidates:
            # proximity to outlier
            d_out = haversine_m(lat, lon, c["lat"], c["lon"])
            if d_out > fallback_radius_m:
                continue
            # on-corridor?
            d_corr = _distance_to_polyline_m(c["lat"], c["lon"], coords)
            if d_corr > on_corridor_buffer_m:
                continue
            # score = d_corr + 0.3 * d_out (prefer on-corridor, then nearby)
            score = d_corr + 0.3 * d_out
            if score < best_score:
                best_score, best = score, {**c, "d_out": round(d_out, 1), "d_corr": round(d_corr, 1)}

        if best is not None:
            cleaned_stops.append((best["lat"], best["lon"]))
            cleaned_ids.append(f"replaced_outlier_{i}")
            replaced.append({
                "original_idx": i, "original_id": sid,
                "original_coord": [lat, lon], "original_dist_m": round(d, 1),
                "replacement_coord": [best["lat"], best["lon"]],
                "replacement_source": best["source"],
                "replacement_name": best.get("name"),
            })
        else:
            dropped.append({
                "original_idx": i, "original_id": sid,
                "original_coord": [lat, lon], "distance_to_polyline_m": round(d, 1),
                "reason": "outlier_no_replacement_candidate",
            })

    audit = {
        "dropped": dropped,
        "replaced": replaced,
        "kept": len(cleaned_stops) - len(replaced),
        "outlier_threshold_m": outlier_threshold_m,
        "fallback_radius_m": fallback_radius_m,
        "on_corridor_buffer_m": on_corridor_buffer_m,
    }
    return cleaned_stops, cleaned_ids, audit


# ---------------------------------------------------------------------------
# Result schema.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class StopCoverageFixResult:
    success: bool
    # "improved" | "relaxed_improved" | "already_good" | "no_fillable_gaps" | "would_regress"
    reason: str
    stops_before: list[tuple[float, float]]  # (lat, lon)
    stops_after: list[tuple[float, float]]
    stop_ids_before: list[str]
    stop_ids_after: list[str]
    report_before: StopCoverageReport
    report_after: Optional[StopCoverageReport]
    stops_added: list[dict[str, Any]] = field(default_factory=list)
    outlier_audit: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": bool(self.success),
            "reason": self.reason,
            "stops_before": len(self.stops_before),
            "stops_after": len(self.stops_after),
            "stops_added_count": len(self.stops_added),
            "stops_added": list(self.stops_added),
            "report_before": self.report_before.to_dict(),
            "report_after": (
                self.report_after.to_dict() if self.report_after is not None else None
            ),
            "outlier_audit": self.outlier_audit,
        }


# ---------------------------------------------------------------------------
# Fill-selection cascade.
# ---------------------------------------------------------------------------

def _select_fill_for_gap(
    gap: Gap,
    report_before: StopCoverageReport,
    dr_landmarks: Optional[dict[int, dict[str, Any]]],
) -> Optional[dict[str, Any]]:
    """Return the fill dict for ``gap`` (or None if nothing to fill).

    Output keys: ``gap_idx``, ``tier``, ``source``, ``lat``, ``lon``,
    ``metadata``. ``source`` is one of ``cross_route_borrow`` /
    ``osm_poi`` / ``dr_landmark`` / ``synthetic``. Tiers match the
    enforcer's nomenclature (1, 2, 4, 5).
    """
    res = gap.resolution

    # Tier 1 / Tier 2 — enforcer already resolved; just apply.
    if res is not None and res.resolved and res.candidate_coord is not None:
        lat, lon = res.candidate_coord
        return {
            "gap_idx": gap.idx,
            "tier": res.tier,
            "source": res.tier_label,
            "lat": float(lat),
            "lon": float(lon),
            "metadata": dict(res.candidate_metadata),
        }

    # Tier 4 — DR landmark, if operator-curated response is available.
    if dr_landmarks and gap.idx in dr_landmarks:
        lm = dr_landmarks[gap.idx]
        lat = lm.get("lat")
        lon = lm.get("lon")
        if lat is not None and lon is not None:
            meta = {k: v for k, v in lm.items() if k not in ("lat", "lon")}
            meta.setdefault("source", "dr_landmark")
            return {
                "gap_idx": gap.idx,
                "tier": 4,
                "source": "dr_landmark",
                "lat": float(lat),
                "lon": float(lon),
                "metadata": meta,
            }

    # Tier 5 — synthetic fill. The enforcer prepared a midpoint in
    # report.synthetic_prepared for this gap; find it by gap_idx.
    synthetic = next(
        (s for s in report_before.synthetic_prepared if s.get("gap_idx") == gap.idx),
        None,
    )
    if synthetic is not None:
        return {
            "gap_idx": gap.idx,
            "tier": 5,
            "source": "synthetic",
            "lat": float(synthetic["lat"]),
            "lon": float(synthetic["lon"]),
            "metadata": {
                "synthetic_confidence": synthetic.get("synthetic_confidence", "medium"),
            },
        }

    # Nothing prepared for this gap (shouldn't happen given enforcer's
    # current logic — it always prepares a synthetic fallback — but the
    # Fixer stays defensive in case the enforcer contract shifts).
    return None


# ---------------------------------------------------------------------------
# Classification comparator.
# ---------------------------------------------------------------------------

def _strictly_better(
    before: StopCoverageReport, after: StopCoverageReport
) -> bool:
    b_rank = _CLASSIFICATION_RANK.get(before.classification, 99)
    a_rank = _CLASSIFICATION_RANK.get(after.classification, 99)
    if a_rank < b_rank:
        return True
    if a_rank > b_rank:
        return False
    if after.n_gaps_unresolved != before.n_gaps_unresolved:
        return after.n_gaps_unresolved < before.n_gaps_unresolved
    # Same class, same unresolved count — the v2 classifier rates a
    # tier-1/2-resolved-in-enforcer route as "good", so we need a final
    # tiebreaker that catches the "we actually spliced the borrowed
    # stop into the route" delta. Fewer total gaps after the apply
    # means the splice took effect.
    return len(after.gaps) < len(before.gaps)


def _relaxed_ok(
    before: StopCoverageReport,
    after: StopCoverageReport,
    stops_added: list[dict[str, Any]],
) -> bool:
    """Relaxed-mode gate: promote iff classification did not worsen AND at
    least one Fixer fill was applied. The v2 report may still classify
    ``degraded`` — operator decides on review whether the inserted stops
    are worth accepting.
    """
    if not stops_added:
        return False
    b_rank = _CLASSIFICATION_RANK.get(before.classification, 99)
    a_rank = _CLASSIFICATION_RANK.get(after.classification, 99)
    if a_rank > b_rank:
        return False
    return True


# ---------------------------------------------------------------------------
# Public Fixer.
# ---------------------------------------------------------------------------

class StopCoverageFixer:
    """Produce a v2 stop list that strictly improves coverage over v1.

    The Fixer is the only component permitted to insert new stops into a
    route during enhance mode. It never writes to the database; the caller
    is responsible for queueing the v2 candidate + fix manifest.
    """

    def __init__(
        self,
        enforcer: Optional[StopCoverageEnforcer] = None,
        thresholds: StopCoverageThresholds = DEFAULT_THRESHOLDS,
    ):
        self.thresholds = thresholds
        self._enforcer = enforcer or StopCoverageEnforcer(thresholds)

    def fix(
        self,
        *,
        route_code: str,
        coords: Sequence[tuple[float, float]],
        stop_coords: Sequence[tuple[float, float]],
        stop_ids: Optional[Sequence[str]] = None,
        zone: Optional[str] = None,
        report_before: Optional[StopCoverageReport] = None,
        dr_landmarks: Optional[dict[int, dict[str, Any]]] = None,
        strictness: StrictnessMode = "strict",
        clean_outliers: bool = False,
        outlier_threshold_m: float = 120.0,
        mode: Literal["gap_fill", "scratch"] = "gap_fill",
        type1_anchors: Optional[list[dict[str, Any]]] = None,
        cross_route_resolver: Optional[Any] = None,
        overpass_resolver: Optional[Any] = None,
    ) -> StopCoverageFixResult:
        stops_list: list[tuple[float, float]] = [
            (float(lat), float(lon)) for (lat, lon) in stop_coords
        ]
        ids_list: list[str] = list(stop_ids) if stop_ids is not None else []

        # Scratch reconstruction: discard v1 stops, build fresh from all
        # anchor pools + polyline. Runs enforcer on both v1 and v2 to
        # compare, then applies the promotion gate.
        scratch_audit: Optional[dict[str, Any]] = None
        if mode == "scratch":
            v2_stops, v2_ids, scratch_audit = rebuild_from_scratch(
                coords=coords,
                thresholds=self.thresholds,
                dr_landmarks=dr_landmarks,
                type1_anchors=type1_anchors,
                cross_route_resolver=cross_route_resolver,
                overpass_resolver=overpass_resolver,
            )
            # Analyze v1 (what we had before) + v2 (scratch-built)
            report_v1 = report_before or self._enforcer.analyze(
                route_code=route_code,
                coords=coords,
                stop_coords=stops_list,
                stop_ids=ids_list or None,
                zone=zone,
            )
            report_v2 = self._enforcer.analyze(
                route_code=route_code,
                coords=coords,
                stop_coords=v2_stops,
                stop_ids=v2_ids,
                zone=zone,
            )
            # Promotion: always use relaxed-style check in scratch mode —
            # v2 is an entirely different stop list, so "strictly better"
            # (same-class fewer-unresolved) is the wrong lens. Require only
            # that class did not worsen and at least 1 stop was built.
            b_rank = _CLASSIFICATION_RANK.get(report_v1.classification, 99)
            a_rank = _CLASSIFICATION_RANK.get(report_v2.classification, 99)
            if a_rank <= b_rank and v2_stops:
                # Flatten the scratch source_counts into a stops_added-like
                # list so downstream logging still has per-source breakdown,
                # but keep it as a single summary dict (not per-stop).
                source_counts = scratch_audit.get("source_counts") or {}
                stops_added_summary = [
                    {"source": src, "count": cnt}
                    for src, cnt in source_counts.items() if cnt
                ]
                return StopCoverageFixResult(
                    success=True,
                    reason=f"scratch_built(n={len(v2_stops)},zone={scratch_audit.get('zone')})",
                    stops_before=stops_list,
                    stops_after=v2_stops,
                    stop_ids_before=list(ids_list),
                    stop_ids_after=v2_ids,
                    report_before=report_v1,
                    report_after=report_v2,
                    stops_added=stops_added_summary,
                    outlier_audit={"scratch_audit": scratch_audit},
                )
            return StopCoverageFixResult(
                success=False,
                reason=(
                    f"scratch_no_improvement(a_rank={a_rank},b_rank={b_rank},n_stops={len(v2_stops)})"
                ),
                stops_before=stops_list,
                stops_after=list(stops_list),
                stop_ids_before=list(ids_list),
                stop_ids_after=list(ids_list),
                report_before=report_v1,
                report_after=report_v2,
                outlier_audit={"scratch_audit": scratch_audit},
            )

        # Optional outlier cleanup BEFORE the enforcer analyses v1.
        # A stop > outlier_threshold_m from the polyline is off-route; we try
        # to swap it for a DR-landmark replacement, else drop it entirely.
        outlier_audit: Optional[dict[str, Any]] = None
        if clean_outliers and stops_list:
            cleaned_stops, cleaned_ids, outlier_audit = _clean_outlier_stops(
                stops_list=stops_list,
                ids_list=ids_list,
                coords=coords,
                dr_landmarks=dr_landmarks,
                outlier_threshold_m=outlier_threshold_m,
            )
            stops_list = cleaned_stops
            ids_list = cleaned_ids

        report_v1 = report_before or self._enforcer.analyze(
            route_code=route_code,
            coords=coords,
            stop_coords=stops_list,
            stop_ids=ids_list or None,
            zone=zone,
        )

        # Short-circuit only when there is genuinely nothing to fix.
        # v2-classifier "good" can also fire when every gap was tier-1/2
        # resolved by the enforcer — those resolutions still need the
        # fixer to splice the candidate stops into stops_list.
        if not report_v1.gaps:
            return StopCoverageFixResult(
                success=False,
                reason="already_good",
                stops_before=stops_list,
                stops_after=list(stops_list),
                stop_ids_before=list(ids_list),
                stop_ids_after=list(ids_list),
                report_before=report_v1,
                report_after=None,
                outlier_audit=outlier_audit,
            )

        fills: list[dict[str, Any]] = []
        for gap in report_v1.gaps:
            if gap.resolution is not None and gap.resolution.resolved:
                fill = _select_fill_for_gap(gap, report_v1, dr_landmarks)
                if fill is not None:
                    fills.append(fill)
                continue
            # Unresolved (tier 4 prepared, tier 5 prepared, or truly
            # unresolved). Try DR, then synthetic.
            fill = _select_fill_for_gap(gap, report_v1, dr_landmarks)
            if fill is not None:
                fills.append(fill)

        if not fills:
            return StopCoverageFixResult(
                success=False,
                reason="no_fillable_gaps",
                stops_before=stops_list,
                stops_after=list(stops_list),
                stop_ids_before=list(ids_list),
                stop_ids_after=list(ids_list),
                report_before=report_v1,
                report_after=None,
                outlier_audit=outlier_audit,
            )

        # Reinforced sequence insertion.
        #
        # Each fill must land in the cum_m window between its gap's flanking
        # stops (with a tolerance), OR it gets dropped as "off-corridor".
        # After the merge, stops within MIN_SEPARATION_M of each other are
        # deduplicated (originals beat fills — we never drop an original
        # stop for a fill). This prevents three known failure modes:
        #  - a Tier 1 borrow whose projection lands in a different gap
        #    because the route loops back on itself (same cum_m both ways);
        #  - a DR landmark whose approx_lat/lon is off the real corridor
        #    but the enforcer couldn't catch it at validation time;
        #  - two tiers filling the same gap with stops 5 m apart.
        cum = cumulative_m(coords)
        TOL_M = 80.0  # window tolerance beyond prev/next stop cum_m
        MIN_SEPARATION_M = 25.0  # dedup threshold after merge

        # 1. Project originals + remember each gap's prev/next cum_m window.
        original_rows: list[tuple[float, tuple[float, float], str, str]] = []
        for i, (lat, lon) in enumerate(stops_list):
            proj_cum, _ = project_point_to_polyline(lat, lon, coords, cum)
            sid = ids_list[i] if i < len(ids_list) else f"v1_{i}"
            original_rows.append((proj_cum, (lat, lon), sid, "original"))

        gap_windows: dict[int, tuple[float, float]] = {}
        for g in report_v1.gaps:
            pi, ni = int(g.prev_stop_idx), int(g.next_stop_idx)
            if 0 <= pi < len(original_rows) and 0 <= ni < len(original_rows):
                a = original_rows[pi][0]
                b = original_rows[ni][0]
                gap_windows[int(g.idx)] = (min(a, b), max(a, b))

        # 2. Validate each fill against its gap window. Drop off-corridor.
        accepted_fills: list[tuple[float, tuple[float, float], str, str]] = []
        rejected_fills: list[dict[str, Any]] = []
        for fill in fills:
            proj_cum, _ = project_point_to_polyline(
                fill["lat"], fill["lon"], coords, cum
            )
            fill["projected_cum_m"] = round(proj_cum, 2)
            gi = int(fill.get("gap_idx", -1))
            window = gap_windows.get(gi)
            if window is None:
                fill["sequence_rejected"] = "no_gap_window"
                rejected_fills.append(fill)
                continue
            lo, hi = window
            if proj_cum < lo - TOL_M or proj_cum > hi + TOL_M:
                fill["sequence_rejected"] = (
                    f"off_corridor_for_gap_{gi} "
                    f"(proj={proj_cum:.0f} window=[{lo:.0f},{hi:.0f}] tol={TOL_M:.0f})"
                )
                rejected_fills.append(fill)
                continue
            sid = f"fix_gap{gi}_tier{fill['tier']}"
            accepted_fills.append(
                (proj_cum, (fill["lat"], fill["lon"]), sid, "fill")
            )

        # 3. Merge + sort + dedupe (originals win ties).
        combined_raw = sorted(original_rows + accepted_fills, key=lambda r: r[0])
        combined: list[tuple[float, tuple[float, float], str]] = []
        kinds: list[str] = []
        for cum_m, coord, sid, kind in combined_raw:
            if combined and abs(cum_m - combined[-1][0]) < MIN_SEPARATION_M:
                # Duplicate or near-dup. Prefer original over fill.
                prev_kind = kinds[-1]
                if kind == "original" and prev_kind == "fill":
                    combined[-1] = (cum_m, coord, sid)
                    kinds[-1] = kind
                # else drop the new one
                continue
            combined.append((cum_m, coord, sid))
            kinds.append(kind)

        # 4. Effective fills = those still present after dedup.
        effective_fill_sids = {
            sid for (_, _, sid), kind in zip(combined, kinds) if kind == "fill"
        }
        fills = [
            f for f in fills
            if f"fix_gap{int(f.get('gap_idx', -1))}_tier{f['tier']}" in effective_fill_sids
        ]
        # Annotate rejections on the fixer output via stops_added metadata.
        for r in rejected_fills:
            r["applied"] = False

        stops_after = [row[1] for row in combined]
        ids_after = [row[2] for row in combined]

        report_v2 = self._enforcer.analyze(
            route_code=route_code,
            coords=coords,
            stop_coords=stops_after,
            stop_ids=ids_after,
            zone=zone,
        )

        if strictness not in ("strict", "relaxed"):
            raise ValueError(f"strictness={strictness!r} invalid; expected 'strict' or 'relaxed'")

        promote = (
            _strictly_better(report_v1, report_v2)
            if strictness == "strict"
            else _relaxed_ok(report_v1, report_v2, fills)
        )
        if promote:
            return StopCoverageFixResult(
                success=True,
                reason="improved" if strictness == "strict" else "relaxed_improved",
                stops_before=stops_list,
                stops_after=stops_after,
                stop_ids_before=list(ids_list),
                stop_ids_after=ids_after,
                report_before=report_v1,
                report_after=report_v2,
                stops_added=fills,
                outlier_audit=outlier_audit,
            )

        return StopCoverageFixResult(
            success=False,
            reason="would_regress",
            stops_before=stops_list,
            stops_after=list(stops_list),
            stop_ids_before=list(ids_list),
            stop_ids_after=list(ids_list),
            report_before=report_v1,
            report_after=report_v2,
            stops_added=fills,
            outlier_audit=outlier_audit,
        )


# ---------------------------------------------------------------------------
# Convenience wrapper.
# ---------------------------------------------------------------------------

def fix_stop_coverage(
    *,
    route_code: str,
    coords: Sequence[tuple[float, float]],
    stop_coords: Sequence[tuple[float, float]],
    stop_ids: Optional[Sequence[str]] = None,
    zone: Optional[str] = None,
    dr_landmarks: Optional[dict[int, dict[str, Any]]] = None,
    enforcer: Optional[StopCoverageEnforcer] = None,
    thresholds: StopCoverageThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    fixer = StopCoverageFixer(enforcer=enforcer, thresholds=thresholds)
    return fixer.fix(
        route_code=route_code,
        coords=coords,
        stop_coords=stop_coords,
        stop_ids=stop_ids,
        zone=zone,
        dr_landmarks=dr_landmarks,
    ).to_dict()


__all__ = [
    "StopCoverageFixer",
    "StopCoverageFixResult",
    "fix_stop_coverage",
]
