from __future__ import annotations

from typing import Sequence

from src.constructor_v2.schemas.route_input import NormalizedStop
from src.constructor_v2.schemas.route_output import ValidationResult


def validate_anchors(
    original_stops: Sequence[NormalizedStop],
    final_stops: Sequence[NormalizedStop],
) -> ValidationResult:
    required_anchor_ids = [stop.stop_id for stop in original_stops if stop.is_fixed_start or stop.is_fixed_end or stop.is_known_anchor]
    final_ids = [stop.stop_id for stop in final_stops]
    missing = [stop_id for stop_id in required_anchor_ids if stop_id not in final_ids]
    start_ok = bool(final_stops) and bool(original_stops) and final_stops[0].stop_id == original_stops[0].stop_id
    end_ok = bool(final_stops) and bool(original_stops) and final_stops[-1].stop_id == original_stops[-1].stop_id
    status = "pass" if not missing and start_ok and end_ok else "fail"
    issues = []
    if not start_ok:
        issues.append("start anchor changed")
    if not end_ok:
        issues.append("end anchor changed")
    if missing:
        issues.append(f"missing anchors: {', '.join(missing[:5])}")
    score = 1.0 if status == "pass" else 0.0
    return ValidationResult(
        name="anchor_respect",
        status=status,
        score=score,
        metrics={
            "required_anchor_count": len(required_anchor_ids),
            "missing_anchor_count": len(missing),
            "start_ok": start_ok,
            "end_ok": end_ok,
        },
        issues=issues,
    )
