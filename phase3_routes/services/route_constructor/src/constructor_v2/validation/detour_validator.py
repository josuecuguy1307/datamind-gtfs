from __future__ import annotations

from statistics import mean
from typing import Sequence

from src.constructor_v2.constants import MAX_ACCEPTABLE_DETOUR_RATIO, MAX_STRONG_DETOUR_RATIO
from src.constructor_v2.schemas.route_output import OrderedLeg, ValidationResult


def validate_detour_ratios(legs: Sequence[OrderedLeg]) -> ValidationResult:
    ratios = [leg.detour_ratio for leg in legs if leg.detour_ratio > 0.0]
    severe = [ratio for ratio in ratios if ratio > MAX_ACCEPTABLE_DETOUR_RATIO]
    max_ratio = max(ratios, default=1.0)
    avg_ratio = mean(ratios) if ratios else 1.0
    status = "pass"
    issues: list[str] = []
    if max_ratio > MAX_ACCEPTABLE_DETOUR_RATIO:
        status = "fail"
        issues.append(f"max detour ratio {max_ratio:.2f} exceeds {MAX_ACCEPTABLE_DETOUR_RATIO:.2f}")
    elif max_ratio > MAX_STRONG_DETOUR_RATIO:
        status = "warn"
        issues.append(f"max detour ratio {max_ratio:.2f} exceeds strong threshold {MAX_STRONG_DETOUR_RATIO:.2f}")
    score = max(0.0, 1.0 - max(0.0, avg_ratio - 1.0) / MAX_ACCEPTABLE_DETOUR_RATIO)
    return ValidationResult(
        name="detour_ratio",
        status=status,
        score=score,
        metrics={
            "avg_detour_ratio": avg_ratio,
            "max_detour_ratio": max_ratio,
            "severe_leg_count": len(severe),
            "leg_count": len(legs),
        },
        issues=issues,
    )
