"""
BUG-010 — Terminus Lock

Clips a Valhalla corridor so it does not extend beyond the terminus anchors.
Applied AFTER corridor construction, BEFORE stop intersection.

The corridor is trimmed to within TOLERANCE_M of each terminus anchor coord.
If no terminus coords are available, the corridor is returned unchanged.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    CorridorResult,
    TypedRouteSeed,
)
from datamind_console.phases.phase3_routes.stop_grounding.catalogs import get_config_section

_LOG = logging.getLogger(__name__)

_terminus_cfg = get_config_section("terminus")
TERMINUS_TOLERANCE_M = _terminus_cfg.get("tolerance_m", 300.0)

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


def _find_closest_index(coords: List[LonLat], target_lon: float, target_lat: float) -> int:
    """Find the index of the closest point in coords to the target."""
    best_idx = 0
    best_dist = float("inf")
    for i, (lon, lat) in enumerate(coords):
        d = _haversine_m(lon, lat, target_lon, target_lat)
        if d < best_dist:
            best_dist = d
            best_idx = i
    return best_idx


def _extract_terminus_coords(seed: TypedRouteSeed) -> Tuple[Optional[LonLat], Optional[LonLat]]:
    """Extract start/end terminus coords from typed seed tokens."""
    start_coord: Optional[LonLat] = None
    end_coord: Optional[LonLat] = None

    for token in seed.sequence_tokens:
        if not token.has_anchor_coords:
            continue
        if token.anchor_role == "terminus":
            role = token.role.lower() if token.role else ""
            if role in ("origin_stop_candidate", "start", "origin_area"):
                start_coord = (token.anchor_lon, token.anchor_lat)
            elif role in ("destination_stop_candidate", "end", "destination_area"):
                end_coord = (token.anchor_lon, token.anchor_lat)

    # Fallback: first and last tokens with anchor coords
    if start_coord is None or end_coord is None:
        tokens_with_coords = [t for t in seed.sequence_tokens if t.has_anchor_coords]
        if tokens_with_coords and start_coord is None:
            t = tokens_with_coords[0]
            if t.role in ("origin_stop_candidate", "start", "origin_area") or t.anchor_role == "terminus":
                start_coord = (t.anchor_lon, t.anchor_lat)
        if tokens_with_coords and end_coord is None:
            t = tokens_with_coords[-1]
            if t.role in ("destination_stop_candidate", "end", "destination_area") or t.anchor_role == "terminus":
                end_coord = (t.anchor_lon, t.anchor_lat)

    return start_coord, end_coord


def apply_terminus_lock(
    corridor: CorridorResult,
    seed: TypedRouteSeed,
    *,
    tolerance_m: float = TERMINUS_TOLERANCE_M,
) -> CorridorResult:
    """
    Clip corridor geometry to not extend beyond terminus anchors.

    If the corridor extends more than tolerance_m past a terminus anchor,
    the geometry is trimmed at that point.

    Returns a new CorridorResult with clipped geometry (or the original if no clipping needed).
    """
    if not corridor.corridor_geojson:
        return corridor

    coords = corridor.corridor_geojson.get("coordinates", [])
    if len(coords) < 3:
        return corridor

    start_coord, end_coord = _extract_terminus_coords(seed)
    if start_coord is None and end_coord is None:
        _LOG.info("[TERMINUS LOCK] No terminus coords in seed — skipping lock")
        return corridor

    clip_start = 0
    clip_end = len(coords)

    if start_coord is not None:
        start_idx = _find_closest_index(coords, start_coord[0], start_coord[1])
        start_dist = _haversine_m(coords[start_idx][0], coords[start_idx][1], start_coord[0], start_coord[1])

        # Check if corridor extends significantly before the start terminus
        if start_idx > 0 and start_dist < tolerance_m:
            # Measure how much corridor is before the start terminus
            pre_terminus_km = _linestring_length_km(coords[:start_idx + 1])
            if pre_terminus_km > tolerance_m / 1000.0:
                clip_start = max(0, start_idx - 1)  # keep one point before for smooth join
                _LOG.info(
                    "[TERMINUS LOCK START] Clipping %.1fm before start terminus (idx %d→%d of %d)",
                    pre_terminus_km * 1000, 0, clip_start, len(coords),
                )

    if end_coord is not None:
        end_idx = _find_closest_index(coords, end_coord[0], end_coord[1])
        end_dist = _haversine_m(coords[end_idx][0], coords[end_idx][1], end_coord[0], end_coord[1])

        if end_idx < len(coords) - 1 and end_dist < tolerance_m:
            post_terminus_km = _linestring_length_km(coords[end_idx:])
            if post_terminus_km > tolerance_m / 1000.0:
                clip_end = min(len(coords), end_idx + 2)  # keep one point after for smooth join
                _LOG.info(
                    "[TERMINUS LOCK END] Clipping %.1fm after end terminus (idx %d→%d of %d)",
                    post_terminus_km * 1000, end_idx, clip_end, len(coords),
                )

    if clip_start == 0 and clip_end == len(coords):
        _LOG.info("[TERMINUS LOCK] No clipping needed — corridor within terminus bounds")
        return corridor

    clipped_coords = coords[clip_start:clip_end]
    if len(clipped_coords) < 2:
        _LOG.warning("[TERMINUS LOCK] Clipping would leave <2 points — skipping lock")
        return corridor

    clipped_km = _linestring_length_km(clipped_coords)
    original_km = corridor.total_length_km or _linestring_length_km(coords)

    # Safety: don't clip more than 30% of corridor
    _min_retained = _terminus_cfg.get("min_retained_fraction", 0.70)
    if clipped_km < original_km * _min_retained:
        _LOG.warning(
            "[TERMINUS LOCK REJECT] Clipped corridor %.1fkm is <70%% of original %.1fkm — skipping lock",
            clipped_km, original_km,
        )
        return corridor

    _LOG.info(
        "[TERMINUS LOCK OK] Clipped corridor: %.1fkm → %.1fkm (%.0f%% retained)",
        original_km, clipped_km, 100 * clipped_km / max(original_km, 0.01),
    )

    clipped_geojson = {
        "type": "LineString",
        "coordinates": clipped_coords,
    }

    # Create new CorridorResult with clipped geometry
    return CorridorResult(
        corridor_geojson=clipped_geojson,
        total_length_km=clipped_km,
        segment_count=corridor.segment_count,
        failed_segments=corridor.failed_segments,
        waypoints_used=corridor.waypoints_used,
        corridor_confidence=corridor.corridor_confidence,
        corridor_notes=f"{corridor.corridor_notes}; terminus_locked {original_km:.1f}→{clipped_km:.1f}km",
        expected_geographic_envelope=corridor.expected_geographic_envelope,
        straight_line_km=corridor.straight_line_km,
        corridor_inflation_ratio=clipped_km / max(corridor.straight_line_km, 0.01),
        in_bounds_fraction=corridor.in_bounds_fraction,
        out_of_bounds_reason=corridor.out_of_bounds_reason,
        geography_plausibility_score=corridor.geography_plausibility_score,
        rejected_for_geographic_implausibility=corridor.rejected_for_geographic_implausibility,
        route_locality_consistency_notes=corridor.route_locality_consistency_notes,
    )
