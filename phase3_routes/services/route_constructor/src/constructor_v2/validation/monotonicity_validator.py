from __future__ import annotations

from typing import Sequence

from shapely.geometry import LineString, Point

from src.constructor_v2.constants import MAX_MONOTONIC_REGRESSION
from src.constructor_v2.schemas.route_input import NormalizedStop
from src.constructor_v2.schemas.route_output import ValidationResult


def validate_monotonic_progress(
    stops: Sequence[NormalizedStop],
    geometry_coords: Sequence[tuple[float, float]],
) -> ValidationResult:
    if len(stops) < 2 or len(geometry_coords) < 2:
        return ValidationResult(
            name="monotonic_progress",
            status="fail",
            score=0.0,
            metrics={},
            issues=["insufficient stops or geometry for projection"],
        )

    line = LineString(geometry_coords)
    progresses = [
        float(line.project(Point(stop.lon, stop.lat), normalized=True))
        for stop in stops
    ]
    regressions = []
    max_regression = 0.0
    for previous, current in zip(progresses, progresses[1:]):
        if current + 1e-9 < previous:
            regression = previous - current
            regressions.append(regression)
            max_regression = max(max_regression, regression)

    status = "pass"
    issues: list[str] = []
    if max_regression > MAX_MONOTONIC_REGRESSION:
        status = "fail"
        issues.append(f"projected progress regressed by {max_regression:.4f}")
    elif regressions:
        status = "warn"
        issues.append(f"{len(regressions)} minor projection regressions detected")

    score = max(0.0, 1.0 - (max_regression / max(MAX_MONOTONIC_REGRESSION, 1e-6)))
    return ValidationResult(
        name="monotonic_progress",
        status=status,
        score=score,
        metrics={
            "projected_progress": progresses,
            "regression_count": len(regressions),
            "max_regression": max_regression,
        },
        issues=issues,
    )
