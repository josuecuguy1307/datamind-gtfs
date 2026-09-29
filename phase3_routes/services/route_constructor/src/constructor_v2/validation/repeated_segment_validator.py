from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from src.constructor_v2.common import haversine_m
from src.constructor_v2.constants import MAX_REPEAT_SEGMENT_RATIO
from src.constructor_v2.schemas.route_output import ValidationResult


def _segment_key(left: tuple[float, float], right: tuple[float, float]) -> tuple[tuple[float, float], tuple[float, float]]:
    a = (round(left[0], 5), round(left[1], 5))
    b = (round(right[0], 5), round(right[1], 5))
    return tuple(sorted((a, b)))


def validate_repeated_segments(
    geometry_coords: Sequence[tuple[float, float]],
    *,
    route_type: str,
) -> ValidationResult:
    if len(geometry_coords) < 2:
        return ValidationResult(
            name="repeated_segments",
            status="fail",
            score=0.0,
            metrics={},
            issues=["geometry has fewer than two points"],
        )

    segment_lengths: dict[tuple[tuple[float, float], tuple[float, float]], float] = defaultdict(float)
    repeated_length = 0.0
    total_length = 0.0
    for left, right in zip(geometry_coords, geometry_coords[1:]):
        seg_length = haversine_m(left[1], left[0], right[1], right[0])
        total_length += seg_length
        key = _segment_key(left, right)
        if key in segment_lengths:
            repeated_length += seg_length
        segment_lengths[key] += seg_length

    repeat_ratio = repeated_length / total_length if total_length else 0.0
    allowed_ratio = MAX_REPEAT_SEGMENT_RATIO * (1.8 if "loop" in route_type else 1.0)
    warn_ratio = allowed_ratio * 0.75
    fail_ratio = allowed_ratio * (2.2 if "loop" in route_type else 1.8)
    status = "pass"
    issues: list[str] = []
    if repeat_ratio > fail_ratio:
        status = "fail"
        issues.append(f"repeated segment ratio {repeat_ratio:.3f} exceeds {fail_ratio:.3f}")
    elif repeat_ratio > warn_ratio:
        status = "warn"
        issues.append(f"repeated segment ratio {repeat_ratio:.3f} is elevated")
    if repeat_ratio <= warn_ratio:
        score = max(0.0, 1.0 - 0.2 * (repeat_ratio / max(warn_ratio, 1e-6)))
    else:
        excess = (repeat_ratio - warn_ratio) / max(fail_ratio - warn_ratio, 1e-6)
        score = max(0.0, 0.8 - (0.8 * min(excess, 1.0)))
    return ValidationResult(
        name="repeated_segments",
        status=status,
        score=score,
        metrics={
            "repeat_ratio": repeat_ratio,
            "repeated_length_m": repeated_length,
            "total_length_m": total_length,
            "warn_ratio": warn_ratio,
            "fail_ratio": fail_ratio,
        },
        issues=issues,
    )
