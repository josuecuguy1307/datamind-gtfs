"""Timing fixers — clamp runtime, set default calendar.

All fixers are idempotent and return FixAttempt with confidence score.
"""
from __future__ import annotations

from ..config import RUNTIME_MAX_MINUTES, RUNTIME_MIN_MINUTES
from ..models import EntityIssue, FixAttempt, ScheduleProfile


def fix_clamp_runtime(sched: ScheduleProfile, issue: EntityIssue) -> FixAttempt:
    """Clamp runtime to province-specific min/max range.

    Confidence: 1 - abs(original - clamped) / clamped.

    phase_origin: 5
    rule_name: unrealistic_runtime
    """
    original = issue.original_value
    if not isinstance(original, (int, float)):
        return FixAttempt(
            success=False, new_value=None, confidence=0.0,
            log=f"Invalid runtime value: {original!r}",
        )

    clamped = max(RUNTIME_MIN_MINUTES, min(RUNTIME_MAX_MINUTES, original))
    if clamped == original:
        return FixAttempt(
            success=False, new_value=None, confidence=0.0,
            log=f"Runtime {original} already within bounds — nothing to clamp",
        )

    confidence = 1.0 - abs(original - clamped) / clamped
    confidence = max(0.0, min(1.0, confidence))

    return FixAttempt(
        success=True, new_value=clamped, confidence=confidence,
        log=f"Clamped runtime {original:.1f} -> {clamped:.1f} min (confidence={confidence:.2f})",
    )


def fix_default_calendar(sched: ScheduleProfile, issue: EntityIssue) -> FixAttempt:
    """Set default weekday service (mon-fri = true) for empty calendars.

    Confidence: 0.6 (safe default but not verified).

    phase_origin: 5
    rule_name: calendar_no_active_days
    """
    default_days = {
        "monday": True, "tuesday": True, "wednesday": True,
        "thursday": True, "friday": True,
        "saturday": False, "sunday": False,
    }
    return FixAttempt(
        success=True, new_value=default_days, confidence=0.6,
        log=f"Applied default weekday calendar for {sched.route_id[:8]}",
    )


FIXERS = {
    "unrealistic_runtime": fix_clamp_runtime,
    "calendar_no_active_days": fix_default_calendar,
}
