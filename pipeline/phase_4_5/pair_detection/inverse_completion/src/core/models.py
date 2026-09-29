from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True, slots=True)
class InverseDirectionSnapshot:
    direction_id: int
    route_id: Optional[str] = None
    phase3_progress_step: int = 0
    direction_approval_status: str = "pending"
    geom_source: str = "unknown"
    route_job_service_route_id: Optional[str] = None
    route_job_direction_id: Optional[int] = None
    route_prod_service_route_id: Optional[str] = None
    route_prod_direction_id: Optional[int] = None
    has_route_prod: bool = False
    chosen_osm_relation_id: Optional[int] = None


@dataclass(frozen=True, slots=True)
class DirectionBlocker:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ServiceRouteDirectionSummary:
    service_route_id: str
    route_short_name: Optional[str] = None
    route_label: Optional[str] = None
    route_name: Optional[str] = None
    operator_name: Optional[str] = None
    route_id_0: Optional[str] = None
    route_id_1: Optional[str] = None
    present_direction_ids: List[int] = field(default_factory=list)
    missing_direction_ids: List[int] = field(default_factory=list)
    bound_direction_ids: List[int] = field(default_factory=list)
    directions: List[InverseDirectionSnapshot] = field(default_factory=list)
    legacy_direction_context_suspect: bool = False
    notes: List[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DirectionReadinessResult:
    service_route_id: Optional[str]
    route_short_name: Optional[str] = None
    route_label: Optional[str] = None
    route_name: Optional[str] = None
    operator_name: Optional[str] = None
    focus_route_id: Optional[str] = None
    route_id_0: Optional[str] = None
    route_id_1: Optional[str] = None
    present_direction_ids: List[int] = field(default_factory=list)
    missing_direction_ids: List[int] = field(default_factory=list)
    is_direction_ready: bool = False
    blockers: List[DirectionBlocker] = field(default_factory=list)
    blocker_codes: List[str] = field(default_factory=list)
    blocker_messages: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    evidence_summary: Dict[str, Any] = field(default_factory=dict)
    service_route: Optional[ServiceRouteDirectionSummary] = None


@dataclass(frozen=True, slots=True)
class InverseCompletionAnalysisResult:
    analysis_version: str
    results: List[DirectionReadinessResult] = field(default_factory=list)
    counts: Dict[str, Any] = field(default_factory=dict)
    generated_at: Optional[str] = None


@dataclass(frozen=True, slots=True)
class PersistedDirectionStatusSnapshot:
    service_route_id: str
    direction_id: int
    anchor_route_id: Optional[str] = None
    bound_route_id: Optional[str] = None
    top_candidate_route_id: Optional[str] = None
    top_candidate_scores: Dict[str, Any] = field(default_factory=dict)
    proposal_payload: Dict[str, Any] = field(default_factory=dict)
    proposal_source: Optional[str] = None
    proposal_evaluated_at: Optional[str] = None
    inverse_status: str = "unknown"
    search_status: str = "not_started"
    search_request_payload: Dict[str, Any] = field(default_factory=dict)
    search_result_payload: Dict[str, Any] = field(default_factory=dict)
    dispatched_route_ids: List[str] = field(default_factory=list)
    materialized_route_ids: List[str] = field(default_factory=list)
    search_started_at: Optional[str] = None
    search_finished_at: Optional[str] = None
    search_error: Optional[str] = None
    manual_required: bool = False
    direction_ready: bool = False
    blocker_codes: List[str] = field(default_factory=list)
    blocker_messages: List[str] = field(default_factory=list)
    evidence_summary: Dict[str, Any] = field(default_factory=dict)
    analysis_version: Optional[str] = None
    last_evaluated_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True, slots=True)
class PersistedDirectionReadinessRow:
    service_route_id: str
    route_short_name: Optional[str] = None
    route_label: Optional[str] = None
    route_name: Optional[str] = None
    operator_name: Optional[str] = None
    direction_id: int = 0
    logical_route_id: Optional[str] = None
    bound_route_id: Optional[str] = None
    anchor_route_id: Optional[str] = None
    top_candidate_route_id: Optional[str] = None
    top_candidate_scores: Dict[str, Any] = field(default_factory=dict)
    proposal_payload: Dict[str, Any] = field(default_factory=dict)
    proposal_source: Optional[str] = None
    proposal_evaluated_at: Optional[str] = None
    phase3_progress_step: int = 0
    direction_approval_status: str = "pending"
    geom_source: str = "unknown"
    inverse_status: str = "unknown"
    search_status: str = "not_started"
    search_request_payload: Dict[str, Any] = field(default_factory=dict)
    search_result_payload: Dict[str, Any] = field(default_factory=dict)
    dispatched_route_ids: List[str] = field(default_factory=list)
    materialized_route_ids: List[str] = field(default_factory=list)
    search_started_at: Optional[str] = None
    search_finished_at: Optional[str] = None
    search_error: Optional[str] = None
    manual_required: bool = False
    direction_ready: bool = False
    blocker_codes: List[str] = field(default_factory=list)
    blocker_messages: List[str] = field(default_factory=list)
    evidence_summary: Dict[str, Any] = field(default_factory=dict)
    analysis_version: Optional[str] = None
    last_evaluated_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True, slots=True)
class PersistedDirectionReadinessResult:
    analysis_version: str
    results: List[PersistedDirectionReadinessRow] = field(default_factory=list)
    counts: Dict[str, Any] = field(default_factory=dict)
    generated_at: Optional[str] = None


@dataclass(frozen=True, slots=True)
class DirectionReadinessRefreshResult:
    analysis_version: str
    analysis: InverseCompletionAnalysisResult
    persisted: PersistedDirectionReadinessResult
    persisted_row_count: int = 0
    generated_at: Optional[str] = None


@dataclass(frozen=True, slots=True)
class InverseProposalSnapshot:
    service_route_id: str
    direction_id: int
    anchor_route_id: Optional[str] = None
    bound_route_id: Optional[str] = None
    top_candidate_route_id: Optional[str] = None
    proposal_status: str = "no_candidate_found"
    top_candidate_scores: Dict[str, Any] = field(default_factory=dict)
    proposal_payload: Dict[str, Any] = field(default_factory=dict)
    proposal_source: Optional[str] = None
    proposal_evaluated_at: Optional[str] = None
    direction_ready: bool = False
    blocker_codes: List[str] = field(default_factory=list)
    blocker_messages: List[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class InverseProposalAnalysisResult:
    analysis_version: str
    results: List[InverseProposalSnapshot] = field(default_factory=list)
    counts: Dict[str, Any] = field(default_factory=dict)
    generated_at: Optional[str] = None


@dataclass(frozen=True, slots=True)
class InverseProposalRefreshResult:
    analysis_version: str
    structural: InverseCompletionAnalysisResult
    proposals: InverseProposalAnalysisResult
    persisted: PersistedDirectionReadinessResult
    persisted_row_count: int = 0
    generated_at: Optional[str] = None


@dataclass(frozen=True, slots=True)
class TargetedInverseSearchRequest:
    service_route_id: str
    direction_id: int
    anchor_route_id: Optional[str] = None
    bbox: Dict[str, float] = field(default_factory=dict)
    refs: List[str] = field(default_factory=list)
    operator: Optional[str] = None
    name: Optional[str] = None
    route_hint_raw: Optional[str] = None
    cooperative_hint: Optional[str] = None
    target_group: str = "inverse_completion"
    target_priority: str = "inverse_search"
    target_attempt_type: str = "targeted_inverse"
    target_seed_origin: str = "inverse_search"
    source_document: Optional[str] = None
    target_place_bundle: Optional[str] = None


@dataclass(frozen=True, slots=True)
class TargetedInverseSearchResult:
    service_route_id: str
    direction_id: int
    eligible: bool = False
    launched: bool = False
    direction_ready: bool = False
    inverse_status: Optional[str] = None
    search_status: str = "not_started"
    anchor_route_id: Optional[str] = None
    request_payload: Dict[str, Any] = field(default_factory=dict)
    result_payload: Dict[str, Any] = field(default_factory=dict)
    dispatched_route_ids: List[str] = field(default_factory=list)
    materialized_route_ids: List[str] = field(default_factory=list)
    search_error: Optional[str] = None
    search_started_at: Optional[str] = None
    search_finished_at: Optional[str] = None
    blocker_codes: List[str] = field(default_factory=list)
    blocker_messages: List[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TargetedInverseSearchResultSet:
    analysis_version: str
    results: List[TargetedInverseSearchResult] = field(default_factory=list)
    counts: Dict[str, Any] = field(default_factory=dict)
    generated_at: Optional[str] = None


@dataclass(frozen=True, slots=True)
class InverseCompletionRow:
    service_route_id: str
    route_short_name: Optional[str] = None
    route_label: Optional[str] = None
    route_name: Optional[str] = None
    operator_name: Optional[str] = None
    direction_id: int = 0
    logical_route_id: Optional[str] = None
    bound_route_id: Optional[str] = None
    anchor_route_id: Optional[str] = None
    direction_ready: bool = False
    inverse_status: str = "unknown"
    search_status: str = "not_started"
    blocker_codes: List[str] = field(default_factory=list)
    blocker_messages: List[str] = field(default_factory=list)
    blocker_summary: Optional[str] = None
    top_candidate_route_id: Optional[str] = None
    top_candidate_scores: Dict[str, Any] = field(default_factory=dict)
    proposal_strength: Optional[str] = None
    proposal_summary: Optional[str] = None
    search_summary: Optional[str] = None
    dispatched_route_ids: List[str] = field(default_factory=list)
    materialized_route_ids: List[str] = field(default_factory=list)
    manual_handoff_recommended: bool = False
    manual_handoff_reason: Optional[str] = None
    next_action: Optional[str] = None
    proposal_payload: Dict[str, Any] = field(default_factory=dict)
    search_request_payload: Dict[str, Any] = field(default_factory=dict)
    search_result_payload: Dict[str, Any] = field(default_factory=dict)
    search_error: Optional[str] = None
    raw_row: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InverseCompletionSurfaceResult:
    analysis_version: str
    rows: List[InverseCompletionRow] = field(default_factory=list)
    unresolved_rows: List[InverseCompletionRow] = field(default_factory=list)
    ready_rows: List[InverseCompletionRow] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    generated_at: Optional[str] = None


@dataclass(frozen=True, slots=True)
class Step20DirectionGateResult:
    gate_passed: bool
    gate_code: str
    service_route_id: Optional[str] = None
    direction_id: Optional[int] = None
    route_id: Optional[str] = None
    direction_ready: bool = False
    inverse_status: Optional[str] = None
    search_status: Optional[str] = None
    blocker_codes: List[str] = field(default_factory=list)
    blocker_messages: List[str] = field(default_factory=list)
    suggested_next_action: Optional[str] = None
    gate_message: Optional[str] = None
    analysis_version: Optional[str] = None
