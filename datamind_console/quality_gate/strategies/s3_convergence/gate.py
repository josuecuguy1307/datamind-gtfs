"""S3 Two-Pass Convergence Gate.

Runs the full quality gate in a loop, applying fixes speculatively to a
working copy. Commits only when the system reaches a fixed point (no new
issues created by the fixes, no oscillation). Max 3 iterations.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from ...shared.contract import StrategyBase
from ...shared.models import (
    DecisionType,
    EntityIssue,
    FixAttempt,
    GateReport,
    QualityGateInput,
    QualityVerdict,
    RouteBack,
    StrategyDecision,
    Stop,
)
from ...shared.fixers import get_fixer as _get_shared_fixer
from ...shared.rules import stop_rules, route_rules, shape_rules, naming_rules, timing_rules
from .convergence import (
    PassResult,
    compute_pass_diff,
    convergence_summary,
    is_converged,
    is_diverged,
    make_issue_key,
)
from .working_copy import WorkingCopy


class S3ConvergenceGate(StrategyBase):
    """Two-pass convergence: apply all fixes, re-run, commit only at fixed point."""

    name = "s3_convergence"
    MAX_ITERATIONS = 3
    BASE_CONFIDENCE_THRESHOLD = 0.5

    def run(self, input: QualityGateInput) -> GateReport:
        working = WorkingCopy.from_input(input)
        passes: List[PassResult] = []
        all_fixes: List[Tuple[EntityIssue, FixAttempt, StrategyDecision]] = []
        all_decisions: List[StrategyDecision] = []
        prev_issue_keys: Set[str] = set()
        # Track which (entity_type, entity_id, rule_name) had fixes applied
        applied_fix_keys: Set[str] = set()

        for iteration in range(1, self.MAX_ITERATIONS + 1):
            # --- 1. Detect all issues against the working copy ---
            issues = self._detect_all(working)
            current_issue_keys = {
                make_issue_key(i.entity_type, i.entity_id, i.rule_name)
                for i in issues
            }

            # --- 2. Propose fixes for each issue ---
            proposed: List[Tuple[EntityIssue, FixAttempt]] = []
            for issue in issues:
                fix = self._propose_fix(issue, working)
                proposed.append((issue, fix))

            # --- 3. Decide on each fix ---
            to_apply: List[Tuple[EntityIssue, FixAttempt]] = []
            deferred_count = 0
            for issue, fix in proposed:
                decision = self.decide(issue, fix)
                all_decisions.append(decision)
                if decision.decision == DecisionType.COMMIT:
                    to_apply.append((issue, fix))
                elif decision.decision == DecisionType.DEFER:
                    deferred_count += 1

            # --- 4. Compute pass stats BEFORE applying fixes ---
            new_count, resolved_count = compute_pass_diff(
                current_issue_keys, prev_issue_keys
            ) if passes else (len(issues), 0)

            # Count undone fixes: issues that re-appear after being fixed,
            # or fixes proposed that contradict a prior fix on the same entity
            undone = self._count_undone(to_apply, applied_fix_keys)

            pass_result = PassResult(
                pass_num=iteration,
                issues_found=len(issues),
                fixes_proposed=len(proposed),
                fixes_applied=len(to_apply),
                fixes_deferred=deferred_count,
                new_issues_vs_previous=new_count if passes else 0,
                resolved_from_previous=resolved_count,
                undone_fixes=undone,
                issue_keys=current_issue_keys,
            )
            passes.append(pass_result)

            # --- 5. Apply fixes to working copy (NOT prod) ---
            for issue, fix in to_apply:
                # Handle merge-remove (duplicate resolution)
                if isinstance(fix.new_value, dict) and "__merge_remove__" in fix.new_value:
                    remove_id = fix.new_value["__merge_remove__"]
                    working.remove_entity(issue.entity_type, remove_id, iteration)
                else:
                    working.apply_fix(
                        issue.entity_type, issue.entity_id, fix, iteration
                    )
                key = make_issue_key(
                    issue.entity_type, issue.entity_id, issue.rule_name
                )
                applied_fix_keys.add(key)
                all_fixes.append((issue, fix, StrategyDecision(
                    decision=DecisionType.COMMIT,
                    reason=f"speculative apply pass {iteration}",
                    confidence_threshold_used=self.BASE_CONFIDENCE_THRESHOLD,
                )))

            prev_issue_keys = current_issue_keys

            # --- 6. Convergence check ---
            if is_converged(passes):
                return self._build_report(
                    input, working, all_fixes, all_decisions, passes,
                    verdict_status="pass_with_fixes" if all_fixes else "pass",
                )

            if is_diverged(passes):
                working.revert_all()
                return self._build_report(
                    input, working, [], all_decisions, passes,
                    verdict_status="fail",
                    fail_reason="diverged: fixes oscillating across passes",
                )

        # --- Max iterations without convergence ---
        working.revert_all()
        return self._build_report(
            input, working, [], all_decisions, passes,
            verdict_status="fail",
            fail_reason=f"max iterations ({self.MAX_ITERATIONS}) without convergence",
        )

    def decide(self, issue: EntityIssue, proposed_fix: FixAttempt) -> StrategyDecision:
        """S3 is permissive per-fix; convergence is the real filter.

        Accept any successful fix with confidence >= threshold.
        Defer low-confidence fixes to the next pass (post-fix context may help).
        Reject failed fixes outright.
        """
        if not proposed_fix.success:
            return StrategyDecision(
                decision=DecisionType.REJECT,
                reason=f"fix failed: {proposed_fix.log}",
                confidence_threshold_used=self.BASE_CONFIDENCE_THRESHOLD,
            )
        if proposed_fix.confidence >= self.BASE_CONFIDENCE_THRESHOLD:
            return StrategyDecision(
                decision=DecisionType.COMMIT,
                reason=f"confidence {proposed_fix.confidence:.2f} >= {self.BASE_CONFIDENCE_THRESHOLD}",
                confidence_threshold_used=self.BASE_CONFIDENCE_THRESHOLD,
            )
        return StrategyDecision(
            decision=DecisionType.DEFER,
            reason=f"confidence {proposed_fix.confidence:.2f} < {self.BASE_CONFIDENCE_THRESHOLD}, deferring",
            confidence_threshold_used=self.BASE_CONFIDENCE_THRESHOLD,
        )

    # ------------------------------------------------------------------
    # Internal: rule detection
    # ------------------------------------------------------------------

    def _detect_all(self, working: WorkingCopy) -> List[EntityIssue]:
        """Run every rule against the working copy's current state."""
        issues: List[EntityIssue] = []

        # Stop rules (single-entity)
        for stop in working.stops.values():
            for rule_fn in stop_rules.RULES:
                issues.extend(rule_fn(stop))

        # Stop batch rules (duplicate detection)
        stop_list = list(working.stops.values())
        for batch_fn in stop_rules.BATCH_RULES:
            issues.extend(batch_fn(stop_list))

        # Route rules
        for route in working.routes.values():
            for rule_fn in route_rules.RULES:
                issues.extend(rule_fn(route))

        # Shape rules
        for shape in working.shapes.values():
            for rule_fn in shape_rules.RULES:
                issues.extend(rule_fn(shape))

        # Naming rules
        for sem in working.semantics.values():
            for rule_fn in naming_rules.RULES:
                issues.extend(rule_fn(sem))

        # Timing rules
        for sched in working.schedules.values():
            for rule_fn in timing_rules.RULES:
                issues.extend(rule_fn(sched))

        return issues

    # ------------------------------------------------------------------
    # Internal: fix proposal (stub — delegates to fixers when available)
    # ------------------------------------------------------------------

    def _propose_fix(self, issue: EntityIssue, working: WorkingCopy) -> FixAttempt:
        """Propose a fix for a detected issue.

        Delegates to shared fixers via _adapt_shared_fixer. Returns a failed
        FixAttempt if no fixer is registered for the rule.
        """
        entity = working.get_entity(issue.entity_type, issue.entity_id)
        return _adapt_shared_fixer(issue, entity, working)

    # ------------------------------------------------------------------
    # Internal: undone-fix counting
    # ------------------------------------------------------------------

    @staticmethod
    def _count_undone(
        to_apply: List[Tuple[EntityIssue, FixAttempt]],
        previously_applied: Set[str],
    ) -> int:
        """Count how many proposed fixes target entities already fixed in prior passes."""
        count = 0
        for issue, _fix in to_apply:
            key = make_issue_key(issue.entity_type, issue.entity_id, issue.rule_name)
            if key in previously_applied:
                count += 1
        return count

    # ------------------------------------------------------------------
    # Internal: report building
    # ------------------------------------------------------------------

    def _build_report(
        self,
        input: QualityGateInput,
        working: WorkingCopy,
        committed_fixes: List[Tuple[EntityIssue, FixAttempt, StrategyDecision]],
        all_decisions: List[StrategyDecision],
        passes: List[PassResult],
        verdict_status: str,
        fail_reason: Optional[str] = None,
    ) -> GateReport:
        total_entities = sum(
            len(v) for v in working.to_entity_lists().values()
        )
        # Final issue count from last pass
        final_issues = passes[-1].issues_found if passes else 0
        fixed_count = len(committed_fixes)

        # Build route-backs for unfixed ERROR issues
        route_backs: List[RouteBack] = []
        if verdict_status == "fail":
            last_pass_issues = self._detect_all(working)
            for issue in last_pass_issues:
                if issue.severity.value == "error":
                    route_backs.append(RouteBack(
                        entity_id=issue.entity_id,
                        entity_type=issue.entity_type,
                        target_phase=issue.phase_origin,
                        action="re_review",
                        reason=issue.description,
                        priority="high" if verdict_status == "fail" else "medium",
                    ))

        verdict = QualityVerdict(
            status=verdict_status,
            entities_checked=total_entities,
            issues_found=final_issues,
            issues_auto_fixed=fixed_count,
            issues_requiring_review=final_issues - fixed_count if final_issues > fixed_count else 0,
        )

        report = GateReport(
            canton=input.canton,
            province=input.province,
            export_run_id=input.export_run_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            verdict=verdict,
            issues=[iss for iss, _, _ in committed_fixes],
            fixes_attempted=[fix for _, fix, _ in committed_fixes],
            route_backs=route_backs,
            pass_to_phase5=(verdict_status in ("pass", "pass_with_fixes")),
            strategy_name=self.name,
            decisions=all_decisions,
        )

        # Attach convergence diagnostics as extra metadata
        report.__dict__["convergence_trace"] = convergence_summary(passes)
        report.__dict__["total_mutations"] = working.total_mutations
        if fail_reason:
            report.__dict__["fail_reason"] = fail_reason

        return report


# ---------------------------------------------------------------------------
# Fixer adapter — bridges shared fixers (issue, context) to S3's calling
# convention (issue, entity, working_copy).
# ---------------------------------------------------------------------------

def _build_context(entity, working_copy) -> dict:
    """Build a context dict from the entity for the shared fixer."""
    ctx: dict = {}
    if entity is None:
        return ctx

    # Stop context
    if isinstance(entity, Stop):
        ctx["coords"] = (entity.lat, entity.lon)
        ctx["reverse_geocode_name"] = f"Parada ({entity.lat:.4f}, {entity.lon:.4f})"
        ctx["reverse_geocode_confidence"] = 0.6

    # Duplicate merge: build keep/remove context from working copy
    if hasattr(entity, "stop_id") and working_copy:
        ctx["entity"] = entity
        ctx["working_copy"] = working_copy

    return ctx


def _adapt_shared_fixer(issue, entity, wc) -> FixAttempt:
    """Call the shared fixer with an adapter for S3's (issue, entity, wc) convention.

    Special cases (duplicates, merge-remove) that depend on the working copy
    are handled inline since the shared fixer signature can't express them.
    """
    if entity is None:
        return FixAttempt(False, None, 0.0, "entity not found")

    # Special: duplicate merge needs working-copy access
    if issue.rule_name == "stop_duplicate_nearby":
        return _fix_stop_duplicate_nearby(issue, entity, wc)

    shared_fixer = _get_shared_fixer(issue.rule_name)
    if shared_fixer is None:
        return FixAttempt(False, None, 0.0,
                          f"no shared fixer for rule {issue.rule_name!r}")

    ctx = _build_context(entity, wc)
    result = shared_fixer(issue, context=ctx)

    # Wrap scalar new_value in a dict patch for WorkingCopy.apply_fix
    if result.success and result.new_value is not None:
        val = result.new_value
        if isinstance(val, dict):
            pass  # already a patch dict
        elif isinstance(val, str) and issue.entity_type == "shape":
            # Shape fixers return placeholder strings like "simplified" —
            # these are not real geometry data, so mark as failed
            result = FixAttempt(False, None, 0.0,
                                f"shape fixer returned placeholder: {val!r}")
        elif isinstance(val, str) and issue.entity_type == "stop":
            result = FixAttempt(True, {"name": val}, result.confidence, result.log)
        elif isinstance(val, str) and issue.entity_type == "naming":
            result = FixAttempt(True, {"route_long_name": val}, result.confidence, result.log)
        elif isinstance(val, tuple) and len(val) == 2 and issue.entity_type == "stop":
            result = FixAttempt(True, {"lat": val[0], "lon": val[1]}, result.confidence, result.log)
        elif isinstance(val, (int, float)) and issue.entity_type == "timing":
            result = FixAttempt(True, {"peak_runtime_min": val}, result.confidence, result.log)

    return result


def _fix_stop_duplicate_nearby(issue, entity, wc):
    """Merge duplicate: keep entity, remove the other (needs working-copy)."""
    other_id = issue.original_value.get("other_id") if isinstance(issue.original_value, dict) else None
    if other_id and other_id in wc.stops:
        other = wc.stops[other_id]
        if other.confidence <= entity.confidence:
            return FixAttempt(
                True, {"__merge_remove__": other_id}, 0.7,
                f"merge: remove {other_id[:8]} (lower confidence)",
            )
        else:
            return FixAttempt(
                True, {"__merge_remove__": entity.stop_id}, 0.7,
                f"merge: remove {entity.stop_id[:8]} (lower confidence)",
            )
    return FixAttempt(False, None, 0.0, "duplicate target not found")
