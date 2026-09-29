from __future__ import annotations

from statistics import median
from typing import Sequence

from src.constructor_v2.common import angular_delta_deg, bearing_deg
from src.constructor_v2.constants import MAX_CORRIDOR_SWITCHES
from src.constructor_v2.schemas.route_input import NormalizedStop
from src.constructor_v2.schemas.route_output import ValidationResult


def validate_corridor_consistency(stops: Sequence[NormalizedStop], *, route_type: str) -> ValidationResult:
    if len(stops) < 3:
        return ValidationResult(
            name="corridor_consistency",
            status="pass",
            score=1.0,
            metrics={"corridor_switch_count": 0, "proxy": True},
            issues=[],
        )

    bearings = [
        bearing_deg(left.lat, left.lon, right.lat, right.lon)
        for left, right in zip(stops, stops[1:])
    ]
    median_bearing = median(bearings)
    major_changes = []
    for previous, current in zip(bearings, bearings[1:]):
        delta = angular_delta_deg(previous, current)
        if delta > 85.0:
            major_changes.append(delta)

    off_axis = [delta for delta in (angular_delta_deg(value, median_bearing) for value in bearings) if delta > 90.0]
    allowed_switches = MAX_CORRIDOR_SWITCHES * (2 if "loop" in route_type else 1)
    segment_count = len(bearings)
    warn_threshold = max(allowed_switches, segment_count // 6)
    fail_threshold = max(warn_threshold + 3, int(round(segment_count * 0.4)))
    status = "pass"
    issues: list[str] = []
    if len(major_changes) >= fail_threshold and len(major_changes) > 0:
        status = "fail"
        issues.append(f"{len(major_changes)} abrupt corridor switches detected")
    elif len(major_changes) >= warn_threshold and len(major_changes) > 0:
        status = "warn"
        issues.append(f"{len(major_changes)} elevated corridor switches detected")
    switch_ratio = len(major_changes) / max(segment_count - 1, 1)
    off_axis_ratio = len(off_axis) / max(segment_count, 1)
    score = max(0.0, 1.0 - (switch_ratio * 1.4) - (off_axis_ratio * 0.5))
    return ValidationResult(
        name="corridor_consistency",
        status=status,
        score=score,
        metrics={
            "proxy": True,
            "median_bearing": median_bearing,
            "corridor_switch_count": len(major_changes),
            "off_axis_count": len(off_axis),
            "warn_threshold": warn_threshold,
            "fail_threshold": fail_threshold,
        },
        issues=issues,
    )
