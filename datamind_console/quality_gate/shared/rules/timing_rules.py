"""Timing quality rules — gaps C15, C16.

Each rule is a pure function: (ScheduleProfile) -> list[EntityIssue]. No DB writes.
"""
from __future__ import annotations

from typing import Callable, List

from ..config import RUNTIME_MAX_MINUTES, RUNTIME_MIN_MINUTES
from ..models import EntityIssue, ScheduleProfile, Severity


def rule_unrealistic_runtime(sched: ScheduleProfile) -> List[EntityIssue]:
    """C15: Total runtime < 5 min or > 4 hours.

    Checks both peak_runtime_min and offpeak_runtime_min.

    phase_origin: 5 (from build_stop_times distribution)
    rule_name: unrealistic_runtime
    """
    issues: List[EntityIssue] = []
    for field_name, val in [("peak_runtime_min", sched.peak_runtime_min),
                             ("offpeak_runtime_min", sched.offpeak_runtime_min)]:
        if val is None:
            continue
        if val < RUNTIME_MIN_MINUTES:
            issues.append(EntityIssue(
                entity_type="timing", entity_id=sched.route_id,
                rule_name="unrealistic_runtime", severity=Severity.ERROR,
                description=f"{field_name} = {val:.1f} min is below minimum {RUNTIME_MIN_MINUTES}",
                original_value=val, phase_origin=5,
            ))
        elif val > RUNTIME_MAX_MINUTES:
            issues.append(EntityIssue(
                entity_type="timing", entity_id=sched.route_id,
                rule_name="unrealistic_runtime", severity=Severity.ERROR,
                description=f"{field_name} = {val:.1f} min exceeds maximum {RUNTIME_MAX_MINUTES}",
                original_value=val, phase_origin=5,
            ))
    return issues


def rule_calendar_no_active_days(sched: ScheduleProfile) -> List[EntityIssue]:
    """C16: Calendar / service pattern where all days = 0.

    phase_origin: 5
    rule_name: calendar_no_active_days
    """
    days = sched.service_days
    if days is None:
        return []
    if not any(days.values()):
        return [EntityIssue(
            entity_type="timing", entity_id=sched.route_id,
            rule_name="calendar_no_active_days", severity=Severity.ERROR,
            description="Service pattern has no active days (all days = false)",
            original_value=days, phase_origin=5,
        )]
    return []


RULES: List[Callable[[ScheduleProfile], List[EntityIssue]]] = [
    rule_unrealistic_runtime,
    rule_calendar_no_active_days,
]
