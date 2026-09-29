"""S4 Hybrid Gate — Threshold + Shadow Cross-Validator + Convergence.

Every fix must survive three filters before committing:
  L1: confidence >= per-rule threshold  (cheapest, short-circuits first)
  L2: independent cross-validator on a shadow copy  (medium cost)
  L3: canton-level convergence — the full gate re-runs after all L1+L2-approved
      fixes are applied; commit only if the system reaches a fixed point.

Self-contained: does NOT import from s1, s2, or s3 strategy directories.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
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
from ...shared.rules import route_rules, shape_rules, stop_rules, naming_rules, timing_rules
from .layered_decision import (
    LayeredDecision,
    LayerResult,
    layer1_threshold,
    layer2_cross_validator,
)
from .thresholds import threshold_for


# ---------------------------------------------------------------------------
# Convergence types (inline — same logic as S3 but no cross-import)
# ---------------------------------------------------------------------------

@dataclass
class PassResult:
    pass_num: int
    issues_found: int
    fixes_proposed: int
    fixes_applied: int
    fixes_deferred: int
    new_issues_vs_previous: int
    resolved_from_previous: int
    undone_fixes: int
    issue_keys: Set[str] = field(default_factory=set)


def _make_issue_key(entity_type: str, entity_id: str, rule_name: str) -> str:
    return f"{entity_type}::{entity_id}::{rule_name}"


def _compute_pass_diff(current: Set[str], previous: Set[str]) -> Tuple[int, int]:
    return len(current - previous), len(previous - current)


def _is_converged(passes: List[PassResult]) -> bool:
    if len(passes) < 2:
        return False
    latest = passes[-1]
    return latest.new_issues_vs_previous == 0


def _is_diverged(passes: List[PassResult]) -> bool:
    """Diverged: the last 3 passes show a pattern of accumulating issues
    without improvement. Specifically: the 2 most recent passes each
    introduced new issues, AND the final pass did not decrease total
    issue count. Pass 0 of the window is ignored because
    new_issues_vs_previous is structurally 0 on the first observation.
    """
    if len(passes) < 3:
        return False
    last_three = passes[-3:]
    all_introduced_new = all(
        p.new_issues_vs_previous > 0 for p in last_three[1:]
    )
    not_decreasing = last_three[-1].issues_found >= last_three[-2].issues_found
    return all_introduced_new and not_decreasing


def _convergence_summary(passes: List[PassResult]) -> dict:
    return {
        "num_passes": len(passes),
        "converged": _is_converged(passes),
        "diverged": _is_diverged(passes),
        "issues_per_pass": [p.issues_found for p in passes],
        "new_per_pass": [p.new_issues_vs_previous for p in passes],
        "resolved_per_pass": [p.resolved_from_previous for p in passes],
        "undone_per_pass": [p.undone_fixes for p in passes],
        "fixes_applied_per_pass": [p.fixes_applied for p in passes],
    }


# ---------------------------------------------------------------------------
# Working copy (inline — same as S3 but self-contained)
# ---------------------------------------------------------------------------

@dataclass
class _Mutation:
    entity_type: str
    entity_id: str
    field_name: str
    old_value: object
    new_value: object
    pass_num: int


class _WorkingCopy:
    """Minimal copy-on-write entity store."""

    def __init__(self, inp: QualityGateInput):
        self.canton = inp.canton
        self.province = inp.province
        self.stops: Dict[str, Stop] = {s.stop_id: copy.deepcopy(s) for s in inp.stops}
        self.routes = {r.route_id: copy.deepcopy(r) for r in inp.routes}
        self.shapes = {s.shape_id: copy.deepcopy(s) for s in inp.shapes}
        self.semantics = {s.route_id: copy.deepcopy(s) for s in inp.semantics}
        self.schedules = {s.route_id: copy.deepcopy(s) for s in inp.schedules}
        self.fares = {f.fare_id: copy.deepcopy(f) for f in inp.fares}
        self.mutations_log: List[_Mutation] = []
        self._originals = {
            "stops": {s.stop_id: copy.deepcopy(s) for s in inp.stops},
            "routes": {r.route_id: copy.deepcopy(r) for r in inp.routes},
            "shapes": {s.shape_id: copy.deepcopy(s) for s in inp.shapes},
            "semantics": {s.route_id: copy.deepcopy(s) for s in inp.semantics},
            "schedules": {s.route_id: copy.deepcopy(s) for s in inp.schedules},
            "fares": {f.fare_id: copy.deepcopy(f) for f in inp.fares},
        }

    _STORE_MAP = {
        "stop": "stops", "route": "routes", "shape": "shapes",
        "naming": "semantics", "timing": "schedules", "fare": "fares",
    }

    _SCALAR_FIELD = {
        "stop": "name", "route": "route_name", "shape": "points",
        "naming": "route_long_name", "timing": "peak_runtime_min", "fare": "price",
    }

    def _store(self, entity_type: str) -> dict:
        attr = self._STORE_MAP.get(entity_type)
        if not attr:
            raise ValueError(f"Unknown entity_type: {entity_type!r}")
        return getattr(self, attr)

    def get_entity(self, entity_type: str, entity_id: str):
        return self._store(entity_type).get(entity_id)

    def apply_fix(self, entity_type: str, entity_id: str,
                  fix: FixAttempt, pass_num: int) -> bool:
        entity = self.get_entity(entity_type, entity_id)
        if entity is None:
            return False
        patches = fix.new_value
        if not isinstance(patches, dict):
            field_name = self._SCALAR_FIELD.get(entity_type, "extra")
            patches = {field_name: patches}
        mutated = False
        for field_name, new_val in patches.items():
            if field_name.startswith("__"):
                continue
            old_val = getattr(entity, field_name, None)
            if old_val != new_val:
                setattr(entity, field_name, new_val)
                self.mutations_log.append(_Mutation(
                    entity_type, entity_id, field_name, old_val, new_val, pass_num,
                ))
                mutated = True
        return mutated

    def remove_entity(self, entity_type: str, entity_id: str, pass_num: int) -> bool:
        store = self._store(entity_type)
        if entity_id in store:
            self.mutations_log.append(_Mutation(
                entity_type, entity_id, "__removed__", True, None, pass_num,
            ))
            del store[entity_id]
            return True
        return False

    def revert_all(self):
        self.stops = copy.deepcopy(self._originals["stops"])
        self.routes = copy.deepcopy(self._originals["routes"])
        self.shapes = copy.deepcopy(self._originals["shapes"])
        self.semantics = copy.deepcopy(self._originals["semantics"])
        self.schedules = copy.deepcopy(self._originals["schedules"])
        self.fares = copy.deepcopy(self._originals["fares"])
        self.mutations_log.clear()

    def to_entity_lists(self) -> dict:
        return {
            "stops": list(self.stops.values()),
            "routes": list(self.routes.values()),
            "shapes": list(self.shapes.values()),
            "semantics": list(self.semantics.values()),
            "schedules": list(self.schedules.values()),
            "fares": list(self.fares.values()),
        }

    @property
    def total_mutations(self) -> int:
        return len(self.mutations_log)


# ---------------------------------------------------------------------------
# Fixer registry (same as S3, copied inline)
# ---------------------------------------------------------------------------

def _fix_stop_name_placeholder(issue, entity, wc):
    if entity is None:
        return FixAttempt(False, None, 0.0, "entity not found")
    return FixAttempt(False, None, 0.0, "placeholder name requires context naming — coordinate fallback disabled")


def _fix_stop_name_empty(issue, entity, wc):
    if entity is None:
        return FixAttempt(False, None, 0.0, "entity not found")
    return FixAttempt(False, None, 0.0, "empty name requires context naming — coordinate fallback disabled")


def _fix_stop_name_uuid_prefix(issue, entity, wc):
    if entity is None:
        return FixAttempt(False, None, 0.0, "entity not found")
    return FixAttempt(False, None, 0.0, "UUID prefix name requires context naming — coordinate fallback disabled")


def _fix_stop_ref_garbage(issue, entity, wc):
    if entity is None:
        return FixAttempt(False, None, 0.0, "entity not found")
    return FixAttempt(True, {"ref": None}, 0.8, "garbage ref cleared")


def _fix_stop_coords_null_island(issue, entity, wc):
    return FixAttempt(False, None, 0.0, "cannot auto-fix null island coords")


def _fix_stop_coords_outside_bbox(issue, entity, wc):
    return FixAttempt(False, None, 0.0, "cannot auto-fix out-of-bounds coords")


def _fix_stop_duplicate_nearby(issue, entity, wc):
    if entity is None:
        return FixAttempt(False, None, 0.0, "entity not found")
    other_id = issue.original_value.get("other_id") if isinstance(issue.original_value, dict) else None
    if other_id and other_id in wc.stops:
        other = wc.stops[other_id]
        if other.confidence <= entity.confidence:
            return FixAttempt(True, {"__merge_remove__": other_id}, 0.7,
                              f"merge: remove {other_id[:8]} (lower confidence)")
        else:
            return FixAttempt(True, {"__merge_remove__": entity.stop_id}, 0.7,
                              f"merge: remove {entity.stop_id[:8]} (lower confidence)")
    return FixAttempt(False, None, 0.0, "duplicate target not found")


def _fix_route_too_few_stops(issue, entity, wc):
    return FixAttempt(False, None, 0.0, "cannot auto-add stops to route")


def _fix_route_no_schedule(issue, entity, wc):
    return FixAttempt(False, None, 0.0, "cannot auto-generate schedule")


def _fix_shape_gap(issue, entity, wc):
    return FixAttempt(False, None, 0.0, "cannot auto-fix shape gap (needs re-routing)")


def _fix_shape_self_intersection(issue, entity, wc):
    return FixAttempt(False, None, 0.0, "cannot auto-fix self-intersection")


def _adapt_shared_fixer(rule_name):
    """Create an (issue, entity, wc) adapter for a shared fixer."""
    def _adapted(issue, entity, wc):
        if entity is None:
            return FixAttempt(False, None, 0.0, "entity not found")
        shared_fixer = _get_shared_fixer(rule_name)
        if shared_fixer is None:
            return FixAttempt(False, None, 0.0, f"no shared fixer for {rule_name!r}")
        ctx: dict = {}
        if isinstance(entity, Stop):
            ctx["coords"] = (entity.lat, entity.lon)
            ctx["reverse_geocode_name"] = f"Parada ({entity.lat:.4f}, {entity.lon:.4f})"
            ctx["reverse_geocode_confidence"] = 0.6
        result = shared_fixer(issue, context=ctx)
        if result.success and result.new_value is not None:
            val = result.new_value
            if isinstance(val, dict):
                pass
            elif isinstance(val, str) and issue.entity_type == "naming":
                result = FixAttempt(True, {"route_long_name": val}, result.confidence, result.log)
            elif isinstance(val, (int, float)) and issue.entity_type == "timing":
                result = FixAttempt(True, {"peak_runtime_min": val}, result.confidence, result.log)
            elif isinstance(val, tuple) and len(val) == 2 and issue.entity_type == "stop":
                result = FixAttempt(True, {"lat": val[0], "lon": val[1]}, result.confidence, result.log)
            elif isinstance(val, str) and issue.entity_type == "stop":
                result = FixAttempt(True, {"name": val}, result.confidence, result.log)
        return result
    return _adapted


_FIXER_REGISTRY: Dict[str, callable] = {
    "stop_name_placeholder": _fix_stop_name_placeholder,
    "stop_name_empty": _fix_stop_name_empty,
    "stop_name_uuid_prefix": _fix_stop_name_uuid_prefix,
    "stop_ref_garbage": _fix_stop_ref_garbage,
    "stop_coords_null_island": _fix_stop_coords_null_island,
    "stop_coords_outside_bbox": _fix_stop_coords_outside_bbox,
    "stop_duplicate_nearby": _fix_stop_duplicate_nearby,
    "route_too_few_stops": _fix_route_too_few_stops,
    "route_no_schedule": _fix_route_no_schedule,
    "shape_gap_too_large": _fix_shape_gap,
    "shape_self_intersection": _fix_shape_self_intersection,
    # Naming fixers (via shared adapter)
    "route_name_garbage": _adapt_shared_fixer("route_name_garbage"),
    "short_name_collision": _adapt_shared_fixer("short_name_collision"),
    "operator_inconsistency": _adapt_shared_fixer("operator_inconsistency"),
    # Timing fixers (via shared adapter)
    "unrealistic_runtime": _adapt_shared_fixer("unrealistic_runtime"),
    "calendar_no_active_days": _adapt_shared_fixer("calendar_no_active_days"),
}


# ---------------------------------------------------------------------------
# S4HybridGate
# ---------------------------------------------------------------------------

class S4HybridGate(StrategyBase):
    """Hybrid strategy: L1(threshold) + L2(cross-validator) + L3(convergence)."""

    name = "s4_hybrid"
    MAX_ITERATIONS = 3

    def run(self, input: QualityGateInput) -> GateReport:
        working = _WorkingCopy(input)
        passes: List[PassResult] = []
        all_committed: List[Tuple[EntityIssue, FixAttempt, StrategyDecision]] = []
        all_decisions: List[StrategyDecision] = []
        layer_decisions: List[LayeredDecision] = []
        prev_issue_keys: Set[str] = set()
        applied_fix_keys: Set[str] = set()

        # Per-layer rejection counters for diagnostics
        l1_rejections = 0
        l2_rejections = 0

        for iteration in range(1, self.MAX_ITERATIONS + 1):
            # 1. Detect all issues
            issues = self._detect_all(working)
            current_keys = {
                _make_issue_key(i.entity_type, i.entity_id, i.rule_name)
                for i in issues
            }

            # 2. Propose fixes
            proposed: List[Tuple[EntityIssue, FixAttempt]] = []
            for issue in issues:
                fix = self._propose_fix(issue, working)
                proposed.append((issue, fix))

            # 3. Three-layer filtering
            after_l1: List[Tuple[EntityIssue, FixAttempt]] = []
            after_l2: List[Tuple[EntityIssue, FixAttempt]] = []

            for issue, fix in proposed:
                if not fix.success:
                    all_decisions.append(StrategyDecision(
                        decision=DecisionType.REJECT,
                        reason=f"fix failed: {fix.log}",
                    ))
                    continue

                # --- Layer 1: threshold ---
                l1_ok, l1_decision = layer1_threshold(issue, fix)
                layer_decisions.append(l1_decision)
                if not l1_ok:
                    l1_rejections += 1
                    all_decisions.append(StrategyDecision(
                        decision=DecisionType.REJECT,
                        reason=(
                            f"L1 rejected: confidence {fix.confidence:.2f} "
                            f"< threshold {l1_decision.threshold_required:.2f}"
                        ),
                        confidence_threshold_used=l1_decision.threshold_required,
                    ))
                    continue
                after_l1.append((issue, fix))

            for issue, fix in after_l1:
                # --- Layer 2: shadow cross-validator ---
                entity = working.get_entity(issue.entity_type, issue.entity_id)
                l2_ok, l2_decision = layer2_cross_validator(issue, fix, entity)
                layer_decisions.append(l2_decision)
                if not l2_ok:
                    l2_rejections += 1
                    all_decisions.append(StrategyDecision(
                        decision=DecisionType.REJECT,
                        reason=f"L2 rejected: {l2_decision.cross_validator_reason}",
                        confidence_threshold_used=l2_decision.threshold_required,
                    ))
                    continue
                after_l2.append((issue, fix))

            # 4. Compute pass stats BEFORE applying
            if passes:
                new_count, resolved_count = _compute_pass_diff(
                    current_keys, prev_issue_keys
                )
            else:
                new_count, resolved_count = len(issues), 0

            undone = self._count_undone(after_l2, applied_fix_keys)

            pass_result = PassResult(
                pass_num=iteration,
                issues_found=len(issues),
                fixes_proposed=len(proposed),
                fixes_applied=len(after_l2),
                fixes_deferred=0,
                new_issues_vs_previous=new_count if passes else 0,
                resolved_from_previous=resolved_count,
                undone_fixes=undone,
                issue_keys=current_keys,
            )
            passes.append(pass_result)

            # 5. Apply L1+L2-approved fixes to working copy
            for issue, fix in after_l2:
                if isinstance(fix.new_value, dict) and "__merge_remove__" in fix.new_value:
                    remove_id = fix.new_value["__merge_remove__"]
                    working.remove_entity(issue.entity_type, remove_id, iteration)
                else:
                    working.apply_fix(
                        issue.entity_type, issue.entity_id, fix, iteration,
                    )
                key = _make_issue_key(
                    issue.entity_type, issue.entity_id, issue.rule_name,
                )
                applied_fix_keys.add(key)
                all_committed.append((issue, fix, StrategyDecision(
                    decision=DecisionType.COMMIT,
                    reason=f"L1+L2 passed, speculative apply pass {iteration}",
                    confidence_threshold_used=threshold_for(issue.rule_name),
                )))
                all_decisions.append(all_committed[-1][2])

            prev_issue_keys = current_keys

            # 6. Layer 3: convergence check
            if _is_converged(passes):
                return self._build_report(
                    input, working, all_committed, all_decisions,
                    passes, layer_decisions,
                    l1_rejections, l2_rejections,
                    verdict_status="pass_with_fixes" if all_committed else "pass",
                )

            if _is_diverged(passes):
                working.revert_all()
                return self._build_report(
                    input, working, [], all_decisions,
                    passes, layer_decisions,
                    l1_rejections, l2_rejections,
                    verdict_status="fail",
                    fail_reason="L3 diverged: fixes oscillating across passes",
                )

        # Max iterations — L3 convergence failed
        working.revert_all()
        layer_decisions.append(LayeredDecision(
            rule_name="__canton__",
            entity_id=input.canton,
            result=LayerResult.REJECTED_BY_CONVERGENCE,
            threshold_confidence=0.0,
            threshold_required=0.0,
            convergence_note=f"Did not converge in {self.MAX_ITERATIONS} passes",
        ))
        return self._build_report(
            input, working, [], all_decisions,
            passes, layer_decisions,
            l1_rejections, l2_rejections,
            verdict_status="fail",
            fail_reason=f"L3: max iterations ({self.MAX_ITERATIONS}) without convergence",
        )

    def decide(self, issue: EntityIssue, proposed_fix: FixAttempt) -> StrategyDecision:
        """Per-fix decide: L1 + L2 only (L3 runs at canton level in run())."""
        if not proposed_fix.success:
            return StrategyDecision(
                decision=DecisionType.REJECT,
                reason=f"fix failed: {proposed_fix.log}",
            )

        # L1
        l1_ok, l1_dec = layer1_threshold(issue, proposed_fix)
        if not l1_ok:
            return StrategyDecision(
                decision=DecisionType.REJECT,
                reason=(
                    f"L1 rejected: confidence {proposed_fix.confidence:.2f} "
                    f"< {l1_dec.threshold_required:.2f}"
                ),
                confidence_threshold_used=l1_dec.threshold_required,
            )

        # L2 — we don't have the entity here, so use fix.new_value as proxy
        # In run(), L2 uses the real entity. decide() is a simplified path.
        return StrategyDecision(
            decision=DecisionType.COMMIT,
            reason=f"L1 passed (confidence {proposed_fix.confidence:.2f}), L2 deferred to run()",
            confidence_threshold_used=l1_dec.threshold_required,
        )

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def _detect_all(self, working: _WorkingCopy) -> List[EntityIssue]:
        issues: List[EntityIssue] = []
        for stop in working.stops.values():
            for rule_fn in stop_rules.RULES:
                issues.extend(rule_fn(stop))
        stop_list = list(working.stops.values())
        for batch_fn in stop_rules.BATCH_RULES:
            issues.extend(batch_fn(stop_list))
        for route in working.routes.values():
            for rule_fn in route_rules.RULES:
                issues.extend(rule_fn(route))
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
    # Fix proposal
    # ------------------------------------------------------------------

    def _propose_fix(self, issue: EntityIssue, working: _WorkingCopy) -> FixAttempt:
        fixer = _FIXER_REGISTRY.get(issue.rule_name)
        if fixer is None:
            return FixAttempt(
                success=False, new_value=None, confidence=0.0,
                log=f"no fixer for rule {issue.rule_name!r}",
            )
        entity = working.get_entity(issue.entity_type, issue.entity_id)
        return fixer(issue, entity, working)

    # ------------------------------------------------------------------
    # Undone-fix counting
    # ------------------------------------------------------------------

    @staticmethod
    def _count_undone(
        to_apply: List[Tuple[EntityIssue, FixAttempt]],
        previously_applied: Set[str],
    ) -> int:
        count = 0
        for issue, _fix in to_apply:
            key = _make_issue_key(issue.entity_type, issue.entity_id, issue.rule_name)
            if key in previously_applied:
                count += 1
        return count

    # ------------------------------------------------------------------
    # Report building
    # ------------------------------------------------------------------

    def _build_report(
        self,
        input: QualityGateInput,
        working: _WorkingCopy,
        committed: List[Tuple[EntityIssue, FixAttempt, StrategyDecision]],
        all_decisions: List[StrategyDecision],
        passes: List[PassResult],
        layer_decisions: List[LayeredDecision],
        l1_rejections: int,
        l2_rejections: int,
        verdict_status: str,
        fail_reason: Optional[str] = None,
    ) -> GateReport:
        total_entities = sum(len(v) for v in working.to_entity_lists().values())
        final_issues = passes[-1].issues_found if passes else 0
        fixed_count = len(committed)

        route_backs: List[RouteBack] = []
        if verdict_status == "fail":
            last_issues = self._detect_all(working)
            for issue in last_issues:
                if issue.severity.value == "error":
                    route_backs.append(RouteBack(
                        entity_id=issue.entity_id,
                        entity_type=issue.entity_type,
                        target_phase=issue.phase_origin,
                        action="re_review",
                        reason=issue.description,
                        priority="high",
                    ))

        verdict = QualityVerdict(
            status=verdict_status,
            entities_checked=total_entities,
            issues_found=final_issues,
            issues_auto_fixed=fixed_count,
            issues_requiring_review=max(0, final_issues - fixed_count),
        )

        report = GateReport(
            canton=input.canton,
            province=input.province,
            export_run_id=input.export_run_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            verdict=verdict,
            issues=[iss for iss, _, _ in committed],
            fixes_attempted=[fix for _, fix, _ in committed],
            route_backs=route_backs,
            pass_to_phase5=(verdict_status in ("pass", "pass_with_fixes")),
            strategy_name=self.name,
            decisions=all_decisions,
        )

        # Attach diagnostics
        report.__dict__["convergence_trace"] = _convergence_summary(passes)
        report.__dict__["total_mutations"] = working.total_mutations
        report.__dict__["layer_diagnostics"] = {
            "l1_rejections": l1_rejections,
            "l2_rejections": l2_rejections,
            "l3_converged": _is_converged(passes),
            "l3_diverged": _is_diverged(passes),
            "total_layer_decisions": len(layer_decisions),
            "per_layer_breakdown": _layer_breakdown(layer_decisions),
        }
        if fail_reason:
            report.__dict__["fail_reason"] = fail_reason

        return report


def _layer_breakdown(decisions: List[LayeredDecision]) -> dict:
    """Count decisions by result type for post-mortem analysis."""
    counts: Dict[str, int] = {}
    for d in decisions:
        counts[d.result.value] = counts.get(d.result.value, 0) + 1
    return counts
