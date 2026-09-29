"""
Stage G — Geometry Candidate Derivation

Derives the final geometry candidate from the Valhalla corridor,
optionally refining it with the full discovered sequence.

BUG-013 fix: Always use full corridor as geometry base. Segments without
stop coverage inherit corridor geometry directly. The refined corridor
(built from skeleton stops only) is used ONLY when it covers the full
route extent; otherwise the initial corridor is kept.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    CorridorResult,
    GeometryCandidate,
    SequenceSkeleton,
)
from datamind_console.phases.phase3_routes.stop_grounding.geography_guardrails import (
    evaluate_corridor_geography,
)

_LOG = logging.getLogger(__name__)

LonLat = Tuple[float, float]


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2.0 * r * math.asin(math.sqrt(a))


def _linestring_length_km(coords: List[LonLat]) -> float:
    total = 0.0
    for i in range(len(coords) - 1):
        total += _haversine_m(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
    return total / 1000.0


def _compute_consistency(
    skeleton: SequenceSkeleton,
    corridor_geojson: Dict[str, Any],
) -> float:
    """
    How well does the corridor match the sequence?
    Measures average distance from each skeleton stop to the corridor.
    """
    coords = corridor_geojson.get("coordinates", [])
    if not coords or not skeleton.ordered_stops:
        return 0.0

    total_dist = 0.0
    for stop in skeleton.ordered_stops:
        # Find closest corridor point
        min_dist = float("inf")
        for coord in coords:
            d = _haversine_m(stop.lon, stop.lat, coord[0], coord[1])
            if d < min_dist:
                min_dist = d
        total_dist += min_dist

    avg_dist = total_dist / len(skeleton.ordered_stops)
    # Score: 1.0 if avg_dist < 30m, decreasing to 0 at 300m
    return max(0.0, min(1.0, 1.0 - (avg_dist - 30.0) / 270.0))


def _stop_extent_fraction(skeleton: SequenceSkeleton) -> Tuple[float, float]:
    """Return (first_stop_pf, last_stop_pf) from skeleton."""
    if not skeleton.ordered_stops:
        return 0.0, 0.0
    return (
        skeleton.ordered_stops[0].path_fraction,
        skeleton.ordered_stops[-1].path_fraction,
    )


def derive_geometry_candidate(
    initial_corridor: CorridorResult,
    skeleton: SequenceSkeleton,
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    refine_with_valhalla: bool = True,
    timeout_s: int = 60,
) -> GeometryCandidate:
    """
    Derive the geometry candidate from the corridor + discovered sequence.

    BUG-013: Always uses the full initial corridor as the geometry base.
    Refinement with skeleton stops is only applied when the refined corridor
    covers >=90% of the initial corridor length. Otherwise, the initial
    corridor geometry is kept intact to prevent clipping.
    """
    if not initial_corridor.corridor_geojson:
        return GeometryCandidate(
            notes="No corridor available — cannot derive geometry",
        )

    initial_km = initial_corridor.total_length_km or 0.0
    first_pf, last_pf = _stop_extent_fraction(skeleton)

    # Log inherited segments (BUG-013 Rule 4)
    if first_pf > 0.08 and skeleton.ordered_stops:
        inherited_start_km = first_pf * initial_km
        _LOG.info(
            "[GEO INHERIT START] route pf=0.0→%.3f (%.1fkm) inherited from corridor",
            first_pf, inherited_start_km,
        )
    if last_pf < 0.92 and skeleton.ordered_stops:
        inherited_end_km = (1.0 - last_pf) * initial_km
        _LOG.info(
            "[GEO INHERIT END] route pf=%.3f→1.0 (%.1fkm) inherited from corridor",
            last_pf, inherited_end_km,
        )

    # Check if we have new waypoints worth refining
    initial_wp_ids = {
        wp.get("stop_id") for wp in (initial_corridor.waypoints_used or [])
        if wp.get("stop_id")
    }
    sequence_ids = set(skeleton.stop_ids())
    new_waypoints = sequence_ids - initial_wp_ids

    refined_corridor = None
    derived_from = "valhalla_initial"

    # BUG-013: Only attempt refinement if skeleton covers most of the corridor
    # (first_pf < 0.10 AND last_pf > 0.90) — otherwise the refined corridor
    # will be clipped to the stop extent, losing geometry.
    stop_coverage_sufficient = (first_pf < 0.10 and last_pf > 0.90)

    if (refine_with_valhalla
            and new_waypoints
            and len(skeleton.ordered_stops) >= 3
            and stop_coverage_sufficient):
        try:
            from datamind_console.phases.phase3_routes.stop_grounding.corridor_builder import (
                rebuild_corridor_with_sequence,
            )
            sequence_dicts = [
                {"lon": s.lon, "lat": s.lat, "stop_id": s.stop_id}
                for s in skeleton.ordered_stops
            ]
            refined = rebuild_corridor_with_sequence(
                sequence_dicts,
                expected_envelope=expected_envelope,
                timeout_s=timeout_s,
            )
            if refined.corridor_geojson:
                refined_km = refined.total_length_km or 0.0
                # BUG-013 safety: only accept refinement if it covers >=80% of initial corridor
                if initial_km > 0 and refined_km >= initial_km * 0.80:
                    refined_corridor = refined
                    derived_from = "valhalla_refined"
                    _LOG.info(
                        "[GEO REFINE OK] Refined corridor: %.1fkm (initial %.1fkm, ratio=%.2f) with %d new waypoints",
                        refined_km, initial_km, refined_km / initial_km, len(new_waypoints),
                    )
                else:
                    _LOG.info(
                        "[GEO REFINE REJECT] Refined corridor %.1fkm too short vs initial %.1fkm "
                        "(ratio=%.2f < 0.80) — keeping full corridor",
                        refined_km, initial_km,
                        refined_km / initial_km if initial_km > 0 else 0,
                    )
        except Exception as exc:
            _LOG.warning("Corridor refinement failed, using initial: %s", exc)
    elif refine_with_valhalla and new_waypoints and not stop_coverage_sufficient:
        _LOG.info(
            "[GEO REFINE SKIP] Stop coverage pf=%.3f→%.3f insufficient for refinement — "
            "keeping full corridor (%.1fkm)",
            first_pf, last_pf, initial_km,
        )

    final_corridor = refined_corridor if refined_corridor else initial_corridor

    # Geography evaluation for refined vs initial
    initial_geo = evaluate_corridor_geography(
        corridor_geojson=initial_corridor.corridor_geojson,
        waypoints_used=initial_corridor.waypoints_used,
        expected_envelope=expected_envelope,
        discovered_stop_count=len(skeleton.ordered_stops),
    )
    if refined_corridor and refined_corridor.corridor_geojson:
        refined_geo = evaluate_corridor_geography(
            corridor_geojson=refined_corridor.corridor_geojson,
            waypoints_used=refined_corridor.waypoints_used,
            expected_envelope=expected_envelope,
            discovered_stop_count=len(skeleton.ordered_stops),
        )
        refined_rejected = bool(refined_geo.get("rejected_for_geographic_implausibility", False))
        initial_rejected = bool(initial_geo.get("rejected_for_geographic_implausibility", False))
        refined_score = float(refined_geo.get("geography_plausibility_score") or 0.0)
        initial_score = float(initial_geo.get("geography_plausibility_score") or 0.0)
        if refined_rejected and not initial_rejected:
            _LOG.info("[GEO REFINE ROLLBACK] Refined corridor geo-rejected, reverting to initial")
            final_corridor = initial_corridor
            derived_from = "valhalla_initial"
        elif refined_score + 0.05 < initial_score:
            _LOG.info(
                "[GEO REFINE ROLLBACK] Refined geo score %.2f < initial %.2f, reverting",
                refined_score, initial_score,
            )
            final_corridor = initial_corridor
            derived_from = "valhalla_initial"

    geojson = final_corridor.corridor_geojson
    consistency = _compute_consistency(skeleton, geojson) if geojson else 0.0

    # Geometry confidence = corridor confidence * consistency * geography
    final_geo = evaluate_corridor_geography(
        corridor_geojson=geojson,
        waypoints_used=final_corridor.waypoints_used,
        expected_envelope=expected_envelope,
        discovered_stop_count=len(skeleton.ordered_stops),
    )
    geo_confidence = (
        final_corridor.corridor_confidence
        * consistency
        * float(final_geo.get("geography_plausibility_score") or 0.0)
    )

    return GeometryCandidate(
        geometry_geojson=geojson,
        derived_from=derived_from,
        geometry_confidence=geo_confidence,
        sequence_to_geometry_consistency=consistency,
        total_length_km=final_corridor.total_length_km,
        waypoint_count=len(skeleton.ordered_stops),
        notes=(
            f"{derived_from}: {final_corridor.total_length_km:.1f}km, "
            f"consistency={consistency:.2f}, confidence={geo_confidence:.2f}, "
            f"stop_coverage=pf[{first_pf:.3f}→{last_pf:.3f}]"
        ),
        straight_line_km=float(final_geo.get("straight_line_km") or 0.0),
        corridor_inflation_ratio=float(final_geo.get("corridor_inflation_ratio") or 0.0),
        in_bounds_fraction=float(final_geo.get("in_bounds_fraction") or 0.0),
        geography_plausibility_score=float(final_geo.get("geography_plausibility_score") or 0.0),
        rejected_for_geographic_implausibility=bool(
            final_geo.get("rejected_for_geographic_implausibility", False)
        ),
        valhalla_meta=final_corridor.valhalla_meta,
    )
