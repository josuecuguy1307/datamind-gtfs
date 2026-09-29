"""Naming quality rules — gaps C3, C12, C13.

Each rule is a pure function: (RouteSemantics) -> list[EntityIssue]. No DB writes.
"""
from __future__ import annotations

from typing import Callable, List

from ..config import ROUTE_NAME_GARBAGE_PATTERNS, SHORT_NAME_MAX_LENGTH
from ..models import EntityIssue, RouteSemantics, Severity


def rule_route_name_garbage(sem: RouteSemantics) -> List[EntityIssue]:
    """C3: Route name is a random number or nonsensical string.

    Checks route_short_name and route_long_name against garbage patterns.

    phase_origin: 4 (Phase 4 naming)
    rule_name: route_name_garbage
    """
    issues: List[EntityIssue] = []

    for field_name, value in [("route_short_name", sem.route_short_name),
                               ("route_long_name", sem.route_long_name)]:
        if not value:
            continue
        for pat in ROUTE_NAME_GARBAGE_PATTERNS:
            if pat.match(value.strip()):
                issues.append(EntityIssue(
                    entity_type="naming", entity_id=sem.route_id,
                    rule_name="route_name_garbage", severity=Severity.ERROR,
                    description=f"{field_name} matches garbage pattern: {value!r}",
                    original_value=value, phase_origin=4,
                ))
                break
    return issues


def rule_short_name_collision(sem: RouteSemantics) -> List[EntityIssue]:
    """C12: Route short_name collision under same agency.

    Caller must set sem.extra["short_name_collision"] = True if another route
    under the same operator has the same short_name.

    phase_origin: 4
    rule_name: short_name_collision
    """
    if sem.extra.get("short_name_collision", False):
        return [EntityIssue(
            entity_type="naming", entity_id=sem.route_id,
            rule_name="short_name_collision", severity=Severity.WARNING,
            description=f"Short name {sem.route_short_name!r} collides with another route under same operator",
            original_value=sem.route_short_name, phase_origin=4,
        )]
    return []


def rule_operator_inconsistency(sem: RouteSemantics) -> List[EntityIssue]:
    """C13: Operator name inconsistency (same cooperative spelled differently).

    Caller must set sem.extra["canonical_operator"] to the expected canonical form.
    If operator != canonical and canonical is set, flag it.

    phase_origin: 4
    rule_name: operator_inconsistency
    """
    canonical = sem.extra.get("canonical_operator")
    if canonical and sem.operator and sem.operator.strip() != canonical.strip():
        return [EntityIssue(
            entity_type="naming", entity_id=sem.route_id,
            rule_name="operator_inconsistency", severity=Severity.WARNING,
            description=f"Operator {sem.operator!r} should be {canonical!r}",
            original_value=sem.operator, phase_origin=4,
        )]
    return []


RULES: List[Callable[[RouteSemantics], List[EntityIssue]]] = [
    rule_route_name_garbage,
    rule_short_name_collision,
    rule_operator_inconsistency,
]
