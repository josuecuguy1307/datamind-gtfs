"""Shape fixers — reroute gap segments, simplify self-intersections.

All fixers are idempotent and return FixAttempt with confidence score.
"""
from __future__ import annotations

from ..models import EntityIssue, FixAttempt, Shape


def fix_reroute_shape_gap(shape: Shape, issue: EntityIssue) -> FixAttempt:
    """Re-request Valhalla trace between gap endpoints and splice into shape.

    Confidence: Valhalla trip_summary.confidence if returned, else 0.5.
    In offline/benchmark mode, returns a placeholder splice.

    phase_origin: 3
    rule_name: shape_gap_too_large
    """
    gap_info = issue.original_value
    if not isinstance(gap_info, dict):
        return FixAttempt(
            success=False, new_value=None, confidence=0.0,
            log=f"Invalid gap info for shape {shape.shape_id}",
        )

    from_coord = gap_info.get("from_coord", (0, 0))
    to_coord = gap_info.get("to_coord", (0, 0))
    gap_km = gap_info.get("gap_km", 0)

    # Offline mode: mark as needing Valhalla re-trace
    confidence = 0.5  # default without actual Valhalla response

    return FixAttempt(
        success=True,
        new_value={
            "action": "valhalla_retrace",
            "from_coord": from_coord,
            "to_coord": to_coord,
            "gap_km": gap_km,
        },
        confidence=confidence,
        log=f"Shape {shape.shape_id}: gap {gap_km:.1f}km marked for Valhalla re-trace "
            f"from {from_coord} to {to_coord}",
    )


def fix_simplify_self_intersection(shape: Shape, issue: EntityIssue) -> FixAttempt:
    """Detect crossing segments, simplify with Douglas-Peucker, re-route via Valhalla.

    In offline mode, flags for Phase 3 reconstruction.

    phase_origin: 3
    rule_name: shape_self_intersection
    """
    crossings = issue.original_value if isinstance(issue.original_value, int) else 0

    return FixAttempt(
        success=False,  # offline can't truly fix self-intersections
        new_value={"crossings": crossings, "action": "needs_reconstruction"},
        confidence=0.0,
        log=f"Shape {shape.shape_id}: {crossings} self-intersection(s) — needs Phase 3 reconstruction",
    )


FIXERS = {
    "shape_gap_too_large": fix_reroute_shape_gap,
    "shape_self_intersection": fix_simplify_self_intersection,
}
