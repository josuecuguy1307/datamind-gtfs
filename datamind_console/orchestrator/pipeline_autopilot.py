from __future__ import annotations

import json
import os
import hashlib
import hmac
import inspect
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional, Sequence
from uuid import uuid4

from datamind_console.ai_insights.scoring import (
    compute_completion_quality_score,
    compute_extractor_efficiency_health_score,
    compute_order_completion_quality_score,
    pick_quality_score_01,
    pick_sequence_quality_score_100,
    validate_warnings,
)
from datamind_console.api_chatgpt.services.snapshot_builder import (
    build_ai_bot_snapshot,
    build_interpreter_snapshot,
)
from datamind_console.common.geography_input_resolver import SharedGeographyResolver
from datamind_console.orchestrator.role_policy import (
    ACTION_DESTRUCTIVE_CLEANUP_EXECUTE,
    ACTION_DISPATCH_PATCH_TASK,
    ACTION_MERGE_BIND,
    ACTION_SEQUENCE_REORDER_APPLY,
    ROLE_OPERATOR,
    ROLE_RUNTIME,
    check_permission,
)
from datamind_console.orchestrator.autopilot_db_store import AutopilotDBStore
from datamind_console.orchestrator.autopilot_feature_flags import AutopilotFeatureFlags
from datamind_console.orchestrator.policy_engine import _normalize_profile as _normalize_profile_canonical

try:
    from local_runner.scripts.runner_core import RunnerError, load_runner_config, run_prompt_file
except Exception:  # pragma: no cover
    RunnerError = RuntimeError  # type: ignore[assignment]
    load_runner_config = None  # type: ignore[assignment]
    run_prompt_file = None  # type: ignore[assignment]


DEFAULT_AUTOPILOT_ADVISORY_MODEL = "gpt-5.2"
DEFAULT_AUTOPILOT_CODEX_MODEL = "gpt-5.2-codex"
DEFAULT_AUTOPILOT_RUNNER_CONFIG = (
    Path(__file__).resolve().parents[2] / "local_runner" / "config" / "runner_config.yaml"
)
EXTRACTOR_PATCH_ROUTING_THRESHOLDS = {
    "completion_quality_score": 0.50,
    "efficiency_health_score": 0.60,
    "strong_completion_quality_score": 0.35,
    "strong_efficiency_health_score": 0.45,
    "repeated_empty_attempts": 2,
}
P3_STRONG_SELECTION_CONFIDENCE_MIN = 0.35
P3_PARTIAL_FETCH_CLASSIFICATIONS = {
    "fetch_partial_after_valid_selection",
    "selected_relation_not_fully_enriched",
    "selected_relation_fetch_inconsistent",
    "downstream_fetch_observability_gap",
}

GEOGRAPHY_QUALITY_THRESHOLDS = {
    "interpretation_confidence_weak": 0.50,
    "completion_quality_low": 0.40,
    "efficiency_low": 0.30,
    "geography_degradation_confidence_floor": 0.55,
    "geography_degradation_quality_ceiling": 0.45,
    "overconstraint_sector_score_floor": 0.80,
    "overconstraint_ai_confidence_floor": 0.60,
}
EXTRACTOR_WEAKNESS_REASON_CODES = {
    "repeated_empty_extraction",
    "retry_not_diversified",
    "same_config_retry_loop",
    "fallback_rescue_failed",
    "low_completion_quality",
    "low_efficiency_health",
    "partial_evidence_extractor_struggle",
    "low_order_completion_quality",
    "spatial_interpretation_failed",
    "spatial_plan_reused_without_change",
    "target_intent_ignored",
    "candidate_universe_too_small",
    "selection_confidence_low",
    "hard_filter_overreach",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _to_jsonable_text(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _extract_model_name_from_command(command: Sequence[Any]) -> Optional[str]:
    items = [str(x) for x in list(command or []) if str(x).strip()]
    for idx, token in enumerate(items):
        if token == "--model" and idx + 1 < len(items):
            value = str(items[idx + 1]).strip()
            return value or None
        if token.startswith("--model="):
            value = token.split("=", 1)[1].strip()
            return value or None
        if token == "-m" and idx + 1 < len(items):
            value = str(items[idx + 1]).strip()
            return value or None
    return None


def _dispatch_provider_name_for_target(target: str) -> str:
    norm = str(target or "").strip().lower()
    if norm == "claude":
        return "claude_cli"
    return "codex_cli"


class AutomationLevel(str, Enum):
    AUTO_SAFE = "AUTO_SAFE"
    AUTO_WITH_CHECKPOINT = "AUTO_WITH_CHECKPOINT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    MANUAL_ASSISTED = "MANUAL_ASSISTED"


class PolicyProfile(str, Enum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE_SUPERVISED = "aggressive_supervised"


class BlockReasonCode(str, Enum):
    EXTRACTION_EMPTY = "EXTRACTION_EMPTY"
    EXTRACTION_LOW_COVERAGE = "EXTRACTION_LOW_COVERAGE"
    REPEATED_EXTRACTOR_FAILURE = "REPEATED_EXTRACTOR_FAILURE"
    NORMALIZE_FAILED = "NORMALIZE_FAILED"
    FEATURES_INVALID = "FEATURES_INVALID"
    CLUSTERING_DEGENERATE = "CLUSTERING_DEGENERATE"
    RESOLVE_ZERO_RESULTS = "RESOLVE_ZERO_RESULTS"
    NO_APPROVED_NODES_FOR_PROMOTE = "NO_APPROVED_NODES_FOR_PROMOTE"
    PROMOTE_NODE_SET_MISSING = "PROMOTE_NODE_SET_MISSING"
    PROMOTE_LOOKUP_EMPTY = "PROMOTE_LOOKUP_EMPTY"
    PHASE1_PROMOTE_NOT_COMPLETED = "PHASE1_PROMOTE_NOT_COMPLETED"
    PLACE_SET_ID_NOT_FOUND = "PLACE_SET_ID_NOT_FOUND"
    PLACE_SET_EMPTY_OR_MISSING = "PLACE_SET_EMPTY_OR_MISSING"
    SEMANTIC_PIPELINE_FAILED = "SEMANTIC_PIPELINE_FAILED"
    SEMANTIC_REGRESSION_HIGH = "SEMANTIC_REGRESSION_HIGH"
    STEP20_UNMATCHED_BLOCKING = "STEP20_UNMATCHED_BLOCKING"
    STEP20_AMBIGUOUS_BLOCKING = "STEP20_AMBIGUOUS_BLOCKING"
    STEP20_SEQUENCE_QUALITY_LOW = "STEP20_SEQUENCE_QUALITY_LOW"
    STEP20_COVERAGE_GAP_BLOCKING = "STEP20_COVERAGE_GAP_BLOCKING"
    STEP20_SYNTHETIC_TERMINUS_ONLY = "STEP20_SYNTHETIC_TERMINUS_ONLY"
    PHASE3_INVERSE_COMPLETION_BLOCKING = "PHASE3_INVERSE_COMPLETION_BLOCKING"
    STEP20_REORDER_SUGGESTED_BLOCKING = "STEP20_REORDER_SUGGESTED_BLOCKING"
    STEP30_SEQUENCE_RESOLUTION_BLOCKING = "STEP30_SEQUENCE_RESOLUTION_BLOCKING"
    GEOMETRY_FAILED = "GEOMETRY_FAILED"
    GEOMETRY_QUALITY_CRITICAL_LOW = "GEOMETRY_QUALITY_CRITICAL_LOW"
    RANKING_FAILED = "RANKING_FAILED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DESTRUCTIVE_CLEANUP_APPROVAL_REQUIRED = "DESTRUCTIVE_CLEANUP_APPROVAL_REQUIRED"
    MERGE_BIND_APPROVAL_REQUIRED = "MERGE_BIND_APPROVAL_REQUIRED"
    GATE_BYPASS_ATTEMPT = "GATE_BYPASS_ATTEMPT"
    VALIDATOR_PAYLOAD_CONTRACT_MISMATCH = "VALIDATOR_PAYLOAD_CONTRACT_MISMATCH"


class ApprovalType(str, Enum):
    RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS = "RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS"
    APPLY_REORDER_PROPOSAL = "APPLY_REORDER_PROPOSAL"
    RUN_DESTRUCTIVE_CLEANUP = "RUN_DESTRUCTIVE_CLEANUP"
    APPROVE_FINAL_ROUTE_OR_MERGE_BIND = "APPROVE_FINAL_ROUTE_OR_MERGE_BIND"
    PROMOTE_NODE_BATCH = "PROMOTE_NODE_BATCH"
    DISPATCH_PATCH_TASK = "DISPATCH_PATCH_TASK"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class RunStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class PatchRegistryStatus(str, Enum):
    GENERATED = "generated"
    PENDING_APPROVAL = "pending_approval"
    DISPATCHED = "dispatched"
    FAILED = "failed"
    RETEST_PENDING = "retest_pending"
    COMPARED = "compared"
    ACCEPTED = "accepted"
    ROLLED_BACK = "rolled_back"
    OBSERVE_MORE = "observe_more"


@dataclass(frozen=True)
class RetryPolicy:
    enabled: bool
    max_attempts: int
    strategies: Sequence[str] = field(default_factory=tuple)
    retry_on_codes: Sequence[BlockReasonCode] = field(default_factory=tuple)


@dataclass(frozen=True)
class ResumeBehavior:
    mode: str = "continue"
    rerun_step_id: Optional[str] = None


@dataclass(frozen=True)
class DiversionRule:
    block_reason_code: BlockReasonCode
    target_step_id: str
    approval_type: ApprovalType
    resume_step_id: str
    optional_phase2_partial_rerun: bool = False


@dataclass(frozen=True)
class StepDefinition:
    phase: str
    step_id: str
    name: str
    automation_level: AutomationLevel
    executor: str
    validator: str
    ai_bot_hooks: Sequence[str]
    chatgpt_interpretation_triggers: Sequence[str]
    retry_policy: RetryPolicy
    pause_conditions: Sequence[str]
    resume_behavior: ResumeBehavior
    artifacts_expected: Sequence[str]
    approval_type: Optional[ApprovalType]
    next_step_on_success: Optional[str]
    diversion_rules: Dict[BlockReasonCode, DiversionRule]
    approval_apply_executor: Optional[str] = None
    approval_action: Optional[str] = None


@dataclass(frozen=True)
class PolicySettings:
    profile: PolicyProfile
    auto_advance_passable_warnings: bool
    warning_requires_operator_review: bool
    non_critical_degradation_tolerance: float
    max_retry_bonus: int


@dataclass
class ExecutorResult:
    ok: bool = True
    summary: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ValidatorResult:
    status: str = "pass"
    gate_passed: bool = True
    passable_warning: bool = False
    warnings: List[str] = field(default_factory=list)
    anomalies: List[str] = field(default_factory=list)
    block_reason_code: Optional[BlockReasonCode] = None
    summary: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    recommended_action: Optional[str] = None
    gate_bypass_attempted: bool = False


@dataclass
class AIBotSnapshot:
    scores: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    anomaly_flags: List[str] = field(default_factory=list)
    proposals: Dict[str, Any] = field(default_factory=dict)
    extractor_status: Dict[str, Any] = field(default_factory=dict)
    extractor_help_needed: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatGPTSnapshot:
    summary: str
    priorities: List[str] = field(default_factory=list)
    risk_notes: List[str] = field(default_factory=list)
    status: str = "ok"
    task: Optional[str] = None
    trigger: Optional[str] = None
    model: Optional[str] = None
    latency_ms: Optional[int] = None
    token_usage: Dict[str, Any] = field(default_factory=dict)
    snapshot_id: Optional[str] = None
    source: Optional[str] = None
    schema_name: Optional[str] = None
    requested_mode: Optional[str] = None
    fallback_used: Optional[bool] = None
    error_code: Optional[str] = None
    error_summary: Optional[str] = None
    prompt_package: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BlockReason:
    code: BlockReasonCode
    severity: str
    summary: str
    validator_evidence: Dict[str, Any]
    ai_bot_metrics_snapshot: Dict[str, Any]
    chatgpt_interpretation: Optional[Dict[str, Any]]
    recommended_next_action: str
    required_approval_type: Optional[ApprovalType]
    diversion_target: Optional[str]


@dataclass
class StepExecutionRecord:
    run_id: str
    phase: str
    step_id: str
    attempt_no: int
    status: str
    executor_result_summary: Dict[str, Any]
    validator_result: Dict[str, Any]
    ai_bot_snapshot: Dict[str, Any]
    chatgpt_snapshot: Optional[Dict[str, Any]]
    block_reason: Optional[Dict[str, Any]]
    artifacts: List[Dict[str, Any]]
    timings: Dict[str, Any]
    created_at: str
    idempotency_key: Optional[str] = None


@dataclass
class PipelineEvent:
    event_id: str
    event_type: str
    timestamp: str
    run_id: str
    phase: Optional[str]
    step_id: Optional[str]
    payload: Dict[str, Any]
    correlation_id: str
    trace_id: str


@dataclass
class ApprovalItem:
    approval_id: str
    run_id: str
    phase: str
    step_id: str
    approval_type: ApprovalType
    status: ApprovalStatus
    created_at: str
    created_by_system: bool
    evidence_payload: Dict[str, Any]
    risk_summary: str
    recommended_action: str
    operator_decision: Optional[str] = None
    operator_id: Optional[str] = None
    operator_role: Optional[str] = None
    decision_at: Optional[str] = None
    decision_signature: Optional[str] = None


@dataclass
class RunSessionState:
    run_id: str
    pipeline_scope: Dict[str, Any]
    policy_profile: str
    status: str
    current_phase: Optional[str]
    current_step_id: Optional[str]
    attempt_counters: Dict[str, int] = field(default_factory=dict)
    diversion_stack: List[Dict[str, Any]] = field(default_factory=list)
    resume_context: Dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    completed_at: Optional[str] = None
    operator_context: Dict[str, Any] = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    step_execution_records: List[StepExecutionRecord] = field(default_factory=list)
    events: List[PipelineEvent] = field(default_factory=list)
    approvals: List[ApprovalItem] = field(default_factory=list)
    artifacts: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    patch_registry: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class RetryDecision:
    strategy: str
    parameter_delta: Dict[str, Any]
    reason: str


@dataclass(frozen=True)
class PatchTaskIntent:
    should_create: bool
    branch: Optional[str] = None
    patch_type: Optional[str] = None
    suggested_target: Optional[str] = None
    justification: Optional[str] = None


POLICY_SETTINGS: Dict[str, PolicySettings] = {
    PolicyProfile.CONSERVATIVE.value: PolicySettings(
        profile=PolicyProfile.CONSERVATIVE,
        auto_advance_passable_warnings=False,
        warning_requires_operator_review=True,
        non_critical_degradation_tolerance=0.10,
        max_retry_bonus=0,
    ),
    PolicyProfile.BALANCED.value: PolicySettings(
        profile=PolicyProfile.BALANCED,
        auto_advance_passable_warnings=True,
        warning_requires_operator_review=False,
        non_critical_degradation_tolerance=0.20,
        max_retry_bonus=1,
    ),
    PolicyProfile.AGGRESSIVE_SUPERVISED.value: PolicySettings(
        profile=PolicyProfile.AGGRESSIVE_SUPERVISED,
        auto_advance_passable_warnings=True,
        warning_requires_operator_review=False,
        non_critical_degradation_tolerance=0.30,
        max_retry_bonus=2,
    ),
}


STEP_P1_1_EXTRACT = "P1.1_EXTRACT_BUILD_NODE_SET"
STEP_P1_2_CHAIN = "P1.2_NORMALIZE_FEATURES_CLUSTER_RESOLVE"
STEP_P1_3A_WORKSPACE = "P1.3A_WORKSPACE_REVIEW"
STEP_P1_3B_NEW_NODES = "P1.3B_NEW_NODES_FROM_PHASE3"
STEP_P1_4_PROMOTE = "P1.4_APPROVE_PROMOTE"
STEP_P2_1_SEMANTIC = "P2.1_SEMANTIC_PIPELINE_RUN"
STEP_P2_2_WORKSPACE = "P2.2_SEMANTICS_WORKSPACE_REVIEW"
STEP_P2_3_CLEANUP = "P2.3_CLEANUP_DEDUP_GLOBAL_NORMALIZE"
STEP_P3_1_EXTRACT = "P3.1_ROUTE_EXTRACTION_CONTEXT"
STEP_P3_15_INVERSE = "P3.15_INVERSE_COMPLETION"
STEP_P3_2_STEP20 = "P3.2_SEQUENCE_STEP20"
STEP_P3_3_REORDER = "P3.3_REORDER_PROPOSAL"
STEP_P3_4_STEP30 = "P3.4_STEP30_GEOMETRY"
STEP_P3_4_STEP32 = "P3.4_STEP32_STOP_RECOVERY"
STEP_P3_4_STEP35 = "P3.4_STEP35_RANK"
STEP_P3_4_STEP40 = "P3.4_STEP40_APPROVE"
STEP_P3_5_MERGE = "P3.5_MERGE_OPPOSITE_DIRECTION"
STEP_P3_6_CATALOG = "P3.6_CATALOG_SYNC_REVIEW"
STEP_P3_7_SECTOR = "P3.7_SECTOR_COVERAGE_REVIEW"
STEP_P3_8_GAPS = "P3.8_GAP_DETECTION_CLASSIFICATION"
STEP_P3_9_EXPORT = "P3.9_MISSING_ROUTE_EXPORT"
STEP_P3_10_RESOLUTION = "P3.10_GAP_RESOLUTION_QUEUE"
STEP_P3_11_VERIFY = "P3.11_GAP_RESOLUTION_VERIFY"

# Canonical Phase 3 route-processing order:
# extract/discovery -> inverse completion -> Step 20 -> downstream review/approval/catalog work.
# The legacy late-merge stage remains available only for non-default compatibility paths.
PHASE3_CANONICAL_ROUTE_STAGE_ORDER: Sequence[str] = (
    STEP_P3_1_EXTRACT,
    STEP_P3_15_INVERSE,
    STEP_P3_2_STEP20,
    STEP_P3_3_REORDER,
    STEP_P3_4_STEP30,
    STEP_P3_4_STEP32,
    STEP_P3_4_STEP35,
    STEP_P3_4_STEP40,
    STEP_P3_6_CATALOG,
)
PHASE3_LEGACY_NON_DEFAULT_STAGES: Sequence[str] = (STEP_P3_5_MERGE,)

PHASE1_TO_PHASE2_HANDOFF_ARTIFACT = "phase1_to_phase2_handoff"

STEP_TELEMETRY_STAGE_ALIASES: Dict[str, Sequence[str]] = {
    STEP_P1_1_EXTRACT: ("phase1_extract", "step_01_extract"),
    STEP_P1_2_CHAIN: ("phase1_chain", "step_02_chain"),
    STEP_P1_3A_WORKSPACE: ("phase1_workspace_review",),
    STEP_P1_3B_NEW_NODES: ("phase1_new_nodes_prefill",),
    STEP_P1_4_PROMOTE: ("phase1_promote_prepare", "phase1_promote_apply"),
    STEP_P2_1_SEMANTIC: ("phase2_semantic_pipeline",),
    STEP_P2_2_WORKSPACE: ("phase2_semantic_workspace_review",),
    STEP_P2_3_CLEANUP: ("phase2_cleanup_preview", "phase2_cleanup_apply"),
    STEP_P3_1_EXTRACT: ("step_10_fetch", "step_05_discover", "phase3_route_extract"),
    STEP_P3_15_INVERSE: ("phase3_inverse_completion",),
    STEP_P3_2_STEP20: ("step_20_sequences", "phase3_step20_sequence"),
    STEP_P3_3_REORDER: ("phase3_reorder_proposal",),
    STEP_P3_4_STEP30: ("step_30_geometry", "phase3_step30_geometry"),
    STEP_P3_4_STEP32: ("step_32_stop_recovery", "phase3_step32_stop_recovery"),
    STEP_P3_4_STEP35: ("step_35_rank", "phase3_step35_rank"),
    STEP_P3_4_STEP40: ("step_40_approve", "phase3_step40_approve_prepare"),
    STEP_P3_5_MERGE: ("merge_proposal_rank", "merge_proposal_eval", "phase3_merge_proposal"),
    STEP_P3_6_CATALOG: ("phase3_catalog_sync_review",),
    STEP_P3_7_SECTOR: ("phase3_sector_coverage_review",),
    STEP_P3_8_GAPS: ("phase3_gap_detection_classification",),
    STEP_P3_9_EXPORT: ("phase3_missing_route_export",),
    STEP_P3_10_RESOLUTION: ("phase3_gap_resolution_queue",),
    STEP_P3_11_VERIFY: ("phase3_gap_resolution_verify",),
}

CONFIDENCE_BAND_TO_SCORE: Dict[str, float] = {
    "high": 0.90,
    "medium": 0.65,
    "low": 0.35,
}


def _handoff_stage_aliases(step_id: str) -> List[str]:
    sid = str(step_id or "").strip()
    out: List[str] = []
    seen: set[str] = set()
    for value in [sid, *list(STEP_TELEMETRY_STAGE_ALIASES.get(sid, ()))]:
        txt = str(value or "").strip()
        if not txt or txt in seen:
            continue
        seen.add(txt)
        out.append(txt)
    return out


def _phase3_canonical_next_step(
    step_id: str,
    *,
    step20_summary: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Return the canonical Phase 3 next step for route processing.

    This helper hardens the autopilot against stale registry wiring that would
    otherwise reintroduce legacy `extract -> Step20` or late-merge-default paths.
    """

    sid = str(step_id or "").strip()
    if sid == STEP_P3_1_EXTRACT:
        return STEP_P3_15_INVERSE
    if sid == STEP_P3_15_INVERSE:
        return STEP_P3_2_STEP20
    if sid == STEP_P3_2_STEP20:
        reorder = dict(step20_summary or {})
        reorder = reorder.get("reorder_proposal") if isinstance(reorder, dict) else None
        if isinstance(reorder, dict):
            if bool(reorder.get("requires_approval") or reorder.get("apply_recommended") or reorder.get("needs_reorder")):
                return STEP_P3_3_REORDER
        elif bool(reorder):
            return STEP_P3_3_REORDER
        return STEP_P3_4_STEP30
    if sid == STEP_P3_4_STEP30:
        return STEP_P3_4_STEP32
    if sid == STEP_P3_4_STEP32:
        return STEP_P3_4_STEP35
    if sid == STEP_P3_4_STEP40:
        return STEP_P3_6_CATALOG
    return None


def _handoff_to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _handoff_to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def _handoff_to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def _handoff_confidence_to_numeric(value: Any) -> Optional[float]:
    parsed = _handoff_to_float(value)
    if parsed is not None:
        return max(0.0, min(1.0, float(parsed)))
    band = str(value or "").strip().lower()
    if band in CONFIDENCE_BAND_TO_SCORE:
        return float(CONFIDENCE_BAND_TO_SCORE[band])
    return None


def _handoff_normalize_score_01(raw_score: Any) -> Optional[float]:
    parsed = _handoff_to_float(raw_score)
    if parsed is None:
        return None
    val = float(parsed)
    if val > 1.0:
        val = val / 100.0
    return max(0.0, min(1.0, val))


def _classify_geography_quality_attribution(
    *,
    interpretation_confidence: Optional[float],
    fallback_used: Optional[bool],
    fallback_reason: Any,
    bbox_validation_status: Any,
    completion_quality_score: Optional[float],
    efficiency_score: Optional[float],
    route_hints_present: Optional[bool] = None,
    route_hints_influenced_bbox: Optional[bool] = None,
    route_hints_overconstrained_geography: Optional[bool] = None,
) -> Dict[str, Any]:
    thresholds = dict(GEOGRAPHY_QUALITY_THRESHOLDS)
    conf = float(interpretation_confidence or 0.0) if interpretation_confidence is not None else None
    fb = bool(fallback_used) if fallback_used is not None else False
    fb_reason = str(fallback_reason or "").strip() or None
    bbox_status = str(bbox_validation_status or "").strip() or None
    cqs = float(completion_quality_score) if completion_quality_score is not None else None
    eff = float(efficiency_score) if efficiency_score is not None else None
    rh_present = bool(route_hints_present) if route_hints_present is not None else False
    rh_influenced = bool(route_hints_influenced_bbox) if route_hints_influenced_bbox is not None else False
    rh_overconstrained = bool(route_hints_overconstrained_geography) if route_hints_overconstrained_geography is not None else False

    geo_weak = bool(
        fb
        or bbox_status in {"invalid", "default_applied", "missing"}
        or (conf is not None and conf < float(thresholds["interpretation_confidence_weak"]))
    )
    quality_low = bool(
        (cqs is not None and cqs < float(thresholds["completion_quality_low"]))
        or (eff is not None and eff < float(thresholds["efficiency_low"]))
    )

    # Geography degradation: geography is not hard-broken but confidence is marginal
    # and extraction quality is poor — a softer signal than geo_weak + quality_low
    geography_degradation = bool(
        not geo_weak
        and quality_low
        and conf is not None
        and conf < float(thresholds["geography_degradation_confidence_floor"])
        and (cqs is not None and cqs < float(thresholds["geography_degradation_quality_ceiling"]))
    )

    # Route hint overreach: hints influenced bbox and quality collapsed
    route_hint_overreach = bool(
        rh_influenced
        and quality_low
        and (rh_overconstrained or (conf is not None and conf < float(thresholds["interpretation_confidence_weak"])))
    )

    if geo_weak and quality_low:
        likely_cause = "geography_interpretation"
        patch_target_hint = "patch_geography_interpretation"
    elif route_hint_overreach:
        # More specific than geography_degradation — route hints actively distorted geography
        likely_cause = "route_hint_overreach"
        patch_target_hint = "patch_geography_interpretation"
    elif geography_degradation:
        # Geography not hard-broken but marginal confidence correlates with poor quality
        likely_cause = "geography_degradation"
        patch_target_hint = "patch_geography_interpretation"
    elif not geo_weak and quality_low:
        likely_cause = "extractor_logic"
        patch_target_hint = "patch_extractor"
    elif geo_weak and not quality_low:
        likely_cause = "geography_marginal_but_quality_ok"
        patch_target_hint = None
    else:
        likely_cause = "none"
        patch_target_hint = None

    return {
        "geography_weak": geo_weak,
        "quality_low": quality_low,
        "geography_degradation": geography_degradation,
        "route_hint_overreach": route_hint_overreach,
        "likely_cause": likely_cause,
        "patch_target_hint": patch_target_hint,
        "interpretation_confidence": conf,
        "fallback_used": fb,
        "fallback_reason": fb_reason,
        "bbox_validation_status": bbox_status,
        "completion_quality_score": cqs,
        "efficiency_score": eff,
        "route_hints_present": rh_present,
        "route_hints_influenced_bbox": rh_influenced,
        "route_hints_overconstrained_geography": rh_overconstrained,
    }


def _handoff_extract_reorder_signal(
    *,
    exec_summary: Dict[str, Any],
    validator_evidence: Optional[Dict[str, Any]] = None,
    ai_proposals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    summary = dict(exec_summary or {})
    evidence = dict(validator_evidence or {})
    proposals = dict(ai_proposals or {})
    step20_diag = dict(summary.get("step20_diagnostics_payload") or {})
    proposal_obj = dict(summary.get("reorder_proposal") or {})
    ai_reorder_obj = dict(proposals.get("reorder") or {})
    legacy_reorder_raw = proposals.get("reorder_proposal")
    legacy_reorder_obj = dict(legacy_reorder_raw or {}) if isinstance(legacy_reorder_raw, dict) else {}
    legacy_reorder_bool = _handoff_to_bool(legacy_reorder_raw)

    recommended_candidates: List[Optional[bool]] = [
        _handoff_to_bool(step20_diag.get("reorder_recommended")),
        _handoff_to_bool(summary.get("reorder_recommended")),
        _handoff_to_bool(evidence.get("reorder_recommended")),
        _handoff_to_bool(ai_reorder_obj.get("recommended")),
        _handoff_to_bool(proposal_obj.get("recommended")),
        _handoff_to_bool(proposal_obj.get("needs_reorder")),
        _handoff_to_bool(proposal_obj.get("apply_recommended")),
        _handoff_to_bool(legacy_reorder_obj.get("recommended")),
        _handoff_to_bool(legacy_reorder_obj.get("needs_reorder")),
        _handoff_to_bool(legacy_reorder_obj.get("apply_recommended")),
        legacy_reorder_bool,
    ]
    recommended: Optional[bool] = None
    for cand in recommended_candidates:
        if cand is None:
            continue
        recommended = bool(cand)
        break

    confidence_candidates = [
        ai_reorder_obj.get("confidence"),
        step20_diag.get("reorder_confidence"),
        summary.get("reorder_confidence"),
        evidence.get("reorder_confidence"),
        proposal_obj.get("confidence"),
        legacy_reorder_obj.get("confidence"),
    ]
    confidence: Optional[float] = None
    for raw in confidence_candidates:
        conf = _handoff_confidence_to_numeric(raw)
        if conf is None:
            continue
        confidence = max(0.0, min(1.0, float(conf)))
        break

    return {
        "recommended": recommended,
        "confidence": confidence,
        "sources": {
            "summary_has_reorder_recommended": (summary.get("reorder_recommended") is not None),
            "summary_has_reorder_proposal": bool(proposal_obj),
            "validator_has_reorder_recommended": (evidence.get("reorder_recommended") is not None),
            "ai_has_reorder_object": bool(ai_reorder_obj),
            "ai_has_legacy_reorder_proposal": (proposals.get("reorder_proposal") is not None),
        },
    }


def _handoff_extract_regression_flags(compare: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for row in list((compare or {}).get("regression_flags") or []):
        if isinstance(row, dict):
            code = str(row.get("code") or "").strip()
            if code:
                out.append(code)
        else:
            txt = str(row or "").strip()
            if txt:
                out.append(txt)
    return list(dict.fromkeys(out))


def _handoff_build_consistency_candidates(
    *,
    step_id: str,
    exec_summary: Dict[str, Any],
    validator_result: Dict[str, Any],
    ai_snapshot: Dict[str, Any],
) -> List[Dict[str, Any]]:
    summary = dict(exec_summary or {})
    validator = dict(validator_result or {})
    evidence = dict(validator.get("evidence") or {})
    ai = dict(ai_snapshot or {})
    compare = dict((dict(ai.get("proposals") or {}).get("comparison") or {}))
    candidates: List[Dict[str, Any]] = []

    resolve = dict(summary.get("resolve") or {})
    resolve_exec = None
    for key in ("resolved_count", "resolved_total", "n_resolved"):
        parsed = _handoff_to_int(resolve.get(key))
        if parsed is not None:
            resolve_exec = parsed
            break

    resolve_val = None
    for key in ("resolved_count", "resolved_total", "n_resolved"):
        parsed = _handoff_to_int(evidence.get(key))
        if parsed is not None:
            resolve_val = parsed
            break

    if resolve_exec is not None and resolve_exec > 0 and resolve_val is not None and resolve_val <= 0:
        candidates.append(
            {
                "code": "RESOLVE_COUNT_CONTRACT_MISMATCH",
                "lhs_path": "executor_summary.resolve.resolved_total",
                "rhs_path": "validator.evidence.resolved_count",
                "lhs_value": int(resolve_exec),
                "rhs_value": int(resolve_val),
                "note": "Executor indicates successful resolve output while validator evidence reports zero.",
            }
        )

    block_code = str(validator.get("block_reason_code") or "").strip()
    if block_code == BlockReasonCode.RESOLVE_ZERO_RESULTS.value and resolve_exec is not None and resolve_exec > 0:
        latest = dict(compare.get("latest") or {})
        candidates.append(
            {
                "code": "BLOCK_REASON_EVIDENCE_MISMATCH",
                "lhs_path": "validator.block_reason_code",
                "rhs_path": "executor_summary.resolve.resolved_total",
                "lhs_value": block_code,
                "rhs_value": int(resolve_exec),
                "note": f"Latest comparison resolved_count={latest.get('resolved_count')!r}",
            }
        )

    unmatched = _handoff_to_int(evidence.get("unmatched_count"))
    ambiguous = _handoff_to_int(evidence.get("ambiguous_count"))
    if block_code == BlockReasonCode.STEP20_UNMATCHED_BLOCKING.value and unmatched is not None and unmatched <= 0:
        candidates.append(
            {
                "code": "STEP20_BLOCK_UNMATCHED_MISMATCH",
                "lhs_path": "validator.block_reason_code",
                "rhs_path": "validator.evidence.unmatched_count",
                "lhs_value": block_code,
                "rhs_value": int(unmatched),
                "note": "Step20 unmatched blocker does not align with unmatched_count evidence.",
            }
        )
    if block_code == BlockReasonCode.STEP20_AMBIGUOUS_BLOCKING.value and ambiguous is not None and ambiguous <= 0:
        candidates.append(
            {
                "code": "STEP20_BLOCK_AMBIGUOUS_MISMATCH",
                "lhs_path": "validator.block_reason_code",
                "rhs_path": "validator.evidence.ambiguous_count",
                "lhs_value": block_code,
                "rhs_value": int(ambiguous),
                "note": "Step20 ambiguous blocker does not align with ambiguous_count evidence.",
            }
        )

    if str(step_id or "").strip() == STEP_P1_2_CHAIN:
        payload_count = _handoff_to_int((dict(summary.get("validator_payload") or {})).get("resolved_count"))
        if resolve_exec is not None and resolve_exec > 0 and payload_count is not None and payload_count <= 0:
            candidates.append(
                {
                    "code": "VALIDATOR_PAYLOAD_STALE_OR_DEFAULTED",
                    "lhs_path": "executor_summary.resolve.resolved_total",
                    "rhs_path": "executor_summary.validator_payload.resolved_count",
                    "lhs_value": int(resolve_exec),
                    "rhs_value": int(payload_count),
                    "note": "Validator payload may be stale/defaulted even though resolve output succeeded.",
                }
            )

    if str(step_id or "").strip() == STEP_P1_4_PROMOTE:
        promote = dict(summary.get("promote_dry_run") or {})
        payload = dict(summary.get("validator_payload") or {})
        workspace_state = (
            dict(summary.get("workspace_state_summary") or {})
            or dict(payload.get("workspace_state_summary") or {})
            or dict(evidence.get("workspace_state_summary") or {})
        )
        promote_resolved = _handoff_to_int(promote.get("n_resolved"))
        if promote_resolved is None:
            promote_resolved = _handoff_to_int(evidence.get("n_resolved"))
        workspace_resolved = _handoff_to_int(workspace_state.get("resolved_total"))
        compare_latest = dict(compare.get("latest") or {})
        compare_resolved = _handoff_to_int(compare_latest.get("resolved_count"))
        evidence_resolved_total = _handoff_to_int(evidence.get("resolved_total"))
        if evidence_resolved_total is None:
            evidence_resolved_total = _handoff_to_int(payload.get("resolved_total"))
        if workspace_resolved is not None and workspace_resolved > 0 and promote_resolved is not None and promote_resolved <= 0:
            candidates.append(
                {
                    "code": "P1_PROMOTE_WORKSPACE_RESOLVE_MISMATCH",
                    "lhs_path": "executor_summary.workspace_state_summary.resolved_total",
                    "rhs_path": "executor_summary.promote_dry_run.n_resolved",
                    "lhs_value": int(workspace_resolved),
                    "rhs_value": int(promote_resolved),
                    "note": "Workspace resolved_total is non-zero but promote dry-run reports zero.",
                }
            )
        if (
            not workspace_state
            and promote_resolved is not None
            and promote_resolved <= 0
            and (((compare_resolved or 0) > 0) or ((evidence_resolved_total or 0) > 0))
        ):
            candidates.append(
                {
                    "code": "P1_PROMOTE_MISSING_WORKSPACE_STATE_SUMMARY",
                    "lhs_path": "executor_summary.workspace_state_summary",
                    "rhs_path": "executor_summary.promote_dry_run.n_resolved",
                    "lhs_value": None,
                    "rhs_value": int(promote_resolved),
                    "note": "Promote dry-run returned zero while recent AI compare indicates resolved_count > 0, but workspace_state_summary is missing.",
                }
            )

    if str(step_id or "").strip() == STEP_P3_1_EXTRACT:
        payload = dict(summary.get("validator_payload") or {})
        phase3_bundle = _phase3_route_bundle_evidence(
            summary=summary,
            payload=payload,
            validator_evidence=evidence,
        )
        prior_stop_count = int(phase3_bundle.get("actual_prior_stop_count") or 0)
        prior_stop_evidence_count = int(phase3_bundle.get("prior_stop_evidence_count") or 0)
        selected_relation_stop_prior_count = int(phase3_bundle.get("selected_relation_stop_prior_count") or 0)
        top_stop_prior_count = int(phase3_bundle.get("top_stop_prior_count") or 0)
        validator_summary = str(validator.get("summary") or "").strip().lower()
        if (
            block_code == BlockReasonCode.EXTRACTION_EMPTY.value
            and bool(phase3_bundle.get("strong_bundle_evidence"))
        ):
            candidates.append(
                {
                    "code": "P3_STRONG_BUNDLE_BLOCKED_AS_EXTRACTION_EMPTY",
                    "lhs_path": "validator.block_reason_code",
                    "rhs_path": "validator.evidence.strong_bundle_evidence",
                    "lhs_value": block_code,
                    "rhs_value": True,
                    "note": "P3.1 classified strong route-bundle evidence as EXTRACTION_EMPTY.",
                    "likely_implication": "validator_mapping_issue",
                }
            )
        if prior_stop_count <= 0 and selected_relation_stop_prior_count > 0:
            candidates.append(
                {
                    "code": "P3_PRIOR_STOP_COUNT_CONTRADICTION",
                    "lhs_path": "validator.evidence.prior_stop_count",
                    "rhs_path": "validator.evidence.selected_relation_stop_prior_count",
                    "lhs_value": int(prior_stop_count),
                    "rhs_value": int(selected_relation_stop_prior_count),
                    "note": "Validator says zero loaded prior stops while selected relation evidence reports prior-stop coverage.",
                    "likely_implication": "validator_scoring_or_fetch_mapping_issue",
                }
            )
        if prior_stop_count <= 0 and top_stop_prior_count > 0:
            candidates.append(
                {
                    "code": "P3_TOP_STOP_PRIOR_CONTRADICTION",
                    "lhs_path": "validator.evidence.prior_stop_count",
                    "rhs_path": "executor_summary.candidate_universe_summary.top_stop_prior_count",
                    "lhs_value": int(prior_stop_count),
                    "rhs_value": int(top_stop_prior_count),
                    "note": "Top discovered relation has prior-stop evidence even though validator reports zero loaded prior stops.",
                    "likely_implication": "fetch_observability_gap",
                }
            )
        if bool(phase3_bundle.get("route_id_present")) and "did not produce route_id" in validator_summary:
            candidates.append(
                {
                    "code": "P3_ROUTE_ID_SUMMARY_MISMATCH",
                    "lhs_path": "validator.summary",
                    "rhs_path": "executor_summary.route_id",
                    "lhs_value": str(validator.get("summary") or ""),
                    "rhs_value": str(phase3_bundle.get("route_id") or ""),
                    "note": "Operator-visible summary says route_id is missing even though route_id is present.",
                    "likely_implication": "validator_summary_bug",
                }
            )
        if (
            bool(phase3_bundle.get("strong_bundle_evidence"))
            and str(phase3_bundle.get("fetch_status_classification") or "") in P3_PARTIAL_FETCH_CLASSIFICATIONS
        ):
            candidates.append(
                {
                    "code": "P3_SELECTED_RELATION_FETCH_OBSERVABILITY_GAP",
                    "lhs_path": "executor_summary.fetch_status_classification",
                    "rhs_path": "validator.evidence.prior_stop_count",
                    "lhs_value": str(phase3_bundle.get("fetch_status_classification") or ""),
                    "rhs_value": int(prior_stop_count),
                    "note": "Strong selected-route evidence exists, but downstream fetch/enrichment did not materialize loaded prior rows.",
                    "likely_implication": "diagnostics_visibility",
                }
            )

    return candidates


def _config_fingerprint(value: Any) -> str:
    body = _to_jsonable_text(value)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def _bbox_hash(bbox: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(bbox, dict):
        return None
    required = {"south", "west", "north", "east"}
    if not required.issubset(set(bbox.keys())):
        return None
    try:
        payload = {
            "south": round(float(bbox.get("south")), 6),
            "west": round(float(bbox.get("west")), 6),
            "north": round(float(bbox.get("north")), 6),
            "east": round(float(bbox.get("east")), 6),
        }
    except Exception:
        return None
    return _config_fingerprint(payload)[:16]


def _phase1_default_bbox() -> Dict[str, float]:
    return {"south": -0.38, "west": -78.60, "north": -0.02, "east": -78.35}


def _coerce_bbox_candidate(raw: Any) -> Optional[Dict[str, float]]:
    candidate: Optional[Dict[str, Any]] = None
    if isinstance(raw, dict):
        candidate = dict(raw or {})
    elif isinstance(raw, str):
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            candidate = dict(parsed or {})
        else:
            nums = re.findall(r"-?\d+(?:\.\d+)?", text)
            if len(nums) == 4:
                candidate = {
                    "south": nums[0],
                    "west": nums[1],
                    "north": nums[2],
                    "east": nums[3],
                }
    if not isinstance(candidate, dict):
        return None
    required = {"south", "west", "north", "east"}
    if not required.issubset(set(candidate.keys())):
        return None
    try:
        out = {
            "south": float(candidate["south"]),
            "west": float(candidate["west"]),
            "north": float(candidate["north"]),
            "east": float(candidate["east"]),
        }
    except Exception:
        return None
    if out["south"] >= out["north"] or out["west"] >= out["east"]:
        return None
    if not (-90.0 <= out["south"] <= 90.0 and -90.0 <= out["north"] <= 90.0):
        return None
    if not (-180.0 <= out["west"] <= 180.0 and -180.0 <= out["east"] <= 180.0):
        return None
    return out


def _normalize_spatial_tokens(*values: Any) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for raw in list(values or []):
        if isinstance(raw, (list, tuple)):
            items = list(raw or [])
        else:
            text = str(raw or "").strip()
            if not text:
                continue
            items = re.split(r"[\s,;|/]+", text)
        for item in items:
            token = str(item or "").strip().lower()
            token = re.sub(r"[^a-z0-9]+", "", token)
            if not token or token in seen:
                continue
            seen.add(token)
            out.append(token)
    return out[:12]


def _spatial_plan_signature(payload: Dict[str, Any]) -> str:
    basis = {
        "target_option_received": bool(payload.get("target_option_received")),
        "target_option_text": str(payload.get("target_option_text") or "").strip() or None,
        "area_group_hint": str(payload.get("area_group_hint") or "").strip() or None,
        "sector_hint": str(payload.get("sector_hint") or "").strip() or None,
        "corridor_hint": str(payload.get("corridor_hint") or "").strip() or None,
        "bbox_candidate_hash": _bbox_hash(_as_dict(payload.get("bbox_candidate"))),
        "strategy_used": str(payload.get("strategy_used") or "").strip() or None,
        "interpretation_status": str(payload.get("interpretation_status") or "").strip() or None,
        "fallback_reason": str(payload.get("fallback_reason") or "").strip() or None,
        "route_tokens": [str(x).strip().lower() for x in list(_as_dict(payload.get("route_context")).get("route_tokens") or []) if str(x).strip()][:8],
    }
    return _config_fingerprint(basis)[:24]


def _route_context_fingerprint(scope: Dict[str, Any]) -> str:
    payload = {
        "service_route_id": scope.get("service_route_id"),
        "direction_id": scope.get("direction_id"),
        "bbox_hash": _bbox_hash(_as_dict(scope.get("bbox"))),
        "refs": list(scope.get("refs") or []),
        "operator": scope.get("operator"),
        "name": scope.get("name"),
    }
    return _config_fingerprint(payload)[:24]


def _expand_bbox_pct(bbox: Dict[str, Any], pct: float) -> Optional[Dict[str, float]]:
    try:
        south = float(bbox["south"])
        west = float(bbox["west"])
        north = float(bbox["north"])
        east = float(bbox["east"])
    except Exception:
        return None
    ratio = max(0.0, float(pct) / 100.0)
    lat_span = max(0.0, north - south)
    lon_span = max(0.0, east - west)
    return {
        "south": south - (lat_span * ratio),
        "west": west - (lon_span * ratio),
        "north": north + (lat_span * ratio),
        "east": east + (lon_span * ratio),
    }


def _as_dict(v: Any) -> Dict[str, Any]:
    return dict(v or {}) if isinstance(v, dict) else {}


def _as_list(v: Any) -> List[Any]:
    return list(v or []) if isinstance(v, (list, tuple)) else []


def _latest_artifact_payload(state: RunSessionState, artifact_type: str) -> Dict[str, Any]:
    wanted = str(artifact_type or "").strip()
    if not wanted:
        return {}
    current = _as_dict(state.resume_context.get(wanted))
    if current:
        return current
    for rec in reversed(list(state.step_execution_records or [])):
        for artifact in reversed(list(rec.artifacts or [])):
            if str(artifact.get("artifact_type") or "").strip() != wanted:
                continue
            payload = _as_dict(artifact.get("payload"))
            if payload:
                return payload
    for artifacts in reversed(list(state.artifacts.values())):
        for artifact in reversed(list(artifacts or [])):
            if str(artifact.get("artifact_type") or "").strip() != wanted:
                continue
            payload = _as_dict(artifact.get("payload"))
            if payload:
                return payload
    return {}


def _phase1_to_phase2_handoff(state: RunSessionState) -> Dict[str, Any]:
    handoff = _as_dict(state.resume_context.get(PHASE1_TO_PHASE2_HANDOFF_ARTIFACT))
    if handoff:
        return handoff
    return _latest_artifact_payload(state, PHASE1_TO_PHASE2_HANDOFF_ARTIFACT)


def _iso_to_timestamp(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _build_patch_history_summary(
    *,
    state: RunSessionState,
    phase: str,
    step_id: str,
    block_reason_code: Optional[str],
    reason_class: Optional[str],
) -> Optional[Dict[str, Any]]:
    records = [
        _as_dict(row)
        for row in list(_as_dict(state.patch_registry).values())
        if isinstance(row, dict)
    ]
    if not records:
        return None

    phase_norm = str(phase or "").strip()
    block_norm = str(block_reason_code or "").strip()
    reason_norm = str(reason_class or "").strip()
    same_phase = [r for r in records if str(r.get("phase") or "").strip() == phase_norm] or records
    matched: List[Dict[str, Any]] = []
    for row in same_phase:
        row_block = str(row.get("origin_block_reason_code") or "").strip()
        row_reason = str(row.get("trigger_reason_class") or "").strip()
        if block_norm and row_block == block_norm:
            matched.append(row)
            continue
        if reason_norm and row_reason == reason_norm:
            matched.append(row)
            continue
    pool = matched or same_phase
    pool.sort(key=lambda row: _iso_to_timestamp(row.get("updated_at") or row.get("created_at")), reverse=True)
    latest = _as_dict(pool[0] if pool else {})
    if not latest:
        return None

    age_seconds = max(0, int(datetime.now(timezone.utc).timestamp() - _iso_to_timestamp(latest.get("updated_at"))))
    recency_note = (
        "very_recent" if age_seconds <= 3600 else ("recent" if age_seconds <= 86400 else "stale")
    )
    out = {
        "similar_pattern_seen": bool(matched),
        "record_count_total": int(len(records)),
        "record_count_phase": int(len(same_phase)),
        "record_count_similar": int(len(matched)),
        "last_patch_task_id": str(latest.get("patch_task_id") or ""),
        "last_patch_type": latest.get("patch_type"),
        "last_patch_branch": latest.get("patch_branch"),
        "last_status": latest.get("status"),
        "last_comparator_outcome": latest.get("comparator_outcome"),
        "last_operator_outcome_decision": latest.get("operator_outcome_decision"),
        "last_updated_at": latest.get("updated_at"),
        "recency_note": recency_note,
        "match_context": {
            "phase": phase_norm,
            "step_id": str(step_id or ""),
            "block_reason_code": (block_norm or None),
            "reason_class": (reason_norm or None),
        },
    }
    return out


def _norm_retry_strategy(retry_params: Dict[str, Any]) -> Optional[str]:
    raw = str(retry_params.get("retry_strategy") or "").strip()
    if raw:
        return raw
    if "bbox_expand_pct" in retry_params:
        return "bbox_expand"
    if "template_variant" in retry_params:
        return "template_alternative"
    if "fallback_config" in retry_params:
        return "fallback_config"
    return None


def _phase1_extract_retry_apply(
    *,
    base_conf: Dict[str, Any],
    retry_params: Dict[str, Any],
) -> Dict[str, Any]:
    conf = dict(base_conf or {})
    params = dict(retry_params or {})
    applied: Dict[str, Any] = {}
    unsupported: List[str] = []
    notes: List[str] = []

    runtime_keys = {
        "actions_path",
        "bbox",
        "candidate_actions",
        "sector_hint",
        "area_group_hint",
        "route_tokens",
        "extra_params",
        "max_actions",
        "max_bbox_retries",
        "eps_m",
        "min_pts",
        "bandit_key",
    }

    if "bbox_expand_pct" in params:
        pct = _handoff_to_float(params.get("bbox_expand_pct"))
        expanded = _expand_bbox_pct(_as_dict(conf.get("bbox")), pct or 0.0) if pct is not None else None
        if expanded is not None:
            conf["bbox"] = expanded
            applied["bbox_expand_pct"] = float(pct or 0.0)
        else:
            unsupported.append("bbox_expand_pct")
            notes.append("bbox_expand_pct_ignored_invalid_bbox")

    if "template_variant" in params:
        variant = str(params.get("template_variant") or "").strip()
        if variant:
            idx = _handoff_to_int(str(variant).split("_")[-1])
            actions = list(conf.get("candidate_actions") or [])
            if actions and idx is not None and len(actions) > 1:
                rotate = max(0, min(len(actions) - 1, int(idx) - 1))
                conf["candidate_actions"] = list(actions[rotate:] + actions[:rotate])
                applied["template_variant"] = variant
            elif actions and len(actions) == 1:
                # No practical alternative available but surfaced.
                notes.append("template_variant_requested_but_single_candidate_action")
                unsupported.append("template_variant")
            else:
                unsupported.append("template_variant")
                notes.append("template_variant_ignored_missing_candidate_actions")
        else:
            unsupported.append("template_variant")

    if "fallback_config" in params:
        fallback = str(params.get("fallback_config") or "").strip()
        if fallback:
            idx = _handoff_to_int(str(fallback).split("_")[-1])
            if idx is not None:
                base = _handoff_to_int(conf.get("max_bbox_retries")) or 4
                conf["max_bbox_retries"] = max(1, int(base + max(1, int(idx) - 1)))
                applied["fallback_config"] = fallback
            else:
                unsupported.append("fallback_config")
                notes.append("fallback_config_ignored_invalid_suffix")
        else:
            unsupported.append("fallback_config")

    passthrough = {"retry_strategy", "retry_reason", "retry_parameter_delta"}
    for key in list(params.keys()):
        if key in {"bbox_expand_pct", "template_variant", "fallback_config"}:
            continue
        if key in passthrough:
            conf[key] = params.get(key)
            continue
        if key in runtime_keys:
            conf[key] = params.get(key)
            applied[key] = params.get(key)
            continue
        unsupported.append(str(key))

    retry_strategy = _norm_retry_strategy(params)
    retry_reason = str(params.get("retry_reason") or "").strip() or None
    retry_delta = dict(params)
    for key in passthrough:
        retry_delta.pop(key, None)

    effective = {
        "bbox": _as_dict(conf.get("bbox")),
        "candidate_actions": list(conf.get("candidate_actions") or []),
        "max_actions": _handoff_to_int(conf.get("max_actions")),
        "max_bbox_retries": _handoff_to_int(conf.get("max_bbox_retries")),
        "extra_params": _as_dict(conf.get("extra_params")),
    }
    effective_fingerprint = _config_fingerprint(effective)

    warnings = [f"unsupported_retry_param:{k}" for k in list(dict.fromkeys(unsupported))]
    return {
        "effective_conf": conf,
        "retry_strategy": retry_strategy,
        "retry_reason": retry_reason,
        "retry_parameter_delta": retry_delta,
        "retry_parameter_applied": applied,
        "retry_parameter_warnings": warnings,
        "notes": list(dict.fromkeys(notes)),
        "effective_config_fingerprint": effective_fingerprint,
    }


def _phase3_extract_retry_apply(
    *,
    base_conf: Dict[str, Any],
    retry_params: Dict[str, Any],
    fetch_only_path: bool,
) -> Dict[str, Any]:
    conf = dict(base_conf or {})
    params = dict(retry_params or {})
    applied: Dict[str, Any] = {}
    unsupported: List[str] = []
    notes: List[str] = []

    if fetch_only_path:
        for key in list(params.keys()):
            if key not in {"osm_relation_id"}:
                unsupported.append(str(key))
        effective = {
            "fetch_only_path": True,
            "osm_relation_id": params.get("osm_relation_id"),
        }
        return {
            "effective_conf": conf,
            "retry_strategy": _norm_retry_strategy(params),
            "retry_reason": (str(params.get("retry_reason") or "").strip() or None),
            "retry_parameter_delta": dict(params),
            "retry_parameter_applied": {"osm_relation_id": params.get("osm_relation_id")} if params.get("osm_relation_id") is not None else {},
            "retry_parameter_warnings": [f"unsupported_retry_param:{k}" for k in list(dict.fromkeys(unsupported))],
            "notes": ["fetch_only_retry_path"] if unsupported else [],
            "effective_config_fingerprint": _config_fingerprint(effective),
            "fallback_profile_used": None,
        }

    if "bbox_expand_pct" in params:
        pct = _handoff_to_float(params.get("bbox_expand_pct"))
        expanded = _expand_bbox_pct(_as_dict(conf.get("bbox")), pct or 0.0) if pct is not None else None
        if expanded is not None:
            conf["bbox"] = expanded
            applied["bbox_expand_pct"] = float(pct or 0.0)
        else:
            unsupported.append("bbox_expand_pct")
            notes.append("bbox_expand_pct_ignored_invalid_bbox")

    if "template_variant" in params:
        variant = str(params.get("template_variant") or "").strip()
        variants = _as_dict(conf.get("discover_variants"))
        candidate = _as_dict(variants.get(variant))
        if candidate:
            for key in ("refs", "operator", "name", "max_candidates", "timeout_s", "query_strategy"):
                if key in candidate:
                    conf[key] = candidate.get(key)
            applied["template_variant"] = variant
            notes.append(f"template_variant_applied:{variant}")
        else:
            unsupported.append("template_variant")
            notes.append("template_variant_ignored_missing_profile")

    fallback_profile_used = None
    if "fallback_config" in params:
        name = str(params.get("fallback_config") or "").strip()
        builtin_profiles = {
            "fallback_1": {"max_candidates": 12, "timeout_s": 120},
            "fallback_2": {"max_candidates": 8, "timeout_s": 90},
            "fallback_3": {"max_candidates": 5, "timeout_s": 75},
        }
        profiles = {**builtin_profiles, **_as_dict(conf.get("fallback_profiles"))}
        selected = _as_dict(profiles.get(name))
        if selected:
            for key in ("max_candidates", "timeout_s", "refs", "operator", "name", "query_strategy"):
                if key in selected:
                    conf[key] = selected.get(key)
            applied["fallback_config"] = name
            fallback_profile_used = name
        else:
            unsupported.append("fallback_config")
            notes.append("fallback_config_ignored_missing_profile")

    passthrough = {
        "retry_strategy",
        "retry_reason",
        "retry_parameter_delta",
        "osm_relation_id",
    }
    for key in list(params.keys()):
        if key in {"bbox_expand_pct", "template_variant", "fallback_config"}:
            continue
        if key in passthrough:
            conf[key] = params.get(key)
            if key == "osm_relation_id":
                applied[key] = params.get(key)
            continue
        unsupported.append(str(key))

    retry_strategy = _norm_retry_strategy(params)
    retry_reason = str(params.get("retry_reason") or "").strip() or None
    retry_delta = dict(params)
    for key in passthrough:
        retry_delta.pop(key, None)

    effective = {
        "bbox": _as_dict(conf.get("bbox")),
        "refs": list(conf.get("refs") or []),
        "operator": conf.get("operator"),
        "name": conf.get("name"),
        "max_candidates": _handoff_to_int(conf.get("max_candidates")),
        "timeout_s": _handoff_to_int(conf.get("timeout_s")),
        "query_strategy": conf.get("query_strategy"),
        "fallback_profile_used": fallback_profile_used,
    }
    effective_fingerprint = _config_fingerprint(effective)
    warnings = [f"unsupported_retry_param:{k}" for k in list(dict.fromkeys(unsupported))]
    return {
        "effective_conf": conf,
        "retry_strategy": retry_strategy,
        "retry_reason": retry_reason,
        "retry_parameter_delta": retry_delta,
        "retry_parameter_applied": applied,
        "retry_parameter_warnings": warnings,
        "notes": list(dict.fromkeys(notes)),
        "effective_config_fingerprint": effective_fingerprint,
        "fallback_profile_used": fallback_profile_used,
    }


def _attempt_record(
    *,
    phase: str,
    step_id: str,
    attempt_no: int,
    run_id: Optional[str],
    trace_id: Optional[str],
    area_group: Optional[str] = None,
    sector: Optional[str] = None,
    bbox_used: Optional[Dict[str, Any]] = None,
    bbox_hash: Optional[str] = None,
    bbox_fingerprint: Optional[str] = None,
    route_id: Optional[str] = None,
    corridor_context: Optional[str] = None,
    route_context_fingerprint: Optional[str] = None,
    retry_strategy: Optional[str] = None,
    retry_reason: Optional[str] = None,
    retry_parameter_delta: Optional[Dict[str, Any]] = None,
    action_or_template_used: Optional[str] = None,
    fallback_config_used: Optional[str] = None,
    fallback_profile_used: Optional[str] = None,
    status: Optional[str] = None,
    duration_ms: Optional[int] = None,
    timeout_flag: Optional[bool] = None,
    response_status: Optional[Any] = None,
    response_size_bytes: Optional[int] = None,
    raw_elements_count: Optional[int] = None,
    candidate_count: Optional[int] = None,
    extractor_diagnostics_summary: Optional[Dict[str, Any]] = None,
    error_code: Optional[str] = None,
    error_summary: Optional[str] = None,
    effective_config_fingerprint: Optional[str] = None,
    attempt_changed_from_previous: Optional[bool] = None,
    spatial_plan_signature: Optional[str] = None,
    retry_changed_spatial_plan: Optional[bool] = None,
    spatial_interpretation_status: Optional[str] = None,
    runtime_spatial_strategy_used: Optional[str] = None,
    spatial_interpretation_failure_reason: Optional[str] = None,
    target_option_received: Optional[bool] = None,
    notes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    return {
        "phase": str(phase or ""),
        "step_id": str(step_id or ""),
        "attempt_no": int(attempt_no),
        "run_id": (str(run_id) if run_id else None),
        "trace_id": (str(trace_id) if trace_id else None),
        "area_group": (str(area_group) if area_group else None),
        "sector": (str(sector) if sector else None),
        "bbox_used": _as_dict(bbox_used) or None,
        "bbox_hash": (str(bbox_hash) if bbox_hash else None),
        "bbox_fingerprint": (str(bbox_fingerprint) if bbox_fingerprint else None),
        "route_id": (str(route_id) if route_id else None),
        "corridor_context": (str(corridor_context) if corridor_context else None),
        "route_context_fingerprint": (str(route_context_fingerprint) if route_context_fingerprint else None),
        "retry_strategy": (str(retry_strategy) if retry_strategy else None),
        "retry_reason": (str(retry_reason) if retry_reason else None),
        "retry_parameter_delta": dict(retry_parameter_delta or {}),
        "action_or_template_used": (str(action_or_template_used) if action_or_template_used else None),
        "fallback_config_used": (str(fallback_config_used) if fallback_config_used else None),
        "fallback_profile_used": (str(fallback_profile_used) if fallback_profile_used else None),
        "status": (str(status) if status else None),
        "duration_ms": (int(duration_ms) if duration_ms is not None else None),
        "timeout_flag": (bool(timeout_flag) if timeout_flag is not None else None),
        "response_status": response_status,
        "response_size_bytes": (int(response_size_bytes) if response_size_bytes is not None else None),
        "raw_elements_count": (int(raw_elements_count) if raw_elements_count is not None else None),
        "candidate_count": (int(candidate_count) if candidate_count is not None else None),
        "extractor_diagnostics_summary": _as_dict(extractor_diagnostics_summary) or None,
        "error_code": (str(error_code) if error_code else None),
        "error_summary": (str(error_summary) if error_summary else None),
        "effective_config_fingerprint": (str(effective_config_fingerprint) if effective_config_fingerprint else None),
        "attempt_changed_from_previous": (bool(attempt_changed_from_previous) if attempt_changed_from_previous is not None else None),
        "spatial_plan_signature": (str(spatial_plan_signature) if spatial_plan_signature else None),
        "retry_changed_spatial_plan": (bool(retry_changed_spatial_plan) if retry_changed_spatial_plan is not None else None),
        "spatial_interpretation_status": (
            str(spatial_interpretation_status) if spatial_interpretation_status else None
        ),
        "runtime_spatial_strategy_used": (
            str(runtime_spatial_strategy_used) if runtime_spatial_strategy_used else None
        ),
        "spatial_interpretation_failure_reason": (
            str(spatial_interpretation_failure_reason) if spatial_interpretation_failure_reason else None
        ),
        "target_option_received": (bool(target_option_received) if target_option_received is not None else None),
        "notes": [str(x) for x in list(notes or []) if str(x).strip()][:16],
    }


def _mark_attempt_change(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    prev_fp: Optional[str] = None
    for row in list(records or []):
        rec = dict(row or {})
        fp = str(rec.get("effective_config_fingerprint") or "").strip() or None
        if rec.get("attempt_changed_from_previous") is None:
            if prev_fp is None or fp is None:
                rec["attempt_changed_from_previous"] = True
            else:
                rec["attempt_changed_from_previous"] = bool(fp != prev_fp)
        if fp:
            prev_fp = fp
        out.append(rec)
    return out


def _summarize_attempt_records(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [dict(x or {}) for x in list(records or [])]
    statuses = [str(r.get("status") or "").strip().lower() for r in rows]
    timeout_count = sum(1 for r in rows if bool(r.get("timeout_flag")))
    unique_fp_ordered: List[str] = []
    fp_seen: set[str] = set()
    same_config_repeat_count = 0
    for r in rows:
        fp = str(r.get("effective_config_fingerprint") or "").strip()
        if not fp:
            continue
        if fp in fp_seen:
            same_config_repeat_count += 1
            continue
        fp_seen.add(fp)
        unique_fp_ordered.append(fp)

    retry_strategies_attempted: List[str] = []
    for r in rows:
        strategy = str(r.get("retry_strategy") or "").strip()
        if not strategy:
            continue
        if strategy in retry_strategies_attempted:
            continue
        retry_strategies_attempted.append(strategy)

    fallback_used = any(
        bool(str(r.get("fallback_config_used") or "").strip())
        or bool(str(r.get("fallback_profile_used") or "").strip())
        for r in rows
    )

    def _is_non_empty(rec: Dict[str, Any]) -> bool:
        cand = _handoff_to_int(rec.get("candidate_count"))
        raw = _handoff_to_int(rec.get("raw_elements_count"))
        if cand is not None and cand > 0:
            return True
        if raw is not None and raw > 0:
            return True
        return False

    non_empty_count = sum(1 for r in rows if _is_non_empty(r))
    unique_spatial_sigs: List[str] = []
    spatial_sig_seen: set[str] = set()
    same_spatial_plan_repeat_count = 0
    target_option_received = any(bool(r.get("target_option_received")) for r in rows)
    latest_spatial_status: Optional[str] = None
    latest_spatial_failure_reason: Optional[str] = None
    for r in rows:
        if str(r.get("spatial_interpretation_status") or "").strip():
            latest_spatial_status = str(r.get("spatial_interpretation_status") or "").strip()
        if str(r.get("spatial_interpretation_failure_reason") or "").strip():
            latest_spatial_failure_reason = str(r.get("spatial_interpretation_failure_reason") or "").strip()
        sig = str(r.get("spatial_plan_signature") or "").strip()
        if not sig:
            continue
        if sig in spatial_sig_seen:
            same_spatial_plan_repeat_count += 1
            continue
        spatial_sig_seen.add(sig)
        unique_spatial_sigs.append(sig)

    def _compact(rec: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "attempt_no": _handoff_to_int(rec.get("attempt_no")),
            "status": rec.get("status"),
            "retry_strategy": rec.get("retry_strategy"),
            "action_or_template_used": rec.get("action_or_template_used"),
            "fallback_config_used": rec.get("fallback_config_used"),
            "fallback_profile_used": rec.get("fallback_profile_used"),
            "candidate_count": _handoff_to_int(rec.get("candidate_count")),
            "raw_elements_count": _handoff_to_int(rec.get("raw_elements_count")),
            "duration_ms": _handoff_to_int(rec.get("duration_ms")),
            "timeout_flag": bool(rec.get("timeout_flag")) if rec.get("timeout_flag") is not None else None,
            "error_code": rec.get("error_code"),
            "effective_config_fingerprint": rec.get("effective_config_fingerprint"),
            "attempt_changed_from_previous": rec.get("attempt_changed_from_previous"),
            "spatial_plan_signature": rec.get("spatial_plan_signature"),
            "retry_changed_spatial_plan": rec.get("retry_changed_spatial_plan"),
            "spatial_interpretation_status": rec.get("spatial_interpretation_status"),
            "runtime_spatial_strategy_used": rec.get("runtime_spatial_strategy_used"),
            "spatial_interpretation_failure_reason": rec.get("spatial_interpretation_failure_reason"),
            "target_option_received": (
                bool(rec.get("target_option_received"))
                if rec.get("target_option_received") is not None
                else None
            ),
        }

    compact_attempts: List[Dict[str, Any]] = [_compact(r) for r in rows[:8]]
    attempts_truncated = len(rows) > len(compact_attempts)
    best_attempt_index: Optional[int] = None
    best_rank: Optional[tuple[int, int, int]] = None
    for idx, r in enumerate(rows, start=1):
        cand = _handoff_to_int(r.get("candidate_count")) or 0
        raw = _handoff_to_int(r.get("raw_elements_count")) or 0
        status = str(r.get("status") or "").strip().lower()
        ok_flag = 1 if status in {"success", "ok", "pass"} else 0
        rank = (cand, raw, ok_flag)
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best_attempt_index = idx

    success_count = int(sum(1 for s in statuses if s in {"success", "ok", "pass"}))
    unique_fp_count = int(len(unique_fp_ordered))
    summary = {
        "attempt_count_total": int(len(rows)),
        "successful_attempt_count": success_count,
        "non_empty_attempt_count": int(non_empty_count),
        "fallback_used": bool(fallback_used),
        "retry_strategies_attempted": retry_strategies_attempted[:8],
        "effective_config_fingerprints": unique_fp_ordered[:12],
        "same_config_repeat_count": int(same_config_repeat_count),
        "same_spatial_plan_repeat_count": int(same_spatial_plan_repeat_count),
        "best_attempt_index": best_attempt_index,
        "attempts": compact_attempts,
        "attempts_truncated": bool(attempts_truncated),
        "target_option_received": bool(target_option_received),
        "spatial_interpretation_status": latest_spatial_status,
        "spatial_interpretation_failure_reason": latest_spatial_failure_reason,
        "spatial_plan_signatures": unique_spatial_sigs[:8],
        # Backward-compatible aliases (Stage 0 / existing consumers)
        "attempt_count": int(len(rows)),
        "success_count": success_count,
        "error_count": int(sum(1 for s in statuses if s in {"error", "failed", "timeout"})),
        "timeout_count": int(timeout_count),
        "diversified_attempts": bool(unique_fp_count > 1),
        "unique_config_count": unique_fp_count,
    }
    return summary


def _status_clip01(value: Any) -> Optional[float]:
    parsed = _handoff_to_float(value)
    if parsed is None:
        return None
    return max(0.0, min(1.0, float(parsed)))


def _status_norm_01(value: Any) -> Optional[float]:
    parsed = _handoff_to_float(value)
    if parsed is None:
        return None
    score = float(parsed)
    if score > 1.0:
        score = score / 100.0
    return max(0.0, min(1.0, score))


def _status_norm_100(value: Any) -> Optional[float]:
    parsed = _handoff_to_float(value)
    if parsed is None:
        return None
    score = float(parsed)
    if score <= 1.0:
        score = score * 100.0
    return max(0.0, min(100.0, score))


def _weighted_score_01(values: Sequence[tuple[Optional[float], float]]) -> Optional[float]:
    used = 0.0
    total = 0.0
    for raw_v, raw_w in list(values or []):
        if raw_v is None:
            continue
        w = float(raw_w or 0.0)
        if w <= 0.0:
            continue
        v = _status_clip01(raw_v)
        if v is None:
            continue
        used += w
        total += float(v) * w
    if used <= 0.0:
        return None
    return max(0.0, min(1.0, total / used))


def _extract_attempt_history_summary(
    *,
    exec_summary: Dict[str, Any],
    validator_evidence: Dict[str, Any],
) -> Dict[str, Any]:
    summary = dict(exec_summary or {})
    evidence = dict(validator_evidence or {})
    hist = _as_dict(
        summary.get("extraction_attempt_history_summary")
        or summary.get("extraction_attempts_summary")
        or evidence.get("extractor_attempt_history_summary")
        or evidence.get("extractor_attempts_summary")
    )
    if not hist:
        rows = _as_list(summary.get("extraction_attempt_records"))
        if rows:
            hist = _summarize_attempt_records([dict(x or {}) for x in rows])
    else:
        attempt_rows = [dict(x or {}) for x in list(hist.get("attempts") or [])]
        attempt_count_total = _handoff_to_int(hist.get("attempt_count_total"))
        if attempt_count_total is None:
            attempt_count_total = _handoff_to_int(hist.get("attempt_count"))
        attempt_count_total = int(attempt_count_total or 0)
        if attempt_rows and attempt_count_total <= 0:
            # Repair incoherent summary where compact attempts exist but total count is zero.
            hist = _summarize_attempt_records(attempt_rows)
        elif attempt_count_total > 0 and not attempt_rows:
            # Repair incoherent summary from full attempt records when compact attempts are missing.
            rows = _as_list(summary.get("extraction_attempt_records"))
            if rows:
                hist = _summarize_attempt_records([dict(x or {}) for x in rows])
    return hist


def _attempt_status_success(attempt: Dict[str, Any]) -> bool:
    status = str(attempt.get("status") or "").strip().lower()
    return status in {"success", "ok", "pass"}


def _attempt_non_empty(attempt: Dict[str, Any]) -> bool:
    cand = _handoff_to_int(attempt.get("candidate_count"))
    raw = _handoff_to_int(attempt.get("raw_elements_count"))
    return bool((cand is not None and cand > 0) or (raw is not None and raw > 0))


def _phase3_has_strong_stop_prior_reason(reason_codes: Sequence[Any]) -> bool:
    for raw in list(reason_codes or []):
        code = str(raw or "").strip().lower()
        if not code:
            continue
        if code == "stop_prior_signal_strong":
            return True
        if "stop_prior_signal" in code and code not in {"stop_prior_signal_missing", "stop_prior_signal_weak"}:
            return True
    return False


def _phase3_route_bundle_evidence(
    *,
    summary: Dict[str, Any],
    payload: Optional[Dict[str, Any]] = None,
    validator_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    summary_obj = dict(summary or {})
    payload_obj = dict(payload or {})
    evidence_obj = dict(validator_evidence or {})

    candidate_universe_summary = _as_dict(
        summary_obj.get("candidate_universe_summary")
        or payload_obj.get("candidate_universe_summary")
        or evidence_obj.get("candidate_universe_summary")
    )
    selection_summary = _as_dict(
        summary_obj.get("selection_summary")
        or payload_obj.get("selection_summary")
        or evidence_obj.get("selection_summary")
    )
    selected_relation_summary = _as_dict(
        summary_obj.get("selected_relation_summary")
        or payload_obj.get("selected_relation_summary")
        or evidence_obj.get("selected_relation_summary")
    )
    extractor_diagnostics = _as_dict(
        summary_obj.get("extractor_diagnostics")
        or payload_obj.get("extractor_diagnostics")
        or evidence_obj.get("extractor_diagnostics")
    )

    route_id = (
        str(
            summary_obj.get("route_id")
            or payload_obj.get("route_id")
            or evidence_obj.get("route_id")
            or ""
        ).strip()
        or None
    )
    actual_prior_stop_count_raw = _handoff_to_int(
        summary_obj.get("prior_stop_count")
        if summary_obj.get("prior_stop_count") is not None
        else (
            payload_obj.get("prior_stop_count")
            if payload_obj.get("prior_stop_count") is not None
            else evidence_obj.get("prior_stop_count")
        )
    )
    actual_prior_stop_count = int(actual_prior_stop_count_raw or 0)
    candidate_universe_count = _handoff_to_int(
        candidate_universe_summary.get("candidate_universe_count")
        if candidate_universe_summary.get("candidate_universe_count") is not None
        else (
            candidate_universe_summary.get("candidate_count")
            if candidate_universe_summary.get("candidate_count") is not None
            else (
                payload_obj.get("discover_candidate_count")
                if payload_obj.get("discover_candidate_count") is not None
                else (
                    payload_obj.get("candidate_universe_count")
                    if payload_obj.get("candidate_universe_count") is not None
                    else extractor_diagnostics.get("candidate_count")
                )
            )
        )
    )
    candidate_universe_count = int(candidate_universe_count or 0)

    selected_relation_id = _handoff_to_int(
        selection_summary.get("selected_osm_relation_id")
        if selection_summary.get("selected_osm_relation_id") is not None
        else (
            selected_relation_summary.get("osm_relation_id")
            if selected_relation_summary.get("osm_relation_id") is not None
            else payload_obj.get("osm_relation_id")
        )
    )
    selected_relation_present = selected_relation_id is not None
    selected_relation_stop_prior_count = _handoff_to_int(
        selection_summary.get("selected_relation_stop_prior_count")
        if selection_summary.get("selected_relation_stop_prior_count") is not None
        else (
            selected_relation_summary.get("stop_prior_count")
            if selected_relation_summary.get("stop_prior_count") is not None
            else evidence_obj.get("selected_relation_stop_prior_count")
        )
    )
    selected_relation_stop_prior_count = int(selected_relation_stop_prior_count or 0)
    top_stop_prior_count = _handoff_to_int(
        candidate_universe_summary.get("top_stop_prior_count")
        if candidate_universe_summary.get("top_stop_prior_count") is not None
        else extractor_diagnostics.get("top_stop_prior_count")
    )
    top_stop_prior_count = int(top_stop_prior_count or 0)
    selection_confidence = _handoff_to_float(
        selection_summary.get("selection_confidence")
        if selection_summary.get("selection_confidence") is not None
        else selected_relation_summary.get("selection_confidence")
    )
    selection_reason_codes = list(
        dict.fromkeys(
            [
                str(x).strip()
                for x in (
                    list(selection_summary.get("selection_reason_codes") or [])
                    + list(selected_relation_summary.get("selection_reason_codes") or [])
                    + list(evidence_obj.get("selection_reason_codes") or [])
                )
                if str(x).strip()
            ]
        )
    )
    candidate_universe_nontrivial = bool(candidate_universe_count >= 2)
    meaningful_selection_confidence = bool(
        selection_confidence is not None
        and float(selection_confidence) >= float(P3_STRONG_SELECTION_CONFIDENCE_MIN)
    )
    strong_stop_prior_reason_present = _phase3_has_strong_stop_prior_reason(selection_reason_codes)
    positive_signal_count = int(
        sum(
            1
            for flag in (
                candidate_universe_nontrivial,
                selected_relation_stop_prior_count > 0,
                top_stop_prior_count > 0,
                meaningful_selection_confidence,
                strong_stop_prior_reason_present,
            )
            if flag
        )
    )
    strong_bundle_evidence = bool(
        route_id
        and selected_relation_present
        and positive_signal_count >= 3
    )
    prior_stop_evidence_count = int(
        max(
            actual_prior_stop_count,
            selected_relation_stop_prior_count,
            top_stop_prior_count,
        )
    )

    fetch_http_status = _handoff_to_int(
        summary_obj.get("http_status")
        if summary_obj.get("http_status") is not None
        else payload_obj.get("http_status")
    )
    fetch_raw_count = _handoff_to_int(
        summary_obj.get("raw_count")
        if summary_obj.get("raw_count") is not None
        else payload_obj.get("raw_count")
    )
    fetch_candidate_count = _handoff_to_int(
        summary_obj.get("candidate_count")
        if summary_obj.get("candidate_count") is not None
        else payload_obj.get("candidate_count")
    )
    fetch_stored = _handoff_to_bool(
        summary_obj.get("stored")
        if summary_obj.get("stored") is not None
        else payload_obj.get("stored")
    )
    fetch_success_signal = bool(
        fetch_stored
        or (
            fetch_http_status is not None
            and 200 <= int(fetch_http_status) < 300
        )
    )
    fetch_nonempty_signal = bool(
        (fetch_raw_count is not None and int(fetch_raw_count) > 0)
        or (fetch_candidate_count is not None and int(fetch_candidate_count) > 0)
    )
    fetch_artifact_present = bool(
        fetch_success_signal
        or fetch_nonempty_signal
        or selected_relation_summary
        or _handoff_to_int(payload_obj.get("osm_relation_id")) is not None
        or fetch_http_status is not None
    )

    fetch_status_classification = (
        str(
            summary_obj.get("fetch_status_classification")
            or payload_obj.get("fetch_status_classification")
            or evidence_obj.get("fetch_status_classification")
            or ""
        ).strip()
        or None
    )
    if not fetch_status_classification:
        if actual_prior_stop_count > 0:
            fetch_status_classification = "success"
        elif strong_bundle_evidence and fetch_success_signal and prior_stop_evidence_count > 0:
            fetch_status_classification = "fetch_partial_after_valid_selection"
        elif strong_bundle_evidence and fetch_artifact_present and prior_stop_evidence_count > 0:
            fetch_status_classification = "selected_relation_fetch_inconsistent"
        elif strong_bundle_evidence:
            fetch_status_classification = "selected_relation_not_fully_enriched"
        elif fetch_success_signal or fetch_nonempty_signal:
            fetch_status_classification = "success"
        else:
            fetch_status_classification = "empty"

    contradiction_codes: List[str] = []
    if actual_prior_stop_count <= 0 and selected_relation_stop_prior_count > 0:
        contradiction_codes.append("selected_relation_prior_stops_exceed_loaded_prior_stops")
    if actual_prior_stop_count <= 0 and top_stop_prior_count > 0:
        contradiction_codes.append("top_relation_prior_stops_exceed_loaded_prior_stops")
    if strong_bundle_evidence and fetch_status_classification in P3_PARTIAL_FETCH_CLASSIFICATIONS:
        contradiction_codes.append("strong_bundle_evidence_with_partial_fetch")

    bundle_success_classification = "extract_empty"
    if route_id and strong_bundle_evidence:
        bundle_success_classification = (
            "strong_selected_bundle"
            if actual_prior_stop_count > 0
            else "strong_selected_bundle_fetch_incomplete"
        )
    elif route_id and selected_relation_present:
        bundle_success_classification = "selected_bundle_low_evidence"
    elif route_id:
        bundle_success_classification = "route_context_created"

    return {
        "route_id": route_id,
        "route_id_present": bool(route_id),
        "actual_prior_stop_count": int(actual_prior_stop_count),
        "prior_stop_evidence_count": int(prior_stop_evidence_count),
        "candidate_universe_count": int(candidate_universe_count),
        "candidate_universe_nontrivial": bool(candidate_universe_nontrivial),
        "selected_relation_present": bool(selected_relation_present),
        "selected_osm_relation_id": selected_relation_id,
        "selected_relation_stop_prior_count": int(selected_relation_stop_prior_count),
        "top_stop_prior_count": int(top_stop_prior_count),
        "selection_confidence": selection_confidence,
        "selection_reason_codes": selection_reason_codes[:8],
        "meaningful_selection_confidence": bool(meaningful_selection_confidence),
        "strong_stop_prior_reason_present": bool(strong_stop_prior_reason_present),
        "positive_signal_count": int(positive_signal_count),
        "strong_bundle_evidence": bool(strong_bundle_evidence),
        "fetch_artifact_present": bool(fetch_artifact_present),
        "fetch_success_signal": bool(fetch_success_signal or fetch_nonempty_signal),
        "fetch_status_classification": fetch_status_classification,
        "fetch_observability_gap": bool(fetch_status_classification in P3_PARTIAL_FETCH_CLASSIFICATIONS),
        "bundle_success_classification": bundle_success_classification,
        "contradiction_codes": contradiction_codes[:8],
        "candidate_universe_summary": candidate_universe_summary,
        "selection_summary": selection_summary,
        "selected_relation_summary": selected_relation_summary,
        "extractor_diagnostics": extractor_diagnostics,
    }


def _cumulative_duration_ms_for_index(attempts: Sequence[Dict[str, Any]], index_1_based: Optional[int]) -> Optional[int]:
    idx = _handoff_to_int(index_1_based)
    if idx is None or idx <= 0:
        return None
    rows = [dict(x or {}) for x in list(attempts or [])]
    selected = [r for r in rows if (_handoff_to_int(r.get("attempt_no")) or 0) <= int(idx)]
    if not selected or len(selected) < int(idx):
        return None
    total = 0
    for row in selected:
        dur = _handoff_to_int(row.get("duration_ms"))
        if dur is None:
            return None
        total += int(dur)
    return int(total)


def _extractor_relevant_deltas(compare: Dict[str, Any]) -> Dict[str, Any]:
    deltas = _as_dict((compare or {}).get("deltas"))
    out: Dict[str, Any] = {}
    tokens = (
        "quality",
        "candidate",
        "prior_stop",
        "matched",
        "unmatched",
        "ambiguous",
        "warning",
        "retry",
        "attempt",
        "extract",
    )
    for key, val in deltas.items():
        k = str(key or "").strip().lower()
        if not k:
            continue
        if any(tok in k for tok in tokens):
            out[str(key)] = val
    return out


def _build_extractor_status(
    *,
    state: RunSessionState,
    step: StepDefinition,
    attempt_no: int = 0,
    exec_summary: Dict[str, Any],
    validator_result: ValidatorResult,
    compare: Dict[str, Any],
    reorder: Dict[str, Any],
) -> Dict[str, Any]:
    if str(step.step_id or "") not in {STEP_P1_1_EXTRACT, STEP_P2_1_SEMANTIC, STEP_P3_1_EXTRACT, STEP_P3_2_STEP20}:
        return {}

    summary = dict(exec_summary or {})
    evidence = dict(validator_result.evidence or {})
    history = _extract_attempt_history_summary(exec_summary=summary, validator_evidence=evidence)
    attempts = [dict(x or {}) for x in list(history.get("attempts") or [])]
    if not attempts:
        # Fallback to full attempt records when compact summary list is unavailable.
        attempts = [dict(x or {}) for x in list(summary.get("extraction_attempt_records") or [])]

    attempt_count_total = int(
        history.get("attempt_count_total")
        or history.get("attempt_count")
        or len(attempts)
        or 0
    )
    successful_attempt_count = int(
        history.get("successful_attempt_count")
        or history.get("success_count")
        or sum(1 for a in attempts if _attempt_status_success(a))
    )
    non_empty_attempt_count = int(
        history.get("non_empty_attempt_count")
        if history.get("non_empty_attempt_count") is not None
        else sum(1 for a in attempts if _attempt_non_empty(a))
    )
    fallback_used = bool(
        history.get("fallback_used")
        or any(
            bool(str(a.get("fallback_config_used") or "").strip())
            or bool(str(a.get("fallback_profile_used") or "").strip())
            for a in attempts
        )
    )
    retry_strategies_attempted = [
        str(x).strip()
        for x in list(history.get("retry_strategies_attempted") or [])
        if str(x).strip()
    ]
    if not retry_strategies_attempted:
        for a in attempts:
            txt = str(a.get("retry_strategy") or "").strip()
            if txt and txt not in retry_strategies_attempted:
                retry_strategies_attempted.append(txt)
    fingerprints = [
        str(x).strip()
        for x in list(history.get("effective_config_fingerprints") or [])
        if str(x).strip()
    ]
    if not fingerprints:
        seen_fp: set[str] = set()
        for a in attempts:
            txt = str(a.get("effective_config_fingerprint") or "").strip()
            if not txt or txt in seen_fp:
                continue
            seen_fp.add(txt)
            fingerprints.append(txt)
    retry_diversity_count = int(
        history.get("retry_diversity_count")
        or history.get("unique_config_count")
        or len(fingerprints)
        or 0
    )
    same_config_repeat_count = int(
        history.get("same_config_repeat_count")
        if history.get("same_config_repeat_count") is not None
        else max(0, int(attempt_count_total - max(1, retry_diversity_count)))
    )
    missing_attempt_history = bool(attempt_count_total <= 0 or not attempts)
    repeated_attempt_context = bool(int(attempt_no or 0) >= 2)
    spatial_context = _as_dict(
        summary.get("spatial_interpretation")
        or evidence.get("spatial_interpretation")
    )
    target_option_received = bool(
        spatial_context.get("target_option_received")
        or str(spatial_context.get("target_option_text") or "").strip()
    )
    target_option_text = str(spatial_context.get("target_option_text") or "").strip() or None
    spatial_interpretation_attempted = bool(
        spatial_context.get("spatial_interpretation_attempted")
        or target_option_received
        or _coerce_bbox_candidate(spatial_context.get("bbox_candidate")) is not None
    )
    spatial_interpretation_status = (
        str(
            spatial_context.get("spatial_interpretation_status")
            or spatial_context.get("interpretation_status")
            or ""
        ).strip()
        or None
    )
    spatial_interpretation_source = (
        str(
            spatial_context.get("spatial_interpretation_source")
            or spatial_context.get("interpreted_by")
            or ""
        ).strip()
        or None
    )
    bbox_candidate = _coerce_bbox_candidate(spatial_context.get("bbox_candidate"))
    bbox_candidate_confidence = _handoff_to_float(spatial_context.get("bbox_candidate_confidence"))
    area_group_hint = str(spatial_context.get("area_group_hint") or "").strip() or None
    sector_hint = str(spatial_context.get("sector_hint") or "").strip() or None
    corridor_hint = str(spatial_context.get("corridor_hint") or "").strip() or None
    runtime_bbox_used = _coerce_bbox_candidate(
        spatial_context.get("runtime_bbox_used")
        if spatial_context.get("runtime_bbox_used") is not None
        else summary.get("bbox")
    )
    runtime_spatial_strategy_used = (
        str(
            spatial_context.get("runtime_spatial_strategy_used")
            or spatial_context.get("strategy_used")
            or ""
        ).strip()
        or None
    )
    retry_changed_spatial_plan = _handoff_to_bool(spatial_context.get("retry_changed_spatial_plan"))
    same_spatial_plan_retry_count = int(
        _handoff_to_int(spatial_context.get("same_spatial_plan_retry_count"))
        or _handoff_to_int(history.get("same_spatial_plan_repeat_count"))
        or 0
    )
    spatial_interpretation_failure_reason = (
        str(
            spatial_context.get("spatial_interpretation_failure_reason")
            or spatial_context.get("fallback_reason")
            or ""
        ).strip()
        or None
    )
    target_intent_ignored = bool(
        target_option_received
        and spatial_interpretation_attempted
        and spatial_interpretation_status in {"failed", "fallback_default_bbox"}
        and runtime_spatial_strategy_used == "default_bbox_fallback"
    )
    bbox_candidate_missing_persistent = bool(
        target_option_received
        and bbox_candidate is None
        and repeated_attempt_context
        and non_empty_attempt_count <= 0
    )
    spatial_interpretation_failed = bool(
        target_option_received
        and spatial_interpretation_attempted
        and (
            spatial_interpretation_status in {"failed", "fallback_default_bbox", "invalid_explicit_bbox"}
            or target_intent_ignored
            or bbox_candidate_missing_persistent
        )
    )
    spatial_plan_reused_without_change = bool(
        target_option_received
        and repeated_attempt_context
        and (
            same_spatial_plan_retry_count >= 1
            or retry_changed_spatial_plan is False
        )
    )

    fallback_rescue_success: Optional[bool]
    if not fallback_used:
        fallback_rescue_success = None
    elif attempts:
        fallback_attempts = [
            a
            for a in attempts
            if bool(str(a.get("fallback_config_used") or "").strip())
            or bool(str(a.get("fallback_profile_used") or "").strip())
        ]
        if fallback_attempts:
            fallback_rescue_success = any(_attempt_status_success(a) and _attempt_non_empty(a) for a in fallback_attempts)
        else:
            fallback_rescue_success = bool(successful_attempt_count > 0 and non_empty_attempt_count > 0)
    else:
        fallback_rescue_success = bool(successful_attempt_count > 0 and non_empty_attempt_count > 0)

    best_attempt_index = _handoff_to_int(history.get("best_attempt_index"))
    time_to_best_attempt_ms = _cumulative_duration_ms_for_index(attempts, best_attempt_index)
    first_non_empty_idx: Optional[int] = None
    for a in attempts:
        if _attempt_non_empty(a):
            first_non_empty_idx = _handoff_to_int(a.get("attempt_no"))
            if first_non_empty_idx is not None and first_non_empty_idx > 0:
                break
    time_to_first_non_empty_ms = _cumulative_duration_ms_for_index(attempts, first_non_empty_idx)

    repeat_ratio = 0.0
    if attempt_count_total > 1:
        repeat_ratio = float(same_config_repeat_count) / float(max(1, attempt_count_total - 1))
    if repeat_ratio >= 0.60:
        repeat_risk = "high"
    elif repeat_ratio >= 0.25:
        repeat_risk = "medium"
    else:
        repeat_risk = "low"

    efficiency_health = compute_extractor_efficiency_health_score(
        attempt_count_total=attempt_count_total,
        retry_diversity_count=retry_diversity_count,
        same_config_repeat_count=same_config_repeat_count,
        non_empty_attempt_count=non_empty_attempt_count,
        successful_attempt_count=successful_attempt_count,
        fallback_used=fallback_used,
        fallback_rescue_success=fallback_rescue_success,
    )

    warnings: List[str] = []
    if missing_attempt_history:
        warnings.append("missing_attempt_history")
    if attempt_count_total >= 2 and non_empty_attempt_count <= 0:
        warnings.append("repeated_empty_extraction")
    if attempt_count_total > 1 and retry_diversity_count <= 1:
        warnings.append("retry_not_diversified")
    if fallback_used and fallback_rescue_success is False:
        warnings.append("fallback_rescue_failed")
    if attempt_count_total > 1 and same_config_repeat_count > 0:
        warnings.append("same_config_retry_loop")
    if spatial_interpretation_failed:
        warnings.append("spatial_interpretation_failed")
    if target_intent_ignored:
        warnings.append("target_intent_ignored")
    if spatial_plan_reused_without_change:
        warnings.append("spatial_plan_reused_without_change")

    compare_latest = _as_dict(compare.get("latest"))
    compare_latest_metrics = _as_dict(compare.get("latest_metrics"))
    regression_flags = _handoff_extract_regression_flags(compare)
    relevant_deltas = _extractor_relevant_deltas(compare)

    completion_quality_score: Optional[float] = None
    order_completion_quality_score: Optional[float] = None
    completion_metrics: Dict[str, Any] = {}

    if str(step.phase or "") == "phase1":
        candidate_count = _handoff_to_int(
            summary.get("candidate_count")
            if summary.get("candidate_count") is not None
            else evidence.get("candidate_count")
        )
        non_empty_extraction = bool((candidate_count or 0) > 0 or non_empty_attempt_count > 0)
        latest_chain = _as_dict(state.resume_context.get("latest_phase1_chain_summary"))
        latest_chain_resolve = _as_dict(latest_chain.get("resolve"))
        downstream_resolve_count = _handoff_to_int(
            evidence.get("resolved_count")
            if evidence.get("resolved_count") is not None
            else (
                latest_chain_resolve.get("resolved_count")
                if latest_chain_resolve.get("resolved_count") is not None
                else latest_chain_resolve.get("resolved_total")
            )
        )
        downstream_resolve_rate = None
        if downstream_resolve_count is not None and candidate_count is not None and candidate_count > 0:
            downstream_resolve_rate = max(0.0, min(1.0, float(downstream_resolve_count) / float(candidate_count)))

        phase1_quality_score = _status_norm_01(
            summary.get("quality_score")
            if summary.get("quality_score") is not None
            else (
                evidence.get("quality_score")
                if evidence.get("quality_score") is not None
                else compare_latest.get("quality_score")
            )
        )
        completion_quality_score = compute_completion_quality_score(
            phase="phase1",
            non_empty_extraction=non_empty_extraction,
            phase1_quality_score=phase1_quality_score,
            downstream_resolve_rate=downstream_resolve_rate,
        )
        if completion_quality_score is not None and completion_quality_score < 0.45:
            warnings.append("extractor_low_completion_usefulness")

        completion_metrics = {
            "candidate_count": candidate_count,
            "non_empty_extraction": bool(non_empty_extraction),
            "downstream_resolve_count": downstream_resolve_count,
            "downstream_resolve_rate": (round(float(downstream_resolve_rate), 4) if downstream_resolve_rate is not None else None),
            "phase1_quality_score": (round(float(phase1_quality_score), 4) if phase1_quality_score is not None else None),
            "completion_quality_score": (
                round(float(completion_quality_score), 4)
                if completion_quality_score is not None
                else None
            ),
        }

    if str(step.phase or "") == "phase2":
        runtime_context = _as_dict(summary.get("runtime_context"))
        handoff = _as_dict(summary.get(PHASE1_TO_PHASE2_HANDOFF_ARTIFACT))
        substep_timings_ms = _as_dict(summary.get("substep_timings_ms"))
        skipped_substeps = _as_dict(summary.get("skipped_substeps"))
        failed_stage = (
            str(summary.get("failed_stage") or evidence.get("failed_stage") or "").strip()
            or None
        )
        place_set_id = (
            str(runtime_context.get("place_set_id") or handoff.get("place_set_id") or "").strip()
            or None
        )
        expected_substeps = (
            "extract",
            "geo_context",
            "candidates",
            "name_candidates",
            "train_ranker",
            "embeddings",
            "reindex",
        )
        completed_substeps = len(
            [
                stage_name
                for stage_name in expected_substeps
                if stage_name in substep_timings_ms or stage_name in skipped_substeps
            ]
        )
        substep_completion_rate = (
            float(completed_substeps) / float(len(expected_substeps))
            if expected_substeps
            else None
        )
        completion_quality_score = compute_completion_quality_score(
            phase="phase2",
            semantic_place_set_present=bool(place_set_id),
            semantic_substep_completion_rate=substep_completion_rate,
            semantic_failed_stage=failed_stage,
        )
        if failed_stage:
            warnings.append("semantic_pipeline_failed")
        if completion_quality_score is not None and completion_quality_score < 0.45:
            warnings.append("extractor_low_completion_usefulness")
        completion_metrics = {
            "place_set_id": place_set_id,
            "failed_stage": failed_stage,
            "completed_substeps": int(completed_substeps),
            "expected_substeps": int(len(expected_substeps)),
            "substep_completion_rate": (
                round(float(substep_completion_rate), 4)
                if substep_completion_rate is not None
                else None
            ),
            "completion_quality_score": (
                round(float(completion_quality_score), 4)
                if completion_quality_score is not None
                else None
            ),
        }

    if str(step.phase or "") == "phase3":
        phase3_bundle = _phase3_route_bundle_evidence(
            summary=summary,
            payload=_as_dict(summary.get("validator_payload")),
            validator_evidence=evidence,
        )
        candidate_universe_summary = _as_dict(phase3_bundle.get("candidate_universe_summary"))
        selection_summary = _as_dict(phase3_bundle.get("selection_summary"))
        route_candidate_count = _handoff_to_int(phase3_bundle.get("candidate_universe_count"))
        actual_prior_stop_count = _handoff_to_int(phase3_bundle.get("actual_prior_stop_count"))
        prior_stop_count = _handoff_to_int(phase3_bundle.get("prior_stop_evidence_count"))
        selection_confidence = _handoff_to_float(phase3_bundle.get("selection_confidence"))
        selection_score_gap_top2 = _handoff_to_float(selection_summary.get("score_gap_top2"))
        query_strategy = (
            str(
                candidate_universe_summary.get("query_strategy")
                or summary.get("query_strategy")
                or evidence.get("query_strategy")
                or ""
            ).strip()
            or None
        )
        hard_filters_applied = list(candidate_universe_summary.get("hard_filters_applied") or [])
        soft_signals_used = list(candidate_universe_summary.get("soft_signals_used") or [])
        step20_ctx = {}
        if step.step_id == STEP_P3_2_STEP20:
            step20_ctx = dict(summary)
        else:
            step20_ctx = _as_dict(state.resume_context.get("latest_step20_summary"))
        matched_count = _handoff_to_int(
            step20_ctx.get("matched_count")
            if step20_ctx.get("matched_count") is not None
            else evidence.get("matched_count")
        )
        unmatched_count = _handoff_to_int(
            step20_ctx.get("unmatched_count")
            if step20_ctx.get("unmatched_count") is not None
            else evidence.get("unmatched_count")
        )
        ambiguous_count = _handoff_to_int(
            step20_ctx.get("ambiguous_count")
            if step20_ctx.get("ambiguous_count") is not None
            else evidence.get("ambiguous_count")
        )
        sequence_quality_score = _status_norm_100(
            step20_ctx.get("sequence_quality_score")
            if step20_ctx.get("sequence_quality_score") is not None
            else (
                evidence.get("sequence_quality_score")
                if evidence.get("sequence_quality_score") is not None
                else compare_latest_metrics.get("sequence_quality_score")
            )
        )
        step20_gate_passed = _handoff_to_bool(
            step20_ctx.get("sequence_gate_pass")
            if step20_ctx.get("sequence_gate_pass") is not None
            else evidence.get("sequence_gate_pass")
        )
        step20_available = bool(
            matched_count is not None
            or unmatched_count is not None
            or ambiguous_count is not None
            or sequence_quality_score is not None
            or step20_gate_passed is not None
        )
        reorder_recommended = _handoff_to_bool(reorder.get("recommended"))
        total_step20 = None
        if matched_count is not None or unmatched_count is not None or ambiguous_count is not None:
            total_step20 = int((matched_count or 0) + (unmatched_count or 0) + (ambiguous_count or 0))
        unmatched_ratio = None
        ambiguous_ratio = None
        if total_step20 is not None and total_step20 > 0:
            unmatched_ratio = float(unmatched_count or 0) / float(total_step20)
            ambiguous_ratio = float(ambiguous_count or 0) / float(total_step20)
        reorder_pressure = 0.0
        if reorder_recommended:
            reorder_pressure += 0.35
        if sequence_quality_score is not None and sequence_quality_score < 70.0:
            reorder_pressure += 0.35
        if unmatched_ratio is not None and unmatched_ratio > 0.15:
            reorder_pressure += 0.20
        if ambiguous_ratio is not None and ambiguous_ratio > 0.10:
            reorder_pressure += 0.10
        reorder_pressure = max(0.0, min(1.0, reorder_pressure))

        if step20_available:
            match_ratio = None
            if total_step20 is not None and total_step20 > 0 and matched_count is not None:
                match_ratio = float(matched_count) / float(total_step20)
            order_completion_quality_score = compute_order_completion_quality_score(
                step20_available=step20_available,
                sequence_quality_score=sequence_quality_score,
                matched_count=matched_count,
                unmatched_count=unmatched_count,
                ambiguous_count=ambiguous_count,
                step20_gate_passed=step20_gate_passed,
            )
        else:
            order_completion_quality_score = compute_order_completion_quality_score(
                step20_available=step20_available,
                route_candidate_count=route_candidate_count,
                prior_stop_count=prior_stop_count,
            )
        if order_completion_quality_score is not None and order_completion_quality_score < 0.45:
            warnings.append("extractor_low_completion_usefulness")
        fetch_status_classification = str(phase3_bundle.get("fetch_status_classification") or "").strip()
        if fetch_status_classification in P3_PARTIAL_FETCH_CLASSIFICATIONS:
            warnings.append(fetch_status_classification)

        if route_candidate_count is not None and int(route_candidate_count) < 5:
            warnings.append("candidate_universe_too_small")
        if selection_confidence is not None and selection_confidence < 0.45:
            warnings.append("selection_confidence_low")
        if (
            query_strategy == "metadata_filtered"
            and route_candidate_count is not None
            and int(route_candidate_count) < 5
        ):
            warnings.append("hard_filter_overreach")

        non_empty_extraction = bool(non_empty_attempt_count > 0 or (route_candidate_count or 0) > 0)
        if non_empty_extraction and step20_available:
            poor_step20 = (
                (step20_gate_passed is False)
                or (sequence_quality_score is not None and sequence_quality_score < 70.0)
                or (unmatched_ratio is not None and unmatched_ratio > 0.20)
            )
            if poor_step20:
                warnings.append("extraction_success_but_step20_poor")

        if any(
            code in {"quality_drop", "phase3_unmatched_increase", "phase3_ambiguous_increase", "sequence_quality_drop"}
            for code in regression_flags
        ):
            warnings.append("repeated_corridor_extractor_failure")

        completion_metrics = {
            "route_candidate_count": route_candidate_count,
            "candidate_universe_count": route_candidate_count,
            "actual_prior_stop_count": actual_prior_stop_count,
            "prior_stop_evidence_count": prior_stop_count,
            "strong_bundle_evidence": bool(phase3_bundle.get("strong_bundle_evidence")),
            "selected_relation_present": bool(phase3_bundle.get("selected_relation_present")),
            "selected_relation_stop_prior_count": _handoff_to_int(
                phase3_bundle.get("selected_relation_stop_prior_count")
            ),
            "top_stop_prior_count": _handoff_to_int(phase3_bundle.get("top_stop_prior_count")),
            "selection_confidence": (
                round(float(selection_confidence), 4)
                if selection_confidence is not None
                else None
            ),
            "selection_score_gap_top2": (
                round(float(selection_score_gap_top2), 4)
                if selection_score_gap_top2 is not None
                else None
            ),
            "query_strategy": query_strategy,
            "hard_filters_applied": hard_filters_applied[:8],
            "soft_signals_used": soft_signals_used[:8],
            "fetch_status_classification": fetch_status_classification or None,
            "bundle_success_classification": (
                str(phase3_bundle.get("bundle_success_classification") or "").strip() or None
            ),
            "bundle_contradiction_codes": list(phase3_bundle.get("contradiction_codes") or [])[:8],
            "step20_available": bool(step20_available),
            "matched_count": matched_count,
            "unmatched_count": unmatched_count,
            "ambiguous_count": ambiguous_count,
            "sequence_quality_score": (round(float(sequence_quality_score), 4) if sequence_quality_score is not None else None),
            "step20_gate_passed": step20_gate_passed,
            "reorder_pressure": round(float(reorder_pressure), 4),
            "order_completion_quality_score": (
                round(float(order_completion_quality_score), 4)
                if order_completion_quality_score is not None
                else None
            ),
        }

    notes: List[str] = []
    if time_to_best_attempt_ms is None:
        notes.append("time_to_best_attempt_ms_unavailable")
    if time_to_first_non_empty_ms is None:
        notes.append("time_to_first_non_empty_ms_unavailable")
    if missing_attempt_history:
        notes.append("sparse_attempt_history_missing")
    if repeated_attempt_context and missing_attempt_history:
        notes.append("retry_attempt_history_missing")
    if spatial_interpretation_failed and spatial_interpretation_failure_reason:
        notes.append(f"spatial_interpretation_failure:{spatial_interpretation_failure_reason}")

    warnings = validate_warnings(
        list(dict.fromkeys([str(x).strip() for x in warnings if str(x).strip()])),
        {
            "phase": str(step.phase or ""),
            "step_id": str(step.step_id or ""),
            "attempt_count_total": int(attempt_count_total),
            "same_config_repeat_count": int(same_config_repeat_count),
            "non_empty_attempt_count": int(non_empty_attempt_count),
            "target_option_received": bool(target_option_received),
            "failed_stage": _as_dict(completion_metrics).get("failed_stage"),
        },
    )

    return {
        "schema_version": "extractor_status_v1",
        "phase": str(step.phase or ""),
        "step_id": str(step.step_id or ""),
        "extractor_efficiency_health_score": round(float(efficiency_health), 4),
        "efficiency_metrics": {
            "attempt_count_total": int(attempt_count_total),
            "successful_attempt_count": int(successful_attempt_count),
            "retry_diversity_count": int(retry_diversity_count),
            "same_config_repeat_count": int(same_config_repeat_count),
            "fallback_used": bool(fallback_used),
            "fallback_rescue_success": fallback_rescue_success,
            "time_to_best_attempt_ms": time_to_best_attempt_ms,
            "time_to_first_non_empty_ms": time_to_first_non_empty_ms,
            "effective_config_repeat_risk": repeat_risk,
            "retry_strategies_attempted": retry_strategies_attempted[:8],
            "effective_config_fingerprints": fingerprints[:12],
            "non_empty_attempt_count": int(non_empty_attempt_count),
        },
        "completion_metrics": completion_metrics,
        "spatial_metrics": {
            "target_option_received": bool(target_option_received),
            "target_option_text": target_option_text,
            "spatial_interpretation_attempted": bool(spatial_interpretation_attempted),
            "spatial_interpretation_status": spatial_interpretation_status,
            "spatial_interpretation_source": spatial_interpretation_source,
            "bbox_candidate": bbox_candidate,
            "bbox_candidate_confidence": (
                round(float(bbox_candidate_confidence), 4)
                if bbox_candidate_confidence is not None
                else None
            ),
            "area_group_hint": area_group_hint,
            "sector_hint": sector_hint,
            "corridor_hint": corridor_hint,
            "runtime_bbox_used": runtime_bbox_used,
            "runtime_spatial_strategy_used": runtime_spatial_strategy_used,
            "retry_changed_spatial_plan": retry_changed_spatial_plan,
            "same_spatial_plan_retry_count": int(same_spatial_plan_retry_count),
            "spatial_interpretation_failure_reason": spatial_interpretation_failure_reason,
            "target_intent_ignored": bool(target_intent_ignored),
        },
        "scores": {
            "extractor_efficiency_health_score": round(float(efficiency_health), 4),
            "completion_quality_score": (
                round(float(completion_quality_score), 4)
                if completion_quality_score is not None
                else None
            ),
            "order_completion_quality_score": (
                round(float(order_completion_quality_score), 4)
                if order_completion_quality_score is not None
                else None
            ),
        },
        "warnings": warnings,
        "regression": {
            "history_count": int(compare.get("history_count") or 0),
            "regression_flags": regression_flags,
            "extractor_relevant_deltas": relevant_deltas,
        },
        "notes": notes[:8],
    }


def _build_extractor_help_needed(
    *,
    step: StepDefinition,
    extractor_status: Dict[str, Any],
    validator_result: ValidatorResult,
) -> Dict[str, Any]:
    status = dict(extractor_status or {})
    if not status:
        return {}

    phase = str(status.get("phase") or step.phase or "").strip().lower()
    if phase not in {"phase1", "phase2", "phase3"}:
        return {}

    eff = _as_dict(status.get("efficiency_metrics"))
    comp = _as_dict(status.get("completion_metrics"))
    spatial = _as_dict(status.get("spatial_metrics"))
    scores = _as_dict(status.get("scores"))
    warning_set = {str(x).strip() for x in list(status.get("warnings") or []) if str(x).strip()}

    attempt_count_total = _handoff_to_int(eff.get("attempt_count_total")) or 0
    same_config_repeat_count = _handoff_to_int(eff.get("same_config_repeat_count")) or 0
    retry_diversity_count = _handoff_to_int(eff.get("retry_diversity_count")) or 0
    fallback_rescue_success = _handoff_to_bool(eff.get("fallback_rescue_success"))
    step20_available = _handoff_to_bool(comp.get("step20_available"))
    if step20_available is None:
        step20_available = False

    efficiency_score = _status_norm_01(
        status.get("extractor_efficiency_health_score")
        if status.get("extractor_efficiency_health_score") is not None
        else scores.get("extractor_efficiency_health_score")
    )
    completion_quality_score = _status_norm_01(scores.get("completion_quality_score"))
    order_completion_quality_score = _status_norm_01(scores.get("order_completion_quality_score"))
    step20_gate_passed = _handoff_to_bool(comp.get("step20_gate_passed"))
    missing_attempt_history = bool(
        ("missing_attempt_history" in warning_set)
        or int(attempt_count_total) <= 0
    )
    target_option_received = bool(
        spatial.get("target_option_received")
        or str(spatial.get("target_option_text") or "").strip()
    )
    spatial_interpretation_status = str(spatial.get("spatial_interpretation_status") or "").strip() or None
    bbox_candidate_confidence = _handoff_to_float(spatial.get("bbox_candidate_confidence"))
    same_spatial_plan_retry_count = _handoff_to_int(spatial.get("same_spatial_plan_retry_count")) or 0
    retry_changed_spatial_plan = _handoff_to_bool(spatial.get("retry_changed_spatial_plan"))
    spatial_interpretation_failure_reason = (
        str(spatial.get("spatial_interpretation_failure_reason") or "").strip() or None
    )
    target_intent_ignored = bool(spatial.get("target_intent_ignored"))
    runtime_spatial_strategy_used = str(spatial.get("runtime_spatial_strategy_used") or "").strip() or None
    spatial_interpretation_failed = bool(
        "spatial_interpretation_failed" in warning_set
        or (
            target_option_received
            and spatial_interpretation_status in {"failed", "fallback_default_bbox", "invalid_explicit_bbox"}
        )
        or (
            target_option_received
            and not _as_dict(spatial.get("bbox_candidate"))
            and attempt_count_total >= 2
            and (completion_quality_score is None or completion_quality_score <= 0.50)
        )
    )
    spatial_plan_reused_without_change = bool(
        "spatial_plan_reused_without_change" in warning_set
        or (
            target_option_received
            and (
                same_spatial_plan_retry_count >= 1
                or (attempt_count_total >= 2 and retry_changed_spatial_plan is False)
            )
        )
    )

    if phase == "phase2":
        failed_stage = str(comp.get("failed_stage") or "").strip() or None
        place_set_id = str(comp.get("place_set_id") or "").strip() or None
        substep_completion_rate = _handoff_to_float(comp.get("substep_completion_rate"))
        partial_evidence = bool(missing_attempt_history or attempt_count_total < 2)
        reasons: List[Dict[str, Any]] = []
        if failed_stage:
            reasons.append(
                {
                    "code": "semantic_pipeline_failed",
                    "value": failed_stage,
                    "threshold": "no_failed_stage",
                    "message": "Semantic pipeline reported a failed substage.",
                    "severity": "high",
                }
            )
        elif completion_quality_score is not None and completion_quality_score <= 0.50:
            reasons.append(
                {
                    "code": "low_completion_quality",
                    "value": completion_quality_score,
                    "threshold": 0.50,
                    "message": "Semantic pipeline completion quality is below healthy threshold.",
                    "severity": "medium",
                }
            )
        if missing_attempt_history:
            reasons.append(
                {
                    "code": "attempt_history_gap",
                    "value": int(attempt_count_total),
                    "threshold": 2,
                    "message": "Attempt history is incomplete for this extraction-class semantic step.",
                    "severity": "medium",
                }
            )

        reason_priority = {"high": 3, "medium": 2, "low": 1}
        reasons_sorted = sorted(
            reasons,
            key=lambda row: (-int(reason_priority.get(str(row.get("severity") or "low"), 1)), str(row.get("code") or "")),
        )[:8]
        severity = "low"
        if any(str(row.get("severity") or "") == "high" for row in reasons_sorted):
            severity = "high"
        elif any(str(row.get("severity") or "") == "medium" for row in reasons_sorted):
            severity = "medium"
        needed = bool(reasons_sorted)
        confidence = 0.75 if failed_stage else 0.60
        if partial_evidence:
            confidence = min(confidence, 0.45)
        evidence_payload = {
            "extractor_efficiency_health_score": efficiency_score,
            "completion_quality_score": completion_quality_score,
            "order_completion_quality_score": order_completion_quality_score,
            "attempt_count_total": int(attempt_count_total),
            "same_config_repeat_count": int(same_config_repeat_count),
            "retry_diversity_count": int(retry_diversity_count),
            "failed_stage": failed_stage,
            "place_set_id": place_set_id,
            "substep_completion_rate": substep_completion_rate,
        }
        notes: List[str] = []
        if missing_attempt_history:
            notes.append("missing_attempt_history")
        if partial_evidence:
            notes.append("confidence_reduced_partial_evidence")
        return {
            "needed": bool(needed),
            "severity": severity,
            "confidence": round(float(confidence), 2),
            "reason_class": (
                str(reasons_sorted[0].get("code") or "")
                if reasons_sorted
                else ("none" if not needed else "unknown")
            ),
            "reasons": [
                {
                    "code": str(row.get("code") or ""),
                    "value": row.get("value"),
                    "threshold": row.get("threshold"),
                    "message": str(row.get("message") or ""),
                }
                for row in reasons_sorted
            ],
            "evidence": evidence_payload,
            "recommended_escalation": "interpreter_patch_evaluation",
            "partial_evidence": bool(partial_evidence),
            "notes": notes,
        }

    reasons: List[Dict[str, Any]] = []

    def _add_reason(
        *,
        code: str,
        value: Any,
        threshold: Any,
        message: str,
        severity: str,
    ) -> None:
        for row in reasons:
            if str(row.get("code") or "") == code:
                return
        reasons.append(
            {
                "code": code,
                "value": value,
                "threshold": threshold,
                "message": str(message or "").strip(),
                "severity": str(severity or "").strip() or "low",
            }
        )

    def _le(value: Optional[float], threshold: float) -> bool:
        return value is not None and float(value) <= float(threshold)

    high_triggered = False
    medium_triggered = False
    low_triggered = False
    severe_pre_step20 = False

    if phase == "phase1":
        repeated_empty = ("repeated_empty_extraction" in warning_set and attempt_count_total >= 2)
        if target_option_received and spatial_interpretation_failed:
            spatial_severity = "high" if (attempt_count_total >= 2 or _le(completion_quality_score, 0.50)) else "medium"
            _add_reason(
                code="spatial_interpretation_failed",
                value=(spatial_interpretation_status or spatial_interpretation_failure_reason or runtime_spatial_strategy_used),
                threshold=2 if spatial_severity == "high" else 1,
                message="Target intent was present but did not resolve into a usable spatial extraction plan.",
                severity=spatial_severity,
            )
            if spatial_severity == "high":
                high_triggered = True
            else:
                medium_triggered = True
        if target_option_received and target_intent_ignored:
            _add_reason(
                code="target_intent_ignored",
                value=(runtime_spatial_strategy_used or spatial_interpretation_status),
                threshold="explicit_target_intent",
                message="Runtime fell back to generic/default spatial parameters despite target intent.",
                severity="high",
            )
            high_triggered = True
        if target_option_received and spatial_plan_reused_without_change:
            spatial_retry_severity = "high" if attempt_count_total >= 2 else "medium"
            _add_reason(
                code="spatial_plan_reused_without_change",
                value=int(max(attempt_count_total, same_spatial_plan_retry_count + 1)),
                threshold=2,
                message="Retries reused the same ineffective spatial interpretation without meaningful change.",
                severity=spatial_retry_severity,
            )
            if spatial_retry_severity == "high":
                high_triggered = True
            else:
                medium_triggered = True
        if repeated_empty:
            _add_reason(
                code="repeated_empty_extraction",
                value=int(attempt_count_total),
                threshold=2,
                message="Repeated empty extraction attempts detected.",
                severity="high",
            )
            high_triggered = True
        if _le(efficiency_score, 0.30):
            _add_reason(
                code="low_efficiency_health",
                value=efficiency_score,
                threshold=0.30,
                message="Extractor efficiency health score is critically low.",
                severity="high",
            )
            high_triggered = True
        if _le(completion_quality_score, 0.35):
            _add_reason(
                code="low_completion_quality",
                value=completion_quality_score,
                threshold=0.35,
                message="Phase1 completion quality is critically low.",
                severity="high",
            )
            high_triggered = True

        if _le(efficiency_score, 0.50):
            _add_reason(
                code="low_efficiency_health",
                value=efficiency_score,
                threshold=0.50,
                message="Extractor efficiency health score is below medium threshold.",
                severity="medium",
            )
            medium_triggered = True
        if _le(completion_quality_score, 0.50):
            _add_reason(
                code="low_completion_quality",
                value=completion_quality_score,
                threshold=0.50,
                message="Phase1 completion quality is below medium threshold.",
                severity="medium",
            )
            medium_triggered = True
        if ("retry_not_diversified" in warning_set and same_config_repeat_count >= 2):
            _add_reason(
                code="retry_not_diversified",
                value=int(same_config_repeat_count),
                threshold=2,
                message="Retry diversity is low relative to repeated attempts.",
                severity="medium",
            )
            medium_triggered = True

        severe_or_empty = repeated_empty or _le(efficiency_score, 0.50) or _le(completion_quality_score, 0.50)
        if _le(efficiency_score, 0.65):
            _add_reason(
                code="efficiency_health_early_warning",
                value=efficiency_score,
                threshold=0.65,
                message="Extractor efficiency is trending weaker than healthy baseline.",
                severity="low",
            )
            low_triggered = True
        if ("same_config_retry_loop" in warning_set and not severe_or_empty):
            _add_reason(
                code="same_config_retry_loop",
                value=int(same_config_repeat_count),
                threshold=1,
                message="Same configuration is being retried without severe failure evidence.",
                severity="low",
            )
            low_triggered = True

    elif phase == "phase3":
        candidate_universe_count = _handoff_to_int(
            comp.get("candidate_universe_count")
            if comp.get("candidate_universe_count") is not None
            else comp.get("route_candidate_count")
        ) or 0
        selection_confidence = _handoff_to_float(comp.get("selection_confidence"))
        query_strategy = str(comp.get("query_strategy") or "").strip() or None
        strong_bundle_evidence = bool(comp.get("strong_bundle_evidence"))
        fetch_status_classification = str(comp.get("fetch_status_classification") or "").strip()
        bundle_fetch_gap = bool(
            not step20_available
            and strong_bundle_evidence
            and fetch_status_classification in P3_PARTIAL_FETCH_CLASSIFICATIONS
        )
        if step20_available:
            if candidate_universe_count > 0 and candidate_universe_count < 5:
                _add_reason(
                    code="candidate_universe_too_small",
                    value=int(candidate_universe_count),
                    threshold=5,
                    message="Phase3 discovery candidate universe is too small for reliable downstream discrimination.",
                    severity="high" if not step20_gate_passed else "medium",
                )
                medium_triggered = True
            if query_strategy == "metadata_filtered" and candidate_universe_count < 5:
                _add_reason(
                    code="hard_filter_overreach",
                    value=int(candidate_universe_count),
                    threshold=5,
                    message="Phase3 discovery relied on narrow metadata filters and produced too few candidates.",
                    severity="medium",
                )
                medium_triggered = True
            if selection_confidence is not None and selection_confidence < 0.45:
                _add_reason(
                    code="selection_confidence_low",
                    value=selection_confidence,
                    threshold=0.45,
                    message="Selected relation confidence is weak relative to the candidate universe.",
                    severity="medium",
                )
                medium_triggered = True
            if _le(order_completion_quality_score, 0.40):
                _add_reason(
                    code="low_order_completion_quality",
                    value=order_completion_quality_score,
                    threshold=0.40,
                    message="Order completion quality is critically low with Step20 evidence.",
                    severity="high",
                )
                high_triggered = True
            if ("extraction_success_but_step20_poor" in warning_set and step20_gate_passed is False):
                _add_reason(
                    code="extraction_success_but_step20_poor",
                    value=bool(step20_gate_passed),
                    threshold=False,
                    message="Extraction succeeded but Step20 quality/gate outcome is poor.",
                    severity="high",
                )
                high_triggered = True

            if _le(order_completion_quality_score, 0.55):
                _add_reason(
                    code="low_order_completion_quality",
                    value=order_completion_quality_score,
                    threshold=0.55,
                    message="Order completion quality is below medium threshold.",
                    severity="medium",
                )
                medium_triggered = True
            if _le(efficiency_score, 0.45):
                _add_reason(
                    code="low_efficiency_health",
                    value=efficiency_score,
                    threshold=0.45,
                    message="Extractor efficiency is low for route extraction context.",
                    severity="medium",
                )
                medium_triggered = True

            if "repeated_corridor_extractor_failure" in warning_set and not high_triggered and not medium_triggered:
                _add_reason(
                    code="repeated_corridor_extractor_failure",
                    value=int(_handoff_to_int(_as_dict(status.get("regression")).get("history_count")) or 0),
                    threshold=1,
                    message="Repeated corridor-level extractor weakness is detected.",
                    severity="low",
                )
                low_triggered = True
            if ({"retry_not_diversified", "same_config_retry_loop"} & warning_set) and not high_triggered:
                _add_reason(
                    code="retry_not_diversified",
                    value=int(same_config_repeat_count),
                    threshold=2,
                    message="Retry diversity remains weak for route extraction attempts.",
                    severity="low",
                )
                low_triggered = True
        else:
            severe_pre_step20 = bool(
                not bundle_fetch_gap
                and
                "repeated_empty_extraction" in warning_set
                and attempt_count_total >= 3
                and _le(efficiency_score, 0.30)
            )
            if candidate_universe_count > 0 and candidate_universe_count < 5:
                _add_reason(
                    code="candidate_universe_too_small",
                    value=int(candidate_universe_count),
                    threshold=5,
                    message="Pre-Step20 Phase3 discovery candidate universe is too small.",
                    severity="high" if severe_pre_step20 else "medium",
                )
                medium_triggered = True
            if query_strategy == "metadata_filtered" and candidate_universe_count < 5:
                _add_reason(
                    code="hard_filter_overreach",
                    value=int(candidate_universe_count),
                    threshold=5,
                    message="Pre-Step20 Phase3 discovery over-relied on metadata filters and constrained coverage.",
                    severity="medium",
                )
                medium_triggered = True
            if selection_confidence is not None and selection_confidence < 0.45:
                _add_reason(
                    code="selection_confidence_low",
                    value=selection_confidence,
                    threshold=0.45,
                    message="Selected relation confidence is weak before Step20 validation.",
                    severity="medium",
                )
                medium_triggered = True
            if severe_pre_step20:
                _add_reason(
                    code="repeated_empty_extraction",
                    value=int(attempt_count_total),
                    threshold=3,
                    message="Repeated empty extraction with low efficiency before Step20 availability.",
                    severity="high",
                )
                high_triggered = True
            if _le(efficiency_score, 0.45):
                _add_reason(
                    code="low_efficiency_health",
                    value=efficiency_score,
                    threshold=0.45,
                    message="Pre-Step20 extractor efficiency is below medium threshold.",
                    severity="medium",
                )
                medium_triggered = True
            if ("retry_not_diversified" in warning_set and same_config_repeat_count >= 2):
                _add_reason(
                    code="retry_not_diversified",
                    value=int(same_config_repeat_count),
                    threshold=2,
                    message="Pre-Step20 retries are not diversified.",
                    severity="medium",
                )
                medium_triggered = True
            if not severe_pre_step20 and not bundle_fetch_gap:
                if _le(efficiency_score, 0.65) or ("same_config_retry_loop" in warning_set):
                    _add_reason(
                        code="partial_evidence_extractor_struggle",
                        value=efficiency_score,
                        threshold=0.65,
                        message="Extractor shows weak signals before Step20 evidence is available.",
                        severity="low",
                    )
                    low_triggered = True

    severity = "low"
    needed = False
    if high_triggered:
        severity = "high"
        needed = True
    elif medium_triggered:
        severity = "medium"
        needed = True
    elif low_triggered:
        severity = "low"
        needed = True

    timing_missing_note = any(str(x).startswith("time_to_") for x in list(status.get("notes") or []))
    sparse_attempt_history = attempt_count_total < 2
    p1_downstream_missing = bool(
        phase == "phase1"
        and _as_dict(status.get("completion_metrics")).get("downstream_resolve_count") is None
    )
    p3_step20_missing = bool(phase == "phase3" and not step20_available)
    partial_evidence = bool(
        (attempt_count_total >= 2 and timing_missing_note)
        or p1_downstream_missing
        or p3_step20_missing
        or sparse_attempt_history
        or missing_attempt_history
    )

    notes: List[str] = []
    if timing_missing_note:
        notes.append("timing_fields_missing")
    if p1_downstream_missing:
        notes.append("phase1_downstream_completion_missing")
    if p3_step20_missing:
        notes.append("step20_unavailable_partial_evidence")
    if sparse_attempt_history:
        notes.append("sparse_attempt_history")
    if missing_attempt_history:
        notes.append("missing_attempt_history")

    if phase == "phase3" and not step20_available and severity == "high" and not severe_pre_step20:
        severity = "medium"
        if needed:
            notes.append("pre_step20_severity_cap_applied")

    core_metrics_available = bool(
        efficiency_score is not None
        and attempt_count_total >= 1
        and retry_diversity_count >= 0
    )
    confidence = 0.40
    if not partial_evidence:
        if attempt_count_total >= 2 and core_metrics_available and (phase != "phase3" or step20_available):
            confidence = 0.85
        elif core_metrics_available:
            confidence = 0.65
    else:
        if core_metrics_available and attempt_count_total >= 2 and (phase != "phase3" or step20_available):
            confidence = 0.65
        else:
            confidence = 0.40
        notes.append("confidence_reduced_partial_evidence")
    if missing_attempt_history:
        confidence = min(float(confidence), 0.35)
        if "confidence_capped_missing_attempt_history" not in notes:
            notes.append("confidence_capped_missing_attempt_history")

    reason_priority = {"high": 3, "medium": 2, "low": 1}
    reason_code_priority = {
        "target_intent_ignored": 110,
        "spatial_plan_reused_without_change": 105,
        "spatial_interpretation_failed": 102,
        "repeated_empty_extraction": 100,
        "extraction_success_but_step20_poor": 95,
        "low_order_completion_quality": 90,
        "fallback_rescue_failed": 85,
        "retry_not_diversified": 80,
        "same_config_retry_loop": 75,
        "low_completion_quality": 70,
        "low_efficiency_health": 65,
        "partial_evidence_extractor_struggle": 60,
        "efficiency_health_early_warning": 55,
        "repeated_corridor_extractor_failure": 50,
    }
    reasons_sorted = sorted(
        reasons,
        key=lambda r: (
            -int(reason_priority.get(str(r.get("severity") or "low"), 1)),
            -int(reason_code_priority.get(str(r.get("code") or ""), 0)),
            str(r.get("code") or ""),
        ),
    )[:8]
    dominant_code = str((reasons_sorted[0].get("code") if reasons_sorted else "") or "")
    reason_class_map = {
        "target_intent_ignored": "target_intent_ignored",
        "spatial_plan_reused_without_change": "spatial_plan_reused_without_change",
        "spatial_interpretation_failed": "spatial_interpretation_failed",
        "repeated_empty_extraction": "repeated_empty_extraction",
        "retry_not_diversified": "retry_not_diversified",
        "same_config_retry_loop": "retry_not_diversified",
        "fallback_rescue_failed": "fallback_rescue_failed",
        "low_completion_quality": "low_completion_quality",
        "low_order_completion_quality": "low_order_completion_quality",
        "partial_evidence_extractor_struggle": "partial_evidence_extractor_struggle",
        "extraction_success_but_step20_poor": "low_order_completion_quality",
        "low_efficiency_health": "low_efficiency_health",
        "efficiency_health_early_warning": "low_efficiency_health",
    }
    reason_class = reason_class_map.get(dominant_code, (dominant_code or ("none" if not needed else "unknown")))

    evidence_payload = {
        "extractor_efficiency_health_score": efficiency_score,
        "completion_quality_score": completion_quality_score,
        "order_completion_quality_score": order_completion_quality_score,
        "attempt_count_total": int(attempt_count_total),
        "same_config_repeat_count": int(same_config_repeat_count),
        "retry_diversity_count": int(retry_diversity_count),
        "target_option_received": bool(target_option_received),
        "spatial_interpretation_status": spatial_interpretation_status,
        "bbox_candidate_confidence": bbox_candidate_confidence,
        "same_spatial_plan_retry_count": int(same_spatial_plan_retry_count),
        "retry_changed_spatial_plan": retry_changed_spatial_plan,
        "runtime_spatial_strategy_used": runtime_spatial_strategy_used,
        "fallback_rescue_success": fallback_rescue_success,
        "step20_available": bool(step20_available),
        "step20_gate_passed": step20_gate_passed,
    }
    evidence_payload["extractor_patch_escalation_triggered"] = bool(
        target_option_received
        and any(
            str(_as_dict(r).get("code") or "") in {
                "spatial_interpretation_failed",
                "spatial_plan_reused_without_change",
                "target_intent_ignored",
            }
            for r in reasons_sorted
        )
    )

    return {
        "needed": bool(needed),
        "severity": str(severity),
        "confidence": round(float(confidence), 2),
        "reason_class": str(reason_class),
        "reasons": [
            {
                "code": str(r.get("code") or ""),
                "value": r.get("value"),
                "threshold": r.get("threshold"),
                "message": str(r.get("message") or ""),
            }
            for r in reasons_sorted
        ],
        "evidence": evidence_payload,
        "recommended_escalation": "interpreter_patch_evaluation",
        "partial_evidence": bool(partial_evidence),
        "notes": list(dict.fromkeys([str(x).strip() for x in notes if str(x).strip()]))[:8],
    }


def build_default_pipeline_step_registry() -> Dict[str, StepDefinition]:
    rules_step20 = {
        BlockReasonCode.STEP20_UNMATCHED_BLOCKING: DiversionRule(
            block_reason_code=BlockReasonCode.STEP20_UNMATCHED_BLOCKING,
            target_step_id=STEP_P1_3B_NEW_NODES,
            approval_type=ApprovalType.RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS,
            resume_step_id=STEP_P3_2_STEP20,
            optional_phase2_partial_rerun=True,
        ),
        BlockReasonCode.STEP20_AMBIGUOUS_BLOCKING: DiversionRule(
            block_reason_code=BlockReasonCode.STEP20_AMBIGUOUS_BLOCKING,
            target_step_id=STEP_P1_3B_NEW_NODES,
            approval_type=ApprovalType.RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS,
            resume_step_id=STEP_P3_2_STEP20,
            optional_phase2_partial_rerun=True,
        ),
        BlockReasonCode.STEP20_COVERAGE_GAP_BLOCKING: DiversionRule(
            block_reason_code=BlockReasonCode.STEP20_COVERAGE_GAP_BLOCKING,
            target_step_id=STEP_P1_3B_NEW_NODES,
            approval_type=ApprovalType.RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS,
            resume_step_id=STEP_P3_2_STEP20,
            optional_phase2_partial_rerun=True,
        ),
        BlockReasonCode.STEP20_SYNTHETIC_TERMINUS_ONLY: DiversionRule(
            block_reason_code=BlockReasonCode.STEP20_SYNTHETIC_TERMINUS_ONLY,
            target_step_id=STEP_P1_3B_NEW_NODES,
            approval_type=ApprovalType.RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS,
            resume_step_id=STEP_P3_2_STEP20,
            optional_phase2_partial_rerun=True,
        ),
    }

    return {
        STEP_P1_1_EXTRACT: StepDefinition(
            phase="phase1",
            step_id=STEP_P1_1_EXTRACT,
            name="P1.1 Extract Build Node Set",
            automation_level=AutomationLevel.AUTO_WITH_CHECKPOINT,
            executor="phase1_extract",
            validator="phase1_extract",
            ai_bot_hooks=("phase1_extract",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly", "failed"),
            retry_policy=RetryPolicy(
                enabled=True,
                max_attempts=3,
                strategies=("bbox_expand", "template_alternative", "fallback_config"),
                retry_on_codes=(
                    BlockReasonCode.EXTRACTION_EMPTY,
                    BlockReasonCode.EXTRACTION_LOW_COVERAGE,
                    BlockReasonCode.REPEATED_EXTRACTOR_FAILURE,
                ),
            ),
            pause_conditions=(
                "extraction_empty",
                "critically_low_coverage",
                "repeated_extractor_failures",
                "strong_regression",
            ),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("node_set_id", "extraction_metrics"),
            approval_type=None,
            next_step_on_success=STEP_P1_2_CHAIN,
            diversion_rules={},
        ),
        STEP_P1_2_CHAIN: StepDefinition(
            phase="phase1",
            step_id=STEP_P1_2_CHAIN,
            name="P1.2 Normalize Features Cluster Resolve",
            automation_level=AutomationLevel.AUTO_SAFE,
            executor="phase1_chain",
            validator="phase1_chain",
            ai_bot_hooks=("phase1_chain",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly", "failed"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=(
                "normalize_failed",
                "features_invalid",
                "clustering_degenerate",
                "resolve_zero_results",
                "critical_score_collapse",
            ),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("resolve_summary", "cluster_metrics"),
            approval_type=None,
            next_step_on_success=STEP_P1_3A_WORKSPACE,
            diversion_rules={},
        ),
        STEP_P1_3A_WORKSPACE: StepDefinition(
            phase="phase1",
            step_id=STEP_P1_3A_WORKSPACE,
            name="P1.3a Workspace Review",
            automation_level=AutomationLevel.MANUAL_ASSISTED,
            executor="phase1_workspace_review",
            validator="phase1_workspace_review",
            ai_bot_hooks=("phase1_workspace_review",),
            chatgpt_interpretation_triggers=("warning", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("manual_review_required",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("review_priority_groups",),
            approval_type=None,
            next_step_on_success=STEP_P1_4_PROMOTE,
            diversion_rules={},
        ),
        STEP_P1_3B_NEW_NODES: StepDefinition(
            phase="phase1",
            step_id=STEP_P1_3B_NEW_NODES,
            name="P1.3b New Nodes Resolution",
            automation_level=AutomationLevel.MANUAL_ASSISTED,
            executor="phase1_new_nodes_prefill",
            validator="phase1_new_nodes_prefill",
            ai_bot_hooks=("phase1_new_nodes_prefill",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("human_resolution_required",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("create_node_suggestions", "ambiguity_candidates"),
            approval_type=None,
            next_step_on_success=STEP_P1_4_PROMOTE,
            diversion_rules={},
        ),
        STEP_P1_4_PROMOTE: StepDefinition(
            phase="phase1",
            step_id=STEP_P1_4_PROMOTE,
            name="P1.4 Approve and Promote",
            automation_level=AutomationLevel.APPROVAL_REQUIRED,
            executor="phase1_promote_prepare",
            validator="phase1_promote_prepare",
            ai_bot_hooks=("phase1_promote_prepare",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("promote_requires_approval",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("promote_dry_run",),
            approval_type=ApprovalType.PROMOTE_NODE_BATCH,
            next_step_on_success=STEP_P2_1_SEMANTIC,
            diversion_rules={},
            approval_apply_executor="phase1_promote_apply",
        ),
        STEP_P2_1_SEMANTIC: StepDefinition(
            phase="phase2",
            step_id=STEP_P2_1_SEMANTIC,
            name="P2.1 Semantic Pipeline Run",
            automation_level=AutomationLevel.AUTO_WITH_CHECKPOINT,
            executor="phase2_semantic_pipeline",
            validator="phase2_semantic_pipeline",
            ai_bot_hooks=("phase2_semantic_pipeline",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly", "failed"),
            retry_policy=RetryPolicy(enabled=True, max_attempts=2, strategies=("fallback_config",)),
            pause_conditions=(
                "semantic_pipeline_failed",
                "semantic_regression_high",
                "critical_db_inconsistency",
            ),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("semantic_metrics", "semantic_compare_report"),
            approval_type=None,
            next_step_on_success=STEP_P2_2_WORKSPACE,
            diversion_rules={},
        ),
        STEP_P2_2_WORKSPACE: StepDefinition(
            phase="phase2",
            step_id=STEP_P2_2_WORKSPACE,
            name="P2.2 Semantics Workspace Review",
            automation_level=AutomationLevel.MANUAL_ASSISTED,
            executor="phase2_semantic_workspace_review",
            validator="phase2_semantic_workspace_review",
            ai_bot_hooks=("phase2_semantic_workspace_review",),
            chatgpt_interpretation_triggers=("warning", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("manual_review_required",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("semantics_priority_groups",),
            approval_type=None,
            next_step_on_success=STEP_P2_3_CLEANUP,
            diversion_rules={},
        ),
        STEP_P2_3_CLEANUP: StepDefinition(
            phase="phase2",
            step_id=STEP_P2_3_CLEANUP,
            name="P2.3 Cleanup Dedup Global Normalize",
            automation_level=AutomationLevel.APPROVAL_REQUIRED,
            executor="phase2_cleanup_preview",
            validator="phase2_cleanup_preview",
            ai_bot_hooks=("phase2_cleanup_preview",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("destructive_cleanup_requires_approval",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("cleanup_impact_analysis",),
            approval_type=ApprovalType.RUN_DESTRUCTIVE_CLEANUP,
            next_step_on_success=STEP_P3_1_EXTRACT,
            diversion_rules={},
            approval_apply_executor="phase2_cleanup_apply",
            approval_action=ACTION_DESTRUCTIVE_CLEANUP_EXECUTE,
        ),
        # Canonical Phase 3 route flow starts here and must pass through
        # inverse completion before Step 20 or any later downstream work.
        STEP_P3_1_EXTRACT: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_1_EXTRACT,
            name="P3.1 Route Extraction and Broad Candidate Context",
            automation_level=AutomationLevel.AUTO_WITH_CHECKPOINT,
            executor="phase3_route_extract",
            validator="phase3_route_extract",
            ai_bot_hooks=("phase3_route_extract",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly", "failed"),
            retry_policy=RetryPolicy(
                enabled=True,
                max_attempts=2,
                strategies=("template_alternative", "fallback_config"),
                retry_on_codes=(
                    BlockReasonCode.EXTRACTION_EMPTY,
                    BlockReasonCode.REPEATED_EXTRACTOR_FAILURE,
                ),
            ),
            pause_conditions=("low_evidence", "very_low_score", "repeated_failures"),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("route_extract_summary", "candidate_context"),
            approval_type=None,
            next_step_on_success=STEP_P3_15_INVERSE,
            diversion_rules={},
        ),
        STEP_P3_15_INVERSE: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_15_INVERSE,
            name="P3.15 Inverse Completion (required before Step20)",
            automation_level=AutomationLevel.AUTO_WITH_CHECKPOINT,
            executor="phase3_inverse_completion",
            validator="phase3_inverse_completion",
            ai_bot_hooks=("phase3_inverse_completion",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("inverse_completion_blocked",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("inverse_completion_state", "inverse_completion_search"),
            approval_type=None,
            next_step_on_success=STEP_P3_2_STEP20,
            diversion_rules={},
        ),
        STEP_P3_2_STEP20: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_2_STEP20,
            name="P3.2 Sequence Step20 (direction-ready only)",
            automation_level=AutomationLevel.AUTO_WITH_CHECKPOINT,
            executor="phase3_step20_sequence",
            validator="phase3_step20_sequence",
            ai_bot_hooks=("phase3_step20_sequence",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly", "failed"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=(
                "unmatched_blocking",
                "ambiguous_blocking",
                "critical_sequence_quality_low",
                "coverage_gap_blocking",
                "synthetic_terminus_only",
            ),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("step20_match_report", "sequence_quality_report", "reorder_proposal", "backfill_candidates"),
            approval_type=None,
            next_step_on_success=STEP_P3_4_STEP30,
            diversion_rules=rules_step20,
        ),
        STEP_P3_3_REORDER: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_3_REORDER,
            name="P3.3 Canonical Sequence Approval",
            automation_level=AutomationLevel.APPROVAL_REQUIRED,
            executor="phase3_reorder_proposal",
            validator="phase3_reorder_proposal",
            ai_bot_hooks=("phase3_reorder_proposal",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("reorder_apply_requires_approval",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("reorder_proposal",),
            approval_type=ApprovalType.APPLY_REORDER_PROPOSAL,
            next_step_on_success=STEP_P3_4_STEP30,
            diversion_rules={},
            approval_apply_executor="phase3_reorder_apply",
            approval_action=ACTION_SEQUENCE_REORDER_APPLY,
        ),
        STEP_P3_4_STEP30: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_4_STEP30,
            name="P3.4 Step30 Geometry",
            automation_level=AutomationLevel.AUTO_WITH_CHECKPOINT,
            executor="phase3_step30_geometry",
            validator="phase3_step30_geometry",
            ai_bot_hooks=("phase3_step30_geometry",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly", "failed"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("geometry_failed", "geometry_quality_critical_low"),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("geometry_set", "geometry_metrics"),
            approval_type=None,
            next_step_on_success=STEP_P3_4_STEP32,
            diversion_rules={},
        ),
        STEP_P3_4_STEP32: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_4_STEP32,
            name="P3.4 Step32 Geometry Stop Recovery",
            automation_level=AutomationLevel.AUTO_WITH_CHECKPOINT,
            executor="phase3_step32_stop_recovery",
            validator="phase3_step32_stop_recovery",
            ai_bot_hooks=("phase3_step32_stop_recovery",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly", "failed"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("stop_recovery_failed",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("geometry_stop_recovery",),
            approval_type=None,
            next_step_on_success=STEP_P3_4_STEP35,
            diversion_rules={},
        ),
        STEP_P3_4_STEP35: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_4_STEP35,
            name="P3.4 Step35 Rank",
            automation_level=AutomationLevel.AUTO_SAFE,
            executor="phase3_step35_rank",
            validator="phase3_step35_rank",
            ai_bot_hooks=("phase3_step35_rank",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly", "failed"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("ranking_failed",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("ranking_report",),
            approval_type=None,
            next_step_on_success=STEP_P3_4_STEP40,
            diversion_rules={},
        ),
        STEP_P3_4_STEP40: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_4_STEP40,
            name="P3.4 Step40 Approve",
            automation_level=AutomationLevel.APPROVAL_REQUIRED,
            executor="phase3_step40_approve_prepare",
            validator="phase3_step40_approve_prepare",
            ai_bot_hooks=("phase3_step40_approve_prepare",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("final_approval_required",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("route_approval_summary",),
            approval_type=ApprovalType.APPROVE_FINAL_ROUTE_OR_MERGE_BIND,
            next_step_on_success=STEP_P3_6_CATALOG,
            diversion_rules={},
            approval_apply_executor="phase3_step40_approve_apply",
        ),
        # Legacy/non-default compatibility stage. Opposite-direction completion
        # is now handled canonically by P3.15 before Step 20.
        STEP_P3_5_MERGE: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_5_MERGE,
            name="P3.5 Legacy Merge Opposite Direction Review (non-default)",
            automation_level=AutomationLevel.APPROVAL_REQUIRED,
            executor="phase3_merge_proposal",
            validator="phase3_merge_proposal",
            ai_bot_hooks=("phase3_merge_proposal",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("merge_bind_requires_approval",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("merge_evidence", "merge_scores"),
            approval_type=ApprovalType.APPROVE_FINAL_ROUTE_OR_MERGE_BIND,
            next_step_on_success=STEP_P3_6_CATALOG,
            diversion_rules={},
            approval_apply_executor="phase3_merge_apply",
            approval_action=ACTION_MERGE_BIND,
        ),
        STEP_P3_6_CATALOG: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_6_CATALOG,
            name="P3.6 Global Catalog Sync Review",
            automation_level=AutomationLevel.AUTO_SAFE,
            executor="phase3_catalog_sync_review",
            validator="phase3_catalog_sync_review",
            ai_bot_hooks=("phase3_catalog_sync_review",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("catalog_empty",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("phase3_global_catalog", "phase3_catalog_summary"),
            approval_type=None,
            next_step_on_success=STEP_P3_7_SECTOR,
            diversion_rules={},
        ),
        STEP_P3_7_SECTOR: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_7_SECTOR,
            name="P3.7 Sector Coverage Review",
            automation_level=AutomationLevel.MANUAL_ASSISTED,
            executor="phase3_sector_coverage_review",
            validator="phase3_sector_coverage_review",
            ai_bot_hooks=("phase3_sector_coverage_review",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("manual_review_required",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("phase3_sector_coverage",),
            approval_type=None,
            next_step_on_success=STEP_P3_8_GAPS,
            diversion_rules={},
        ),
        STEP_P3_8_GAPS: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_8_GAPS,
            name="P3.8 Missing Route Detection and Classification",
            automation_level=AutomationLevel.AUTO_WITH_CHECKPOINT,
            executor="phase3_gap_detection_classification",
            validator="phase3_gap_detection_classification",
            ai_bot_hooks=("phase3_gap_detection_classification",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("coverage_gap_sync_failed",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("phase3_coverage_gaps",),
            approval_type=None,
            next_step_on_success=STEP_P3_9_EXPORT,
            diversion_rules={},
        ),
        STEP_P3_9_EXPORT: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_9_EXPORT,
            name="P3.9 Missing Route Catalog Export",
            automation_level=AutomationLevel.AUTO_SAFE,
            executor="phase3_missing_route_export",
            validator="phase3_missing_route_export",
            ai_bot_hooks=("phase3_missing_route_export",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("missing_route_export_empty",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("phase3_missing_route_catalogs",),
            approval_type=None,
            next_step_on_success=STEP_P3_10_RESOLUTION,
            diversion_rules={},
        ),
        STEP_P3_10_RESOLUTION: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_10_RESOLUTION,
            name="P3.10 Gap Resolution Queue",
            automation_level=AutomationLevel.MANUAL_ASSISTED,
            executor="phase3_gap_resolution_queue",
            validator="phase3_gap_resolution_queue",
            ai_bot_hooks=("phase3_gap_resolution_queue",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("manual_review_required",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("phase3_gap_resolution_queue",),
            approval_type=None,
            next_step_on_success=STEP_P3_11_VERIFY,
            diversion_rules={},
        ),
        STEP_P3_11_VERIFY: StepDefinition(
            phase="phase3",
            step_id=STEP_P3_11_VERIFY,
            name="P3.11 Gap Resolution Verification",
            automation_level=AutomationLevel.AUTO_WITH_CHECKPOINT,
            executor="phase3_gap_resolution_verify",
            validator="phase3_gap_resolution_verify",
            ai_bot_hooks=("phase3_gap_resolution_verify",),
            chatgpt_interpretation_triggers=("warning", "blocked", "anomaly"),
            retry_policy=RetryPolicy(enabled=False, max_attempts=1),
            pause_conditions=("manual_gap_work_remaining",),
            resume_behavior=ResumeBehavior(mode="continue"),
            artifacts_expected=("phase3_gap_resolution_status",),
            approval_type=None,
            next_step_on_success=None,
            diversion_rules={},
        ),
    }


def _normalize_profile(profile: str) -> str:
    """Delegate to policy_engine canonical implementation (handles aliases like cautious→conservative)."""
    return _normalize_profile_canonical(profile)


def _policy(profile: str) -> PolicySettings:
    return POLICY_SETTINGS[_normalize_profile(profile)]


def _default_operator_roles() -> List[str]:
    raw = str(os.getenv("HADES_AUTOPILOT_ALLOWED_OPERATOR_ROLES", "admin,editor,operator")).strip()
    out = [x.strip().lower() for x in raw.split(",") if x.strip()]
    return out or ["admin", "editor", "operator"]


class SafeRetryEngine:
    def choose_retry(
        self,
        *,
        step: StepDefinition,
        profile: str,
        attempt_no: int,
        validator_result: ValidatorResult,
        strategy_order: Optional[Sequence[str]] = None,
        max_attempts_override: Optional[int] = None,
    ) -> Optional[RetryDecision]:
        cfg = step.retry_policy
        if not cfg.enabled:
            return None

        if max_attempts_override is None:
            max_attempts = int(cfg.max_attempts) + int(_policy(profile).max_retry_bonus)
        else:
            max_attempts = int(max_attempts_override)
        max_attempts = max(1, max_attempts)
        if attempt_no >= max_attempts:
            return None

        if validator_result.status not in {"blocked", "failed", "warning"}:
            return None

        allowed_codes = set(cfg.retry_on_codes or [])
        if allowed_codes and validator_result.block_reason_code not in allowed_codes:
            return None

        strategies = [str(x).strip() for x in list(strategy_order or []) if str(x).strip()]
        if not strategies:
            strategies = list(cfg.strategies or [])
        if not strategies:
            return None

        strategy_idx = min(max(0, attempt_no - 1), len(strategies) - 1)
        strategy = str(strategies[strategy_idx])
        param_delta = self._parameter_delta(strategy, attempt_no)
        reason = validator_result.summary or f"retry_after_{validator_result.status}"

        return RetryDecision(
            strategy=strategy,
            parameter_delta=param_delta,
            reason=reason,
        )

    @staticmethod
    def _parameter_delta(strategy: str, attempt_no: int) -> Dict[str, Any]:
        if strategy == "bbox_expand":
            return {"bbox_expand_pct": min(100, 25 * int(attempt_no))}
        if strategy == "template_alternative":
            return {"template_variant": f"alternative_{int(attempt_no)}"}
        if strategy == "fallback_config":
            return {"fallback_config": f"fallback_{int(attempt_no)}"}
        return {"retry_strategy": strategy, "retry_iteration": int(attempt_no)}


class PipelineApprovalQueue:
    def __init__(self) -> None:
        self._items_by_run: Dict[str, Dict[str, ApprovalItem]] = {}

    def create_item(
        self,
        *,
        run_id: str,
        phase: str,
        step_id: str,
        approval_type: ApprovalType,
        evidence_payload: Dict[str, Any],
        risk_summary: str,
        recommended_action: str,
    ) -> ApprovalItem:
        item = ApprovalItem(
            approval_id=str(uuid4()),
            run_id=str(run_id),
            phase=str(phase),
            step_id=str(step_id),
            approval_type=approval_type,
            status=ApprovalStatus.PENDING,
            created_at=_utc_now_iso(),
            created_by_system=True,
            evidence_payload=dict(evidence_payload or {}),
            risk_summary=str(risk_summary or ""),
            recommended_action=str(recommended_action or "review"),
        )
        self._items_by_run.setdefault(item.run_id, {})[item.approval_id] = item
        return item

    def resolve_item(
        self,
        *,
        run_id: str,
        approval_id: str,
        decision: str,
        operator_id: Optional[str],
        operator_decision: Optional[str],
    ) -> ApprovalItem:
        run_items = self._items_by_run.get(str(run_id)) or {}
        item = run_items.get(str(approval_id))
        if item is None:
            raise KeyError(f"approval not found: {approval_id}")

        norm = str(decision or "").strip().lower()
        if norm in {"approve", "approved"}:
            item.status = ApprovalStatus.APPROVED
        elif norm in {"reject", "rejected"}:
            item.status = ApprovalStatus.REJECTED
        elif norm in {"expire", "expired"}:
            item.status = ApprovalStatus.EXPIRED
        elif norm in {"supersede", "superseded"}:
            item.status = ApprovalStatus.SUPERSEDED
        else:
            raise ValueError(f"unsupported approval decision: {decision}")

        item.operator_id = (str(operator_id).strip() if operator_id else None)
        item.operator_decision = str(operator_decision or "").strip() or None
        item.decision_at = _utc_now_iso()
        return item

    def list_pending(self, run_id: str) -> List[ApprovalItem]:
        rows = list((self._items_by_run.get(str(run_id)) or {}).values())
        out = [x for x in rows if x.status == ApprovalStatus.PENDING]
        out.sort(key=lambda row: row.created_at)
        return out

    def list_all(self, run_id: str) -> List[ApprovalItem]:
        rows = list((self._items_by_run.get(str(run_id)) or {}).values())
        rows.sort(key=lambda row: row.created_at)
        return rows

    def get_item(self, run_id: str, approval_id: str) -> Optional[ApprovalItem]:
        return (self._items_by_run.get(str(run_id)) or {}).get(str(approval_id))

    def restore_items(self, run_id: str, items: Sequence[ApprovalItem]) -> None:
        rid = str(run_id)
        self._items_by_run.setdefault(rid, {})
        for item in list(items or []):
            self._items_by_run[rid][str(item.approval_id)] = item


ExecutorFn = Callable[[RunSessionState, StepDefinition, int, Dict[str, Any]], ExecutorResult]
ValidatorFn = Callable[[RunSessionState, StepDefinition, int, ExecutorResult], ValidatorResult]
AIBotFn = Callable[[RunSessionState, StepDefinition, int, ExecutorResult, ValidatorResult], AIBotSnapshot]
ChatGPTFn = Callable[
    [RunSessionState, StepDefinition, int, ExecutorResult, ValidatorResult, AIBotSnapshot, str],
    ChatGPTSnapshot,
]
ShadowLearnedRetryRankerFn = Callable[[Dict[str, Any]], Dict[str, Any]]


class SupervisedPipelineAutopilot:
    def __init__(
        self,
        *,
        step_registry: Optional[Dict[str, StepDefinition]] = None,
        executors: Optional[Dict[str, ExecutorFn]] = None,
        validators: Optional[Dict[str, ValidatorFn]] = None,
        ai_bot_hooks: Optional[Dict[str, AIBotFn]] = None,
        chatgpt_interpreter: Optional[ChatGPTFn] = None,
        approval_queue: Optional[PipelineApprovalQueue] = None,
        retry_engine: Optional[SafeRetryEngine] = None,
        enabled: Optional[bool] = None,
        feature_flag_name: str = "HADES_PIPELINE_AUTOPILOT_ENABLED",
        persistence_dir: Optional[str] = None,
        persist_runs: bool = True,
        persistence_backend: Optional[str] = None,
        feature_flags: Optional[AutopilotFeatureFlags] = None,
        db_store: Optional[AutopilotDBStore] = None,
        operator_signing_secret: Optional[str] = None,
        patch_dispatch_runner: Optional[Callable[..., Dict[str, Any]]] = None,
        runner_config_path: Optional[str] = None,
    ) -> None:
        self.step_registry = dict(step_registry or build_default_pipeline_step_registry())
        self.executors = dict(executors or {})
        self.validators = dict(validators or {})
        self.ai_bot_hooks = dict(ai_bot_hooks or {})
        self.chatgpt_interpreter = chatgpt_interpreter or self._default_chatgpt_interpreter
        self.shadow_learned_retry_ranker: Optional[ShadowLearnedRetryRankerFn] = None
        self.approval_queue = approval_queue or PipelineApprovalQueue()
        self.retry_engine = retry_engine or SafeRetryEngine()
        self.feature_flags = feature_flags or AutopilotFeatureFlags.from_env()
        self.feature_flag_name = str(feature_flag_name)
        if enabled is None:
            raw = str(os.getenv(self.feature_flag_name, "true")).strip().lower()
            self.enabled = raw in {"1", "true", "yes", "on"}
        else:
            self.enabled = bool(enabled)

        self.runs: Dict[str, RunSessionState] = {}
        self.max_loop_iterations = 500
        self.persist_runs = bool(persist_runs)
        self.persistence_backend = str(
            persistence_backend
            or os.getenv("HADES_AUTOPILOT_PERSISTENCE_BACKEND")
            or ("hybrid" if self.feature_flags.db_persistence_enabled else "json")
        ).strip().lower()
        if self.persistence_backend not in {"json", "db", "hybrid"}:
            self.persistence_backend = "json"
        db_enabled = self.persistence_backend in {"db", "hybrid"} and self.feature_flags.db_persistence_enabled
        self.db_store = db_store or AutopilotDBStore(enabled=db_enabled)
        self._idempotency_cache: Dict[str, Dict[str, Any]] = {}
        self.runner_config_path = Path(str(runner_config_path or DEFAULT_AUTOPILOT_RUNNER_CONFIG)).expanduser().resolve()
        self.patch_dispatch_runner = patch_dispatch_runner or self._default_patch_dispatch_runner
        self.operator_signing_secret = str(
            operator_signing_secret
            or os.getenv("HADES_OPERATOR_SIGNING_SECRET")
            or os.getenv("DATAMIND_OPERATOR_SIGNING_SECRET")
            or "hades-autopilot-dev-secret"
        )
        self.allowed_operator_roles = _default_operator_roles()
        self.persistence_dir = Path(
            str(
                persistence_dir
                or (
                    Path(__file__).resolve().parents[1]
                    / "orchestrator_logs"
                    / "autopilot_runs"
                )
            )
        )
        if self.persist_runs:
            self.persistence_dir.mkdir(parents=True, exist_ok=True)
            self._load_persisted_runs()

    def register_executor(self, name: str, fn: ExecutorFn) -> None:
        self.executors[str(name)] = fn

    def register_validator(self, name: str, fn: ValidatorFn) -> None:
        self.validators[str(name)] = fn

    def register_ai_hook(self, name: str, fn: AIBotFn) -> None:
        self.ai_bot_hooks[str(name)] = fn

    def register_shadow_learned_retry_ranker(self, fn: ShadowLearnedRetryRankerFn) -> None:
        self.shadow_learned_retry_ranker = fn

    def start_run(
        self,
        *,
        pipeline_scope: Dict[str, Any],
        policy_profile: str = PolicyProfile.BALANCED.value,
        start_step_id: Optional[str] = None,
        operator_context: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> RunSessionState:
        if not self.enabled:
            raise RuntimeError(
                f"Pipeline autopilot is disabled. Set {self.feature_flag_name}=true to enable."
            )

        profile = _normalize_profile(policy_profile)
        if not self.feature_flags.policy_profiles_enabled:
            profile = PolicyProfile.BALANCED.value
        initial_step_id = str(start_step_id or STEP_P1_1_EXTRACT)
        if initial_step_id not in self.step_registry:
            raise KeyError(f"unknown start_step_id: {initial_step_id}")

        now = _utc_now_iso()
        sid = str(run_id or uuid4())
        step = self.step_registry[initial_step_id]
        state = RunSessionState(
            run_id=sid,
            pipeline_scope=dict(pipeline_scope or {}),
            policy_profile=profile,
            status=RunStatus.RUNNING.value,
            current_phase=step.phase,
            current_step_id=step.step_id,
            started_at=now,
            updated_at=now,
            operator_context=dict(operator_context or {}),
        )
        raw_roles = state.operator_context.get("operator_roles")
        if isinstance(raw_roles, (list, tuple)):
            state.operator_context["operator_roles"] = [
                str(role).strip().lower()
                for role in raw_roles
                if str(role).strip()
            ]
        else:
            state.operator_context["operator_roles"] = []

        self.runs[sid] = state
        self._event(
            state,
            event_type="run_started",
            phase=state.current_phase,
            step_id=state.current_step_id,
            payload={
                "pipeline_scope": dict(state.pipeline_scope or {}),
                "policy_profile": state.policy_profile,
            },
        )
        return state

    def get_run(self, run_id: str) -> RunSessionState:
        rid = str(run_id)
        if rid not in self.runs:
            raise KeyError(f"run not found: {rid}")
        return self.runs[rid]

    def list_pending_approvals(self, run_id: str) -> List[ApprovalItem]:
        return self.approval_queue.list_pending(str(run_id))

    def list_patch_registry_records(self, run_id: str) -> List[Dict[str, Any]]:
        state = self.get_run(run_id)
        rows: List[Dict[str, Any]] = [
            _as_dict(row)
            for row in list(_as_dict(state.patch_registry).values())
            if isinstance(row, dict) and str(_as_dict(row).get("patch_task_id") or "").strip()
        ]
        if not rows and self.db_store.available:
            try:
                rows = [
                    _as_dict(row)
                    for row in list(self.db_store.list_patch_registry_records(run_id=state.run_id, limit=500))
                    if isinstance(row, dict)
                ]
            except Exception:
                rows = []
        rows.sort(
            key=lambda row: _iso_to_timestamp(row.get("updated_at") or row.get("created_at")),
            reverse=True,
        )
        return rows

    def _dispatch_target_metadata(self, target: Optional[str]) -> Dict[str, Any]:
        norm_target = str(target or "").strip().lower() or "codex"
        provider_name = _dispatch_provider_name_for_target(norm_target)
        configured_model_name: Optional[str] = None
        configured_command: List[str] = []
        config_exists = False
        if load_runner_config is not None and self.runner_config_path.exists():
            try:
                cfg = load_runner_config(self.runner_config_path)
                config_exists = True
                target_cfg = dict(getattr(cfg, "targets", {}) or {}).get(norm_target)
                if target_cfg is not None:
                    configured_command = [str(x) for x in list(getattr(target_cfg, "command", []) or []) if str(x).strip()]
                    configured_model_name = _extract_model_name_from_command(configured_command)
            except Exception:
                config_exists = False
        if not configured_model_name and norm_target == "codex":
            configured_model_name = str(os.getenv("CODEX_MODEL") or DEFAULT_AUTOPILOT_CODEX_MODEL).strip()
        return {
            "target": norm_target,
            "configured_provider_name": provider_name,
            "configured_model_name": configured_model_name,
            "runner_config_path": str(self.runner_config_path),
            "runner_config_exists": bool(config_exists and self.runner_config_path.exists()),
            "configured_command": configured_command[:12],
        }

    def _build_patch_prompt_artifact(
        self,
        *,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        intent: PatchTaskIntent,
        patch_task_response: Dict[str, Any],
    ) -> Dict[str, Any]:
        patch_task = _as_dict(patch_task_response.get("patch_task"))
        prompt_text = str(patch_task.get("prompt_text") or "").strip()
        target_meta = self._dispatch_target_metadata(intent.suggested_target)
        return {
            "artifact_id": str(uuid4()),
            "artifact_type": "patch_prompt_text",
            "created_at": _utc_now_iso(),
            "run_id": state.run_id,
            "trace_id": str(state.trace_id or ""),
            "phase": step.phase,
            "step_id": step.step_id,
            "attempt_no": int(attempt_no),
            "target": target_meta.get("target"),
            "configured_provider_name": target_meta.get("configured_provider_name"),
            "configured_model_name": target_meta.get("configured_model_name"),
            "prompt_chars": len(prompt_text),
            "prompt_preview": (prompt_text[:400] if prompt_text else ""),
            "patch_title": str(patch_task.get("title") or ""),
            "patch_type": str(intent.patch_type or ""),
            "suggested_target": str(intent.suggested_target or ""),
        }

    def _default_patch_dispatch_runner(
        self,
        *,
        patch_task_id: str,
        prompt_text: str,
        target: str,
        prompt_artifact: Dict[str, Any],
        dispatch_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        del dispatch_context
        if load_runner_config is None or run_prompt_file is None:
            raise RuntimeError("local_runner_unavailable")
        if not self.runner_config_path.exists():
            raise RuntimeError(f"runner_config_missing:{self.runner_config_path}")
        cfg = load_runner_config(self.runner_config_path)
        cfg.paths.inbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        prompt_name = f"{str(target or 'codex').strip().lower()}_patch_{patch_task_id}_{stamp}.md"
        prompt_path = cfg.paths.inbox / prompt_name
        prompt_path.write_text(str(prompt_text or ""), encoding="utf-8")
        result = run_prompt_file(
            file_path=prompt_path,
            target=str(target or "").strip().lower() or None,
            config_path=self.runner_config_path,
        )
        meta = self._dispatch_target_metadata(target)
        return {
            "status": str(result.status or ""),
            "prompt_artifact_id": str(prompt_artifact.get("artifact_id") or ""),
            "target": str(result.target or target or ""),
            "configured_provider_name": meta.get("configured_provider_name"),
            "configured_model_name": meta.get("configured_model_name"),
            "runner_config_path": str(self.runner_config_path),
            "command_used": str(result.command_used or ""),
            "duration_ms": int(result.duration_ms or 0),
            "output_file": str(result.output_file or ""),
            "stderr_file": (str(result.stderr_file) if result.stderr_file else None),
            "error_summary": (str(result.error_summary) if result.error_summary else None),
            "return_code": result.return_code,
            "prompt_file": str(result.prompt_file or ""),
            "prompt_file_final": str(result.prompt_file_final or ""),
            "log_file": str(result.log_file or ""),
        }

    def _execute_patch_dispatch(
        self,
        *,
        state: RunSessionState,
        phase: str,
        step_id: str,
        correlation_id: Optional[str],
        patch_task_id: str,
        patch_artifact: Dict[str, Any],
        approval_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        operator_role: Optional[str] = None,
    ) -> Dict[str, Any]:
        prompt_artifact = _as_dict(patch_artifact.get("prompt_artifact"))
        patch_task = _as_dict(_as_dict(patch_artifact.get("patch_task_response")).get("patch_task"))
        prompt_text = str(prompt_artifact.get("prompt_text") or patch_task.get("prompt_text") or "").strip()
        target_meta = self._dispatch_target_metadata(prompt_artifact.get("target") or _as_dict(patch_artifact.get("interpreter_ref")).get("suggested_target"))
        dispatch_ref = {
            "dispatch_id": str(uuid4()),
            "requested_at": _utc_now_iso(),
            "approval_id": (str(approval_id) if approval_id else None),
            "requested_by_operator_id": (str(operator_id) if operator_id else None),
            "requested_by_operator_role": (str(operator_role) if operator_role else None),
            "patch_task_artifact_id": str(patch_artifact.get("artifact_id") or ""),
            "patch_task_id": str(patch_task_id or ""),
            "patch_type": str(_as_dict(patch_artifact.get("interpreter_ref")).get("patch_type") or ""),
            "suggested_target": str(_as_dict(patch_artifact.get("interpreter_ref")).get("suggested_target") or target_meta.get("target") or ""),
            "target": str(target_meta.get("target") or ""),
            "codex_prompt_artifact_id": str(prompt_artifact.get("artifact_id") or ""),
            "configured_provider_name": target_meta.get("configured_provider_name"),
            "configured_model_name": target_meta.get("configured_model_name"),
        }
        requests = [dict(x or {}) for x in list(state.resume_context.get("patch_dispatch_requests") or [])]
        requests.append(dict(dispatch_ref))
        state.resume_context["patch_dispatch_requests"] = requests[-30:]
        self._event(
            state,
            event_type="patch_dispatch_requested",
            phase=phase,
            step_id=step_id,
            correlation_id=correlation_id,
            payload=dict(dispatch_ref),
        )

        if not prompt_text:
            failure = {
                **dispatch_ref,
                "dispatch_state": "failed",
                "codex_dispatch_status": "failed",
                "dispatch_block_reason": "patch_prompt_missing",
                "fallback_used": False,
                "error_summary": "patch task prompt_text is missing; cannot dispatch patch task",
            }
            self._event(
                state,
                event_type="patch_dispatch_failed",
                phase=phase,
                step_id=step_id,
                correlation_id=correlation_id,
                payload=dict(failure),
            )
            return failure

        self._event(
            state,
            event_type="patch_dispatch_attempted",
            phase=phase,
            step_id=step_id,
            correlation_id=correlation_id,
            payload=dict(dispatch_ref),
        )
        try:
            runner_result = dict(
                self.patch_dispatch_runner(
                    patch_task_id=str(patch_task_id or ""),
                    prompt_text=prompt_text,
                    target=str(target_meta.get("target") or ""),
                    prompt_artifact=dict(prompt_artifact or {}),
                    dispatch_context={
                        "run_id": state.run_id,
                        "trace_id": str(state.trace_id or ""),
                        "phase": str(phase or ""),
                        "step_id": str(step_id or ""),
                    },
                )
                or {}
            )
        except Exception as exc:
            failure = {
                **dispatch_ref,
                "dispatch_state": "failed",
                "codex_dispatch_status": "failed",
                "dispatch_block_reason": "dispatch_exception",
                "fallback_used": False,
                "error_summary": str(exc),
            }
            self._event(
                state,
                event_type="patch_dispatch_failed",
                phase=phase,
                step_id=step_id,
                correlation_id=correlation_id,
                payload=dict(failure),
            )
            return failure

        dispatch_status = str(runner_result.get("status") or "").strip().lower()
        result = {
            **dispatch_ref,
            **runner_result,
            "dispatch_state": ("dispatched" if dispatch_status == "success" else "failed"),
            "codex_dispatch_status": dispatch_status or "unknown",
            "fallback_used": False,
        }
        event_type = "patch_dispatch_completed" if dispatch_status == "success" else "patch_dispatch_failed"
        self._event(
            state,
            event_type=event_type,
            phase=phase,
            step_id=step_id,
            correlation_id=correlation_id,
            payload=dict(result),
        )
        return result

    def run_patch_retest_comparator(
        self,
        *,
        run_id: str,
        patch_task_id: str,
        baseline_run_snapshot: Optional[Dict[str, Any]] = None,
        retest_run_snapshot: Optional[Dict[str, Any]] = None,
        change_type: Optional[str] = None,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        state = self.get_run(run_id)
        patch_id = str(patch_task_id or "").strip()
        if not patch_id:
            raise ValueError("patch_task_id is required")
        record = self._get_patch_registry_record(state=state, patch_task_id=patch_id)
        if not record:
            raise KeyError(f"patch_task_id not found: {patch_id}")

        baseline = (
            dict(baseline_run_snapshot or {})
            or self._derive_patch_baseline_snapshot(state=state, patch_record=record)
        )
        retest = (
            dict(retest_run_snapshot or {})
            or self._derive_patch_retest_snapshot(state=state, patch_record=record)
        )
        if not baseline and not retest:
            raise ValueError("baseline_run_snapshot or retest_run_snapshot is required for comparator")

        rec_extra = dict(extra_context or {})
        if not rec_extra:
            rec_extra = {
                "policy_profile_before": str(record.get("policy_profile") or state.policy_profile or ""),
                "policy_profile_after": str(state.policy_profile or ""),
                "notes": "Stage6 patch comparator evaluation.",
            }

        baseline_ref = {
            "run_id": str(baseline.get("run_id") or record.get("run_id") or state.run_id),
            "step_id": str(baseline.get("step_id") or record.get("step_id") or ""),
            "timestamp": str(baseline.get("timestamp") or ""),
        }
        retest_ref = {
            "run_id": str(retest.get("run_id") or state.run_id),
            "step_id": str(retest.get("step_id") or record.get("step_id") or ""),
            "timestamp": str(retest.get("timestamp") or ""),
        }

        record["baseline_run_ref"] = dict(baseline_ref)
        record["baseline_context_ref"] = {
            "phase": str(record.get("phase") or ""),
            "step_id": str(record.get("step_id") or ""),
            "patch_task_id": patch_id,
        }
        retest_refs = list(record.get("retest_run_refs") or [])
        retest_refs.append(dict(retest_ref))
        record["retest_run_refs"] = retest_refs[-8:]
        self._upsert_patch_registry_record(state=state, record=record)
        self._patch_registry_update_status(
            state=state,
            patch_task_id=patch_id,
            status=PatchRegistryStatus.RETEST_PENDING.value,
            reason="retest_comparator_requested",
        )

        self._event(
            state,
            event_type="retest_comparator_started",
            phase=str(record.get("phase") or state.current_phase or ""),
            step_id=str(record.get("step_id") or state.current_step_id or ""),
            payload={
                "patch_task_id": patch_id,
                "baseline_run_ref": dict(baseline_ref),
                "retest_run_ref": dict(retest_ref),
            },
        )

        comparator = getattr(self.chatgpt_interpreter, "compare_retest_outcome", None)
        if not callable(comparator):
            err = "chatgpt_interpreter_missing_retest_comparator"
            self._patch_registry_update_status(
                state=state,
                patch_task_id=patch_id,
                status=PatchRegistryStatus.FAILED.value,
                reason=err,
            )
            self._event(
                state,
                event_type="retest_comparator_failed",
                phase=str(record.get("phase") or state.current_phase or ""),
                step_id=str(record.get("step_id") or state.current_step_id or ""),
                payload={"patch_task_id": patch_id, "error": err},
            )
            raise RuntimeError(err)

        try:
            out = comparator(
                patch_record=dict(record),
                baseline_run_snapshot=dict(baseline or {}),
                retest_run_snapshot=dict(retest or {}),
                change_type=(str(change_type or "") or str(record.get("change_type") or "") or None),
                extra_context=dict(rec_extra or {}),
            )
        except Exception as exc:
            self._patch_registry_update_status(
                state=state,
                patch_task_id=patch_id,
                status=PatchRegistryStatus.FAILED.value,
                reason=f"retest_comparator_error:{exc}",
            )
            self._event(
                state,
                event_type="retest_comparator_failed",
                phase=str(record.get("phase") or state.current_phase or ""),
                step_id=str(record.get("step_id") or state.current_step_id or ""),
                payload={"patch_task_id": patch_id, "error": str(exc)},
            )
            raise

        response = dict(out.get("response") or {})
        meta = dict(out.get("meta") or {})
        comparator_result_ref = {
            "task": "hades_retest_comparator",
            "snapshot_id": str(meta.get("snapshot_id") or ""),
            "schema_name": str(meta.get("schema_name") or ""),
            "model": str(meta.get("model") or ""),
            "latency_ms": (int(meta.get("latency_ms")) if meta.get("latency_ms") is not None else None),
            "token_usage": dict(meta.get("token_usage") or {}),
            "source": str(meta.get("source") or ""),
            "prompt_package": dict(meta.get("prompt_package") or {}),
        }
        result = str(response.get("result") or "").strip()
        recommendation = str(response.get("recommendation") or "").strip()
        confidence = _handoff_confidence_to_numeric(response.get("confidence"))
        if confidence is None:
            confidence = 0.0
        impact_summary = {
            "result": result or "inconclusive",
            "recommendation": recommendation or "observe_more",
            "confidence": round(float(confidence), 4),
            "metric_deltas": list(response.get("metric_deltas") or [])[:20],
            "summary": str(response.get("summary") or "").strip(),
        }
        record["comparator_result_ref"] = dict(comparator_result_ref)
        record["comparator_outcome"] = (result or "inconclusive")
        record["impact_summary"] = dict(impact_summary)
        self._upsert_patch_registry_record(state=state, record=record)
        self._patch_registry_update_status(
            state=state,
            patch_task_id=patch_id,
            status=PatchRegistryStatus.COMPARED.value,
            reason="retest_comparator_completed",
        )
        self._event(
            state,
            event_type="retest_comparator_completed",
            phase=str(record.get("phase") or state.current_phase or ""),
            step_id=str(record.get("step_id") or state.current_step_id or ""),
            payload={
                "patch_task_id": patch_id,
                "comparator_outcome": (result or "inconclusive"),
                "confidence": round(float(confidence), 4),
                "recommendation": recommendation,
                "snapshot_id": str(meta.get("snapshot_id") or ""),
            },
        )
        return {
            "patch_task_id": patch_id,
            "status": PatchRegistryStatus.COMPARED.value,
            "comparator_outcome": (result or "inconclusive"),
            "recommendation": recommendation or "observe_more",
            "confidence": round(float(confidence), 4),
        }

    def record_patch_outcome_decision(
        self,
        *,
        run_id: str,
        patch_task_id: str,
        decision: str,
        operator_id: str,
        operator_role: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        state = self.get_run(run_id)
        self._require_operator_identity(
            state,
            operator_id=operator_id,
            operator_role=operator_role,
            action="patch_outcome_decision",
        )
        patch_id = str(patch_task_id or "").strip()
        if not patch_id:
            raise ValueError("patch_task_id is required")
        record = self._get_patch_registry_record(state=state, patch_task_id=patch_id)
        if not record:
            raise KeyError(f"patch_task_id not found: {patch_id}")

        decision_norm = str(decision or "").strip().lower()
        decision_to_status = {
            "accepted": PatchRegistryStatus.ACCEPTED.value,
            "accept": PatchRegistryStatus.ACCEPTED.value,
            "rolled_back": PatchRegistryStatus.ROLLED_BACK.value,
            "rollback": PatchRegistryStatus.ROLLED_BACK.value,
            "observe_more": PatchRegistryStatus.OBSERVE_MORE.value,
            "observe": PatchRegistryStatus.OBSERVE_MORE.value,
        }
        status = decision_to_status.get(decision_norm)
        if not status:
            raise ValueError("decision must be one of: accepted|rollback|observe_more")

        note = str(notes or "").strip()
        record["operator_outcome_decision"] = (
            "accepted"
            if status == PatchRegistryStatus.ACCEPTED.value
            else ("rolled_back" if status == PatchRegistryStatus.ROLLED_BACK.value else "observe_more")
        )
        record["outcome_notes"] = note
        record["outcome_operator_id"] = str(operator_id or "")
        record["outcome_operator_role"] = str(operator_role or "")
        record["outcome_recorded_at"] = _utc_now_iso()
        self._upsert_patch_registry_record(state=state, record=record)
        self._patch_registry_update_status(
            state=state,
            patch_task_id=patch_id,
            status=status,
            reason="operator_outcome_decision",
        )
        self._event(
            state,
            event_type="patch_outcome_recorded",
            phase=str(record.get("phase") or state.current_phase or ""),
            step_id=str(record.get("step_id") or state.current_step_id or ""),
            payload={
                "patch_task_id": patch_id,
                "decision": record.get("operator_outcome_decision"),
                "operator_id": str(operator_id or ""),
                "operator_role": str(operator_role or ""),
                "notes": note,
            },
        )
        return {
            "patch_task_id": patch_id,
            "status": status,
            "operator_outcome_decision": record.get("operator_outcome_decision"),
        }

    def mark_manual_step_resolved(
        self,
        *,
        run_id: str,
        step_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        operator_role: Optional[str] = None,
        operator_notes: Optional[str] = None,
        resolution_payload: Optional[Dict[str, Any]] = None,
        max_steps_after_resume: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> RunSessionState:
        state = self.get_run(run_id)
        self._require_operator_identity(state, operator_id=operator_id, operator_role=operator_role, action="manual_resolution")
        sid = str(step_id or state.current_step_id or "").strip()
        if not sid:
            raise ValueError("manual step resolution requires step_id")
        step = self.step_registry.get(sid)
        if step is None:
            raise KeyError(f"unknown step_id: {sid}")
        if step.automation_level != AutomationLevel.MANUAL_ASSISTED:
            raise ValueError(f"step {sid} is not MANUAL_ASSISTED")

        payload = dict(resolution_payload or {})
        idem_key = str(idempotency_key or f"manual:{state.run_id}:{sid}:{hashlib.sha256(_to_jsonable_text(payload).encode('utf-8')).hexdigest()[:16]}")
        idem_existing = self._idempotency_get(idem_key)
        if idem_existing and str(idem_existing.get("status") or "") == "completed":
            state.status = RunStatus.RUNNING.value
            state.updated_at = _utc_now_iso()
            return self.advance_run(state.run_id, max_steps=max_steps_after_resume)
        self._apply_manual_resolution(
            state,
            step_id=sid,
            operator_id=operator_id,
            operator_role=operator_role,
            operator_notes=operator_notes,
            payload=payload,
            idempotency_key=idem_key,
        )
        self._idempotency_put(
            key=idem_key,
            run_id=state.run_id,
            scope="manual_resolution",
            action=sid,
            payload=payload,
            result_payload={"step_id": sid, "status": "manual_resolved"},
        )
        state.status = RunStatus.RUNNING.value
        state.updated_at = _utc_now_iso()
        self._event(
            state,
            event_type="run_resumed",
            phase=step.phase,
            step_id=sid,
            payload={
                "resume_mode": "manual_assisted_resolution",
                "step_id": sid,
                "operator_id": operator_id,
            },
        )
        return self.advance_run(state.run_id, max_steps=max_steps_after_resume)

    def advance_run(self, run_id: str, *, max_steps: Optional[int] = None) -> RunSessionState:
        state = self.get_run(run_id)
        if state.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value}:
            return state
        if state.status == RunStatus.WAITING_FOR_APPROVAL.value:
            return state

        state.status = RunStatus.RUNNING.value
        state.updated_at = _utc_now_iso()

        loop_guard = 0
        completed_steps = 0
        while state.status == RunStatus.RUNNING.value and state.current_step_id:
            loop_guard += 1
            if loop_guard > self.max_loop_iterations:
                state.status = RunStatus.FAILED.value
                state.completed_at = _utc_now_iso()
                self._event(
                    state,
                    event_type="run_failed",
                    phase=state.current_phase,
                    step_id=state.current_step_id,
                    payload={"error": "max_loop_iterations_exceeded"},
                )
                return state

            if max_steps is not None and completed_steps >= int(max_steps):
                break

            decision = self._execute_current_step(state)
            if decision == "advanced":
                completed_steps += 1
                continue
            if decision == "completed":
                return state
            if decision == "retry":
                continue
            if decision in {"paused", "blocked", "failed"}:
                return state

        return state

    def resolve_approval(
        self,
        *,
        run_id: str,
        approval_id: str,
        decision: str,
        operator_id: Optional[str] = None,
        operator_role: Optional[str] = None,
        operator_decision: Optional[str] = None,
        resolution_payload: Optional[Dict[str, Any]] = None,
        max_steps_after_resume: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> RunSessionState:
        state = self.get_run(run_id)
        self._require_operator_identity(state, operator_id=operator_id, operator_role=operator_role, action="approval_resolution")
        payload = dict(resolution_payload or {})
        preview = self.approval_queue.get_item(run_id, approval_id)
        decision_norm = str(decision or "").strip().lower()
        idem_key = str(idempotency_key or f"approval:{approval_id}:{decision_norm}")
        idem_existing = self._idempotency_get(idem_key)
        if idem_existing and str(idem_existing.get("status") or "") == "completed":
            return state
        if (
            preview
            and decision_norm in {"approve", "approved"}
            and preview.approval_type == ApprovalType.RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS
            and not bool(payload.get("promote_confirmed", False))
        ):
            raise ValueError("promote_confirmed=true is required before resuming Step20 diversion loop")
        if (
            preview
            and decision_norm in {"approve", "approved"}
            and preview.approval_type == ApprovalType.APPLY_REORDER_PROPOSAL
            and not str(
                payload.get("stop_sequence_candidate_id")
                or payload.get("selected_stop_sequence_candidate_id")
                or ""
            ).strip()
        ):
            raise ValueError("stop_sequence_candidate_id is required for canonical sequence approval")

        item = self.approval_queue.resolve_item(
            run_id=run_id,
            approval_id=approval_id,
            decision=decision,
            operator_id=operator_id,
            operator_decision=operator_decision,
        )
        item.operator_role = str(operator_role or "").strip() or None
        signature_payload = {
            "run_id": run_id,
            "approval_id": approval_id,
            "decision": decision_norm,
            "operator_id": operator_id,
            "operator_role": operator_role,
            "resolution_payload": payload,
            "decision_at": item.decision_at,
        }
        item.decision_signature = self._sign_operator_decision(signature_payload)
        state.approvals = self.approval_queue.list_all(state.run_id)
        self._sync_approval_state(state)
        state.updated_at = _utc_now_iso()

        self._event(
            state,
            event_type="approval_item_resolved",
            phase=item.phase,
            step_id=item.step_id,
            payload={
                "approval_id": item.approval_id,
                "approval_type": item.approval_type.value,
                "status": item.status.value,
                "operator_id": item.operator_id,
                "operator_role": item.operator_role,
                "operator_decision": item.operator_decision,
                "decision_signature": item.decision_signature,
            },
        )

        if item.approval_type == ApprovalType.DISPATCH_PATCH_TASK:
            out = self._resolve_patch_dispatch_approval(
                state=state,
                item=item,
                resolution_payload=payload,
            )
            self._idempotency_put(
                key=idem_key,
                run_id=state.run_id,
                scope="approval_resolution",
                action=str(item.approval_type.value),
                payload=signature_payload,
                result_payload={"status": item.status.value, "run_status": out.status},
            )
            return out

        if item.status != ApprovalStatus.APPROVED:
            state.status = RunStatus.PAUSED.value
            state.updated_at = _utc_now_iso()
            self._idempotency_put(
                key=idem_key,
                run_id=state.run_id,
                scope="approval_resolution",
                action=str(item.approval_type.value),
                payload=signature_payload,
                result_payload={"status": item.status.value, "run_status": state.status},
            )
            return state

        diversion = self._find_open_diversion_for_step(state, item.step_id)
        if diversion and item.approval_type == ApprovalType.RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS:
            self._apply_manual_resolution(
                state,
                step_id=STEP_P1_3B_NEW_NODES,
                operator_id=operator_id,
                operator_role=operator_role,
                operator_notes=operator_decision,
                payload=payload,
            )
            out = self._resume_after_diversion(
                state,
                diversion=diversion,
                resolution_payload=payload,
                max_steps_after_resume=max_steps_after_resume,
            )
            self._idempotency_put(
                key=idem_key,
                run_id=state.run_id,
                scope="approval_resolution",
                action=str(item.approval_type.value),
                payload=signature_payload,
                result_payload={"status": item.status.value, "run_status": out.status},
            )
            return out

        out = self._resume_after_standard_approval(
            state,
            approval=item,
            resolution_payload=payload,
            max_steps_after_resume=max_steps_after_resume,
        )
        self._idempotency_put(
            key=idem_key,
            run_id=state.run_id,
            scope="approval_resolution",
            action=str(item.approval_type.value),
            payload=signature_payload,
            result_payload={"status": item.status.value, "run_status": out.status},
        )
        return out

    def snapshot(self, run_id: str) -> Dict[str, Any]:
        state = self.get_run(run_id)
        return self._serialize_state(state)

    def get_audit_timeline(
        self,
        *,
        run_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        if self.db_store.available and (run_id or trace_id):
            rows = self.db_store.fetch_timeline(run_id=run_id, trace_id=trace_id, limit=int(limit))
            if rows:
                return rows

        out: List[Dict[str, Any]] = []
        if run_id:
            state = self.runs.get(str(run_id))
            if state is None:
                return []
            for evt in list(state.events or []):
                if trace_id and str(evt.trace_id) != str(trace_id):
                    continue
                out.append(
                    {
                        "ts": evt.timestamp,
                        "run_id": evt.run_id,
                        "trace_id": evt.trace_id,
                        "phase": evt.phase,
                        "step_id": evt.step_id,
                        "kind": evt.event_type,
                        "payload": dict(evt.payload or {}),
                        "correlation_id": evt.correlation_id,
                        "ref_id": evt.event_id,
                    }
                )
            out.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
            return out[: int(limit)]
        return []

    def export_evidence_bundle(self, *, run_id: str, out_dir: Optional[str] = None) -> Dict[str, Any]:
        if self.db_store.available:
            out = self.db_store.export_evidence_bundle(run_id=run_id, out_dir=out_dir)
            if out.get("ok"):
                return out
        state = self.get_run(run_id)
        bundle = self._serialize_state(state)
        base = Path(out_dir or (Path(__file__).resolve().parents[1] / "orchestrator_logs" / "autopilot_evidence"))
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{state.run_id}_evidence_bundle.json"
        path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "run_id": state.run_id, "path": str(path), "counts": {"events": len(state.events), "steps": len(state.step_execution_records)}}

    def enqueue_background_run(
        self,
        *,
        run_id: str,
        requested_by: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.feature_flags.worker_enabled:
            return None
        if not self.db_store.available:
            return None
        key = str(idempotency_key or f"queue:{run_id}")
        return self.db_store.enqueue_run(run_id=str(run_id), requested_by=requested_by, idempotency_key=key)

    def evaluate_slo_alerts(self, *, run_id: str) -> List[Dict[str, Any]]:
        if not self.feature_flags.slo_alerting_enabled:
            return []
        state = self.get_run(run_id)
        alerts: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        # Stuck run detection
        updated = None
        try:
            updated = datetime.fromisoformat(str(state.updated_at).replace("Z", "+00:00"))
        except Exception:
            updated = None
        stuck_seconds = int(os.getenv("HADES_AUTOPILOT_SLO_STUCK_RUN_S", "900") or 900)
        if state.status == RunStatus.RUNNING.value and updated is not None:
            if (now - updated).total_seconds() > float(stuck_seconds):
                alerts.append(
                    {
                        "alert_code": "RUN_STUCK",
                        "severity": "critical",
                        "summary": f"Run has been running without updates for more than {stuck_seconds}s.",
                        "details": {"updated_at": state.updated_at, "current_step_id": state.current_step_id},
                    }
                )

        # Retry exhaustion
        if state.step_execution_records:
            last = state.step_execution_records[-1]
            step = self.step_registry.get(last.step_id)
            if step and step.retry_policy.enabled and last.status == "blocked":
                max_attempts = int(step.retry_policy.max_attempts) + int(_policy(state.policy_profile).max_retry_bonus)
                if int(state.attempt_counters.get(last.step_id) or 0) >= max_attempts:
                    alerts.append(
                        {
                            "alert_code": "RETRY_EXHAUSTED",
                            "severity": "warning",
                            "summary": f"Retry budget exhausted for step {last.step_id}.",
                            "details": {"attempts": int(state.attempt_counters.get(last.step_id) or 0), "max_attempts": max_attempts},
                        }
                    )

        # Long-paused approvals
        paused_hours = float(os.getenv("HADES_AUTOPILOT_SLO_APPROVAL_PAUSE_H", "6") or 6.0)
        cutoff = now.timestamp() - (paused_hours * 3600.0)
        for item in self.approval_queue.list_pending(state.run_id):
            try:
                created_ts = datetime.fromisoformat(str(item.created_at).replace("Z", "+00:00")).timestamp()
            except Exception:
                created_ts = now.timestamp()
            if created_ts < cutoff:
                alerts.append(
                    {
                        "alert_code": "APPROVAL_LONG_PAUSED",
                        "severity": "warning",
                        "summary": f"Approval {item.approval_id} pending for longer than {paused_hours}h.",
                        "details": {"approval_id": item.approval_id, "approval_type": item.approval_type.value},
                    }
                )

        # Missing artifacts/events
        if len(state.events or []) == 0:
            alerts.append(
                {
                    "alert_code": "MISSING_EVENTS",
                    "severity": "critical",
                    "summary": "Run has no event records.",
                    "details": {"run_id": state.run_id},
                }
            )
        if len(state.step_execution_records or []) > 0 and len(state.artifacts or {}) == 0:
            alerts.append(
                {
                    "alert_code": "MISSING_ARTIFACTS",
                    "severity": "warning",
                    "summary": "Run has step attempts but no artifacts registered.",
                    "details": {"run_id": state.run_id},
                }
            )

        for alert in alerts:
            self._event(
                state,
                event_type="step_warning",
                phase=state.current_phase,
                step_id=state.current_step_id,
                payload={"warnings": [alert["alert_code"]], "slo_alert": dict(alert)},
            )
            if self.db_store.available:
                self.db_store.record_alert(
                    run_id=state.run_id,
                    trace_id=state.trace_id,
                    alert_code=str(alert.get("alert_code") or "UNKNOWN"),
                    severity=str(alert.get("severity") or "warning"),
                    summary=str(alert.get("summary") or ""),
                    details=dict(alert.get("details") or {}),
                )
        return alerts

    # ------------------------------------------------------------
    # Core loop internals
    # ------------------------------------------------------------
    def _execute_current_step(self, state: RunSessionState) -> str:
        step_id = str(state.current_step_id or "").strip()
        if not step_id:
            state.status = RunStatus.COMPLETED.value
            state.completed_at = _utc_now_iso()
            self._event(
                state,
                event_type="run_completed",
                phase=state.current_phase,
                step_id=None,
                payload={"reason": "no_current_step"},
            )
            return "completed"

        step = self.step_registry.get(step_id)
        if step is None:
            state.status = RunStatus.FAILED.value
            state.completed_at = _utc_now_iso()
            self._event(
                state,
                event_type="run_failed",
                phase=state.current_phase,
                step_id=step_id,
                payload={"error": "unknown_step"},
            )
            return "failed"

        state.current_phase = step.phase
        if step.automation_level == AutomationLevel.MANUAL_ASSISTED and self._is_manual_step_resolved(state, step.step_id):
            next_step_id = self._next_step_after_success(state, step)
            self._event(
                state,
                event_type="step_completed",
                phase=step.phase,
                step_id=step.step_id,
                payload={"manual_resolution_applied": True},
            )
            if next_step_id is None:
                state.current_step_id = None
                state.current_phase = None
                state.status = RunStatus.COMPLETED.value
                state.completed_at = _utc_now_iso()
                state.updated_at = state.completed_at
                self._event(
                    state,
                    event_type="run_completed",
                    phase=step.phase,
                    step_id=step.step_id,
                    payload={"final_step_id": step.step_id},
                )
                return "completed"
            state.current_step_id = next_step_id
            state.current_phase = self.step_registry[next_step_id].phase
            state.status = RunStatus.RUNNING.value
            state.updated_at = _utc_now_iso()
            return "advanced"

        attempt_no = int(state.attempt_counters.get(step.step_id) or 0) + 1
        state.attempt_counters[step.step_id] = attempt_no
        step_idem_key = f"step:{state.run_id}:{step.step_id}:{attempt_no}"
        existing_step_idem = self._idempotency_get(step_idem_key)
        if existing_step_idem and str(existing_step_idem.get("status") or "") == "completed":
            self._event(
                state,
                event_type="step_warning",
                phase=step.phase,
                step_id=step.step_id,
                payload={
                    "attempt_no": attempt_no,
                    "warnings": ["duplicate_step_execution_suppressed"],
                    "idempotency_key": step_idem_key,
                },
            )
            state.status = RunStatus.PAUSED.value
            state.updated_at = _utc_now_iso()
            return "paused"
        self._idempotency_put(
            key=step_idem_key,
            run_id=state.run_id,
            scope="step_execution",
            action=step.step_id,
            payload={"attempt_no": attempt_no},
            result_payload={"status": "pending"},
            status="pending",
        )

        started_at = _utc_now_iso()
        corr_id = str(uuid4())
        self._event(
            state,
            event_type="step_started",
            phase=step.phase,
            step_id=step.step_id,
            payload={"attempt_no": attempt_no},
            correlation_id=corr_id,
        )

        retry_params = (
            dict((state.resume_context.get("retry_params") or {}).pop(step.step_id, {}) or {})
            if isinstance(state.resume_context.get("retry_params"), dict)
            else {}
        )

        try:
            executor = self.executors.get(step.executor) or self._default_executor
            raw_exec = executor(state, step, attempt_no, retry_params)
            exec_result = self._coerce_executor_result(raw_exec)
            exec_summary = dict(exec_result.summary or {})
            effective_fp = str(exec_summary.get("effective_config_fingerprint") or "").strip()
            if effective_fp:
                fp_cache = state.resume_context.setdefault("effective_config_fingerprints", {})
                prev_fp = str(fp_cache.get(step.step_id) or "").strip() or None
                if attempt_no > 1 and prev_fp is not None:
                    changed = bool(prev_fp != effective_fp)
                    exec_summary["attempt_changed_from_previous"] = changed
                    if not changed:
                        warnings = [str(x) for x in list(exec_summary.get("retry_parameter_warnings") or []) if str(x).strip()]
                        if "retry_effective_config_unchanged" not in warnings:
                            warnings.append("retry_effective_config_unchanged")
                        exec_summary["retry_parameter_warnings"] = warnings
                fp_cache[step.step_id] = effective_fp
                exec_result.summary = exec_summary

            retry_param_warnings = [
                str(x).strip()
                for x in list(dict.fromkeys(exec_summary.get("retry_parameter_warnings") or []))
                if str(x).strip()
            ]
            if retry_param_warnings:
                self._event(
                    state,
                    event_type="step_warning",
                    phase=step.phase,
                    step_id=step.step_id,
                    payload={
                        "attempt_no": attempt_no,
                        "warnings": retry_param_warnings,
                        "warning_type": "retry_parameter",
                    },
                    correlation_id=corr_id,
                )

            validator = self.validators.get(step.validator) or self._default_validator
            raw_validator = validator(state, step, attempt_no, exec_result)
            validator_result = self._coerce_validator_result(raw_validator)
            exec_result = self._ensure_extractor_attempt_history_summary(
                step=step,
                attempt_no=attempt_no,
                exec_result=exec_result,
                validator_result=validator_result,
            )

            ai_hook_name = str((list(step.ai_bot_hooks or [])[:1] or [""])[0])
            ai_hook = self.ai_bot_hooks.get(ai_hook_name) or self._default_ai_bot_hook
            raw_ai = ai_hook(state, step, attempt_no, exec_result, validator_result)
            ai_snapshot = self._coerce_ai_snapshot(raw_ai)

            trigger = self._interpretation_trigger(validator_result, ai_snapshot)
            chatgpt_snapshot: Optional[ChatGPTSnapshot] = None
            if trigger and trigger in set(step.chatgpt_interpretation_triggers or []):
                self._event(
                    state,
                    event_type="interpreter_called",
                    phase=step.phase,
                    step_id=step.step_id,
                    payload={
                        "attempt_no": int(attempt_no),
                        "trigger": str(trigger or ""),
                    },
                    correlation_id=corr_id,
                )
                raw_chat = self.chatgpt_interpreter(
                    state,
                    step,
                    attempt_no,
                    exec_result,
                    validator_result,
                    ai_snapshot,
                    trigger,
                )
                chatgpt_snapshot = self._coerce_chatgpt_snapshot(raw_chat)
                self._event(
                    state,
                    event_type="interpreter_result_received",
                    phase=step.phase,
                    step_id=step.step_id,
                    payload={
                        "attempt_no": int(attempt_no),
                        "status": str(chatgpt_snapshot.status or ""),
                        "task": str(chatgpt_snapshot.task or ""),
                        "snapshot_id": chatgpt_snapshot.snapshot_id,
                        "source": chatgpt_snapshot.source,
                        "model": chatgpt_snapshot.model,
                        "requested_mode": chatgpt_snapshot.requested_mode,
                        "fallback_used": chatgpt_snapshot.fallback_used,
                        "error_code": chatgpt_snapshot.error_code,
                    },
                    correlation_id=corr_id,
                )
                if str(chatgpt_snapshot.status or "").strip().lower() == "error":
                    self._event(
                        state,
                        event_type="step_warning",
                        phase=step.phase,
                        step_id=step.step_id,
                        payload={
                            "attempt_no": attempt_no,
                            "warning_type": "chatgpt_advisory_failure",
                            "requested_mode": chatgpt_snapshot.requested_mode,
                            "source": chatgpt_snapshot.source,
                            "error_code": chatgpt_snapshot.error_code,
                            "error_summary": chatgpt_snapshot.error_summary,
                            "fallback_used": chatgpt_snapshot.fallback_used,
                        },
                        correlation_id=corr_id,
                    )
                patch_artifact = self._maybe_chain_patch_task(
                    state=state,
                    step=step,
                    attempt_no=attempt_no,
                    trigger=trigger,
                    exec_result=exec_result,
                    validator_result=validator_result,
                    ai_snapshot=ai_snapshot,
                    chatgpt_snapshot=chatgpt_snapshot,
                    correlation_id=corr_id,
                )
                if patch_artifact:
                    exec_result.artifacts = list(exec_result.artifacts or [])
                    exec_result.artifacts.append(dict(patch_artifact))

            block_reason: Optional[BlockReason] = None
            if self._gate_bypass_attempted(exec_result, validator_result):
                block_reason = self._build_block_reason(
                    step=step,
                    validator_result=ValidatorResult(
                        status="blocked",
                        gate_passed=False,
                        passable_warning=False,
                        warnings=list(validator_result.warnings or []),
                        anomalies=list(validator_result.anomalies or []),
                        block_reason_code=BlockReasonCode.GATE_BYPASS_ATTEMPT,
                        summary="Gate bypass attempt detected and rejected.",
                        evidence={
                            **dict(validator_result.evidence or {}),
                            "gate_bypass_attempted": True,
                            "executor_summary": dict(exec_result.summary or {}),
                        },
                        recommended_action="review_gate_integrity",
                        gate_bypass_attempted=True,
                    ),
                    ai_snapshot=ai_snapshot,
                    chatgpt_snapshot=chatgpt_snapshot,
                )
                self._record_step_attempt(
                    state=state,
                    step=step,
                    attempt_no=attempt_no,
                    status="blocked",
                    started_at=started_at,
                    exec_result=exec_result,
                    validator_result=validator_result,
                    ai_snapshot=ai_snapshot,
                    chatgpt_snapshot=chatgpt_snapshot,
                    block_reason=block_reason,
                    idempotency_key=step_idem_key,
                )
                self._idempotency_put(
                    key=step_idem_key,
                    run_id=state.run_id,
                    scope="step_execution",
                    action=step.step_id,
                    payload={"attempt_no": attempt_no},
                    result_payload={"status": "blocked", "block_reason_code": block_reason.code.value},
                    status="completed",
                )
                self._block_step(state, step, block_reason)
                return "blocked"

            if validator_result.status == "warning":
                self._event(
                    state,
                    event_type="step_warning",
                    phase=step.phase,
                    step_id=step.step_id,
                    payload={
                        "attempt_no": attempt_no,
                        "warnings": list(validator_result.warnings or []),
                        "anomalies": list(validator_result.anomalies or []),
                    },
                )

            if validator_result.status in {"blocked", "failed"} or not bool(validator_result.gate_passed):
                adaptive_plan: Optional[Dict[str, Any]] = None
                shadow_learned_plan: Optional[Dict[str, Any]] = None
                shadow_retry_comparison: Optional[Dict[str, Any]] = None
                if self._is_extractor_retry_step(step) and bool(getattr(self.feature_flags, "adaptive_retry_enabled", True)):
                    adaptive_plan = self._build_adaptive_retry_plan(
                        state=state,
                        step=step,
                        attempt_no=attempt_no,
                        exec_result=exec_result,
                        validator_result=validator_result,
                        ai_snapshot=ai_snapshot,
                    )
                    compact_plan = self._compact_adaptive_retry_plan(adaptive_plan)
                    exec_summary = dict(exec_result.summary or {})
                    exec_summary["adaptive_retry_plan"] = compact_plan
                    exec_result.summary = exec_summary
                    ai_metrics = dict(ai_snapshot.metrics or {})
                    ai_metrics["adaptive_retry_plan"] = compact_plan
                    ai_metrics["adaptive_retry_reordered"] = bool(compact_plan.get("reordered"))
                    ai_metrics["adaptive_retry_history_support_level"] = compact_plan.get("history_support_level")
                    ai_metrics["adaptive_retry_escalation_bias"] = compact_plan.get("escalation_bias")
                    ai_snapshot.metrics = ai_metrics
                    state.resume_context.setdefault("adaptive_retry_plans", {})[step.step_id] = compact_plan

                    adaptive_payload = self._adaptive_plan_event_payload(adaptive_plan)
                    adaptive_payload["attempt_no"] = int(attempt_no)
                    self._event(
                        state,
                        event_type="adaptive_retry_plan_evaluated",
                        phase=step.phase,
                        step_id=step.step_id,
                        payload=adaptive_payload,
                        correlation_id=corr_id,
                    )
                    if str(adaptive_payload.get("skip_reason") or "").strip():
                        self._event(
                            state,
                            event_type="adaptive_retry_plan_skipped",
                            phase=step.phase,
                            step_id=step.step_id,
                            payload=adaptive_payload,
                            correlation_id=corr_id,
                        )
                    elif bool(adaptive_payload.get("reordered")) or adaptive_payload.get("max_attempts_override") is not None:
                        self._event(
                            state,
                            event_type="adaptive_retry_plan_applied",
                            phase=step.phase,
                            step_id=step.step_id,
                            payload=adaptive_payload,
                            correlation_id=corr_id,
                        )
                    early_escalation_applied = bool(
                        str(adaptive_payload.get("escalation_bias") or "") == "early_escalation"
                        or adaptive_payload.get("max_attempts_override") is not None
                    )
                    if early_escalation_applied:
                        self._event(
                            state,
                            event_type="adaptive_early_escalation_bias_applied",
                            phase=step.phase,
                            step_id=step.step_id,
                            payload=adaptive_payload,
                            correlation_id=corr_id,
                        )

                if (
                    adaptive_plan
                    and self._is_extractor_retry_step(step)
                    and bool(getattr(self.feature_flags, "shadow_learned_retry_ranking_enabled", False))
                ):
                    shadow_features = self._build_shadow_retry_features(
                        state=state,
                        step=step,
                        attempt_no=attempt_no,
                        exec_result=exec_result,
                        validator_result=validator_result,
                        ai_snapshot=ai_snapshot,
                        deterministic_plan=adaptive_plan,
                    )
                    shadow_learned_plan = self._evaluate_shadow_learned_retry_plan(
                        state=state,
                        step=step,
                        attempt_no=attempt_no,
                        exec_result=exec_result,
                        validator_result=validator_result,
                        ai_snapshot=ai_snapshot,
                        deterministic_plan=adaptive_plan,
                        features=shadow_features,
                    )
                    shadow_retry_comparison = self._build_shadow_retry_plan_comparison(
                        deterministic_plan=adaptive_plan,
                        learned_plan=shadow_learned_plan,
                    )
                    shadow_eval_row = self._build_shadow_retry_eval_row(
                        state=state,
                        step=step,
                        attempt_no=attempt_no,
                        features=shadow_features,
                        deterministic_plan=adaptive_plan,
                        learned_plan=shadow_learned_plan,
                        comparison=shadow_retry_comparison,
                    )
                    compact_shadow_plan = self._compact_shadow_learned_retry_plan(shadow_learned_plan)
                    compact_shadow_comparison = self._compact_shadow_retry_comparison(shadow_retry_comparison)
                    exec_summary = dict(exec_result.summary or {})
                    exec_summary["shadow_learned_retry_plan"] = compact_shadow_plan
                    exec_summary["shadow_retry_plan_comparison"] = compact_shadow_comparison
                    exec_summary["shadow_retry_eval_row"] = dict(shadow_eval_row or {})
                    exec_result.summary = exec_summary
                    ai_metrics = dict(ai_snapshot.metrics or {})
                    ai_metrics["shadow_learned_retry_plan"] = compact_shadow_plan
                    ai_metrics["shadow_retry_plan_comparison"] = compact_shadow_comparison
                    ai_metrics["shadow_learned_retry_available"] = bool(compact_shadow_plan.get("available"))
                    ai_metrics["shadow_retry_top1_match"] = _handoff_to_bool(compact_shadow_comparison.get("top1_match"))
                    ai_snapshot.metrics = ai_metrics
                    state.resume_context.setdefault("shadow_learned_retry_plans", {})[step.step_id] = compact_shadow_plan
                    state.resume_context.setdefault("shadow_retry_plan_comparisons", {})[step.step_id] = compact_shadow_comparison
                    eval_rows = [dict(x or {}) for x in list(state.resume_context.get("shadow_retry_eval_rows") or [])]
                    eval_rows.append(dict(shadow_eval_row or {}))
                    state.resume_context["shadow_retry_eval_rows"] = eval_rows[-50:]

                    shadow_payload = {
                        "attempt_no": int(attempt_no),
                        "available": bool(compact_shadow_plan.get("available")),
                        "model_type": compact_shadow_plan.get("model_type"),
                        "model_name": compact_shadow_plan.get("model_name"),
                        "model_version": compact_shadow_plan.get("model_version"),
                        "ranked_order": list(compact_shadow_plan.get("ranked_order") or [])[:6],
                        "reason_codes": list(compact_shadow_plan.get("reason_codes") or [])[:8],
                        "confidence": compact_shadow_plan.get("confidence"),
                        "support_level": compact_shadow_plan.get("support_level"),
                        "unavailable_reason": compact_shadow_plan.get("unavailable_reason"),
                        "comparison_status": compact_shadow_comparison.get("comparison_status"),
                        "top1_match": compact_shadow_comparison.get("top1_match"),
                        "rank_overlap_count": compact_shadow_comparison.get("rank_overlap_count"),
                        "learned_confidence": compact_shadow_comparison.get("learned_confidence"),
                    }
                    self._event(
                        state,
                        event_type="shadow_learned_retry_plan_evaluated",
                        phase=step.phase,
                        step_id=step.step_id,
                        payload=shadow_payload,
                        correlation_id=corr_id,
                    )
                    if not bool(compact_shadow_plan.get("available")):
                        self._event(
                            state,
                            event_type="shadow_learned_retry_plan_unavailable",
                            phase=step.phase,
                            step_id=step.step_id,
                            payload=shadow_payload,
                            correlation_id=corr_id,
                        )
                    self._event(
                        state,
                        event_type="shadow_retry_plan_comparison_recorded",
                        phase=step.phase,
                        step_id=step.step_id,
                        payload={
                            "attempt_no": int(attempt_no),
                            **compact_shadow_comparison,
                            "eval_row": {
                                "feature_summary_hash": shadow_eval_row.get("feature_summary_hash"),
                                "comparison_status": shadow_eval_row.get("comparison_status"),
                            },
                        },
                        correlation_id=corr_id,
                    )

                retry = self.retry_engine.choose_retry(
                    step=step,
                    profile=state.policy_profile,
                    attempt_no=attempt_no,
                    validator_result=validator_result,
                    strategy_order=(
                        list(_as_dict(adaptive_plan).get("recommended_order") or [])
                        if adaptive_plan
                        else None
                    ),
                    max_attempts_override=(
                        _handoff_to_int(_as_dict(adaptive_plan).get("max_attempts_override"))
                        if adaptive_plan
                        else None
                    ),
                )
                if retry is not None:
                    state.resume_context.setdefault("retry_params", {})[step.step_id] = dict(retry.parameter_delta or {})
                    self._record_step_attempt(
                        state=state,
                        step=step,
                        attempt_no=attempt_no,
                        status="retry_scheduled",
                        started_at=started_at,
                        exec_result=exec_result,
                        validator_result=validator_result,
                        ai_snapshot=ai_snapshot,
                        chatgpt_snapshot=chatgpt_snapshot,
                        block_reason=None,
                        idempotency_key=step_idem_key,
                    )
                    self._event(
                        state,
                        event_type="step_retry_scheduled",
                        phase=step.phase,
                        step_id=step.step_id,
                        payload={
                            "attempt_no": attempt_no,
                            "retry_strategy": retry.strategy,
                            "retry_reason": retry.reason,
                            "parameter_delta": dict(retry.parameter_delta or {}),
                            "adaptive_retry_plan": (
                                self._adaptive_plan_event_payload(adaptive_plan)
                                if adaptive_plan
                                else None
                            ),
                            "shadow_learned_retry_plan": (
                                self._compact_shadow_learned_retry_plan(shadow_learned_plan)
                                if shadow_learned_plan
                                else None
                            ),
                            "shadow_retry_plan_comparison": (
                                self._compact_shadow_retry_comparison(shadow_retry_comparison)
                                if shadow_retry_comparison
                                else None
                            ),
                        },
                    )
                    state.updated_at = _utc_now_iso()
                    self._idempotency_put(
                        key=step_idem_key,
                        run_id=state.run_id,
                        scope="step_execution",
                        action=step.step_id,
                        payload={"attempt_no": attempt_no},
                        result_payload={"status": "retry_scheduled", "retry_strategy": retry.strategy},
                        status="completed",
                    )
                    return "retry"

                block_reason = self._build_block_reason(
                    step=step,
                    validator_result=validator_result,
                    ai_snapshot=ai_snapshot,
                    chatgpt_snapshot=chatgpt_snapshot,
                )
                self._record_step_attempt(
                    state=state,
                    step=step,
                    attempt_no=attempt_no,
                    status="blocked",
                    started_at=started_at,
                    exec_result=exec_result,
                    validator_result=validator_result,
                    ai_snapshot=ai_snapshot,
                    chatgpt_snapshot=chatgpt_snapshot,
                    block_reason=block_reason,
                    idempotency_key=step_idem_key,
                )
                self._idempotency_put(
                    key=step_idem_key,
                    run_id=state.run_id,
                    scope="step_execution",
                    action=step.step_id,
                    payload={"attempt_no": attempt_no},
                    result_payload={"status": "blocked", "block_reason_code": block_reason.code.value},
                    status="completed",
                )
                self._block_step(state, step, block_reason)
                return "blocked"

            if step.automation_level == AutomationLevel.APPROVAL_REQUIRED:
                self._record_step_attempt(
                    state=state,
                    step=step,
                    attempt_no=attempt_no,
                    status="waiting_for_approval",
                    started_at=started_at,
                    exec_result=exec_result,
                    validator_result=validator_result,
                    ai_snapshot=ai_snapshot,
                    chatgpt_snapshot=chatgpt_snapshot,
                    block_reason=None,
                    idempotency_key=step_idem_key,
                )
                self._idempotency_put(
                    key=step_idem_key,
                    run_id=state.run_id,
                    scope="step_execution",
                    action=step.step_id,
                    payload={"attempt_no": attempt_no},
                    result_payload={"status": "waiting_for_approval"},
                    status="completed",
                )
                self._pause_for_approval(
                    state,
                    step,
                    validator_result=validator_result,
                    ai_snapshot=ai_snapshot,
                    chatgpt_snapshot=chatgpt_snapshot,
                    reason="approval_required",
                )
                return "paused"

            if step.automation_level == AutomationLevel.MANUAL_ASSISTED:
                self._record_step_attempt(
                    state=state,
                    step=step,
                    attempt_no=attempt_no,
                    status="paused_manual",
                    started_at=started_at,
                    exec_result=exec_result,
                    validator_result=validator_result,
                    ai_snapshot=ai_snapshot,
                    chatgpt_snapshot=chatgpt_snapshot,
                    block_reason=None,
                    idempotency_key=step_idem_key,
                )
                state.status = RunStatus.PAUSED.value
                state.updated_at = _utc_now_iso()
                self._idempotency_put(
                    key=step_idem_key,
                    run_id=state.run_id,
                    scope="step_execution",
                    action=step.step_id,
                    payload={"attempt_no": attempt_no},
                    result_payload={"status": "paused_manual"},
                    status="completed",
                )
                return "paused"

            if validator_result.status == "warning":
                if self._should_pause_for_warning(state.policy_profile, validator_result):
                    self._record_step_attempt(
                        state=state,
                        step=step,
                        attempt_no=attempt_no,
                        status="paused_warning",
                        started_at=started_at,
                        exec_result=exec_result,
                        validator_result=validator_result,
                        ai_snapshot=ai_snapshot,
                        chatgpt_snapshot=chatgpt_snapshot,
                        block_reason=None,
                        idempotency_key=step_idem_key,
                    )
                    state.status = RunStatus.PAUSED.value
                    state.updated_at = _utc_now_iso()
                    self._idempotency_put(
                        key=step_idem_key,
                        run_id=state.run_id,
                        scope="step_execution",
                        action=step.step_id,
                        payload={"attempt_no": attempt_no},
                        result_payload={"status": "paused_warning"},
                        status="completed",
                    )
                    return "paused"

            # Success path.
            self._record_step_attempt(
                state=state,
                step=step,
                attempt_no=attempt_no,
                status="completed",
                started_at=started_at,
                exec_result=exec_result,
                validator_result=validator_result,
                ai_snapshot=ai_snapshot,
                chatgpt_snapshot=chatgpt_snapshot,
                block_reason=None,
                idempotency_key=step_idem_key,
            )
            self._idempotency_put(
                key=step_idem_key,
                run_id=state.run_id,
                scope="step_execution",
                action=step.step_id,
                payload={"attempt_no": attempt_no},
                result_payload={"status": "completed"},
                status="completed",
            )
            self._event(
                state,
                event_type="step_completed",
                phase=step.phase,
                step_id=step.step_id,
                payload={
                    "attempt_no": attempt_no,
                    "warnings": list(validator_result.warnings or []),
                    "auto_advanced": True,
                },
            )

            self._apply_run_context_from_executor(state, step, exec_result)
            next_step_id = self._next_step_after_success(state, step)
            if next_step_id is None:
                state.current_step_id = None
                state.current_phase = None
                state.status = RunStatus.COMPLETED.value
                state.completed_at = _utc_now_iso()
                state.updated_at = state.completed_at
                self._event(
                    state,
                    event_type="run_completed",
                    phase=step.phase,
                    step_id=step.step_id,
                    payload={"final_step_id": step.step_id},
                )
                return "completed"

            state.current_step_id = next_step_id
            state.current_phase = self.step_registry[next_step_id].phase
            state.status = RunStatus.RUNNING.value
            state.updated_at = _utc_now_iso()
            return "advanced"

        except Exception as exc:
            state.status = RunStatus.FAILED.value
            state.completed_at = _utc_now_iso()
            state.updated_at = state.completed_at
            self._idempotency_put(
                key=step_idem_key,
                run_id=state.run_id,
                scope="step_execution",
                action=step.step_id,
                payload={"attempt_no": attempt_no},
                result_payload={"status": "failed", "error": str(exc)},
                status="failed",
            )
            self._event(
                state,
                event_type="run_failed",
                phase=step.phase,
                step_id=step.step_id,
                payload={"error": str(exc), "attempt_no": attempt_no},
            )
            return "failed"

    # ------------------------------------------------------------
    # Decision / approval / diversion internals
    # ------------------------------------------------------------
    def _block_step(self, state: RunSessionState, step: StepDefinition, block_reason: BlockReason) -> None:
        state.resume_context["last_block_reason"] = asdict(block_reason)
        self._event(
            state,
            event_type="step_blocked",
            phase=step.phase,
            step_id=step.step_id,
            payload={"block_reason": asdict(block_reason)},
        )

        rule = step.diversion_rules.get(block_reason.code)
        if rule is not None:
            diversion_id = str(uuid4())
            diversion = {
                "diversion_id": diversion_id,
                "origin_phase": step.phase,
                "origin_step_id": step.step_id,
                "target_step_id": rule.target_step_id,
                "resume_step_id": rule.resume_step_id,
                "optional_phase2_partial_rerun": bool(rule.optional_phase2_partial_rerun),
                "started_at": _utc_now_iso(),
                "completed_at": None,
                "status": "open",
                "block_reason_code": block_reason.code.value,
            }
            state.diversion_stack.append(diversion)

            approval = self.approval_queue.create_item(
                run_id=state.run_id,
                phase=step.phase,
                step_id=step.step_id,
                approval_type=rule.approval_type,
                evidence_payload={
                    "block_reason": asdict(block_reason),
                    "diversion": diversion,
                },
                risk_summary="Step20 blocker requires human resolution for unmatched/ambiguous items.",
                recommended_action="resolve_in_phase1_new_nodes_and_confirm_promote",
            )
            state.approvals = self.approval_queue.list_all(state.run_id)
            self._sync_approval_state(state)

            self._event(
                state,
                event_type="approval_item_created",
                phase=step.phase,
                step_id=step.step_id,
                payload={
                    "approval_id": approval.approval_id,
                    "approval_type": approval.approval_type.value,
                    "status": approval.status.value,
                },
            )
            self._event(
                state,
                event_type="diversion_started",
                phase=step.phase,
                step_id=step.step_id,
                payload={
                    "diversion_id": diversion_id,
                    "target_step_id": rule.target_step_id,
                    "resume_step_id": rule.resume_step_id,
                },
            )

            state.current_step_id = rule.target_step_id
            state.current_phase = self.step_registry[rule.target_step_id].phase
            state.status = RunStatus.WAITING_FOR_APPROVAL.value
            state.updated_at = _utc_now_iso()
            return

        # No diversion rule: plain pause.
        if block_reason.required_approval_type is not None:
            approval = self.approval_queue.create_item(
                run_id=state.run_id,
                phase=step.phase,
                step_id=step.step_id,
                approval_type=block_reason.required_approval_type,
                evidence_payload={"block_reason": asdict(block_reason)},
                risk_summary="Blocked step requires operator approval before continuing.",
                recommended_action=block_reason.recommended_next_action,
            )
            state.approvals = self.approval_queue.list_all(state.run_id)
            self._sync_approval_state(state)
            self._event(
                state,
                event_type="approval_item_created",
                phase=step.phase,
                step_id=step.step_id,
                payload={
                    "approval_id": approval.approval_id,
                    "approval_type": approval.approval_type.value,
                    "status": approval.status.value,
                },
            )
            state.status = RunStatus.WAITING_FOR_APPROVAL.value
        else:
            state.status = RunStatus.PAUSED.value

        state.updated_at = _utc_now_iso()

    def _pause_for_approval(
        self,
        state: RunSessionState,
        step: StepDefinition,
        *,
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
        chatgpt_snapshot: Optional[ChatGPTSnapshot],
        reason: str,
    ) -> None:
        approval_type = step.approval_type
        if approval_type is None:
            raise RuntimeError(f"step {step.step_id} is approval-required but approval_type is missing")

        approval = self.approval_queue.create_item(
            run_id=state.run_id,
            phase=step.phase,
            step_id=step.step_id,
            approval_type=approval_type,
            evidence_payload={
                "validator_result": asdict(validator_result),
                "ai_bot_snapshot": asdict(ai_snapshot),
                "chatgpt_snapshot": (asdict(chatgpt_snapshot) if chatgpt_snapshot else None),
                "reason": reason,
            },
            risk_summary=validator_result.summary or "Critical action requires approval.",
            recommended_action="approve_or_reject",
        )
        state.approvals = self.approval_queue.list_all(state.run_id)
        self._sync_approval_state(state)

        self._event(
            state,
            event_type="approval_item_created",
            phase=step.phase,
            step_id=step.step_id,
            payload={
                "approval_id": approval.approval_id,
                "approval_type": approval.approval_type.value,
                "status": approval.status.value,
                "reason": reason,
            },
        )

        state.status = RunStatus.WAITING_FOR_APPROVAL.value
        state.updated_at = _utc_now_iso()

    def _resume_after_standard_approval(
        self,
        state: RunSessionState,
        *,
        approval: ApprovalItem,
        resolution_payload: Dict[str, Any],
        max_steps_after_resume: Optional[int],
    ) -> RunSessionState:
        step = self.step_registry.get(approval.step_id)
        if step is None:
            state.status = RunStatus.FAILED.value
            state.completed_at = _utc_now_iso()
            self._event(
                state,
                event_type="run_failed",
                phase=approval.phase,
                step_id=approval.step_id,
                payload={"error": "approval_step_missing"},
            )
            return state

        if step.approval_apply_executor:
            apply_idem_key = f"approval_apply:{approval.approval_id}"
            apply_cached = self._idempotency_get(apply_idem_key)
            if apply_cached and str(apply_cached.get("status") or "") == "completed":
                cached_payload = dict(apply_cached.get("result_payload") or {})
                self._event(
                    state,
                    event_type="step_warning",
                    phase=step.phase,
                    step_id=step.step_id,
                    payload={
                        "warnings": ["duplicate_approval_apply_suppressed"],
                        "approval_id": approval.approval_id,
                        "idempotency_key": apply_idem_key,
                    },
                )
                apply_summary = dict(cached_payload.get("apply_summary") or {})
                cached_artifacts = list(cached_payload.get("artifacts") or [])
                if cached_artifacts:
                    state.artifacts.setdefault(step.step_id, [])
                    state.artifacts[step.step_id].extend(cached_artifacts)
                self._event(
                    state,
                    event_type="step_completed",
                    phase=step.phase,
                    step_id=step.step_id,
                    payload={
                        "approval_applied": True,
                        "approval_type": approval.approval_type.value,
                        "apply_summary": apply_summary,
                        "idempotency_replay": True,
                    },
                )
            else:
                self._idempotency_put(
                    key=apply_idem_key,
                    run_id=state.run_id,
                    scope="approval_apply",
                    action=step.step_id,
                    payload={"approval_id": approval.approval_id},
                    result_payload={"status": "pending"},
                    status="pending",
                )
            action = step.approval_action
            if action:
                policy = check_permission(ROLE_RUNTIME, action, operator_confirmed=True)
                if not policy.allowed:
                    state.status = RunStatus.FAILED.value
                    state.completed_at = _utc_now_iso()
                    self._event(
                        state,
                        event_type="run_failed",
                        phase=step.phase,
                        step_id=step.step_id,
                        payload={
                            "error": policy.message,
                            "error_code": policy.code,
                            "action": action,
                        },
                    )
                    return state

            if not (apply_cached and str(apply_cached.get("status") or "") == "completed"):
                apply_executor = self.executors.get(step.approval_apply_executor) or self._default_executor
                apply_out = self._coerce_executor_result(
                    apply_executor(
                        state,
                        step,
                        int(state.attempt_counters.get(step.step_id) or 1),
                        {
                            "approved": True,
                            "operator_id": approval.operator_id,
                            "operator_role": approval.operator_role,
                            "operator_decision": approval.operator_decision,
                            "decision_signature": approval.decision_signature,
                            **resolution_payload,
                        },
                    )
                )
                if not apply_out.ok:
                    apply_summary = dict(apply_out.summary or {})
                    state.status = RunStatus.FAILED.value
                    state.completed_at = _utc_now_iso()
                    state.updated_at = state.completed_at
                    self._idempotency_put(
                        key=apply_idem_key,
                        run_id=state.run_id,
                        scope="approval_apply",
                        action=step.step_id,
                        payload={"approval_id": approval.approval_id, **resolution_payload},
                        result_payload={
                            "status": "failed",
                            "apply_summary": apply_summary,
                            "artifacts": list(apply_out.artifacts or []),
                        },
                        status="failed",
                    )
                    self._event(
                        state,
                        event_type="run_failed",
                        phase=step.phase,
                        step_id=step.step_id,
                        payload={
                            "error": str(apply_summary.get("error") or f"{step.step_id} approval apply failed"),
                            "approval_id": approval.approval_id,
                            "approval_type": approval.approval_type.value,
                            "apply_summary": apply_summary,
                        },
                    )
                    return state
                self._event(
                    state,
                    event_type="step_completed",
                    phase=step.phase,
                    step_id=step.step_id,
                    payload={
                        "approval_applied": True,
                        "approval_type": approval.approval_type.value,
                        "apply_summary": dict(apply_out.summary or {}),
                    },
                )
                state.artifacts.setdefault(step.step_id, [])
                state.artifacts[step.step_id].extend(list(apply_out.artifacts or []))
                self._idempotency_put(
                    key=apply_idem_key,
                    run_id=state.run_id,
                    scope="approval_apply",
                    action=step.step_id,
                    payload={"approval_id": approval.approval_id, **resolution_payload},
                    result_payload={
                        "status": "completed",
                        "apply_summary": dict(apply_out.summary or {}),
                        "artifacts": list(apply_out.artifacts or []),
                    },
                    status="completed",
                )

        next_step_id = step.next_step_on_success
        if step.resume_behavior.mode == "rerun_step" and step.resume_behavior.rerun_step_id:
            next_step_id = step.resume_behavior.rerun_step_id

        state.current_step_id = next_step_id
        state.current_phase = self.step_registry[next_step_id].phase if next_step_id else None
        state.status = RunStatus.RUNNING.value
        state.updated_at = _utc_now_iso()

        self._event(
            state,
            event_type="run_resumed",
            phase=state.current_phase,
            step_id=state.current_step_id,
            payload={
                "approved_step_id": step.step_id,
                "approval_id": approval.approval_id,
                "approval_type": approval.approval_type.value,
            },
        )
        return self.advance_run(state.run_id, max_steps=max_steps_after_resume)

    def _resume_after_diversion(
        self,
        state: RunSessionState,
        *,
        diversion: Dict[str, Any],
        resolution_payload: Dict[str, Any],
        max_steps_after_resume: Optional[int],
    ) -> RunSessionState:
        diversion["status"] = "completed"
        diversion["completed_at"] = _utc_now_iso()

        self._event(
            state,
            event_type="diversion_completed",
            phase=state.current_phase,
            step_id=diversion.get("origin_step_id"),
            payload={
                "diversion_id": diversion.get("diversion_id"),
                "origin_step_id": diversion.get("origin_step_id"),
            },
        )

        requires_partial = self._resolve_phase2_partial_rerun_requirement(
            diversion=diversion,
            resolution_payload=resolution_payload,
            state=state,
        )

        resume_step_id = str(diversion.get("resume_step_id") or STEP_P3_2_STEP20)
        self._event(
            state,
            event_type="resume_triggered",
            phase=state.current_phase,
            step_id=resume_step_id,
            payload={
                "diversion_id": diversion.get("diversion_id"),
                "resume_step_id": resume_step_id,
                "requires_phase2_partial_rerun": bool(requires_partial),
            },
        )

        state.resume_context["last_resolution"] = dict(resolution_payload or {})
        if not self.feature_flags.auto_resume_enabled:
            state.current_step_id = resume_step_id
            state.current_phase = self.step_registry[resume_step_id].phase
            state.status = RunStatus.PAUSED.value
            state.updated_at = _utc_now_iso()
            self._event(
                state,
                event_type="step_warning",
                phase=state.current_phase,
                step_id=state.current_step_id,
                payload={
                    "warnings": ["auto_resume_feature_flag_disabled"],
                    "resume_step_id": resume_step_id,
                },
            )
            return state

        state.status = RunStatus.RUNNING.value

        if requires_partial:
            state.current_step_id = STEP_P2_1_SEMANTIC
            state.current_phase = self.step_registry[STEP_P2_1_SEMANTIC].phase
            state.resume_context["forced_next_step_after"] = {
                "after_step_id": STEP_P2_1_SEMANTIC,
                "next_step_id": resume_step_id,
            }
        else:
            state.current_step_id = resume_step_id
            state.current_phase = self.step_registry[resume_step_id].phase

        state.updated_at = _utc_now_iso()

        self._event(
            state,
            event_type="run_resumed",
            phase=state.current_phase,
            step_id=state.current_step_id,
            payload={
                "resume_mode": "diversion",
                "diversion_id": diversion.get("diversion_id"),
            },
        )

        out = self.advance_run(state.run_id, max_steps=max_steps_after_resume)
        self._event(
            out,
            event_type="resume_completed",
            phase=out.current_phase,
            step_id=out.current_step_id,
            payload={
                "diversion_id": diversion.get("diversion_id"),
                "status": out.status,
            },
        )
        return out

    def _resolve_phase2_partial_rerun_requirement(
        self,
        *,
        diversion: Dict[str, Any],
        resolution_payload: Dict[str, Any],
        state: RunSessionState,
    ) -> bool:
        explicit = resolution_payload.get("requires_p2_partial_rerun")
        if explicit is not None:
            return bool(explicit)

        if not bool(diversion.get("optional_phase2_partial_rerun")):
            return False

        block = dict(state.resume_context.get("last_block_reason") or {})
        evidence = dict(block.get("validator_evidence") or {})

        unmatched = int(evidence.get("unmatched_count") or 0)
        ambiguous = int(evidence.get("ambiguous_count") or 0)
        return bool(ambiguous > 0 or unmatched > 3)

    @staticmethod
    def _build_run_snapshot_from_step_record(
        *,
        rec: StepExecutionRecord,
        label: str,
    ) -> Dict[str, Any]:
        validator = _as_dict(rec.validator_result)
        validator_evidence = _as_dict(validator.get("evidence"))
        ai_snapshot = _as_dict(rec.ai_bot_snapshot)
        ai_scores = _as_dict(ai_snapshot.get("scores"))
        ai_metrics = _as_dict(ai_snapshot.get("metrics"))
        exec_summary = _as_dict(rec.executor_result_summary)
        warnings = [
            str(x).strip()
            for x in list(dict.fromkeys(list(validator.get("warnings") or []) + list(ai_snapshot.get("warnings") or [])))
            if str(x).strip()
        ]
        quality_score = _handoff_to_float(ai_scores.get("quality_score"))
        if quality_score is None:
            quality_score = _handoff_to_float(ai_scores.get("quality"))
        if quality_score is None:
            quality_score = _handoff_to_float(exec_summary.get("quality_score"))
        if quality_score is None:
            quality_score = _handoff_to_float(validator_evidence.get("quality_score"))
        sequence_quality_score = _handoff_to_float(ai_scores.get("sequence_quality_score"))
        if sequence_quality_score is None:
            sequence_quality_score = _handoff_to_float(ai_metrics.get("sequence_quality_score"))
        if sequence_quality_score is None:
            sequence_quality_score = _handoff_to_float(exec_summary.get("sequence_quality_score"))
        if sequence_quality_score is None:
            sequence_quality_score = _handoff_to_float(validator_evidence.get("sequence_quality_score"))
        return {
            "label": str(label or "run"),
            "run_id": str(rec.run_id or ""),
            "timestamp": str(rec.created_at or ""),
            "phase": str(rec.phase or ""),
            "step_id": str(rec.step_id or ""),
            "metrics": {
                "quality_score": quality_score,
                "sequence_quality_score": sequence_quality_score,
                "warning_count": len(warnings),
                "unmatched_count": _handoff_to_int(
                    exec_summary.get("unmatched_count")
                    if exec_summary.get("unmatched_count") is not None
                    else validator_evidence.get("unmatched_count")
                ),
                "ambiguous_count": _handoff_to_int(
                    exec_summary.get("ambiguous_count")
                    if exec_summary.get("ambiguous_count") is not None
                    else validator_evidence.get("ambiguous_count")
                ),
            },
            "warnings": warnings[:20],
            "regression_flags": list(ai_metrics.get("regression_flags") or [])[:20],
        }

    def _derive_patch_baseline_snapshot(
        self,
        *,
        state: RunSessionState,
        patch_record: Dict[str, Any],
    ) -> Dict[str, Any]:
        step_id = str(patch_record.get("step_id") or "")
        attempt_no = int(patch_record.get("attempt_no") or 0)
        for row in list(state.step_execution_records or []):
            if str(row.step_id or "") != step_id:
                continue
            if int(row.attempt_no or 0) != attempt_no:
                continue
            return self._build_run_snapshot_from_step_record(rec=row, label="baseline")
        if state.step_execution_records:
            return self._build_run_snapshot_from_step_record(rec=state.step_execution_records[-1], label="baseline")
        return {}

    def _derive_patch_retest_snapshot(
        self,
        *,
        state: RunSessionState,
        patch_record: Dict[str, Any],
    ) -> Dict[str, Any]:
        step_id = str(patch_record.get("step_id") or "")
        attempt_no = int(patch_record.get("attempt_no") or 0)
        candidates: List[StepExecutionRecord] = []
        for row in list(state.step_execution_records or []):
            if str(row.step_id or "") != step_id:
                continue
            if int(row.attempt_no or 0) < attempt_no:
                continue
            candidates.append(row)
        if candidates:
            candidates.sort(key=lambda row: _iso_to_timestamp(row.created_at), reverse=True)
            return self._build_run_snapshot_from_step_record(rec=candidates[0], label="retest")
        if state.step_execution_records:
            return self._build_run_snapshot_from_step_record(rec=state.step_execution_records[-1], label="retest")
        return {}

    def _get_patch_registry_record(
        self,
        *,
        state: RunSessionState,
        patch_task_id: str,
    ) -> Optional[Dict[str, Any]]:
        patch_id = str(patch_task_id or "").strip()
        if not patch_id:
            return None
        row = _as_dict(state.patch_registry).get(patch_id)
        if isinstance(row, dict):
            return dict(row)
        for candidate in list(_as_dict(state.patch_registry).values()):
            item = _as_dict(candidate)
            if str(item.get("patch_task_artifact_id") or "").strip() == patch_id:
                return dict(item)
        return None

    def _upsert_patch_registry_record(
        self,
        *,
        state: RunSessionState,
        record: Dict[str, Any],
    ) -> None:
        row = dict(record or {})
        patch_task_id = str(row.get("patch_task_id") or "").strip()
        if not patch_task_id:
            raise ValueError("patch_registry record requires patch_task_id")
        row["patch_task_id"] = patch_task_id
        row["run_id"] = str(row.get("run_id") or state.run_id)
        row["trace_id"] = str(row.get("trace_id") or state.trace_id or "")
        if not str(row.get("created_at") or "").strip():
            row["created_at"] = _utc_now_iso()
        row["updated_at"] = _utc_now_iso()
        state.patch_registry[patch_task_id] = dict(row)
        if self.db_store.available:
            try:
                self.db_store.upsert_patch_registry_record(dict(row), trace_id=state.trace_id)
            except Exception:
                pass

    def _create_patch_registry_record(
        self,
        *,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        correlation_id: Optional[str],
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
        chatgpt_snapshot: ChatGPTSnapshot,
        intent: PatchTaskIntent,
        patch_artifact: Dict[str, Any],
    ) -> Dict[str, Any]:
        patch_response = _as_dict(patch_artifact.get("patch_task_response"))
        patch_task = _as_dict(patch_response.get("patch_task"))
        patch_task_id = str(
            patch_task.get("patch_task_id")
            or patch_task.get("task_id")
            or patch_artifact.get("artifact_id")
            or uuid4()
        ).strip()
        now = _utc_now_iso()
        block_reason_code = None
        if isinstance(validator_result.block_reason_code, BlockReasonCode):
            block_reason_code = validator_result.block_reason_code.value
        elif validator_result.block_reason_code:
            block_reason_code = str(validator_result.block_reason_code)
        extractor_help = _as_dict(ai_snapshot.extractor_help_needed or _as_dict(ai_snapshot.metrics).get("extractor_help_payload"))
        row = {
            "patch_task_id": patch_task_id,
            "run_id": str(state.run_id or ""),
            "trace_id": str(state.trace_id or ""),
            "phase": str(step.phase or ""),
            "step_id": str(step.step_id or ""),
            "attempt_no": int(attempt_no),
            "origin_interpreter_snapshot_id": str(chatgpt_snapshot.snapshot_id or ""),
            "origin_interpreter_task": str(chatgpt_snapshot.task or ""),
            "origin_interpreter_correlation_id": str(correlation_id or ""),
            "origin_block_reason_code": block_reason_code,
            "policy_profile": str(state.policy_profile or ""),
            "patch_branch": intent.branch,
            "patch_type": intent.patch_type,
            "recommended_target": intent.suggested_target,
            "justification": intent.justification,
            "trigger_reason_class": str(extractor_help.get("reason_class") or "").strip() or None,
            "status": PatchRegistryStatus.GENERATED.value,
            "status_history": [{"status": PatchRegistryStatus.GENERATED.value, "at": now, "reason": "patch_task_generated"}],
            "dispatch_approval_state": "unknown",
            "dispatch_metadata": {},
            "error_summary": None,
            "baseline_run_ref": None,
            "baseline_context_ref": None,
            "retest_run_refs": [],
            "comparator_result_ref": None,
            "comparator_outcome": "not_run",
            "operator_outcome_decision": "unknown",
            "outcome_notes": None,
            "impact_summary": {},
            "patch_task_artifact_id": str(patch_artifact.get("artifact_id") or ""),
            "created_at": now,
            "updated_at": now,
            "generated_at": now,
        }
        self._upsert_patch_registry_record(state=state, record=row)
        self._event(
            state,
            event_type="patch_registry_record_created",
            phase=step.phase,
            step_id=step.step_id,
            correlation_id=correlation_id,
            payload={
                "patch_task_id": patch_task_id,
                "patch_branch": intent.branch,
                "patch_type": intent.patch_type,
                "attempt_no": int(attempt_no),
                "artifact_id": str(patch_artifact.get("artifact_id") or ""),
                "origin_interpreter_snapshot_id": str(chatgpt_snapshot.snapshot_id or ""),
            },
        )
        return row

    def _patch_registry_update_status(
        self,
        *,
        state: RunSessionState,
        patch_task_id: str,
        status: str,
        reason: str,
        correlation_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        patch_id = str(patch_task_id or "").strip()
        if not patch_id:
            return None
        row = self._get_patch_registry_record(state=state, patch_task_id=patch_id)
        if not row:
            return None
        patch_id = str(row.get("patch_task_id") or patch_id)
        old_status = str(row.get("status") or "")
        new_status = str(status or old_status or PatchRegistryStatus.GENERATED.value)
        if old_status != new_status:
            history = list(row.get("status_history") or [])
            history.append(
                {
                    "status": new_status,
                    "at": _utc_now_iso(),
                    "reason": str(reason or "").strip() or None,
                }
            )
            row["status_history"] = history[-40:]
        row["status"] = new_status
        if extra:
            row.update(dict(extra or {}))
        self._upsert_patch_registry_record(state=state, record=row)
        self._event(
            state,
            event_type="patch_registry_status_updated",
            phase=str(row.get("phase") or state.current_phase or ""),
            step_id=str(row.get("step_id") or state.current_step_id or ""),
            correlation_id=correlation_id,
            payload={
                "patch_task_id": patch_id,
                "old_status": old_status,
                "new_status": new_status,
                "reason": str(reason or ""),
            },
        )
        return row

    def _maybe_chain_patch_task(
        self,
        *,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        trigger: str,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
        chatgpt_snapshot: ChatGPTSnapshot,
        correlation_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not bool(getattr(self.feature_flags, "patch_chaining_enabled", True)):
            return None

        intent = self._extract_patch_task_intent(chatgpt_snapshot=chatgpt_snapshot)
        if not intent.should_create:
            self._event(
                state,
                event_type="patch_recommendation_not_actionable",
                phase=step.phase,
                step_id=step.step_id,
                correlation_id=correlation_id,
                payload={
                    "attempt_no": int(attempt_no),
                    "interpreter_snapshot_id": chatgpt_snapshot.snapshot_id,
                    "status": str(chatgpt_snapshot.status or ""),
                    "recommended_branch": str(_as_dict(dict(chatgpt_snapshot.prompt_package or {}).get("interpreter_structured")).get("recommended_branch") or ""),
                    "requested_mode": chatgpt_snapshot.requested_mode,
                    "fallback_used": chatgpt_snapshot.fallback_used,
                },
            )
            return None

        self._event(
            state,
            event_type="patch_recommendation_detected",
            phase=step.phase,
            step_id=step.step_id,
            correlation_id=correlation_id,
            payload={
                "attempt_no": int(attempt_no),
                "interpreter_snapshot_id": chatgpt_snapshot.snapshot_id,
                "branch": intent.branch,
                "patch_type": intent.patch_type,
                "suggested_target": intent.suggested_target,
                "justification": intent.justification,
                "interpreter_model": chatgpt_snapshot.model,
                "interpreter_source": chatgpt_snapshot.source,
                "requested_mode": chatgpt_snapshot.requested_mode,
                "fallback_used": chatgpt_snapshot.fallback_used,
            },
        )

        intent_hash = _config_fingerprint(
            {
                "branch": intent.branch,
                "patch_type": intent.patch_type,
                "target": intent.suggested_target,
                "justification": intent.justification,
            }
        )[:16]
        idem_key = f"patchgen:{state.run_id}:{step.step_id}:{attempt_no}:{intent_hash}"
        cached = self._idempotency_get(idem_key)
        if cached and str(cached.get("status") or "") == "completed":
            cached_artifact = _as_dict(cached.get("result_payload")).get("patch_artifact")
            if isinstance(cached_artifact, dict) and cached_artifact:
                self._event(
                    state,
                    event_type="patch_task_generation_completed",
                    phase=step.phase,
                    step_id=step.step_id,
                    correlation_id=correlation_id,
                    payload={
                        "attempt_no": int(attempt_no),
                        "idempotency_replay": True,
                        "patch_task_artifact_id": str(cached_artifact.get("artifact_id") or ""),
                        "branch": intent.branch,
                        "patch_type": intent.patch_type,
                    },
                )
                return dict(cached_artifact)
            return None

        self._idempotency_put(
            key=idem_key,
            run_id=state.run_id,
            scope="patch_task_generation",
            action=step.step_id,
            payload={
                "attempt_no": int(attempt_no),
                "branch": intent.branch,
                "patch_type": intent.patch_type,
            },
            result_payload={"status": "pending"},
            status="pending",
        )
        self._event(
            state,
            event_type="patch_task_generation_started",
            phase=step.phase,
            step_id=step.step_id,
            correlation_id=correlation_id,
            payload={
                "attempt_no": int(attempt_no),
                "branch": intent.branch,
                "patch_type": intent.patch_type,
                "trigger": trigger,
                "interpreter_snapshot_id": chatgpt_snapshot.snapshot_id,
            },
        )

        try:
            response, meta = self._generate_patch_task_payload(
                state=state,
                step=step,
                attempt_no=attempt_no,
                trigger=trigger,
                exec_result=exec_result,
                validator_result=validator_result,
                ai_snapshot=ai_snapshot,
                chatgpt_snapshot=chatgpt_snapshot,
                intent=intent,
            )
        except Exception as exc:
            self._idempotency_put(
                key=idem_key,
                run_id=state.run_id,
                scope="patch_task_generation",
                action=step.step_id,
                payload={"attempt_no": int(attempt_no), "branch": intent.branch, "patch_type": intent.patch_type},
                result_payload={"status": "failed", "error": str(exc)},
                status="failed",
            )
            self._event(
                state,
                event_type="patch_task_generation_failed",
                phase=step.phase,
                step_id=step.step_id,
                correlation_id=correlation_id,
                payload={
                    "attempt_no": int(attempt_no),
                    "branch": intent.branch,
                    "patch_type": intent.patch_type,
                    "error": str(exc),
                },
            )
            return None

        patch_task = _as_dict(response.get("patch_task"))
        prompt_artifact = self._build_patch_prompt_artifact(
            state=state,
            step=step,
            attempt_no=attempt_no,
            intent=intent,
            patch_task_response=dict(response or {}),
        )
        dispatch_target_meta = self._dispatch_target_metadata(prompt_artifact.get("target") or intent.suggested_target)
        artifact = {
            "artifact_id": str(uuid4()),
            "artifact_type": "patch_task",
            "created_at": _utc_now_iso(),
            "run_id": state.run_id,
            "trace_id": str(state.trace_id or ""),
            "phase": step.phase,
            "step_id": step.step_id,
            "attempt_no": int(attempt_no),
            "policy_profile": str(state.policy_profile or ""),
            "interpreter_ref": {
                "task": chatgpt_snapshot.task,
                "trigger": chatgpt_snapshot.trigger,
                "snapshot_id": chatgpt_snapshot.snapshot_id,
                "branch": intent.branch,
                "patch_type": intent.patch_type,
                "suggested_target": intent.suggested_target,
                "justification": intent.justification,
            },
            "generator_ref": {
                "task": "hades_patch_task_generator",
                "model": (str(meta.get("model")) if meta.get("model") else None),
                "latency_ms": (int(meta.get("latency_ms")) if meta.get("latency_ms") is not None else None),
                "token_usage": dict(meta.get("token_usage") or {}),
                "schema_name": (str(meta.get("schema_name")) if meta.get("schema_name") else None),
                "snapshot_id": (str(meta.get("snapshot_id")) if meta.get("snapshot_id") else None),
                "source": (str(meta.get("source")) if meta.get("source") else None),
                "requested_mode": (str(meta.get("requested_mode")) if meta.get("requested_mode") else None),
                "fallback_used": (
                    bool(meta.get("fallback_used"))
                    if meta.get("fallback_used") is not None
                    else False
                ),
                "prompt_package": dict(meta.get("prompt_package") or {}),
            },
            "prompt_artifact": dict(prompt_artifact),
            "codex_prompt_artifact_id": str(prompt_artifact.get("artifact_id") or ""),
            "dispatch_target": dispatch_target_meta.get("target"),
            "configured_provider_name": dispatch_target_meta.get("configured_provider_name"),
            "configured_model_name": dispatch_target_meta.get("configured_model_name"),
            "patch_task_response": dict(response or {}),
            "patch_chain_trace": {
                "interpreter_called": True,
                "interpreter_result_received": True,
                "patch_recommendation_detected": True,
                "patch_task_created": True,
                "dispatch_attempted": False,
                "dispatch_block_reason": None,
                "codex_dispatch_status": "not_requested",
                "configured_provider_name": dispatch_target_meta.get("configured_provider_name"),
                "configured_model_name": dispatch_target_meta.get("configured_model_name"),
                "fallback_used": (
                    bool(meta.get("fallback_used"))
                    if meta.get("fallback_used") is not None
                    else False
                ),
            },
        }

        dispatch_decision = self._evaluate_patch_dispatch_policy(
            state=state,
            step=step,
            attempt_no=attempt_no,
            correlation_id=correlation_id,
            intent=intent,
            patch_artifact=artifact,
        )
        trace = _as_dict(artifact.get("patch_chain_trace"))
        trace["dispatch_attempted"] = str(dispatch_decision.get("dispatch_state") or "") in {"dispatched", "failed"}
        trace["dispatch_block_reason"] = dispatch_decision.get("dispatch_block_reason")
        trace["codex_dispatch_status"] = dispatch_decision.get("codex_dispatch_status") or dispatch_decision.get("dispatch_state")
        trace["configured_provider_name"] = dispatch_decision.get("configured_provider_name") or trace.get("configured_provider_name")
        trace["configured_model_name"] = dispatch_decision.get("configured_model_name") or trace.get("configured_model_name")
        trace["fallback_used"] = (
            bool(dispatch_decision.get("fallback_used"))
            if dispatch_decision.get("fallback_used") is not None
            else bool(trace.get("fallback_used"))
        )
        artifact["patch_chain_trace"] = trace
        artifact["dispatch_decision"] = dict(dispatch_decision or {})
        patch_row = self._create_patch_registry_record(
            state=state,
            step=step,
            attempt_no=attempt_no,
            correlation_id=correlation_id,
            validator_result=validator_result,
            ai_snapshot=ai_snapshot,
            chatgpt_snapshot=chatgpt_snapshot,
            intent=intent,
            patch_artifact=artifact,
        )
        patch_task_id = str(patch_row.get("patch_task_id") or "")
        artifact["patch_task_id"] = patch_task_id
        if bool(dispatch_decision.get("requires_approval")):
            self._patch_registry_update_status(
                state=state,
                patch_task_id=patch_task_id,
                status=PatchRegistryStatus.PENDING_APPROVAL.value,
                reason="patch_dispatch_policy_gate",
                correlation_id=correlation_id,
                extra={
                    "dispatch_approval_state": "pending",
                    "dispatch_metadata": dict(dispatch_decision or {}),
                },
            )
        else:
            if str(dispatch_decision.get("dispatch_state") or "") == "dispatched":
                self._patch_registry_update_status(
                    state=state,
                    patch_task_id=patch_task_id,
                    status=PatchRegistryStatus.DISPATCHED.value,
                    reason="patch_dispatch_requested",
                    correlation_id=correlation_id,
                    extra={
                        "dispatch_approval_state": "not_required",
                        "dispatch_metadata": dict(dispatch_decision or {}),
                    },
                )
                self._patch_registry_update_status(
                    state=state,
                    patch_task_id=patch_task_id,
                    status=PatchRegistryStatus.RETEST_PENDING.value,
                    reason="awaiting_retest_after_dispatch",
                    correlation_id=correlation_id,
                )
            else:
                self._patch_registry_update_status(
                    state=state,
                    patch_task_id=patch_task_id,
                    status=PatchRegistryStatus.FAILED.value,
                    reason="patch_dispatch_failed",
                    correlation_id=correlation_id,
                    extra={
                        "dispatch_approval_state": "not_required",
                        "dispatch_metadata": dict(dispatch_decision or {}),
                        "error_summary": str(dispatch_decision.get("error_summary") or ""),
                    },
                )

        self._idempotency_put(
            key=idem_key,
            run_id=state.run_id,
            scope="patch_task_generation",
            action=step.step_id,
            payload={"attempt_no": int(attempt_no), "branch": intent.branch, "patch_type": intent.patch_type},
            result_payload={
                "status": "completed",
                "patch_artifact": dict(artifact),
                "dispatch_decision": dict(dispatch_decision or {}),
            },
            status="completed",
        )
        dispatch_state = str(dispatch_decision.get("dispatch_state") or "skipped")
        if bool(dispatch_decision.get("requires_approval")):
            dispatch_status = "pending_approval"
        elif dispatch_state == "dispatched":
            dispatch_status = "dispatched"
        elif dispatch_state == "failed":
            dispatch_status = "failed"
        else:
            dispatch_status = "skipped"
        self._event(
            state,
            event_type="patch_task_generation_completed",
            phase=step.phase,
            step_id=step.step_id,
            correlation_id=correlation_id,
            payload={
                "attempt_no": int(attempt_no),
                "patch_task_artifact_id": str(artifact.get("artifact_id") or ""),
                "patch_task_id": patch_task_id,
                "patch_task_created": True,
                "patch_dispatch_status": dispatch_status,
                "patch_dispatch_failure_reason": (
                    str(dispatch_decision.get("dispatch_block_reason") or dispatch_decision.get("error_summary") or "")
                    or None
                ),
                "branch": intent.branch,
                "patch_type": intent.patch_type,
                "suggested_target": intent.suggested_target,
                "patch_title": str(patch_task.get("title") or ""),
                "operator_confirmation_required": bool(response.get("operator_confirmation_required", True)),
                "codex_prompt_artifact_id": str(prompt_artifact.get("artifact_id") or ""),
                "configured_provider_name": dispatch_target_meta.get("configured_provider_name"),
                "configured_model_name": dispatch_target_meta.get("configured_model_name"),
                "fallback_used": (
                    bool(meta.get("fallback_used"))
                    if meta.get("fallback_used") is not None
                    else False
                ),
            },
        )
        return artifact

    @staticmethod
    def _extract_patch_task_intent(*, chatgpt_snapshot: ChatGPTSnapshot) -> PatchTaskIntent:
        patch_branch_to_type = {
            "patch_extractor": "extractor",
            "patch_detector_scoring": "detector_scoring",
            "patch_diagnostics": "diagnostics",
        }
        prompt_package = dict(chatgpt_snapshot.prompt_package or {})
        structured = _as_dict(prompt_package.get("interpreter_structured"))
        patch_rec = _as_dict(structured.get("patch_task_recommendation"))

        branch = str(structured.get("recommended_branch") or "").strip() or None
        for token in list(chatgpt_snapshot.priorities or []):
            txt = str(token or "").strip()
            if txt.startswith("branch:") and not branch:
                branch = txt.split(":", 1)[1].strip() or None

        patch_type = str(patch_rec.get("patch_type") or "").strip() or None
        suggested_target = str(patch_rec.get("suggested_target") or "").strip() or None
        should_create = bool(_handoff_to_bool(patch_rec.get("should_create_patch_task")))
        justification = str(patch_rec.get("justification") or "").strip() or None

        for token in list(chatgpt_snapshot.priorities or []):
            txt = str(token or "").strip()
            if txt.startswith("patch:"):
                parts = txt.split(":", 2)
                if len(parts) >= 2 and not patch_type:
                    patch_type = parts[1].strip() or None
                if len(parts) >= 3 and not suggested_target:
                    suggested_target = parts[2].strip() or None
                should_create = True
                break

        if not patch_type and branch in patch_branch_to_type:
            patch_type = patch_branch_to_type[branch]
        if not suggested_target:
            suggested_target = "either"
        if not justification:
            for row in list(chatgpt_snapshot.risk_notes or []):
                txt = str(row or "").strip()
                if txt.lower().startswith("patch_recommendation "):
                    justification = txt.split(" ", 1)[1].strip() or None
                    break

        if branch in patch_branch_to_type:
            should_create = True

        return PatchTaskIntent(
            should_create=bool(should_create),
            branch=branch,
            patch_type=patch_type,
            suggested_target=suggested_target,
            justification=justification,
        )

    def _generate_patch_task_payload(
        self,
        *,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        trigger: str,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
        chatgpt_snapshot: ChatGPTSnapshot,
        intent: PatchTaskIntent,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        generator = getattr(self.chatgpt_interpreter, "generate_patch_task", None)
        if not callable(generator):
            raise RuntimeError("chatgpt_interpreter_missing_patch_generator")
        out = generator(
            state=state,
            step=step,
            attempt_no=int(attempt_no),
            trigger=str(trigger or ""),
            exec_result=exec_result,
            validator_result=validator_result,
            ai_snapshot=ai_snapshot,
            chatgpt_snapshot=chatgpt_snapshot,
            patch_intent={
                "branch": intent.branch,
                "patch_type": intent.patch_type,
                "suggested_target": intent.suggested_target,
                "justification": intent.justification,
            },
        )
        if not isinstance(out, dict):
            raise RuntimeError("patch_task_generator_invalid_response")
        return dict(out.get("response") or {}), dict(out.get("meta") or {})

    def _evaluate_patch_dispatch_policy(
        self,
        *,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        correlation_id: Optional[str],
        intent: PatchTaskIntent,
        patch_artifact: Dict[str, Any],
    ) -> Dict[str, Any]:
        profile = _normalize_profile(state.policy_profile)
        target_meta = self._dispatch_target_metadata(intent.suggested_target)
        auto_dispatch_requested = bool(
            getattr(self.feature_flags, "patch_auto_dispatch_enabled", False)
            and profile == PolicyProfile.AGGRESSIVE_SUPERVISED.value
        )
        policy_check = check_permission(
            ROLE_RUNTIME,
            ACTION_DISPATCH_PATCH_TASK,
            operator_confirmed=bool(auto_dispatch_requested),
        )
        dispatch_allowed = bool(auto_dispatch_requested and policy_check.allowed)
        requires_approval = not dispatch_allowed

        decision = {
            "profile": profile,
            "auto_dispatch_requested": bool(auto_dispatch_requested),
            "dispatch_allowed": bool(dispatch_allowed),
            "requires_approval": bool(requires_approval),
            "policy_code": str(policy_check.code or ""),
            "policy_message": str(policy_check.message or ""),
            "codex_prompt_artifact_id": str(patch_artifact.get("codex_prompt_artifact_id") or ""),
            "configured_provider_name": target_meta.get("configured_provider_name"),
            "configured_model_name": target_meta.get("configured_model_name"),
            "dispatch_target": target_meta.get("target"),
        }
        patch_task_id_hint = str(
            patch_artifact.get("patch_task_id")
            or patch_artifact.get("artifact_id")
            or ""
        ).strip()
        self._event(
            state,
            event_type="patch_dispatch_evaluation",
            phase=step.phase,
            step_id=step.step_id,
            correlation_id=correlation_id,
            payload={
                "attempt_no": int(attempt_no),
                "patch_task_artifact_id": str(patch_artifact.get("artifact_id") or ""),
                "patch_type": intent.patch_type,
                **decision,
            },
        )

        if requires_approval:
            approval = self.approval_queue.create_item(
                run_id=state.run_id,
                phase=step.phase,
                step_id=step.step_id,
                approval_type=ApprovalType.DISPATCH_PATCH_TASK,
                evidence_payload={
                    "patch_task_artifact_ref": {
                        "artifact_id": str(patch_artifact.get("artifact_id") or ""),
                        "artifact_type": str(patch_artifact.get("artifact_type") or "patch_task"),
                    },
                    "patch_task_id": patch_task_id_hint,
                    "patch_branch": intent.branch,
                    "patch_type": intent.patch_type,
                    "suggested_target": intent.suggested_target,
                    "interpreter_ref": dict(patch_artifact.get("interpreter_ref") or {}),
                    "dispatch_decision": dict(decision),
                },
                risk_summary="Patch-task dispatch requires explicit operator confirmation.",
                recommended_action="approve_patch_task_dispatch_or_reject",
            )
            state.approvals = self.approval_queue.list_all(state.run_id)
            self._sync_approval_state(state)
            self._event(
                state,
                event_type="approval_item_created",
                phase=step.phase,
                step_id=step.step_id,
                correlation_id=correlation_id,
                payload={
                    "approval_id": approval.approval_id,
                    "approval_type": approval.approval_type.value,
                    "status": approval.status.value,
                    "reason": "patch_dispatch_policy_gate",
                },
            )
            self._event(
                state,
                event_type="patch_dispatch_pending_approval",
                phase=step.phase,
                step_id=step.step_id,
                correlation_id=correlation_id,
                payload={
                    "approval_id": approval.approval_id,
                    "patch_task_artifact_id": str(patch_artifact.get("artifact_id") or ""),
                    "patch_type": intent.patch_type,
                    "profile": profile,
                    "codex_prompt_artifact_id": str(patch_artifact.get("codex_prompt_artifact_id") or ""),
                    "configured_provider_name": target_meta.get("configured_provider_name"),
                    "configured_model_name": target_meta.get("configured_model_name"),
                },
            )
            return {
                "dispatch_state": "pending_approval",
                "codex_dispatch_status": "pending_approval",
                "dispatch_block_reason": "approval_required",
                "fallback_used": False,
                "approval_id": approval.approval_id,
                **decision,
            }

        patch_task_id = str(patch_artifact.get("patch_task_id") or patch_artifact.get("artifact_id") or "").strip()
        dispatch_result = self._execute_patch_dispatch(
            state=state,
            phase=step.phase,
            step_id=step.step_id,
            correlation_id=correlation_id,
            patch_task_id=patch_task_id,
            patch_artifact=patch_artifact,
        )
        return {**decision, **dict(dispatch_result or {})}

    def _resolve_patch_dispatch_approval(
        self,
        *,
        state: RunSessionState,
        item: ApprovalItem,
        resolution_payload: Dict[str, Any],
    ) -> RunSessionState:
        patch_ref = _as_dict(item.evidence_payload).get("patch_task_artifact_ref")
        patch_task_artifact_id = str(_as_dict(patch_ref).get("artifact_id") or "")
        patch_task_id = str(_as_dict(item.evidence_payload).get("patch_task_id") or patch_task_artifact_id or "").strip()
        decision = str(item.status.value or "")
        self._event(
            state,
            event_type="patch_dispatch_evaluation",
            phase=item.phase,
            step_id=item.step_id,
            payload={
                "approval_id": item.approval_id,
                "approval_status": decision,
                "patch_task_artifact_id": patch_task_artifact_id,
                "resolved_by": item.operator_id,
            },
        )

        if item.status == ApprovalStatus.APPROVED:
            policy = check_permission(ROLE_OPERATOR, ACTION_DISPATCH_PATCH_TASK, operator_confirmed=True)
            if not policy.allowed:
                if patch_task_id:
                    self._patch_registry_update_status(
                        state=state,
                        patch_task_id=patch_task_id,
                        status=PatchRegistryStatus.FAILED.value,
                        reason="patch_dispatch_policy_denied_after_approval",
                        extra={"error_summary": str(policy.message or "")},
                    )
                self._event(
                    state,
                    event_type="patch_dispatch_failed",
                    phase=item.phase,
                    step_id=item.step_id,
                    payload={
                        "approval_id": item.approval_id,
                        "patch_task_artifact_id": patch_task_artifact_id,
                        "error": policy.message,
                        "error_code": policy.code,
                    },
                )
                return state

            patch_artifact: Dict[str, Any] = {}
            for artifacts in list(state.artifacts.values()):
                for candidate in list(artifacts or []):
                    row = _as_dict(candidate)
                    if str(row.get("artifact_id") or "") == patch_task_artifact_id:
                        patch_artifact = row
                        break
                if patch_artifact:
                    break
            dispatch_result = self._execute_patch_dispatch(
                state=state,
                phase=item.phase,
                step_id=item.step_id,
                correlation_id=None,
                patch_task_id=patch_task_id,
                patch_artifact=patch_artifact,
                approval_id=item.approval_id,
                operator_id=item.operator_id,
                operator_role=item.operator_role,
            )
            if patch_task_id:
                if str(dispatch_result.get("dispatch_state") or "") == "dispatched":
                    self._patch_registry_update_status(
                        state=state,
                        patch_task_id=patch_task_id,
                        status=PatchRegistryStatus.DISPATCHED.value,
                        reason="patch_dispatch_approved",
                        extra={
                            "dispatch_approval_state": "approved",
                            "dispatch_metadata": dict(dispatch_result or {}),
                        },
                    )
                    self._patch_registry_update_status(
                        state=state,
                        patch_task_id=patch_task_id,
                        status=PatchRegistryStatus.RETEST_PENDING.value,
                        reason="awaiting_retest_after_approved_dispatch",
                    )
                else:
                    self._patch_registry_update_status(
                        state=state,
                        patch_task_id=patch_task_id,
                        status=PatchRegistryStatus.FAILED.value,
                        reason="patch_dispatch_failed_after_approval",
                        extra={
                            "dispatch_approval_state": "approved",
                            "dispatch_metadata": dict(dispatch_result or {}),
                            "error_summary": str(dispatch_result.get("error_summary") or ""),
                        },
                    )
        elif patch_task_id:
            self._patch_registry_update_status(
                state=state,
                patch_task_id=patch_task_id,
                status=PatchRegistryStatus.FAILED.value,
                reason="patch_dispatch_rejected",
                extra={"dispatch_approval_state": str(item.status.value or "rejected")},
            )
        return state

    def _find_open_diversion_for_step(self, state: RunSessionState, step_id: str) -> Optional[Dict[str, Any]]:
        sid = str(step_id or "")
        for row in reversed(list(state.diversion_stack or [])):
            if str(row.get("origin_step_id") or "") == sid and str(row.get("status") or "") == "open":
                return row
        return None

    def _next_step_after_success(self, state: RunSessionState, step: StepDefinition) -> Optional[str]:
        forced = dict(state.resume_context.get("forced_next_step_after") or {})
        after_step_id = str(forced.get("after_step_id") or "")
        next_step_id = str(forced.get("next_step_id") or "")
        if after_step_id and next_step_id and step.step_id == after_step_id:
            state.resume_context.pop("forced_next_step_after", None)
            return next_step_id
        canonical_next = _phase3_canonical_next_step(
            step.step_id,
            step20_summary=(
                dict(state.resume_context.get("latest_step20_summary") or {})
                if step.step_id == STEP_P3_2_STEP20
                else None
            ),
        )
        if canonical_next is not None:
            return canonical_next
        return step.next_step_on_success

    def _is_manual_step_resolved(self, state: RunSessionState, step_id: str) -> bool:
        rows = dict(state.resume_context.get("manual_resolutions") or {})
        return bool(rows.get(str(step_id), False))

    def _apply_manual_resolution(
        self,
        state: RunSessionState,
        *,
        step_id: str,
        operator_id: Optional[str],
        operator_role: Optional[str],
        operator_notes: Optional[str],
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> None:
        sid = str(step_id)
        manual_map = dict(state.resume_context.get("manual_resolutions") or {})
        signature_payload = {
            "run_id": state.run_id,
            "step_id": sid,
            "operator_id": operator_id,
            "operator_role": operator_role,
            "operator_notes": operator_notes,
            "payload": payload,
        }
        manual_map[sid] = {
            "resolved": True,
            "operator_id": (str(operator_id).strip() if operator_id else None),
            "operator_role": (str(operator_role).strip() if operator_role else None),
            "operator_notes": str(operator_notes or "").strip() or None,
            "payload": dict(payload or {}),
            "resolved_at": _utc_now_iso(),
            "decision_signature": self._sign_operator_decision(signature_payload),
        }
        state.resume_context["manual_resolutions"] = manual_map

        step = self.step_registry.get(sid)
        if step is not None:
            now = _utc_now_iso()
            rec = StepExecutionRecord(
                run_id=state.run_id,
                phase=step.phase,
                step_id=sid,
                attempt_no=int(state.attempt_counters.get(sid) or 1),
                status="manual_resolved",
                executor_result_summary={
                    "operator_id": manual_map[sid]["operator_id"],
                    "operator_role": manual_map[sid]["operator_role"],
                    "operator_notes": manual_map[sid]["operator_notes"],
                    "resolution_payload": dict(payload or {}),
                    "decision_signature": manual_map[sid]["decision_signature"],
                },
                validator_result={"status": "pass", "gate_passed": True},
                ai_bot_snapshot={},
                chatgpt_snapshot=None,
                block_reason=None,
                artifacts=list(payload.get("artifacts") or []),
                timings={"started_at": now, "ended_at": now},
                created_at=now,
                idempotency_key=(str(idempotency_key) if idempotency_key else None),
            )
            state.step_execution_records.append(rec)
            if payload.get("artifacts"):
                state.artifacts.setdefault(sid, [])
                state.artifacts[sid].extend(list(payload.get("artifacts") or []))
            if self.db_store.available:
                self.db_store.insert_step_attempt(asdict(rec), trace_id=state.trace_id)

    def _operator_roles_from_state(self, state: RunSessionState) -> List[str]:
        roles = list((state.operator_context or {}).get("operator_roles") or [])
        return [str(r).strip().lower() for r in roles if str(r).strip()]

    def _require_operator_identity(
        self,
        state: RunSessionState,
        *,
        operator_id: Optional[str],
        operator_role: Optional[str],
        action: str,
    ) -> None:
        oid = str(operator_id or "").strip()
        if not oid:
            raise PermissionError(f"operator_id is required for {action}")

        role = str(operator_role or "").strip().lower()
        roles = self._operator_roles_from_state(state)
        if role:
            roles = list(dict.fromkeys([role, *roles]))
        if not roles:
            raise PermissionError(f"operator role is required for {action}")

        allowed = set(self.allowed_operator_roles or [])
        if not any(r in allowed for r in roles):
            raise PermissionError(
                f"operator role not allowed for {action}; required one of {sorted(allowed)}"
            )

    def _sign_operator_decision(self, payload: Dict[str, Any]) -> str:
        body = _to_jsonable_text(payload or {})
        digest = hmac.new(
            self.operator_signing_secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"sha256:{digest}"

    def _idempotency_get(self, key: str) -> Optional[Dict[str, Any]]:
        k = str(key or "").strip()
        if not k:
            return None
        cached = self._idempotency_cache.get(k)
        if cached is not None:
            return dict(cached)
        if self.db_store.available:
            try:
                row = self.db_store.get_idempotency(k)
                if row:
                    normalized = dict(row)
                    normalized.setdefault("result_payload", dict(row.get("result_payload") or {}))
                    self._idempotency_cache[k] = dict(normalized)
                    return normalized
            except Exception:
                return None
        return None

    def _idempotency_put(
        self,
        *,
        key: str,
        run_id: str,
        scope: str,
        action: str,
        payload: Dict[str, Any],
        result_payload: Dict[str, Any],
        status: str = "completed",
    ) -> None:
        k = str(key or "").strip()
        if not k:
            return
        payload_hash = hashlib.sha256(_to_jsonable_text(payload).encode("utf-8")).hexdigest()
        row = {
            "idempotency_key": k,
            "run_id": str(run_id),
            "scope": str(scope),
            "action": str(action),
            "status": str(status or "completed"),
            "payload_hash": payload_hash,
            "result_payload": dict(result_payload or {}),
            "updated_at": _utc_now_iso(),
        }
        self._idempotency_cache[k] = dict(row)
        if self.db_store.available:
            try:
                self.db_store.upsert_idempotency(
                    key=k,
                    run_id=str(run_id),
                    scope=str(scope),
                    action=str(action),
                    status=str(status or "completed"),
                    payload_hash=payload_hash,
                    result_payload=dict(result_payload or {}),
                )
            except Exception:
                return

    # ------------------------------------------------------------
    # Coercion / helpers
    # ------------------------------------------------------------
    def _record_step_attempt(
        self,
        *,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        status: str,
        started_at: str,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
        chatgpt_snapshot: Optional[ChatGPTSnapshot],
        block_reason: Optional[BlockReason],
        idempotency_key: Optional[str],
    ) -> None:
        ended_at = _utc_now_iso()
        rec = StepExecutionRecord(
            run_id=state.run_id,
            phase=step.phase,
            step_id=step.step_id,
            attempt_no=int(attempt_no),
            status=str(status),
            executor_result_summary=dict(exec_result.summary or {}),
            validator_result=asdict(validator_result),
            ai_bot_snapshot=asdict(ai_snapshot),
            chatgpt_snapshot=(asdict(chatgpt_snapshot) if chatgpt_snapshot else None),
            block_reason=(asdict(block_reason) if block_reason else None),
            artifacts=list(exec_result.artifacts or []),
            timings={
                "started_at": started_at,
                "ended_at": ended_at,
            },
            created_at=ended_at,
            idempotency_key=(str(idempotency_key) if idempotency_key else None),
        )
        state.step_execution_records.append(rec)
        state.artifacts.setdefault(step.step_id, [])
        state.artifacts[step.step_id].extend(list(exec_result.artifacts or []))
        state.updated_at = ended_at
        self._persist_state(state)
        if self.db_store.available:
            self.db_store.insert_step_attempt(asdict(rec), trace_id=state.trace_id)

    def _event(
        self,
        state: RunSessionState,
        *,
        event_type: str,
        phase: Optional[str],
        step_id: Optional[str],
        payload: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
    ) -> None:
        event = PipelineEvent(
            event_id=str(uuid4()),
            event_type=str(event_type),
            timestamp=_utc_now_iso(),
            run_id=state.run_id,
            phase=(str(phase) if phase is not None else None),
            step_id=(str(step_id) if step_id is not None else None),
            payload=dict(payload or {}),
            correlation_id=str(correlation_id or uuid4()),
            trace_id=str(state.trace_id),
        )
        state.events.append(event)
        state.updated_at = event.timestamp
        self._persist_state(state)
        if self.db_store.available:
            self.db_store.insert_event(asdict(event))

    def _persist_state(self, state: RunSessionState) -> None:
        if not self.persist_runs:
            return
        if self.persistence_backend in {"db", "hybrid"} and self.db_store.available:
            try:
                self.db_store.upsert_run_state(self._serialize_state(state))
                self._sync_approval_state(state)
            except Exception:
                pass
        if self.persistence_backend == "db":
            return
        try:
            data = self._serialize_state(state)
            path = self.persistence_dir / f"{state.run_id}.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            return

    def _serialize_state(self, state: RunSessionState) -> Dict[str, Any]:
        return _jsonable(asdict(state))

    def _load_persisted_runs(self) -> None:
        if not self.persist_runs:
            return
        loaded: Dict[str, RunSessionState] = {}
        if self.persistence_backend in {"db", "hybrid"} and self.db_store.available:
            for raw in self.db_store.list_run_states(limit=500):
                try:
                    state = self._deserialize_state(raw)
                except Exception:
                    continue
                loaded[state.run_id] = state

        if self.persistence_backend in {"json", "hybrid"}:
            if self.persistence_dir.exists():
                for path in sorted(self.persistence_dir.glob("*.json")):
                    try:
                        raw = json.loads(path.read_text(encoding="utf-8"))
                        state = self._deserialize_state(raw)
                    except Exception:
                        continue
                    loaded.setdefault(state.run_id, state)

        for state in loaded.values():
            self.runs[state.run_id] = state
            self.approval_queue.restore_items(state.run_id, state.approvals)

    def _sync_approval_state(self, state: RunSessionState) -> None:
        if not self.db_store.available:
            return
        for item in list(state.approvals or []):
            try:
                self.db_store.upsert_approval(asdict(item), trace_id=state.trace_id)
            except Exception:
                continue

    def _deserialize_state(self, raw: Dict[str, Any]) -> RunSessionState:
        state = RunSessionState(
            run_id=str(raw.get("run_id") or uuid4()),
            pipeline_scope=dict(raw.get("pipeline_scope") or {}),
            policy_profile=str(raw.get("policy_profile") or PolicyProfile.BALANCED.value),
            status=str(raw.get("status") or RunStatus.PAUSED.value),
            current_phase=(str(raw.get("current_phase")) if raw.get("current_phase") is not None else None),
            current_step_id=(str(raw.get("current_step_id")) if raw.get("current_step_id") is not None else None),
            attempt_counters=dict(raw.get("attempt_counters") or {}),
            diversion_stack=list(raw.get("diversion_stack") or []),
            resume_context=dict(raw.get("resume_context") or {}),
            started_at=str(raw.get("started_at") or _utc_now_iso()),
            updated_at=str(raw.get("updated_at") or _utc_now_iso()),
            completed_at=(str(raw.get("completed_at")) if raw.get("completed_at") else None),
            operator_context=dict(raw.get("operator_context") or {}),
            trace_id=str(raw.get("trace_id") or uuid4()),
            artifacts=dict(raw.get("artifacts") or {}),
            patch_registry=dict(raw.get("patch_registry") or {}),
        )

        for row in list(raw.get("step_execution_records") or []):
            state.step_execution_records.append(
                StepExecutionRecord(
                    run_id=str(row.get("run_id") or state.run_id),
                    phase=str(row.get("phase") or ""),
                    step_id=str(row.get("step_id") or ""),
                    attempt_no=int(row.get("attempt_no") or 0),
                    status=str(row.get("status") or ""),
                    executor_result_summary=dict(row.get("executor_result_summary") or {}),
                    validator_result=dict(row.get("validator_result") or {}),
                    ai_bot_snapshot=dict(row.get("ai_bot_snapshot") or {}),
                    chatgpt_snapshot=(dict(row.get("chatgpt_snapshot")) if row.get("chatgpt_snapshot") else None),
                    block_reason=(dict(row.get("block_reason")) if row.get("block_reason") else None),
                    artifacts=list(row.get("artifacts") or []),
                    timings=dict(row.get("timings") or {}),
                    created_at=str(row.get("created_at") or _utc_now_iso()),
                    idempotency_key=(str(row.get("idempotency_key")) if row.get("idempotency_key") else None),
                )
            )

        for row in list(raw.get("events") or []):
            state.events.append(
                PipelineEvent(
                    event_id=str(row.get("event_id") or uuid4()),
                    event_type=str(row.get("event_type") or ""),
                    timestamp=str(row.get("timestamp") or _utc_now_iso()),
                    run_id=str(row.get("run_id") or state.run_id),
                    phase=(str(row.get("phase")) if row.get("phase") is not None else None),
                    step_id=(str(row.get("step_id")) if row.get("step_id") is not None else None),
                    payload=dict(row.get("payload") or {}),
                    correlation_id=str(row.get("correlation_id") or uuid4()),
                    trace_id=str(row.get("trace_id") or state.trace_id),
                )
            )

        for row in list(raw.get("approvals") or []):
            try:
                approval_type = ApprovalType(str(row.get("approval_type")))
            except Exception:
                approval_type = ApprovalType.APPROVE_FINAL_ROUTE_OR_MERGE_BIND
            try:
                status = ApprovalStatus(str(row.get("status")))
            except Exception:
                status = ApprovalStatus.PENDING
            state.approvals.append(
                ApprovalItem(
                    approval_id=str(row.get("approval_id") or uuid4()),
                    run_id=str(row.get("run_id") or state.run_id),
                    phase=str(row.get("phase") or ""),
                    step_id=str(row.get("step_id") or ""),
                    approval_type=approval_type,
                    status=status,
                    created_at=str(row.get("created_at") or _utc_now_iso()),
                    created_by_system=bool(row.get("created_by_system", True)),
                    evidence_payload=dict(row.get("evidence_payload") or {}),
                    risk_summary=str(row.get("risk_summary") or ""),
                    recommended_action=str(row.get("recommended_action") or "review"),
                    operator_decision=(str(row.get("operator_decision")) if row.get("operator_decision") else None),
                    operator_id=(str(row.get("operator_id")) if row.get("operator_id") else None),
                    operator_role=(str(row.get("operator_role")) if row.get("operator_role") else None),
                    decision_at=(str(row.get("decision_at")) if row.get("decision_at") else None),
                    decision_signature=(str(row.get("decision_signature")) if row.get("decision_signature") else None),
                )
            )
        return state

    def _coerce_executor_result(self, raw: Any) -> ExecutorResult:
        if isinstance(raw, ExecutorResult):
            return raw
        if isinstance(raw, dict):
            return ExecutorResult(
                ok=bool(raw.get("ok", True)),
                summary=dict(raw.get("summary") or raw),
                artifacts=list(raw.get("artifacts") or []),
            )
        return ExecutorResult(ok=True, summary={"raw": str(raw)}, artifacts=[])

    def _coerce_validator_result(self, raw: Any) -> ValidatorResult:
        if isinstance(raw, ValidatorResult):
            return raw
        if isinstance(raw, dict):
            code = raw.get("block_reason_code")
            parsed_code: Optional[BlockReasonCode] = None
            if code:
                try:
                    parsed_code = BlockReasonCode(str(code))
                except Exception:
                    parsed_code = None
            return ValidatorResult(
                status=str(raw.get("status") or "pass"),
                gate_passed=bool(raw.get("gate_passed", True)),
                passable_warning=bool(raw.get("passable_warning", False)),
                warnings=list(raw.get("warnings") or []),
                anomalies=list(raw.get("anomalies") or []),
                block_reason_code=parsed_code,
                summary=str(raw.get("summary") or ""),
                evidence=dict(raw.get("evidence") or {}),
                recommended_action=(str(raw.get("recommended_action")) if raw.get("recommended_action") else None),
                gate_bypass_attempted=bool(raw.get("gate_bypass_attempted", False)),
            )
        return ValidatorResult()

    def _coerce_ai_snapshot(self, raw: Any) -> AIBotSnapshot:
        if isinstance(raw, AIBotSnapshot):
            return raw
        if isinstance(raw, dict):
            metrics = dict(raw.get("metrics") or {})
            return AIBotSnapshot(
                scores=dict(raw.get("scores") or {}),
                metrics=metrics,
                warnings=list(raw.get("warnings") or []),
                anomaly_flags=list(raw.get("anomaly_flags") or []),
                proposals=dict(raw.get("proposals") or {}),
                extractor_status=dict(raw.get("extractor_status") or {}),
                extractor_help_needed=dict(
                    raw.get("extractor_help_needed")
                    or metrics.get("extractor_help_payload")
                    or {}
                ),
            )
        return AIBotSnapshot()

    def _coerce_chatgpt_snapshot(self, raw: Any) -> ChatGPTSnapshot:
        if isinstance(raw, ChatGPTSnapshot):
            return raw
        if isinstance(raw, dict):
            return ChatGPTSnapshot(
                summary=str(raw.get("summary") or ""),
                priorities=list(raw.get("priorities") or []),
                risk_notes=list(raw.get("risk_notes") or []),
                status=(str(raw.get("status")) if raw.get("status") else "ok"),
                task=(str(raw.get("task")) if raw.get("task") else None),
                trigger=(str(raw.get("trigger")) if raw.get("trigger") else None),
                model=(str(raw.get("model")) if raw.get("model") else None),
                latency_ms=(int(raw.get("latency_ms")) if raw.get("latency_ms") is not None else None),
                token_usage=dict(raw.get("token_usage") or {}),
                snapshot_id=(str(raw.get("snapshot_id")) if raw.get("snapshot_id") else None),
                source=(str(raw.get("source")) if raw.get("source") else None),
                schema_name=(str(raw.get("schema_name")) if raw.get("schema_name") else None),
                requested_mode=(str(raw.get("requested_mode")) if raw.get("requested_mode") else None),
                fallback_used=(
                    bool(raw.get("fallback_used"))
                    if raw.get("fallback_used") is not None
                    else None
                ),
                error_code=(str(raw.get("error_code")) if raw.get("error_code") else None),
                error_summary=(str(raw.get("error_summary")) if raw.get("error_summary") else None),
                prompt_package=dict(raw.get("prompt_package") or {}),
            )
        return ChatGPTSnapshot(summary=str(raw or ""), priorities=[], risk_notes=[])

    def _interpretation_trigger(self, validator_result: ValidatorResult, ai_snapshot: AIBotSnapshot) -> Optional[str]:
        if validator_result.status == "warning":
            return "warning"
        if validator_result.status == "failed":
            return "failed"
        if validator_result.status == "blocked":
            if validator_result.block_reason_code is None:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "BLOCKED_WITHOUT_REASON_CODE: validator.status='blocked' but block_reason_code is null — "
                    "this is a contract violation. Emitting trigger='failed' instead."
                )
                return "failed"
            return "blocked"
        if list(validator_result.anomalies or []) or list(ai_snapshot.anomaly_flags or []):
            return "anomaly"
        return None

    def _build_block_reason(
        self,
        *,
        step: StepDefinition,
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
        chatgpt_snapshot: Optional[ChatGPTSnapshot],
    ) -> BlockReason:
        code = validator_result.block_reason_code
        if code is None:
            code = self._fallback_block_reason_code(step, validator_result)

        diversion_rule = step.diversion_rules.get(code)
        required_approval = None
        diversion_target = None
        if diversion_rule is not None:
            required_approval = diversion_rule.approval_type
            diversion_target = diversion_rule.target_step_id
        elif step.automation_level == AutomationLevel.APPROVAL_REQUIRED and step.approval_type is not None:
            required_approval = step.approval_type

        severity = self._severity_for_code(code)
        summary = validator_result.summary or self._summary_for_code(code)
        recommended = validator_result.recommended_action or self._recommended_action_for_code(code)

        return BlockReason(
            code=code,
            severity=severity,
            summary=summary,
            validator_evidence=dict(validator_result.evidence or {}),
            ai_bot_metrics_snapshot=dict(ai_snapshot.metrics or {}),
            chatgpt_interpretation=(asdict(chatgpt_snapshot) if chatgpt_snapshot else None),
            recommended_next_action=recommended,
            required_approval_type=required_approval,
            diversion_target=diversion_target,
        )

    @staticmethod
    def _severity_for_code(code: BlockReasonCode) -> str:
        critical = {
            BlockReasonCode.GATE_BYPASS_ATTEMPT,
            BlockReasonCode.STEP20_UNMATCHED_BLOCKING,
            BlockReasonCode.STEP20_AMBIGUOUS_BLOCKING,
            BlockReasonCode.DESTRUCTIVE_CLEANUP_APPROVAL_REQUIRED,
            BlockReasonCode.MERGE_BIND_APPROVAL_REQUIRED,
        }
        high = {
            BlockReasonCode.EXTRACTION_EMPTY,
            BlockReasonCode.REPEATED_EXTRACTOR_FAILURE,
            BlockReasonCode.PHASE1_PROMOTE_NOT_COMPLETED,
            BlockReasonCode.PLACE_SET_ID_NOT_FOUND,
            BlockReasonCode.PLACE_SET_EMPTY_OR_MISSING,
            BlockReasonCode.SEMANTIC_PIPELINE_FAILED,
            BlockReasonCode.PHASE3_INVERSE_COMPLETION_BLOCKING,
            BlockReasonCode.GEOMETRY_FAILED,
            BlockReasonCode.RANKING_FAILED,
            BlockReasonCode.NORMALIZE_FAILED,
            BlockReasonCode.FEATURES_INVALID,
            BlockReasonCode.VALIDATOR_PAYLOAD_CONTRACT_MISMATCH,
        }
        if code in critical:
            return "critical"
        if code in high:
            return "high"
        return "medium"

    @staticmethod
    def _summary_for_code(code: BlockReasonCode) -> str:
        return str(code.value).replace("_", " ").title()

    @staticmethod
    def _recommended_action_for_code(code: BlockReasonCode) -> str:
        mapping = {
            BlockReasonCode.EXTRACTION_EMPTY: "retry_with_bbox_or_template",
            BlockReasonCode.EXTRACTION_LOW_COVERAGE: "retry_with_bbox_or_template",
            BlockReasonCode.REPEATED_EXTRACTOR_FAILURE: "pause_and_investigate_extractor",
            BlockReasonCode.NORMALIZE_FAILED: "inspect_normalization_inputs",
            BlockReasonCode.FEATURES_INVALID: "fix_feature_generation",
            BlockReasonCode.CLUSTERING_DEGENERATE: "adjust_cluster_parameters",
            BlockReasonCode.RESOLVE_ZERO_RESULTS: "inspect_resolve_rules",
            BlockReasonCode.NO_APPROVED_NODES_FOR_PROMOTE: "approve_nodes_before_promote",
            BlockReasonCode.PROMOTE_NODE_SET_MISSING: "inspect_promote_lookup",
            BlockReasonCode.PROMOTE_LOOKUP_EMPTY: "inspect_promote_lookup",
            BlockReasonCode.PHASE1_PROMOTE_NOT_COMPLETED: "complete_phase1_promote",
            BlockReasonCode.PLACE_SET_ID_NOT_FOUND: "verify_phase2_candidate_build_output",
            BlockReasonCode.PLACE_SET_EMPTY_OR_MISSING: "re_run_phase2_candidate_build",
            BlockReasonCode.SEMANTIC_PIPELINE_FAILED: "rerun_semantics_and_check_db",
            BlockReasonCode.SEMANTIC_REGRESSION_HIGH: "review_semantic_regression",
            BlockReasonCode.PHASE3_INVERSE_COMPLETION_BLOCKING: "complete_inverse_work_before_step20",
            BlockReasonCode.STEP20_UNMATCHED_BLOCKING: "divert_to_phase1_new_nodes",
            BlockReasonCode.STEP20_AMBIGUOUS_BLOCKING: "divert_to_phase1_new_nodes",
            BlockReasonCode.STEP20_SEQUENCE_QUALITY_LOW: "review_sequence_quality",
            BlockReasonCode.STEP20_REORDER_SUGGESTED_BLOCKING: "prepare_reorder_approval",
            BlockReasonCode.STEP30_SEQUENCE_RESOLUTION_BLOCKING: "approve_canonical_sequence_before_geometry",
            BlockReasonCode.GEOMETRY_FAILED: "inspect_geometry_candidates",
            BlockReasonCode.GEOMETRY_QUALITY_CRITICAL_LOW: "review_geometry_quality",
            BlockReasonCode.RANKING_FAILED: "inspect_ranking_pipeline",
            BlockReasonCode.APPROVAL_REQUIRED: "operator_approval_required",
            BlockReasonCode.DESTRUCTIVE_CLEANUP_APPROVAL_REQUIRED: "operator_cleanup_approval_required",
            BlockReasonCode.MERGE_BIND_APPROVAL_REQUIRED: "operator_merge_approval_required",
            BlockReasonCode.GATE_BYPASS_ATTEMPT: "investigate_gate_bypass_attempt",
            BlockReasonCode.VALIDATOR_PAYLOAD_CONTRACT_MISMATCH: "fix_executor_validator_payload_contract",
        }
        return mapping.get(code, "review_and_resolve")

    def _fallback_block_reason_code(self, step: StepDefinition, validator_result: ValidatorResult) -> BlockReasonCode:
        if validator_result.gate_bypass_attempted:
            return BlockReasonCode.GATE_BYPASS_ATTEMPT

        if step.step_id == STEP_P1_1_EXTRACT:
            if int((validator_result.evidence or {}).get("attempt_no") or 0) >= 3:
                return BlockReasonCode.REPEATED_EXTRACTOR_FAILURE
            return BlockReasonCode.EXTRACTION_LOW_COVERAGE

        if step.step_id == STEP_P1_2_CHAIN:
            return BlockReasonCode.NORMALIZE_FAILED
        if step.step_id == STEP_P2_1_SEMANTIC:
            return BlockReasonCode.SEMANTIC_PIPELINE_FAILED
        if step.step_id == STEP_P2_3_CLEANUP:
            return BlockReasonCode.DESTRUCTIVE_CLEANUP_APPROVAL_REQUIRED
        if step.step_id == STEP_P3_15_INVERSE:
            return BlockReasonCode.PHASE3_INVERSE_COMPLETION_BLOCKING
        if step.step_id == STEP_P3_2_STEP20:
            if int((validator_result.evidence or {}).get("ambiguous_count") or 0) > 0:
                return BlockReasonCode.STEP20_AMBIGUOUS_BLOCKING
            return BlockReasonCode.STEP20_UNMATCHED_BLOCKING
        if step.step_id == STEP_P3_4_STEP30:
            return BlockReasonCode.GEOMETRY_FAILED
        if step.step_id == STEP_P3_4_STEP35:
            return BlockReasonCode.RANKING_FAILED
        if step.step_id == STEP_P3_5_MERGE:
            return BlockReasonCode.MERGE_BIND_APPROVAL_REQUIRED
        return BlockReasonCode.APPROVAL_REQUIRED

    def _should_pause_for_warning(self, profile: str, validator_result: ValidatorResult) -> bool:
        settings = _policy(profile)
        if not validator_result.passable_warning:
            return True
        return bool(settings.warning_requires_operator_review)

    @staticmethod
    def _is_extractor_retry_step(step: StepDefinition) -> bool:
        return str(step.step_id or "") in {STEP_P1_1_EXTRACT, STEP_P3_1_EXTRACT}

    @staticmethod
    def _is_extraction_class_step(step: StepDefinition) -> bool:
        """Steps that have extraction-like semantics and need attempt history integrity."""
        return str(step.step_id or "") in {STEP_P1_1_EXTRACT, STEP_P2_1_SEMANTIC, STEP_P3_1_EXTRACT}

    def _ensure_extractor_attempt_history_summary(
        self,
        *,
        step: StepDefinition,
        attempt_no: int,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
    ) -> ExecutorResult:
        if not self._is_extraction_class_step(step):
            return exec_result

        summary = dict(exec_result.summary or {})
        validator_evidence = dict(validator_result.evidence or {})
        hist = _extract_attempt_history_summary(exec_summary=summary, validator_evidence=validator_evidence)
        attempts = [dict(x or {}) for x in list(hist.get("attempts") or [])]
        hist_attempt_count = _handoff_to_int(hist.get("attempt_count_total"))
        if hist_attempt_count is None:
            hist_attempt_count = _handoff_to_int(hist.get("attempt_count"))
        hist_attempt_count = int(hist_attempt_count or 0)
        coherent = bool(hist_attempt_count > 0 and attempts)
        if coherent:
            summary["extraction_attempt_history_summary"] = dict(hist)
            summary["extraction_attempts_summary"] = dict(hist)
            payload = _as_dict(summary.get("validator_payload"))
            payload["extractor_attempt_history_summary"] = dict(hist)
            payload["extractor_attempts_summary"] = dict(hist)
            if payload.get("attempt_no") is None:
                payload["attempt_no"] = int(attempt_no)
            summary["validator_payload"] = payload
            exec_result.summary = summary
            return exec_result

        should_enforce = bool(
            int(attempt_no or 0) > 1
            or str(validator_result.status or "").strip() in {"warning", "blocked", "failed"}
            or bool(list(validator_result.warnings or []))
        )

        records = [dict(x or {}) for x in list(summary.get("extraction_attempt_records") or [])]
        if records:
            records = _mark_attempt_change(records)
            hist = _summarize_attempt_records(records)
            summary["extraction_attempt_records"] = records
        elif should_enforce:
            candidate_count = _handoff_to_int(summary.get("candidate_count"))
            raw_elements_count = _handoff_to_int(summary.get("raw_elements_count"))
            spatial_interpretation = _as_dict(summary.get("spatial_interpretation"))
            status = "success" if bool(exec_result.ok) else "error"
            if bool(exec_result.ok) and (candidate_count or 0) <= 0 and (raw_elements_count or 0) <= 0:
                status = "empty"
            error_code = str(summary.get("error_code") or "").strip() or None
            error_summary = (
                str(summary.get("error_summary") or summary.get("error") or summary.get("reason") or "").strip() or None
            )
            if "timeout" in str(f"{error_code or ''} {error_summary or ''}").lower():
                status = "timeout"
            synthesized = _attempt_record(
                phase=str(step.phase or ""),
                step_id=str(step.step_id or ""),
                attempt_no=int(attempt_no),
                run_id=str(summary.get("run_id") or "") or None,
                trace_id=str(summary.get("trace_id") or "") or None,
                area_group=(summary.get("area_group") or None),
                sector=(summary.get("sector") or None),
                bbox_used=_as_dict(summary.get("bbox")),
                bbox_hash=_bbox_hash(_as_dict(summary.get("bbox"))),
                bbox_fingerprint=_bbox_hash(_as_dict(summary.get("bbox"))),
                route_id=(summary.get("route_id") or None),
                retry_strategy=(summary.get("retry_strategy") or None),
                retry_reason=(summary.get("retry_reason") or None),
                retry_parameter_delta=dict(summary.get("retry_parameter_delta") or {}),
                action_or_template_used=(
                    summary.get("action_or_template_used")
                    or summary.get("action_id")
                    or _as_dict(summary.get("best")).get("action_id")
                ),
                fallback_config_used=_as_dict(summary.get("retry_parameter_applied")).get("fallback_config"),
                fallback_profile_used=_as_dict(summary.get("retry_parameter_applied")).get("fallback_profile"),
                status=status,
                duration_ms=_handoff_to_int(summary.get("duration_ms")),
                timeout_flag=(True if status == "timeout" else None),
                raw_elements_count=raw_elements_count,
                candidate_count=candidate_count,
                error_code=error_code,
                error_summary=error_summary,
                effective_config_fingerprint=str(summary.get("effective_config_fingerprint") or "").strip() or None,
                attempt_changed_from_previous=_handoff_to_bool(summary.get("attempt_changed_from_previous")),
                spatial_plan_signature=(str(spatial_interpretation.get("spatial_plan_signature") or "").strip() or None),
                retry_changed_spatial_plan=_handoff_to_bool(spatial_interpretation.get("retry_changed_spatial_plan")),
                spatial_interpretation_status=(
                    str(spatial_interpretation.get("spatial_interpretation_status") or "").strip() or None
                ),
                runtime_spatial_strategy_used=(
                    str(spatial_interpretation.get("runtime_spatial_strategy_used") or "").strip() or None
                ),
                spatial_interpretation_failure_reason=(
                    str(spatial_interpretation.get("spatial_interpretation_failure_reason") or "").strip() or None
                ),
                target_option_received=_handoff_to_bool(spatial_interpretation.get("target_option_received")),
            )
            records = _mark_attempt_change([synthesized])
            hist = _summarize_attempt_records(records)
            summary["extraction_attempt_records"] = records

        if hist:
            summary["extraction_attempt_history_summary"] = dict(hist)
            summary["extraction_attempts_summary"] = dict(hist)
            payload = _as_dict(summary.get("validator_payload"))
            payload["extractor_attempt_history_summary"] = dict(hist)
            payload["extractor_attempts_summary"] = dict(hist)
            if payload.get("attempt_no") is None:
                payload["attempt_no"] = int(attempt_no)
            summary["validator_payload"] = payload
            exec_result.summary = summary

        # --- Attempt history gap detection (Fix 1.2) ---
        if int(attempt_no or 0) >= 2:
            final_hist = _extract_attempt_history_summary(
                exec_summary=dict(exec_result.summary or {}),
                validator_evidence=dict(validator_result.evidence or {}),
            )
            final_attempts = [dict(x or {}) for x in list(final_hist.get("attempts") or [])]
            expected_prior = int(attempt_no) - 1
            actual_prior = len(final_attempts)
            if actual_prior < expected_prior:
                import logging as _logging
                missing = sorted(
                    set(range(1, int(attempt_no))) - {_handoff_to_int(a.get("attempt_no")) for a in final_attempts}
                )
                _logging.getLogger(__name__).warning(
                    "ATTEMPT_HISTORY_GAP: step=%s attempt_no=%d expected_prior=%d actual_prior=%d missing=%s",
                    step.step_id, attempt_no, expected_prior, actual_prior, missing,
                )
                gap_flag = "attempt_history_gap"
                s = dict(exec_result.summary or {})
                flags = list(s.get("insufficient_data_flags") or [])
                if gap_flag not in flags:
                    flags.append(gap_flag)
                s["insufficient_data_flags"] = flags
                s["attempt_history_gap"] = {
                    "expected_count": expected_prior,
                    "actual_count": actual_prior,
                    "missing_attempts": missing,
                }
                exec_result.summary = s

        return exec_result

    @staticmethod
    def _adaptive_history_support_level(
        *,
        history_sample_size: int,
        similar_pattern_seen: bool,
        comparator_outcome: Optional[str],
    ) -> str:
        score = int(max(0, history_sample_size))
        if bool(similar_pattern_seen):
            score += 2
        if str(comparator_outcome or "").strip() not in {"", "not_run"}:
            score += 1
        if score >= 7:
            return "high"
        if score >= 4:
            return "medium"
        if score >= 2:
            return "low"
        return "none"

    @staticmethod
    def _adaptive_history_confidence(level: str) -> float:
        norm = str(level or "").strip().lower()
        if norm == "high":
            return 0.85
        if norm == "medium":
            return 0.65
        if norm == "low":
            return 0.45
        return 0.25

    @staticmethod
    def _compact_adaptive_retry_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
        raw = dict(plan or {})
        reasons: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for row in list(raw.get("reasons") or []):
            item = _as_dict(row)
            code = str(item.get("code") or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            reasons.append(
                {
                    "code": code,
                    "message": str(item.get("message") or "").strip(),
                }
            )
            if len(reasons) >= 8:
                break
        evidence = _as_dict(raw.get("evidence_summary"))
        return {
            "enabled": bool(raw.get("enabled")),
            "reordered": bool(raw.get("reordered")),
            "default_order": [str(x) for x in list(raw.get("default_order") or []) if str(x).strip()][:8],
            "recommended_order": [str(x) for x in list(raw.get("recommended_order") or []) if str(x).strip()][:8],
            "reasons": reasons,
            "history_support_level": str(raw.get("history_support_level") or "none"),
            "history_confidence": _handoff_to_float(raw.get("history_confidence")),
            "escalation_bias": str(raw.get("escalation_bias") or "none"),
            "max_attempts_override": (
                int(raw.get("max_attempts_override"))
                if raw.get("max_attempts_override") is not None
                else None
            ),
            "skip_reason": (str(raw.get("skip_reason")) if raw.get("skip_reason") else None),
            "evidence_summary": {
                "attempt_count_total": _handoff_to_int(evidence.get("attempt_count_total")),
                "same_config_repeat_count": _handoff_to_int(evidence.get("same_config_repeat_count")),
                "retry_diversity_count": _handoff_to_int(evidence.get("retry_diversity_count")),
                "history_sample_size": _handoff_to_int(evidence.get("history_sample_size")),
                "extractor_help_needed": _handoff_to_bool(evidence.get("extractor_help_needed")),
                "target_option_received": _handoff_to_bool(evidence.get("target_option_received")),
                "spatial_interpretation_status": evidence.get("spatial_interpretation_status"),
                "same_spatial_plan_retry_count": _handoff_to_int(evidence.get("same_spatial_plan_retry_count")),
                "retry_changed_spatial_plan": _handoff_to_bool(evidence.get("retry_changed_spatial_plan")),
                "patch_outcome": evidence.get("patch_outcome"),
                "patch_type": evidence.get("patch_type"),
                "step20_available": _handoff_to_bool(evidence.get("step20_available")),
            },
        }

    def _adaptive_plan_event_payload(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        compact = self._compact_adaptive_retry_plan(plan)
        reason_codes = [
            str(_as_dict(x).get("code") or "")
            for x in list(compact.get("reasons") or [])
            if str(_as_dict(x).get("code") or "").strip()
        ]
        return {
            "default_order": list(compact.get("default_order") or [])[:6],
            "recommended_order": list(compact.get("recommended_order") or [])[:6],
            "reordered": bool(compact.get("reordered")),
            "reason_codes": reason_codes[:8],
            "history_support_level": compact.get("history_support_level"),
            "history_confidence": compact.get("history_confidence"),
            "escalation_bias": compact.get("escalation_bias"),
            "max_attempts_override": compact.get("max_attempts_override"),
            "skip_reason": compact.get("skip_reason"),
            "evidence_summary": dict(compact.get("evidence_summary") or {}),
        }

    @staticmethod
    def _shadow_support_level_from_confidence(value: Any) -> str:
        conf = _handoff_to_float(value)
        if conf is None:
            return "none"
        score = max(0.0, min(1.0, float(conf)))
        if score >= 0.80:
            return "high"
        if score >= 0.60:
            return "medium"
        if score >= 0.40:
            return "low"
        return "none"

    @staticmethod
    def _normalize_shadow_order(raw_order: Any, *, allowed: Sequence[str]) -> List[str]:
        allowed_set = {str(x).strip() for x in list(allowed or []) if str(x).strip()}
        out: List[str] = []
        for item in list(raw_order or []):
            key = str(item or "").strip()
            if not key or key not in allowed_set or key in out:
                continue
            out.append(key)
            if len(out) >= 8:
                break
        return out

    @staticmethod
    def _normalize_shadow_scores(raw: Any, *, allowed: Sequence[str]) -> List[Dict[str, Any]]:
        allowed_set = {str(x).strip() for x in list(allowed or []) if str(x).strip()}
        pairs: List[tuple[str, float]] = []
        if isinstance(raw, dict):
            for key, val in raw.items():
                strategy = str(key or "").strip()
                if not strategy or strategy not in allowed_set:
                    continue
                score = _handoff_to_float(val)
                if score is None:
                    continue
                pairs.append((strategy, float(score)))
        elif isinstance(raw, (list, tuple)):
            for row in list(raw):
                item = _as_dict(row)
                strategy = str(item.get("strategy") or item.get("name") or "").strip()
                if not strategy or strategy not in allowed_set:
                    continue
                score = _handoff_to_float(item.get("score"))
                if score is None:
                    continue
                pairs.append((strategy, float(score)))
        pairs.sort(key=lambda p: p[1], reverse=True)
        return [{"strategy": k, "score": round(float(v), 4)} for k, v in pairs[:8]]

    @staticmethod
    def _compact_shadow_learned_retry_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
        raw = dict(plan or {})
        return {
            "available": bool(raw.get("available")),
            "mode": "shadow",
            "model_type": (str(raw.get("model_type")) if raw.get("model_type") else None),
            "model_name": (str(raw.get("model_name")) if raw.get("model_name") else None),
            "model_version": (str(raw.get("model_version")) if raw.get("model_version") else None),
            "ranked_order": [str(x) for x in list(raw.get("ranked_order") or []) if str(x).strip()][:8],
            "score_by_strategy": [dict(x or {}) for x in list(raw.get("score_by_strategy") or [])][:8],
            "confidence": _handoff_to_float(raw.get("confidence")),
            "support_level": str(raw.get("support_level") or "none"),
            "reason_codes": [str(x) for x in list(raw.get("reason_codes") or []) if str(x).strip()][:8],
            "feature_summary": _as_dict(raw.get("feature_summary")),
            "unavailable_reason": (str(raw.get("unavailable_reason")) if raw.get("unavailable_reason") else None),
        }

    @staticmethod
    def _compact_shadow_retry_comparison(comparison: Dict[str, Any]) -> Dict[str, Any]:
        raw = dict(comparison or {})
        return {
            "comparison_status": str(raw.get("comparison_status") or "unavailable"),
            "deterministic_order": [str(x) for x in list(raw.get("deterministic_order") or []) if str(x).strip()][:8],
            "learned_order": [str(x) for x in list(raw.get("learned_order") or []) if str(x).strip()][:8],
            "top1_match": _handoff_to_bool(raw.get("top1_match")),
            "rank_overlap_count": _handoff_to_int(raw.get("rank_overlap_count")),
            "rank_overlap_ratio": _handoff_to_float(raw.get("rank_overlap_ratio")),
            "deterministic_escalation_bias": (str(raw.get("deterministic_escalation_bias")) if raw.get("deterministic_escalation_bias") else None),
            "learned_confidence": _handoff_to_float(raw.get("learned_confidence")),
            "learned_support_level": (str(raw.get("learned_support_level")) if raw.get("learned_support_level") else None),
        }

    def _build_shadow_retry_features(
        self,
        *,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
        deterministic_plan: Dict[str, Any],
    ) -> Dict[str, Any]:
        summary = dict(exec_result.summary or {})
        evidence = dict(validator_result.evidence or {})
        ai_metrics = dict(ai_snapshot.metrics or {})
        extractor_status = _as_dict(ai_snapshot.extractor_status or ai_metrics.get("extractor_status"))
        extractor_help = _as_dict(ai_snapshot.extractor_help_needed or ai_metrics.get("extractor_help_payload"))
        efficiency = _as_dict(extractor_status.get("efficiency_metrics"))
        completion = _as_dict(extractor_status.get("completion_metrics"))
        scores = _as_dict(extractor_status.get("scores"))
        history = _extract_attempt_history_summary(exec_summary=summary, validator_evidence=evidence)
        phase_scope = _as_dict(state.pipeline_scope.get(str(step.phase or "")))
        block_reason_code = (
            str(validator_result.block_reason_code.value)
            if isinstance(validator_result.block_reason_code, BlockReasonCode)
            else (str(validator_result.block_reason_code) if validator_result.block_reason_code else None)
        )
        patch_history = _build_patch_history_summary(
            state=state,
            phase=step.phase,
            step_id=step.step_id,
            block_reason_code=block_reason_code,
            reason_class=(str(extractor_help.get("reason_class") or "").strip() or None),
        ) or {}

        context: Dict[str, Any] = {
            "phase": str(step.phase or ""),
            "step_id": str(step.step_id or ""),
        }
        if str(step.phase or "") == "phase1":
            p1_scope = _as_dict(state.pipeline_scope.get("phase1"))
            extract_scope = _as_dict(p1_scope.get("extract"))
            context.update(
                {
                    "area_group": summary.get("area_group") or extract_scope.get("area_group_hint") or None,
                    "sector": summary.get("sector") or extract_scope.get("sector_hint") or None,
                    "bbox_hash": (
                        _bbox_hash(_as_dict(summary.get("bbox")))
                        or _bbox_hash(_as_dict(extract_scope.get("bbox")))
                    ),
                }
            )
        elif str(step.phase or "") == "phase3":
            context.update(
                {
                    "service_route_id": phase_scope.get("service_route_id"),
                    "direction_id": _handoff_to_int(phase_scope.get("direction_id")),
                    "route_id": summary.get("route_id") or state.resume_context.get("route_id"),
                    "bbox_hash": _bbox_hash(_as_dict(phase_scope.get("bbox"))),
                }
            )

        return {
            "run_id": str(state.run_id or ""),
            "trace_id": str(state.trace_id or ""),
            "attempt_no": int(attempt_no),
            "context": context,
            "default_order": [str(x) for x in list(deterministic_plan.get("default_order") or []) if str(x).strip()][:8],
            "extractor_metrics": {
                "extractor_efficiency_health_score": _status_norm_01(
                    extractor_status.get("extractor_efficiency_health_score")
                ),
                "completion_quality_score": _status_norm_01(scores.get("completion_quality_score")),
                "order_completion_quality_score": _status_norm_01(scores.get("order_completion_quality_score")),
                "attempt_count_total": _handoff_to_int(history.get("attempt_count_total") or history.get("attempt_count")),
                "retry_diversity_count": _handoff_to_int(
                    history.get("retry_diversity_count")
                    if history.get("retry_diversity_count") is not None
                    else efficiency.get("retry_diversity_count")
                ),
                "same_config_repeat_count": _handoff_to_int(
                    history.get("same_config_repeat_count")
                    if history.get("same_config_repeat_count") is not None
                    else efficiency.get("same_config_repeat_count")
                ),
                "fallback_rescue_success": _handoff_to_bool(efficiency.get("fallback_rescue_success")),
                "step20_available": _handoff_to_bool(completion.get("step20_available")),
                "step20_gate_passed": _handoff_to_bool(completion.get("step20_gate_passed")),
            },
            "extractor_help_needed": {
                "needed": _handoff_to_bool(extractor_help.get("needed")),
                "severity": extractor_help.get("severity"),
                "confidence": _handoff_to_float(extractor_help.get("confidence")),
                "reason_class": extractor_help.get("reason_class"),
            },
            "extractor_warning_codes": [
                str(x).strip()
                for x in list(dict.fromkeys(list(extractor_status.get("warnings") or []) + list(validator_result.warnings or [])))
                if str(x).strip()
            ][:8],
            "adaptive_context": {
                "default_order": [str(x) for x in list(deterministic_plan.get("default_order") or []) if str(x).strip()][:8],
                "recommended_order": [str(x) for x in list(deterministic_plan.get("recommended_order") or []) if str(x).strip()][:8],
                "history_support_level": deterministic_plan.get("history_support_level"),
                "history_confidence": _handoff_to_float(deterministic_plan.get("history_confidence")),
                "escalation_bias": deterministic_plan.get("escalation_bias"),
                "reason_codes": [
                    str(_as_dict(x).get("code") or "")
                    for x in list(deterministic_plan.get("reasons") or [])
                    if str(_as_dict(x).get("code") or "").strip()
                ][:8],
            },
            "patch_history_summary": {
                "similar_pattern_seen": _handoff_to_bool(patch_history.get("similar_pattern_seen")),
                "last_patch_type": patch_history.get("last_patch_type"),
                "last_comparator_outcome": patch_history.get("last_comparator_outcome"),
                "last_operator_outcome_decision": patch_history.get("last_operator_outcome_decision"),
                "recency_note": patch_history.get("recency_note"),
                "record_count_similar": _handoff_to_int(patch_history.get("record_count_similar")),
            },
        }

    def _evaluate_shadow_learned_retry_plan(
        self,
        *,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
        deterministic_plan: Dict[str, Any],
        features: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        default_order = [str(x).strip() for x in list(deterministic_plan.get("default_order") or []) if str(x).strip()]
        features = dict(
            features
            or self._build_shadow_retry_features(
                state=state,
                step=step,
                attempt_no=attempt_no,
                exec_result=exec_result,
                validator_result=validator_result,
                ai_snapshot=ai_snapshot,
                deterministic_plan=deterministic_plan,
            )
        )
        feature_summary = {
            "phase": _as_dict(features.get("context")).get("phase"),
            "step_id": _as_dict(features.get("context")).get("step_id"),
            "attempt_count_total": _handoff_to_int(_as_dict(features.get("extractor_metrics")).get("attempt_count_total")),
            "same_config_repeat_count": _handoff_to_int(_as_dict(features.get("extractor_metrics")).get("same_config_repeat_count")),
            "retry_diversity_count": _handoff_to_int(_as_dict(features.get("extractor_metrics")).get("retry_diversity_count")),
            "step20_available": _handoff_to_bool(_as_dict(features.get("extractor_metrics")).get("step20_available")),
            "deterministic_support": _as_dict(features.get("adaptive_context")).get("history_support_level"),
            "patch_outcome": _as_dict(features.get("patch_history_summary")).get("last_comparator_outcome"),
        }

        ranker = self.shadow_learned_retry_ranker
        if ranker is None:
            return {
                "available": False,
                "mode": "shadow",
                "model_type": None,
                "model_name": None,
                "model_version": None,
                "ranked_order": [],
                "score_by_strategy": [],
                "confidence": None,
                "support_level": "none",
                "reason_codes": ["shadow_model_unavailable"],
                "feature_summary": feature_summary,
                "unavailable_reason": "model_unavailable",
            }

        try:
            raw = dict(ranker(dict(features)) or {})
        except Exception as exc:
            return {
                "available": False,
                "mode": "shadow",
                "model_type": None,
                "model_name": None,
                "model_version": None,
                "ranked_order": [],
                "score_by_strategy": [],
                "confidence": None,
                "support_level": "none",
                "reason_codes": ["shadow_ranker_error"],
                "feature_summary": feature_summary,
                "unavailable_reason": f"ranker_error:{type(exc).__name__}",
            }

        ranked_order = self._normalize_shadow_order(
            raw.get("ranked_order")
            if raw.get("ranked_order") is not None
            else raw.get("recommended_order"),
            allowed=default_order,
        )
        score_by_strategy = self._normalize_shadow_scores(
            raw.get("score_by_strategy")
            if raw.get("score_by_strategy") is not None
            else raw.get("scores"),
            allowed=default_order,
        )
        confidence = _handoff_to_float(
            raw.get("confidence")
            if raw.get("confidence") is not None
            else _as_dict(raw.get("confidence_band")).get("score")
        )
        if confidence is not None:
            confidence = max(0.0, min(1.0, float(confidence)))
        support_level = str(raw.get("support_level") or "").strip().lower() or self._shadow_support_level_from_confidence(confidence)
        if support_level not in {"none", "low", "medium", "high"}:
            support_level = self._shadow_support_level_from_confidence(confidence)
        reason_codes = [
            str(x).strip()
            for x in list(
                raw.get("reason_codes")
                or [(_as_dict(row).get("code")) for row in list(raw.get("reasons") or [])]
            )
            if str(x).strip()
        ][:8]

        available = bool(ranked_order)
        unavailable_reason = None if available else "insufficient_rank_output"
        if not available and not reason_codes:
            reason_codes = ["shadow_ranker_missing_order"]
        return {
            "available": available,
            "mode": "shadow",
            "model_type": (str(raw.get("model_type")) if raw.get("model_type") else "heuristic"),
            "model_name": (str(raw.get("model_name")) if raw.get("model_name") else None),
            "model_version": (str(raw.get("model_version")) if raw.get("model_version") else None),
            "ranked_order": ranked_order[:8],
            "score_by_strategy": score_by_strategy[:8],
            "confidence": (round(float(confidence), 4) if confidence is not None else None),
            "support_level": support_level,
            "reason_codes": reason_codes,
            "feature_summary": feature_summary,
            "unavailable_reason": unavailable_reason,
        }

    def _build_shadow_retry_plan_comparison(
        self,
        *,
        deterministic_plan: Dict[str, Any],
        learned_plan: Dict[str, Any],
    ) -> Dict[str, Any]:
        det_order = [str(x).strip() for x in list(deterministic_plan.get("recommended_order") or deterministic_plan.get("default_order") or []) if str(x).strip()][:8]
        learned_order = [str(x).strip() for x in list(learned_plan.get("ranked_order") or []) if str(x).strip()][:8]
        if not bool(_handoff_to_bool(learned_plan.get("available"))):
            return {
                "comparison_status": "unavailable",
                "deterministic_order": det_order,
                "learned_order": learned_order,
                "top1_match": None,
                "rank_overlap_count": 0,
                "rank_overlap_ratio": 0.0,
                "deterministic_escalation_bias": deterministic_plan.get("escalation_bias"),
                "learned_confidence": _handoff_to_float(learned_plan.get("confidence")),
                "learned_support_level": learned_plan.get("support_level"),
            }
        if not det_order or not learned_order:
            return {
                "comparison_status": "insufficient_features",
                "deterministic_order": det_order,
                "learned_order": learned_order,
                "top1_match": None,
                "rank_overlap_count": 0,
                "rank_overlap_ratio": 0.0,
                "deterministic_escalation_bias": deterministic_plan.get("escalation_bias"),
                "learned_confidence": _handoff_to_float(learned_plan.get("confidence")),
                "learned_support_level": learned_plan.get("support_level"),
            }
        overlap = len(set(det_order).intersection(set(learned_order)))
        denom = max(1, min(len(det_order), len(learned_order)))
        return {
            "comparison_status": "comparable",
            "deterministic_order": det_order,
            "learned_order": learned_order,
            "top1_match": bool(det_order[0] == learned_order[0]),
            "rank_overlap_count": int(overlap),
            "rank_overlap_ratio": round(float(overlap) / float(denom), 4),
            "deterministic_escalation_bias": deterministic_plan.get("escalation_bias"),
            "learned_confidence": _handoff_to_float(learned_plan.get("confidence")),
            "learned_support_level": learned_plan.get("support_level"),
        }

    def _build_shadow_retry_eval_row(
        self,
        *,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        features: Dict[str, Any],
        deterministic_plan: Dict[str, Any],
        learned_plan: Dict[str, Any],
        comparison: Dict[str, Any],
    ) -> Dict[str, Any]:
        compact_features = {
            "context": _as_dict(features.get("context")),
            "extractor_metrics": _as_dict(features.get("extractor_metrics")),
            "extractor_help_needed": _as_dict(features.get("extractor_help_needed")),
            "adaptive_context": _as_dict(features.get("adaptive_context")),
            "patch_history_summary": _as_dict(features.get("patch_history_summary")),
        }
        return {
            "run_id": str(state.run_id or ""),
            "trace_id": str(state.trace_id or ""),
            "phase": str(step.phase or ""),
            "step_id": str(step.step_id or ""),
            "attempt_no": int(attempt_no),
            "feature_summary_hash": _config_fingerprint(compact_features)[:24],
            "deterministic_order": [str(x) for x in list(deterministic_plan.get("recommended_order") or deterministic_plan.get("default_order") or []) if str(x).strip()][:8],
            "learned_order": [str(x) for x in list(learned_plan.get("ranked_order") or []) if str(x).strip()][:8],
            "comparison_status": str(comparison.get("comparison_status") or "unavailable"),
            "top1_match": _handoff_to_bool(comparison.get("top1_match")),
            "rank_overlap_count": _handoff_to_int(comparison.get("rank_overlap_count")),
            "deterministic_escalation_bias": deterministic_plan.get("escalation_bias"),
            "learned_confidence": _handoff_to_float(learned_plan.get("confidence")),
            "patch_outcome_hint": _as_dict(features.get("patch_history_summary")).get("last_comparator_outcome"),
        }

    def _build_adaptive_retry_plan(
        self,
        *,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
    ) -> Dict[str, Any]:
        default_order = [str(x).strip() for x in list(step.retry_policy.strategies or []) if str(x).strip()]
        plan: Dict[str, Any] = {
            "enabled": bool(getattr(self.feature_flags, "adaptive_retry_enabled", True)),
            "phase": str(step.phase or ""),
            "step_id": str(step.step_id or ""),
            "attempt_no": int(attempt_no),
            "default_order": default_order[:8],
            "recommended_order": default_order[:8],
            "reordered": False,
            "reasons": [],
            "evidence_summary": {},
            "history_support_level": "none",
            "history_confidence": 0.25,
            "escalation_bias": "none",
            "max_attempts_override": None,
            "skip_reason": None,
        }
        if not plan["enabled"]:
            plan["skip_reason"] = "feature_flag_disabled"
            return plan
        if not default_order:
            plan["skip_reason"] = "no_retry_strategies"
            return plan
        if not self._is_extractor_retry_step(step):
            plan["skip_reason"] = "non_extractor_step"
            return plan

        summary = dict(exec_result.summary or {})
        evidence = dict(validator_result.evidence or {})
        ai_metrics = dict(ai_snapshot.metrics or {})
        extractor_status = _as_dict(ai_snapshot.extractor_status or ai_metrics.get("extractor_status"))
        extractor_help = _as_dict(ai_snapshot.extractor_help_needed or ai_metrics.get("extractor_help_payload"))
        efficiency_metrics = _as_dict(extractor_status.get("efficiency_metrics"))
        completion_metrics = _as_dict(extractor_status.get("completion_metrics"))
        spatial_metrics = _as_dict(extractor_status.get("spatial_metrics"))
        score_metrics = _as_dict(extractor_status.get("scores"))
        warning_set = {
            str(x).strip()
            for x in (
                list(extractor_status.get("warnings") or [])
                + list(validator_result.warnings or [])
                + [str(extractor_help.get("reason_class") or "").strip()]
            )
            if str(x).strip()
        }

        history = _extract_attempt_history_summary(exec_summary=summary, validator_evidence=evidence)
        attempts = [dict(x or {}) for x in list(history.get("attempts") or [])]
        if not attempts:
            attempts = [dict(x or {}) for x in list(summary.get("extraction_attempt_records") or [])][:8]
        attempt_count_total = int(
            _handoff_to_int(history.get("attempt_count_total"))
            or _handoff_to_int(history.get("attempt_count"))
            or len(attempts)
            or 0
        )
        history_missing = bool(attempt_count_total <= 0 or not attempts)
        same_config_repeat_count = int(
            _handoff_to_int(history.get("same_config_repeat_count"))
            or _handoff_to_int(efficiency_metrics.get("same_config_repeat_count"))
            or 0
        )
        retry_diversity_count = int(
            _handoff_to_int(history.get("retry_diversity_count"))
            or _handoff_to_int(efficiency_metrics.get("retry_diversity_count"))
            or 0
        )
        fallback_rescue_success = _handoff_to_bool(efficiency_metrics.get("fallback_rescue_success"))
        efficiency_score = _status_norm_01(
            extractor_status.get("extractor_efficiency_health_score")
            if extractor_status.get("extractor_efficiency_health_score") is not None
            else score_metrics.get("extractor_efficiency_health_score")
        )
        completion_quality_score = _status_norm_01(score_metrics.get("completion_quality_score"))
        order_completion_quality_score = _status_norm_01(score_metrics.get("order_completion_quality_score"))
        step20_available = _handoff_to_bool(completion_metrics.get("step20_available"))
        if step20_available is None:
            step20_available = False
        step20_gate_passed = _handoff_to_bool(completion_metrics.get("step20_gate_passed"))
        target_option_received = bool(
            spatial_metrics.get("target_option_received")
            or str(spatial_metrics.get("target_option_text") or "").strip()
        )
        spatial_interpretation_status = (
            str(spatial_metrics.get("spatial_interpretation_status") or "").strip() or None
        )
        target_intent_ignored = bool(spatial_metrics.get("target_intent_ignored"))
        same_spatial_plan_retry_count = _handoff_to_int(spatial_metrics.get("same_spatial_plan_retry_count")) or 0
        retry_changed_spatial_plan = _handoff_to_bool(spatial_metrics.get("retry_changed_spatial_plan"))
        spatial_interpretation_failed = bool(
            target_option_received
            and (
                "spatial_interpretation_failed" in warning_set
                or spatial_interpretation_status in {"failed", "fallback_default_bbox", "invalid_explicit_bbox"}
                or target_intent_ignored
                or (
                    not _as_dict(spatial_metrics.get("bbox_candidate"))
                    and attempt_count_total >= 2
                    and ("repeated_empty_extraction" in warning_set or block_reason_code == BlockReasonCode.EXTRACTION_EMPTY.value)
                )
            )
        )
        spatial_noop_retry_loop = bool(
            target_option_received
            and (
                "spatial_plan_reused_without_change" in warning_set
                or same_spatial_plan_retry_count >= 1
                or (attempt_count_total >= 2 and retry_changed_spatial_plan is False)
            )
        )

        block_reason_code = (
            str(validator_result.block_reason_code.value)
            if isinstance(validator_result.block_reason_code, BlockReasonCode)
            else (str(validator_result.block_reason_code) if validator_result.block_reason_code else "")
        )
        patch_history = _build_patch_history_summary(
            state=state,
            phase=step.phase,
            step_id=step.step_id,
            block_reason_code=(block_reason_code or None),
            reason_class=(str(extractor_help.get("reason_class") or "").strip() or None),
        ) or {}

        strategy_stats: Dict[str, Dict[str, int]] = {
            key: {"attempts": 0, "success": 0, "non_empty": 0, "empty": 0}
            for key in default_order
        }
        for row in attempts:
            strategy = str(row.get("retry_strategy") or "").strip()
            if strategy not in strategy_stats:
                continue
            stats_row = strategy_stats[strategy]
            stats_row["attempts"] += 1
            if _attempt_status_success(row):
                stats_row["success"] += 1
            if _attempt_non_empty(row):
                stats_row["non_empty"] += 1
            status_txt = str(row.get("status") or "").strip().lower()
            cand = _handoff_to_int(row.get("candidate_count"))
            if status_txt == "empty" or (cand is not None and cand <= 0):
                stats_row["empty"] += 1

        history_sample_size = int(sum(int(_as_dict(v).get("attempts") or 0) for v in strategy_stats.values()))
        support_level = self._adaptive_history_support_level(
            history_sample_size=history_sample_size,
            similar_pattern_seen=bool(_handoff_to_bool(patch_history.get("similar_pattern_seen"))),
            comparator_outcome=(str(patch_history.get("last_comparator_outcome") or "").strip() or None),
        )
        if history_missing:
            support_level = "none"
        plan["history_support_level"] = support_level
        plan["history_confidence"] = self._adaptive_history_confidence(support_level)

        reasons: List[Dict[str, Any]] = []
        seen_reason_codes: set[str] = set()

        def _add_reason(code: str, message: str) -> None:
            norm = str(code or "").strip()
            if not norm or norm in seen_reason_codes:
                return
            seen_reason_codes.add(norm)
            reasons.append({"code": norm, "message": str(message or "").strip()})

        strategy_scores: Dict[str, float] = {
            key: float(len(default_order) - idx)
            for idx, key in enumerate(default_order)
        }

        if history_missing:
            _add_reason(
                "missing_attempt_history",
                "Attempt history summary is missing; adaptive ranking confidence is limited.",
            )

        if history_sample_size >= 2:
            for key in default_order:
                row = strategy_stats.get(key) or {}
                attempts_n = int(row.get("attempts") or 0)
                if attempts_n <= 0:
                    continue
                success_rate = float(row.get("success") or 0) / float(attempts_n)
                non_empty_rate = float(row.get("non_empty") or 0) / float(attempts_n)
                empty_rate = float(row.get("empty") or 0) / float(attempts_n)
                strategy_scores[key] = strategy_scores.get(key, 0.0) + (success_rate * 1.6) + (non_empty_rate * 1.2) - (empty_rate * 1.1)
        else:
            _add_reason("adaptive_low_history_support", "History support is low; preserving near-default retry order.")

        last_strategy = str(summary.get("retry_strategy") or "").strip()
        same_config_loop = bool(
            (not history_missing)
            and
            attempt_count_total >= 2
            and (
                same_config_repeat_count >= 2
                or "retry_not_diversified" in warning_set
                or "same_config_retry_loop" in warning_set
                or retry_diversity_count <= 1
            )
        )
        if same_config_loop and len(default_order) > 1:
            if last_strategy and last_strategy in strategy_scores:
                strategy_scores[last_strategy] = strategy_scores.get(last_strategy, 0.0) - 1.75
            for key in default_order:
                if key == last_strategy:
                    continue
                strategy_scores[key] = strategy_scores.get(key, 0.0) + 0.70
            _add_reason(
                "same_config_loop_risk",
                "Detected same-config retry loop risk; promoting retry diversification.",
            )
            plan["escalation_bias"] = "diversify_retries"

        phase = str(step.phase or "").strip()
        if phase == "phase1":
            repeated_empty = bool(
                "repeated_empty_extraction" in warning_set
                or block_reason_code == BlockReasonCode.EXTRACTION_EMPTY.value
            )
            if spatial_interpretation_failed:
                _add_reason(
                    "p1_spatial_interpretation_failed",
                    "Target intent did not resolve into a usable Phase1 spatial plan.",
                )
            if target_intent_ignored:
                _add_reason(
                    "p1_target_intent_ignored",
                    "Runtime fell back to generic/default spatial parameters despite target intent.",
                )
            if spatial_noop_retry_loop:
                _add_reason(
                    "p1_spatial_noop_retry_loop",
                    "Retries reused the same ineffective spatial interpretation without meaningful change.",
                )
            if history_sample_size >= 2:
                best_strategy = None
                best_tuple: tuple[float, float, int] = (-1.0, -1.0, -9999)
                for idx, key in enumerate(default_order):
                    row = strategy_stats.get(key) or {}
                    attempts_n = int(row.get("attempts") or 0)
                    if attempts_n <= 0:
                        continue
                    non_empty_rate = float(row.get("non_empty") or 0) / float(attempts_n)
                    success_rate = float(row.get("success") or 0) / float(attempts_n)
                    rank = (non_empty_rate, success_rate, -idx)
                    if rank > best_tuple:
                        best_tuple = rank
                        best_strategy = key
                if best_strategy and best_strategy != default_order[0]:
                    strategy_scores[best_strategy] = strategy_scores.get(best_strategy, 0.0) + 1.20
                    strategy_scores[default_order[0]] = strategy_scores.get(default_order[0], 0.0) - 0.40
                    _add_reason(
                        "p1_history_prefers_alternate_strategy",
                        f"Recent attempt history favors `{best_strategy}` for this area/sector context.",
                    )
            if repeated_empty and attempt_count_total >= 2:
                if "template_alternative" in strategy_scores:
                    strategy_scores["template_alternative"] = strategy_scores.get("template_alternative", 0.0) + 0.80
                if "bbox_expand" in strategy_scores:
                    strategy_scores["bbox_expand"] = strategy_scores.get("bbox_expand", 0.0) + 0.55
                _add_reason(
                    "p1_repeated_empty_pattern",
                    "Repeated empty extraction pattern detected; prioritizing broader/diversified fallback attempts.",
                )
            known_bad = bool(
                repeated_empty
                and (efficiency_score is not None and efficiency_score <= 0.35)
                and fallback_rescue_success is False
            )
            patch_outcome = str(patch_history.get("last_comparator_outcome") or "").strip()
            patch_type = str(patch_history.get("last_patch_type") or "").strip()
            if patch_type == "extractor" and patch_outcome in {"regressed", "inconclusive"}:
                known_bad = True
                _add_reason(
                    "p1_patch_outcome_low_roi",
                    "Recent extractor patch outcomes were inconclusive/regressed for similar context.",
                )
            if known_bad:
                base_max = int(step.retry_policy.max_attempts) + int(_policy(state.policy_profile).max_retry_bonus)
                override = max(1, min(base_max, int(step.retry_policy.max_attempts) - 1))
                plan["max_attempts_override"] = int(override)
                plan["escalation_bias"] = "early_escalation"
                _add_reason(
                    "p1_early_escalation_bias",
                    "Known-bad repeated-empty pattern detected; reducing retry depth before escalation.",
                )
            if spatial_noop_retry_loop and int(attempt_no) >= 2:
                base_max = int(step.retry_policy.max_attempts) + int(_policy(state.policy_profile).max_retry_bonus)
                override = max(1, min(base_max, int(attempt_no)))
                plan["max_attempts_override"] = int(override)
                plan["escalation_bias"] = "early_escalation"
                _add_reason(
                    "p1_spatial_interpretation_retry_cap",
                    "Repeated no-op spatial retries detected; capping further default retries before extractor patch evaluation.",
                )
            elif spatial_interpretation_failed:
                if str(plan.get("escalation_bias") or "none") == "none":
                    plan["escalation_bias"] = "prefer_patch_eval"

        elif phase == "phase3":
            step20_poor = bool(
                (order_completion_quality_score is not None and order_completion_quality_score <= 0.55)
                or ("extraction_success_but_step20_poor" in warning_set)
                or (step20_gate_passed is False)
            )
            extractor_efficient = bool(efficiency_score is not None and efficiency_score >= 0.60)
            if bool(step20_available) and step20_poor and extractor_efficient:
                if "template_alternative" in strategy_scores:
                    strategy_scores["template_alternative"] = strategy_scores.get("template_alternative", 0.0) - 2.25
                if "fallback_config" in strategy_scores:
                    strategy_scores["fallback_config"] = strategy_scores.get("fallback_config", 0.0) + 2.40
                if "template_alternative" in strategy_scores and "fallback_config" in strategy_scores:
                    # Order-completion quality dominates here: force fallback exploration before repeating extractor-only wins.
                    t_score = float(strategy_scores.get("template_alternative", 0.0))
                    f_score = float(strategy_scores.get("fallback_config", 0.0))
                    if f_score <= t_score:
                        strategy_scores["fallback_config"] = t_score + 0.25
                plan["escalation_bias"] = "prefer_patch_eval"
                _add_reason(
                    "p3_step20_poor_despite_extractor_efficiency",
                    "Step20/order completion remains poor despite extractor efficiency; biasing away from extractor-only retries.",
                )

            weak_persistent = bool(
                step20_poor
                and (
                    (efficiency_score is not None and efficiency_score <= 0.45)
                    or "repeated_corridor_extractor_failure" in warning_set
                )
                and attempt_count_total >= 2
            )
            patch_outcome = str(patch_history.get("last_comparator_outcome") or "").strip()
            patch_type = str(patch_history.get("last_patch_type") or "").strip()
            if patch_type == "extractor" and patch_outcome in {"regressed", "inconclusive"}:
                weak_persistent = True
                _add_reason(
                    "p3_extractor_patch_low_roi",
                    "Recent extractor patch outcomes were weak for similar corridor pattern.",
                )
            if patch_type in {"detector_scoring", "diagnostics"} and patch_outcome == "improved":
                plan["escalation_bias"] = "prefer_patch_eval"
                _add_reason(
                    "p3_detector_or_diagnostics_recently_effective",
                    "Recent detector/diagnostics patch outcome improved for similar pattern.",
                )
            if weak_persistent:
                base_max = int(step.retry_policy.max_attempts) + int(_policy(state.policy_profile).max_retry_bonus)
                override = max(1, min(base_max, int(step.retry_policy.max_attempts) - 1))
                plan["max_attempts_override"] = int(override)
                plan["escalation_bias"] = "early_escalation"
                _add_reason(
                    "p3_early_escalation_bias",
                    "Repeated weak extraction + poor order completion suggests earlier escalation.",
                )

        similar_pattern_seen = bool(_handoff_to_bool(patch_history.get("similar_pattern_seen")))
        if history_sample_size < 2 and not similar_pattern_seen and plan.get("max_attempts_override") is None:
            plan["recommended_order"] = list(default_order)
            plan["reordered"] = False
            if not reasons:
                _add_reason(
                    "adaptive_no_history_default",
                    "No reliable retry history for this context; using default strategy order.",
                )
            plan["skip_reason"] = "insufficient_history"
        else:
            idx_map = {key: idx for idx, key in enumerate(default_order)}
            ordered = sorted(
                list(default_order),
                key=lambda key: (-float(strategy_scores.get(key, 0.0)), int(idx_map.get(key, 999))),
            )
            plan["recommended_order"] = ordered[:8]
            plan["reordered"] = bool(plan["recommended_order"] != default_order)
            if not plan["reordered"] and plan.get("max_attempts_override") is None and plan.get("escalation_bias") == "none":
                plan["skip_reason"] = "no_adaptive_delta"

        if plan.get("max_attempts_override") is not None:
            plan["escalation_bias"] = "early_escalation"

        plan["reasons"] = reasons[:8]
        plan["evidence_summary"] = {
            "attempt_count_total": int(attempt_count_total),
            "history_missing": bool(history_missing),
            "same_config_repeat_count": int(same_config_repeat_count),
            "retry_diversity_count": int(retry_diversity_count),
            "history_sample_size": int(history_sample_size),
            "extractor_help_needed": bool(_handoff_to_bool(extractor_help.get("needed"))),
            "target_option_received": bool(target_option_received),
            "spatial_interpretation_status": spatial_interpretation_status,
            "same_spatial_plan_retry_count": int(same_spatial_plan_retry_count),
            "retry_changed_spatial_plan": retry_changed_spatial_plan,
            "patch_outcome": (str(patch_history.get("last_comparator_outcome") or "").strip() or None),
            "patch_type": (str(patch_history.get("last_patch_type") or "").strip() or None),
            "step20_available": bool(step20_available),
            "completion_quality_score": completion_quality_score,
            "order_completion_quality_score": order_completion_quality_score,
        }
        return plan

    @staticmethod
    def _gate_bypass_attempted(exec_result: ExecutorResult, validator_result: ValidatorResult) -> bool:
        if bool(validator_result.gate_bypass_attempted):
            return True
        return bool(dict(exec_result.summary or {}).get("gate_bypass_attempted", False))

    def _apply_run_context_from_executor(self, state: RunSessionState, step: StepDefinition, exec_result: ExecutorResult) -> None:
        summary = dict(exec_result.summary or {})
        if "node_set_id" in summary:
            state.resume_context["node_set_id"] = summary.get("node_set_id")
        if "route_id" in summary:
            state.resume_context["route_id"] = summary.get("route_id")
        if "stop_sequence_candidate_id" in summary:
            state.resume_context["stop_sequence_candidate_id"] = summary.get("stop_sequence_candidate_id")
        if "geometry_set_id" in summary:
            state.resume_context["geometry_set_id"] = summary.get("geometry_set_id")
        if "approved_geometry_candidate_id" in summary:
            state.resume_context["approved_geometry_candidate_id"] = summary.get("approved_geometry_candidate_id")
        handoff = _as_dict(summary.get(PHASE1_TO_PHASE2_HANDOFF_ARTIFACT))
        if handoff:
            state.resume_context[PHASE1_TO_PHASE2_HANDOFF_ARTIFACT] = handoff
        runtime_context = _as_dict(summary.get("runtime_context"))
        if runtime_context:
            if "context_key" in runtime_context:
                state.resume_context["phase2_context_key"] = runtime_context.get("context_key")
            if "place_set_id" in runtime_context:
                state.resume_context["place_set_id"] = runtime_context.get("place_set_id")
        if step.step_id == STEP_P1_2_CHAIN:
            state.resume_context["latest_phase1_chain_summary"] = summary
        if step.step_id == STEP_P3_1_EXTRACT:
            state.resume_context["latest_phase3_extract_summary"] = summary
        if step.step_id == STEP_P3_15_INVERSE:
            state.resume_context["latest_inverse_completion_summary"] = summary
        if step.step_id == STEP_P3_2_STEP20:
            state.resume_context["latest_step20_summary"] = summary

    # ------------------------------------------------------------
    # Default adapters
    # ------------------------------------------------------------
    @staticmethod
    def _default_executor(
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        params: Dict[str, Any],
    ) -> ExecutorResult:
        del state
        return ExecutorResult(
            ok=True,
            summary={
                "step_id": step.step_id,
                "attempt_no": int(attempt_no),
                "params": dict(params or {}),
                "stub": True,
            },
            artifacts=[],
        )

    @staticmethod
    def _default_validator(
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        exec_result: ExecutorResult,
    ) -> ValidatorResult:
        del state, step, attempt_no
        return ValidatorResult(
            status="pass" if exec_result.ok else "failed",
            gate_passed=bool(exec_result.ok),
            passable_warning=False,
            warnings=[],
            anomalies=[],
            block_reason_code=None,
            summary=("" if exec_result.ok else "executor_failed"),
            evidence=dict(exec_result.summary or {}),
        )

    @staticmethod
    def _default_ai_bot_hook(
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
    ) -> AIBotSnapshot:
        summary = dict(exec_result.summary or {})
        evidence = dict(validator_result.evidence or {})
        reorder = _handoff_extract_reorder_signal(
            exec_summary=summary,
            validator_evidence=evidence,
            ai_proposals={},
        )
        extractor_status = _build_extractor_status(
            state=state,
            step=step,
            attempt_no=attempt_no,
            exec_summary=summary,
            validator_result=validator_result,
            compare={},
            reorder=reorder,
        )
        extractor_help_needed = _build_extractor_help_needed(
            step=step,
            extractor_status=extractor_status,
            validator_result=validator_result,
        )
        payload = build_ai_bot_snapshot(
            scores={
                "quality": pick_quality_score_01([], fallback_validator_status=validator_result.status),
            },
            metrics={
                "phase": step.phase,
                "step_id": step.step_id,
                "attempt_no": int(attempt_no),
                "validator_status": validator_result.status,
                "failed_stage": _as_dict(extractor_status.get("completion_metrics")).get("failed_stage"),
                "sequence_quality_score": pick_sequence_quality_score_100(
                    [
                        summary.get("sequence_quality_score"),
                        evidence.get("sequence_quality_score"),
                    ]
                ),
                "adaptive_retry_plan": _as_dict(summary.get("adaptive_retry_plan")),
                "adaptive_retry_reordered": _handoff_to_bool(_as_dict(summary.get("adaptive_retry_plan")).get("reordered")),
                "adaptive_retry_history_support_level": _as_dict(summary.get("adaptive_retry_plan")).get("history_support_level"),
                "adaptive_retry_escalation_bias": _as_dict(summary.get("adaptive_retry_plan")).get("escalation_bias"),
                "shadow_learned_retry_plan": _as_dict(summary.get("shadow_learned_retry_plan")),
                "shadow_retry_plan_comparison": _as_dict(summary.get("shadow_retry_plan_comparison")),
                "shadow_learned_retry_available": _handoff_to_bool(
                    _as_dict(summary.get("shadow_learned_retry_plan")).get("available")
                ),
                "shadow_retry_comparison_status": _as_dict(summary.get("shadow_retry_plan_comparison")).get("comparison_status"),
                "shadow_retry_top1_match": _handoff_to_bool(
                    _as_dict(summary.get("shadow_retry_plan_comparison")).get("top1_match")
                ),
                "spatial_interpretation": _as_dict(summary.get("spatial_interpretation")),
                "spatial_interpretation_status": _as_dict(summary.get("spatial_interpretation")).get("spatial_interpretation_status"),
                "target_option_received": _handoff_to_bool(
                    _as_dict(summary.get("spatial_interpretation")).get("target_option_received")
                ),
                "retry_changed_spatial_plan": _handoff_to_bool(
                    _as_dict(summary.get("spatial_interpretation")).get("retry_changed_spatial_plan")
                ),
            },
            warnings=list(validator_result.warnings or []),
            anomaly_flags=list(validator_result.anomalies or []),
            proposals={
                "reorder_proposal": bool(reorder.get("recommended")) if reorder.get("recommended") is not None else False,
                "reorder": reorder,
            },
            extractor_status=extractor_status,
            extractor_help_needed=extractor_help_needed,
        )
        return AIBotSnapshot(**payload)

    @staticmethod
    def _default_chatgpt_interpreter(
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
        trigger: str,
    ) -> ChatGPTSnapshot:
        del state, exec_result, ai_snapshot
        return ChatGPTSnapshot(
            summary=(
                f"{step.step_id} attempt {attempt_no}: "
                f"interpreted {trigger} with validator status `{validator_result.status}`"
            ),
            priorities=["fix_blocker_first", "preserve_gate_authority"],
            risk_notes=["advisory_only_no_gate_override"],
        )


class AdvisoryChatGPTInterpreter:
    """
    ChatGPT API interpreter adapter for blocker/anomaly interpretation.

    This adapter intentionally uses advisory-only context and never grants
    execution authority.
    """

    _HADES_INTERPRETER_TASK = "hades_pipeline_interpreter"
    _HADES_CONSISTENCY_TASK = "hades_evidence_consistency_checker"
    _LEGACY_PIPELINE_TASKS = {
        "interpret_pipeline_blocker",
        "prioritize_pipeline_resolution",
        "explain_cleanup_risk",
        "interpret_merge_evidence",
    }
    _GENERIC_SUMMARY_MARKERS = {
        "advisory interpretation generated",
        "interpretation complete",
        "summary unavailable",
    }

    def __init__(
        self,
        *,
        advisory_service: Any = None,
        endpoint_task: str = "hades_pipeline_interpreter",
        advisory_mode: Optional[str] = None,
    ) -> None:
        self._advisory_mode = str(advisory_mode or "").strip().lower() or None
        self.init_error: Optional[Exception] = None
        if advisory_service is None:
            from datamind_console.api_chatgpt.services.advisory_service import AdvisoryService

            try:
                advisory_service = AdvisoryService(requested_mode=self._advisory_mode)
            except Exception as exc:
                advisory_service = None
                self.init_error = exc
        self._service = advisory_service
        requested_task = str(endpoint_task or "").strip()
        self._endpoint_task = requested_task or self._HADES_INTERPRETER_TASK

    def _error_snapshot(
        self,
        *,
        task: str,
        trigger: str,
        snapshot: Dict[str, Any],
        error_code: str,
        error_summary: str,
        risk_notes: Sequence[str],
    ) -> ChatGPTSnapshot:
        return ChatGPTSnapshot(
            summary=f"ChatGPT interpretation unavailable: {error_summary}",
            priorities=["fallback_to_operator_review"],
            risk_notes=[str(x) for x in list(risk_notes or []) if str(x).strip()],
            status="error",
            task=task,
            trigger=trigger,
            model=None,
            latency_ms=None,
            token_usage={},
            snapshot_id=str(snapshot.get("snapshot_id") or "") or None,
            source="real_advisory_error" if self._advisory_mode == "real_advisory" else "advisory_error",
            schema_name=(
                "hades_pipeline_interpreter_response.json"
                if task == self._HADES_INTERPRETER_TASK
                else None
            ),
            requested_mode=self._advisory_mode,
            fallback_used=False,
            error_code=error_code,
            error_summary=error_summary,
            prompt_package={
                "task": task,
                "schema_name": (
                    "hades_pipeline_interpreter_response.json"
                    if task == self._HADES_INTERPRETER_TASK
                    else None
                ),
                "snapshot": dict(snapshot or {}),
                "advisory_error": {
                    "task": task,
                    "status": "error",
                    "source": "real_advisory_error" if self._advisory_mode == "real_advisory" else "advisory_error",
                    "requested_mode": self._advisory_mode,
                    "model": None,
                    "error_code": error_code,
                    "error_summary": error_summary,
                    "fallback_used": False,
                    "token_usage": None,
                    "snapshot_id": (str(snapshot.get("snapshot_id") or "") or None),
                    "schema_name": (
                        "hades_pipeline_interpreter_response.json"
                        if task == self._HADES_INTERPRETER_TASK
                        else None
                    ),
                },
            },
        )

    def _task_for_call(
        self,
        *,
        step: StepDefinition,
        trigger: str,
        validator_result: ValidatorResult,
    ) -> str:
        del step
        diagnostic_triggered = (
            trigger in {"warning", "blocked", "anomaly", "failed"}
            or validator_result.status in {"warning", "blocked", "failed"}
        )
        if diagnostic_triggered:
            # Allow explicit legacy routing only when intentionally configured.
            if self._endpoint_task in self._LEGACY_PIPELINE_TASKS:
                return self._endpoint_task
            return self._HADES_INTERPRETER_TASK
        return self._endpoint_task or self._HADES_INTERPRETER_TASK

    @staticmethod
    def _recent_history_summary(state: RunSessionState) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in list(state.step_execution_records or [])[-5:]:
            out.append(
                {
                    "phase": str(row.phase or ""),
                    "step_id": str(row.step_id or ""),
                    "attempt_no": int(row.attempt_no or 0),
                    "status": str(row.status or ""),
                    "validator_status": str((dict(row.validator_result or {})).get("status") or ""),
                    "block_reason_code": str((dict(row.block_reason or {})).get("code") or ""),
                    "created_at": str(row.created_at or ""),
                }
            )
        return out

    @staticmethod
    def _first_score_01(candidates: Sequence[Any]) -> Optional[float]:
        for raw in list(candidates or []):
            parsed = _handoff_normalize_score_01(raw)
            if parsed is not None:
                return parsed
        return None

    def _build_normalized_snapshot(
        self,
        *,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
        trigger: str,
    ) -> Dict[str, Any]:
        exec_summary = dict(exec_result.summary or {})
        validator = asdict(validator_result)
        validator_evidence = dict(validator.get("evidence") or {})
        ai_raw = asdict(ai_snapshot)
        ai_scores = dict(ai_raw.get("scores") or {})
        ai_metrics = dict(ai_raw.get("metrics") or {})
        ai_proposals = dict(ai_raw.get("proposals") or {})
        compare = dict(ai_proposals.get("comparison") or {})
        compare_latest = dict(compare.get("latest") or {})
        compare_latest_metrics = dict(compare.get("latest_metrics") or {})
        regression_flags = _handoff_extract_regression_flags(compare)
        reorder = _handoff_extract_reorder_signal(
            exec_summary=exec_summary,
            validator_evidence=validator_evidence,
            ai_proposals=ai_proposals,
        )
        if reorder.get("recommended") is None and compare.get("latest_reorder_recommended") is not None:
            parsed_recommended = _handoff_to_bool(compare.get("latest_reorder_recommended"))
            if parsed_recommended is not None:
                reorder["recommended"] = parsed_recommended
        if reorder.get("confidence") is None:
            for raw in [
                compare_latest.get("reorder_confidence"),
                compare_latest_metrics.get("reorder_confidence"),
                ai_metrics.get("reorder_confidence"),
            ]:
                parsed_conf = _handoff_confidence_to_numeric(raw)
                if parsed_conf is not None:
                    reorder["confidence"] = parsed_conf
                    break
        reorder_conf = _handoff_confidence_to_numeric(reorder.get("confidence"))
        if reorder_conf is not None:
            reorder["confidence"] = reorder_conf
        quality_score = pick_quality_score_01(
            [
                ai_scores.get("quality_score"),
                ai_scores.get("quality"),
                ai_metrics.get("quality_score"),
                compare_latest.get("quality_score"),
                compare_latest_metrics.get("quality_score"),
                exec_summary.get("quality_score"),
                validator_evidence.get("quality_score"),
            ],
            fallback_validator_status=validator_result.status,
        )
        sequence_quality_score = pick_sequence_quality_score_100(
            [
                ai_scores.get("sequence_quality_score"),
                ai_metrics.get("sequence_quality_score"),
                compare_latest.get("sequence_quality_score"),
                compare_latest_metrics.get("sequence_quality_score"),
                exec_summary.get("sequence_quality_score"),
                validator_evidence.get("sequence_quality_score"),
            ]
        )
        history = self._recent_history_summary(state)
        policy_profile_payload = {"name": str(state.policy_profile or "")} if str(state.policy_profile or "").strip() else None
        warnings: List[str] = []
        anomaly_flags: List[str] = []
        for raw in list(ai_snapshot.warnings or []) + list(validator_result.warnings or []):
            txt = str(raw or "").strip()
            if txt and txt not in warnings:
                warnings.append(txt)
        for raw in list(ai_snapshot.anomaly_flags or []) + list(validator_result.anomalies or []) + list(regression_flags):
            txt = str(raw or "").strip()
            if txt and txt not in anomaly_flags:
                anomaly_flags.append(txt)

        consistency_candidates = _handoff_build_consistency_candidates(
            step_id=step.step_id,
            exec_summary=exec_summary,
            validator_result=validator,
            ai_snapshot=ai_raw,
        )
        insufficient_data_flags: List[str] = []
        candidate_codes = {
            str(_as_dict(row).get("code") or "").strip()
            for row in list(consistency_candidates or [])
            if str(_as_dict(row).get("code") or "").strip()
        }
        if "P1_PROMOTE_MISSING_WORKSPACE_STATE_SUMMARY" in candidate_codes:
            insufficient_data_flags.append("missing_workspace_state_summary")
        if str(step.step_id or "") in {STEP_P1_1_EXTRACT, STEP_P2_1_SEMANTIC, STEP_P3_1_EXTRACT, STEP_P3_2_STEP20}:
            hist = _extract_attempt_history_summary(
                exec_summary=exec_summary,
                validator_evidence=validator_evidence,
            )
            hist_attempts = [dict(x or {}) for x in list(hist.get("attempts") or [])]
            hist_attempt_count = _handoff_to_int(hist.get("attempt_count_total"))
            if hist_attempt_count is None:
                hist_attempt_count = _handoff_to_int(hist.get("attempt_count"))
            hist_attempt_count = int(hist_attempt_count or 0)
            if int(attempt_no or 0) >= 1 and (hist_attempt_count <= 0 or not hist_attempts):
                insufficient_data_flags.append("missing_attempt_history")
        insufficient_data_flags = list(dict.fromkeys([str(x).strip() for x in insufficient_data_flags if str(x).strip()]))[:8]
        stage_aliases = _handoff_stage_aliases(step.step_id)
        telemetry_stage_raw = str(compare.get("stage") or "").strip() or None
        extractor_status = dict(ai_raw.get("extractor_status") or ai_metrics.get("extractor_status") or {})
        extractor_help_needed = dict(
            ai_raw.get("extractor_help_needed")
            or ai_metrics.get("extractor_help_payload")
            or {}
        )
        adaptive_plan_ctx = _as_dict(state.resume_context.get("adaptive_retry_plans")).get(step.step_id)
        adaptive_retry_plan = _as_dict(
            ai_metrics.get("adaptive_retry_plan")
            or exec_summary.get("adaptive_retry_plan")
            or adaptive_plan_ctx
        )
        shadow_plan_ctx = _as_dict(state.resume_context.get("shadow_learned_retry_plans")).get(step.step_id)
        shadow_learned_retry_plan = _as_dict(
            ai_metrics.get("shadow_learned_retry_plan")
            or exec_summary.get("shadow_learned_retry_plan")
            or shadow_plan_ctx
        )
        shadow_comparison_ctx = _as_dict(state.resume_context.get("shadow_retry_plan_comparisons")).get(step.step_id)
        shadow_retry_plan_comparison = _as_dict(
            ai_metrics.get("shadow_retry_plan_comparison")
            or exec_summary.get("shadow_retry_plan_comparison")
            or shadow_comparison_ctx
        )
        patch_history_summary = _build_patch_history_summary(
            state=state,
            phase=step.phase,
            step_id=step.step_id,
            block_reason_code=(
                str(validator_result.block_reason_code.value)
                if isinstance(validator_result.block_reason_code, BlockReasonCode)
                else (str(validator_result.block_reason_code) if validator_result.block_reason_code else None)
            ),
            reason_class=(str(extractor_help_needed.get("reason_class") or "").strip() or None),
        )
        spatial_context = _as_dict(
            exec_summary.get("spatial_interpretation")
            or validator_evidence.get("spatial_interpretation")
            or ai_metrics.get("spatial_interpretation")
        )
        promote_context: Dict[str, Any] = {}
        if step.step_id == STEP_P1_4_PROMOTE:
            promote = _as_dict(exec_summary.get("promote_dry_run"))
            promote_workspace = (
                _as_dict(promote.get("workspace_state_summary"))
                or _as_dict(exec_summary.get("workspace_state_summary"))
                or _as_dict(validator_evidence.get("workspace_state_summary"))
            )
            promote_status = (
                str(
                    promote.get("status")
                    or validator_evidence.get("promote_status")
                    or ""
                ).strip()
                or None
            )
            promote_context = {
                "promote_status": promote_status,
                "legacy_status": (
                    str(promote.get("legacy_status") or "").strip()
                    or None
                ),
                "promote_precondition": (
                    str(
                        promote.get("promote_precondition")
                        or validator_evidence.get("promote_precondition")
                        or ""
                    ).strip()
                    or None
                ),
                "n_resolved": _handoff_to_int(
                    promote.get("n_resolved")
                    if promote.get("n_resolved") is not None
                    else validator_evidence.get("n_resolved")
                ),
                "approved_count": _handoff_to_int(
                    promote_workspace.get("approved_count")
                    if promote_workspace.get("approved_count") is not None
                    else validator_evidence.get("approved_count")
                ),
                "pending_review_count": _handoff_to_int(promote_workspace.get("pending_review_count")),
                "resolved_total": _handoff_to_int(
                    promote_workspace.get("resolved_total")
                    if promote_workspace.get("resolved_total") is not None
                    else validator_evidence.get("resolved_total")
                ),
                "recommended_action": (
                    str(
                        promote.get("recommended_action")
                        or validator_evidence.get("recommended_action")
                        or validator_result.recommended_action
                        or ""
                    ).strip()
                    or None
                ),
                "diagnostics_hint": (
                    str(
                        promote.get("diagnostics_hint")
                        or validator_evidence.get("diagnostics_hint")
                        or ""
                    ).strip()
                    or None
                ),
                "workspace_state_summary": promote_workspace,
            }

        phase3_route_extractor_packet: Dict[str, Any] = {}
        geographic_context: Dict[str, Any] = {}
        if str(step.phase or "") == "phase3":
            latest_extract_summary = (
                exec_summary
                if step.step_id == STEP_P3_1_EXTRACT
                else _as_dict(state.resume_context.get("latest_phase3_extract_summary"))
            )
            latest_step20_summary = (
                exec_summary
                if step.step_id == STEP_P3_2_STEP20
                else _as_dict(state.resume_context.get("latest_step20_summary"))
            )
            extract_validator_payload = _as_dict(latest_extract_summary.get("validator_payload"))
            phase3_bundle = _phase3_route_bundle_evidence(
                summary=latest_extract_summary,
                payload=extract_validator_payload,
                validator_evidence=validator_evidence,
            )
            candidate_universe_summary = _as_dict(phase3_bundle.get("candidate_universe_summary"))
            selection_summary = _as_dict(phase3_bundle.get("selection_summary"))
            phase3_scope = _as_dict(state.pipeline_scope.get("phase3"))
            attempt_hist = _extract_attempt_history_summary(
                exec_summary=latest_extract_summary,
                validator_evidence=extract_validator_payload,
            )
            step20_payload = _as_dict(latest_step20_summary.get("validator_payload"))
            phase3_spatial_context = _as_dict(
                latest_extract_summary.get("spatial_interpretation")
                or extract_validator_payload.get("spatial_interpretation")
                or spatial_context
            )
            phase3_route_extractor_packet = {
                "scope": {
                    "bbox": _as_dict(phase3_scope.get("bbox")),
                    "refs": list(phase3_scope.get("refs") or []),
                    "operator": phase3_scope.get("operator"),
                    "name": phase3_scope.get("name"),
                    "service_route_id": phase3_scope.get("service_route_id"),
                    "direction_id": phase3_scope.get("direction_id"),
                    "query_strategy": (
                        candidate_universe_summary.get("query_strategy")
                        or latest_extract_summary.get("query_strategy")
                    ),
                    "original_geographic_input": (
                        phase3_spatial_context.get("original_geographic_input")
                        or phase3_spatial_context.get("target_option_text")
                    ),
                    "normalized_geographic_input": (
                        str(phase3_spatial_context.get("normalized_geographic_input") or "").strip() or None
                    ),
                    "geographic_interpretation_source": (
                        phase3_spatial_context.get("geographic_interpretation_source")
                        or phase3_spatial_context.get("spatial_interpretation_source")
                    ),
                    "geographic_interpretation_status": (
                        phase3_spatial_context.get("geographic_interpretation_status")
                        or phase3_spatial_context.get("spatial_interpretation_status")
                    ),
                    "bbox_validation_status": phase3_spatial_context.get("bbox_validation_status"),
                    "interpretation_confidence": _handoff_to_float(
                        phase3_spatial_context.get("interpretation_confidence")
                        if phase3_spatial_context.get("interpretation_confidence") is not None
                        else phase3_spatial_context.get("bbox_candidate_confidence")
                    ),
                    "fallback_used": _handoff_to_bool(phase3_spatial_context.get("fallback_used")),
                    "fallback_reason": phase3_spatial_context.get("fallback_reason"),
                    "effective_bbox_used": _as_dict(
                        phase3_spatial_context.get("effective_bbox_used")
                        or phase3_spatial_context.get("runtime_bbox_used")
                    ),
                    "effective_bbox_fingerprint": (
                        str(phase3_spatial_context.get("effective_bbox_fingerprint") or "").strip() or None
                    ),
                    "target_option_received": _handoff_to_bool(phase3_spatial_context.get("target_option_received")),
                    "phase_applicability": list(phase3_spatial_context.get("phase_applicability") or []),
                },
                "candidate_universe": {
                    "count_total": _handoff_to_int(phase3_bundle.get("candidate_universe_count")),
                    "count_scored": _handoff_to_int(candidate_universe_summary.get("candidate_scored_count")),
                    "count_fetched": _handoff_to_int(candidate_universe_summary.get("candidate_fetch_evaluated_count")),
                    "hard_filters_applied": list(candidate_universe_summary.get("hard_filters_applied") or []),
                    "soft_signals_used": list(candidate_universe_summary.get("soft_signals_used") or []),
                    "selection_confidence": _handoff_to_float(phase3_bundle.get("selection_confidence")),
                    "score_gap_top2": _handoff_to_float(selection_summary.get("score_gap_top2")),
                    "top_stop_prior_count": _handoff_to_int(phase3_bundle.get("top_stop_prior_count")),
                    "signal_strength": latest_extract_summary.get("extractor_diagnostics", {}).get("signal_strength")
                    if isinstance(latest_extract_summary.get("extractor_diagnostics"), dict)
                    else None,
                },
                "selection": {
                    "selected_osm_relation_id": _handoff_to_int(phase3_bundle.get("selected_osm_relation_id")),
                    "selected_rank": _handoff_to_int(selection_summary.get("selected_rank")),
                    "selected_score": _handoff_to_float(selection_summary.get("selected_score")),
                    "selected_relation_stop_prior_count": _handoff_to_int(phase3_bundle.get("selected_relation_stop_prior_count")),
                    "selection_status": selection_summary.get("selection_status"),
                    "selection_reason_codes": list(selection_summary.get("selection_reason_codes") or []),
                    "strong_bundle_evidence": bool(phase3_bundle.get("strong_bundle_evidence")),
                },
                "fetch": {
                    "selected_relation_fetched": bool(phase3_bundle.get("fetch_artifact_present")),
                    "raw_relation_available": bool(phase3_bundle.get("fetch_success_signal")),
                    "prior_stop_count": _handoff_to_int(phase3_bundle.get("actual_prior_stop_count")),
                    "prior_stop_evidence_count": _handoff_to_int(phase3_bundle.get("prior_stop_evidence_count")),
                    "fetch_status_classification": (
                        str(phase3_bundle.get("fetch_status_classification") or "").strip() or None
                    ),
                    "fetch_observability_gap": bool(phase3_bundle.get("fetch_observability_gap")),
                    "bundle_contradiction_codes": list(phase3_bundle.get("contradiction_codes") or [])[:8],
                    "selected_relation_summary": _as_dict(phase3_bundle.get("selected_relation_summary")),
                },
                "step20_bridge": {
                    "matched_count": _handoff_to_int(
                        latest_step20_summary.get("matched_count")
                        if latest_step20_summary.get("matched_count") is not None
                        else step20_payload.get("matched_count")
                    ),
                    "unmatched_count": _handoff_to_int(
                        latest_step20_summary.get("unmatched_count")
                        if latest_step20_summary.get("unmatched_count") is not None
                        else step20_payload.get("unmatched_count")
                    ),
                    "ambiguous_count": _handoff_to_int(
                        latest_step20_summary.get("ambiguous_count")
                        if latest_step20_summary.get("ambiguous_count") is not None
                        else step20_payload.get("ambiguous_count")
                    ),
                    "sequence_quality_score": _handoff_to_float(
                        latest_step20_summary.get("sequence_quality_score")
                        if latest_step20_summary.get("sequence_quality_score") is not None
                        else step20_payload.get("sequence_quality_score")
                    ),
                    "blocker_origin_hint": (
                        latest_step20_summary.get("blocker_origin_hint")
                        or step20_payload.get("step20_dominant_cause")
                    ),
                    "step20_gate_passed": _handoff_to_bool(
                        latest_step20_summary.get("sequence_gate_pass")
                        if latest_step20_summary.get("sequence_gate_pass") is not None
                        else step20_payload.get("sequence_gate_pass")
                    ),
                },
                "attempts": {
                    "attempt_count_total": _handoff_to_int(attempt_hist.get("attempt_count_total")),
                    "retry_diversified": _handoff_to_bool(attempt_hist.get("diversified_attempts")),
                    "same_config_repeat_count": _handoff_to_int(attempt_hist.get("same_config_repeat_count")),
                    "fallback_profile_used": latest_extract_summary.get("fallback_profile_used"),
                },
            }
        geographic_source = spatial_context
        if not geographic_source and str(step.phase or "") == "phase3":
            geographic_source = _as_dict(
                exec_summary.get("spatial_interpretation")
                or validator_evidence.get("spatial_interpretation")
            )
        if geographic_source:
            extractor_scores = _as_dict(extractor_status.get("scores")) if isinstance(extractor_status, dict) else {}
            geographic_context = {
                "original_geographic_input": (
                    geographic_source.get("original_geographic_input")
                    or geographic_source.get("target_option_text")
                    or geographic_source.get("source_text")
                ),
                "normalized_geographic_input": (
                    str(geographic_source.get("normalized_geographic_input") or "").strip() or None
                ),
                "geographic_input_type": (
                    str(geographic_source.get("geographic_input_type") or "").strip() or None
                ),
                "interpreted_place_meaning": (
                    geographic_source.get("interpreted_place_meaning")
                    or geographic_source.get("target_option_text")
                ),
                "geographic_interpretation_source": (
                    geographic_source.get("geographic_interpretation_source")
                    or geographic_source.get("spatial_interpretation_source")
                    or geographic_source.get("interpreted_by")
                ),
                "geographic_interpretation_status": (
                    geographic_source.get("geographic_interpretation_status")
                    or geographic_source.get("spatial_interpretation_status")
                    or geographic_source.get("interpretation_status")
                ),
                "bbox_candidate": _as_dict(geographic_source.get("bbox_candidate")) or None,
                "bbox_candidate_confidence": _handoff_to_float(geographic_source.get("bbox_candidate_confidence")),
                "interpretation_confidence": _handoff_to_float(
                    geographic_source.get("interpretation_confidence")
                    if geographic_source.get("interpretation_confidence") is not None
                    else geographic_source.get("bbox_candidate_confidence")
                ),
                "bbox_validation_status": geographic_source.get("bbox_validation_status"),
                "effective_bbox_used": _as_dict(
                    geographic_source.get("effective_bbox_used")
                    or geographic_source.get("runtime_bbox_used")
                ) or None,
                "effective_bbox_fingerprint": (
                    str(geographic_source.get("effective_bbox_fingerprint") or "").strip() or None
                ),
                "fallback_used": _handoff_to_bool(geographic_source.get("fallback_used")),
                "fallback_reason": geographic_source.get("fallback_reason"),
                "runtime_spatial_strategy_used": geographic_source.get("runtime_spatial_strategy_used"),
                "phase_applicability": list(geographic_source.get("phase_applicability") or []),
                "supporting_hints": list(_as_dict(geographic_source.get("supporting_hints")).keys())[:10],
                "advisory_trace": _as_dict(geographic_source.get("advisory_trace")) or None,
                "geography_priority_enforced": _handoff_to_bool(
                    geographic_source.get("geography_priority_enforced")
                ) if geographic_source.get("geography_priority_enforced") is not None else True,
                "route_hints_influenced_bbox": _handoff_to_bool(
                    geographic_source.get("route_hints_influenced_bbox")
                ),
                "route_hint_provenance": _as_dict(geographic_source.get("route_context")) or None,
                "extractor_quality_summary": {
                    "extractor_efficiency_health_score": _handoff_to_float(
                        extractor_scores.get("extractor_efficiency_health_score")
                    ),
                    "completion_quality_score": _handoff_to_float(extractor_scores.get("completion_quality_score")),
                    "order_completion_quality_score": _handoff_to_float(
                        extractor_scores.get("order_completion_quality_score")
                    ),
                },
                "route_hints_present": _handoff_to_bool(
                    geographic_source.get("route_hints_present")
                ) if geographic_source.get("route_hints_present") is not None else False,
                "route_hints_used_as_secondary_signal": _handoff_to_bool(
                    geographic_source.get("route_hints_used_as_secondary_signal")
                ) if geographic_source.get("route_hints_used_as_secondary_signal") is not None else False,
                "route_hints_overconstrained_geography": _handoff_to_bool(
                    geographic_source.get("route_hints_overconstrained_geography")
                ) if geographic_source.get("route_hints_overconstrained_geography") is not None else False,
                "route_hint_effect_reason": (
                    str(geographic_source.get("route_hint_effect_reason") or "").strip() or None
                ),
                "geography_quality_attribution": _classify_geography_quality_attribution(
                    interpretation_confidence=_handoff_to_float(
                        geographic_source.get("interpretation_confidence")
                        if geographic_source.get("interpretation_confidence") is not None
                        else geographic_source.get("bbox_candidate_confidence")
                    ),
                    fallback_used=_handoff_to_bool(geographic_source.get("fallback_used")),
                    fallback_reason=geographic_source.get("fallback_reason"),
                    bbox_validation_status=geographic_source.get("bbox_validation_status"),
                    completion_quality_score=_handoff_to_float(extractor_scores.get("completion_quality_score")),
                    efficiency_score=_handoff_to_float(extractor_scores.get("extractor_efficiency_health_score")),
                    route_hints_present=_handoff_to_bool(geographic_source.get("route_hints_present")),
                    route_hints_influenced_bbox=_handoff_to_bool(geographic_source.get("route_hints_influenced_bbox")),
                    route_hints_overconstrained_geography=_handoff_to_bool(
                        geographic_source.get("route_hints_overconstrained_geography")
                    ),
                ),
            }

        return build_interpreter_snapshot(
            snapshot_id=f"{state.run_id}:{step.step_id}:{attempt_no}",
            run_id=state.run_id,
            trace_id=str(state.trace_id or ""),
            phase=step.phase,
            step_id=step.step_id,
            trigger=trigger,
            stage_normalization={
                "canonical_step_id": step.step_id,
                "telemetry_stage_raw": telemetry_stage_raw,
                "telemetry_stage_aliases": stage_aliases,
                "telemetry_stage_matched": bool(telemetry_stage_raw and telemetry_stage_raw in set(stage_aliases)),
            },
            validator={
                "status": str(validator_result.status or ""),
                "summary": str(validator_result.summary or ""),
                "gate_passed": bool(validator_result.gate_passed),
                "passable_warning": bool(validator_result.passable_warning),
                "warnings": list(validator_result.warnings or []),
                "anomalies": list(validator_result.anomalies or []),
                "block_reason_code": (
                    str(validator_result.block_reason_code.value)
                    if isinstance(validator_result.block_reason_code, BlockReasonCode)
                    else (str(validator_result.block_reason_code) if validator_result.block_reason_code else None)
                ),
                "evidence": validator_evidence,
                "recommended_action": validator_result.recommended_action,
                "gate_bypass_attempted": bool(validator_result.gate_bypass_attempted),
            },
            executor_summary=exec_summary,
            spatial_context=(spatial_context or None),
            geographic_context=(geographic_context or None),
            promote_context=promote_context or None,
            phase3_route_extractor_packet=(phase3_route_extractor_packet or None),
            ai_scores={
                **dict(ai_scores or {}),
                "quality_score": quality_score,
                "sequence_quality_score": sequence_quality_score,
            },
            ai_metrics={
                **dict(ai_metrics or {}),
                "phase": step.phase,
                "step_id": step.step_id,
                "sequence_quality_score": sequence_quality_score,
                "regression_flag_count": len(regression_flags),
                "warning_count": len(warnings),
                "extractor_status": extractor_status,
                "extractor_help_payload": extractor_help_needed,
            },
            ai_warnings=warnings,
            ai_anomaly_flags=anomaly_flags,
            regression_flags=regression_flags,
            compare=compare,
            reorder=reorder,
            extractor_status=extractor_status,
            extractor_help_needed=extractor_help_needed,
            adaptive_retry_plan=(adaptive_retry_plan or None),
            shadow_learned_retry_plan=(shadow_learned_retry_plan or None),
            shadow_retry_plan_comparison=(shadow_retry_plan_comparison or None),
            patch_history_summary=(patch_history_summary or None),
            recent_history_summary=(history or None),
            history=(history or None),
            policy_profile=policy_profile_payload,
            insufficient_data_flags=insufficient_data_flags,
            consistency_candidates=consistency_candidates,
        )

    def _call_task_detailed(self, *, task: str, envelope: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        if self._service is None:
            raise RuntimeError(f"advisory_service_unavailable: {self.init_error}")
        if hasattr(self._service, "run_task_detailed"):
            detailed = dict(self._service.run_task_detailed(endpoint_task=task, envelope=envelope) or {})
            return dict(detailed.get("response") or {}), dict(detailed.get("meta") or {})
        response = dict(self._service.run_task(endpoint_task=task, envelope=envelope) or {})
        return response, {}

    def _hades_quality_guard(self, *, response: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[str]]:
        out = dict(response or {})
        issues: List[str] = []

        summary = str(out.get("summary") or "").strip()
        if (not summary) or any(marker in summary.lower() for marker in self._GENERIC_SUMMARY_MARKERS):
            issues.append("generic_summary")
        if not str(out.get("dominant_cause_class") or "").strip():
            issues.append("missing_dominant_cause")
        if not str(out.get("recommended_branch") or "").strip():
            issues.append("missing_recommended_branch")
        actions = out.get("recommended_next_actions")
        if not isinstance(actions, list) or not any(str(x or "").strip() for x in actions):
            issues.append("missing_recommended_actions")

        if not issues:
            return out, None

        confidence = _handoff_confidence_to_numeric(out.get("confidence"))
        out["confidence"] = confidence if confidence is not None else 0.25
        out["summary"] = summary or "Interpreter output was low quality; fallback routing was applied."
        out["dominant_cause_class"] = str(out.get("dominant_cause_class") or "").strip() or "unknown"
        out["recommended_branch"] = str(out.get("recommended_branch") or "").strip() or "manual_review_required"
        out["secondary_causes"] = list(out.get("secondary_causes") or [])
        out["evidence_consistency_checks"] = dict(
            out.get("evidence_consistency_checks")
            or {"contradictions_found": False, "contradictions": []}
        )
        out["recommended_next_actions"] = list(out.get("recommended_next_actions") or []) or [
            "Pause automated action for this step.",
            "Request operator review with validator and AI evidence attached.",
        ]
        out["patch_task_recommendation"] = dict(
            out.get("patch_task_recommendation")
            or {
                "should_create_patch_task": False,
                "patch_type": None,
                "justification": None,
                "suggested_target": None,
            }
        )
        out["operator_action_required"] = bool(out.get("operator_action_required", True))
        out["approval_type_if_needed"] = out.get("approval_type_if_needed")

        risk_notes = list(out.get("risk_notes") or [])
        risk_notes.append(
            {
                "code": "INTERPRETER_OUTPUT_LOW_QUALITY",
                "severity": "medium",
                "message": "Interpreter response required guarded fallback due to missing or generic fields.",
            }
        )
        out["risk_notes"] = risk_notes
        return out, ",".join(issues)

    @staticmethod
    def _extractor_signal_routing_override(
        *,
        snapshot: Dict[str, Any],
        response: Dict[str, Any],
        consistency_precheck: Dict[str, Any],
    ) -> tuple[Dict[str, Any], Optional[str]]:
        out = dict(response or {})
        phase = str(snapshot.get("phase") or "").strip()
        step_id = str(snapshot.get("step_id") or "").strip()
        if phase not in {"phase1", "phase3"}:
            return out, None

        insufficient_flags = {
            str(x or "").strip()
            for x in list(snapshot.get("insufficient_data_flags") or [])
            if str(x or "").strip()
        }
        promote_missing_workspace_summary = bool(
            step_id == STEP_P1_4_PROMOTE and "missing_workspace_state_summary" in insufficient_flags
        )
        ai_bot = _as_dict(snapshot.get("ai_bot"))
        extractor_status = _as_dict(ai_bot.get("extractor_status"))
        extractor_help = _as_dict(ai_bot.get("extractor_help_needed"))
        if not extractor_help:
            extractor_help = _as_dict(snapshot.get("extractor_help_needed"))
        if not extractor_status and not extractor_help and not promote_missing_workspace_summary:
            return out, None

        needed = bool(extractor_help.get("needed"))
        severity = str(extractor_help.get("severity") or "").strip().lower()
        if severity not in {"low", "medium", "high"}:
            severity = "low"
        help_conf = _handoff_confidence_to_numeric(extractor_help.get("confidence"))
        if help_conf is None:
            help_conf = 0.40
        partial_evidence = bool(extractor_help.get("partial_evidence") or promote_missing_workspace_summary)
        reason_class = str(extractor_help.get("reason_class") or "").strip()
        reason_codes = {
            str(_as_dict(row).get("code") or "").strip()
            for row in list(extractor_help.get("reasons") or [])
            if str(_as_dict(row).get("code") or "").strip()
        }
        evidence = _as_dict(extractor_help.get("evidence"))
        completion_metrics = _as_dict(extractor_status.get("completion_metrics"))
        extractor_scores = _as_dict(extractor_status.get("scores"))
        validator = _as_dict(snapshot.get("validator"))
        validator_evidence = _as_dict(validator.get("evidence"))
        executor_summary = _as_dict(snapshot.get("executor_summary"))
        extractor_diag = _as_dict(executor_summary.get("extractor_diagnostics"))
        spatial_context = _as_dict(
            snapshot.get("spatial_context")
            or executor_summary.get("spatial_interpretation")
            or validator_evidence.get("spatial_interpretation")
        )
        phase3_route_packet = _as_dict(snapshot.get("phase3_route_extractor_packet"))
        phase3_candidate_packet = _as_dict(phase3_route_packet.get("candidate_universe"))
        phase3_selection_packet = _as_dict(phase3_route_packet.get("selection"))
        phase3_bundle = _phase3_route_bundle_evidence(
            summary=executor_summary,
            payload=_as_dict(executor_summary.get("validator_payload")),
            validator_evidence=validator_evidence,
        )
        promote_context = _as_dict(snapshot.get("promote_context"))
        thresholds = dict(EXTRACTOR_PATCH_ROUTING_THRESHOLDS)
        repeated_empty_threshold = int(thresholds.get("repeated_empty_attempts") or 2)

        efficiency_score = _status_norm_01(
            evidence.get("extractor_efficiency_health_score")
            if evidence.get("extractor_efficiency_health_score") is not None
            else extractor_status.get("extractor_efficiency_health_score")
        )
        completion_quality_score = _status_norm_01(
            evidence.get("completion_quality_score")
            if evidence.get("completion_quality_score") is not None
            else extractor_scores.get("completion_quality_score")
        )
        order_completion_quality_score = _status_norm_01(
            evidence.get("order_completion_quality_score")
            if evidence.get("order_completion_quality_score") is not None
            else extractor_scores.get("order_completion_quality_score")
        )
        attempt_count_total = _handoff_to_int(evidence.get("attempt_count_total")) or 0
        same_config_repeat_count = _handoff_to_int(evidence.get("same_config_repeat_count")) or 0
        candidate_count = _handoff_to_int(
            completion_metrics.get("candidate_count")
            if completion_metrics.get("candidate_count") is not None
            else (
                extractor_diag.get("candidate_count")
                if extractor_diag.get("candidate_count") is not None
                else (
                    executor_summary.get("candidate_count")
                    if executor_summary.get("candidate_count") is not None
                    else validator_evidence.get("candidate_count")
                )
            )
        )
        candidate_count = int(candidate_count or 0)
        candidate_universe_count = _handoff_to_int(
            phase3_candidate_packet.get("count_total")
            if phase3_candidate_packet.get("count_total") is not None
            else completion_metrics.get("candidate_universe_count")
        )
        if candidate_universe_count is None:
            candidate_universe_count = candidate_count
        candidate_universe_count = int(candidate_universe_count or 0)
        selection_confidence = _handoff_to_float(
            phase3_candidate_packet.get("selection_confidence")
            if phase3_candidate_packet.get("selection_confidence") is not None
            else completion_metrics.get("selection_confidence")
        )
        query_strategy = str(
            phase3_route_packet.get("scope", {}).get("query_strategy")
            if isinstance(phase3_route_packet.get("scope"), dict)
            else completion_metrics.get("query_strategy")
            or ""
        ).strip() or None
        hard_filters_applied = (
            list(phase3_candidate_packet.get("hard_filters_applied") or [])
            if phase3_candidate_packet
            else list(completion_metrics.get("hard_filters_applied") or [])
        )
        non_empty_extraction = _handoff_to_bool(
            completion_metrics.get("non_empty_extraction")
            if completion_metrics.get("non_empty_extraction") is not None
            else evidence.get("non_empty_extraction")
        )
        if non_empty_extraction is None:
            non_empty_extraction = bool(candidate_count > 0)
        spatial_metrics = _as_dict(extractor_status.get("spatial_metrics"))
        target_option_received = bool(
            spatial_metrics.get("target_option_received")
            or spatial_context.get("target_option_received")
            or str(spatial_metrics.get("target_option_text") or spatial_context.get("target_option_text") or "").strip()
        )
        spatial_interpretation_status = str(
            spatial_metrics.get("spatial_interpretation_status")
            or spatial_context.get("spatial_interpretation_status")
            or ""
        ).strip() or None
        spatial_failure_reason = str(
            spatial_metrics.get("spatial_interpretation_failure_reason")
            or spatial_context.get("spatial_interpretation_failure_reason")
            or ""
        ).strip() or None
        runtime_spatial_strategy_used = str(
            spatial_metrics.get("runtime_spatial_strategy_used")
            or spatial_context.get("runtime_spatial_strategy_used")
            or ""
        ).strip() or None
        same_spatial_plan_retry_count = _handoff_to_int(
            spatial_metrics.get("same_spatial_plan_retry_count")
            if spatial_metrics.get("same_spatial_plan_retry_count") is not None
            else spatial_context.get("same_spatial_plan_retry_count")
        ) or 0
        retry_changed_spatial_plan = _handoff_to_bool(
            spatial_metrics.get("retry_changed_spatial_plan")
            if spatial_metrics.get("retry_changed_spatial_plan") is not None
            else spatial_context.get("retry_changed_spatial_plan")
        )
        target_intent_ignored = bool(
            spatial_metrics.get("target_intent_ignored")
            or spatial_context.get("target_intent_ignored")
        )
        spatial_execution_broken = bool(
            target_option_received
            and (
                target_intent_ignored
                or spatial_interpretation_status in {"failed", "fallback_default_bbox", "invalid_explicit_bbox"}
                or (
                    not _as_dict(spatial_metrics.get("bbox_candidate"))
                    and attempt_count_total >= repeated_empty_threshold
                    and non_empty_extraction is False
                )
                or str(reason_class or "").strip() in {"spatial_interpretation_failed", "target_intent_ignored"}
                or {"spatial_interpretation_failed", "target_intent_ignored"} & reason_codes
            )
        )
        repeated_spatial_noop = bool(
            target_option_received
            and (
                same_spatial_plan_retry_count >= 1
                or (attempt_count_total >= 2 and retry_changed_spatial_plan is False)
                or "spatial_plan_reused_without_change" in reason_codes
                or reason_class == "spatial_plan_reused_without_change"
            )
        )
        block_reason_code = str(validator.get("block_reason_code") or "").strip()
        repeated_empty = bool(
            "repeated_empty_extraction" in reason_codes
            or reason_class == "repeated_empty_extraction"
            or (
                attempt_count_total >= repeated_empty_threshold
                and non_empty_extraction is False
            )
        )
        phase3_bundle_first_override = bool(
            phase == "phase3"
            and phase3_bundle.get("strong_bundle_evidence")
            and str(phase3_bundle.get("fetch_status_classification") or "") in P3_PARTIAL_FETCH_CLASSIFICATIONS
        )
        if phase3_bundle_first_override:
            repeated_empty = False
        material_score_weakness = bool(
            (completion_quality_score is not None and completion_quality_score < float(thresholds["completion_quality_score"]))
            or (efficiency_score is not None and efficiency_score < float(thresholds["efficiency_health_score"]))
        )
        severe_score_weakness = bool(
            (completion_quality_score is not None and completion_quality_score < float(thresholds["strong_completion_quality_score"]))
            or (efficiency_score is not None and efficiency_score < float(thresholds["strong_efficiency_health_score"]))
        )
        help_reason_is_extractor = bool(
            reason_class in EXTRACTOR_WEAKNESS_REASON_CODES
            or bool(reason_codes & EXTRACTOR_WEAKNESS_REASON_CODES)
        )
        low_candidate_yield = bool(candidate_count <= 1 and material_score_weakness)
        candidate_universe_weak = bool(
            phase == "phase3"
            and candidate_universe_count > 0
            and candidate_universe_count < 5
        )
        selection_confidence_low = bool(
            phase == "phase3"
            and selection_confidence is not None
            and selection_confidence < 0.45
        )
        hard_filter_overreach = bool(
            phase == "phase3"
            and query_strategy == "metadata_filtered"
            and candidate_universe_count < 5
            and bool(hard_filters_applied)
        )
        strong_extractor_failure = bool(
            (block_reason_code == BlockReasonCode.EXTRACTION_EMPTY.value and candidate_count == 0)
            or (non_empty_extraction is False and candidate_count == 0)
            or repeated_empty
            or severe_score_weakness
            or (spatial_execution_broken and candidate_count == 0)
            or (candidate_universe_weak and selection_confidence_low and candidate_count <= 1)
        )
        persistent_extractor_weakness = bool(
            repeated_empty
            or repeated_spatial_noop
            or (
                attempt_count_total >= repeated_empty_threshold
                and (
                    same_config_repeat_count >= repeated_empty_threshold
                    or material_score_weakness
                    or spatial_execution_broken
                    or candidate_universe_weak
                )
            )
        )
        material_extractor_weakness = bool(
            strong_extractor_failure
            or material_score_weakness
            or low_candidate_yield
            or (bool(needed) and help_reason_is_extractor)
            or spatial_execution_broken
            or repeated_spatial_noop
            or candidate_universe_weak
            or selection_confidence_low
            or hard_filter_overreach
        )
        moderate_extractor_weakness = bool(
            not strong_extractor_failure
            and (
                material_extractor_weakness
                or (bool(needed) and severity in {"low", "medium"})
            )
        )
        if phase3_bundle_first_override:
            strong_extractor_failure = False
            persistent_extractor_weakness = False
            material_extractor_weakness = False
            moderate_extractor_weakness = False

        step20_available = _handoff_to_bool(evidence.get("step20_available"))
        if step20_available is None:
            step20_available = _handoff_to_bool(completion_metrics.get("step20_available"))
        if step20_available is None:
            step20_available = False
        step20_gate_passed = _handoff_to_bool(evidence.get("step20_gate_passed"))
        if step20_gate_passed is None:
            step20_gate_passed = _handoff_to_bool(completion_metrics.get("step20_gate_passed"))
        if step20_gate_passed is None:
            step20_gate_passed = _handoff_to_bool(validator_evidence.get("sequence_gate_pass"))

        unmatched_count = _handoff_to_int(completion_metrics.get("unmatched_count"))
        if unmatched_count is None:
            unmatched_count = _handoff_to_int(validator_evidence.get("unmatched_count"))
        ambiguous_count = _handoff_to_int(completion_metrics.get("ambiguous_count"))
        if ambiguous_count is None:
            ambiguous_count = _handoff_to_int(validator_evidence.get("ambiguous_count"))
        unmatched_count = int(unmatched_count or 0)
        ambiguous_count = int(ambiguous_count or 0)

        consistency_candidates = list(snapshot.get("consistency_candidates") or [])
        response_consistency = _as_dict(out.get("evidence_consistency_checks"))
        contradictions_found = bool(
            response_consistency.get("contradictions_found")
            or consistency_precheck.get("contradictions_found")
            or consistency_candidates
        )

        geo_quality_attr = _as_dict(
            _as_dict(snapshot.get("geographic_context")).get("geography_quality_attribution")
        )
        geography_likely_cause = bool(
            str(geo_quality_attr.get("likely_cause") or "").strip() in {
                "geography_interpretation",
                "geography_degradation",
                "route_hint_overreach",
            }
        )

        raw_branch = str(out.get("recommended_branch") or "").strip()
        raw_cause = str(out.get("dominant_cause_class") or "").strip()
        raw_generic = bool(raw_branch in {"", "manual_review_required"} and raw_cause in {"", "unknown"})

        should_override = bool(needed or contradictions_found or raw_generic or promote_missing_workspace_summary)
        if not should_override:
            return out, None

        def _set_patch_recommendation(
            *,
            branch: str,
            patch_type: Optional[str],
            justification: Optional[str],
            suggested_target: Optional[str] = "either",
        ) -> Dict[str, Any]:
            if branch in {"patch_extractor", "patch_detector_scoring", "patch_diagnostics"} and patch_type:
                return {
                    "should_create_patch_task": True,
                    "patch_type": patch_type,
                    "justification": str(justification or "").strip() or None,
                    "suggested_target": suggested_target,
                }
            return {
                "should_create_patch_task": False,
                "patch_type": None,
                "justification": None,
                "suggested_target": None,
            }

        def _ensure_risk_note(code: str, severity_level: str, message: str) -> None:
            existing = list(out.get("risk_notes") or [])
            normalized: List[Dict[str, Any]] = []
            for row in existing:
                if isinstance(row, dict):
                    normalized.append(
                        {
                            "code": str(row.get("code") or "").strip() or "NOTE",
                            "severity": str(row.get("severity") or "medium").strip() or "medium",
                            "message": str(row.get("message") or "").strip() or str(row),
                        }
                    )
                elif str(row or "").strip():
                    normalized.append(
                        {
                            "code": "NOTE",
                            "severity": "medium",
                            "message": str(row).strip(),
                        }
                    )
            if not any(str(r.get("code") or "") == code for r in normalized):
                normalized.append(
                    {
                        "code": code,
                        "severity": severity_level,
                        "message": message,
                    }
                )
            out["risk_notes"] = normalized[:20]

        branch = raw_branch or "manual_review_required"
        cause = raw_cause or "unknown"
        confidence = max(0.30, min(0.95, float(help_conf)))
        operator_action_required = bool(out.get("operator_action_required", False))
        approval_type_if_needed = out.get("approval_type_if_needed")
        next_actions: List[str] = []
        override_tag: Optional[str] = None

        contradiction_codes = {
            str(_as_dict(row).get("code") or "").strip()
            for row in list(response_consistency.get("contradictions") or []) + list(consistency_precheck.get("contradictions") or []) + list(consistency_candidates)
            if str(_as_dict(row).get("code") or "").strip()
        }
        if step_id == STEP_P1_4_PROMOTE:
            promote_status = str(
                promote_context.get("promote_status")
                or validator_evidence.get("promote_status")
                or ""
            ).strip()
            promote_precondition = str(
                promote_context.get("promote_precondition")
                or validator_evidence.get("promote_precondition")
                or ""
            ).strip()
            promote_resolved_total = _handoff_to_int(
                promote_context.get("resolved_total")
                if promote_context.get("resolved_total") is not None
                else validator_evidence.get("resolved_total")
            )
            promote_approved_count = _handoff_to_int(
                promote_context.get("approved_count")
                if promote_context.get("approved_count") is not None
                else validator_evidence.get("approved_count")
            )
            promote_pending_review = _handoff_to_int(
                promote_context.get("pending_review_count")
                if promote_context.get("pending_review_count") is not None
                else _as_dict(validator_evidence.get("workspace_state_summary")).get("pending_review_count")
            )
            promote_diag_hint = str(
                promote_context.get("diagnostics_hint")
                or validator_evidence.get("diagnostics_hint")
                or ""
            ).strip()
            promote_recommended_action = str(
                promote_context.get("recommended_action")
                or validator.get("recommended_action")
                or ""
            ).strip()
            if (
                (promote_status == "no_approved_nodes" or promote_precondition == "no_approved_nodes")
                and int(promote_resolved_total or 0) > 0
                and int(promote_approved_count or 0) <= 0
            ):
                branch = "manual_review_required"
                cause = "promote_precondition_unmet"
                confidence = max(confidence, 0.86 if not partial_evidence else 0.72)
                operator_action_required = True
                approval_type_if_needed = approval_type_if_needed or ApprovalType.PROMOTE_NODE_BATCH.value
                next_actions = [
                    "Approve nodes in the Phase1 workspace before attempting promote again.",
                    "Re-run the promote precheck after approvals exist and confirm approved_count is non-zero.",
                ]
                if contradictions_found:
                    next_actions.append(
                        "After approvals exist, review promote status semantics and dry-run lookup behavior if zero-result contradictions persist."
                    )
                out["patch_task_recommendation"] = _set_patch_recommendation(
                    branch=branch,
                    patch_type=None,
                    justification=None,
                )
                _ensure_risk_note(
                    "P1_PROMOTE_PRECONDITION_UNMET",
                    "medium",
                    "Promote is blocked primarily because approved_count is zero; operator approval/workspace action is required before patch escalation.",
                )
                if contradictions_found:
                    _ensure_risk_note(
                        "P1_PROMOTE_STATUS_SEMANTICS_AMBIGUOUS",
                        "medium",
                        "Promote status/block semantics remain worth cleaning up, but only after approved nodes exist.",
                    )
                override_tag = "extractor_override:p1_promote_no_approved_nodes"
                summary_reason = promote_status or promote_precondition or "no_approved_nodes"
                partial_txt = "partial_evidence=true" if partial_evidence else "partial_evidence=false"
                out["summary"] = (
                    f"{phase} {step_id} selected `{branch}` because promote preconditions are unmet "
                    f"(approved_count=0, resolved_total={int(promote_resolved_total or 0)}, reason={summary_reason}, {partial_txt})."
                )
                out["recommended_branch"] = branch
                out["dominant_cause_class"] = cause
                out["confidence"] = round(float(max(0.0, min(1.0, confidence))), 2)
                out["recommended_next_actions"] = list(dict.fromkeys([str(x).strip() for x in next_actions if str(x).strip()]))[:16]
                out["operator_action_required"] = bool(operator_action_required)
                out["approval_type_if_needed"] = approval_type_if_needed
                return out, override_tag
            if promote_status in {"staging_missing", "query_filtered_empty", "node_set_missing"} and int(promote_approved_count or 0) > 0:
                if promote_status == "node_set_missing" or partial_evidence:
                    branch = "patch_diagnostics"
                    cause = "diagnostics_visibility"
                    confidence = max(confidence, 0.72)
                    patch_type = "diagnostics"
                elif contradictions_found:
                    branch = "patch_detector_scoring"
                    cause = "scoring_logic"
                    confidence = max(confidence, 0.80)
                    patch_type = "detector_scoring"
                else:
                    branch = "patch_diagnostics"
                    cause = "diagnostics_visibility"
                    confidence = max(confidence, 0.74)
                    patch_type = "diagnostics"
                operator_action_required = True
                next_actions = [
                    "Inspect promote lookup joins/filters now that approved nodes exist.",
                    "Re-run promote dry-run and confirm approved_count, resolved_total, and n_resolved remain consistent.",
                ]
                if promote_diag_hint:
                    next_actions.append(promote_diag_hint)
                out["patch_task_recommendation"] = _set_patch_recommendation(
                    branch=branch,
                    patch_type=patch_type,
                    justification=(
                        "Approved nodes exist but promote dry-run is still empty, indicating lookup/filter or diagnostics inconsistency."
                    ),
                )
                _ensure_risk_note(
                    "P1_PROMOTE_LOOKUP_EMPTY_AFTER_APPROVAL",
                    "high" if contradictions_found else "medium",
                    "Promote remains empty despite approved workspace data, so diagnostics/lookup investigation is warranted.",
                )
                override_tag = f"extractor_override:p1_promote_{promote_status or 'lookup_empty'}"
                summary_reason = promote_status or promote_precondition or "lookup_empty"
                partial_txt = "partial_evidence=true" if partial_evidence else "partial_evidence=false"
                out["summary"] = (
                    f"{phase} {step_id} selected `{branch}` because promote remains empty after approvals exist "
                    f"(status={summary_reason}, approved_count={int(promote_approved_count or 0)}, {partial_txt})."
                )
                out["recommended_branch"] = branch
                out["dominant_cause_class"] = cause
                out["confidence"] = round(float(max(0.0, min(1.0, confidence))), 2)
                out["recommended_next_actions"] = list(dict.fromkeys([str(x).strip() for x in next_actions if str(x).strip()]))[:16]
                out["operator_action_required"] = bool(operator_action_required)
                out["approval_type_if_needed"] = approval_type_if_needed
                return out, override_tag
        if promote_missing_workspace_summary and (contradictions_found or raw_generic):
            branch = "patch_diagnostics"
            cause = "diagnostics_visibility"
            confidence = max(confidence, 0.72)
            operator_action_required = True
            next_actions = [
                "Attach workspace_state_summary to P1.4 promote snapshot before further root-cause classification.",
                "Re-run promote precheck and confirm approved_count/resolved_total fields are present and consistent.",
                "Only evaluate extractor patching after promote diagnostics contract is complete.",
            ]
            out["patch_task_recommendation"] = _set_patch_recommendation(
                branch=branch,
                patch_type="diagnostics",
                justification="P1.4 promote snapshot is missing workspace_state_summary while zero-result block is active.",
            )
            _ensure_risk_note(
                "P1_PROMOTE_MISSING_WORKSPACE_SUMMARY",
                "high",
                "Missing workspace_state_summary reduces interpreter confidence and points to diagnostics visibility gap.",
            )
            override_tag = "extractor_override:p1_promote_missing_workspace_summary"
        elif contradictions_found and not material_extractor_weakness and not phase3_bundle_first_override:
            if partial_evidence:
                branch = "patch_diagnostics"
                cause = "diagnostics_visibility"
                confidence = max(confidence, 0.72)
                patch_type = "diagnostics"
            else:
                branch = "patch_detector_scoring"
                cause = "detector_thresholds"
                confidence = max(confidence, 0.82)
                patch_type = "detector_scoring"
            operator_action_required = True
            next_actions = [
                "Do not tune extractor yet; contradiction signals indicate validator/scoring contract mismatch.",
                "Create a detector/diagnostics patch task to align payload evidence with gate decisions.",
                "Re-run the same case and compare contradiction set before/after patch.",
            ]
            out["patch_task_recommendation"] = _set_patch_recommendation(
                branch=branch,
                patch_type=patch_type,
                justification=(
                    "Consistency checks reported contradictions: "
                    + ", ".join(sorted(contradiction_codes)[:4])
                ),
            )
            _ensure_risk_note(
                "EXTRACTOR_INTERPRETER_CONTRADICTION_PRIORITY",
                "high",
                "Contradictions are primary only because extractor degradation is not materially established.",
            )
            override_tag = "extractor_override:contradictions"
        elif phase == "phase1":
            low_diversity = bool({"retry_not_diversified", "same_config_retry_loop"} & reason_codes)
            usable_completion = bool(completion_quality_score is not None and completion_quality_score >= 0.55)

            if needed and low_diversity and usable_completion and severity in {"low", "medium"} and not strong_extractor_failure:
                branch = "tuning_retry"
                cause = "extractor_config"
                confidence = max(confidence, 0.66)
                operator_action_required = False
                out["patch_task_recommendation"] = _set_patch_recommendation(
                    branch=branch,
                    patch_type=None,
                    justification=None,
                )
                next_actions = [
                    "Diversify retry strategy before patching (bbox expand, template alternative, fallback profile).",
                    "Track effective_config_fingerprint changes to avoid same-config loops.",
                    "Escalate to patch only if diversity increases but extraction quality remains weak.",
                ]
                _ensure_risk_note(
                    "P1_RETRY_DIVERSITY_GAP",
                    "medium",
                    "Retry strategy appears under-diversified while completion remains usable; tuning should be attempted first.",
                )
                override_tag = "extractor_override:p1_tuning_retry"
            elif strong_extractor_failure or (material_extractor_weakness and persistent_extractor_weakness):
                operator_action_required = True
                branch = "patch_extractor"
                cause = "extractor_logic" if (repeated_empty or block_reason_code == BlockReasonCode.EXTRACTION_EMPTY.value) else "extractor_config"
                confidence = max(confidence, 0.80 if not partial_evidence else 0.72)
                out["patch_task_recommendation"] = _set_patch_recommendation(
                    branch=branch,
                    patch_type="extractor",
                    justification="Extractor degradation is materially established and outweighs diagnostics-first routing.",
                )
                next_actions = [
                    "Create extractor patch task focused on template/selection/retry configuration for this area group.",
                    "Re-test same-case and compare candidate_count, resolve_count, and completion-quality metrics.",
                    "Keep validator gates unchanged while patch is evaluated.",
                ]
                if target_option_received and (spatial_execution_broken or repeated_spatial_noop):
                    next_actions.insert(
                        0,
                        "Patch extractor spatial interpretation first: target intent was not converted into a usable bbox/sector plan and retries did not meaningfully change it.",
                    )
                if contradictions_found:
                    next_actions.append(
                        "Capture contradictions as a secondary diagnostics note, but do not suppress the extractor patch path."
                    )
                _ensure_risk_note(
                    "P1_REPEATED_EMPTY_ESCALATION",
                    "high",
                    "Extractor degradation is materially established, so extractor patching takes precedence over diagnostics-first routing.",
                )
                override_tag = "extractor_override:p1_patch_extractor_primary"
            elif moderate_extractor_weakness and not persistent_extractor_weakness:
                branch = "tuning_retry"
                cause = "extractor_config"
                confidence = max(confidence, 0.61)
                operator_action_required = False
                out["patch_task_recommendation"] = _set_patch_recommendation(
                    branch=branch,
                    patch_type=None,
                    justification=None,
                )
                next_actions = [
                    "Run a bounded diversified retry before patch escalation.",
                    "If completion-quality or efficiency stays below threshold on repeated attempts, escalate to extractor patch next.",
                ]
                if contradictions_found:
                    next_actions.append(
                        "Track contradiction evidence as secondary diagnostics, but current extractor weakness is not yet persistent enough for patch-first escalation."
                    )
                _ensure_risk_note(
                    "P1_EXTRACTOR_WEAKNESS_NOT_YET_PERSISTENT",
                    "medium",
                    "Extractor weakness is present but not yet persistent; tuning retry is preferred before patching.",
                )
                override_tag = "extractor_override:p1_tuning_retry_moderate"
            elif needed and partial_evidence:
                if severity in {"high", "medium"}:
                    branch = "patch_diagnostics"
                    cause = "diagnostics_visibility"
                    confidence = max(confidence, 0.62)
                    operator_action_required = True
                    out["patch_task_recommendation"] = _set_patch_recommendation(
                        branch=branch,
                        patch_type="diagnostics",
                        justification="Extractor-help signal is active but evidence is partial and needs better diagnostics.",
                    )
                    next_actions = [
                        "Improve extraction diagnostics contract before applying extractor logic patch.",
                        "Re-run with complete attempt telemetry (timings, fingerprints, fallback signals).",
                    ]
                    override_tag = "extractor_override:p1_partial_patch_diagnostics"
                else:
                    branch = "tuning_retry"
                    cause = "extractor_config"
                    confidence = max(confidence, 0.55)
                    operator_action_required = False
                    out["patch_task_recommendation"] = _set_patch_recommendation(
                        branch=branch,
                        patch_type=None,
                        justification=None,
                    )
                    next_actions = [
                        "Run bounded diversified retry to gather stronger evidence before patch escalation.",
                        "If weakness persists with fuller evidence, revisit diagnostics or extractor patch path.",
                    ]
                    override_tag = "extractor_override:p1_partial_tuning"
            elif needed and severity in {"medium", "high"}:
                branch = "patch_extractor"
                cause = "extractor_logic" if reason_class in {"fallback_rescue_failed", "repeated_empty_extraction"} else "extractor_config"
                confidence = max(confidence, 0.72)
                operator_action_required = True
                out["patch_task_recommendation"] = _set_patch_recommendation(
                    branch=branch,
                    patch_type="extractor",
                    justification="Extractor-help signal indicates persistent extractor weakness with sufficient evidence.",
                )
                next_actions = [
                    "Generate targeted extractor patch task and keep gate/approval boundaries unchanged.",
                    "Re-test same corridor and compare before/after extraction metrics and downstream quality.",
                ]
                if target_option_received and (spatial_execution_broken or repeated_spatial_noop):
                    next_actions.insert(
                        0,
                        "Patch extractor spatial interpretation first: target intent is present but the runtime spatial plan is failing or being reused without meaningful change.",
                    )
                override_tag = "extractor_override:p1_patch_extractor"
            elif raw_generic:
                branch = "no_patch_continue"
                cause = "extractor_config"
                confidence = max(confidence, 0.52)
                operator_action_required = False
                out["patch_task_recommendation"] = _set_patch_recommendation(
                    branch=branch,
                    patch_type=None,
                    justification=None,
                )
                next_actions = [
                    "Continue under current policy and monitor extractor health trend for this area group.",
                ]
                override_tag = "extractor_override:p1_no_patch_continue"

        elif phase == "phase3":
            weak_extractor_signal = bool(help_reason_is_extractor)
            step20_poor = bool(
                ("extraction_success_but_step20_poor" in reason_codes)
                or (order_completion_quality_score is not None and order_completion_quality_score <= 0.55)
                or (step20_gate_passed is False)
            )
            extractor_efficient = bool(efficiency_score is not None and efficiency_score >= 0.60)

            if phase3_bundle_first_override:
                operator_action_required = True
                branch = "patch_diagnostics" if partial_evidence or bool(phase3_bundle.get("fetch_observability_gap")) else "patch_detector_scoring"
                cause = "diagnostics_visibility" if branch == "patch_diagnostics" else "detector_thresholds"
                confidence = max(confidence, 0.76 if branch == "patch_diagnostics" else 0.72)
                patch_type = "diagnostics" if branch == "patch_diagnostics" else "detector_scoring"
                out["patch_task_recommendation"] = _set_patch_recommendation(
                    branch=branch,
                    patch_type=patch_type,
                    justification=(
                        "P3.1 has strong selected-route bundle evidence; remaining failure is fetch/diagnostics inconsistency, not extractor emptiness."
                    ),
                )
                next_actions = [
                    "Do not patch extractor yet; selected relation, route_id, and prior-stop evidence indicate bundle extraction succeeded provisionally.",
                    "Patch diagnostics or detector/scoring visibility around Step10 fetch and validator payload reconciliation.",
                    "Re-run the same route and compare fetch classification, prior-stop evidence counts, and contradiction codes before/after patch.",
                ]
                _ensure_risk_note(
                    "P3_BUNDLE_FIRST_FETCH_GAP",
                    "high",
                    "Strong Phase3 bundle evidence is present, so fetch/diagnostics inconsistency takes precedence over extractor patch routing.",
                )
                override_tag = "extractor_override:p3_bundle_first_fetch_gap"
            elif needed and step20_available and step20_poor and extractor_efficient:
                operator_action_required = True
                if ambiguous_count > 0 or unmatched_count > 0:
                    if ambiguous_count >= unmatched_count and ambiguous_count > 0:
                        cause = "matching_ambiguity"
                    else:
                        cause = "node_db_gap"
                    branch = "phase1_new_nodes"
                    approval_type_if_needed = "RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS"
                    confidence = max(confidence, 0.78)
                    out["patch_task_recommendation"] = _set_patch_recommendation(
                        branch=branch,
                        patch_type=None,
                        justification=None,
                    )
                    next_actions = [
                        "Route blocking unmatched/ambiguous stops to Phase1 New Nodes resolution flow.",
                        "Require operator confirmation and promote before Step20 rerun.",
                        "Resume Step20 automatically after resolution persistence.",
                    ]
                    override_tag = "extractor_override:p3_phase1_new_nodes"
                elif partial_evidence:
                    branch = "patch_diagnostics"
                    cause = "diagnostics_visibility"
                    confidence = max(confidence, 0.66)
                    out["patch_task_recommendation"] = _set_patch_recommendation(
                        branch=branch,
                        patch_type="diagnostics",
                        justification="Step20 poor outcome with efficient extraction but partial evidence limits root-cause certainty.",
                    )
                    next_actions = [
                        "Improve Step20/extractor diagnostics packaging before changing extractor logic.",
                        "Re-run extraction + Step20 with full evidence bundle.",
                    ]
                    override_tag = "extractor_override:p3_step20_poor_partial"
                else:
                    branch = "patch_detector_scoring"
                    cause = "detector_thresholds"
                    confidence = max(confidence, 0.75)
                    out["patch_task_recommendation"] = _set_patch_recommendation(
                        branch=branch,
                        patch_type="detector_scoring",
                        justification="Extraction appears efficient while Step20 outcomes remain poor without node-gap evidence.",
                    )
                    next_actions = [
                        "Create detector/scoring patch task for Step20 threshold/classification quality.",
                        "Validate with before/after Step20 blocker and warning subtype comparisons.",
                    ]
                    override_tag = "extractor_override:p3_detector_scoring"
            elif (strong_extractor_failure or (material_extractor_weakness and persistent_extractor_weakness)) and step20_available and step20_poor:
                operator_action_required = True
                if partial_evidence or help_conf < 0.50:
                    branch = "patch_diagnostics"
                    cause = "diagnostics_visibility"
                    confidence = max(confidence, 0.64)
                    out["patch_task_recommendation"] = _set_patch_recommendation(
                        branch=branch,
                        patch_type="diagnostics",
                        justification="P3 extractor weakness is present but evidence quality is not strong enough for direct extractor patch.",
                    )
                    next_actions = [
                        "Patch diagnostics first, then rerun extraction + Step20 for stronger attribution.",
                    ]
                    override_tag = "extractor_override:p3_weak_partial_diagnostics"
                else:
                    branch = "patch_extractor"
                    cause = "extractor_logic"
                    confidence = max(confidence, 0.78)
                    out["patch_task_recommendation"] = _set_patch_recommendation(
                        branch=branch,
                        patch_type="extractor",
                        justification="Extractor degradation is materially established and remains the primary patch target despite contradictions or poor Step20 outcome.",
                    )
                    next_actions = [
                        "Create route extractor patch task focused on candidate viability and fallback behavior.",
                        "Re-test extraction and Step20 to confirm reduced blocker recurrence.",
                    ]
                    if candidate_universe_weak or hard_filter_overreach:
                        next_actions.insert(
                            0,
                            "Patch Phase3 discover strategy first: broaden bbox-first candidate universe and reduce hard metadata filtering before ranking.",
                        )
                    if contradictions_found:
                        next_actions.append(
                            "Record contradictions as a secondary diagnostics follow-up after extractor patch validation."
                        )
                    override_tag = "extractor_override:p3_patch_extractor"
            elif needed and not step20_available:
                if moderate_extractor_weakness and not persistent_extractor_weakness:
                    branch = "tuning_retry"
                    cause = "extractor_config"
                    confidence = max(confidence, 0.52)
                    operator_action_required = False
                    out["patch_task_recommendation"] = _set_patch_recommendation(
                        branch=branch,
                        patch_type=None,
                        justification=None,
                    )
                    next_actions = [
                        "Pre-Step20 extractor weakness is not yet persistent; run bounded diversified retries first.",
                        "Escalate to extractor patch if repeated attempts stay below completion/efficiency thresholds.",
                    ]
                    override_tag = "extractor_override:p3_pre_step20_tuning_moderate"
                elif partial_evidence and severity in {"low", "medium"}:
                    branch = "tuning_retry"
                    cause = "extractor_config"
                    confidence = max(confidence, 0.52)
                    operator_action_required = False
                    out["patch_task_recommendation"] = _set_patch_recommendation(
                        branch=branch,
                        patch_type=None,
                        justification=None,
                    )
                    next_actions = [
                        "Pre-Step20 evidence is partial; run bounded diversified retries before patch escalation.",
                        "Collect richer attempt telemetry to reduce uncertainty.",
                    ]
                    override_tag = "extractor_override:p3_pre_step20_tuning"
                elif severity == "high" and attempt_count_total >= 3 and material_extractor_weakness and not partial_evidence:
                    branch = "patch_extractor"
                    cause = "extractor_logic"
                    confidence = max(confidence, 0.74)
                    operator_action_required = True
                    out["patch_task_recommendation"] = _set_patch_recommendation(
                        branch=branch,
                        patch_type="extractor",
                        justification="Severe repeated extraction weakness before Step20 with sufficient attempt evidence.",
                    )
                    next_actions = [
                        "Create extractor patch task and re-run extraction, then Step20 for downstream impact validation.",
                    ]
                    if target_option_received and (spatial_execution_broken or repeated_spatial_noop):
                        next_actions.insert(
                            0,
                            "Patch Phase3 geographic interpretation first: target intent did not produce a usable bbox plan and repeated retries did not materially change the spatial execution path.",
                        )
                    override_tag = "extractor_override:p3_pre_step20_patch"
                else:
                    branch = "manual_review_required"
                    cause = "diagnostics_visibility"
                    confidence = min(confidence, 0.58)
                    operator_action_required = True
                    out["patch_task_recommendation"] = _set_patch_recommendation(
                        branch=branch,
                        patch_type=None,
                        justification=None,
                    )
                    next_actions = [
                        "Evidence is partial/conflicting pre-Step20; require operator review before patch path.",
                        "Prefer diagnostics improvements if uncertainty persists across retries.",
                    ]
                    override_tag = "extractor_override:p3_pre_step20_manual"
            elif raw_generic:
                branch = "no_patch_continue"
                cause = "detector_thresholds"
                confidence = max(confidence, 0.50)
                operator_action_required = False
                out["patch_task_recommendation"] = _set_patch_recommendation(
                    branch=branch,
                    patch_type=None,
                    justification=None,
                )
                next_actions = [
                    "Continue with traceability; extractor-help signal is not indicating escalation.",
                ]
                override_tag = "extractor_override:p3_no_patch_continue"

        if not override_tag:
            return out, None

        if contradictions_found and branch == "patch_extractor":
            secondary = list(out.get("secondary_causes") or [])
            if "diagnostics_visibility" not in [str(x or "").strip() for x in secondary]:
                secondary.append("diagnostics_visibility")
            out["secondary_causes"] = secondary[:8]
            _ensure_risk_note(
                "EXTRACTOR_PATCH_PRECEDENCE_OVER_CONTRADICTIONS",
                "medium",
                "Contradictions remain secondary diagnostics evidence, but extractor degradation is strong enough to keep patch_extractor as the primary route.",
            )

        geography_degradation_detected = bool(
            str(geo_quality_attr.get("likely_cause") or "").strip() in {
                "geography_interpretation",
                "geography_degradation",
                "route_hint_overreach",
            }
        )
        if geography_degradation_detected and branch == "patch_extractor":
            geography_escalation_reason = str(geo_quality_attr.get("likely_cause") or "geography_interpretation")
            cause = "geography_interpretation"
            secondary = list(out.get("secondary_causes") or [])
            if "extractor_logic" not in [str(x or "").strip() for x in secondary]:
                secondary.append("extractor_logic")
            out["secondary_causes"] = secondary[:8]
            if spatial_execution_broken:
                next_actions.insert(
                    0,
                    "Patch geography interpretation first: low extraction quality correlates with weak/failed geographic interpretation, not extractor logic.",
                )
            else:
                next_actions.insert(
                    0,
                    f"Geography degradation detected ({geography_escalation_reason}): review geographic interpretation quality before patching extractor logic.",
                )
            _ensure_risk_note(
                "GEOGRAPHY_INTERPRETATION_LIKELY_CAUSE",
                "high" if spatial_execution_broken else "medium",
                (
                    f"Geography quality attribution indicates {geography_escalation_reason} is the likely cause of low quality "
                    f"(geo_confidence={geo_quality_attr.get('interpretation_confidence')}, "
                    f"fallback={geo_quality_attr.get('fallback_used')}, "
                    f"bbox_status={geo_quality_attr.get('bbox_validation_status')}, "
                    f"route_hint_overreach={geo_quality_attr.get('route_hint_overreach')}, "
                    f"geography_degradation={geo_quality_attr.get('geography_degradation')})."
                ),
            )

        summary_reason = reason_class or (sorted(reason_codes)[0] if reason_codes else "unspecified")
        partial_txt = "partial_evidence=true" if partial_evidence else "partial_evidence=false"
        contradictions_txt = ""
        if contradictions_found and branch == "patch_extractor":
            contradictions_txt = " Contradictions were retained as secondary diagnostics evidence, but did not override the extractor patch path."
        out["summary"] = (
            f"{phase} {step_id} extractor-aware interpretation selected `{branch}` "
            f"(cause `{cause}`) using extractor_help_needed "
            f"(needed={str(bool(needed)).lower()}, severity={severity}, reason={summary_reason}, {partial_txt})."
            f"{contradictions_txt}"
        )
        out["recommended_branch"] = branch
        out["dominant_cause_class"] = cause
        out["confidence"] = round(float(max(0.0, min(1.0, confidence))), 2)
        out["recommended_next_actions"] = list(dict.fromkeys([str(x).strip() for x in next_actions if str(x).strip()]))[:16]
        out["operator_action_required"] = bool(operator_action_required)
        out["approval_type_if_needed"] = approval_type_if_needed

        if "patch_task_recommendation" not in out or not isinstance(out.get("patch_task_recommendation"), dict):
            out["patch_task_recommendation"] = {
                "should_create_patch_task": False,
                "patch_type": None,
                "justification": None,
                "suggested_target": None,
            }

        _ensure_risk_note(
            "EXTRACTOR_HELP_SIGNAL_APPLIED",
            "medium",
            "Interpreter branch/cause recommendation was refined using deterministic extractor_help_needed signals.",
        )
        if partial_evidence:
            _ensure_risk_note(
                "PARTIAL_EVIDENCE_CAUTION",
                "medium",
                "Partial evidence reduced escalation aggressiveness and confidence.",
            )
        return out, override_tag

    def __call__(
        self,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
        trigger: str,
    ) -> ChatGPTSnapshot:
        task = self._task_for_call(step=step, trigger=trigger, validator_result=validator_result)
        snapshot = self._build_normalized_snapshot(
            state=state,
            step=step,
            attempt_no=attempt_no,
            exec_result=exec_result,
            validator_result=validator_result,
            ai_snapshot=ai_snapshot,
            trigger=trigger,
        )
        envelope = {
            "task": task,
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
            "snapshot": snapshot,
            "operator_context": dict(state.operator_context or {}),
        }
        if self._advisory_mode == "real_advisory" and self.init_error is not None:
            init_err = str(self.init_error or "").strip() or "real advisory service initialization failed"
            return self._error_snapshot(
                task=task,
                trigger=trigger,
                snapshot=snapshot,
                error_code="REAL_ADVISORY_PROVIDER_UNAVAILABLE",
                error_summary=init_err,
                risk_notes=[
                    "REAL_ADVISORY_PROVIDER_UNAVAILABLE",
                    "REAL_ADVISORY_FALLBACK_BLOCKED",
                    "manual_review_required",
                ],
            )
        consistency_precheck: Dict[str, Any] = {}
        if task == self._HADES_INTERPRETER_TASK:
            checker_task = self._HADES_CONSISTENCY_TASK
            checker_envelope = {
                "task": checker_task,
                "safety_context": dict(envelope.get("safety_context") or {}),
                "snapshot": dict(envelope.get("snapshot") or {}),
                "operator_context": dict(envelope.get("operator_context") or {}),
            }
            try:
                consistency_precheck, _ = self._call_task_detailed(task=checker_task, envelope=checker_envelope)
            except Exception as checker_exc:
                consistency_precheck = {
                    "contradictions_found": False,
                    "contradictions": [],
                    "consistency_summary": f"consistency_checker_unavailable: {checker_exc}",
                }
            envelope["snapshot"]["consistency_precheck"] = dict(consistency_precheck or {})
        try:
            response, meta = self._call_task_detailed(task=task, envelope=envelope)
        except Exception as exc:
            err = str(exc or "").strip() or "unknown advisory failure"
            lower_err = err.lower()
            code = "chatgpt_call_failed"
            detail = dict(getattr(exc, "detail", {}) or {})
            if detail.get("error_code"):
                code = str(detail.get("error_code") or code)
            elif any(marker in lower_err for marker in ["schema", "validation", "policy"]):
                code = "chatgpt_schema_validation_failed"
            risk_notes = [code]
            if self._advisory_mode == "real_advisory":
                risk_notes.extend(["REAL_ADVISORY_FALLBACK_BLOCKED", "manual_review_required"])
            else:
                risk_notes.append("manual_review_required")
            snap = self._error_snapshot(
                task=task,
                trigger=trigger,
                snapshot=dict(envelope.get("snapshot") or {}),
                error_code=code,
                error_summary=str(detail.get("error_summary") or err),
                risk_notes=risk_notes,
            )
            if detail:
                snap.model = (str(detail.get("model")) if detail.get("model") else None)
                snap.snapshot_id = (str(detail.get("snapshot_id")) if detail.get("snapshot_id") else snap.snapshot_id)
                snap.schema_name = (str(detail.get("schema_name")) if detail.get("schema_name") else snap.schema_name)
                snap.requested_mode = (str(detail.get("requested_mode")) if detail.get("requested_mode") else snap.requested_mode)
                if detail.get("fallback_used") is not None:
                    snap.fallback_used = bool(detail.get("fallback_used"))
                prompt_pkg = dict(snap.prompt_package or {})
                prompt_pkg["advisory_error"] = dict(detail)
                snap.prompt_package = prompt_pkg
            return snap
        guard_issue = None
        if task == self._HADES_INTERPRETER_TASK:
            response, guard_issue = self._hades_quality_guard(response=response)
            response, extractor_override_issue = self._extractor_signal_routing_override(
                snapshot=snapshot,
                response=response,
                consistency_precheck=consistency_precheck,
            )
        else:
            extractor_override_issue = None

        priorities = list(
            response.get("prioritized_improvements")
            or response.get("recommended_actions")
            or response.get("recommended_next_actions")
            or []
        )
        priority_labels: List[str] = []
        for row in priorities:
            if isinstance(row, dict):
                txt = str(row.get("action") or row.get("improvement") or row.get("priority") or "").strip()
                if txt:
                    priority_labels.append(txt)
            elif str(row or "").strip():
                priority_labels.append(str(row).strip())

        branch = str(response.get("recommended_branch") or "").strip()
        if branch:
            priority_labels.insert(0, f"branch:{branch}")
        cause = str(response.get("dominant_cause_class") or "").strip()
        if cause:
            priority_labels.insert(0, f"cause:{cause}")

        summary = str(response.get("summary") or response.get("message") or "").strip() or (
            f"Interpretation complete for {step.step_id}."
        )
        risk_notes: List[str] = []
        for raw in list(response.get("risk_flags") or []):
            if isinstance(raw, dict):
                code = str(raw.get("code") or "").strip()
                msg = str(raw.get("message") or "").strip()
                sev = str(raw.get("severity") or "").strip()
                txt = " ".join(x for x in [code, sev, msg] if x)
                if txt:
                    risk_notes.append(txt)
            elif str(raw or "").strip():
                risk_notes.append(str(raw).strip())
        for raw in list(response.get("risk_notes") or []):
            if isinstance(raw, dict):
                code = str(raw.get("code") or "").strip()
                msg = str(raw.get("message") or "").strip()
                sev = str(raw.get("severity") or "").strip()
                txt = " ".join(x for x in [code, sev, msg] if x)
                if txt:
                    risk_notes.append(txt)
            elif str(raw or "").strip():
                risk_notes.append(str(raw).strip())

        patch = dict(response.get("patch_task_recommendation") or {})
        if bool(patch.get("should_create_patch_task")):
            ptype = str(patch.get("patch_type") or "").strip() or "unknown"
            target = str(patch.get("suggested_target") or "").strip() or "unspecified"
            priority_labels.append(f"patch:{ptype}:{target}")
            patch_why = str(patch.get("justification") or "").strip()
            if patch_why:
                risk_notes.append(f"patch_recommendation {patch_why}")
        if guard_issue:
            priority_labels.append("interpreter:low_quality_output")
            risk_notes.append(f"interpreter_output_guard:{guard_issue}")
        if extractor_override_issue:
            priority_labels.append(extractor_override_issue)
        if bool(consistency_precheck.get("contradictions_found")):
            priority_labels.append("consistency:contradictions_found")
            risk_notes.append(str(consistency_precheck.get("consistency_summary") or "consistency_checker_detected_mismatch"))
        prompt_package = dict(meta.get("prompt_package") or {})
        if task == self._HADES_INTERPRETER_TASK:
            prompt_package["interpreter_structured"] = {
                "recommended_branch": str(response.get("recommended_branch") or "").strip() or None,
                "dominant_cause_class": str(response.get("dominant_cause_class") or "").strip() or None,
                "confidence": _handoff_confidence_to_numeric(response.get("confidence")),
                "patch_task_recommendation": dict(response.get("patch_task_recommendation") or {}),
                "operator_action_required": bool(response.get("operator_action_required", False)),
                "approval_type_if_needed": response.get("approval_type_if_needed"),
            }
        return ChatGPTSnapshot(
            summary=summary,
            priorities=priority_labels[:8],
            risk_notes=risk_notes[:12],
            status="ok",
            task=task,
            trigger=trigger,
            model=(str(meta.get("model")) if meta.get("model") else None),
            latency_ms=(int(meta.get("latency_ms")) if meta.get("latency_ms") is not None else None),
            token_usage=dict(meta.get("token_usage") or {}),
            snapshot_id=(str(meta.get("snapshot_id")) if meta.get("snapshot_id") else None),
            source=(str(meta.get("source")) if meta.get("source") else None),
            schema_name=(str(meta.get("schema_name")) if meta.get("schema_name") else None),
            requested_mode=(str(meta.get("requested_mode")) if meta.get("requested_mode") else self._advisory_mode),
            fallback_used=(
                bool(meta.get("fallback_used"))
                if meta.get("fallback_used") is not None
                else False
            ),
            prompt_package=prompt_package,
        )

    def generate_patch_task(
        self,
        *,
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        trigger: str,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
        ai_snapshot: AIBotSnapshot,
        chatgpt_snapshot: ChatGPTSnapshot,
        patch_intent: Dict[str, Any],
    ) -> Dict[str, Any]:
        base_snapshot = _as_dict(dict(chatgpt_snapshot.prompt_package or {}).get("snapshot"))
        if not base_snapshot:
            base_snapshot = self._build_normalized_snapshot(
                state=state,
                step=step,
                attempt_no=attempt_no,
                exec_result=exec_result,
                validator_result=validator_result,
                ai_snapshot=ai_snapshot,
                trigger=trigger,
            )

        enriched_snapshot = dict(base_snapshot)
        enriched_snapshot["interpreter_result"] = {
            "summary": str(chatgpt_snapshot.summary or ""),
            "priorities": list(chatgpt_snapshot.priorities or []),
            "risk_notes": list(chatgpt_snapshot.risk_notes or []),
            "task": str(chatgpt_snapshot.task or ""),
            "trigger": str(chatgpt_snapshot.trigger or trigger or ""),
            "snapshot_id": chatgpt_snapshot.snapshot_id,
        }
        enriched_snapshot["patch_intent"] = dict(patch_intent or {})
        enriched_snapshot["patch_chain_context"] = {
            "autopilot_mode": "supervised",
            "run_id": str(state.run_id or ""),
            "trace_id": str(state.trace_id or ""),
            "phase": str(step.phase or ""),
            "step_id": str(step.step_id or ""),
            "attempt_no": int(attempt_no),
            "policy_profile": str(state.policy_profile or ""),
            "authority_constraints": {
                "no_gate_bypass": True,
                "no_auto_approve_sensitive_actions": True,
                "no_auto_patch_apply": True,
                "no_auto_merge": True,
            },
        }

        envelope = {
            "task": "hades_patch_task_generator",
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
            "snapshot": enriched_snapshot,
            "operator_context": dict(state.operator_context or {}),
        }
        response, meta = self._call_task_detailed(task="hades_patch_task_generator", envelope=envelope)
        return {
            "response": dict(response or {}),
            "meta": dict(meta or {}),
        }

    def compare_retest_outcome(
        self,
        *,
        patch_record: Dict[str, Any],
        baseline_run_snapshot: Dict[str, Any],
        retest_run_snapshot: Dict[str, Any],
        change_type: Optional[str] = None,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        record = dict(patch_record or {})
        phase = str(record.get("phase") or "phase3")
        step_focus = str(record.get("step_id") or "P3.2_SEQUENCE_STEP20")
        patch_type = str(record.get("patch_type") or "").strip()
        inferred_change_type = str(change_type or "").strip()
        if not inferred_change_type:
            inferred_change_type = f"patch_{patch_type}" if patch_type else "tuning"
        snapshot = {
            "task": "hades_retest_comparator",
            "change_type": inferred_change_type,
            "phase": phase,
            "step_focus": step_focus,
            "baseline_run_snapshot": dict(baseline_run_snapshot or {}),
            "retest_run_snapshot": dict(retest_run_snapshot or {}),
            "extra_context": {
                "patch_task_id": str(record.get("patch_task_id") or ""),
                "patch_branch": str(record.get("patch_branch") or ""),
                "patch_type": patch_type or None,
                "policy_profile_before": str(record.get("policy_profile") or ""),
                "policy_profile_after": str(record.get("policy_profile") or ""),
                **dict(extra_context or {}),
            },
            "evidence_refs": [
                {
                    "evidence_id": str(record.get("patch_task_id") or ""),
                    "kind": "patch_registry",
                    "description": "Patch registry context for retest comparator",
                }
            ],
        }
        envelope = {
            "task": "hades_retest_comparator",
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
            "snapshot": snapshot,
            "operator_context": {},
        }
        response, meta = self._call_task_detailed(task="hades_retest_comparator", envelope=envelope)
        return {
            "response": dict(response or {}),
            "meta": dict(meta or {}),
        }


def build_ai_bot_telemetry_hook(*, insights_service: Any = None) -> AIBotFn:
    """
    AI Bot adapter using existing AI insights telemetry comparison service.
    """

    try:
        from datamind_console.ai_insights.service import AIInsightsService
    except Exception:
        AIInsightsService = None  # type: ignore[assignment]

    if insights_service is not None:
        svc = insights_service
    else:
        svc = AIInsightsService() if AIInsightsService is not None else None

    def _hook(
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        exec_result: ExecutorResult,
        validator_result: ValidatorResult,
    ) -> AIBotSnapshot:
        compare: Dict[str, Any] = {}
        stage_aliases = _handoff_stage_aliases(step.step_id)
        if svc is not None:
            try:
                compare_candidates: List[tuple[str, Dict[str, Any]]] = []
                for stage_alias in stage_aliases:
                    raw = dict(svc.compare_latest_vs_recent(phase=step.phase, stage=stage_alias, lookback=20) or {})
                    if not raw:
                        continue
                    compare_candidates.append((stage_alias, raw))
                    if bool(raw.get("ok")) and int(raw.get("history_count") or 0) > 0:
                        compare = raw
                        break
                if not compare and compare_candidates:
                    compare = dict(compare_candidates[0][1] or {})
                if not compare:
                    compare = dict(svc.compare_latest_vs_recent(phase=step.phase, stage=None, lookback=20) or {})
                selected_stage = str(compare.get("stage") or "") or None
                stage_resolution: Dict[str, Any] = {
                    "canonical_step_id": step.step_id,
                    "attempted_aliases": stage_aliases,
                    "selected_stage": selected_stage,
                }
                compare_reason = str(compare.get("reason") or "").strip()
                if selected_stage is None and compare_reason == "invalid_phase" and step.step_id:
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "STAGE_RESOLUTION_ERROR: canonical_step_id=%s present but compare returned "
                        "reason='invalid_phase'. Phase normalization may be incomplete.",
                        step.step_id,
                    )
                    stage_resolution["stage_resolution_error"] = True
                    stage_resolution["reason"] = "canonical_step_found_but_resolution_failed"
                    stage_resolution["compare_reason"] = compare_reason
                compare["stage_resolution"] = stage_resolution
            except Exception:
                compare = {}

        flags = list(compare.get("regression_flags") or [])
        regression_flags = _handoff_extract_regression_flags(compare)
        warnings = [str(x).strip() for x in list(validator_result.warnings or []) if str(x).strip()]
        anomaly_flags = list(validator_result.anomalies or [])
        for code in list(regression_flags):
            if str(code or "").strip():
                anomaly_flags.append(str(code).strip())

        summary = dict(exec_result.summary or {})
        evidence = dict(validator_result.evidence or {})
        reorder = _handoff_extract_reorder_signal(
            exec_summary=summary,
            validator_evidence=evidence,
            ai_proposals={},
        )
        compare_latest = dict(compare.get("latest") or {})
        compare_latest_metrics = dict(compare.get("latest_metrics") or {})
        if reorder.get("recommended") is None and compare.get("latest_reorder_recommended") is not None:
            parsed_recommended = _handoff_to_bool(compare.get("latest_reorder_recommended"))
            if parsed_recommended is not None:
                reorder["recommended"] = parsed_recommended
        if reorder.get("confidence") is None:
            for raw in [
                compare_latest.get("reorder_confidence"),
                compare_latest_metrics.get("reorder_confidence"),
                evidence.get("reorder_confidence"),
                summary.get("reorder_confidence"),
            ]:
                parsed_conf = _handoff_confidence_to_numeric(raw)
                if parsed_conf is not None:
                    reorder["confidence"] = parsed_conf
                    break

        extractor_status = _build_extractor_status(
            state=state,
            step=step,
            attempt_no=attempt_no,
            exec_summary=summary,
            validator_result=validator_result,
            compare=compare,
            reorder=reorder,
        )
        extractor_help_needed = _build_extractor_help_needed(
            step=step,
            extractor_status=extractor_status,
            validator_result=validator_result,
        )
        payload = build_ai_bot_snapshot(
            scores={
                "quality_score": pick_quality_score_01(
                    [
                        summary.get("quality_score"),
                        evidence.get("quality_score"),
                        compare_latest.get("quality_score"),
                        compare_latest_metrics.get("quality_score"),
                    ],
                    fallback_validator_status=validator_result.status,
                ),
                "sequence_quality_score": pick_sequence_quality_score_100(
                    [
                        summary.get("sequence_quality_score"),
                        evidence.get("sequence_quality_score"),
                        compare_latest.get("sequence_quality_score"),
                        compare_latest_metrics.get("sequence_quality_score"),
                    ]
                ),
            },
            metrics={
                "phase": step.phase,
                "step_id": step.step_id,
                "attempt_no": int(attempt_no),
                "validator_status": validator_result.status,
                "regression_flag_count": len(regression_flags),
                "failed_stage": _as_dict(extractor_status.get("completion_metrics")).get("failed_stage"),
                "adaptive_retry_plan": _as_dict(summary.get("adaptive_retry_plan")),
                "adaptive_retry_reordered": _handoff_to_bool(_as_dict(summary.get("adaptive_retry_plan")).get("reordered")),
                "adaptive_retry_history_support_level": _as_dict(summary.get("adaptive_retry_plan")).get("history_support_level"),
                "adaptive_retry_escalation_bias": _as_dict(summary.get("adaptive_retry_plan")).get("escalation_bias"),
                "shadow_learned_retry_plan": _as_dict(summary.get("shadow_learned_retry_plan")),
                "shadow_retry_plan_comparison": _as_dict(summary.get("shadow_retry_plan_comparison")),
                "shadow_learned_retry_available": _handoff_to_bool(
                    _as_dict(summary.get("shadow_learned_retry_plan")).get("available")
                ),
                "shadow_retry_comparison_status": _as_dict(summary.get("shadow_retry_plan_comparison")).get("comparison_status"),
                "shadow_retry_top1_match": _handoff_to_bool(
                    _as_dict(summary.get("shadow_retry_plan_comparison")).get("top1_match")
                ),
                "spatial_interpretation": _as_dict(summary.get("spatial_interpretation")),
                "spatial_interpretation_status": _as_dict(summary.get("spatial_interpretation")).get("spatial_interpretation_status"),
                "target_option_received": _handoff_to_bool(
                    _as_dict(summary.get("spatial_interpretation")).get("target_option_received")
                ),
                "retry_changed_spatial_plan": _handoff_to_bool(
                    _as_dict(summary.get("spatial_interpretation")).get("retry_changed_spatial_plan")
                ),
            },
            warnings=warnings,
            anomaly_flags=list(dict.fromkeys(anomaly_flags)),
            proposals={
                "reorder_proposal": bool(reorder.get("recommended")) if reorder.get("recommended") is not None else False,
                "reorder": reorder,
                "comparison": compare,
                "regression_flags": flags,
            },
            extractor_status=extractor_status,
            extractor_help_needed=extractor_help_needed,
        )
        return AIBotSnapshot(**payload)

    return _hook


def build_phase_client_executor_bridge(
    *,
    phase1_client: Any = None,
    phase2_client: Any = None,
    phase3_client: Any = None,
    geography_resolver: Any = None,
) -> Dict[str, ExecutorFn]:
    """Bridge all registry executor names to concrete runtime paths."""

    shared_geography_resolver = geography_resolver or SharedGeographyResolver()

    def _missing(name: str, phase_name: str) -> ExecutorFn:
        def _fn(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del state, step, attempt_no, params
            raise RuntimeError(f"{phase_name} runtime client is required for executor `{name}`")

        return _fn

    def _route_id(state: RunSessionState) -> str:
        return str(
            (state.resume_context.get("route_id") or (state.pipeline_scope.get("phase3") or {}).get("route_id") or "")
        ).strip()

    def _node_set_id(state: RunSessionState) -> str:
        return str(
            (state.resume_context.get("node_set_id") or (state.pipeline_scope.get("phase1") or {}).get("node_set_id") or "")
        ).strip()

    def _phase1_area_group(state: RunSessionState) -> Optional[str]:
        phase1_scope = _as_dict(state.pipeline_scope.get("phase1"))
        extract_scope = _as_dict(phase1_scope.get("extract"))
        area_group = str(
            extract_scope.get("area")
            or phase1_scope.get("area_group")
            or _phase1_to_phase2_handoff(state).get("area_group")
            or ""
        ).strip()
        return area_group or None

    def _build_phase1_to_phase2_handoff(state: RunSessionState, **updates: Any) -> Dict[str, Any]:
        existing = _phase1_to_phase2_handoff(state)
        phase1_seen = any(str(rec.phase or "").strip().lower() == "phase1" for rec in list(state.step_execution_records or []))
        payload: Dict[str, Any] = {
            "artifact_type": PHASE1_TO_PHASE2_HANDOFF_ARTIFACT,
            "session_id": str(existing.get("session_id") or state.run_id or "").strip() or None,
            "phase1_run_id": str(
                existing.get("phase1_run_id")
                or (state.run_id if phase1_seen or str(state.current_phase or "").strip().lower() == "phase1" else "")
                or ""
            ).strip()
            or None,
            "source_node_set_id": str(existing.get("source_node_set_id") or _node_set_id(state) or "").strip() or None,
            "phase1_promote_completed": bool(_handoff_to_bool(existing.get("phase1_promote_completed"))),
            "promote_completed_at": existing.get("promote_completed_at"),
            "phase2_context_key": str(
                existing.get("phase2_context_key") or state.resume_context.get("phase2_context_key") or ""
            ).strip()
            or None,
            "place_set_id": str(existing.get("place_set_id") or state.resume_context.get("place_set_id") or "").strip() or None,
            "place_set_id_source": str(existing.get("place_set_id_source") or "").strip() or None,
            "area_group": existing.get("area_group") or _phase1_area_group(state),
            "resolved_count": _handoff_to_int(existing.get("resolved_count")),
            "approved_count": _handoff_to_int(existing.get("approved_count")),
            "promoted_count": _handoff_to_int(existing.get("promoted_count")),
            "place_candidate_count": _handoff_to_int(existing.get("place_candidate_count")),
            "promote_status": str(existing.get("promote_status") or "").strip() or None,
            "ready_for_phase2": bool(_handoff_to_bool(existing.get("ready_for_phase2"))),
            "ready_for_step25": bool(_handoff_to_bool(existing.get("ready_for_step25"))),
        }
        payload.update(existing)
        payload.update(dict(updates or {}))
        payload["artifact_type"] = PHASE1_TO_PHASE2_HANDOFF_ARTIFACT
        payload["session_id"] = str(payload.get("session_id") or state.run_id or "").strip() or None
        for key in (
            "phase1_run_id",
            "source_node_set_id",
            "phase2_context_key",
            "place_set_id",
            "place_set_id_source",
            "promote_completed_at",
            "area_group",
            "promote_status",
        ):
            payload[key] = str(payload.get(key) or "").strip() or None
        for key in ("resolved_count", "approved_count", "promoted_count", "place_candidate_count"):
            payload[key] = _handoff_to_int(payload.get(key))
        payload["phase1_promote_completed"] = bool(_handoff_to_bool(payload.get("phase1_promote_completed")))
        payload["ready_for_phase2"] = bool(_handoff_to_bool(payload.get("ready_for_phase2")))
        payload["ready_for_step25"] = bool(_handoff_to_bool(payload.get("ready_for_step25")))
        return payload

    def _handoff_artifact(state: RunSessionState, **updates: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        payload = _build_phase1_to_phase2_handoff(state, **updates)
        state.resume_context[PHASE1_TO_PHASE2_HANDOFF_ARTIFACT] = dict(payload)
        return payload, {"artifact_type": PHASE1_TO_PHASE2_HANDOFF_ARTIFACT, "payload": payload}

    def _phase2_precondition_result(
        state: RunSessionState,
        *,
        runtime_context: Dict[str, Any],
        handoff: Dict[str, Any],
        code: BlockReasonCode,
        message: str,
        recommended_action: str,
        precondition_stage: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> ExecutorResult:
        payload = {
            "precondition_status": "blocked",
            "precondition_stage": str(precondition_stage or "").strip() or None,
            "block_reason_code": str(code.value),
            "summary": str(message or "").strip(),
            "message": str(message or "").strip(),
            "recommended_action": str(recommended_action or "").strip() or None,
            "runtime_context": dict(runtime_context or {}),
            "phase1_to_phase2_handoff": dict(handoff or {}),
            "session_id": str(state.run_id or "").strip() or None,
        }
        if details:
            payload.update(dict(details or {}))
        summary = {
            "runtime_context": dict(runtime_context or {}),
            "substep_timings_ms": {},
            "skipped_substeps": {},
            PHASE1_TO_PHASE2_HANDOFF_ARTIFACT: dict(handoff or {}),
            "validator_payload": payload,
        }
        return ExecutorResult(
            ok=True,
            summary=summary,
            artifacts=[{"artifact_type": PHASE1_TO_PHASE2_HANDOFF_ARTIFACT, "payload": dict(handoff or {})}],
        )

    def _validate_phase2_start_preconditions(
        state: RunSessionState,
        *,
        runtime_context: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[ExecutorResult]]:
        source_node_set_id = str(runtime_context.get("source_node_set_id") or _node_set_id(state) or "").strip()
        handoff, _ = _handoff_artifact(
            state,
            source_node_set_id=(source_node_set_id or None),
            phase2_context_key=str(runtime_context.get("context_key") or "").strip() or None,
        )
        if not source_node_set_id:
            handoff, _ = _handoff_artifact(state, ready_for_phase2=True)
            return handoff, None
        if bool(handoff.get("phase1_promote_completed")):
            handoff, _ = _handoff_artifact(state, ready_for_phase2=True)
            return handoff, None

        promote_status_fn = getattr(phase1_client, "get_promote_status", None)
        if callable(promote_status_fn):
            try:
                promote_status = _as_dict(promote_status_fn(source_node_set_id))
            except Exception as exc:
                promote_status = {"lookup_error": str(exc)}
            promote_state = str(promote_status.get("promote_status") or "").strip()
            promoted_count = _handoff_to_int(promote_status.get("n_prod"))
            if promote_state == "promoted_to_node_prod" or int(promoted_count or 0) > 0:
                handoff, _ = _handoff_artifact(
                    state,
                    source_node_set_id=source_node_set_id,
                    phase1_promote_completed=True,
                    promote_completed_at=promote_status.get("last_promoted_at"),
                    promote_status=promote_state or "promoted_to_node_prod",
                    promoted_count=promoted_count,
                    ready_for_phase2=True,
                )
                return handoff, None
            handoff, _ = _handoff_artifact(
                state,
                source_node_set_id=source_node_set_id,
                phase1_promote_completed=False,
                promote_status=promote_state or None,
                promoted_count=promoted_count,
                ready_for_phase2=False,
            )
            message = (
                "Phase 1 promote (P1.4) must complete before Phase 2 can start. "
                f"source_node_set_id={source_node_set_id}, promote_status={promote_state or 'unknown'}"
            )
            return handoff, _phase2_precondition_result(
                state,
                runtime_context=runtime_context,
                handoff=handoff,
                code=BlockReasonCode.PHASE1_PROMOTE_NOT_COMPLETED,
                message=message,
                recommended_action="complete_phase1_promote",
                precondition_stage="phase2_start",
                details={"phase1_lookup": promote_status},
            )

        handoff, _ = _handoff_artifact(
            state,
            source_node_set_id=source_node_set_id,
            phase1_promote_completed=False,
            ready_for_phase2=False,
        )
        return handoff, _phase2_precondition_result(
            state,
            runtime_context=runtime_context,
            handoff=handoff,
            code=BlockReasonCode.PHASE1_PROMOTE_NOT_COMPLETED,
            message=(
                "Phase 1 promote (P1.4) could not be verified before Phase 2 start because "
                "the Phase 1 runtime client is unavailable."
            ),
            recommended_action="complete_phase1_promote",
            precondition_stage="phase2_start",
            details={"phase1_lookup": {"available": False}},
        )

    def _validate_phase2_place_set_preconditions(
        state: RunSessionState,
        *,
        runtime_context: Dict[str, Any],
        place_set_id: Optional[str],
        place_set_id_source: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Optional[ExecutorResult]]:
        psid = str(place_set_id or "").strip() or None
        handoff, _ = _handoff_artifact(
            state,
            phase2_context_key=str(runtime_context.get("context_key") or "").strip() or None,
            place_set_id=psid,
            place_set_id_source=(str(place_set_id_source or "").strip() or None),
            ready_for_step25=bool(psid),
        )
        if not psid:
            handoff, _ = _handoff_artifact(state, ready_for_step25=False)
            return handoff, _phase2_precondition_result(
                state,
                runtime_context=runtime_context,
                handoff=handoff,
                code=BlockReasonCode.PLACE_SET_ID_NOT_FOUND,
                message=(
                    "place_set_id could not be resolved after Step 20 candidate construction. "
                    f"session_id={state.run_id}, phase1_run_id={handoff.get('phase1_run_id')}, "
                    f"context_key={runtime_context.get('context_key')}, "
                    f"source_node_set_id={runtime_context.get('source_node_set_id')}"
                ),
                recommended_action="verify_phase2_candidate_build_output",
                precondition_stage="name_candidates",
                details={
                    "phase1_lookup_key": runtime_context.get("source_node_set_id"),
                    "phase2_lookup_key": runtime_context.get("context_key"),
                },
            )

        list_candidates_fn = getattr(phase2_client, "list_place_candidates_for_set", None)
        if callable(list_candidates_fn):
            try:
                preview_rows = list(list_candidates_fn(psid, limit=1) or [])
            except Exception as exc:
                preview_rows = []
                handoff, _ = _handoff_artifact(state, place_set_validation_error=str(exc))
            if len(preview_rows) <= 0:
                handoff, _ = _handoff_artifact(
                    state,
                    place_set_id=psid,
                    place_candidate_count=0,
                    ready_for_step25=False,
                )
                return handoff, _phase2_precondition_result(
                    state,
                    runtime_context=runtime_context,
                    handoff=handoff,
                    code=BlockReasonCode.PLACE_SET_EMPTY_OR_MISSING,
                    message=f"place_set_id={psid} was resolved but no place candidates were found for Step 25.",
                    recommended_action="re_run_phase2_candidate_build",
                    precondition_stage="name_candidates",
                )
            handoff, _ = _handoff_artifact(
                state,
                place_set_id=psid,
                place_candidate_count=len(preview_rows),
                ready_for_step25=True,
            )
        return handoff, None

    def _scope_bbox(raw: Dict[str, Any]) -> Dict[str, float]:
        box = _coerce_bbox_candidate(_as_dict(raw).get("bbox"))
        return dict(box or _phase1_default_bbox())

    def _shared_geography_scope(state: RunSessionState) -> Dict[str, Any]:
        return _as_dict(state.pipeline_scope.get("geography"))

    def _shared_place_input(
        state: RunSessionState,
        *legacy_values: Any,
    ) -> Optional[str]:
        geo_scope = _shared_geography_scope(state)
        primary = str(geo_scope.get("place_input") or "").strip()
        if primary:
            return primary
        for raw in legacy_values:
            value = str(raw or "").strip()
            if value:
                return value
        return None

    def _shared_ai_assist_requested(state: RunSessionState) -> bool:
        return bool(_handoff_to_bool(_shared_geography_scope(state).get("ai_assist_requested")))

    def _build_phase1_spatial_interpretation(
        *,
        state: RunSessionState,
        extract_conf: Dict[str, Any],
    ) -> Dict[str, Any]:
        conf = dict(extract_conf or {})
        phase_scope = _as_dict(state.pipeline_scope.get("phase1"))
        extract_scope = _as_dict(phase_scope.get("extract"))
        area_text = str(conf.get("area") or extract_scope.get("area") or "").strip()
        target_option_text = str(state.pipeline_scope.get("target_entities") or "").strip()
        place_input = _shared_place_input(state, area_text, target_option_text)
        explicit_bbox = _coerce_bbox_candidate(conf.get("bbox"))
        route_tokens = _normalize_spatial_tokens(
            conf.get("route_tokens"),
            place_input,
            target_option_text,
            area_text,
            conf.get("sector_hint"),
        )
        resolved = dict(
            shared_geography_resolver.resolve(
                phase="phase1",
                place_input=place_input,
                explicit_bbox=explicit_bbox,
                supporting_hints={
                    "legacy_target_entities": (target_option_text or None),
                    "phase1_area_hint": (area_text or None),
                    "route_tokens": list(route_tokens or [])[:8],
                    "area_group_hint": str(conf.get("area_group_hint") or "").strip().lower() or None,
                    "sector_hint": str(conf.get("sector_hint") or "").strip() or None,
                },
                allow_ai_assist=_shared_ai_assist_requested(state),
            )
            or {}
        )

        bbox_candidate = _coerce_bbox_candidate(resolved.get("bbox_candidate"))
        target_option_received = bool(place_input or explicit_bbox)
        interpretation_source = str(
            resolved.get("geographic_interpretation_source")
            or resolved.get("interpretation_source")
            or ""
        ).strip()
        if interpretation_source == "bbox_catalog":
            interpretation_source = "phase1_catalog"
        elif interpretation_source == "sector_catalog":
            interpretation_source = "sector_catalog_alias"
        elif not interpretation_source:
            interpretation_source = "runtime_phase1_spatial_resolver_v2"

        interpretation_status = str(
            resolved.get("geographic_interpretation_status")
            or resolved.get("interpretation_status")
            or ""
        ).strip()
        fallback_reason = str(resolved.get("fallback_reason") or "").strip() or None
        fallback_used = bool(_handoff_to_bool(resolved.get("fallback_used")))
        bbox_validation_status = str(resolved.get("bbox_validation_status") or "").strip() or None
        if bbox_candidate is None and target_option_received and interpretation_status not in {
            "invalid_explicit_bbox",
            "fallback_default_bbox",
        }:
            interpretation_status = "fallback_default_bbox"
            fallback_used = True
            fallback_reason = fallback_reason or "target_intent_unresolved"
            bbox_validation_status = bbox_validation_status or "default_applied"
        elif bbox_candidate is None and not target_option_received:
            interpretation_status = interpretation_status or "default_bbox_only"
            fallback_used = True
            fallback_reason = fallback_reason or "phase1_scope_bbox_unspecified"
            bbox_validation_status = bbox_validation_status or "default_applied"
        elif bbox_candidate is not None and interpretation_status in {"", "unresolved", "ambiguous"}:
            interpretation_status = "ok"
        if explicit_bbox is not None and interpretation_status == "ok":
            strategy_used = "explicit_bbox"
        elif interpretation_source == "sector_catalog_alias" and bbox_candidate is not None:
            strategy_used = "sector_catalog_bbox"
        elif interpretation_source == "phase1_catalog" and bbox_candidate is not None:
            strategy_used = "bbox_catalog_bbox"
        elif interpretation_source == "hades_geography_interpreter" and bbox_candidate is not None:
            strategy_used = "ai_geography_bbox"
        elif bbox_candidate is None:
            strategy_used = "default_bbox_fallback"
        else:
            strategy_used = "shared_geography_bbox"

        runtime_bbox_used = dict(bbox_candidate or _scope_bbox(conf))
        resolved_area_group = str(
            resolved.get("area_group_hint")
            or conf.get("area_group_hint")
            or ""
        ).strip().lower() or None
        resolved_sector = str(
            resolved.get("sector_hint")
            or conf.get("sector_hint")
            or area_text
            or ""
        ).strip() or None
        corridor_hint = str(resolved.get("corridor_hint") or "").strip() or None

        out = {
            "source_text": place_input or None,
            "original_geographic_input": place_input or None,
            "normalized_geographic_input": resolved.get("normalized_geographic_input"),
            "interpreted_place_meaning": (
                resolved.get("interpreted_place_meaning")
                or place_input
                or None
            ),
            "interpreted_by": interpretation_source,
            "target_option": (place_input or ("explicit_bbox" if explicit_bbox is not None else None)),
            "target_option_received": bool(target_option_received),
            "target_option_text": (place_input or None),
            "geographic_input_type": (
                str(resolved.get("geographic_input_type") or "").strip()
                or ("explicit_bbox" if explicit_bbox is not None else "place_input")
            ),
            "spatial_interpretation_attempted": bool(target_option_received or explicit_bbox is not None),
            "spatial_interpretation_status": interpretation_status,
            "spatial_interpretation_source": interpretation_source,
            "geographic_interpretation_source": interpretation_source,
            "geographic_interpretation_status": interpretation_status,
            "interpretation_confidence": _handoff_to_float(
                resolved.get("interpretation_confidence")
                if resolved.get("interpretation_confidence") is not None
                else resolved.get("bbox_candidate_confidence")
            ),
            "bbox_candidate": (dict(bbox_candidate) if bbox_candidate else None),
            "bbox_candidate_confidence": _handoff_to_float(
                resolved.get("bbox_candidate_confidence")
                if resolved.get("bbox_candidate_confidence") is not None
                else resolved.get("interpretation_confidence")
            ),
            "bbox_validation_status": bbox_validation_status,
            "area_group_hint": resolved_area_group,
            "sector_hint": resolved_sector,
            "corridor_hint": corridor_hint,
            "route_context": {"route_tokens": route_tokens[:8]},
            "strategy_used": strategy_used,
            "interpretation_status": interpretation_status,
            "fallback_used": bool(fallback_used),
            "fallback_reason": fallback_reason,
            "runtime_bbox_used": runtime_bbox_used,
            "effective_bbox_used": runtime_bbox_used,
            "runtime_spatial_strategy_used": strategy_used,
            "spatial_interpretation_failure_reason": fallback_reason,
            "phase_applicability": list(resolved.get("phase_applicability") or ["phase1"]),
            "supporting_hints": _as_dict(resolved.get("supporting_hints")) or {},
            "advisory_trace": _as_dict(resolved.get("advisory_trace")) or None,
            "geography_priority_enforced": bool(
                resolved.get("geography_priority_enforced")
                if resolved.get("geography_priority_enforced") is not None
                else True
            ),
            "route_hints_present": bool(resolved.get("route_hints_present")),
            "route_hints_influenced_bbox": bool(resolved.get("route_hints_influenced_bbox")),
            "route_hints_used_as_secondary_signal": bool(resolved.get("route_hints_used_as_secondary_signal")),
            "route_hints_overconstrained_geography": bool(resolved.get("route_hints_overconstrained_geography")),
            "route_hint_effect_reason": (
                str(resolved.get("route_hint_effect_reason") or "").strip() or None
            ),
        }
        out["effective_bbox_fingerprint"] = _bbox_hash(_as_dict(runtime_bbox_used))
        out["spatial_plan_signature"] = _spatial_plan_signature(out)
        return out

    def _build_phase3_spatial_interpretation(
        *,
        state: RunSessionState,
        phase3_scope: Dict[str, Any],
        extract_conf: Dict[str, Any],
        fetch_only_path: bool,
    ) -> Dict[str, Any]:
        scope = dict(phase3_scope or {})
        conf = dict(extract_conf or {})
        target_option_text = str(state.pipeline_scope.get("target_entities") or "").strip()
        bbox_input_text = str(scope.get("bbox_input_text") or "").strip()
        place_input = _shared_place_input(state, target_option_text)
        explicit_bbox = _coerce_bbox_candidate(conf.get("bbox"))
        query_strategy = str(conf.get("query_strategy") or "bbox_first_broad").strip() or "bbox_first_broad"
        route_tokens = _normalize_spatial_tokens(
            conf.get("refs"),
            conf.get("name"),
            place_input,
            target_option_text,
        )
        hint_strength = str(conf.get("hint_strength") or "").strip() or "empty"
        route_context = {
            "refs": [str(x).strip() for x in list(conf.get("refs") or []) if str(x).strip()][:12],
            "operator": str(conf.get("operator") or "").strip() or None,
            "name": str(conf.get("name") or "").strip() or None,
            "service_route_id": str(conf.get("service_route_id") or "").strip() or None,
            "direction_id": _handoff_to_int(conf.get("direction_id")),
            "query_strategy": query_strategy,
            "hint_strength": hint_strength,
        }
        resolved = dict(
            shared_geography_resolver.resolve(
                phase="phase3",
                place_input=place_input,
                explicit_bbox=explicit_bbox,
                supporting_hints={
                    "legacy_target_entities": (target_option_text or None),
                    "bbox_input_text": (bbox_input_text or None),
                    "query_strategy": query_strategy,
                    "refs": list(route_context.get("refs") or []),
                    "operator": route_context.get("operator"),
                    "name": route_context.get("name"),
                    "service_route_id": route_context.get("service_route_id"),
                    "direction_id": route_context.get("direction_id"),
                    "route_tokens": list(route_tokens or [])[:12],
                },
                allow_ai_assist=_shared_ai_assist_requested(state),
            )
            or {}
        )
        target_option_received = bool(place_input or explicit_bbox is not None or bbox_input_text)
        original_geographic_input = place_input or bbox_input_text or None
        interpretation_source = str(
            resolved.get("geographic_interpretation_source")
            or resolved.get("interpretation_source")
            or ""
        ).strip()
        if not interpretation_source:
            interpretation_source = "runtime_phase3_scope"
        interpretation_status = str(
            resolved.get("geographic_interpretation_status")
            or resolved.get("interpretation_status")
            or ""
        ).strip()
        bbox_candidate = _coerce_bbox_candidate(resolved.get("bbox_candidate"))
        bbox_validation_status = str(resolved.get("bbox_validation_status") or "").strip() or None
        fallback_reason = str(resolved.get("fallback_reason") or "").strip() or None
        fallback_used = bool(_handoff_to_bool(resolved.get("fallback_used")))
        if fetch_only_path:
            strategy_used = "fetch_only_existing_route_id"
            if bbox_validation_status in {None, ""}:
                bbox_validation_status = "unused_for_fetch_only" if (bbox_candidate or explicit_bbox or bbox_input_text) else None
        elif bbox_candidate is not None:
            if explicit_bbox is not None:
                strategy_used = "explicit_bbox"
            elif interpretation_source == "hades_geography_interpreter":
                strategy_used = "ai_geography_bbox"
            else:
                strategy_used = query_strategy
            if interpretation_status in {"", "unresolved", "ambiguous"}:
                interpretation_status = "ok"
            bbox_validation_status = bbox_validation_status or "valid"
        elif bbox_input_text:
            interpretation_status = "invalid_explicit_bbox"
            fallback_used = True
            fallback_reason = fallback_reason or "invalid_phase3_bbox_input"
            bbox_validation_status = bbox_validation_status or "invalid"
            strategy_used = "default_bbox_fallback"
        elif target_option_received:
            interpretation_status = "fallback_default_bbox"
            fallback_used = True
            fallback_reason = fallback_reason or "phase3_bbox_missing_from_scope"
            bbox_validation_status = bbox_validation_status or "default_applied"
            strategy_used = "default_bbox_fallback"
        else:
            interpretation_status = interpretation_status or "default_bbox_only"
            fallback_used = True
            fallback_reason = fallback_reason or "phase3_scope_bbox_unspecified"
            bbox_validation_status = bbox_validation_status or "default_applied"
            strategy_used = "default_bbox_fallback"

        runtime_bbox_used = None if fetch_only_path else dict(bbox_candidate or _scope_bbox(conf))
        out = {
            "source_text": original_geographic_input,
            "original_geographic_input": original_geographic_input,
            "normalized_geographic_input": resolved.get("normalized_geographic_input"),
            "interpreted_place_meaning": (resolved.get("interpreted_place_meaning") or place_input or None),
            "interpreted_by": interpretation_source,
            "target_option": (place_input or ("explicit_bbox" if explicit_bbox is not None else None)),
            "target_option_received": bool(target_option_received),
            "target_option_text": (place_input or None),
            "geographic_input_type": (
                str(resolved.get("geographic_input_type") or "").strip()
                or (
                    "explicit_bbox"
                    if explicit_bbox is not None
                    else ("place_input" if place_input else ("bbox_input_text" if bbox_input_text else None))
                )
            ),
            "spatial_interpretation_attempted": bool(target_option_received or not fetch_only_path),
            "spatial_interpretation_status": interpretation_status,
            "spatial_interpretation_source": interpretation_source,
            "geographic_interpretation_source": interpretation_source,
            "geographic_interpretation_status": interpretation_status,
            "interpretation_confidence": _handoff_to_float(
                resolved.get("interpretation_confidence")
                if resolved.get("interpretation_confidence") is not None
                else resolved.get("bbox_candidate_confidence")
            ),
            "bbox_candidate": bbox_candidate,
            "bbox_candidate_confidence": _handoff_to_float(
                resolved.get("bbox_candidate_confidence")
                if resolved.get("bbox_candidate_confidence") is not None
                else resolved.get("interpretation_confidence")
            ),
            "bbox_validation_status": bbox_validation_status,
            "area_group_hint": str(resolved.get("area_group_hint") or "").strip().lower() or None,
            "sector_hint": str(resolved.get("sector_hint") or "").strip() or None,
            "corridor_hint": str(resolved.get("corridor_hint") or "").strip() or None,
            "route_context": route_context,
            "strategy_used": strategy_used,
            "interpretation_status": interpretation_status,
            "fallback_used": bool(fallback_used),
            "fallback_reason": fallback_reason,
            "runtime_bbox_used": runtime_bbox_used,
            "effective_bbox_used": runtime_bbox_used,
            "runtime_spatial_strategy_used": strategy_used,
            "spatial_interpretation_failure_reason": fallback_reason,
            "phase_applicability": list(resolved.get("phase_applicability") or ["phase3"]),
            "supporting_hints": _as_dict(resolved.get("supporting_hints")) or {},
            "advisory_trace": _as_dict(resolved.get("advisory_trace")) or None,
            "geography_priority_enforced": bool(
                resolved.get("geography_priority_enforced")
                if resolved.get("geography_priority_enforced") is not None
                else True
            ),
            "route_hints_present": bool(resolved.get("route_hints_present")),
            "route_hints_influenced_bbox": bool(resolved.get("route_hints_influenced_bbox")),
            "route_hints_used_as_secondary_signal": bool(resolved.get("route_hints_used_as_secondary_signal")),
            "route_hints_overconstrained_geography": bool(resolved.get("route_hints_overconstrained_geography")),
            "route_hint_effect_reason": (
                str(resolved.get("route_hint_effect_reason") or "").strip() or None
            ),
        }
        out["effective_bbox_fingerprint"] = _bbox_hash(_as_dict(runtime_bbox_used))
        out["spatial_plan_signature"] = _config_fingerprint(
            {
                "target_option_received": bool(out.get("target_option_received")),
                "target_option_text": out.get("target_option_text"),
                "bbox_candidate_hash": _bbox_hash(_as_dict(out.get("bbox_candidate"))),
                "runtime_bbox_hash": _bbox_hash(_as_dict(out.get("runtime_bbox_used"))),
                "strategy_used": out.get("strategy_used"),
                "interpretation_status": out.get("interpretation_status"),
                "fallback_reason": out.get("fallback_reason"),
                "route_context": out.get("route_context"),
            }
        )[:24]
        return out

    bridge: Dict[str, ExecutorFn] = {
        "phase1_extract": _missing("phase1_extract", "Phase1"),
        "phase1_chain": _missing("phase1_chain", "Phase1"),
        "phase1_workspace_review": _missing("phase1_workspace_review", "Phase1"),
        "phase1_new_nodes_prefill": _missing("phase1_new_nodes_prefill", "Phase1"),
        "phase1_promote_prepare": _missing("phase1_promote_prepare", "Phase1"),
        "phase1_promote_apply": _missing("phase1_promote_apply", "Phase1"),
        "phase2_semantic_pipeline": _missing("phase2_semantic_pipeline", "Phase2"),
        "phase2_semantic_workspace_review": _missing("phase2_semantic_workspace_review", "Phase2"),
        "phase2_cleanup_preview": _missing("phase2_cleanup_preview", "Phase2"),
        "phase2_cleanup_apply": _missing("phase2_cleanup_apply", "Phase2"),
        "phase3_route_extract": _missing("phase3_route_extract", "Phase3"),
        "phase3_step20_sequence": _missing("phase3_step20_sequence", "Phase3"),
        "phase3_reorder_proposal": _missing("phase3_reorder_proposal", "Phase3"),
        "phase3_reorder_apply": _missing("phase3_reorder_apply", "Phase3"),
        "phase3_step30_geometry": _missing("phase3_step30_geometry", "Phase3"),
        "phase3_step32_stop_recovery": _missing("phase3_step32_stop_recovery", "Phase3"),
        "phase3_step35_rank": _missing("phase3_step35_rank", "Phase3"),
        "phase3_step40_approve_prepare": _missing("phase3_step40_approve_prepare", "Phase3"),
        "phase3_step40_approve_apply": _missing("phase3_step40_approve_apply", "Phase3"),
        "phase3_merge_proposal": _missing("phase3_merge_proposal", "Phase3"),
        "phase3_merge_apply": _missing("phase3_merge_apply", "Phase3"),
        "phase3_catalog_sync_review": _missing("phase3_catalog_sync_review", "Phase3"),
        "phase3_sector_coverage_review": _missing("phase3_sector_coverage_review", "Phase3"),
        "phase3_gap_detection_classification": _missing("phase3_gap_detection_classification", "Phase3"),
        "phase3_missing_route_export": _missing("phase3_missing_route_export", "Phase3"),
        "phase3_gap_resolution_queue": _missing("phase3_gap_resolution_queue", "Phase3"),
        "phase3_gap_resolution_verify": _missing("phase3_gap_resolution_verify", "Phase3"),
    }

    if phase1_client is not None:
        def _p1_extract(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step
            phase_scope = dict(state.pipeline_scope.get("phase1") or {})
            base_conf = dict(phase_scope.get("extract") or {})
            base_conf.setdefault(
                "actions_path",
                str(Path(__file__).resolve().parents[2] / "phase1_nodes" / "datamind" / "services" / "openmaps_extractor" / "actions.json"),
            )
            spatial_interpretation = _build_phase1_spatial_interpretation(
                state=state,
                extract_conf=base_conf,
            )
            if _coerce_bbox_candidate(spatial_interpretation.get("bbox_candidate")) is not None:
                base_conf["bbox"] = dict(_coerce_bbox_candidate(spatial_interpretation.get("bbox_candidate")) or {})
            else:
                base_conf.setdefault("bbox", _scope_bbox(base_conf))
            if str(spatial_interpretation.get("area_group_hint") or "").strip() and not str(base_conf.get("area_group_hint") or "").strip():
                base_conf["area_group_hint"] = spatial_interpretation.get("area_group_hint")
            if str(spatial_interpretation.get("sector_hint") or "").strip() and not str(base_conf.get("sector_hint") or "").strip():
                base_conf["sector_hint"] = spatial_interpretation.get("sector_hint")
            route_tokens = list(_as_dict(spatial_interpretation.get("route_context")).get("route_tokens") or [])
            if route_tokens and not list(base_conf.get("route_tokens") or []):
                base_conf["route_tokens"] = route_tokens
            base_conf.pop("area", None)
            base_conf.setdefault("candidate_actions", list(base_conf.get("candidate_actions") or []))

            retry_applied = _phase1_extract_retry_apply(base_conf=base_conf, retry_params=dict(params or {}))
            conf = dict(retry_applied.get("effective_conf") or {})
            conf.pop("area", None)
            spatial_interpretation = dict(spatial_interpretation or {})
            spatial_interpretation["runtime_bbox_used"] = dict(_scope_bbox(conf))
            spatial_interpretation["runtime_spatial_strategy_used"] = (
                str(spatial_interpretation.get("strategy_used") or "").strip()
                or "default_bbox_fallback"
            )
            spatial_sig = str(spatial_interpretation.get("spatial_plan_signature") or "").strip()
            spatial_state_cache = _as_dict(state.resume_context.get("phase1_spatial_interpretation_state"))
            prev_spatial = _as_dict(spatial_state_cache.get(STEP_P1_1_EXTRACT))
            prev_sig = str(prev_spatial.get("signature") or "").strip()
            retry_changed_spatial_plan = True
            same_spatial_plan_retry_count = 0
            if int(attempt_no or 0) > 1 and prev_sig:
                retry_changed_spatial_plan = bool(prev_sig != spatial_sig)
                same_spatial_plan_retry_count = int(prev_spatial.get("same_spatial_plan_retry_count") or 0)
                if not retry_changed_spatial_plan:
                    same_spatial_plan_retry_count += 1
            spatial_interpretation["retry_changed_spatial_plan"] = bool(retry_changed_spatial_plan)
            spatial_interpretation["same_spatial_plan_retry_count"] = int(same_spatial_plan_retry_count)
            spatial_state_cache[str(STEP_P1_1_EXTRACT)] = {
                "signature": spatial_sig,
                "same_spatial_plan_retry_count": int(same_spatial_plan_retry_count),
            }
            state.resume_context["phase1_spatial_interpretation_state"] = spatial_state_cache
            spatial_warning_codes: List[str] = []
            if bool(spatial_interpretation.get("target_option_received")) and str(
                spatial_interpretation.get("spatial_interpretation_status") or ""
            ).strip() in {"failed", "fallback_default_bbox", "invalid_explicit_bbox"}:
                spatial_warning_codes.append("spatial_interpretation_failed")
            if bool(spatial_interpretation.get("target_option_received")) and str(
                spatial_interpretation.get("runtime_spatial_strategy_used") or ""
            ).strip() == "default_bbox_fallback":
                spatial_warning_codes.append("target_intent_ignored")
            if bool(spatial_interpretation.get("target_option_received")) and retry_changed_spatial_plan is False:
                spatial_warning_codes.append("spatial_plan_reused_without_change")

            dropped_runtime_conf_keys: List[str] = []
            try:
                tuned_sig = inspect.signature(phase1_client.run_step_build_node_set_tuned)
                has_var_kwargs = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in list(tuned_sig.parameters.values())
                )
                if not has_var_kwargs:
                    allowed_conf_keys = set(tuned_sig.parameters.keys())
                    for key in list(conf.keys()):
                        if key not in allowed_conf_keys:
                            dropped_runtime_conf_keys.append(str(key))
                            conf.pop(key, None)
            except Exception:
                # Keep backward compatibility when runtime signature inspection fails.
                pass

            if dropped_runtime_conf_keys:
                existing = list(retry_applied.get("retry_parameter_warnings") or [])
                existing.extend(
                    [f"unsupported_runtime_conf_key:{k}" for k in list(dict.fromkeys(dropped_runtime_conf_keys))]
                )
                retry_applied["retry_parameter_warnings"] = list(dict.fromkeys(existing))
            if spatial_warning_codes:
                existing = list(retry_applied.get("retry_parameter_warnings") or [])
                existing.extend(list(spatial_warning_codes))
                retry_applied["retry_parameter_warnings"] = list(dict.fromkeys(existing))
            out = dict(phase1_client.run_step_build_node_set_tuned(**conf) or {})
            best = dict(out.get("best") or {})
            node_set_id = str(best.get("node_set_id") or conf.get("node_set_id") or "").strip()
            best_metrics = dict(best.get("metrics") or {})
            candidate_count = _handoff_to_int(best_metrics.get("candidate_count"))
            quality_score = _handoff_to_float(best.get("quality_score"))

            raw_attempt_rows = [dict(r or {}) for r in list(out.get("attempts") or [])]
            attempt_records: List[Dict[str, Any]] = []
            for idx, row in enumerate(raw_attempt_rows, start=1):
                metrics = dict(row.get("metrics") or {})
                bbox = _as_dict(row.get("bbox"))
                attempt_candidate_count = _handoff_to_int(
                    row.get("candidate_count")
                    if row.get("candidate_count") is not None
                    else metrics.get("candidate_count")
                )
                row_error = str(row.get("error") or metrics.get("error_summary") or "").strip()
                status = "success" if row.get("node_set_id") else "error"
                if status == "success" and attempt_candidate_count is not None and attempt_candidate_count <= 0:
                    status = "empty"
                if "timeout" in str(row.get("error_code") or row_error).lower():
                    status = "timeout"
                attempt_fp_source = {
                    "bbox": bbox,
                    "action_id": row.get("action_id"),
                    "retry_parameter_delta": dict(row.get("retry_parameter_delta") or {}),
                }
                attempt_records.append(
                    _attempt_record(
                        phase="phase1",
                        step_id=STEP_P1_1_EXTRACT,
                        attempt_no=idx,
                        run_id=state.run_id,
                        trace_id=state.trace_id,
                        area_group=(row.get("area_group") or out.get("area_group")),
                        sector=(row.get("sector") or out.get("sector")),
                        bbox_used=bbox,
                        bbox_hash=_bbox_hash(bbox),
                        bbox_fingerprint=_bbox_hash(bbox),
                        retry_strategy=(row.get("retry_strategy") or retry_applied.get("retry_strategy")),
                        retry_reason=(row.get("retry_reason") or retry_applied.get("retry_reason")),
                        retry_parameter_delta=dict(row.get("retry_parameter_delta") or retry_applied.get("retry_parameter_delta") or {}),
                        action_or_template_used=row.get("action_id"),
                        fallback_config_used=(retry_applied.get("retry_parameter_applied") or {}).get("fallback_config"),
                        status=status,
                        duration_ms=_handoff_to_int(metrics.get("runtime_ms") if metrics.get("runtime_ms") is not None else metrics.get("duration_ms")),
                        timeout_flag=("timeout" in str(row.get("error_code") or row_error).lower()) if row_error else None,
                        response_status=(metrics.get("http_status") if metrics.get("http_status") is not None else metrics.get("response_status")),
                        response_size_bytes=_handoff_to_int(
                            metrics.get("response_size_bytes")
                            if metrics.get("response_size_bytes") is not None
                            else metrics.get("bytes")
                        ),
                        raw_elements_count=_handoff_to_int(
                            metrics.get("raw_count")
                            if metrics.get("raw_count") is not None
                            else metrics.get("raw_elements_count")
                        ),
                        candidate_count=attempt_candidate_count,
                        error_code=(str(row.get("error_code") or "").strip() or None),
                        error_summary=(row_error or None),
                        extractor_diagnostics_summary={
                            "quality_score": _handoff_to_float(row.get("quality_score")),
                            "http_status": metrics.get("http_status"),
                        },
                        effective_config_fingerprint=_config_fingerprint(attempt_fp_source)[:24],
                        spatial_plan_signature=spatial_sig,
                        retry_changed_spatial_plan=(bool(retry_changed_spatial_plan) if idx == 1 else False),
                        spatial_interpretation_status=spatial_interpretation.get("spatial_interpretation_status"),
                        runtime_spatial_strategy_used=spatial_interpretation.get("runtime_spatial_strategy_used"),
                        spatial_interpretation_failure_reason=spatial_interpretation.get("spatial_interpretation_failure_reason"),
                        target_option_received=_handoff_to_bool(spatial_interpretation.get("target_option_received")),
                        notes=list(retry_applied.get("notes") or []),
                    )
                )

            if not attempt_records:
                attempt_fp_source = {
                    "bbox": _as_dict(conf.get("bbox")),
                    "candidate_actions": list(conf.get("candidate_actions") or []),
                    "retry_parameter_delta": dict(retry_applied.get("retry_parameter_delta") or {}),
                }
                fallback_status = "success" if bool(out.get("ok", True)) else "error"
                if fallback_status == "success" and (candidate_count or 0) <= 0:
                    fallback_status = "empty"
                attempt_records.append(
                    _attempt_record(
                        phase="phase1",
                        step_id=STEP_P1_1_EXTRACT,
                        attempt_no=int(attempt_no),
                        run_id=state.run_id,
                        trace_id=state.trace_id,
                        area_group=out.get("area_group"),
                        sector=out.get("sector"),
                        bbox_used=_as_dict(conf.get("bbox")),
                        bbox_hash=_bbox_hash(_as_dict(conf.get("bbox"))),
                        bbox_fingerprint=_bbox_hash(_as_dict(conf.get("bbox"))),
                        retry_strategy=retry_applied.get("retry_strategy"),
                        retry_reason=retry_applied.get("retry_reason"),
                        retry_parameter_delta=dict(retry_applied.get("retry_parameter_delta") or {}),
                        action_or_template_used=best.get("action_id"),
                        fallback_config_used=(retry_applied.get("retry_parameter_applied") or {}).get("fallback_config"),
                        status=fallback_status,
                        duration_ms=_handoff_to_int(
                            best_metrics.get("runtime_ms")
                            if best_metrics.get("runtime_ms") is not None
                            else best_metrics.get("duration_ms")
                        ),
                        timeout_flag=None,
                        response_status=(
                            best_metrics.get("http_status")
                            if best_metrics.get("http_status") is not None
                            else best_metrics.get("response_status")
                        ),
                        response_size_bytes=_handoff_to_int(
                            best_metrics.get("response_size_bytes")
                            if best_metrics.get("response_size_bytes") is not None
                            else best_metrics.get("bytes")
                        ),
                        raw_elements_count=_handoff_to_int(
                            best_metrics.get("raw_count")
                            if best_metrics.get("raw_count") is not None
                            else best_metrics.get("raw_elements_count")
                        ),
                        candidate_count=candidate_count,
                        error_code=(str(out.get("error_code") or "") or None),
                        error_summary=(str(out.get("reason") or "") or None),
                        extractor_diagnostics_summary={
                            "quality_score": _handoff_to_float(best.get("quality_score")),
                            "http_status": best_metrics.get("http_status"),
                        },
                        effective_config_fingerprint=_config_fingerprint(attempt_fp_source)[:24],
                        spatial_plan_signature=spatial_sig,
                        retry_changed_spatial_plan=bool(retry_changed_spatial_plan),
                        spatial_interpretation_status=spatial_interpretation.get("spatial_interpretation_status"),
                        runtime_spatial_strategy_used=spatial_interpretation.get("runtime_spatial_strategy_used"),
                        spatial_interpretation_failure_reason=spatial_interpretation.get("spatial_interpretation_failure_reason"),
                        target_option_received=_handoff_to_bool(spatial_interpretation.get("target_option_received")),
                        notes=list(retry_applied.get("notes") or []),
                    )
                )

            attempt_records = _mark_attempt_change(attempt_records)
            attempt_summary = _summarize_attempt_records(attempt_records)
            retry_diversified = bool(attempt_summary.get("diversified_attempts"))
            extraction_outcome = "success"
            if not bool(out.get("ok", True)):
                extraction_outcome = "error"
            elif (candidate_count or 0) <= 0:
                extraction_outcome = "empty"
            return ExecutorResult(
                ok=bool(out.get("ok", True)),
                summary={
                    "node_set_id": node_set_id,
                    "quality_score": quality_score,
                    "candidate_count": candidate_count,
                    "effective_config_fingerprint": str(retry_applied.get("effective_config_fingerprint") or ""),
                    "retry_strategy": retry_applied.get("retry_strategy"),
                    "retry_reason": retry_applied.get("retry_reason"),
                    "retry_parameter_delta": dict(retry_applied.get("retry_parameter_delta") or {}),
                    "retry_parameter_applied": dict(retry_applied.get("retry_parameter_applied") or {}),
                    "retry_parameter_warnings": list(retry_applied.get("retry_parameter_warnings") or []),
                    "retry_diversified": retry_diversified,
                    "bbox": _as_dict(spatial_interpretation.get("runtime_bbox_used")),
                    "spatial_interpretation": dict(spatial_interpretation or {}),
                    "extraction_attempt_records": attempt_records,
                    "extraction_attempts_summary": attempt_summary,
                    "extraction_attempt_history_summary": attempt_summary,
                    "attempt_record_schema_version": "extractor_attempt_v1",
                    "validator_payload": {
                        "candidate_count": candidate_count,
                        "quality_score": quality_score,
                        "attempt_no": int(attempt_no),
                        "extractor_attempts_summary": attempt_summary,
                        "extractor_attempt_history_summary": attempt_summary,
                        "retry_diversified": retry_diversified,
                        "spatial_interpretation": dict(spatial_interpretation or {}),
                        "target_option_received": _handoff_to_bool(spatial_interpretation.get("target_option_received")),
                        "target_option_text": spatial_interpretation.get("target_option_text"),
                        "spatial_interpretation_attempted": _handoff_to_bool(spatial_interpretation.get("spatial_interpretation_attempted")),
                        "spatial_interpretation_status": spatial_interpretation.get("spatial_interpretation_status"),
                        "spatial_interpretation_source": spatial_interpretation.get("spatial_interpretation_source"),
                        "bbox_candidate": _as_dict(spatial_interpretation.get("bbox_candidate")) or None,
                        "bbox_candidate_confidence": _handoff_to_float(spatial_interpretation.get("bbox_candidate_confidence")),
                        "area_group_hint": spatial_interpretation.get("area_group_hint"),
                        "sector_hint": spatial_interpretation.get("sector_hint"),
                        "corridor_hint": spatial_interpretation.get("corridor_hint"),
                        "runtime_bbox_used": _as_dict(spatial_interpretation.get("runtime_bbox_used")) or None,
                        "runtime_spatial_strategy_used": spatial_interpretation.get("runtime_spatial_strategy_used"),
                        "retry_changed_spatial_plan": _handoff_to_bool(spatial_interpretation.get("retry_changed_spatial_plan")),
                        "same_spatial_plan_retry_count": _handoff_to_int(spatial_interpretation.get("same_spatial_plan_retry_count")),
                        "spatial_interpretation_failure_reason": spatial_interpretation.get("spatial_interpretation_failure_reason"),
                        "recommended_action": (
                            "pause_and_patch_extractor_spatial_interpretation"
                            if (
                                extraction_outcome in {"empty", "error"}
                                and bool(spatial_interpretation.get("target_option_received"))
                                and int(attempt_no or 0) >= 2
                                and _handoff_to_bool(spatial_interpretation.get("retry_changed_spatial_plan")) is False
                                and int(same_spatial_plan_retry_count or 0) >= 1
                            )
                            else (
                                "inspect_spatial_interpretation_before_retry"
                                if (
                                    extraction_outcome in {"empty", "error"}
                                    and bool(spatial_interpretation.get("target_option_received"))
                                    and str(spatial_interpretation.get("spatial_interpretation_status") or "").strip() in {"failed", "fallback_default_bbox", "invalid_explicit_bbox"}
                                )
                                else (
                                    "retry_with_bbox_or_template"
                                    if extraction_outcome in {"empty", "error"}
                                    else "continue_or_review"
                                )
                            )
                        ),
                        "extraction_outcome_classification": extraction_outcome,
                    },
                    "raw": out,
                },
                artifacts=[
                    {"artifact_type": "phase1_extract", "payload": out},
                    {"artifact_type": "extraction_attempt_history_summary", "payload": attempt_summary},
                    {"artifact_type": "phase1_spatial_interpretation", "payload": dict(spatial_interpretation or {})},
                ],
            )

        def _p1_chain(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no, params
            node_set_id = _node_set_id(state)
            if not node_set_id:
                return ExecutorResult(ok=False, summary={"error": "node_set_id_required"}, artifacts=[])
            out_norm = dict(phase1_client.run_step_normalize(node_set_id) or {})
            out_feat = dict(phase1_client.run_step_features(node_set_id) or {})
            out_cluster = dict(phase1_client.run_step_cluster(node_set_id) or {})
            out_resolve = dict(phase1_client.run_step_resolve(node_set_id) or {})
            resolved_count = int(
                out_resolve.get("resolved_count")
                or out_resolve.get("resolved_total")
                or out_resolve.get("n_resolved")
                or 0
            )
            summary = {
                "node_set_id": node_set_id,
                "normalize": out_norm,
                "features": out_feat,
                "cluster": out_cluster,
                "resolve": out_resolve,
                "validator_payload": {
                    "normalize_ok": bool(out_norm.get("ok", True)),
                    "features_ok": bool(out_feat.get("ok", True)),
                    "cluster_ok": bool(out_cluster.get("ok", True)),
                    "cluster_degenerate": bool(out_cluster.get("degenerate", False)),
                    "resolve_ok": bool(out_resolve.get("ok", True)),
                    "resolved_count": resolved_count,
                    "resolved_total": resolved_count,
                },
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[{"artifact_type": "phase1_chain", "payload": summary}],
            )

        def _p1_workspace_review(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no, params
            counts = dict(phase1_client.get_node_review_request_counts(source="phase3_route") or {})
            pending = list(phase1_client.list_node_review_requests(source="phase3_route", status="requested", limit=300) or [])
            summary = {
                "pending_request_count": int(counts.get("requested") or 0),
                "review_groups": {
                    "high_priority": [str(r.get("request_id")) for r in pending[:25]],
                    "queue_size": len(pending),
                },
                "validator_payload": {"manual_review_required": True, "pending_request_count": int(counts.get("requested") or 0)},
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[{"artifact_type": "phase1_workspace_review", "payload": summary}],
            )

        def _p1_new_nodes_prefill(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no, params
            pending = list(phase1_client.list_node_review_requests(source="phase3_route", status="requested", limit=400) or [])
            create_suggestions: List[Dict[str, Any]] = []
            ambiguity_suggestions: List[Dict[str, Any]] = []
            for req in pending:
                lat = req.get("lat")
                lon = req.get("lon")
                if lat is None or lon is None:
                    continue
                near = list(
                    phase1_client.list_stop_candidates_near_point(
                        lat=float(lat),
                        lon=float(lon),
                        radius_m=float((state.pipeline_scope.get("phase3") or {}).get("match_radius_m") or 5.0),
                        limit=8,
                    )
                    or []
                )
                if len(near) <= 0:
                    create_suggestions.append(
                        {
                            "request_id": str(req.get("request_id") or ""),
                            "route_id": req.get("route_id"),
                            "seq": req.get("seq"),
                            "lat": lat,
                            "lon": lon,
                            "suggested_name": str(req.get("name") or "Stop").strip() or "Stop",
                            "evidence": {"candidate_count": 0},
                        }
                    )
                else:
                    ambiguity_suggestions.append(
                        {
                            "request_id": str(req.get("request_id") or ""),
                            "route_id": req.get("route_id"),
                            "seq": req.get("seq"),
                            "candidates": near[:3],
                            "ambiguity_score": min(1.0, 0.2 * len(near)),
                        }
                    )

            summary = {
                "create_node_suggestions": create_suggestions,
                "ambiguity_candidates": ambiguity_suggestions,
                "prefilled_approval_payload": {
                    "source": "phase3_route_blocker",
                    "pending_request_ids": [str(r.get("request_id") or "") for r in pending],
                },
                "validator_payload": {
                    "pending_requests": len(pending),
                    "create_suggestions": len(create_suggestions),
                    "ambiguity_suggestions": len(ambiguity_suggestions),
                    "manual_review_required": True,
                },
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[{"artifact_type": "phase1_new_nodes_prefill", "payload": summary}],
            )

        def _p1_promote_prepare(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no, params
            node_set_id = _node_set_id(state)
            if not node_set_id:
                return ExecutorResult(ok=False, summary={"error": "node_set_id_required_for_promote_prepare"}, artifacts=[])
            dry_run: Dict[str, Any]
            if hasattr(phase1_client, "get_promote_dry_run"):
                dry_run = dict(phase1_client.get_promote_dry_run(node_set_id) or {})
            else:
                summary_row = dict(phase1_client.get_node_set_summary(node_set_id) or {})
                dry_run = {
                    "node_set_id": node_set_id,
                    "n_resolved": int(summary_row.get("n_resolved") or 0),
                    "n_approved": int(summary_row.get("n_approved") or 0),
                    "n_rejected": int(summary_row.get("n_rejected") or 0),
                    "status": summary_row.get("status"),
                }
            workspace_state_summary = dict(dry_run.get("workspace_state_summary") or {})
            if not workspace_state_summary:
                _resolved = int(dry_run.get("n_resolved") or 0)
                _approved = int(dry_run.get("n_approved") or 0)
                _rejected = int(dry_run.get("n_rejected") or 0)
                workspace_state_summary = {
                    "node_set_id": node_set_id,
                    "resolved_total": _resolved,
                    "approved_count": _approved,
                    "rejected_count": _rejected,
                    "pending_review_count": max(0, _resolved - _approved - _rejected),
                    "promote_eligible_count": _approved,
                }
            dry_run["workspace_state_summary"] = workspace_state_summary
            ready_for_approval = bool(
                str(dry_run.get("status") or "").strip() == "ok"
                and int(dry_run.get("n_resolved") or 0) > 0
            )
            out = {
                "promote_dry_run": dry_run,
                "workspace_state_summary": workspace_state_summary,
                "validator_payload": {
                    "ready_for_approval": ready_for_approval,
                    "n_resolved": int(dry_run.get("n_resolved") or 0),
                    "promote_status": str(dry_run.get("status") or ""),
                    "legacy_status": dry_run.get("legacy_status"),
                    "promote_precondition": dry_run.get("promote_precondition"),
                    "workspace_state_summary": workspace_state_summary,
                    "recommended_action": dry_run.get("recommended_action"),
                    "diagnostics_hint": dry_run.get("diagnostics_hint"),
                },
            }
            return ExecutorResult(
                ok=True,
                summary=out,
                artifacts=[{"artifact_type": "phase1_promote_prepare", "payload": out}],
            )

        def _p1_promote_apply(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no, params
            node_set_id = _node_set_id(state)
            if not node_set_id:
                return ExecutorResult(ok=False, summary={"error": "node_set_id_required_for_promote"}, artifacts=[])
            out = dict(phase1_client.run_step_promote(node_set_id) or {})
            promote_prepare = _latest_artifact_payload(state, "phase1_promote_prepare")
            dry_run = _as_dict(promote_prepare.get("promote_dry_run"))
            workspace_state_summary = _as_dict(
                promote_prepare.get("workspace_state_summary")
                or dry_run.get("workspace_state_summary")
            )
            handoff, handoff_artifact = _handoff_artifact(
                state,
                source_node_set_id=node_set_id,
                phase1_promote_completed=True,
                promote_completed_at=_utc_now_iso(),
                place_set_id=None,
                place_set_id_source=None,
                resolved_count=(
                    _handoff_to_int(workspace_state_summary.get("resolved_total"))
                    or _handoff_to_int(dry_run.get("n_resolved"))
                ),
                approved_count=(
                    _handoff_to_int(workspace_state_summary.get("approved_count"))
                    or _handoff_to_int(dry_run.get("n_approved"))
                ),
                promoted_count=(
                    _handoff_to_int(out.get("promoted"))
                    or _handoff_to_int(workspace_state_summary.get("promote_eligible_count"))
                ),
                promote_status="promoted_to_node_prod",
                ready_for_phase2=True,
                ready_for_step25=False,
            )
            return ExecutorResult(
                ok=True,
                summary={
                    "node_set_id": node_set_id,
                    "promote": out,
                    PHASE1_TO_PHASE2_HANDOFF_ARTIFACT: handoff,
                },
                artifacts=[
                    {"artifact_type": "phase1_promote", "payload": out},
                    handoff_artifact,
                ],
            )

        bridge.update(
            {
                "phase1_extract": _p1_extract,
                "phase1_chain": _p1_chain,
                "phase1_workspace_review": _p1_workspace_review,
                "phase1_new_nodes_prefill": _p1_new_nodes_prefill,
                "phase1_promote_prepare": _p1_promote_prepare,
                "phase1_promote_apply": _p1_promote_apply,
            }
        )

    if phase2_client is not None:
        def _p2_semantic(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            source_node_set_id = _node_set_id(state) or None
            runtime_context_key = (
                str(params.get("phase2_context_key") or "").strip()
                or f"autopilot_phase2:{state.run_id}:{source_node_set_id or 'global'}"
            )
            runtime_context: Dict[str, Any] = {
                "context_key": runtime_context_key,
                "source_node_set_id": source_node_set_id,
                "place_set_id": str(params.get("place_set_id") or "").strip() or None,
            }
            substep_timings_ms: Dict[str, int] = {}
            skipped_substeps: Dict[str, Any] = {}

            def _run_substep(stage_name: str, fn: Callable[..., Any], /, **kwargs: Any) -> Dict[str, Any]:
                t0 = perf_counter()
                out = dict(fn(**kwargs) or {})
                substep_timings_ms[stage_name] = int(round((perf_counter() - t0) * 1000.0))
                if out.get("skipped"):
                    skipped_substeps[stage_name] = {
                        "skip_reason": str(out.get("skip_reason") or "").strip() or None,
                    }
                return out

            handoff, precondition_result = _validate_phase2_start_preconditions(
                state,
                runtime_context=runtime_context,
            )
            if precondition_result is not None:
                return precondition_result
            handoff_artifact = {
                "artifact_type": PHASE1_TO_PHASE2_HANDOFF_ARTIFACT,
                "payload": dict(handoff or {}),
            }

            try:
                o10 = _run_substep(
                    "extract",
                    phase2_client.run_step_10_extract,
                    context_key=runtime_context_key,
                    source_node_set_id=source_node_set_id,
                )
                o15 = _run_substep(
                    "geo_context",
                    phase2_client.run_step_15_build_geo_context,
                    context_key=runtime_context_key,
                )
                o20 = _run_substep(
                    "candidates",
                    phase2_client.run_step_20_build_candidates,
                    context_key=runtime_context_key,
                )
                latest_place_set_fn = getattr(phase2_client, "get_latest_place_set_id", None)
                latest_place_set_id = (
                    latest_place_set_fn(context_key=runtime_context_key)
                    if callable(latest_place_set_fn)
                    else None
                )
                step20_summary = _as_dict(o20.get("summary"))
                place_set_id = (
                    str(params.get("place_set_id") or "").strip()
                    or str(step20_summary.get("place_set_id") or "").strip()
                    or str(handoff.get("place_set_id") or "").strip()
                    or str(latest_place_set_id or "").strip()
                )
                place_set_id_source = (
                    ("runtime_param" if str(params.get("place_set_id") or "").strip() else None)
                    or str(step20_summary.get("place_set_id_source") or "").strip()
                    or ("handoff_artifact" if str(handoff.get("place_set_id") or "").strip() else None)
                    or ("context_lookup" if str(latest_place_set_id or "").strip() else None)
                )
                handoff, handoff_artifact = _handoff_artifact(
                    state,
                    source_node_set_id=source_node_set_id,
                    phase2_context_key=runtime_context_key,
                    place_set_id=(place_set_id or None),
                    place_set_id_source=(place_set_id_source or None),
                    ready_for_phase2=True,
                )
                runtime_context["place_set_id"] = place_set_id or None
                handoff, precondition_result = _validate_phase2_place_set_preconditions(
                    state,
                    runtime_context=runtime_context,
                    place_set_id=place_set_id,
                    place_set_id_source=place_set_id_source,
                )
                handoff_artifact = {
                    "artifact_type": PHASE1_TO_PHASE2_HANDOFF_ARTIFACT,
                    "payload": dict(handoff or {}),
                }
                if precondition_result is not None:
                    return precondition_result
                o25 = _run_substep(
                    "name_candidates",
                    phase2_client.run_step_25_build_name_candidates,
                    place_set_id=(place_set_id or None),
                    context_key=runtime_context_key,
                )
                o35 = _run_substep(
                    "train_ranker",
                    phase2_client.run_step_35_train_name_ranker,
                    force=bool(params.get("force_model_refresh", False)),
                )
                o40 = _run_substep(
                    "embeddings",
                    phase2_client.run_step_40_build_embeddings,
                    force=bool(params.get("force_embeddings_refresh", False)),
                )
                o50 = _run_substep(
                    "reindex",
                    phase2_client.run_step_50_reindex_opensearch,
                    force=bool(params.get("force_reindex_refresh", False)),
                    embeddings_step_result=o40,
                )
            except Exception as exc:
                failed_stage = next((name for name in ("extract", "geo_context", "candidates", "name_candidates", "train_ranker", "embeddings", "reindex") if name not in substep_timings_ms), "semantic_pipeline")
                return ExecutorResult(
                    ok=False,
                    summary={
                        "error": str(exc),
                        "failed_stage": failed_stage,
                        "runtime_context": dict(runtime_context),
                        "substep_timings_ms": dict(substep_timings_ms),
                        "skipped_substeps": dict(skipped_substeps),
                        PHASE1_TO_PHASE2_HANDOFF_ARTIFACT: dict(handoff or {}),
                    },
                    artifacts=[handoff_artifact],
                )

            summary = {
                "extract": o10,
                "geo_context": o15,
                "candidates": o20,
                "name_candidates": o25,
                "train_ranker": o35,
                "embeddings": o40,
                "reindex": o50,
                "runtime_context": dict(runtime_context),
                PHASE1_TO_PHASE2_HANDOFF_ARTIFACT: dict(handoff or {}),
                "substep_timings_ms": dict(substep_timings_ms),
                "skipped_substeps": dict(skipped_substeps),
                "validator_payload": {
                    "failed_stage": None,
                    "critical_db_inconsistency": False,
                    "runtime_context": dict(runtime_context),
                    "phase1_to_phase2_handoff": dict(handoff or {}),
                    "substep_timings_ms": dict(substep_timings_ms),
                    "skipped_substeps": dict(skipped_substeps),
                },
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[
                    {"artifact_type": "phase2_semantic", "payload": summary},
                    handoff_artifact,
                ],
            )

        def _p2_workspace_review(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del state, step, attempt_no, params
            training = dict(phase2_client.get_step35_training_summary() or {})
            quality = dict(phase2_client.get_step35_quality_rows() or {})
            summary = {
                "training_summary": training,
                "quality_rows_count": {
                    "name_rows": len(quality.get("name_rows") or []),
                    "type_rows": len(quality.get("type_rows") or []),
                },
                "validator_payload": {"manual_review_required": True},
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[{"artifact_type": "phase2_workspace_review", "payload": summary}],
            )

        def _p2_cleanup_preview(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del state, step, attempt_no
            out = dict(
                phase2_client.normalize_prod_nodes_global(
                    dedup_radius_m=float(params.get("dedup_radius_m") or 2.0),
                    delete_bad_named_nodes=bool(params.get("delete_bad_named_nodes", True)),
                    normalize_names=bool(params.get("normalize_names", True)),
                    only_stop_nodes=bool(params.get("only_stop_nodes", True)),
                    dry_run=True,
                )
                or {}
            )
            summary = {
                "cleanup_impact_analysis": out,
                "validator_payload": {"preview_ok": True, "delete_candidates": int(out.get("final_delete_candidates") or 0)},
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[{"artifact_type": "phase2_cleanup_preview", "payload": out}],
            )

        def _p2_cleanup_apply(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del state, step, attempt_no
            out = dict(
                phase2_client.normalize_prod_nodes_global(
                    dedup_radius_m=float(params.get("dedup_radius_m") or 2.0),
                    delete_bad_named_nodes=bool(params.get("delete_bad_named_nodes", True)),
                    normalize_names=bool(params.get("normalize_names", True)),
                    only_stop_nodes=bool(params.get("only_stop_nodes", True)),
                    dry_run=False,
                )
                or {}
            )
            return ExecutorResult(
                ok=True,
                summary={"cleanup_apply": out},
                artifacts=[{"artifact_type": "phase2_cleanup_apply", "payload": out}],
            )

        bridge.update(
            {
                "phase2_semantic_pipeline": _p2_semantic,
                "phase2_semantic_workspace_review": _p2_workspace_review,
                "phase2_cleanup_preview": _p2_cleanup_preview,
                "phase2_cleanup_apply": _p2_cleanup_apply,
            }
        )

    if phase3_client is not None:
        def _p3_route_extract(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step
            p3_scope = dict(state.pipeline_scope.get("phase3") or {})
            route_id = _route_id(state)
            artifacts: List[Dict[str, Any]] = []
            route_extract_conf: Dict[str, Any] = {
                "bbox": _coerce_bbox_candidate(p3_scope.get("bbox")),
                "refs": list(p3_scope.get("refs") or []),
                "operator": p3_scope.get("operator"),
                "name": p3_scope.get("name"),
                "max_candidates": _handoff_to_int(p3_scope.get("max_candidates")) or 50,
                "timeout_s": _handoff_to_int(p3_scope.get("timeout_s")) or 180,
                "query_strategy": str(p3_scope.get("query_strategy") or "bbox_first_broad").strip() or "bbox_first_broad",
                "service_route_id": p3_scope.get("service_route_id"),
                "direction_id": p3_scope.get("direction_id"),
                "discover_variants": _as_dict(p3_scope.get("discover_variants")),
                "fallback_profiles": _as_dict(p3_scope.get("fallback_profiles")),
            }
            spatial_interpretation = _build_phase3_spatial_interpretation(
                state=state,
                phase3_scope=p3_scope,
                extract_conf=route_extract_conf,
                fetch_only_path=bool(route_id),
            )
            route_context_fp = _route_context_fingerprint(route_extract_conf)
            retry_applied = _phase3_extract_retry_apply(
                base_conf=route_extract_conf,
                retry_params=dict(params or {}),
                fetch_only_path=bool(route_id),
            )
            route_extract_conf = dict(retry_applied.get("effective_conf") or route_extract_conf)
            if _coerce_bbox_candidate(spatial_interpretation.get("bbox_candidate")) is not None:
                route_extract_conf["bbox"] = dict(_coerce_bbox_candidate(spatial_interpretation.get("bbox_candidate")) or {})
            elif route_id:
                route_extract_conf.pop("bbox", None)
            else:
                route_extract_conf["bbox"] = dict(_scope_bbox(route_extract_conf))
            spatial_interpretation = dict(spatial_interpretation or {})
            spatial_interpretation["runtime_bbox_used"] = (
                None if route_id else dict(_scope_bbox(route_extract_conf))
            )
            spatial_interpretation["runtime_spatial_strategy_used"] = (
                "fetch_only_existing_route_id"
                if route_id
                else (
                    str(route_extract_conf.get("query_strategy") or spatial_interpretation.get("strategy_used") or "").strip()
                    or "bbox_first_broad"
                )
            )
            spatial_interpretation["effective_bbox_fingerprint"] = _bbox_hash(
                _as_dict(spatial_interpretation.get("runtime_bbox_used"))
            )
            spatial_sig = _config_fingerprint(
                {
                    "bbox_hash": _bbox_hash(_as_dict(spatial_interpretation.get("runtime_bbox_used"))),
                    "bbox_candidate_hash": _bbox_hash(_as_dict(spatial_interpretation.get("bbox_candidate"))),
                    "target_option_text": spatial_interpretation.get("target_option_text"),
                    "interpretation_status": spatial_interpretation.get("interpretation_status"),
                    "fallback_reason": spatial_interpretation.get("fallback_reason"),
                    "route_context": spatial_interpretation.get("route_context"),
                    "runtime_spatial_strategy_used": spatial_interpretation.get("runtime_spatial_strategy_used"),
                }
            )[:24]
            spatial_interpretation["spatial_plan_signature"] = spatial_sig
            spatial_state_cache = _as_dict(state.resume_context.get("phase3_spatial_interpretation_state"))
            prev_spatial = _as_dict(spatial_state_cache.get(STEP_P3_1_EXTRACT))
            prev_sig = str(prev_spatial.get("signature") or "").strip()
            retry_changed_spatial_plan = True
            same_spatial_plan_retry_count = 0
            if int(attempt_no or 0) > 1 and prev_sig:
                retry_changed_spatial_plan = bool(prev_sig != spatial_sig)
                same_spatial_plan_retry_count = int(prev_spatial.get("same_spatial_plan_retry_count") or 0)
                if not retry_changed_spatial_plan:
                    same_spatial_plan_retry_count += 1
            spatial_interpretation["retry_changed_spatial_plan"] = bool(retry_changed_spatial_plan)
            spatial_interpretation["same_spatial_plan_retry_count"] = int(same_spatial_plan_retry_count)
            spatial_state_cache[str(STEP_P3_1_EXTRACT)] = {
                "signature": spatial_sig,
                "same_spatial_plan_retry_count": int(same_spatial_plan_retry_count),
            }
            state.resume_context["phase3_spatial_interpretation_state"] = spatial_state_cache
            spatial_warning_codes: List[str] = []
            if bool(spatial_interpretation.get("target_option_received")) and str(
                spatial_interpretation.get("spatial_interpretation_status") or ""
            ).strip() in {"failed", "fallback_default_bbox", "invalid_explicit_bbox"}:
                spatial_warning_codes.append("spatial_interpretation_failed")
            if bool(spatial_interpretation.get("target_option_received")) and str(
                spatial_interpretation.get("runtime_spatial_strategy_used") or ""
            ).strip() == "default_bbox_fallback":
                spatial_warning_codes.append("target_intent_ignored")
            if bool(spatial_interpretation.get("target_option_received")) and retry_changed_spatial_plan is False:
                spatial_warning_codes.append("spatial_plan_reused_without_change")
            if spatial_warning_codes:
                existing = list(retry_applied.get("retry_parameter_warnings") or [])
                existing.extend(list(spatial_warning_codes))
                retry_applied["retry_parameter_warnings"] = list(dict.fromkeys(existing))
            attempt_records: List[Dict[str, Any]] = []

            def _fetch_attempt_status(fetch_payload: Dict[str, Any]) -> str:
                fetched_obj = dict(fetch_payload or {})
                if not fetched_obj:
                    return "error"
                http_status = _handoff_to_int(fetched_obj.get("http_status"))
                fetch_success_signal = bool(
                    _handoff_to_bool(fetched_obj.get("stored"))
                    or (
                        http_status is not None
                        and 200 <= int(http_status) < 300
                    )
                )
                fetch_nonempty_signal = bool(
                    (_handoff_to_int(fetched_obj.get("raw_count")) or 0) > 0
                    or (_handoff_to_int(fetched_obj.get("candidate_count")) or 0) > 0
                    or _as_dict(fetched_obj.get("selected_relation_summary"))
                )
                if fetch_success_signal or fetch_nonempty_signal:
                    return "success"
                return "empty"

            if route_id:
                fetched = dict(
                    phase3_client.run_step_10_fetch(
                        route_id=route_id,
                        osm_relation_id=route_extract_conf.get("osm_relation_id"),
                    )
                    or {}
                )
                artifacts.append({"artifact_type": "phase3_fetch", "payload": fetched})
                if fetched:
                    artifacts.append(
                        {
                            "artifact_type": "phase3_fetch_selected_relation",
                            "payload": {
                                "route_id": route_id,
                                "candidate_universe_count": _handoff_to_int(fetched.get("candidate_universe_count")),
                                "selected_relation_summary": _as_dict(fetched.get("selected_relation_summary")),
                                "http_status": fetched.get("http_status"),
                                "raw_count": _handoff_to_int(fetched.get("raw_count")),
                            },
                        }
                    )
                attempt_records.append(
                    _attempt_record(
                        phase="phase3",
                        step_id=STEP_P3_1_EXTRACT,
                        attempt_no=int(attempt_no),
                        run_id=state.run_id,
                        trace_id=state.trace_id,
                        bbox_used=_as_dict(route_extract_conf.get("bbox")),
                        bbox_fingerprint=_bbox_hash(_as_dict(route_extract_conf.get("bbox"))),
                        route_id=route_id,
                        corridor_context=(str(route_extract_conf.get("service_route_id") or "") or None),
                        route_context_fingerprint=route_context_fp,
                        retry_strategy=retry_applied.get("retry_strategy"),
                        retry_reason=retry_applied.get("retry_reason"),
                        retry_parameter_delta=dict(retry_applied.get("retry_parameter_delta") or {}),
                        action_or_template_used="step_10_fetch",
                        fallback_profile_used=retry_applied.get("fallback_profile_used"),
                        status=_fetch_attempt_status(fetched),
                        duration_ms=_handoff_to_int(fetched.get("runtime_ms")),
                        timeout_flag=None,
                        response_status=fetched.get("http_status"),
                        response_size_bytes=_handoff_to_int(
                            fetched.get("response_size_bytes")
                            if fetched.get("response_size_bytes") is not None
                            else fetched.get("bytes")
                        ),
                        raw_elements_count=_handoff_to_int(fetched.get("raw_count")),
                        candidate_count=_handoff_to_int(fetched.get("candidate_count")),
                        extractor_diagnostics_summary={
                            "fetch_http_status": fetched.get("http_status"),
                            "fetch_candidate_count": _handoff_to_int(fetched.get("candidate_count")),
                        },
                        error_code=(str(fetched.get("error_code") or "").strip() or None),
                        error_summary=(str(fetched.get("error") or "") or None),
                        effective_config_fingerprint=str(retry_applied.get("effective_config_fingerprint") or ""),
                        spatial_plan_signature=spatial_sig,
                        retry_changed_spatial_plan=bool(retry_changed_spatial_plan),
                        spatial_interpretation_status=(
                            str(spatial_interpretation.get("spatial_interpretation_status") or "").strip() or None
                        ),
                        runtime_spatial_strategy_used=(
                            str(spatial_interpretation.get("runtime_spatial_strategy_used") or "").strip() or None
                        ),
                        spatial_interpretation_failure_reason=(
                            str(spatial_interpretation.get("spatial_interpretation_failure_reason") or "").strip() or None
                        ),
                        target_option_received=_handoff_to_bool(spatial_interpretation.get("target_option_received")),
                        notes=list(retry_applied.get("notes") or []),
                    )
                )
            else:
                bbox_obj = _scope_bbox(route_extract_conf)
                bbox = (
                    float(bbox_obj.get("south")),
                    float(bbox_obj.get("west")),
                    float(bbox_obj.get("north")),
                    float(bbox_obj.get("east")),
                )
                discovered = dict(
                    phase3_client.run_step_05_discover(
                        route_id=None,
                        service_route_id=route_extract_conf.get("service_route_id"),
                        direction_id=route_extract_conf.get("direction_id"),
                        bbox=bbox,
                        refs=list(route_extract_conf.get("refs") or []),
                        operator=route_extract_conf.get("operator"),
                        name=route_extract_conf.get("name"),
                        max_candidates=int(route_extract_conf.get("max_candidates") or 50),
                        timeout_s=int(route_extract_conf.get("timeout_s") or 180),
                        query_strategy=str(route_extract_conf.get("query_strategy") or "bbox_first_broad"),
                    )
                    or {}
                )
                route_id = str(discovered.get("route_id") or "").strip()
                artifacts.append({"artifact_type": "phase3_discover", "payload": discovered})
                if _as_dict(discovered.get("candidate_universe_summary")):
                    artifacts.append(
                        {
                            "artifact_type": "phase3_candidate_universe",
                            "payload": _as_dict(discovered.get("candidate_universe_summary")),
                        }
                    )
                if _as_dict(discovered.get("selection_summary")):
                    artifacts.append(
                        {
                            "artifact_type": "phase3_selection_summary",
                            "payload": _as_dict(discovered.get("selection_summary")),
                        }
                    )
                if list(discovered.get("candidate_preview") or []):
                    artifacts.append(
                        {
                            "artifact_type": "phase3_candidate_preview",
                            "payload": list(discovered.get("candidate_preview") or []),
                        }
                    )
                extractor_attempts = [dict(x or {}) for x in list(discovered.get("extractor_attempts") or [])]
                discover_diag = _as_dict(discovered.get("extractor_diagnostics"))
                for idx, row in enumerate(extractor_attempts, start=1):
                    error_class = str(row.get("error_class") or "").strip()
                    status = "success" if int(row.get("returncode") or 1) == 0 else ("timeout" if error_class == "timeout" else "error")
                    row_candidate_count = _handoff_to_int(
                        row.get("candidate_count")
                        if row.get("candidate_count") is not None
                        else discover_diag.get("candidate_count")
                    )
                    if status == "success" and row_candidate_count is not None and row_candidate_count <= 0:
                        status = "empty"
                    attempt_fp_source = {
                        "bbox": bbox_obj,
                        "refs": list(route_extract_conf.get("refs") or []),
                        "operator": route_extract_conf.get("operator"),
                        "name": route_extract_conf.get("name"),
                        "max_candidates": route_extract_conf.get("max_candidates"),
                        "timeout_s": route_extract_conf.get("timeout_s"),
                        "query_strategy": route_extract_conf.get("query_strategy"),
                        "overpass_url": row.get("overpass_url"),
                        "phase": row.get("phase"),
                    }
                    attempt_records.append(
                        _attempt_record(
                            phase="phase3",
                            step_id=STEP_P3_1_EXTRACT,
                            attempt_no=idx,
                            run_id=state.run_id,
                            trace_id=state.trace_id,
                            bbox_used=_as_dict(route_extract_conf.get("bbox")),
                            bbox_fingerprint=_bbox_hash(_as_dict(route_extract_conf.get("bbox"))),
                            route_id=route_id or None,
                            corridor_context=(str(route_extract_conf.get("service_route_id") or "") or None),
                            route_context_fingerprint=route_context_fp,
                            retry_strategy=retry_applied.get("retry_strategy"),
                            retry_reason=retry_applied.get("retry_reason"),
                            retry_parameter_delta=dict(retry_applied.get("retry_parameter_delta") or {}),
                            action_or_template_used="step_05_discover",
                            fallback_config_used=(retry_applied.get("retry_parameter_applied") or {}).get("fallback_config"),
                            fallback_profile_used=(
                                str(_as_dict(discovered.get("extractor_fallback_profile")).get("trigger") or "")
                                or retry_applied.get("fallback_profile_used")
                            ),
                            status=status,
                            duration_ms=_handoff_to_int(
                                row.get("duration_ms")
                                if row.get("duration_ms") is not None
                                else row.get("runtime_ms")
                            ),
                            timeout_flag=(error_class == "timeout"),
                            response_status=(row.get("http_status") if row.get("http_status") is not None else row.get("returncode")),
                            response_size_bytes=_handoff_to_int(
                                row.get("response_size_bytes")
                                if row.get("response_size_bytes") is not None
                                else row.get("bytes")
                            ),
                            raw_elements_count=None,
                            candidate_count=row_candidate_count,
                            extractor_diagnostics_summary={
                                "discover_candidate_count": _handoff_to_int(discover_diag.get("candidate_count")),
                                "discover_signal_strength": discover_diag.get("signal_strength"),
                                "discover_quality_flags": list(discover_diag.get("quality_flags") or []),
                                "candidate_universe_count": _handoff_to_int(
                                    _as_dict(discovered.get("candidate_universe_summary")).get("candidate_universe_count")
                                ),
                                "selection_confidence": _handoff_to_float(
                                    _as_dict(discovered.get("selection_summary")).get("selection_confidence")
                                ),
                                "query_strategy": discovered.get("query_strategy"),
                            },
                            error_code=(str(row.get("error_code") or error_class).strip() or None),
                            error_summary=(str(row.get("error_summary") or error_class).strip() or None),
                            effective_config_fingerprint=_config_fingerprint(attempt_fp_source)[:24],
                            spatial_plan_signature=spatial_sig,
                            retry_changed_spatial_plan=(bool(retry_changed_spatial_plan) if idx == 1 else False),
                            spatial_interpretation_status=(
                                str(spatial_interpretation.get("spatial_interpretation_status") or "").strip() or None
                            ),
                            runtime_spatial_strategy_used=(
                                str(spatial_interpretation.get("runtime_spatial_strategy_used") or "").strip() or None
                            ),
                            spatial_interpretation_failure_reason=(
                                str(spatial_interpretation.get("spatial_interpretation_failure_reason") or "").strip() or None
                            ),
                            target_option_received=_handoff_to_bool(spatial_interpretation.get("target_option_received")),
                            notes=list(retry_applied.get("notes") or []),
                        )
                    )
                if route_id:
                    fetched = dict(
                        phase3_client.run_step_10_fetch(
                            route_id=route_id,
                            osm_relation_id=discovered.get("chosen_osm_relation_id"),
                        )
                        or {}
                    )
                    artifacts.append({"artifact_type": "phase3_fetch", "payload": fetched})
                    if fetched:
                        artifacts.append(
                            {
                                "artifact_type": "phase3_fetch_selected_relation",
                                "payload": {
                                    "route_id": route_id,
                                    "candidate_universe_count": _handoff_to_int(fetched.get("candidate_universe_count")),
                                    "selected_relation_summary": _as_dict(fetched.get("selected_relation_summary")),
                                    "http_status": fetched.get("http_status"),
                                    "raw_count": _handoff_to_int(fetched.get("raw_count")),
                                },
                            }
                        )
                    attempt_records.append(
                        _attempt_record(
                            phase="phase3",
                            step_id=STEP_P3_1_EXTRACT,
                            attempt_no=max(1, len(attempt_records) + 1),
                            run_id=state.run_id,
                            trace_id=state.trace_id,
                            bbox_used=_as_dict(route_extract_conf.get("bbox")),
                            bbox_fingerprint=_bbox_hash(_as_dict(route_extract_conf.get("bbox"))),
                            route_id=route_id,
                            corridor_context=(str(route_extract_conf.get("service_route_id") or "") or None),
                            route_context_fingerprint=route_context_fp,
                            retry_strategy=retry_applied.get("retry_strategy"),
                            retry_reason=retry_applied.get("retry_reason"),
                            retry_parameter_delta=dict(retry_applied.get("retry_parameter_delta") or {}),
                            action_or_template_used="step_10_fetch",
                            fallback_config_used=(retry_applied.get("retry_parameter_applied") or {}).get("fallback_config"),
                            fallback_profile_used=retry_applied.get("fallback_profile_used"),
                            status=_fetch_attempt_status(fetched),
                            duration_ms=_handoff_to_int(fetched.get("runtime_ms")),
                            timeout_flag=None,
                            response_status=fetched.get("http_status"),
                            response_size_bytes=_handoff_to_int(
                                fetched.get("response_size_bytes")
                                if fetched.get("response_size_bytes") is not None
                                else fetched.get("bytes")
                            ),
                            raw_elements_count=_handoff_to_int(fetched.get("raw_count")),
                            candidate_count=_handoff_to_int(fetched.get("candidate_count")),
                            extractor_diagnostics_summary={
                                "fetch_http_status": fetched.get("http_status"),
                                "fetch_candidate_count": _handoff_to_int(fetched.get("candidate_count")),
                            },
                            error_code=(str(fetched.get("error_code") or "").strip() or None),
                            error_summary=(str(fetched.get("error") or "") or None),
                            effective_config_fingerprint=str(retry_applied.get("effective_config_fingerprint") or ""),
                            spatial_plan_signature=spatial_sig,
                            retry_changed_spatial_plan=bool(retry_changed_spatial_plan),
                            spatial_interpretation_status=(
                                str(spatial_interpretation.get("spatial_interpretation_status") or "").strip() or None
                            ),
                            runtime_spatial_strategy_used=(
                                str(spatial_interpretation.get("runtime_spatial_strategy_used") or "").strip() or None
                            ),
                            spatial_interpretation_failure_reason=(
                                str(spatial_interpretation.get("spatial_interpretation_failure_reason") or "").strip() or None
                            ),
                            target_option_received=_handoff_to_bool(spatial_interpretation.get("target_option_received")),
                            notes=list(retry_applied.get("notes") or []),
                        )
                    )

            if not route_id:
                attempt_records = _mark_attempt_change(attempt_records)
                attempt_summary = _summarize_attempt_records(attempt_records)
                target_option_received = bool(
                    _handoff_to_bool(spatial_interpretation.get("target_option_received"))
                )
                spatial_status = str(spatial_interpretation.get("spatial_interpretation_status") or "").strip()
                phase3_recommended_action = (
                    "pause_and_patch_extractor_spatial_interpretation"
                    if (
                        target_option_received
                        and int(attempt_no or 0) >= 2
                        and _handoff_to_bool(spatial_interpretation.get("retry_changed_spatial_plan")) is False
                        and int(same_spatial_plan_retry_count or 0) >= 1
                    )
                    else (
                        "inspect_spatial_interpretation_before_retry"
                        if (
                            target_option_received
                            and spatial_status in {"failed", "fallback_default_bbox", "invalid_explicit_bbox"}
                        )
                        else "retry_route_extraction_or_adjust_scope"
                    )
                )
                return ExecutorResult(
                    ok=False,
                    summary={
                        "error": "route_id_unavailable_after_extract",
                        "bbox": _as_dict(spatial_interpretation.get("runtime_bbox_used")) or None,
                        "spatial_interpretation": dict(spatial_interpretation or {}),
                        "query_strategy": route_extract_conf.get("query_strategy"),
                        "effective_config_fingerprint": str(retry_applied.get("effective_config_fingerprint") or ""),
                        "retry_strategy": retry_applied.get("retry_strategy"),
                        "retry_reason": retry_applied.get("retry_reason"),
                        "retry_parameter_delta": dict(retry_applied.get("retry_parameter_delta") or {}),
                        "retry_parameter_applied": dict(retry_applied.get("retry_parameter_applied") or {}),
                        "retry_parameter_warnings": list(retry_applied.get("retry_parameter_warnings") or []),
                        "retry_diversified": bool(attempt_summary.get("diversified_attempts")),
                        "extraction_attempt_records": attempt_records,
                        "extraction_attempts_summary": attempt_summary,
                        "extraction_attempt_history_summary": attempt_summary,
                        "attempt_record_schema_version": "extractor_attempt_v1",
                        "candidate_universe_summary": _as_dict(discovered.get("candidate_universe_summary")),
                        "selection_summary": _as_dict(discovered.get("selection_summary")),
                        "candidate_preview": list(discovered.get("candidate_preview") or []),
                        "validator_payload": {
                            "route_id": None,
                            "prior_stop_count": 0,
                            "discover_candidate_count": None,
                            "discover_signal_strength": None,
                            "discover_quality_flags": [],
                            "spatial_interpretation": dict(spatial_interpretation or {}),
                            "target_option_received": _handoff_to_bool(spatial_interpretation.get("target_option_received")),
                            "target_option_text": spatial_interpretation.get("target_option_text"),
                            "spatial_interpretation_attempted": _handoff_to_bool(spatial_interpretation.get("spatial_interpretation_attempted")),
                            "spatial_interpretation_status": spatial_interpretation.get("spatial_interpretation_status"),
                            "spatial_interpretation_source": spatial_interpretation.get("spatial_interpretation_source"),
                            "bbox_candidate": _as_dict(spatial_interpretation.get("bbox_candidate")) or None,
                            "bbox_candidate_confidence": _handoff_to_float(spatial_interpretation.get("bbox_candidate_confidence")),
                            "bbox_validation_status": spatial_interpretation.get("bbox_validation_status"),
                            "runtime_bbox_used": _as_dict(spatial_interpretation.get("runtime_bbox_used")) or None,
                            "runtime_spatial_strategy_used": spatial_interpretation.get("runtime_spatial_strategy_used"),
                            "retry_changed_spatial_plan": _handoff_to_bool(spatial_interpretation.get("retry_changed_spatial_plan")),
                            "same_spatial_plan_retry_count": _handoff_to_int(spatial_interpretation.get("same_spatial_plan_retry_count")),
                            "spatial_interpretation_failure_reason": spatial_interpretation.get("spatial_interpretation_failure_reason"),
                            "candidate_universe_summary": _as_dict(discovered.get("candidate_universe_summary")),
                            "selection_summary": _as_dict(discovered.get("selection_summary")),
                            "extractor_attempts_summary": attempt_summary,
                            "extractor_attempt_history_summary": attempt_summary,
                            "retry_diversified": bool(attempt_summary.get("diversified_attempts")),
                            "fallback_profile_used": retry_applied.get("fallback_profile_used"),
                            "recommended_action": phase3_recommended_action,
                            "extraction_outcome_classification": "extract_failed",
                        },
                    },
                    artifacts=[
                        *artifacts,
                        {"artifact_type": "extraction_attempt_history_summary", "payload": attempt_summary},
                        {"artifact_type": "phase3_geographic_interpretation", "payload": dict(spatial_interpretation or {})},
                    ],
                )

            prior_rows = list(phase3_client.get_relation_stop_prior(uuid.UUID(route_id)) or [])
            discover_diag: Dict[str, Any] = {}
            discover_payload: Dict[str, Any] = {}
            for art in artifacts:
                if str(art.get("artifact_type") or "") != "phase3_discover":
                    continue
                discover_payload = dict(art.get("payload") or {})
                discover_diag = dict(discover_payload.get("extractor_diagnostics") or {})
                break
            fetch_payload = dict(fetched or {}) if "fetched" in locals() and isinstance(fetched, dict) else {}
            phase3_bundle = _phase3_route_bundle_evidence(
                summary={
                    "route_id": route_id,
                    "prior_stop_count": len(prior_rows),
                    "candidate_universe_summary": _as_dict(discover_payload.get("candidate_universe_summary")),
                    "selection_summary": _as_dict(discover_payload.get("selection_summary")),
                    "selected_relation_summary": _as_dict(fetch_payload.get("selected_relation_summary")),
                    "extractor_diagnostics": discover_diag,
                },
                payload=fetch_payload,
                validator_evidence={},
            )
            for record in reversed(attempt_records):
                if str(record.get("action_or_template_used") or "") != "step_10_fetch":
                    continue
                record["status"] = (
                    "success"
                    if str(phase3_bundle.get("fetch_status_classification") or "") != "empty"
                    else "empty"
                )
                diagnostics = _as_dict(record.get("extractor_diagnostics_summary"))
                diagnostics["fetch_status_classification"] = phase3_bundle.get("fetch_status_classification")
                diagnostics["fetch_observability_gap"] = bool(phase3_bundle.get("fetch_observability_gap"))
                record["extractor_diagnostics_summary"] = diagnostics
                break
            attempt_records = _mark_attempt_change(attempt_records)
            attempt_summary = _summarize_attempt_records(attempt_records)
            fetch_status_classification = str(phase3_bundle.get("fetch_status_classification") or "").strip()
            extraction_outcome = (
                fetch_status_classification
                if fetch_status_classification in P3_PARTIAL_FETCH_CLASSIFICATIONS
                else ("success" if len(prior_rows) > 0 else "empty")
            )
            target_option_received = bool(_handoff_to_bool(spatial_interpretation.get("target_option_received")))
            spatial_status = str(spatial_interpretation.get("spatial_interpretation_status") or "").strip()
            phase3_recommended_action = (
                "pause_and_patch_extractor_spatial_interpretation"
                if (
                    extraction_outcome in {"empty", "error"}
                    and target_option_received
                    and int(attempt_no or 0) >= 2
                    and _handoff_to_bool(spatial_interpretation.get("retry_changed_spatial_plan")) is False
                    and int(same_spatial_plan_retry_count or 0) >= 1
                )
                else (
                    "inspect_spatial_interpretation_before_retry"
                    if (
                        extraction_outcome in {"empty", "error"}
                        and target_option_received
                        and spatial_status in {"failed", "fallback_default_bbox", "invalid_explicit_bbox"}
                    )
                    else (
                        "inspect_selected_relation_fetch_and_validator_mapping"
                        if fetch_status_classification in P3_PARTIAL_FETCH_CLASSIFICATIONS
                        else ("retry_route_extraction_or_adjust_scope" if extraction_outcome in {"empty", "error"} else "continue_or_review")
                    )
                )
            )
            summary = {
                "route_id": route_id,
                "prior_stop_count": len(prior_rows),
                "prior_stop_evidence_count": int(phase3_bundle.get("prior_stop_evidence_count") or 0),
                "extractor_diagnostics": discover_diag,
                "bbox": _as_dict(spatial_interpretation.get("runtime_bbox_used")) or None,
                "spatial_interpretation": dict(spatial_interpretation or {}),
                "query_strategy": route_extract_conf.get("query_strategy"),
                "candidate_universe_summary": _as_dict(discover_payload.get("candidate_universe_summary")),
                "selection_summary": _as_dict(discover_payload.get("selection_summary")),
                "candidate_preview": list(discover_payload.get("candidate_preview") or []),
                "selected_relation_summary": _as_dict(fetch_payload.get("selected_relation_summary")),
                "strong_bundle_evidence": bool(phase3_bundle.get("strong_bundle_evidence")),
                "bundle_success_classification": phase3_bundle.get("bundle_success_classification"),
                "bundle_contradiction_codes": list(phase3_bundle.get("contradiction_codes") or []),
                "fetch_status_classification": fetch_status_classification or None,
                "fetch_observability_gap": bool(phase3_bundle.get("fetch_observability_gap")),
                "effective_config_fingerprint": str(retry_applied.get("effective_config_fingerprint") or ""),
                "retry_strategy": retry_applied.get("retry_strategy"),
                "retry_reason": retry_applied.get("retry_reason"),
                "retry_parameter_delta": dict(retry_applied.get("retry_parameter_delta") or {}),
                "retry_parameter_applied": dict(retry_applied.get("retry_parameter_applied") or {}),
                "retry_parameter_warnings": list(retry_applied.get("retry_parameter_warnings") or []),
                "retry_diversified": bool(attempt_summary.get("diversified_attempts")),
                "extraction_attempt_records": attempt_records,
                "extraction_attempts_summary": attempt_summary,
                "extraction_attempt_history_summary": attempt_summary,
                "attempt_record_schema_version": "extractor_attempt_v1",
                "validator_payload": {
                    "prior_stop_count": len(prior_rows),
                    "prior_stop_evidence_count": int(phase3_bundle.get("prior_stop_evidence_count") or 0),
                    "route_id": route_id,
                    "discover_candidate_count": discover_diag.get("candidate_count"),
                    "discover_signal_strength": discover_diag.get("signal_strength"),
                    "discover_quality_flags": list(discover_diag.get("quality_flags") or []),
                    "spatial_interpretation": dict(spatial_interpretation or {}),
                    "target_option_received": _handoff_to_bool(spatial_interpretation.get("target_option_received")),
                    "target_option_text": spatial_interpretation.get("target_option_text"),
                    "spatial_interpretation_attempted": _handoff_to_bool(spatial_interpretation.get("spatial_interpretation_attempted")),
                    "spatial_interpretation_status": spatial_interpretation.get("spatial_interpretation_status"),
                    "spatial_interpretation_source": spatial_interpretation.get("spatial_interpretation_source"),
                    "bbox_candidate": _as_dict(spatial_interpretation.get("bbox_candidate")) or None,
                    "bbox_candidate_confidence": _handoff_to_float(spatial_interpretation.get("bbox_candidate_confidence")),
                    "bbox_validation_status": spatial_interpretation.get("bbox_validation_status"),
                    "runtime_bbox_used": _as_dict(spatial_interpretation.get("runtime_bbox_used")) or None,
                    "runtime_spatial_strategy_used": spatial_interpretation.get("runtime_spatial_strategy_used"),
                    "retry_changed_spatial_plan": _handoff_to_bool(spatial_interpretation.get("retry_changed_spatial_plan")),
                    "same_spatial_plan_retry_count": _handoff_to_int(spatial_interpretation.get("same_spatial_plan_retry_count")),
                    "spatial_interpretation_failure_reason": spatial_interpretation.get("spatial_interpretation_failure_reason"),
                    "candidate_universe_summary": _as_dict(discover_payload.get("candidate_universe_summary")),
                    "selection_summary": _as_dict(discover_payload.get("selection_summary")),
                    "selected_relation_summary": _as_dict(fetch_payload.get("selected_relation_summary")),
                    "selected_relation_present": bool(phase3_bundle.get("selected_relation_present")),
                    "selected_osm_relation_id": phase3_bundle.get("selected_osm_relation_id"),
                    "selected_relation_stop_prior_count": int(phase3_bundle.get("selected_relation_stop_prior_count") or 0),
                    "top_stop_prior_count": int(phase3_bundle.get("top_stop_prior_count") or 0),
                    "selection_confidence": phase3_bundle.get("selection_confidence"),
                    "selection_reason_codes": list(phase3_bundle.get("selection_reason_codes") or []),
                    "strong_bundle_evidence": bool(phase3_bundle.get("strong_bundle_evidence")),
                    "bundle_success_classification": phase3_bundle.get("bundle_success_classification"),
                    "bundle_contradiction_codes": list(phase3_bundle.get("contradiction_codes") or []),
                    "fetch_status_classification": fetch_status_classification or None,
                    "fetch_observability_gap": bool(phase3_bundle.get("fetch_observability_gap")),
                    "extractor_attempts_summary": attempt_summary,
                    "extractor_attempt_history_summary": attempt_summary,
                    "retry_diversified": bool(attempt_summary.get("diversified_attempts")),
                    "fallback_profile_used": retry_applied.get("fallback_profile_used"),
                    "recommended_action": phase3_recommended_action,
                    "extraction_outcome_classification": extraction_outcome,
                },
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[
                    *artifacts,
                    {"artifact_type": "extraction_attempt_history_summary", "payload": attempt_summary},
                    {"artifact_type": "phase3_geographic_interpretation", "payload": dict(spatial_interpretation or {})},
                ],
            )

        def _p3_step20(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            route_id = _route_id(state)
            if not route_id:
                return ExecutorResult(ok=False, summary={"error": "route_id_required_for_step20"}, artifacts=[])
            out = dict(
                phase3_client.run_step_20_sequences(
                    route_id=uuid.UUID(route_id),
                    match_radius_m=(float(params.get("match_radius_m")) if params.get("match_radius_m") is not None else None),
                )
                or {}
            )
            sync_out = dict(
                phase3_client.sync_unresolved_prior_to_phase1_requests(
                    uuid.UUID(route_id),
                    radius_m=float(params.get("match_radius_m") or 3.0),
                    dry_run=False,
                )
                or {}
            )
            summary = {
                **out,
                "phase1_request_sync": sync_out,
                "validator_payload": {
                    "unmatched_count": int(out.get("unmatched_count") or 0),
                    "ambiguous_count": int(out.get("ambiguous_count") or 0),
                    "sequence_gate_pass": out.get("sequence_gate_pass"),
                    "sequence_quality_score": (
                        float(out.get("sequence_quality_score"))
                        if out.get("sequence_quality_score") is not None
                        else None
                    ),
                    "sequence_warning_tags": list(out.get("sequence_warning_tags") or []),
                    "sequence_warning_subtypes": dict(out.get("sequence_warning_subtypes") or {}),
                    "sequence_diagnostic_profile_version": out.get("sequence_diagnostic_profile_version"),
                    "sequence_quality_warning_threshold": (
                        dict(
                            dict(out.get("step20_diagnostics_payload") or {}).get("threshold_profile") or {}
                        ).get("thresholds", {}).get("sequence_quality_warning_score")
                    ),
                    "step20_dominant_cause": out.get("blocker_origin_hint"),
                    "step20_dominant_cause_confidence": out.get("blocker_origin_confidence"),
                    "step20_dominant_cause_reason": out.get("blocker_origin_reason"),
                    "step20_triage_route": (dict(out.get("step20_diagnostics_payload") or {}).get("triage_route")),
                    "candidate_count": int(out.get("candidate_count") or 0),
                    "candidate_shortlist": list(out.get("candidate_shortlist") or []),
                    "recommended_candidate_id": out.get("recommended_stop_sequence_candidate_id"),
                    "approved_candidate_id": out.get("approved_stop_sequence_candidate_id"),
                    "sequence_stabilized": bool(out.get("sequence_stabilized")),
                    "variant_state": out.get("variant_state"),
                    "sequence_approval_status": out.get("sequence_approval_status"),
                    "variant_pressure_detected": bool(out.get("variant_pressure_detected")),
                    "variant_pressure_reasons": list(out.get("variant_pressure_reasons") or []),
                    "variant_groups": list(out.get("variant_groups") or []),
                    "recommended_variant_group_key": out.get("recommended_variant_group_key"),
                    "approved_variant_group_key": out.get("approved_variant_group_key"),
                    "recommended_candidates_by_variant": list(out.get("recommended_candidates_by_variant") or []),
                    "variant_evidence_summary": list(out.get("variant_evidence_summary") or []),
                    "direction_stable": bool(out.get("direction_stable")),
                    "direction_reasons": list(out.get("direction_reasons") or []),
                    "reorder_recommended": out.get("reorder_recommended"),
                    "reorder_confidence": out.get("reorder_confidence"),
                    "proposal_ready": bool(out.get("reorder_proposal")),
                    "operator_approval_required": bool(
                        dict(out.get("reorder_proposal") or {}).get("operator_approval_required")
                    ),
                    "needs_reorder": bool(dict(out.get("reorder_proposal") or {}).get("needs_reorder")),
                    "reorder_proposal": dict(out.get("reorder_proposal") or {}),
                },
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[
                    {"artifact_type": "phase3_step20", "payload": out},
                    {"artifact_type": "phase3_step20_phase1_sync", "payload": sync_out},
                ],
            )

        def _p3_inverse_completion(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no, params
            route_id = _route_id(state)
            if not route_id:
                return ExecutorResult(
                    ok=False,
                    summary={"error": "route_id_required_for_inverse_completion"},
                    artifacts=[],
                )
            initial_gate = dict(phase3_client.get_step20_direction_gate(route_id=route_id) or {})
            service_route_id = str(initial_gate.get("service_route_id") or "").strip() or None
            raw_direction_id = initial_gate.get("direction_id")
            direction_id = int(raw_direction_id) if raw_direction_id in (0, 1) else None

            refresh_out: Dict[str, Any] = {}
            search_out: Dict[str, Any] = {}
            targeted_search_attempted = False
            targeted_search_ran = False

            try:
                refresh_out = dict(
                    phase3_client.refresh_inverse_proposals(
                        service_route_id=service_route_id,
                        route_id=route_id,
                        limit=20,
                        include_ready=True,
                    )
                    or {}
                )
            except Exception as exc:
                refresh_out = {"error": str(exc)}

            gate_after_refresh = dict(
                phase3_client.get_step20_direction_gate(
                    service_route_id=service_route_id,
                    direction_id=direction_id,
                    route_id=route_id,
                )
                or {}
            )

            if (
                not bool(gate_after_refresh.get("gate_passed"))
                and service_route_id
                and direction_id in {0, 1}
            ):
                targeted_search_attempted = True
                try:
                    search_out = dict(
                        phase3_client.dispatch_targeted_inverse_search(
                            service_route_id=service_route_id,
                            direction_id=int(direction_id),
                            force=False,
                        )
                        or {}
                    )
                except Exception as exc:
                    search_out = {
                        "service_route_id": service_route_id,
                        "direction_id": int(direction_id),
                        "eligible": True,
                        "launched": False,
                        "direction_ready": False,
                        "search_status": "failed",
                        "search_error": str(exc),
                        "blocker_codes": ["targeted_inverse_search_failed"],
                        "blocker_messages": [str(exc)],
                    }
                targeted_search_ran = bool(search_out.get("launched"))

            final_gate = dict(
                phase3_client.get_step20_direction_gate(
                    service_route_id=service_route_id,
                    direction_id=direction_id,
                    route_id=route_id,
                )
                or {}
            )
            inverse_summary = {}
            if service_route_id:
                try:
                    inverse_summary = dict(
                        phase3_client.get_inverse_completion_summary(
                            service_route_id=service_route_id,
                            limit=20,
                            include_ready=True,
                        )
                        or {}
                    )
                except Exception:
                    inverse_summary = {}

            validator_payload = {
                "route_id": route_id,
                "service_route_id": service_route_id,
                "direction_id": direction_id,
                "gate_passed": bool(final_gate.get("gate_passed")),
                "direction_ready": bool(final_gate.get("direction_ready")),
                "gate_code": final_gate.get("gate_code"),
                "inverse_status": final_gate.get("inverse_status"),
                "search_status": final_gate.get("search_status"),
                "blocker_codes": list(final_gate.get("blocker_codes") or []),
                "blocker_messages": list(final_gate.get("blocker_messages") or []),
                "suggested_next_action": final_gate.get("suggested_next_action"),
                "targeted_inverse_search_attempted": bool(targeted_search_attempted),
                "targeted_inverse_search_ran": bool(targeted_search_ran),
                "targeted_inverse_search_status": search_out.get("search_status"),
                "targeted_inverse_search_eligible": search_out.get("eligible"),
            }
            summary = {
                "route_id": route_id,
                "service_route_id": service_route_id,
                "direction_id": direction_id,
                "inverse_completion_passed": bool(final_gate.get("gate_passed")),
                "direction_ready": bool(final_gate.get("direction_ready")),
                "gate_code": final_gate.get("gate_code"),
                "gate_message": final_gate.get("gate_message"),
                "inverse_status": final_gate.get("inverse_status"),
                "search_status": final_gate.get("search_status"),
                "blocker_codes": list(final_gate.get("blocker_codes") or []),
                "blocker_messages": list(final_gate.get("blocker_messages") or []),
                "suggested_next_action": final_gate.get("suggested_next_action"),
                "targeted_inverse_search_attempted": bool(targeted_search_attempted),
                "targeted_inverse_search_ran": bool(targeted_search_ran),
                "targeted_inverse_search_status": search_out.get("search_status"),
                "inverse_completion_summary": inverse_summary,
                "validator_payload": validator_payload,
            }
            artifacts: List[Dict[str, Any]] = [
                {"artifact_type": "phase3_inverse_completion", "payload": summary},
            ]
            if refresh_out:
                artifacts.append({"artifact_type": "phase3_inverse_completion_refresh", "payload": refresh_out})
            if search_out:
                artifacts.append({"artifact_type": "phase3_inverse_search", "payload": search_out})
            return ExecutorResult(ok=True, summary=summary, artifacts=artifacts)

        def _p3_reorder_proposal(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            route_id = _route_id(state)
            if not route_id:
                return ExecutorResult(ok=False, summary={"error": "route_id_required_for_reorder_proposal"}, artifacts=[])
            latest_step20 = dict(state.resume_context.get("latest_step20_summary") or {})
            resolution = dict(phase3_client.get_sequence_resolution_state(uuid.UUID(route_id)) or {})
            proposal = dict(latest_step20.get("reorder_proposal") or {})
            shortlist = list(
                proposal.get("candidate_shortlist")
                or latest_step20.get("candidate_shortlist")
                or resolution.get("candidate_shortlist")
                or []
            )
            recommended_candidate_id = (
                str(
                    proposal.get("recommended_candidate_id")
                    or latest_step20.get("recommended_stop_sequence_candidate_id")
                    or resolution.get("recommended_stop_sequence_candidate_id")
                    or ""
                ).strip()
                or None
            )
            approved_candidate_id = (
                str(
                    proposal.get("approved_candidate_id")
                    or latest_step20.get("approved_stop_sequence_candidate_id")
                    or resolution.get("approved_stop_sequence_candidate_id")
                    or ""
                ).strip()
                or None
            )
            sequence_stabilized = bool(
                proposal.get("sequence_stabilized")
                if proposal.get("sequence_stabilized") is not None
                else resolution.get("sequence_stabilized")
            )
            direction_stable = bool(
                proposal.get("direction_stable")
                if proposal.get("direction_stable") is not None
                else resolution.get("direction_stable")
            )
            variant_pressure_detected = bool(
                proposal.get("variant_pressure_detected")
                if proposal.get("variant_pressure_detected") is not None
                else resolution.get("variant_pressure_detected")
            )
            variant_state = str(
                proposal.get("variant_state")
                if proposal.get("variant_state") is not None
                else resolution.get("variant_state")
                or "no_variant_issue"
            )
            operator_approval_required = bool(
                proposal.get("operator_approval_required")
                if proposal.get("operator_approval_required") is not None
                else (
                    proposal.get("requires_approval")
                    if proposal.get("requires_approval") is not None
                    else (not sequence_stabilized)
                )
            )
            needs_reorder = bool(
                proposal.get("needs_reorder")
                if proposal.get("needs_reorder") is not None
                else (
                    operator_approval_required
                    or (approved_candidate_id is None and recommended_candidate_id is not None)
                )
            )
            if not proposal:
                proposal = {
                    "requires_approval": operator_approval_required,
                    "operator_approval_required": operator_approval_required,
                    "needs_reorder": needs_reorder,
                    "apply_recommended": bool(recommended_candidate_id and approved_candidate_id != recommended_candidate_id),
                    "blocking": not sequence_stabilized,
                    "blocking_reason": (
                        "canonical_sequence_unapproved"
                        if not sequence_stabilized
                        else None
                    ),
                    "risk_level": "high" if not sequence_stabilized else "medium",
                    "recommended_candidate_id": recommended_candidate_id,
                    "approved_candidate_id": approved_candidate_id,
                    "candidate_shortlist": shortlist,
                    "recommendation_reason": (
                        "Step 20 recommended a canonical sequence candidate for operator confirmation."
                        if recommended_candidate_id
                        else "Sequence proposal prepared for operator confirmation."
                    ),
                    "sequence_stabilized": sequence_stabilized,
                    "variant_pressure_detected": variant_pressure_detected,
                    "variant_state": variant_state,
                    "variant_pressure_reasons": list(resolution.get("variant_pressure_reasons") or []),
                    "variant_groups": list(resolution.get("variant_groups") or []),
                    "recommended_variant_group_key": resolution.get("recommended_variant_group_key"),
                    "approved_variant_group_key": resolution.get("approved_variant_group_key"),
                    "recommended_candidates_by_variant": list(resolution.get("recommended_candidates_by_variant") or []),
                    "variant_evidence_summary": list(resolution.get("variant_evidence_summary") or []),
                    "direction_stable": direction_stable,
                    "direction_reasons": list(resolution.get("direction_reasons") or []),
                }
            else:
                proposal.setdefault("candidate_shortlist", shortlist)
                proposal.setdefault("recommended_candidate_id", recommended_candidate_id)
                proposal.setdefault("approved_candidate_id", approved_candidate_id)
                proposal.setdefault("sequence_stabilized", sequence_stabilized)
                proposal.setdefault("variant_pressure_detected", variant_pressure_detected)
                proposal.setdefault("variant_state", variant_state)
                proposal.setdefault("variant_pressure_reasons", list(resolution.get("variant_pressure_reasons") or []))
                proposal.setdefault("variant_groups", list(resolution.get("variant_groups") or []))
                proposal.setdefault("recommended_variant_group_key", resolution.get("recommended_variant_group_key"))
                proposal.setdefault("approved_variant_group_key", resolution.get("approved_variant_group_key"))
                proposal.setdefault("recommended_candidates_by_variant", list(resolution.get("recommended_candidates_by_variant") or []))
                proposal.setdefault("variant_evidence_summary", list(resolution.get("variant_evidence_summary") or []))
                proposal.setdefault("direction_stable", direction_stable)
                proposal.setdefault("direction_reasons", list(resolution.get("direction_reasons") or []))
                proposal.setdefault("operator_approval_required", operator_approval_required)
                proposal.setdefault("requires_approval", operator_approval_required)
                proposal.setdefault("needs_reorder", needs_reorder)
            summary = {
                "route_id": route_id,
                "reorder_proposal": proposal,
                "recommended_stop_sequence_candidate_id": recommended_candidate_id,
                "approved_stop_sequence_candidate_id": approved_candidate_id,
                "sequence_stabilized": sequence_stabilized,
                "validator_payload": {
                    "proposal_ready": bool(proposal),
                    "needs_reorder": bool(proposal.get("needs_reorder")),
                    "operator_approval_required": bool(proposal.get("operator_approval_required")),
                    "recommended_candidate_id": recommended_candidate_id,
                    "approved_candidate_id": approved_candidate_id,
                    "candidate_shortlist": shortlist,
                    "sequence_stabilized": sequence_stabilized,
                    "variant_pressure_detected": variant_pressure_detected,
                    "variant_state": variant_state,
                    "variant_groups": list(
                        proposal.get("variant_groups")
                        or resolution.get("variant_groups")
                        or []
                    ),
                    "recommended_variant_group_key": (
                        proposal.get("recommended_variant_group_key")
                        or resolution.get("recommended_variant_group_key")
                    ),
                    "approved_variant_group_key": (
                        proposal.get("approved_variant_group_key")
                        or resolution.get("approved_variant_group_key")
                    ),
                    "recommended_candidates_by_variant": list(
                        proposal.get("recommended_candidates_by_variant")
                        or resolution.get("recommended_candidates_by_variant")
                        or []
                    ),
                    "variant_evidence_summary": list(
                        proposal.get("variant_evidence_summary")
                        or resolution.get("variant_evidence_summary")
                        or []
                    ),
                    "direction_stable": direction_stable,
                    "proposal": proposal,
                },
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[{"artifact_type": "phase3_reorder_proposal", "payload": summary}],
            )

        def _p3_reorder_apply(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            route_id = _route_id(state)
            if not route_id:
                return ExecutorResult(ok=False, summary={"error": "route_id_required_for_reorder_apply"}, artifacts=[])
            selected_candidate_id = str(
                params.get("stop_sequence_candidate_id")
                or params.get("selected_stop_sequence_candidate_id")
                or ""
            ).strip()
            if not selected_candidate_id:
                return ExecutorResult(
                    ok=False,
                    summary={"error": "stop_sequence_candidate_id_required_for_apply"},
                    artifacts=[],
                )
            out = dict(
                phase3_client.approve_stop_sequence_candidate(
                    route_id=uuid.UUID(route_id),
                    stop_sequence_candidate_id=uuid.UUID(selected_candidate_id),
                    approved_by=(
                        str(params.get("operator_id") or "").strip()
                        or str(params.get("operator_role") or "").strip()
                        or None
                    ),
                    notes=(
                        str(params.get("operator_notes") or "").strip()
                        or str(params.get("operator_decision") or "").strip()
                        or None
                    ),
                )
                or {}
            )
            out.update(
                {
                    "route_id": route_id,
                    "stop_sequence_candidate_id": selected_candidate_id,
                    "decision_signature": params.get("decision_signature"),
                    "geometry_set_id": None,
                    "approved_geometry_candidate_id": None,
                }
            )
            return ExecutorResult(
                ok=True,
                summary=out,
                artifacts=[{"artifact_type": "phase3_reorder_apply", "payload": out}],
            )

        def _p3_step30(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            route_id = _route_id(state)
            resolution = {}
            if route_id:
                try:
                    resolution = dict(phase3_client.get_sequence_resolution_state(uuid.UUID(route_id)) or {})
                except Exception:
                    resolution = {}
            stop_sequence_candidate_id = str(
                params.get("stop_sequence_candidate_id")
                or state.resume_context.get("stop_sequence_candidate_id")
                or resolution.get("approved_stop_sequence_candidate_id")
                or (state.pipeline_scope.get("phase3") or {}).get("stop_sequence_candidate_id")
                or ""
            ).strip()
            if not route_id:
                return ExecutorResult(ok=False, summary={"error": "route_id_required_for_step30"}, artifacts=[])
            gate = dict(
                phase3_client.get_step30_gate(
                    uuid.UUID(route_id),
                    stop_sequence_candidate_id=(uuid.UUID(stop_sequence_candidate_id) if stop_sequence_candidate_id else None),
                    match_radius_m=3.0,
                )
                or {}
            )
            if not bool(gate.get("can_run_step30")):
                summary = {
                    "route_id": route_id,
                    "stop_sequence_candidate_id": stop_sequence_candidate_id or gate.get("approved_stop_sequence_candidate_id"),
                    "step30_gate": gate,
                    "validator_payload": {
                        "candidate_count": 0,
                        "geometry_quality": None,
                        "step30_gate": gate,
                        "approved_stop_sequence_candidate_id": gate.get("approved_stop_sequence_candidate_id"),
                        "sequence_stabilized": gate.get("sequence_stabilized"),
                        "variant_pressure_detected": gate.get("variant_pressure_detected"),
                        "variant_state": gate.get("variant_state"),
                        "direction_stable": gate.get("direction_stable"),
                        "blocking_reasons": list(gate.get("blocking_reasons") or []),
                    },
                }
                return ExecutorResult(
                    ok=True,
                    summary=summary,
                    artifacts=[{"artifact_type": "phase3_step30_gate", "payload": gate}],
                )
            if not stop_sequence_candidate_id:
                return ExecutorResult(
                    ok=False,
                    summary={"error": "approved_stop_sequence_candidate_id_required_for_step30"},
                    artifacts=[],
                )
            out = dict(
                phase3_client.run_step_30_geometry(
                    route_id=uuid.UUID(route_id),
                    stop_sequence_candidate_id=uuid.UUID(stop_sequence_candidate_id),
                )
                or {}
            )
            summary = {
                **out,
                "route_id": route_id,
                "stop_sequence_candidate_id": stop_sequence_candidate_id,
                "geometry_set_id": out.get("geometry_set_id"),
                "validator_payload": {
                    "candidate_count": int(out.get("n_candidates") or out.get("candidate_count") or 0),
                    "geometry_quality": out.get("geometry_quality"),
                    "step30_gate": dict(out.get("step30_gate") or gate),
                    "approved_stop_sequence_candidate_id": (
                        dict(out.get("step30_gate") or gate).get("approved_stop_sequence_candidate_id")
                    ),
                    "sequence_stabilized": (
                        dict(out.get("step30_gate") or gate).get("sequence_stabilized")
                    ),
                    "variant_pressure_detected": (
                        dict(out.get("step30_gate") or gate).get("variant_pressure_detected")
                    ),
                    "variant_state": (
                        dict(out.get("step30_gate") or gate).get("variant_state")
                    ),
                    "direction_stable": (
                        dict(out.get("step30_gate") or gate).get("direction_stable")
                    ),
                    "blocking_reasons": list(
                        dict(out.get("step30_gate") or gate).get("blocking_reasons") or []
                    ),
                },
            }
            return ExecutorResult(ok=True, summary=summary, artifacts=[{"artifact_type": "phase3_step30", "payload": out}])

        def _p3_step32(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            route_id = _route_id(state)
            geometry_set_id = str(
                params.get("geometry_set_id")
                or state.resume_context.get("geometry_set_id")
                or (state.pipeline_scope.get("phase3") or {}).get("geometry_set_id")
                or ""
            ).strip()
            if not route_id or not geometry_set_id:
                return ExecutorResult(
                    ok=False,
                    summary={"error": "route_id_and_geometry_set_id_required_for_step32"},
                    artifacts=[],
                )
            out = dict(
                phase3_client.run_step_32_stop_recovery(
                    route_id=uuid.UUID(route_id),
                    geometry_set_id=uuid.UUID(geometry_set_id),
                )
                or {}
            )
            summary = {
                **out,
                "route_id": route_id,
                "geometry_set_id": geometry_set_id,
                "validator_payload": {
                    "geometry_candidate_count": int(out.get("geometry_candidate_count") or 0),
                    "recovered_total": int(out.get("recovered_total") or 0),
                    "ambiguous_total": int(out.get("ambiguous_total") or 0),
                    "rejected_total": int(out.get("rejected_total") or 0),
                },
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[{"artifact_type": "phase3_step32", "payload": out}],
            )

        def _p3_step35(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            route_id = _route_id(state)
            geometry_set_id = str(
                params.get("geometry_set_id")
                or state.resume_context.get("geometry_set_id")
                or (state.pipeline_scope.get("phase3") or {}).get("geometry_set_id")
                or ""
            ).strip()
            if not route_id or not geometry_set_id:
                return ExecutorResult(ok=False, summary={"error": "route_id_and_geometry_set_id_required"}, artifacts=[])
            out = dict(
                phase3_client.run_step_35_rank(
                    route_id=uuid.UUID(route_id),
                    geometry_set_id=uuid.UUID(geometry_set_id),
                    explain=bool(params.get("explain", False)),
                )
                or {}
            )
            ranked = dict(phase3_client.get_ranked_candidates(route_id=uuid.UUID(route_id), geometry_set_id=uuid.UUID(geometry_set_id)) or {})
            summary = {
                **out,
                "route_id": route_id,
                "geometry_set_id": geometry_set_id,
                "ranked_count": len(ranked.get("candidates") or []),
                "validator_payload": {"ranked_count": len(ranked.get("candidates") or [])},
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[{"artifact_type": "phase3_step35", "payload": {"rank": out, "ranked": ranked}}],
            )

        def _p3_step40_prepare(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            route_id = _route_id(state)
            geometry_set_id = str(
                params.get("geometry_set_id")
                or state.resume_context.get("geometry_set_id")
                or (state.pipeline_scope.get("phase3") or {}).get("geometry_set_id")
                or ""
            ).strip()
            if not route_id or not geometry_set_id:
                return ExecutorResult(ok=False, summary={"error": "route_id_and_geometry_set_id_required_for_approve_prepare"}, artifacts=[])
            ranked = dict(phase3_client.get_ranked_candidates(route_id=uuid.UUID(route_id), geometry_set_id=uuid.UUID(geometry_set_id)) or {})
            candidates = list(ranked.get("candidates") or [])
            top = dict(candidates[0] or {}) if candidates else {}
            summary = {
                "route_id": route_id,
                "geometry_set_id": geometry_set_id,
                "ranked_count": len(candidates),
                "top_candidate": top,
                "validator_payload": {"ranked_count": len(candidates), "top_candidate_score": top.get("score")},
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[{"artifact_type": "phase3_step40_prepare", "payload": summary}],
            )

        def _p3_step40_apply(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            route_id = _route_id(state)
            geometry_set_id = str(
                params.get("geometry_set_id")
                or state.resume_context.get("geometry_set_id")
                or (state.pipeline_scope.get("phase3") or {}).get("geometry_set_id")
                or ""
            ).strip()
            if not route_id or not geometry_set_id:
                return ExecutorResult(
                    ok=False,
                    summary={"error": "route_id_and_geometry_set_id_required_for_step40_apply"},
                    artifacts=[],
                )
            out = dict(
                phase3_client.run_step_40_approve(
                    route_id=uuid.UUID(route_id),
                    geometry_set_id=uuid.UUID(geometry_set_id),
                )
                or {}
            )
            summary = {
                **out,
                "route_id": route_id,
                "geometry_set_id": geometry_set_id,
                "approved_geometry_candidate_id": out.get("approved_geometry_candidate_id"),
                "decision_signature": params.get("decision_signature"),
            }
            return ExecutorResult(
                ok=True,
                summary=summary,
                artifacts=[{"artifact_type": "phase3_step40_apply", "payload": summary}],
            )

        def _p3_merge_proposal(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            p3_scope = dict(state.pipeline_scope.get("phase3") or {})
            route_ids = list(params.get("route_ids") or p3_scope.get("route_ids") or [])
            if not route_ids:
                rid = _route_id(state)
                rid_b = str(p3_scope.get("route_id_b") or params.get("route_id_b") or "").strip()
                route_ids = [x for x in [rid, rid_b] if x]
            if len(route_ids) < 2:
                return ExecutorResult(ok=False, summary={"error": "at_least_two_route_ids_required_for_merge_proposal"}, artifacts=[])
            out = dict(
                phase3_client.list_route_pair_merge_proposals(
                    route_ids=route_ids,
                    top_k=int(params.get("top_k") or 8),
                    max_pairs=int(params.get("max_pairs") or 40),
                    min_merge_readiness=float(params.get("min_merge_readiness") or 0.0),
                    target_service_route_id=(params.get("target_service_route_id") or p3_scope.get("service_route_id")),
                    log_event=True,
                )
                or {}
            )
            proposals = list(out.get("proposals") or [])
            top = dict(proposals[0] or {}) if proposals else {}
            summary = {
                **out,
                "legacy_non_default_stage": True,
                "top_proposal": top,
                "validator_payload": {"proposal_count": len(proposals), "top_readiness": top.get("merge_readiness_score")},
            }
            return ExecutorResult(ok=True, summary=summary, artifacts=[{"artifact_type": "phase3_merge_proposal", "payload": out}])

        def _p3_merge_apply(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            service_route_id = str(params.get("service_route_id") or (state.pipeline_scope.get("phase3") or {}).get("service_route_id") or "").strip()
            route_id = str(params.get("route_id") or _route_id(state) or "").strip()
            direction_id = params.get("direction_id")
            if service_route_id and route_id and direction_id in {0, 1}:
                out = dict(
                    phase3_client.bind_route_to_direction(
                        service_route_id=service_route_id,
                        direction_id=int(direction_id),
                        route_id=route_id,
                        geom_source=str(params.get("geom_source") or "observed"),
                    )
                    or {}
                )
            elif service_route_id:
                out = dict(
                    phase3_client.approve_service_route(
                        service_route_id=service_route_id,
                        approved_by=(params.get("operator_id") or "autopilot"),
                        notes=str(params.get("operator_notes") or "autopilot_merge_apply"),
                    )
                    or {}
                )
            else:
                return ExecutorResult(ok=False, summary={"error": "service_route_id_required_for_merge_apply"}, artifacts=[])
            return ExecutorResult(
                ok=True,
                summary={"merge_apply": out, "legacy_non_default_stage": True},
                artifacts=[{"artifact_type": "phase3_merge_apply", "payload": out}],
            )

        def _p3_catalog_sync_review(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no, params
            summary = dict(phase3_client.get_phase3_catalog_summary() or {})
            rows = list(phase3_client.list_phase3_global_catalog(limit=300, include_suppressed=True) or [])
            payload = {
                **summary,
                "catalog_row_count": int(len(rows)),
                "sample_rows": rows[:20],
                "validator_payload": {
                    "catalog_row_count": int(len(rows)),
                    "sector_count": int(summary.get("sector_count") or 0),
                    "route_family_count": int(summary.get("route_family_count") or 0),
                },
            }
            state.resume_context["phase3_catalog_summary"] = payload
            return ExecutorResult(
                ok=True,
                summary=payload,
                artifacts=[{"artifact_type": "phase3_global_catalog", "payload": payload}],
            )

        def _p3_sector_coverage_review(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no, params
            rows = list(phase3_client.list_phase3_sector_coverage(limit=300) or [])
            open_gaps = int(
                len(
                    list(
                        phase3_client.list_phase3_coverage_gaps(
                            limit=5000,
                            resolution_status="open",
                        )
                        or []
                    )
                )
            )
            payload = {
                "sector_count": int(len(rows)),
                "rows": rows,
                "open_gap_count": open_gaps,
                "manual_review_required": True,
                "validator_payload": {
                    "sector_count": int(len(rows)),
                    "open_gap_count": open_gaps,
                    "manual_review_required": True,
                },
            }
            return ExecutorResult(
                ok=True,
                summary=payload,
                artifacts=[{"artifact_type": "phase3_sector_coverage", "payload": payload}],
            )

        def _p3_gap_detection_classification(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            p3_scope = dict(state.pipeline_scope.get("phase3") or {})
            catalog_paths = list(params.get("catalog_paths") or p3_scope.get("coverage_catalog_paths") or [])
            sync_out = dict(
                phase3_client.sync_phase3_coverage_gaps(
                    catalog_paths=(catalog_paths or None),
                )
                or {}
            )
            gap_rows = list(phase3_client.list_phase3_coverage_gaps(limit=5000) or [])
            next_action_counts: Dict[str, int] = {}
            classification_counts: Dict[str, int] = {}
            for row in gap_rows:
                next_action = str(row.get("recommended_next_action") or "unspecified").strip() or "unspecified"
                next_action_counts[next_action] = int(next_action_counts.get(next_action, 0)) + 1
                cls = str(row.get("effective_classification") or row.get("classification_status") or "needs_review").strip()
                classification_counts[cls] = int(classification_counts.get(cls, 0)) + 1
            payload = {
                **sync_out,
                "gap_count": int(len(gap_rows)),
                "classification_counts": classification_counts,
                "recommended_next_action_counts": next_action_counts,
                "rows_sample": gap_rows[:20],
                "validator_payload": {
                    "gap_count": int(len(gap_rows)),
                    "open_gap_count": int(sync_out.get("open_gap_count") or 0),
                    "resolved_gap_count": int(sync_out.get("resolved_gap_count") or 0),
                    "classification_counts": classification_counts,
                },
            }
            state.resume_context["phase3_gap_detection_summary"] = payload
            return ExecutorResult(
                ok=True,
                summary=payload,
                artifacts=[{"artifact_type": "phase3_coverage_gaps", "payload": payload}],
            )

        def _p3_missing_route_export(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no
            p3_scope = dict(state.pipeline_scope.get("phase3") or {})
            sector_keys = list(params.get("sector_keys") or p3_scope.get("coverage_sector_keys") or [])
            out = dict(
                phase3_client.export_phase3_missing_route_catalogs(
                    output_dir=(params.get("output_dir") or p3_scope.get("missing_route_output_dir")),
                    sector_keys=(sector_keys or None),
                    include_resolved=bool(params.get("include_resolved", False)),
                )
                or {}
            )
            payload = {
                **out,
                "validator_payload": {
                    "file_count": int(out.get("file_count") or 0),
                    "output_dir": out.get("output_dir"),
                },
            }
            return ExecutorResult(
                ok=True,
                summary=payload,
                artifacts=[{"artifact_type": "phase3_missing_route_catalogs", "payload": out}],
            )

        def _p3_gap_resolution_queue(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del step, attempt_no, params
            rows = list(phase3_client.list_phase3_coverage_gaps(limit=5000) or [])
            retry_extract_queue: List[Dict[str, Any]] = []
            patch_extract_queue: List[Dict[str, Any]] = []
            manual_construct_queue: List[Dict[str, Any]] = []
            for row in rows:
                if str(row.get("resolution_status") or "") not in {"open", "in_progress"}:
                    continue
                action = str(row.get("recommended_next_action") or "").strip()
                record = {
                    "gap_id": row.get("gap_id"),
                    "sector_key": row.get("sector_key"),
                    "sector_label": row.get("sector_label"),
                    "route_family_hint": row.get("route_family_hint"),
                    "classification": row.get("effective_classification") or row.get("classification_status"),
                    "recommended_next_action": action,
                    "manual_priority": row.get("manual_priority"),
                    "resolution_status": row.get("resolution_status"),
                }
                if "patch" in action:
                    patch_extract_queue.append(record)
                elif "manual" in action:
                    manual_construct_queue.append(record)
                else:
                    retry_extract_queue.append(record)
            payload = {
                "retry_extract_queue": retry_extract_queue[:50],
                "patch_extract_queue": patch_extract_queue[:50],
                "manual_construct_queue": manual_construct_queue[:50],
                "manual_review_required": True,
                "validator_payload": {
                    "manual_review_required": True,
                    "open_gap_count": int(
                        sum(1 for row in rows if str(row.get("resolution_status") or "") == "open")
                    ),
                    "manual_construct_count": int(len(manual_construct_queue)),
                    "retry_extract_count": int(len(retry_extract_queue)),
                    "patch_extract_count": int(len(patch_extract_queue)),
                },
            }
            return ExecutorResult(
                ok=True,
                summary=payload,
                artifacts=[{"artifact_type": "phase3_gap_resolution_queue", "payload": payload}],
            )

        def _p3_gap_resolution_verify(
            state: RunSessionState,
            step: StepDefinition,
            attempt_no: int,
            params: Dict[str, Any],
        ) -> ExecutorResult:
            del state, step, attempt_no, params
            rows = list(phase3_client.list_phase3_coverage_gaps(limit=5000) or [])
            payload = {
                "open_gap_count": int(sum(1 for row in rows if str(row.get("resolution_status") or "") == "open")),
                "in_progress_gap_count": int(sum(1 for row in rows if str(row.get("resolution_status") or "") == "in_progress")),
                "resolved_gap_count": int(sum(1 for row in rows if str(row.get("resolution_status") or "") == "resolved")),
                "validator_payload": {
                    "open_gap_count": int(sum(1 for row in rows if str(row.get("resolution_status") or "") == "open")),
                    "in_progress_gap_count": int(sum(1 for row in rows if str(row.get("resolution_status") or "") == "in_progress")),
                    "resolved_gap_count": int(sum(1 for row in rows if str(row.get("resolution_status") or "") == "resolved")),
                },
            }
            return ExecutorResult(
                ok=True,
                summary=payload,
                artifacts=[{"artifact_type": "phase3_gap_resolution_status", "payload": payload}],
            )

        bridge.update(
            {
                "phase3_route_extract": _p3_route_extract,
                "phase3_inverse_completion": _p3_inverse_completion,
                "phase3_step20_sequence": _p3_step20,
                "phase3_reorder_proposal": _p3_reorder_proposal,
                "phase3_reorder_apply": _p3_reorder_apply,
                "phase3_step30_geometry": _p3_step30,
                "phase3_step32_stop_recovery": _p3_step32,
                "phase3_step35_rank": _p3_step35,
                "phase3_step40_approve_prepare": _p3_step40_prepare,
                "phase3_step40_approve_apply": _p3_step40_apply,
                "phase3_merge_proposal": _p3_merge_proposal,
                "phase3_merge_apply": _p3_merge_apply,
                "phase3_catalog_sync_review": _p3_catalog_sync_review,
                "phase3_sector_coverage_review": _p3_sector_coverage_review,
                "phase3_gap_detection_classification": _p3_gap_detection_classification,
                "phase3_missing_route_export": _p3_missing_route_export,
                "phase3_gap_resolution_queue": _p3_gap_resolution_queue,
                "phase3_gap_resolution_verify": _p3_gap_resolution_verify,
            }
        )

    return bridge


def build_phase_client_validator_bridge() -> Dict[str, ValidatorFn]:
    """Validator adapters based on runtime-originated `validator_payload` signals."""

    def _parse_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            try:
                return int(float(value))
            except Exception:
                return None

    def _pick_first_int(data: Dict[str, Any], keys: Sequence[str]) -> Optional[int]:
        for key in keys:
            if key in data:
                parsed = _parse_int(data.get(key))
                if parsed is not None:
                    return parsed
        return None

    def _parse_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except Exception:
            return None

    def _normalize_quality_score_100(raw_score: Optional[float]) -> Optional[float]:
        if raw_score is None:
            return None
        # Accept both legacy 0..1 and standard 0..100 score payloads.
        if 0.0 <= float(raw_score) <= 1.0:
            return float(raw_score) * 100.0
        return float(raw_score)

    def _payload_schema_errors(step_id: str, payload: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if step_id != STEP_P1_2_CHAIN:
            return errors

        bool_keys = ("normalize_ok", "features_ok", "cluster_ok", "cluster_degenerate", "resolve_ok")
        for key in bool_keys:
            if key in payload and payload.get(key) is not None and not isinstance(payload.get(key), bool):
                errors.append(f"`{key}` must be boolean when provided")

        for key in ("resolved_count", "resolved_total", "n_resolved"):
            if key in payload and payload.get(key) is not None and _parse_int(payload.get(key)) is None:
                errors.append(f"`{key}` must be integer-compatible when provided")

        resolved_count = _parse_int(payload.get("resolved_count")) if "resolved_count" in payload else None
        resolved_total = _parse_int(payload.get("resolved_total")) if "resolved_total" in payload else None
        if resolved_count is not None and resolved_total is not None and resolved_count != resolved_total:
            errors.append("`resolved_count` and `resolved_total` disagree")

        return errors

    def _for_step(
        state: RunSessionState,
        step: StepDefinition,
        attempt_no: int,
        exec_result: ExecutorResult,
    ) -> ValidatorResult:
        del state
        summary = dict(exec_result.summary or {})
        payload = dict(summary.get("validator_payload") or {})
        schema_errors = _payload_schema_errors(step.step_id, payload)
        if schema_errors:
            return ValidatorResult(
                status="blocked",
                gate_passed=False,
                block_reason_code=BlockReasonCode.VALIDATOR_PAYLOAD_CONTRACT_MISMATCH,
                summary="Validator payload schema mismatch detected.",
                evidence={
                    "attempt_no": int(attempt_no),
                    "step_id": step.step_id,
                    "schema_errors": list(schema_errors),
                    "validator_payload": payload,
                    "summary": summary,
                },
                recommended_action="fix_executor_validator_payload_contract",
            )
        if bool(payload.get("gate_bypass_attempted")) or bool(summary.get("gate_bypass_attempted")):
            return ValidatorResult(
                status="blocked",
                gate_passed=False,
                block_reason_code=BlockReasonCode.GATE_BYPASS_ATTEMPT,
                summary="Gate bypass attempt detected.",
                evidence={"attempt_no": int(attempt_no), "summary": summary, "validator_payload": payload},
                recommended_action="review_gate_integrity",
                gate_bypass_attempted=True,
            )
        if not exec_result.ok:
            return ValidatorResult(
                status="failed",
                gate_passed=False,
                block_reason_code=BlockReasonCode.APPROVAL_REQUIRED if step.automation_level == AutomationLevel.APPROVAL_REQUIRED else None,
                summary=str(summary.get("error") or f"{step.step_id} executor failed"),
                evidence={"attempt_no": int(attempt_no), "summary": summary},
                recommended_action="inspect_runtime_error",
            )

        if step.step_id == STEP_P1_1_EXTRACT:
            candidate_count = int(payload.get("candidate_count") or summary.get("candidate_count") or 0)
            quality = payload.get("quality_score")
            if quality is not None:
                try:
                    quality = float(quality)
                except Exception:
                    quality = None
            attempts_summary = _as_dict(
                payload.get("extractor_attempt_history_summary")
                or payload.get("extractor_attempts_summary")
                or summary.get("extraction_attempt_history_summary")
                or summary.get("extraction_attempts_summary")
            )
            if not attempts_summary:
                attempts_summary = _summarize_attempt_records(_as_list(summary.get("extraction_attempt_records")))
            retry_diversified_raw = payload.get("retry_diversified")
            if retry_diversified_raw is None:
                retry_diversified_raw = summary.get("retry_diversified")
            retry_diversified = _handoff_to_bool(retry_diversified_raw)
            if retry_diversified is None:
                retry_diversified = bool(attempts_summary.get("diversified_attempts"))
            spatial_interpretation = _as_dict(
                payload.get("spatial_interpretation")
                or summary.get("spatial_interpretation")
            )
            target_option_received = bool(
                spatial_interpretation.get("target_option_received")
                or str(spatial_interpretation.get("target_option_text") or "").strip()
            )
            spatial_interpretation_status = str(
                spatial_interpretation.get("spatial_interpretation_status")
                or ""
            ).strip()
            retry_changed_spatial_plan = _handoff_to_bool(
                spatial_interpretation.get("retry_changed_spatial_plan")
            )
            same_spatial_plan_retry_count = _handoff_to_int(
                spatial_interpretation.get("same_spatial_plan_retry_count")
            ) or 0
            recommended_action = str(payload.get("recommended_action") or "").strip()
            if not recommended_action:
                if (
                    target_option_received
                    and int(attempt_no or 0) >= 2
                    and retry_changed_spatial_plan is False
                    and int(same_spatial_plan_retry_count or 0) >= 1
                ):
                    recommended_action = "pause_and_patch_extractor_spatial_interpretation"
                elif target_option_received and spatial_interpretation_status in {"failed", "fallback_default_bbox", "invalid_explicit_bbox"}:
                    recommended_action = "inspect_spatial_interpretation_before_retry"
                else:
                    recommended_action = "retry_with_bbox_or_template" if candidate_count <= 0 else "continue_or_review"
            extraction_outcome = str(
                payload.get("extraction_outcome_classification")
                or summary.get("extraction_outcome_classification")
                or ("empty" if candidate_count <= 0 else "success")
            ).strip()
            evidence = {
                "candidate_count": candidate_count,
                "quality_score": quality,
                "extractor_attempts_summary": attempts_summary,
                "extractor_attempt_history_summary": attempts_summary,
                "retry_diversified": bool(retry_diversified),
                "recommended_action": recommended_action,
                "extraction_outcome_classification": extraction_outcome,
                "spatial_interpretation": spatial_interpretation,
                "target_option_received": bool(target_option_received),
                "spatial_interpretation_status": (spatial_interpretation_status or None),
                "retry_changed_spatial_plan": retry_changed_spatial_plan,
                "same_spatial_plan_retry_count": int(same_spatial_plan_retry_count),
                "runtime_bbox_used": _as_dict(spatial_interpretation.get("runtime_bbox_used")) or None,
            }
            if candidate_count <= 0:
                return ValidatorResult(status="blocked", gate_passed=False, block_reason_code=BlockReasonCode.EXTRACTION_EMPTY, summary="Extraction returned zero candidates.", evidence=evidence, recommended_action=recommended_action)
            if quality is not None and quality < 0.35:
                return ValidatorResult(status="blocked", gate_passed=False, block_reason_code=BlockReasonCode.EXTRACTION_LOW_COVERAGE, summary=f"Extraction quality low ({quality:.2f}).", evidence=evidence, recommended_action=recommended_action)
            if quality is not None and quality < 0.55:
                return ValidatorResult(status="warning", gate_passed=True, passable_warning=True, warnings=["extraction_quality_low"], anomalies=["coverage_degradation"], summary=f"Extraction quality warning ({quality:.2f}).", evidence=evidence, recommended_action=recommended_action)
            return ValidatorResult(status="pass", gate_passed=True, summary="Extraction passed.", evidence=evidence)

        if step.step_id == STEP_P1_2_CHAIN:
            normalize_ok = bool(payload.get("normalize_ok", True))
            features_ok = bool(payload.get("features_ok", True))
            cluster_ok = bool(payload.get("cluster_ok", True))
            cluster_degenerate = bool(payload.get("cluster_degenerate", False))
            resolve_summary = dict(summary.get("resolve") or {})
            resolve_ok = bool(payload.get("resolve_ok", resolve_summary.get("ok", True)))
            resolved_count_payload = _pick_first_int(payload, ("resolved_count", "resolved_total", "n_resolved"))
            resolved_count_exec = _pick_first_int(resolve_summary, ("resolved_count", "resolved_total", "n_resolved"))
            resolved_count = int(
                resolved_count_payload
                if resolved_count_payload is not None
                else (resolved_count_exec if resolved_count_exec is not None else 0)
            )
            contract_errors: List[str] = []
            if (
                resolve_ok
                and resolved_count <= 0
                and resolved_count_exec is not None
                and resolved_count_exec > 0
            ):
                contract_errors.append(
                    "resolve_ok=true and executor resolve count > 0, but validator resolved_count <= 0"
                )
            payload_count = _parse_int(payload.get("resolved_count")) if "resolved_count" in payload else None
            payload_total = _parse_int(payload.get("resolved_total")) if "resolved_total" in payload else None
            if payload_count is not None and payload_total is not None and payload_count != payload_total:
                contract_errors.append("validator payload count aliases disagree")
            evidence = {
                "normalize_ok": normalize_ok,
                "features_ok": features_ok,
                "cluster_ok": cluster_ok,
                "cluster_degenerate": cluster_degenerate,
                "resolve_ok": resolve_ok,
                "resolved_count": resolved_count,
                "resolved_count_payload": resolved_count_payload,
                "resolved_count_executor": resolved_count_exec,
            }
            if contract_errors:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.VALIDATOR_PAYLOAD_CONTRACT_MISMATCH,
                    summary="Resolve validator payload contract mismatch: execution and payload counts disagree.",
                    evidence={**evidence, "contract_errors": contract_errors, "validator_payload": payload, "resolve_summary": resolve_summary},
                    recommended_action="fix_executor_validator_payload_contract",
                )
            if not normalize_ok:
                return ValidatorResult(status="failed", gate_passed=False, block_reason_code=BlockReasonCode.NORMALIZE_FAILED, summary="Normalize failed.", evidence=evidence, recommended_action="inspect_normalization_inputs")
            if not features_ok:
                return ValidatorResult(status="failed", gate_passed=False, block_reason_code=BlockReasonCode.FEATURES_INVALID, summary="Features invalid.", evidence=evidence, recommended_action="fix_feature_generation")
            if not cluster_ok or cluster_degenerate:
                return ValidatorResult(status="blocked", gate_passed=False, block_reason_code=BlockReasonCode.CLUSTERING_DEGENERATE, summary="Clustering degenerate.", evidence=evidence, recommended_action="adjust_cluster_parameters")
            if not resolve_ok or resolved_count <= 0:
                return ValidatorResult(status="blocked", gate_passed=False, block_reason_code=BlockReasonCode.RESOLVE_ZERO_RESULTS, summary="Resolve produced zero results.", evidence=evidence, recommended_action="inspect_resolve_rules")
            return ValidatorResult(status="pass", gate_passed=True, summary="P1.2 chain passed.", evidence=evidence)

        if step.step_id == STEP_P1_3A_WORKSPACE:
            pending = int(payload.get("pending_request_count") or summary.get("pending_request_count") or 0)
            return ValidatorResult(
                status="pass",
                gate_passed=True,
                summary="Workspace review assistance prepared.",
                evidence={"pending_request_count": pending, "manual_review_required": True},
            )

        if step.step_id == STEP_P1_3B_NEW_NODES:
            pending = int(payload.get("pending_requests") or summary.get("pending_requests") or 0)
            create_n = int(payload.get("create_suggestions") or len(summary.get("create_node_suggestions") or []))
            ambiguity_n = int(payload.get("ambiguity_suggestions") or len(summary.get("ambiguity_candidates") or []))
            return ValidatorResult(
                status="pass",
                gate_passed=True,
                summary="New nodes prefill prepared for operator resolution.",
                evidence={
                    "pending_requests": pending,
                    "create_suggestions": create_n,
                    "ambiguity_suggestions": ambiguity_n,
                    "manual_review_required": True,
                },
            )

        if step.step_id == STEP_P1_4_PROMOTE:
            ready = bool(payload.get("ready_for_approval", True))
            dry_run = dict(summary.get("promote_dry_run") or {})
            resolved_n = int(payload.get("n_resolved") or dry_run.get("n_resolved") or 0)
            status_code = str(payload.get("promote_status") or dry_run.get("status") or "").strip() or None
            legacy_status = str(payload.get("legacy_status") or dry_run.get("legacy_status") or "").strip() or None
            promote_precondition = str(
                payload.get("promote_precondition") or dry_run.get("promote_precondition") or ""
            ).strip() or None
            workspace_state_summary = dict(
                payload.get("workspace_state_summary")
                or summary.get("workspace_state_summary")
                or dry_run.get("workspace_state_summary")
                or {}
            )
            approved_count = int(
                workspace_state_summary.get("approved_count")
                or payload.get("n_approved")
                or dry_run.get("n_approved")
                or 0
            )
            resolved_total = int(workspace_state_summary.get("resolved_total") or 0)
            pending_review_count = _handoff_to_int(workspace_state_summary.get("pending_review_count"))
            evidence = {
                "ready_for_approval": ready,
                "n_resolved": resolved_n,
                "promote_status": status_code,
                "legacy_status": legacy_status,
                "promote_precondition": promote_precondition,
                "workspace_state_summary": workspace_state_summary,
                "approved_count": approved_count,
                "resolved_total": resolved_total,
                "pending_review_count": pending_review_count,
            }
            diagnostics_hint = str(
                payload.get("diagnostics_hint")
                or dry_run.get("diagnostics_hint")
                or "Promote precheck returned zero although workspace may contain resolved rows."
            ).strip()
            if diagnostics_hint:
                evidence["diagnostics_hint"] = diagnostics_hint
            if status_code == "node_set_missing":
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.PROMOTE_NODE_SET_MISSING,
                    summary="Promote dry-run node set lookup returned missing.",
                    evidence=evidence,
                    recommended_action="inspect_promote_lookup",
                )
            if status_code == "no_approved_nodes" or (
                resolved_total > 0 and approved_count <= 0 and int(pending_review_count or 0) > 0
            ):
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.NO_APPROVED_NODES_FOR_PROMOTE,
                    summary="Promote cannot proceed because no nodes have been approved yet.",
                    evidence=evidence,
                    recommended_action="approve_nodes_before_promote",
                )
            if not ready:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.APPROVAL_REQUIRED,
                    summary="Promote precheck not ready for approval.",
                    evidence=evidence,
                    recommended_action="inspect_promote_precheck",
                )
            if resolved_n <= 0:
                if approved_count <= 0:
                    recommended_action = "approve_nodes_before_promote"
                    block_summary = "Promote dry-run has zero approved nodes."
                    block_code = BlockReasonCode.NO_APPROVED_NODES_FOR_PROMOTE
                else:
                    recommended_action = "inspect_promote_lookup"
                    if status_code == "query_filtered_empty":
                        block_summary = "Promote dry-run returned zero after filters/joins removed approved rows."
                    else:
                        block_summary = "Promote dry-run returned zero after approved nodes exist."
                    block_code = BlockReasonCode.PROMOTE_LOOKUP_EMPTY
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=block_code,
                    summary=block_summary,
                    evidence=evidence,
                    recommended_action=recommended_action,
                )
            return ValidatorResult(status="pass", gate_passed=True, summary="Promote precheck passed.", evidence=evidence)

        if step.step_id == STEP_P2_1_SEMANTIC:
            block_reason_raw = str(payload.get("block_reason_code") or "").strip()
            if block_reason_raw:
                try:
                    block_code = BlockReasonCode(block_reason_raw)
                except Exception:
                    block_code = None
                if block_code in {
                    BlockReasonCode.PHASE1_PROMOTE_NOT_COMPLETED,
                    BlockReasonCode.PLACE_SET_ID_NOT_FOUND,
                    BlockReasonCode.PLACE_SET_EMPTY_OR_MISSING,
                }:
                    return ValidatorResult(
                        status="blocked",
                        gate_passed=False,
                        block_reason_code=block_code,
                        summary=str(payload.get("message") or payload.get("summary") or str(block_code.value).replace("_", " ").title()),
                        evidence={
                            "runtime_context": _as_dict(payload.get("runtime_context") or summary.get("runtime_context")),
                            "phase1_to_phase2_handoff": _as_dict(
                                payload.get("phase1_to_phase2_handoff")
                                or summary.get(PHASE1_TO_PHASE2_HANDOFF_ARTIFACT)
                            ),
                            "validator_payload": payload,
                        },
                        recommended_action=(
                            str(payload.get("recommended_action") or "").strip()
                            or {
                                BlockReasonCode.PHASE1_PROMOTE_NOT_COMPLETED: "complete_phase1_promote",
                                BlockReasonCode.PLACE_SET_ID_NOT_FOUND: "verify_phase2_candidate_build_output",
                                BlockReasonCode.PLACE_SET_EMPTY_OR_MISSING: "re_run_phase2_candidate_build",
                            }.get(block_code, "review_and_resolve")
                        ),
                    )
            failed_stage = payload.get("failed_stage")
            if failed_stage:
                return ValidatorResult(status="failed", gate_passed=False, block_reason_code=BlockReasonCode.SEMANTIC_PIPELINE_FAILED, summary=f"Semantic pipeline failed at {failed_stage}.", evidence={"failed_stage": failed_stage}, recommended_action="rerun_semantics_and_check_db")
            return ValidatorResult(status="pass", gate_passed=True, summary="Semantic pipeline passed.", evidence=payload)

        if step.step_id == STEP_P2_2_WORKSPACE:
            return ValidatorResult(
                status="pass",
                gate_passed=True,
                summary="Semantics workspace assistance prepared.",
                evidence={"manual_review_required": True, **dict(payload or {})},
            )

        if step.step_id == STEP_P2_3_CLEANUP:
            preview_ok = bool(payload.get("preview_ok", True))
            delete_candidates = int(payload.get("delete_candidates") or 0)
            evidence = {"preview_ok": preview_ok, "delete_candidates": delete_candidates}
            if not preview_ok:
                return ValidatorResult(
                    status="failed",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.SEMANTIC_PIPELINE_FAILED,
                    summary="Cleanup preview failed.",
                    evidence=evidence,
                    recommended_action="inspect_cleanup_preview",
                )
            if delete_candidates > 0:
                return ValidatorResult(
                    status="warning",
                    gate_passed=True,
                    passable_warning=True,
                    warnings=["cleanup_has_delete_candidates"],
                    anomalies=[],
                    summary=f"Cleanup preview includes {delete_candidates} delete candidates.",
                    evidence=evidence,
                    recommended_action="review_cleanup_risk_before_approval",
                )
            return ValidatorResult(status="pass", gate_passed=True, summary="Cleanup preview passed.", evidence=evidence)

        if step.step_id == STEP_P3_1_EXTRACT:
            phase3_bundle = _phase3_route_bundle_evidence(
                summary=summary,
                payload=payload,
                validator_evidence={},
            )
            route_id = str(phase3_bundle.get("route_id") or "").strip()
            prior_stop_count = int(phase3_bundle.get("actual_prior_stop_count") or 0)
            attempts_summary = _as_dict(
                payload.get("extractor_attempt_history_summary")
                or payload.get("extractor_attempts_summary")
                or summary.get("extraction_attempt_history_summary")
                or summary.get("extraction_attempts_summary")
            )
            if not attempts_summary:
                attempts_summary = _summarize_attempt_records(_as_list(summary.get("extraction_attempt_records")))
            retry_diversified_raw = payload.get("retry_diversified")
            if retry_diversified_raw is None:
                retry_diversified_raw = summary.get("retry_diversified")
            retry_diversified = _handoff_to_bool(retry_diversified_raw)
            if retry_diversified is None:
                retry_diversified = bool(attempts_summary.get("diversified_attempts"))
            fallback_profile = payload.get("fallback_profile_used")
            if fallback_profile is None:
                fallback_profile = summary.get("fallback_profile_used")
            fetch_status_classification = str(
                phase3_bundle.get("fetch_status_classification")
                or payload.get("fetch_status_classification")
                or summary.get("fetch_status_classification")
                or ""
            ).strip()
            recommended_action = str(payload.get("recommended_action") or "").strip()
            if not recommended_action:
                if fetch_status_classification in P3_PARTIAL_FETCH_CLASSIFICATIONS:
                    recommended_action = "inspect_selected_relation_fetch_and_validator_mapping"
                else:
                    recommended_action = "retry_route_extraction_or_adjust_scope" if prior_stop_count <= 0 else "continue_or_review"
            extraction_outcome = str(
                payload.get("extraction_outcome_classification")
                or summary.get("extraction_outcome_classification")
                or (
                    fetch_status_classification
                    if fetch_status_classification in P3_PARTIAL_FETCH_CLASSIFICATIONS
                    else ("empty" if prior_stop_count <= 0 else "success")
                )
            ).strip()
            evidence = {
                "route_id": route_id,
                "prior_stop_count": prior_stop_count,
                "prior_stop_evidence_count": int(phase3_bundle.get("prior_stop_evidence_count") or 0),
                "discover_candidate_count": payload.get("discover_candidate_count"),
                "discover_signal_strength": payload.get("discover_signal_strength"),
                "discover_quality_flags": list(payload.get("discover_quality_flags") or []),
                "candidate_universe_summary": _as_dict(phase3_bundle.get("candidate_universe_summary")),
                "selection_summary": _as_dict(phase3_bundle.get("selection_summary")),
                "selected_relation_summary": _as_dict(phase3_bundle.get("selected_relation_summary")),
                "selected_relation_present": bool(phase3_bundle.get("selected_relation_present")),
                "selected_osm_relation_id": phase3_bundle.get("selected_osm_relation_id"),
                "selected_relation_stop_prior_count": int(phase3_bundle.get("selected_relation_stop_prior_count") or 0),
                "top_stop_prior_count": int(phase3_bundle.get("top_stop_prior_count") or 0),
                "selection_confidence": phase3_bundle.get("selection_confidence"),
                "selection_reason_codes": list(phase3_bundle.get("selection_reason_codes") or []),
                "strong_bundle_evidence": bool(phase3_bundle.get("strong_bundle_evidence")),
                "bundle_success_classification": phase3_bundle.get("bundle_success_classification"),
                "bundle_contradiction_codes": list(phase3_bundle.get("contradiction_codes") or []),
                "fetch_status_classification": fetch_status_classification or None,
                "fetch_observability_gap": bool(phase3_bundle.get("fetch_observability_gap")),
                "query_strategy": (
                    str(payload.get("query_strategy") or summary.get("query_strategy") or "").strip()
                    or None
                ),
                "extractor_attempts_summary": attempts_summary,
                "extractor_attempt_history_summary": attempts_summary,
                "retry_diversified": bool(retry_diversified),
                "fallback_profile_used": fallback_profile,
                "recommended_action": recommended_action,
                "extraction_outcome_classification": extraction_outcome,
            }
            if not route_id:
                return ValidatorResult(
                    status="failed",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.REPEATED_EXTRACTOR_FAILURE,
                    summary="Route extraction did not produce route_id.",
                    evidence=evidence,
                    recommended_action=recommended_action,
                )
            if (
                bool(phase3_bundle.get("strong_bundle_evidence"))
                and prior_stop_count <= 0
            ):
                summary_by_fetch_state = {
                    "fetch_partial_after_valid_selection": "Route selected, but fetch only partially enriched the selected relation.",
                    "selected_relation_not_fully_enriched": "Route selected, but the selected relation is not fully enriched yet.",
                    "selected_relation_fetch_inconsistent": "Route selected, but fetch evidence is inconsistent with the selected relation bundle.",
                    "downstream_fetch_observability_gap": "Route selected, but downstream fetch observability is incomplete.",
                }
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.VALIDATOR_PAYLOAD_CONTRACT_MISMATCH,
                    summary=summary_by_fetch_state.get(
                        fetch_status_classification,
                        "Route selected, but fetch evidence is incomplete or inconsistent.",
                    ),
                    evidence=evidence,
                    recommended_action=recommended_action,
                    anomalies=list(
                        dict.fromkeys(
                            ["phase3_bundle_fetch_contradiction"]
                            + list(phase3_bundle.get("contradiction_codes") or [])
                        )
                    )[:8],
                )
            if prior_stop_count <= 0:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
                    summary="Route extraction produced zero prior stops.",
                    evidence=evidence,
                    recommended_action=recommended_action,
                )
            if prior_stop_count < 3:
                return ValidatorResult(
                    status="warning",
                    gate_passed=True,
                    passable_warning=True,
                    warnings=["route_prior_stops_low"],
                    anomalies=["route_evidence_low"],
                    summary=f"Route extraction low evidence ({prior_stop_count} stops).",
                    evidence=evidence,
                    recommended_action=recommended_action,
                )
            return ValidatorResult(status="pass", gate_passed=True, summary="Route extraction passed.", evidence=evidence)

        if step.step_id == STEP_P3_15_INVERSE:
            evidence = {
                "route_id": payload.get("route_id") or summary.get("route_id"),
                "service_route_id": payload.get("service_route_id") or summary.get("service_route_id"),
                "direction_id": payload.get("direction_id") if payload.get("direction_id") in (0, 1) else summary.get("direction_id"),
                "gate_code": str(payload.get("gate_code") or summary.get("gate_code") or "").strip() or None,
                "direction_ready": bool(payload.get("direction_ready")),
                "inverse_status": payload.get("inverse_status") or summary.get("inverse_status"),
                "search_status": payload.get("search_status") or summary.get("search_status"),
                "blocker_codes": list(payload.get("blocker_codes") or summary.get("blocker_codes") or []),
                "blocker_messages": list(payload.get("blocker_messages") or summary.get("blocker_messages") or []),
                "suggested_next_action": payload.get("suggested_next_action") or summary.get("suggested_next_action"),
                "targeted_inverse_search_attempted": bool(
                    payload.get("targeted_inverse_search_attempted")
                    if payload.get("targeted_inverse_search_attempted") is not None
                    else summary.get("targeted_inverse_search_attempted")
                ),
                "targeted_inverse_search_ran": bool(
                    payload.get("targeted_inverse_search_ran")
                    if payload.get("targeted_inverse_search_ran") is not None
                    else summary.get("targeted_inverse_search_ran")
                ),
                "targeted_inverse_search_status": (
                    payload.get("targeted_inverse_search_status")
                    or summary.get("targeted_inverse_search_status")
                ),
            }
            if not bool(payload.get("gate_passed")):
                blocker_messages = list(evidence.get("blocker_messages") or [])
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.PHASE3_INVERSE_COMPLETION_BLOCKING,
                    summary=(
                        blocker_messages[0]
                        if blocker_messages
                        else "Inverse completion blocked Phase 3 progression before Step 20."
                    ),
                    evidence=evidence,
                    recommended_action=str(evidence.get("suggested_next_action") or "open_step15_inverse"),
                )
            return ValidatorResult(
                status="pass",
                gate_passed=True,
                summary="Inverse completion passed and Step 20 may proceed.",
                evidence=evidence,
            )

        if step.step_id == STEP_P3_2_STEP20:
            unmatched = int(payload.get("unmatched_count") or summary.get("unmatched_count") or 0)
            ambiguous = int(payload.get("ambiguous_count") or summary.get("ambiguous_count") or 0)
            gate = payload.get("sequence_gate_pass")
            quality_raw = _parse_float(payload.get("sequence_quality_score"))
            quality_score = _normalize_quality_score_100(quality_raw)
            warning_threshold = _parse_float(payload.get("sequence_quality_warning_threshold"))
            if warning_threshold is None:
                warning_threshold = 70.0
            warning_tags = list(payload.get("sequence_warning_tags") or [])
            warning_subtypes = dict(payload.get("sequence_warning_subtypes") or {})
            evidence = {
                "unmatched_count": unmatched,
                "ambiguous_count": ambiguous,
                "sequence_gate_pass": gate,
                "sequence_quality_score": quality_score,
                "sequence_quality_warning_threshold": warning_threshold,
                "sequence_warning_tags": warning_tags,
                "sequence_warning_subtypes": warning_subtypes,
                "sequence_diagnostic_profile_version": payload.get("sequence_diagnostic_profile_version"),
                "step20_dominant_cause": payload.get("step20_dominant_cause"),
                "step20_dominant_cause_confidence": payload.get("step20_dominant_cause_confidence"),
                "step20_dominant_cause_reason": payload.get("step20_dominant_cause_reason"),
                "step20_triage_route": payload.get("step20_triage_route"),
                "reorder_recommended": payload.get("reorder_recommended"),
                "reorder_confidence": payload.get("reorder_confidence"),
            }
            if unmatched > 0:
                return ValidatorResult(status="blocked", gate_passed=False, block_reason_code=BlockReasonCode.STEP20_UNMATCHED_BLOCKING, summary=f"Step20 blocked: {unmatched} unmatched stops.", evidence=evidence, recommended_action="divert_to_phase1_new_nodes")
            if ambiguous > 0:
                return ValidatorResult(status="blocked", gate_passed=False, block_reason_code=BlockReasonCode.STEP20_AMBIGUOUS_BLOCKING, summary=f"Step20 blocked: {ambiguous} ambiguous stops.", evidence=evidence, recommended_action="divert_to_phase1_new_nodes")
            if gate is False:
                return ValidatorResult(status="blocked", gate_passed=False, block_reason_code=BlockReasonCode.STEP20_SEQUENCE_QUALITY_LOW, summary="Step20 gate failed.", evidence=evidence, recommended_action="review_sequence_quality")
            anomalies: List[str] = []
            if warning_subtypes.get("detector_threshold_noise_candidate"):
                anomalies.append("detector_threshold_noise_candidate")
            if quality_score is not None and quality_score < float(warning_threshold):
                anomalies.append("sequence_quality_low")
                return ValidatorResult(
                    status="warning",
                    gate_passed=True,
                    passable_warning=True,
                    warnings=["sequence_quality_low"],
                    anomalies=list(dict.fromkeys(anomalies)),
                    summary=f"Step20 warning: quality low ({quality_score:.2f}/100).",
                    evidence=evidence,
                    recommended_action="operator_review_optional",
                )
            if warning_subtypes.get("detector_threshold_noise_candidate"):
                return ValidatorResult(
                    status="warning",
                    gate_passed=True,
                    passable_warning=True,
                    warnings=["sequence_threshold_noise_candidate"],
                    anomalies=list(dict.fromkeys(anomalies)),
                    summary="Step20 warning: detector threshold noise candidate.",
                    evidence=evidence,
                    recommended_action="review_step20_thresholds",
                )
            return ValidatorResult(status="pass", gate_passed=True, summary="Step20 gate passed.", evidence=evidence)

        if step.step_id == STEP_P3_3_REORDER:
            proposal = dict(payload.get("proposal") or summary.get("reorder_proposal") or {})
            shortlist = list(
                payload.get("candidate_shortlist")
                or proposal.get("candidate_shortlist")
                or summary.get("candidate_shortlist")
                or []
            )
            recommended_candidate_id = (
                str(
                    payload.get("recommended_candidate_id")
                    or proposal.get("recommended_candidate_id")
                    or summary.get("recommended_stop_sequence_candidate_id")
                    or ""
                ).strip()
                or None
            )
            approved_candidate_id = (
                str(
                    payload.get("approved_candidate_id")
                    or proposal.get("approved_candidate_id")
                    or summary.get("approved_stop_sequence_candidate_id")
                    or ""
                ).strip()
                or None
            )
            ready = bool(payload.get("proposal_ready", False) or proposal)
            operator_approval_required = bool(
                payload.get("operator_approval_required")
                if payload.get("operator_approval_required") is not None
                else proposal.get("operator_approval_required")
            )
            needs_reorder = bool(
                payload.get("needs_reorder", False)
                or proposal.get("needs_reorder")
                or operator_approval_required
            )
            sequence_stabilized = bool(
                payload.get("sequence_stabilized")
                if payload.get("sequence_stabilized") is not None
                else proposal.get("sequence_stabilized")
            )
            evidence = {
                "proposal_ready": ready,
                "needs_reorder": needs_reorder,
                "operator_approval_required": operator_approval_required,
                "proposal": proposal,
                "candidate_shortlist": shortlist,
                "recommended_candidate_id": recommended_candidate_id,
                "approved_candidate_id": approved_candidate_id,
                "sequence_stabilized": sequence_stabilized,
                "variant_pressure_detected": bool(
                    payload.get("variant_pressure_detected")
                    if payload.get("variant_pressure_detected") is not None
                    else proposal.get("variant_pressure_detected")
                ),
                "variant_state": str(
                    payload.get("variant_state")
                    if payload.get("variant_state") is not None
                    else proposal.get("variant_state")
                    or "no_variant_issue"
                ),
                "variant_groups": list(
                    payload.get("variant_groups")
                    or proposal.get("variant_groups")
                    or []
                ),
                "recommended_variant_group_key": (
                    payload.get("recommended_variant_group_key")
                    or proposal.get("recommended_variant_group_key")
                ),
                "approved_variant_group_key": (
                    payload.get("approved_variant_group_key")
                    or proposal.get("approved_variant_group_key")
                ),
                "recommended_candidates_by_variant": list(
                    payload.get("recommended_candidates_by_variant")
                    or proposal.get("recommended_candidates_by_variant")
                    or []
                ),
                "direction_stable": bool(
                    payload.get("direction_stable")
                    if payload.get("direction_stable") is not None
                    else proposal.get("direction_stable")
                ),
            }
            if not ready:
                return ValidatorResult(
                    status="failed",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.STEP20_REORDER_SUGGESTED_BLOCKING,
                    summary="Reorder proposal is not ready.",
                    evidence=evidence,
                    recommended_action="recompute_reorder_proposal",
                )
            if not shortlist and not recommended_candidate_id and not approved_candidate_id:
                return ValidatorResult(
                    status="failed",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.STEP20_REORDER_SUGGESTED_BLOCKING,
                    summary="Canonical sequence proposal is missing a candidate shortlist.",
                    evidence=evidence,
                    recommended_action="rerun_step20_sequence_resolution",
                )
            if not needs_reorder and not operator_approval_required:
                return ValidatorResult(
                    status="warning",
                    gate_passed=True,
                    passable_warning=True,
                    warnings=["reorder_not_needed"],
                    anomalies=[],
                    summary="Canonical sequence proposal is already stabilized.",
                    evidence=evidence,
                    recommended_action="review_canonical_sequence_status",
                )
            return ValidatorResult(
                status="pass",
                gate_passed=True,
                summary="Canonical sequence proposal ready for operator approval.",
                evidence=evidence,
            )

        if step.step_id == STEP_P3_4_STEP30:
            gate = dict(payload.get("step30_gate") or summary.get("step30_gate") or {})
            blocking_reasons = [str(x).strip() for x in list(gate.get("blocking_reasons") or []) if str(x).strip()]
            evidence = {
                "candidate_count": int(payload.get("candidate_count") or summary.get("n_candidates") or 0),
                "geometry_quality": None,
                "step30_gate": gate,
                "blocking_reasons": blocking_reasons,
                "approved_stop_sequence_candidate_id": (
                    payload.get("approved_stop_sequence_candidate_id")
                    or gate.get("approved_stop_sequence_candidate_id")
                ),
            }
            if gate:
                evidence.update(
                    {
                        "matching_complete": bool(
                            (gate.get("matched_count") or 0) == (gate.get("total_count") or 0)
                            and int(gate.get("unmatched_count") or 0) == 0
                            and int(gate.get("ambiguous_count") or 0) == 0
                        ),
                        "sequence_stabilized": bool(gate.get("sequence_stabilized")),
                        "variant_pressure_detected": bool(gate.get("variant_pressure_detected")),
                        "variant_state": gate.get("variant_state"),
                        "direction_stable": bool(gate.get("direction_stable")),
                    }
                )
            if not bool(gate.get("can_run_step30", True)):
                recommended_action = "approve_canonical_sequence_before_geometry"
                if "matching_incomplete" in blocking_reasons:
                    recommended_action = "return_to_step20_matching_before_geometry"
                elif "direction_instability" in blocking_reasons:
                    recommended_action = "stabilize_direction_before_geometry"
                elif any(reason.startswith("variant_pressure") or reason.startswith("multi_variant") for reason in blocking_reasons):
                    recommended_action = "review_variant_pressure_before_geometry"
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.STEP30_SEQUENCE_RESOLUTION_BLOCKING,
                    summary="Step30 blocked until canonical sequence resolution is complete.",
                    evidence=evidence,
                    recommended_action=recommended_action,
                )
            n = int(payload.get("candidate_count") or summary.get("n_candidates") or 0)
            quality = payload.get("geometry_quality")
            if quality is not None:
                try:
                    quality = float(quality)
                except Exception:
                    quality = None
            evidence["candidate_count"] = n
            evidence["geometry_quality"] = quality
            if n <= 0:
                return ValidatorResult(status="failed", gate_passed=False, block_reason_code=BlockReasonCode.GEOMETRY_FAILED, summary="Geometry produced zero candidates.", evidence=evidence, recommended_action="inspect_geometry_candidates")
            if quality is not None and quality < 0.30:
                return ValidatorResult(status="blocked", gate_passed=False, block_reason_code=BlockReasonCode.GEOMETRY_QUALITY_CRITICAL_LOW, summary=f"Geometry quality critically low ({quality:.2f}).", evidence=evidence, recommended_action="review_geometry_quality")
            return ValidatorResult(status="pass", gate_passed=True, summary="Geometry validation passed.", evidence=evidence)

        if step.step_id == STEP_P3_4_STEP35:
            ranked = int(payload.get("ranked_count") or summary.get("ranked_count") or 0)
            if ranked <= 0:
                return ValidatorResult(status="failed", gate_passed=False, block_reason_code=BlockReasonCode.RANKING_FAILED, summary="Ranking produced zero candidates.", evidence={"ranked_count": ranked}, recommended_action="inspect_ranking_pipeline")
            return ValidatorResult(status="pass", gate_passed=True, summary="Ranking validation passed.", evidence={"ranked_count": ranked})

        if step.step_id == STEP_P3_4_STEP40:
            ranked_count = int(payload.get("ranked_count") or summary.get("ranked_count") or 0)
            top_score = summary.get("top_candidate", {}).get("score")
            evidence = {"ranked_count": ranked_count, "top_candidate_score": top_score}
            if ranked_count <= 0:
                return ValidatorResult(
                    status="failed",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.RANKING_FAILED,
                    summary="Step40 approval precheck found zero ranked candidates.",
                    evidence=evidence,
                    recommended_action="rerun_step35_rank",
                )
            return ValidatorResult(status="pass", gate_passed=True, summary="Step40 approval precheck passed.", evidence=evidence)

        if step.step_id == STEP_P3_5_MERGE:
            proposal_count = int(payload.get("proposal_count") or summary.get("proposal_count") or len(summary.get("proposals") or []))
            top_readiness = payload.get("top_readiness") or summary.get("top_proposal", {}).get("merge_readiness_score")
            evidence = {"proposal_count": proposal_count, "top_readiness": top_readiness}
            if proposal_count <= 0:
                return ValidatorResult(
                    status="failed",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.MERGE_BIND_APPROVAL_REQUIRED,
                    summary="Legacy merge compatibility stage produced no candidate route pairs.",
                    evidence=evidence,
                    recommended_action="collect_more_merge_evidence",
                )
            return ValidatorResult(
                status="pass",
                gate_passed=True,
                summary="Legacy merge compatibility proposal ready for manual approval.",
                evidence=evidence,
            )

        if step.step_id == STEP_P3_6_CATALOG:
            catalog_row_count = int(payload.get("catalog_row_count") or summary.get("catalog_row_count") or 0)
            sector_count = int(payload.get("sector_count") or summary.get("sector_count") or 0)
            route_family_count = int(payload.get("route_family_count") or summary.get("route_family_count") or 0)
            evidence = {
                "catalog_row_count": catalog_row_count,
                "sector_count": sector_count,
                "route_family_count": route_family_count,
            }
            if catalog_row_count <= 0:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
                    summary="Phase 3 global catalog is empty.",
                    evidence=evidence,
                    recommended_action="sync_phase3_catalog_before_coverage_review",
                )
            return ValidatorResult(status="pass", gate_passed=True, summary="Phase 3 catalog review surface is populated.", evidence=evidence)

        if step.step_id == STEP_P3_7_SECTOR:
            sector_count = int(payload.get("sector_count") or summary.get("sector_count") or 0)
            open_gap_count = int(payload.get("open_gap_count") or summary.get("open_gap_count") or 0)
            evidence = {
                "sector_count": sector_count,
                "open_gap_count": open_gap_count,
                "manual_review_required": True,
            }
            if sector_count <= 0:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
                    summary="No sector coverage rows are available for review.",
                    evidence=evidence,
                    recommended_action="sync_phase3_catalog_before_sector_review",
                )
            return ValidatorResult(status="pass", gate_passed=True, summary="Sector coverage review surface is ready.", evidence=evidence)

        if step.step_id == STEP_P3_8_GAPS:
            gap_count = int(payload.get("gap_count") or summary.get("gap_count") or 0)
            open_gap_count = int(payload.get("open_gap_count") or summary.get("open_gap_count") or 0)
            evidence = {
                "gap_count": gap_count,
                "open_gap_count": open_gap_count,
                "classification_counts": dict(payload.get("classification_counts") or summary.get("classification_counts") or {}),
            }
            if gap_count <= 0:
                return ValidatorResult(
                    status="warning",
                    gate_passed=True,
                    passable_warning=True,
                    warnings=["coverage_gaps_zero"],
                    anomalies=[],
                    summary="Coverage gap sync completed with zero persisted gaps.",
                    evidence=evidence,
                    recommended_action="review_catalog_expectations",
                )
            return ValidatorResult(status="pass", gate_passed=True, summary="Coverage gaps detected and classified.", evidence=evidence)

        if step.step_id == STEP_P3_9_EXPORT:
            file_count = int(payload.get("file_count") or summary.get("file_count") or 0)
            evidence = {
                "file_count": file_count,
                "output_dir": payload.get("output_dir") or summary.get("output_dir"),
            }
            if file_count <= 0:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.APPROVAL_REQUIRED,
                    summary="Missing-route export produced zero JSON catalogs.",
                    evidence=evidence,
                    recommended_action="sync_coverage_gaps_before_export",
                )
            return ValidatorResult(status="pass", gate_passed=True, summary="Missing-route JSON catalogs exported.", evidence=evidence)

        if step.step_id == STEP_P3_10_RESOLUTION:
            open_gap_count = int(payload.get("open_gap_count") or summary.get("open_gap_count") or 0)
            manual_construct_count = int(payload.get("manual_construct_count") or summary.get("manual_construct_count") or len(summary.get("manual_construct_queue") or []))
            retry_extract_count = int(payload.get("retry_extract_count") or summary.get("retry_extract_count") or len(summary.get("retry_extract_queue") or []))
            patch_extract_count = int(payload.get("patch_extract_count") or summary.get("patch_extract_count") or len(summary.get("patch_extract_queue") or []))
            evidence = {
                "open_gap_count": open_gap_count,
                "manual_construct_count": manual_construct_count,
                "retry_extract_count": retry_extract_count,
                "patch_extract_count": patch_extract_count,
                "manual_review_required": True,
            }
            return ValidatorResult(status="pass", gate_passed=True, summary="Gap-resolution work queues prepared.", evidence=evidence)

        if step.step_id == STEP_P3_11_VERIFY:
            open_gap_count = int(payload.get("open_gap_count") or summary.get("open_gap_count") or 0)
            in_progress_gap_count = int(payload.get("in_progress_gap_count") or summary.get("in_progress_gap_count") or 0)
            resolved_gap_count = int(payload.get("resolved_gap_count") or summary.get("resolved_gap_count") or 0)
            evidence = {
                "open_gap_count": open_gap_count,
                "in_progress_gap_count": in_progress_gap_count,
                "resolved_gap_count": resolved_gap_count,
            }
            if open_gap_count > 0:
                return ValidatorResult(
                    status="warning",
                    gate_passed=True,
                    passable_warning=True,
                    warnings=["manual_gap_work_remaining"],
                    anomalies=[],
                    summary=f"Gap resolution still has {open_gap_count} open work items.",
                    evidence=evidence,
                    recommended_action="continue_gap_resolution_queue",
                )
            return ValidatorResult(status="pass", gate_passed=True, summary="Gap resolution verification passed.", evidence=evidence)

        return ValidatorResult(status="pass", gate_passed=True, summary="Validation passed.", evidence=dict(payload or {}))

    return {
        "phase1_extract": _for_step,
        "phase1_chain": _for_step,
        "phase1_workspace_review": _for_step,
        "phase1_new_nodes_prefill": _for_step,
        "phase1_promote_prepare": _for_step,
        "phase2_semantic_pipeline": _for_step,
        "phase2_semantic_workspace_review": _for_step,
        "phase2_cleanup_preview": _for_step,
        "phase3_route_extract": _for_step,
        "phase3_inverse_completion": _for_step,
        "phase3_step20_sequence": _for_step,
        "phase3_reorder_proposal": _for_step,
        "phase3_step30_geometry": _for_step,
        "phase3_step32_stop_recovery": _for_step,
        "phase3_step35_rank": _for_step,
        "phase3_step40_approve_prepare": _for_step,
        "phase3_merge_proposal": _for_step,
        "phase3_catalog_sync_review": _for_step,
        "phase3_sector_coverage_review": _for_step,
        "phase3_gap_detection_classification": _for_step,
        "phase3_missing_route_export": _for_step,
        "phase3_gap_resolution_queue": _for_step,
        "phase3_gap_resolution_verify": _for_step,
    }
