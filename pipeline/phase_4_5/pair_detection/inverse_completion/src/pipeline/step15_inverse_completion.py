from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ..adapters.merge_proposal_adapter import analyze_inverse_proposals_for_results
from ..core.models import (
    DirectionReadinessRefreshResult,
    DirectionReadinessResult,
    InverseCompletionRow,
    InverseCompletionAnalysisResult,
    InverseCompletionSurfaceResult,
    InverseProposalAnalysisResult,
    InverseProposalRefreshResult,
    InverseProposalSnapshot,
    PersistedDirectionReadinessResult,
    PersistedDirectionReadinessRow,
    PersistedDirectionStatusSnapshot,
    Step20DirectionGateResult,
    TargetedInverseSearchResult,
    TargetedInverseSearchResultSet,
)
from ..dispatch.targeted_inverse_dispatch import dispatch_targeted_inverse_search
from ..db.inventory_repo import (
    get_route_service_route_context,
    get_service_route_direction_summary,
    list_service_route_direction_summaries,
)
from ..db.status_repo import (
    get_persisted_direction_readiness_row,
    list_persisted_direction_readiness_rows,
    upsert_inverse_direction_status_rows,
)
from ..readiness.gate import evaluate_direction_readiness, missing_service_route_context_result
from ..reporting.unresolved_report import (
    build_inverse_completion_surface_summary,
    build_persisted_readiness_counts,
    build_proposal_analysis_counts,
    build_readiness_counts,
    build_targeted_search_counts,
)


ANALYSIS_VERSION = "phase3_inverse_completion_readonly_v1"
PROPOSAL_ANALYSIS_VERSION = "phase3_inverse_completion_proposals_v1"
SEARCH_ANALYSIS_VERSION = "phase3_inverse_completion_targeted_search_v1"
SURFACE_ANALYSIS_VERSION = "phase3_inverse_completion_surface_v1"
STEP20_GATE_ANALYSIS_VERSION = "phase3_step20_direction_gate_v1"


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slot_anchor_route_id(result: DirectionReadinessResult, direction_id: int) -> str | None:
    if direction_id == 0 and result.route_id_0:
        return result.route_id_0
    if direction_id == 1 and result.route_id_1:
        return result.route_id_1
    if result.route_id_0:
        return result.route_id_0
    if result.route_id_1:
        return result.route_id_1
    return None


def _persisted_slot_status(
    result: DirectionReadinessResult,
    *,
    direction_id: int,
    evaluated_at: str,
) -> PersistedDirectionStatusSnapshot | None:
    service_route_id = str(result.service_route_id or "").strip()
    if not service_route_id:
        return None

    slot_route_id = result.route_id_0 if int(direction_id) == 0 else result.route_id_1
    service_route = result.service_route
    evidence_summary = {
        "route_short_name": result.route_short_name,
        "route_label": result.route_label,
        "route_name": result.route_name,
        "operator_name": result.operator_name,
        "present_direction_ids": list(result.present_direction_ids or []),
        "missing_direction_ids": list(result.missing_direction_ids or []),
        "route_id_0": result.route_id_0,
        "route_id_1": result.route_id_1,
        "slot_route_id": slot_route_id,
        "slot_present": bool(service_route and int(direction_id) in list(service_route.present_direction_ids or [])),
        "bound_direction_ids": list(service_route.bound_direction_ids or []) if service_route else [],
        "legacy_direction_context_suspect": bool(
            service_route.legacy_direction_context_suspect if service_route else False
        ),
        "notes": list(result.notes or []),
    }
    return PersistedDirectionStatusSnapshot(
        service_route_id=service_route_id,
        direction_id=int(direction_id),
        anchor_route_id=_slot_anchor_route_id(result, int(direction_id)),
        bound_route_id=slot_route_id,
        top_candidate_route_id=None,
        top_candidate_scores={},
        proposal_payload={},
        proposal_source=None,
        proposal_evaluated_at=None,
        inverse_status=("structurally_ready" if bool(result.is_direction_ready) else "structurally_blocked"),
        search_status=("not_applicable" if bool(result.is_direction_ready) else "not_started"),
        manual_required=False,
        direction_ready=bool(result.is_direction_ready),
        blocker_codes=list(result.blocker_codes or []),
        blocker_messages=list(result.blocker_messages or []),
        evidence_summary=evidence_summary,
        analysis_version=ANALYSIS_VERSION,
        last_evaluated_at=evaluated_at,
    )


def _persisted_snapshots_from_results(
    results: list[DirectionReadinessResult],
    *,
    evaluated_at: str,
) -> list[PersistedDirectionStatusSnapshot]:
    snapshots: list[PersistedDirectionStatusSnapshot] = []
    for result in list(results or []):
        service_route_id = str(result.service_route_id or "").strip()
        if not service_route_id:
            continue
        for direction_id in (0, 1):
            snapshot = _persisted_slot_status(result, direction_id=direction_id, evaluated_at=evaluated_at)
            if snapshot is not None:
                snapshots.append(snapshot)
    return snapshots


def _overlay_inverse_proposals(
    snapshots: list[PersistedDirectionStatusSnapshot],
    proposals: list[InverseProposalSnapshot],
) -> list[PersistedDirectionStatusSnapshot]:
    proposal_map = {
        (str(proposal.service_route_id), int(proposal.direction_id)): proposal
        for proposal in list(proposals or [])
    }
    out: list[PersistedDirectionStatusSnapshot] = []
    for snapshot in list(snapshots or []):
        key = (str(snapshot.service_route_id), int(snapshot.direction_id))
        proposal = proposal_map.get(key)
        if proposal is None:
            out.append(snapshot)
            continue

        inverse_status = snapshot.inverse_status
        if not bool(snapshot.direction_ready):
            status = str(proposal.proposal_status or "").strip()
            if status in {
                "reliable_opposite_candidate",
                "plausible_opposite_candidate",
                "no_candidate_found",
            }:
                inverse_status = status

        out.append(
            PersistedDirectionStatusSnapshot(
                service_route_id=snapshot.service_route_id,
                direction_id=snapshot.direction_id,
                anchor_route_id=proposal.anchor_route_id or snapshot.anchor_route_id,
                bound_route_id=snapshot.bound_route_id,
                top_candidate_route_id=proposal.top_candidate_route_id,
                top_candidate_scores=dict(proposal.top_candidate_scores or {}),
                proposal_payload=dict(proposal.proposal_payload or {}),
                proposal_source=proposal.proposal_source or snapshot.proposal_source,
                proposal_evaluated_at=proposal.proposal_evaluated_at or snapshot.proposal_evaluated_at,
                inverse_status=inverse_status,
                search_status=snapshot.search_status,
                manual_required=snapshot.manual_required,
                direction_ready=snapshot.direction_ready,
                blocker_codes=list(snapshot.blocker_codes or []),
                blocker_messages=list(snapshot.blocker_messages or []),
                evidence_summary=dict(snapshot.evidence_summary or {}),
                analysis_version=PROPOSAL_ANALYSIS_VERSION,
                last_evaluated_at=snapshot.last_evaluated_at,
                created_at=snapshot.created_at,
                updated_at=snapshot.updated_at,
            )
        )
    return out


def _overlay_targeted_search_result(
    snapshot: PersistedDirectionStatusSnapshot,
    result: TargetedInverseSearchResult,
) -> PersistedDirectionStatusSnapshot:
    return PersistedDirectionStatusSnapshot(
        service_route_id=snapshot.service_route_id,
        direction_id=snapshot.direction_id,
        anchor_route_id=result.anchor_route_id or snapshot.anchor_route_id,
        bound_route_id=snapshot.bound_route_id,
        top_candidate_route_id=snapshot.top_candidate_route_id,
        top_candidate_scores=dict(snapshot.top_candidate_scores or {}),
        proposal_payload=dict(snapshot.proposal_payload or {}),
        proposal_source=snapshot.proposal_source,
        proposal_evaluated_at=snapshot.proposal_evaluated_at,
        inverse_status=snapshot.inverse_status,
        search_status=str(result.search_status or snapshot.search_status or "not_started"),
        search_request_payload=dict(result.request_payload or {}),
        search_result_payload=dict(result.result_payload or {}),
        dispatched_route_ids=list(result.dispatched_route_ids or []),
        materialized_route_ids=list(result.materialized_route_ids or []),
        search_started_at=result.search_started_at,
        search_finished_at=result.search_finished_at,
        search_error=result.search_error,
        manual_required=snapshot.manual_required,
        direction_ready=snapshot.direction_ready,
        blocker_codes=list(snapshot.blocker_codes or []),
        blocker_messages=list(snapshot.blocker_messages or []),
        evidence_summary=dict(snapshot.evidence_summary or {}),
        analysis_version=SEARCH_ANALYSIS_VERSION,
        last_evaluated_at=snapshot.last_evaluated_at,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


def _merge_persisted_slot_state(
    snapshot: PersistedDirectionStatusSnapshot,
    persisted: PersistedDirectionReadinessRow,
) -> PersistedDirectionStatusSnapshot:
    return PersistedDirectionStatusSnapshot(
        service_route_id=snapshot.service_route_id,
        direction_id=snapshot.direction_id,
        anchor_route_id=persisted.anchor_route_id or snapshot.anchor_route_id,
        bound_route_id=persisted.bound_route_id or snapshot.bound_route_id,
        top_candidate_route_id=persisted.top_candidate_route_id,
        top_candidate_scores=dict(persisted.top_candidate_scores or {}),
        proposal_payload=dict(persisted.proposal_payload or {}),
        proposal_source=persisted.proposal_source,
        proposal_evaluated_at=persisted.proposal_evaluated_at,
        inverse_status=str(persisted.inverse_status or snapshot.inverse_status or "unknown"),
        search_status=str(persisted.search_status or snapshot.search_status or "not_started"),
        search_request_payload=dict(persisted.search_request_payload or {}),
        search_result_payload=dict(persisted.search_result_payload or {}),
        dispatched_route_ids=list(persisted.dispatched_route_ids or []),
        materialized_route_ids=list(persisted.materialized_route_ids or []),
        search_started_at=persisted.search_started_at,
        search_finished_at=persisted.search_finished_at,
        search_error=persisted.search_error,
        manual_required=bool(persisted.manual_required),
        direction_ready=bool(snapshot.direction_ready),
        blocker_codes=list(snapshot.blocker_codes or persisted.blocker_codes or []),
        blocker_messages=list(snapshot.blocker_messages or persisted.blocker_messages or []),
        evidence_summary=dict(snapshot.evidence_summary or persisted.evidence_summary or {}),
        analysis_version=persisted.analysis_version or snapshot.analysis_version,
        last_evaluated_at=snapshot.last_evaluated_at or persisted.last_evaluated_at,
        created_at=persisted.created_at or snapshot.created_at,
        updated_at=persisted.updated_at or snapshot.updated_at,
    )


def _search_result_from_persisted_row(row: PersistedDirectionReadinessRow) -> TargetedInverseSearchResult:
    return TargetedInverseSearchResult(
        service_route_id=str(row.service_route_id),
        direction_id=int(row.direction_id),
        eligible=bool(
            not bool(row.direction_ready)
            and str(row.inverse_status or "").strip() in {
                "structurally_blocked",
                "plausible_opposite_candidate",
                "no_candidate_found",
            }
        ),
        launched=bool(
            str(row.search_status or "").strip() in {"dispatched", "discovered", "materialized"}
            or list(row.dispatched_route_ids or [])
            or list(row.materialized_route_ids or [])
        ),
        direction_ready=bool(row.direction_ready),
        inverse_status=row.inverse_status,
        search_status=row.search_status,
        anchor_route_id=row.anchor_route_id,
        request_payload=dict(row.search_request_payload or {}),
        result_payload=dict(row.search_result_payload or {}),
        dispatched_route_ids=list(row.dispatched_route_ids or []),
        materialized_route_ids=list(row.materialized_route_ids or []),
        search_error=row.search_error,
        search_started_at=row.search_started_at,
        search_finished_at=row.search_finished_at,
        blocker_codes=list(row.blocker_codes or []),
        blocker_messages=list(row.blocker_messages or []),
    )


def _proposal_strength(row: PersistedDirectionReadinessRow) -> str | None:
    status = str(row.inverse_status or "").strip()
    if status == "reliable_opposite_candidate":
        return "reliable"
    if status == "plausible_opposite_candidate":
        return "plausible"
    if status == "no_candidate_found":
        return "none"
    if bool(row.direction_ready) or status == "structurally_ready":
        return "ready"
    return None


def _proposal_summary(row: PersistedDirectionReadinessRow) -> str | None:
    candidate_id = str(row.top_candidate_route_id or "").strip()
    if not candidate_id:
        if str(row.inverse_status or "").strip() == "no_candidate_found":
            return "No strong opposite candidate in current inventory."
        return None
    score_payload = dict(row.top_candidate_scores or {})
    merge_score = score_payload.get("merge_readiness_score")
    opposite_score = score_payload.get("opposite_direction_score")
    score_bits: list[str] = []
    if merge_score is not None:
        try:
            score_bits.append(f"merge={float(merge_score):.2f}")
        except Exception:
            pass
    if opposite_score is not None:
        try:
            score_bits.append(f"opp={float(opposite_score):.2f}")
        except Exception:
            pass
    suffix = f" ({', '.join(score_bits)})" if score_bits else ""
    return f"Top candidate: {candidate_id}{suffix}"


def _search_summary(row: PersistedDirectionReadinessRow) -> str | None:
    if list(row.materialized_route_ids or []):
        return f"Materialized {len(list(row.materialized_route_ids or []))} route candidate(s)."
    if list(row.dispatched_route_ids or []):
        return f"Dispatched {len(list(row.dispatched_route_ids or []))} route candidate(s)."
    if str(row.search_error or "").strip():
        return str(row.search_error).strip()
    status = str(row.search_status or "").strip()
    if status in {"no_results", "failed"}:
        return status.replace("_", " ")
    if status in {"pending", "dispatched", "discovered", "materialized"}:
        return status.replace("_", " ")
    return None


def _manual_handoff_metadata(row: PersistedDirectionReadinessRow) -> tuple[bool, str | None, str | None]:
    if bool(row.direction_ready):
        return False, None, "Ready for downstream review."
    if bool(row.manual_required):
        return True, "Manual construction is already marked as required.", "Open manual builder."
    inverse_status = str(row.inverse_status or "").strip()
    search_status = str(row.search_status or "").strip()
    if inverse_status == "reliable_opposite_candidate":
        return False, None, "Review opposite candidate before manual construction."
    if inverse_status == "plausible_opposite_candidate":
        return False, None, "Review plausible candidate or run targeted inverse search."
    if search_status in {"pending", "dispatched"}:
        return False, None, "Wait for targeted inverse search to complete."
    if search_status == "discovered":
        return False, None, "Review discovered candidate route before manual construction."
    if search_status == "materialized":
        return True, "Targeted search materialized candidate routes but nothing is bound yet.", "Review candidate or open manual builder."
    if search_status in {"no_results", "failed"}:
        return True, "Targeted inverse search did not resolve this opposite direction.", "Open manual builder."
    if inverse_status == "no_candidate_found":
        return True, "No strong opposite candidate is present in current inventory.", "Open manual builder."
    return False, None, "Continue inverse completion review."


def _step20_next_action(row: PersistedDirectionReadinessRow | None, *, blocker_codes: list[str]) -> str:
    if row is None:
        return "open_step15_inverse"
    inverse_status = str(row.inverse_status or "").strip()
    search_status = str(row.search_status or "").strip()
    if inverse_status == "reliable_opposite_candidate":
        return "review_reliable_candidate"
    if search_status in {"discovered", "materialized"}:
        return "open_step15_inverse"
    if inverse_status == "plausible_opposite_candidate":
        return "run_targeted_inverse_search"
    if inverse_status == "no_candidate_found" or search_status in {"no_results", "failed"}:
        return "handoff_manual_builder"
    if "inverse_state_missing" in list(blocker_codes or []):
        return "open_step15_inverse"
    return "open_step15_inverse"


def _surface_row_from_persisted(row: PersistedDirectionReadinessRow) -> InverseCompletionRow:
    blocker_messages = list(row.blocker_messages or [])
    blocker_codes = list(row.blocker_codes or [])
    blocker_summary = blocker_messages[0] if blocker_messages else (blocker_codes[0] if blocker_codes else None)
    manual_handoff_recommended, manual_reason, next_action = _manual_handoff_metadata(row)
    return InverseCompletionRow(
        service_route_id=str(row.service_route_id),
        route_short_name=row.route_short_name,
        route_label=row.route_label,
        route_name=row.route_name,
        operator_name=row.operator_name,
        direction_id=int(row.direction_id),
        logical_route_id=row.logical_route_id,
        bound_route_id=row.bound_route_id,
        anchor_route_id=row.anchor_route_id,
        direction_ready=bool(row.direction_ready),
        inverse_status=row.inverse_status,
        search_status=row.search_status,
        blocker_codes=blocker_codes,
        blocker_messages=blocker_messages,
        blocker_summary=blocker_summary,
        top_candidate_route_id=row.top_candidate_route_id,
        top_candidate_scores=dict(row.top_candidate_scores or {}),
        proposal_strength=_proposal_strength(row),
        proposal_summary=_proposal_summary(row),
        search_summary=_search_summary(row),
        dispatched_route_ids=list(row.dispatched_route_ids or []),
        materialized_route_ids=list(row.materialized_route_ids or []),
        manual_handoff_recommended=bool(manual_handoff_recommended),
        manual_handoff_reason=manual_reason,
        next_action=next_action,
        proposal_payload=dict(row.proposal_payload or {}),
        search_request_payload=dict(row.search_request_payload or {}),
        search_result_payload=dict(row.search_result_payload or {}),
        search_error=row.search_error,
        raw_row=asdict(row),
    )


def _service_route_ids_from_results(results: list[DirectionReadinessResult]) -> list[str]:
    return [
        str(result.service_route_id).strip()
        for result in list(results or [])
        if str(result.service_route_id or "").strip()
    ]


def _list_persisted_rows_for_results(
    results: list[DirectionReadinessResult],
    *,
    include_ready: bool,
) -> list:
    service_route_ids = _service_route_ids_from_results(results)
    if not service_route_ids:
        return []
    return list_persisted_direction_readiness_rows(
        service_route_ids=service_route_ids,
        include_ready=bool(include_ready),
        limit=None,
    )


def list_direction_readiness(
    *,
    limit: int = 100,
    include_ready: bool = True,
) -> InverseCompletionAnalysisResult:
    summaries = list_service_route_direction_summaries(limit=max(1, int(limit)))
    results = [evaluate_direction_readiness(summary) for summary in summaries]
    if not include_ready:
        results = [row for row in results if not bool(row.is_direction_ready)]
    return InverseCompletionAnalysisResult(
        analysis_version=ANALYSIS_VERSION,
        results=results,
        counts=build_readiness_counts(results),
        generated_at=_generated_at(),
    )


def get_direction_readiness(
    *,
    service_route_id: str | None = None,
    route_id: str | None = None,
) -> DirectionReadinessResult:
    sid = str(service_route_id or "").strip() or None
    rid = str(route_id or "").strip() or None

    if rid and not sid:
        context = get_route_service_route_context(rid)
        sid = str(context.get("service_route_id") or "").strip() or None
        if not sid:
            return missing_service_route_context_result(route_id=rid)

    if not sid:
        return missing_service_route_context_result(route_id=rid)

    summary = get_service_route_direction_summary(sid)
    return evaluate_direction_readiness(summary, focus_route_id=rid)


def analyze_inverse_completion(
    *,
    service_route_id: str | None = None,
    route_id: str | None = None,
    limit: int = 100,
    include_ready: bool = True,
) -> InverseCompletionAnalysisResult:
    sid = str(service_route_id or "").strip() or None
    rid = str(route_id or "").strip() or None
    if sid or rid:
        result = get_direction_readiness(service_route_id=sid, route_id=rid)
        results = [result]
        if not include_ready and result.is_direction_ready:
            results = []
        return InverseCompletionAnalysisResult(
            analysis_version=ANALYSIS_VERSION,
            results=results,
            counts=build_readiness_counts(results),
            generated_at=_generated_at(),
        )
    return list_direction_readiness(limit=limit, include_ready=include_ready)


def list_persisted_direction_readiness(
    *,
    service_route_id: str | None = None,
    include_ready: bool = True,
    limit: int = 100,
) -> PersistedDirectionReadinessResult:
    rows = list_persisted_direction_readiness_rows(
        service_route_id=(str(service_route_id).strip() if service_route_id else None),
        include_ready=bool(include_ready),
        limit=max(1, int(limit)),
    )
    return PersistedDirectionReadinessResult(
        analysis_version=ANALYSIS_VERSION,
        results=rows,
        counts=build_persisted_readiness_counts(rows),
        generated_at=_generated_at(),
    )


def get_persisted_direction_readiness(
    *,
    service_route_id: str,
    direction_id: int,
) -> PersistedDirectionReadinessResult:
    row = get_persisted_direction_readiness_row(
        service_route_id=str(service_route_id).strip(),
        direction_id=int(direction_id),
    )
    rows = [row] if row is not None else []
    return PersistedDirectionReadinessResult(
        analysis_version=ANALYSIS_VERSION,
        results=rows,
        counts=build_persisted_readiness_counts(rows),
        generated_at=_generated_at(),
    )


def refresh_direction_readiness(
    *,
    service_route_id: str | None = None,
    route_id: str | None = None,
    limit: int = 100,
    include_ready: bool = True,
) -> DirectionReadinessRefreshResult:
    analysis = analyze_inverse_completion(
        service_route_id=service_route_id,
        route_id=route_id,
        limit=max(1, int(limit)),
        include_ready=True,
    )
    evaluated_at = _generated_at()
    persisted_snapshots = _persisted_snapshots_from_results(analysis.results, evaluated_at=evaluated_at)
    upsert_inverse_direction_status_rows(persisted_snapshots)

    persisted_rows = _list_persisted_rows_for_results(list(analysis.results or []), include_ready=bool(include_ready))
    filtered_results = list(analysis.results or [])
    if not include_ready:
        filtered_results = [row for row in filtered_results if not bool(row.is_direction_ready)]

    return DirectionReadinessRefreshResult(
        analysis_version=ANALYSIS_VERSION,
        analysis=InverseCompletionAnalysisResult(
            analysis_version=ANALYSIS_VERSION,
            results=filtered_results,
            counts=build_readiness_counts(filtered_results),
            generated_at=analysis.generated_at,
        ),
        persisted=PersistedDirectionReadinessResult(
            analysis_version=ANALYSIS_VERSION,
            results=persisted_rows,
            counts=build_persisted_readiness_counts(persisted_rows),
            generated_at=evaluated_at,
        ),
        persisted_row_count=len(list(persisted_rows or [])),
        generated_at=evaluated_at,
    )


def analyze_inverse_proposals(
    *,
    service_route_id: str | None = None,
    route_id: str | None = None,
    limit: int = 100,
    include_ready: bool = True,
) -> InverseProposalAnalysisResult:
    structural = analyze_inverse_completion(
        service_route_id=service_route_id,
        route_id=route_id,
        limit=max(1, int(limit)),
        include_ready=True,
    )
    proposal_results = analyze_inverse_proposals_for_results(structural.results)
    if include_ready:
        ready_results = [
            InverseProposalSnapshot(
                service_route_id=str(result.service_route_id or ""),
                direction_id=0,
                anchor_route_id=result.route_id_0,
                bound_route_id=result.route_id_0,
                top_candidate_route_id=None,
                proposal_status="structurally_ready",
                top_candidate_scores={},
                proposal_payload={},
                proposal_source=None,
                proposal_evaluated_at=None,
                direction_ready=True,
                blocker_codes=list(result.blocker_codes or []),
                blocker_messages=list(result.blocker_messages or []),
            )
            for result in list(structural.results or [])
            if bool(result.is_direction_ready) and str(result.service_route_id or "").strip()
        ]
        ready_results += [
            InverseProposalSnapshot(
                service_route_id=str(result.service_route_id or ""),
                direction_id=1,
                anchor_route_id=result.route_id_1,
                bound_route_id=result.route_id_1,
                top_candidate_route_id=None,
                proposal_status="structurally_ready",
                top_candidate_scores={},
                proposal_payload={},
                proposal_source=None,
                proposal_evaluated_at=None,
                direction_ready=True,
                blocker_codes=list(result.blocker_codes or []),
                blocker_messages=list(result.blocker_messages or []),
            )
            for result in list(structural.results or [])
            if bool(result.is_direction_ready) and str(result.service_route_id or "").strip()
        ]
        proposal_results = list(proposal_results or []) + ready_results
    return InverseProposalAnalysisResult(
        analysis_version=PROPOSAL_ANALYSIS_VERSION,
        results=proposal_results,
        counts=build_proposal_analysis_counts(proposal_results),
        generated_at=_generated_at(),
    )


def refresh_inverse_proposals(
    *,
    service_route_id: str | None = None,
    route_id: str | None = None,
    limit: int = 100,
    include_ready: bool = True,
) -> InverseProposalRefreshResult:
    structural = analyze_inverse_completion(
        service_route_id=service_route_id,
        route_id=route_id,
        limit=max(1, int(limit)),
        include_ready=True,
    )
    evaluated_at = _generated_at()
    structural_snapshots = _persisted_snapshots_from_results(structural.results, evaluated_at=evaluated_at)
    proposal_results = analyze_inverse_proposals_for_results(structural.results)
    enriched_snapshots = _overlay_inverse_proposals(structural_snapshots, proposal_results)
    upsert_inverse_direction_status_rows(enriched_snapshots)
    persisted_rows = _list_persisted_rows_for_results(list(structural.results or []), include_ready=bool(include_ready))

    filtered_structural = list(structural.results or [])
    if not include_ready:
        filtered_structural = [row for row in filtered_structural if not bool(row.is_direction_ready)]

    filtered_proposals = list(proposal_results or [])
    if include_ready:
        ready_rows = [
            InverseProposalSnapshot(
                service_route_id=str(result.service_route_id or ""),
                direction_id=0,
                anchor_route_id=result.route_id_0,
                bound_route_id=result.route_id_0,
                top_candidate_route_id=None,
                proposal_status="structurally_ready",
                top_candidate_scores={},
                proposal_payload={},
                proposal_source=None,
                proposal_evaluated_at=None,
                direction_ready=True,
                blocker_codes=list(result.blocker_codes or []),
                blocker_messages=list(result.blocker_messages or []),
            )
            for result in filtered_structural
            if bool(result.is_direction_ready) and str(result.service_route_id or "").strip()
        ]
        ready_rows += [
            InverseProposalSnapshot(
                service_route_id=str(result.service_route_id or ""),
                direction_id=1,
                anchor_route_id=result.route_id_1,
                bound_route_id=result.route_id_1,
                top_candidate_route_id=None,
                proposal_status="structurally_ready",
                top_candidate_scores={},
                proposal_payload={},
                proposal_source=None,
                proposal_evaluated_at=None,
                direction_ready=True,
                blocker_codes=list(result.blocker_codes or []),
                blocker_messages=list(result.blocker_messages or []),
            )
            for result in filtered_structural
            if bool(result.is_direction_ready) and str(result.service_route_id or "").strip()
        ]
        filtered_proposals = filtered_proposals + ready_rows

    return InverseProposalRefreshResult(
        analysis_version=PROPOSAL_ANALYSIS_VERSION,
        structural=InverseCompletionAnalysisResult(
            analysis_version=ANALYSIS_VERSION,
            results=filtered_structural,
            counts=build_readiness_counts(filtered_structural),
            generated_at=structural.generated_at,
        ),
        proposals=InverseProposalAnalysisResult(
            analysis_version=PROPOSAL_ANALYSIS_VERSION,
            results=filtered_proposals,
            counts=build_proposal_analysis_counts(filtered_proposals),
            generated_at=evaluated_at,
        ),
        persisted=PersistedDirectionReadinessResult(
            analysis_version=PROPOSAL_ANALYSIS_VERSION,
            results=persisted_rows,
            counts=build_persisted_readiness_counts(persisted_rows),
            generated_at=evaluated_at,
        ),
        persisted_row_count=len(list(persisted_rows or [])),
        generated_at=evaluated_at,
    )


def list_inverse_proposal_rows(
    *,
    service_route_id: str | None = None,
    include_ready: bool = True,
    limit: int = 100,
) -> PersistedDirectionReadinessResult:
    return list_persisted_direction_readiness(
        service_route_id=service_route_id,
        include_ready=include_ready,
        limit=limit,
    )


def get_inverse_proposal_row(
    *,
    service_route_id: str,
    direction_id: int,
) -> PersistedDirectionReadinessResult:
    return get_persisted_direction_readiness(
        service_route_id=service_route_id,
        direction_id=direction_id,
    )


def dispatch_targeted_inverse_search_for_slot(
    *,
    service_route_id: str,
    direction_id: int,
    phase3_client: Any | None = None,
    force: bool = False,
) -> TargetedInverseSearchResult:
    from datamind_console.phases.phase3_routes.client import Phase3Client

    client = phase3_client or Phase3Client()
    structural = get_direction_readiness(service_route_id=service_route_id)
    persisted = get_persisted_direction_readiness_row(
        service_route_id=str(service_route_id).strip(),
        direction_id=int(direction_id),
    )
    if persisted is None:
        refresh_direction_readiness(service_route_id=service_route_id, include_ready=True)
        persisted = get_persisted_direction_readiness_row(
            service_route_id=str(service_route_id).strip(),
            direction_id=int(direction_id),
        )
    if persisted is None:
        raise RuntimeError("Could not load persisted inverse-completion slot state.")

    structural_snapshots = _persisted_snapshots_from_results([structural], evaluated_at=_generated_at())
    snapshot_map = {
        (str(item.service_route_id), int(item.direction_id)): item
        for item in list(structural_snapshots or [])
    }
    slot_snapshot = snapshot_map.get((str(service_route_id), int(direction_id)))
    if slot_snapshot is None:
        raise RuntimeError("Could not build a structural snapshot for the requested slot.")
    slot_snapshot = _merge_persisted_slot_state(slot_snapshot, persisted)

    pending_result = dispatch_targeted_inverse_search(
        phase3_client=client,
        structural=structural,
        persisted_row=persisted,
        service_route_id=str(service_route_id),
        direction_id=int(direction_id),
        force=bool(force),
    )
    merged_snapshot = _overlay_targeted_search_result(slot_snapshot, pending_result)
    upsert_inverse_direction_status_rows([merged_snapshot])

    persisted_after = get_persisted_direction_readiness_row(
        service_route_id=str(service_route_id).strip(),
        direction_id=int(direction_id),
    )
    if persisted_after is not None:
        return _search_result_from_persisted_row(persisted_after)
    return pending_result


def refresh_targeted_inverse_search(
    *,
    service_route_id: str,
    direction_id: int,
    phase3_client: Any | None = None,
    force: bool = False,
) -> TargetedInverseSearchResult:
    return dispatch_targeted_inverse_search_for_slot(
        service_route_id=service_route_id,
        direction_id=direction_id,
        phase3_client=phase3_client,
        force=force,
    )


def list_targeted_inverse_search_results(
    *,
    service_route_id: str | None = None,
    include_ready: bool = True,
    limit: int = 100,
) -> TargetedInverseSearchResultSet:
    persisted = list_persisted_direction_readiness(
        service_route_id=service_route_id,
        include_ready=include_ready,
        limit=limit,
    )
    results = [_search_result_from_persisted_row(row) for row in list(persisted.results or [])]
    return TargetedInverseSearchResultSet(
        analysis_version=SEARCH_ANALYSIS_VERSION,
        results=results,
        counts=build_targeted_search_counts(results),
        generated_at=_generated_at(),
    )


def get_targeted_inverse_search_result(
    *,
    service_route_id: str,
    direction_id: int,
) -> TargetedInverseSearchResultSet:
    persisted = get_persisted_direction_readiness(
        service_route_id=service_route_id,
        direction_id=direction_id,
    )
    results = [_search_result_from_persisted_row(row) for row in list(persisted.results or [])]
    return TargetedInverseSearchResultSet(
        analysis_version=SEARCH_ANALYSIS_VERSION,
        results=results,
        counts=build_targeted_search_counts(results),
        generated_at=_generated_at(),
    )


def list_inverse_completion_rows(
    *,
    service_route_id: str | None = None,
    include_ready: bool = True,
    limit: int = 100,
) -> InverseCompletionSurfaceResult:
    persisted = list_persisted_direction_readiness(
        service_route_id=service_route_id,
        include_ready=include_ready,
        limit=limit,
    )
    rows = [_surface_row_from_persisted(row) for row in list(persisted.results or [])]
    unresolved_rows = [row for row in rows if not bool(row.direction_ready)]
    ready_rows = [row for row in rows if bool(row.direction_ready)]
    return InverseCompletionSurfaceResult(
        analysis_version=SURFACE_ANALYSIS_VERSION,
        rows=rows,
        unresolved_rows=unresolved_rows,
        ready_rows=ready_rows,
        summary=build_inverse_completion_surface_summary(rows),
        generated_at=_generated_at(),
    )


def get_inverse_completion_summary(
    *,
    service_route_id: str | None = None,
    include_ready: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    surface = list_inverse_completion_rows(
        service_route_id=service_route_id,
        include_ready=include_ready,
        limit=limit,
    )
    return dict(surface.summary or {})


def get_step20_direction_gate(
    *,
    service_route_id: str | None = None,
    direction_id: int | None = None,
    route_id: str | None = None,
) -> Step20DirectionGateResult:
    sid = str(service_route_id or "").strip() or None
    rid = str(route_id or "").strip() or None
    did = int(direction_id) if direction_id in (0, 1) else None

    if rid and (not sid or did not in (0, 1)):
        context = get_route_service_route_context(rid)
        sid = sid or (str(context.get("service_route_id") or "").strip() or None)
        if did not in (0, 1):
            raw_did = context.get("direction_id")
            did = int(raw_did) if raw_did in (0, 1) else None

    if not sid or did not in (0, 1):
        return Step20DirectionGateResult(
            gate_passed=False,
            gate_code="direction_not_ready",
            service_route_id=sid,
            direction_id=did,
            route_id=rid,
            direction_ready=False,
            inverse_status=None,
            search_status=None,
            blocker_codes=["service_route_context_missing"],
            blocker_messages=[
                "Step 20 requires a logical-route direction context with persisted inverse-completion state."
            ],
            suggested_next_action="open_step15_inverse",
            gate_message="Step 20 is blocked until the logical route is direction-ready in Step 15.",
            analysis_version=STEP20_GATE_ANALYSIS_VERSION,
        )

    persisted = get_persisted_direction_readiness_row(service_route_id=sid, direction_id=did)
    if persisted is None:
        return Step20DirectionGateResult(
            gate_passed=False,
            gate_code="direction_not_ready",
            service_route_id=sid,
            direction_id=did,
            route_id=rid,
            direction_ready=False,
            inverse_status=None,
            search_status=None,
            blocker_codes=["inverse_state_missing"],
            blocker_messages=[
                "Persisted inverse-completion state is missing for this logical-route slot. Refresh Step 15 first."
            ],
            suggested_next_action="open_step15_inverse",
            gate_message="Step 20 is blocked until the logical route is direction-ready in Step 15.",
            analysis_version=STEP20_GATE_ANALYSIS_VERSION,
        )

    if bool(persisted.direction_ready):
        return Step20DirectionGateResult(
            gate_passed=True,
            gate_code="direction_ready",
            service_route_id=sid,
            direction_id=did,
            route_id=rid or persisted.bound_route_id,
            direction_ready=True,
            inverse_status=persisted.inverse_status,
            search_status=persisted.search_status,
            blocker_codes=[],
            blocker_messages=[],
            suggested_next_action="proceed_step20",
            gate_message="Direction readiness confirmed.",
            analysis_version=STEP20_GATE_ANALYSIS_VERSION,
        )

    blocker_codes = list(persisted.blocker_codes or []) or ["direction_not_ready"]
    blocker_messages = list(persisted.blocker_messages or []) or [
        "Logical route direction structure is not yet ready for Step 20."
    ]
    return Step20DirectionGateResult(
        gate_passed=False,
        gate_code="direction_not_ready",
        service_route_id=sid,
        direction_id=did,
        route_id=rid or persisted.bound_route_id,
        direction_ready=False,
        inverse_status=persisted.inverse_status,
        search_status=persisted.search_status,
        blocker_codes=blocker_codes,
        blocker_messages=blocker_messages,
        suggested_next_action=_step20_next_action(persisted, blocker_codes=blocker_codes),
        gate_message="Step 20 is blocked until the logical route is direction-ready in Step 15.",
        analysis_version=STEP20_GATE_ANALYSIS_VERSION,
    )
