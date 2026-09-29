"""Three-layer decision pipeline for S4 Hybrid strategy.

Layer 1: Confidence threshold (cheapest — integer comparison)
Layer 2: Shadow cross-validator (medium — local structure checks)
Layer 3: Convergence (expensive — full gate re-run, canton-level only)

Layers are ordered cheapest-first to short-circuit early.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

from ...shared.models import (
    EntityIssue,
    FixAttempt,
    Route,
    RouteSemantics,
    ScheduleProfile,
    Shape,
    Stop,
)
from .cross_validators import CROSS_VALIDATORS
from .thresholds import threshold_for


class LayerResult(str, Enum):
    PASSED = "passed"
    REJECTED_BY_THRESHOLD = "rejected_threshold"
    REJECTED_BY_CROSS_VALIDATOR = "rejected_cross_validator"
    REJECTED_BY_CONVERGENCE = "rejected_convergence"


@dataclass
class LayeredDecision:
    """Full diagnostic record for a single fix's journey through the 3 layers."""
    rule_name: str
    entity_id: str
    result: LayerResult
    threshold_confidence: float
    threshold_required: float
    cross_validator_passed: Optional[bool] = None
    cross_validator_reason: Optional[str] = None
    convergence_note: Optional[str] = None


def layer1_threshold(
    issue: EntityIssue, fix: FixAttempt,
) -> Tuple[bool, LayeredDecision]:
    """L1: reject if confidence < per-rule threshold."""
    required = threshold_for(issue.rule_name)
    passed = fix.confidence >= required
    result = LayerResult.PASSED if passed else LayerResult.REJECTED_BY_THRESHOLD
    return passed, LayeredDecision(
        rule_name=issue.rule_name,
        entity_id=issue.entity_id,
        result=result,
        threshold_confidence=fix.confidence,
        threshold_required=required,
    )


def _make_shadow(entity, fix: FixAttempt):
    """Create a shadow (post-fix copy) of an entity without importing Shadow class."""
    mutated = deepcopy(entity)
    val = fix.new_value
    if val is None:
        return mutated

    if isinstance(mutated, Stop):
        if isinstance(val, dict) and "__merge_remove__" not in val:
            for k, v in val.items():
                if hasattr(mutated, k):
                    setattr(mutated, k, v)
        elif isinstance(val, str):
            mutated.name = val
        elif isinstance(val, tuple) and len(val) == 2:
            mutated.lat, mutated.lon = val
    elif isinstance(mutated, Route):
        if isinstance(val, dict):
            mutated.extra = {**mutated.extra, **val}
    elif isinstance(mutated, RouteSemantics):
        if isinstance(val, dict):
            for k, v in val.items():
                if hasattr(mutated, k):
                    setattr(mutated, k, v)
        elif isinstance(val, str):
            mutated.route_long_name = val
    elif isinstance(mutated, ScheduleProfile):
        if isinstance(val, dict) and "monday" in val:
            mutated.service_days = val
        elif isinstance(val, (int, float)):
            if mutated.peak_runtime_min is not None:
                mutated.peak_runtime_min = float(val)
            if mutated.offpeak_runtime_min is not None:
                mutated.offpeak_runtime_min = float(val)
    return mutated


def layer2_cross_validator(
    issue: EntityIssue, fix: FixAttempt, entity,
) -> Tuple[bool, LayeredDecision]:
    """L2: run the cross-validator for this rule against a shadow entity."""
    required = threshold_for(issue.rule_name)
    shadow = _make_shadow(entity, fix)

    validator = CROSS_VALIDATORS.get(issue.rule_name)
    if validator is None:
        # No cross-validator registered = pass by default (only L1 gates it)
        return True, LayeredDecision(
            rule_name=issue.rule_name,
            entity_id=issue.entity_id,
            result=LayerResult.PASSED,
            threshold_confidence=fix.confidence,
            threshold_required=required,
            cross_validator_passed=True,
            cross_validator_reason="no cross-validator registered — pass by default",
        )

    xv_passed, xv_reason = validator(issue, fix, shadow)
    result = LayerResult.PASSED if xv_passed else LayerResult.REJECTED_BY_CROSS_VALIDATOR
    return xv_passed, LayeredDecision(
        rule_name=issue.rule_name,
        entity_id=issue.entity_id,
        result=result,
        threshold_confidence=fix.confidence,
        threshold_required=required,
        cross_validator_passed=xv_passed,
        cross_validator_reason=xv_reason,
    )
