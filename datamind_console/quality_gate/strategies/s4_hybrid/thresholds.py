"""Per-rule confidence thresholds for Layer 1 (S1-style).

Each rule_name maps to the minimum confidence required for a fix to pass L1.
Rules not listed here use DEFAULT_THRESHOLD.

Copied inline (not imported from S1) to keep strategies independent.
"""
from __future__ import annotations

# Conservative default — fixes must be reasonably confident to proceed.
DEFAULT_THRESHOLD = 0.55

# Per-rule overrides.  Lower = more permissive, higher = stricter.
THRESHOLDS: dict[str, float] = {
    # Stop fixers — coord fallback names are moderate quality
    "stop_name_placeholder": 0.50,
    "stop_name_empty": 0.50,
    "stop_name_uuid_prefix": 0.50,
    "stop_ref_garbage": 0.70,          # clearing ref is high-confidence or bust
    "stop_coords_null_island": 0.90,   # auto-fix almost never possible
    "stop_coords_outside_bbox": 0.80,  # swap only if very confident
    "stop_duplicate_nearby": 0.60,     # merge is consequential

    # Route
    "route_too_few_stops": 0.60,
    "route_no_schedule": 0.80,

    # Shape
    "shape_gap_too_large": 0.50,
    "shape_self_intersection": 0.80,

    # Naming
    "route_name_garbage": 0.50,
    "short_name_collision": 0.60,
    "operator_inconsistency": 0.50,

    # Timing
    "unrealistic_runtime": 0.60,
    "calendar_no_active_days": 0.55,

}


def threshold_for(rule_name: str) -> float:
    """Return the confidence threshold for a rule."""
    return THRESHOLDS.get(rule_name, DEFAULT_THRESHOLD)
