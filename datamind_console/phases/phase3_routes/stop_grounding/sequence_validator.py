"""
Layer 5 — Post-corridor sequence validation.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    SequenceSkeleton,
    StopGroundingResult,
)

BACKTRACK_TOLERANCE_M = 50.0
MAX_LEG_M = 3000.0
MIN_LEG_M = 30.0


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * radius_m * math.asin(math.sqrt(a))


def validate_sequence(
    skeleton: SequenceSkeleton,
    grounding: StopGroundingResult,
    *,
    corridor_length_km: float = 0.0,
) -> Dict[str, Any]:
    del corridor_length_km
    stops = list(skeleton.ordered_stops or [])
    if len(stops) < 2:
        return {
            "is_valid": len(stops) >= 1,
            "checks": [],
            "warnings": ["Sequence too short for post-corridor validation"],
            "repaired_stops": None,
            "validation_confidence_delta": 0.0,
            "pass_rate": 1.0,
            "stops_removed_by_validation": 0,
        }

    warnings: List[str] = []
    checks: List[Dict[str, Any]] = []
    repaired = list(stops)
    removed_indices: List[int] = []

    # Directional consistency via path_fraction monotonicity.
    monotonic_issues: List[Dict[str, Any]] = []
    for idx in range(1, len(repaired)):
        previous = repaired[idx - 1]
        current = repaired[idx]
        delta_fraction = current.path_fraction - previous.path_fraction
        if delta_fraction >= 0:
            continue
        leg_m = _haversine_m(previous.lon, previous.lat, current.lon, current.lat)
        if leg_m <= BACKTRACK_TOLERANCE_M:
            continue
        monotonic_issues.append(
            {
                "stop_index": idx,
                "stop_name": current.stop_name,
                "delta_fraction": round(delta_fraction, 6),
                "backtrack_m": round(leg_m, 1),
            }
        )
        if not current.is_known_anchor and not current.is_known_intermediate:
            removed_indices.append(idx)

    if removed_indices:
        remove_set = set(removed_indices)
        repaired = [stop for idx, stop in enumerate(repaired) if idx not in remove_set]
        warnings.append(f"Removed {len(removed_indices)} backtracking stop(s)")

    checks.append(
        {
            "check": "directional_consistency",
            "passed": len(monotonic_issues) == 0,
            "issues": monotonic_issues,
        }
    )

    # Leg plausibility after repair.
    implausible_legs: List[Dict[str, Any]] = []
    duplicate_legs: List[Dict[str, Any]] = []
    for idx in range(1, len(repaired)):
        previous = repaired[idx - 1]
        current = repaired[idx]
        leg_m = _haversine_m(previous.lon, previous.lat, current.lon, current.lat)
        if leg_m > MAX_LEG_M:
            implausible_legs.append(
                {
                    "from_stop": previous.stop_name,
                    "to_stop": current.stop_name,
                    "distance_m": round(leg_m, 1),
                }
            )
        if leg_m < MIN_LEG_M:
            duplicate_legs.append(
                {
                    "from_stop": previous.stop_name,
                    "to_stop": current.stop_name,
                    "distance_m": round(leg_m, 1),
                }
            )

    if implausible_legs:
        warnings.append(f"{len(implausible_legs)} leg(s) exceed 3km")
    if duplicate_legs:
        warnings.append(f"{len(duplicate_legs)} leg(s) are shorter than 30m")

    checks.append(
        {
            "check": "leg_plausibility",
            "passed": len(implausible_legs) == 0 and len(duplicate_legs) == 0,
            "too_long": implausible_legs,
            "too_short": duplicate_legs,
        }
    )

    anchor_gaps = []
    anchor_a = grounding.best_anchor_a()
    anchor_b = grounding.best_anchor_b()
    if anchor_a and repaired:
        anchor_gaps.append(
            {
                "anchor": "A",
                "distance_m": round(_haversine_m(anchor_a.lon, anchor_a.lat, repaired[0].lon, repaired[0].lat), 1),
                "expected": anchor_a.stop_name,
                "actual": repaired[0].stop_name,
            }
        )
    if anchor_b and repaired:
        anchor_gaps.append(
            {
                "anchor": "B",
                "distance_m": round(_haversine_m(anchor_b.lon, anchor_b.lat, repaired[-1].lon, repaired[-1].lat), 1),
                "expected": anchor_b.stop_name,
                "actual": repaired[-1].stop_name,
            }
        )

    pass_rate = sum(1 for check in checks if check["passed"]) / max(len(checks), 1)
    if pass_rate >= 1.0:
        confidence_delta = 0.03
    elif pass_rate >= 0.5:
        confidence_delta = -0.03
    else:
        confidence_delta = -0.10

    return {
        "is_valid": all(check["passed"] for check in checks),
        "checks": checks,
        "warnings": warnings,
        "repaired_stops": repaired if len(repaired) != len(stops) else None,
        "validation_confidence_delta": confidence_delta,
        "pass_rate": round(pass_rate, 4),
        "stops_removed_by_validation": len(removed_indices),
        "anchor_distances": anchor_gaps,
    }
