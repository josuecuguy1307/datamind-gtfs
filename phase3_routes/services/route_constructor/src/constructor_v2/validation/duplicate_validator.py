from __future__ import annotations

from typing import Sequence

from src.constructor_v2.common import haversine_m, normalize_name
from src.constructor_v2.schemas.route_input import NormalizedStop
from src.constructor_v2.schemas.route_output import ValidationResult


def validate_duplicates(stops: Sequence[NormalizedStop]) -> ValidationResult:
    duplicate_pairs = []
    seen_ids: set[str] = set()
    for index, stop in enumerate(stops):
        if stop.stop_id in seen_ids:
            duplicate_pairs.append((stop.stop_id, stop.stop_name))
        seen_ids.add(stop.stop_id)
        for other in stops[index + 1 :]:
            if stop.stop_id == other.stop_id:
                continue
            if normalize_name(stop.stop_name) != normalize_name(other.stop_name):
                continue
            distance_m = haversine_m(stop.lat, stop.lon, other.lat, other.lon)
            if distance_m <= 70.0:
                duplicate_pairs.append((stop.stop_id, other.stop_id))

    status = "pass" if not duplicate_pairs else "warn"
    issues = [] if not duplicate_pairs else [f"{len(duplicate_pairs)} duplicate or near-duplicate pairs remain"]
    score = max(0.0, 1.0 - min(len(duplicate_pairs), max(len(stops) - 2, 1)) / max(len(stops), 1))
    return ValidationResult(
        name="duplicate_stops",
        status=status,
        score=score,
        metrics={
            "duplicate_pair_count": len(duplicate_pairs),
            "duplicate_pairs": [list(pair) for pair in duplicate_pairs[:20]],
        },
        issues=issues,
    )
