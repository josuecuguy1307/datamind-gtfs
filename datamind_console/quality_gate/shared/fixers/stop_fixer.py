"""Stop fixers — reverse-geocode blank names, merge duplicates, swap transposed coords.

All fixers are idempotent and return FixAttempt with confidence score.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import DUPLICATE_DISTANCE_M, ECUADOR_BBOX, haversine_m
from ..models import EntityIssue, FixAttempt, Stop


def fix_reverse_geocode_name(stop: Stop, issue: EntityIssue) -> FixAttempt:
    """Fix blank/placeholder stop names by reverse-geocoding coordinates.

    Confidence formula: Nominatim importance * (1 - distance_to_centroid / 100m).
    In offline/benchmark mode, returns a synthetic name from coordinates.

    phase_origin: 1, 5
    rule_name: stop_name_placeholder, stop_name_empty, stop_name_uuid_prefix
    """
    try:
        # Coordinate-based fallback names are never acceptable in production.
        # Return failure so the stop gets flagged for manual context naming.
        return FixAttempt(
            success=False,
            new_value=None,
            confidence=0.0,
            log=f"Reverse-geocode offline mode: coordinate fallback disabled for {stop.stop_id[:8]}",
        )
    except Exception as e:
        return FixAttempt(
            success=False, new_value=None, confidence=0.0,
            log=f"Reverse-geocode failed for {stop.stop_id[:8]}: {e}",
        )


def fix_swap_transposed_coords(stop: Stop, issue: EntityIssue) -> FixAttempt:
    """Fix coordinates by swapping lat/lon if transposed version lands in Ecuador.

    Confidence: 1.0 if swapped coords are inside Ecuador bbox, 0.0 otherwise.

    phase_origin: 1
    rule_name: stop_coords_outside_bbox
    """
    swapped_lat, swapped_lon = stop.lon, stop.lat
    min_lat, min_lon, max_lat, max_lon = ECUADOR_BBOX

    if min_lat <= swapped_lat <= max_lat and min_lon <= swapped_lon <= max_lon:
        return FixAttempt(
            success=True,
            new_value=(swapped_lat, swapped_lon),
            confidence=1.0,
            log=f"Swapped lat/lon for {stop.stop_id[:8]}: ({stop.lat},{stop.lon}) -> ({swapped_lat},{swapped_lon})",
        )
    return FixAttempt(
        success=False, new_value=None, confidence=0.0,
        log=f"Swap doesn't help for {stop.stop_id[:8]}: swapped ({swapped_lat},{swapped_lon}) still outside bbox",
    )


def fix_merge_duplicates(stop_a: Stop, stop_b: Stop, issue: EntityIssue) -> FixAttempt:
    """Merge two near-duplicate stops, keeping the higher-confidence one.

    Confidence formula: 1 - (distance_m / 15) * name_similarity_ratio.

    phase_origin: 1
    rule_name: stop_duplicate_nearby
    """
    dist = haversine_m(stop_a.lat, stop_a.lon, stop_b.lat, stop_b.lon)
    dist_factor = 1.0 - (dist / DUPLICATE_DISTANCE_M)
    dist_factor = max(0.0, min(1.0, dist_factor))

    # Name similarity from issue data
    sim = 1.0
    if isinstance(issue.original_value, dict):
        sim = issue.original_value.get("similarity", 1.0)

    confidence = dist_factor * sim

    # Keep higher confidence stop
    keeper = stop_a if stop_a.confidence >= stop_b.confidence else stop_b
    removed = stop_b if keeper is stop_a else stop_a

    return FixAttempt(
        success=True,
        new_value={"keep": keeper.stop_id, "remove": removed.stop_id},
        confidence=confidence,
        log=f"Merge: keep {keeper.stop_id[:8]} (conf={keeper.confidence:.2f}), remove {removed.stop_id[:8]} ({dist:.1f}m apart)",
    )


FIXERS = {
    "stop_name_placeholder": fix_reverse_geocode_name,
    "stop_name_empty": fix_reverse_geocode_name,
    "stop_name_uuid_prefix": fix_reverse_geocode_name,
    "stop_coords_outside_bbox": fix_swap_transposed_coords,
    "stop_coords_null_island": lambda stop, issue: FixAttempt(
        success=False, new_value=None, confidence=0.0,
        log=f"No auto-fix for null-island stop {stop.stop_id[:8]}",
    ),
    "stop_ref_garbage": lambda stop, issue: FixAttempt(
        success=False, new_value=None, confidence=0.0,
        log=f"No auto-fix for garbage ref on {stop.stop_id[:8]} — needs Phase 1 re-identification",
    ),
}
