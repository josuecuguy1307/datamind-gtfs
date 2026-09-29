"""S2 Shadow + Cross-Validator gate — the decision engine.

For each detected issue:
1. Run the shared fixer to produce a FixAttempt.
2. If fixer failed -> REJECT immediately.
3. Build an in-memory Shadow (deep copy + apply fix).
4. Run the cross-validator for that rule on the shadow entity.
5. If cross-validator passes -> COMMIT.  If it fails -> REJECT.

No entity is mutated in the real input — shadows are ephemeral.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from ...shared.contract import StrategyBase
from ...shared.models import (
    DecisionType,
    EntityIssue,
    FixAttempt,
    GateReport,
    QualityGateInput,
    QualityVerdict,
    Route,
    RouteBack,
    RouteSemantics,
    ScheduleProfile,
    Shape,
    Stop,
    StrategyDecision,
)
from ...shared.rules.stop_rules import BATCH_RULES as STOP_BATCH_RULES
from ...shared.rules.stop_rules import RULES as STOP_RULES
from ...shared.rules.route_rules import RULES as ROUTE_RULES
from ...shared.rules.shape_rules import RULES as SHAPE_RULES
from ...shared.rules.naming_rules import RULES as NAMING_RULES
from ...shared.rules.timing_rules import RULES as TIMING_RULES
from .cross_validators import CROSS_VALIDATORS
from .shadow import Shadow

Entity = Union[Stop, Route, Shape, RouteSemantics, ScheduleProfile]

# Phase reroute targets — which phase to send rejects back to
_REROUTE_PHASE: Dict[str, int] = {
    "stop_name_placeholder": 1,
    "stop_name_empty": 1,
    "stop_name_uuid_prefix": 5,
    "stop_coords_outside_bbox": 1,
    "stop_coords_null_island": 1,
    "stop_ref_garbage": 1,
    "stop_duplicate_nearby": 1,
    "route_too_few_stops": 3,
    "route_no_schedule": 4,
    "shape_gap_too_large": 3,
    "shape_self_intersection": 3,
    "route_name_garbage": 4,
    "short_name_collision": 4,
    "operator_inconsistency": 4,
    "unrealistic_runtime": 5,
    "calendar_no_active_days": 5,
}

# Reroute actions
_REROUTE_ACTION: Dict[str, str] = {
    "stop_name_placeholder": "re_geocode",
    "stop_name_empty": "re_geocode",
    "stop_name_uuid_prefix": "re_geocode",
    "stop_coords_outside_bbox": "re_survey",
    "stop_coords_null_island": "re_survey",
    "stop_ref_garbage": "re_identify",
    "stop_duplicate_nearby": "re_dedup",
    "route_too_few_stops": "re_ground",
    "route_no_schedule": "re_schedule",
    "shape_gap_too_large": "re_trace",
    "shape_self_intersection": "re_construct",
    "route_name_garbage": "re_name",
    "short_name_collision": "re_name",
    "operator_inconsistency": "re_canonicalize",
    "unrealistic_runtime": "re_time",
    "calendar_no_active_days": "re_calendar",
}


class S2ShadowGate(StrategyBase):
    """Shadow + Cross-Validator strategy.

    Higher precision than S1 (fewer silent corruptions) at the cost of
    lower autonomy (more rejections when cross-validator disagrees).
    """

    name = "s2_shadow"

    def run(self, input: QualityGateInput) -> GateReport:
        """Execute the full gate pass."""
        all_issues: List[EntityIssue] = []
        all_fixes: List[FixAttempt] = []
        all_decisions: List[StrategyDecision] = []
        all_routebacks: List[RouteBack] = []

        entities_checked = 0

        # ----- Detect issues -----
        # Stop rules (single-entity)
        for stop in input.stops:
            entities_checked += 1
            for rule_fn in STOP_RULES:
                all_issues.extend(rule_fn(stop))

        # Stop batch rules (duplicate detection)
        if input.stops:
            for batch_fn in STOP_BATCH_RULES:
                all_issues.extend(batch_fn(input.stops))

        # Route rules
        for route in input.routes:
            entities_checked += 1
            for rule_fn in ROUTE_RULES:
                all_issues.extend(rule_fn(route))

        # Shape rules
        for shape in input.shapes:
            entities_checked += 1
            for rule_fn in SHAPE_RULES:
                all_issues.extend(rule_fn(shape))

        # Naming rules
        for sem in input.semantics:
            entities_checked += 1
            for rule_fn in NAMING_RULES:
                all_issues.extend(rule_fn(sem))

        # Timing rules
        for sched in input.schedules:
            entities_checked += 1
            for rule_fn in TIMING_RULES:
                all_issues.extend(rule_fn(sched))

        # ----- Attempt fixes and decide -----
        entity_index = _build_entity_index(input)

        for issue in all_issues:
            entity = entity_index.get((issue.entity_type, issue.entity_id))
            fix = _attempt_fix(issue, entity, input)
            all_fixes.append(fix)

            decision = self.decide(issue, fix, entity)
            all_decisions.append(decision)

            if decision.decision == DecisionType.REJECT:
                all_routebacks.append(RouteBack(
                    entity_id=issue.entity_id,
                    entity_type=issue.entity_type,
                    target_phase=_REROUTE_PHASE.get(issue.rule_name, issue.phase_origin),
                    action=_REROUTE_ACTION.get(issue.rule_name, "manual_review"),
                    reason=decision.reason,
                    priority="high" if issue.severity.value == "error" else "medium",
                ))

        # ----- Build verdict -----
        committed = sum(1 for d in all_decisions if d.decision == DecisionType.COMMIT)
        rejected = sum(1 for d in all_decisions if d.decision == DecisionType.REJECT)

        if not all_issues:
            status = "pass"
        elif rejected == 0:
            status = "pass_with_fixes"
        else:
            status = "fail"

        return GateReport(
            canton=input.canton,
            province=input.province,
            export_run_id=input.export_run_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            verdict=QualityVerdict(
                status=status,
                entities_checked=entities_checked,
                issues_found=len(all_issues),
                issues_auto_fixed=committed,
                issues_requiring_review=rejected,
            ),
            issues=all_issues,
            fixes_attempted=all_fixes,
            route_backs=all_routebacks,
            pass_to_phase5=(status != "fail"),
            strategy_name=self.name,
            decisions=all_decisions,
        )

    def decide(
        self,
        issue: EntityIssue,
        proposed_fix: FixAttempt,
        entity: Optional[Entity] = None,
    ) -> StrategyDecision:
        """Shadow + cross-validate a single proposed fix.

        The two-arg signature satisfies the StrategyBase contract;
        the optional *entity* parameter is used internally by run().
        """
        if not proposed_fix.success:
            return StrategyDecision(
                decision=DecisionType.REJECT,
                reason=f"Fixer failed: {proposed_fix.log}",
            )

        # Look up cross-validator
        validator = CROSS_VALIDATORS.get(issue.rule_name)

        if validator is None:
            # No cross-validator registered — strict threshold fallback
            if proposed_fix.confidence >= 0.9:
                return StrategyDecision(
                    decision=DecisionType.COMMIT,
                    reason=f"No cross-validator for {issue.rule_name!r}; "
                           f"confidence {proposed_fix.confidence:.2f} >= 0.9",
                    confidence_threshold_used=0.9,
                )
            return StrategyDecision(
                decision=DecisionType.REJECT,
                reason=f"No cross-validator for {issue.rule_name!r}; "
                       f"confidence {proposed_fix.confidence:.2f} < 0.9",
                confidence_threshold_used=0.9,
            )

        # Build shadow (needs the entity)
        if entity is not None:
            shadow = Shadow.from_fix(entity, proposed_fix)
            shadow_entity = shadow.mutated
        else:
            # If no entity provided (direct decide() call), use fix as-is
            shadow_entity = None  # type: ignore[assignment]

        passed, reason = validator(issue, proposed_fix, shadow_entity)

        if passed:
            return StrategyDecision(
                decision=DecisionType.COMMIT,
                reason=f"Cross-validator passed for {issue.rule_name!r}: {reason}",
            )

        return StrategyDecision(
            decision=DecisionType.REJECT,
            reason=f"Cross-validator REJECTED for {issue.rule_name!r}: {reason}",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_entity_index(
    input: QualityGateInput,
) -> Dict[tuple, Entity]:
    """Build a (entity_type, entity_id) -> entity lookup."""
    idx: Dict[tuple, Entity] = {}
    for stop in input.stops:
        idx[("stop", stop.stop_id)] = stop
    for route in input.routes:
        idx[("route", route.route_id)] = route
    for shape in input.shapes:
        idx[("shape", shape.shape_id)] = shape
    for sem in input.semantics:
        idx[("naming", sem.route_id)] = sem
    for sched in input.schedules:
        idx[("timing", sched.route_id)] = sched
    return idx


def _attempt_fix(
    issue: EntityIssue,
    entity: Optional[Entity],
    input: QualityGateInput,
) -> FixAttempt:
    """Dispatch to the appropriate shared fixer."""
    rule = issue.rule_name

    # Import fixer registries
    from ...shared.fixers.stop_fixer import FIXERS as STOP_FIXERS
    from ...shared.fixers.route_fixer import FIXERS as ROUTE_FIXERS
    from ...shared.fixers.shape_fixer import FIXERS as SHAPE_FIXERS
    from ...shared.fixers.naming_fixer import FIXERS as NAMING_FIXERS
    from ...shared.fixers.timing_fixer import FIXERS as TIMING_FIXERS

    # Stop fixers — special handling for duplicate merge (needs two stops)
    if rule == "stop_duplicate_nearby" and isinstance(entity, Stop):
        from ...shared.fixers.stop_fixer import fix_merge_duplicates
        other_id = (issue.original_value or {}).get("other_id") if isinstance(issue.original_value, dict) else None
        if other_id:
            other = next((s for s in input.stops if s.stop_id == other_id), None)
            if other:
                return fix_merge_duplicates(entity, other, issue)
        return FixAttempt(
            success=False, new_value=None, confidence=0.0,
            log=f"Cannot find duplicate partner for {entity.stop_id[:8]}",
        )

    if rule in STOP_FIXERS and isinstance(entity, Stop):
        return STOP_FIXERS[rule](entity, issue)

    # Route fixers
    if rule in ROUTE_FIXERS and isinstance(entity, Route):
        if rule == "route_too_few_stops":
            return ROUTE_FIXERS[rule](entity, issue, candidates=input.routes)
        return ROUTE_FIXERS[rule](entity, issue)

    # Shape fixers
    if rule in SHAPE_FIXERS and isinstance(entity, Shape):
        return SHAPE_FIXERS[rule](entity, issue)

    # Naming fixers
    if rule in NAMING_FIXERS and isinstance(entity, RouteSemantics):
        return NAMING_FIXERS[rule](entity, issue)

    # Timing fixers
    if rule in TIMING_FIXERS and isinstance(entity, ScheduleProfile):
        return TIMING_FIXERS[rule](entity, issue)

    # No fixer found
    return FixAttempt(
        success=False,
        new_value=None,
        confidence=0.0,
        log=f"No fixer registered for rule {rule!r} / entity type {type(entity).__name__}",
    )
