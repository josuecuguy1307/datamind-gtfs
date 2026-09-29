"""Per-rule commit thresholds for S1.

Each key is a rule_name (matching the rule functions in shared/rules/).
The value is the minimum fixer confidence required to COMMIT the fix.
Below threshold → REJECT (route-back to origin phase).

These defaults are conservative starting points — designed to be swept
during benchmarking via S1ThresholdsGate(overrides={...}).
"""
from __future__ import annotations

from typing import Dict

# Default threshold when a rule_name has no explicit entry
DEFAULT_THRESHOLD: float = 0.80

COMMIT_THRESHOLDS: Dict[str, float] = {
    # ── Stop rules ──────────────────────────────────────────────────────
    "stop_name_placeholder":    0.30,   # reverse-geocode / coord fallback (was 0.60)
    "stop_name_empty":          0.30,   # same fixer as placeholder (was 0.60)
    "stop_coords_outside_bbox": 0.65,   # lat/lon swap — relaxed from 0.95
    "stop_coords_null_island":  1.00,   # never auto-fix (0,0) — always reject
    "stop_duplicate_nearby":    0.85,   # merge only if strong name match + close (raised from 0.75)
    "stop_ref_garbage":         0.50,   # clearing a garbage ref is safe
    "stop_name_uuid_prefix":    0.30,   # same fixer as placeholder (was 0.60)

    # ── Route rules ─────────────────────────────────────────────────────
    "route_too_few_stops":      0.50,   # fragment merge — relaxed from 0.80
    "route_no_schedule":        0.90,   # default schedule = risky guess

    # ── Shape rules ─────────────────────────────────────────────────────
    "shape_gap_too_large":      0.65,   # Valhalla re-trace or linear interpolation
    "shape_self_intersection":  0.75,   # geometry simplification (raised from 0.70)

    # ── Route geometry rules ────────────────────────────────────────────
    "route_geometry_straight_line": 0.85,  # Valhalla retrace for straight-line geometry
    "route_geometry_low_detail":   0.85,   # Valhalla retrace for low-detail geometry
    "route_geometry_low_sinuosity": 0.85,  # Valhalla retrace for low-sinuosity geometry

    # ── Naming rules ────────────────────────────────────────────────────
    "route_name_garbage":       0.40,   # catalog/origin-dest fallback (was 0.70)
    "short_name_collision":     0.85,   # disambiguate with suffix
    "operator_inconsistency":   0.80,   # canonical operator replacement

    # ── Timing rules ────────────────────────────────────────────────────
    "unrealistic_runtime":      0.45,   # clamp to bounds — relaxed from 0.75
    "calendar_no_active_days":  1.00,   # never auto-fix — always reject

}
