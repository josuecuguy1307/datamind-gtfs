"""S1 — Hard Thresholds gate strategy.

For each detected issue, run the corresponding fixer, compare fixer confidence
against a per-rule threshold, and either COMMIT or REJECT. No retries, no
second opinions, no state across decisions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from datamind_console.quality_gate.shared.contract import StrategyBase
from datamind_console.quality_gate.shared.fixers import get_fixer
from datamind_console.quality_gate.shared.models import (
    DecisionType,
    EntityIssue,
    FixAttempt,
    GateReport,
    QualityGateInput,
    QualityVerdict,
    RouteBack,
    StrategyDecision,
)
from datamind_console.quality_gate.shared.rules.naming_rules import RULES as NAMING_RULES
from datamind_console.quality_gate.shared.rules.route_rules import RULES as ROUTE_RULES
from datamind_console.quality_gate.shared.rules.shape_rules import RULES as SHAPE_RULES
from datamind_console.quality_gate.shared.rules.stop_rules import (
    BATCH_RULES as STOP_BATCH_RULES,
    RULES as STOP_RULES,
)
from datamind_console.quality_gate.shared.rules.timing_rules import RULES as TIMING_RULES

from .thresholds import COMMIT_THRESHOLDS, DEFAULT_THRESHOLD

# Phase-origin → route-back action mapping
_ROUTEBACK_ACTIONS: Dict[int, str] = {
    1: "re_extract",
    2: "re_semanticize",
    3: "re_ground",
    4: "re_name",
    5: "re_build_gtfs",
}


class S1ThresholdsGate(StrategyBase):
    """Hard-threshold decision strategy.

    Each issue is decided independently: fixer confidence >= threshold → COMMIT,
    otherwise → REJECT and route-back to origin phase.
    """

    name = "s1_thresholds"

    def __init__(self, overrides: Optional[Dict[str, float]] = None):
        self._thresholds = dict(COMMIT_THRESHOLDS)
        if overrides:
            self._thresholds.update(overrides)

    # ------------------------------------------------------------------
    # StrategyBase.run
    # ------------------------------------------------------------------

    def run(self, input: QualityGateInput) -> GateReport:
        issues: List[EntityIssue] = []
        fixes: List[FixAttempt] = []
        decisions: List[StrategyDecision] = []
        route_backs: List[RouteBack] = []

        committed = 0

        # ── 1. Detect issues ─────────────────────────────────────────
        # Stop rules (per-entity)
        for stop in input.stops:
            for rule_fn in STOP_RULES:
                issues.extend(rule_fn(stop))

        # Stop batch rules (duplicate detection)
        for batch_fn in STOP_BATCH_RULES:
            issues.extend(batch_fn(input.stops))

        # Route rules
        for route in input.routes:
            for rule_fn in ROUTE_RULES:
                issues.extend(rule_fn(route))

        # Shape rules
        for shape in input.shapes:
            for rule_fn in SHAPE_RULES:
                issues.extend(rule_fn(shape))

        # Naming rules
        for sem in input.semantics:
            for rule_fn in NAMING_RULES:
                issues.extend(rule_fn(sem))

        # Timing rules
        for sched in input.schedules:
            for rule_fn in TIMING_RULES:
                issues.extend(rule_fn(sched))

        # Build entity lookup for fixer context enrichment
        _stop_by_id = {s.stop_id: s for s in input.stops}
        _route_by_id = {r.route_id: r for r in input.routes}
        _sem_by_route = {s.route_id: s for s in input.semantics}

        # ── 2. For each issue: fix → decide ──────────────────────────
        for issue in issues:
            fixer = get_fixer(issue.rule_name)
            if fixer is None:
                # No fixer registered — reject by default
                fix_attempt = FixAttempt(
                    success=False, new_value=None, confidence=0.0,
                    log=f"No fixer registered for rule {issue.rule_name!r}",
                )
            else:
                ctx = self._build_fixer_context(issue, _stop_by_id, _sem_by_route, _route_by_id)
                fix_attempt = fixer(issue, context=ctx)

            fixes.append(fix_attempt)

            decision = self.decide(issue, fix_attempt)
            decisions.append(decision)

            if decision.decision == DecisionType.COMMIT:
                committed += 1
            elif decision.decision == DecisionType.REJECT:
                route_backs.append(RouteBack(
                    entity_id=issue.entity_id,
                    entity_type=issue.entity_type,
                    target_phase=issue.phase_origin,
                    action=_ROUTEBACK_ACTIONS.get(issue.phase_origin, "re_review"),
                    reason=(
                        f"{issue.rule_name}: fixer confidence "
                        f"{fix_attempt.confidence:.2f} < threshold "
                        f"{decision.confidence_threshold_used or 0:.2f}"
                    ),
                    priority="high" if issue.severity.value == "error" else "medium",
                ))

        # ── 3. Build verdict ─────────────────────────────────────────
        total_entities = (
            len(input.stops) + len(input.routes) + len(input.shapes) +
            len(input.semantics) + len(input.schedules) + len(input.fares)
        )

        if not issues:
            status = "pass"
        elif route_backs:
            status = "fail"
        else:
            status = "pass_with_fixes"

        verdict = QualityVerdict(
            status=status,
            entities_checked=total_entities,
            issues_found=len(issues),
            issues_auto_fixed=committed,
            issues_requiring_review=len(route_backs),
        )

        return GateReport(
            canton=input.canton,
            province=input.province,
            export_run_id=input.export_run_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            verdict=verdict,
            issues=issues,
            fixes_attempted=fixes,
            route_backs=route_backs,
            pass_to_phase5=(status != "fail"),
            strategy_name=self.name,
            decisions=decisions,
        )

    # ------------------------------------------------------------------
    # StrategyBase.decide
    # ------------------------------------------------------------------

    def decide(self, issue: EntityIssue, proposed_fix: FixAttempt) -> StrategyDecision:
        threshold = self._thresholds.get(issue.rule_name, DEFAULT_THRESHOLD)

        if not proposed_fix.success:
            return StrategyDecision(
                decision=DecisionType.REJECT,
                reason=f"Fixer failed: {proposed_fix.log}",
                confidence_threshold_used=threshold,
            )

        if proposed_fix.confidence >= threshold:
            return StrategyDecision(
                decision=DecisionType.COMMIT,
                reason=(
                    f"Confidence {proposed_fix.confidence:.2f} >= "
                    f"threshold {threshold:.2f}"
                ),
                confidence_threshold_used=threshold,
            )

        return StrategyDecision(
            decision=DecisionType.REJECT,
            reason=(
                f"Confidence {proposed_fix.confidence:.2f} < "
                f"threshold {threshold:.2f}"
            ),
            confidence_threshold_used=threshold,
        )

    # ------------------------------------------------------------------
    # Context builder — enriches fixer calls with entity-level data
    # ------------------------------------------------------------------

    @staticmethod
    def _build_fixer_context(issue, stop_by_id, sem_by_route, route_by_id=None):
        """Build a context dict from input data to help fixers succeed."""
        ctx = {}
        route_by_id = route_by_id or {}

        if issue.entity_type == "stop" and issue.entity_id in stop_by_id:
            stop = stop_by_id[issue.entity_id]
            ctx["coords"] = (stop.lat, stop.lon)

        if issue.entity_type == "route" and issue.rule_name.startswith("route_geometry_"):
            route = route_by_id.get(issue.entity_id)
            if route:
                stop_coords = []
                for sid in route.stop_node_ids:
                    s = stop_by_id.get(sid)
                    if s:
                        stop_coords.append((s.lat, s.lon))
                if stop_coords:
                    ctx["stop_coordinates"] = stop_coords

        if issue.entity_type == "naming" and issue.entity_id in sem_by_route:
            sem = sem_by_route[issue.entity_id]
            if sem.operator:
                ctx["operator"] = sem.operator
            if sem.public_origin:
                ctx["origin"] = sem.public_origin
            if sem.public_destination:
                ctx["destination"] = sem.public_destination
            if hasattr(sem, "extra") and sem.extra.get("canonical_operator"):
                ctx["canonical_operator"] = sem.extra["canonical_operator"]
        return ctx
