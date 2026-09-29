from __future__ import annotations

from typing import Any, Dict, List

from ..core.models import (
    DirectionReadinessResult,
    InverseCompletionRow,
    InverseProposalSnapshot,
    PersistedDirectionReadinessRow,
    TargetedInverseSearchResult,
)


def build_readiness_counts(results: List[DirectionReadinessResult]) -> Dict[str, Any]:
    blocked_by_code: Dict[str, int] = {}
    ready = 0
    blocked = 0
    missing_direction_rows = 0
    incomplete_binding = 0
    legacy_suspect = 0

    for result in list(results or []):
        if bool(result.is_direction_ready):
            ready += 1
        else:
            blocked += 1

        if list(result.missing_direction_ids or []):
            missing_direction_rows += 1
        if any(code in {"no_bound_routes", "only_one_bound_route", "incomplete_logical_route_binding"} for code in list(result.blocker_codes or [])):
            incomplete_binding += 1
        if "legacy_direction_context_untrusted" in list(result.blocker_codes or []):
            legacy_suspect += 1
        for code in list(result.blocker_codes or []):
            blocked_by_code[code] = int(blocked_by_code.get(code, 0) or 0) + 1

    return {
        "service_routes_total": len(list(results or [])),
        "direction_ready": int(ready),
        "direction_blocked": int(blocked),
        "with_missing_direction_rows": int(missing_direction_rows),
        "with_incomplete_binding": int(incomplete_binding),
        "with_legacy_suspect_context": int(legacy_suspect),
        "blocked_by_code": blocked_by_code,
    }


def build_persisted_readiness_counts(rows: List[PersistedDirectionReadinessRow]) -> Dict[str, Any]:
    inverse_status_counts: Dict[str, int] = {}
    search_status_counts: Dict[str, int] = {}
    blocked_by_code: Dict[str, int] = {}
    service_route_ids: set[str] = set()
    ready_rows = 0
    blocked_rows = 0
    manual_required_rows = 0

    for row in list(rows or []):
        service_route_ids.add(str(row.service_route_id))
        if bool(row.direction_ready):
            ready_rows += 1
        else:
            blocked_rows += 1
        if bool(row.manual_required):
            manual_required_rows += 1

        inverse_status = str(row.inverse_status or "unknown").strip() or "unknown"
        search_status = str(row.search_status or "not_started").strip() or "not_started"
        inverse_status_counts[inverse_status] = int(inverse_status_counts.get(inverse_status, 0) or 0) + 1
        search_status_counts[search_status] = int(search_status_counts.get(search_status, 0) or 0) + 1

        for code in list(row.blocker_codes or []):
            blocked_by_code[code] = int(blocked_by_code.get(code, 0) or 0) + 1

    return {
        "rows_total": len(list(rows or [])),
        "service_routes_total": len(service_route_ids),
        "ready_rows": int(ready_rows),
        "blocked_rows": int(blocked_rows),
        "manual_required_rows": int(manual_required_rows),
        "inverse_status_counts": inverse_status_counts,
        "search_status_counts": search_status_counts,
        "blocked_by_code": blocked_by_code,
    }


def build_proposal_analysis_counts(results: List[InverseProposalSnapshot]) -> Dict[str, Any]:
    service_route_ids: set[str] = set()
    proposal_status_counts: Dict[str, int] = {}
    reliable = 0
    plausible = 0
    no_candidate = 0

    for row in list(results or []):
        service_route_ids.add(str(row.service_route_id))
        status = str(row.proposal_status or "no_candidate_found").strip() or "no_candidate_found"
        proposal_status_counts[status] = int(proposal_status_counts.get(status, 0) or 0) + 1
        if status == "reliable_opposite_candidate":
            reliable += 1
        elif status == "plausible_opposite_candidate":
            plausible += 1
        elif status == "no_candidate_found":
            no_candidate += 1

    return {
        "rows_total": len(list(results or [])),
        "service_routes_total": len(service_route_ids),
        "reliable_candidate_rows": int(reliable),
        "plausible_candidate_rows": int(plausible),
        "no_candidate_rows": int(no_candidate),
        "proposal_status_counts": proposal_status_counts,
    }


def build_targeted_search_counts(results: List[TargetedInverseSearchResult]) -> Dict[str, Any]:
    service_route_ids: set[str] = set()
    search_status_counts: Dict[str, int] = {}
    launched = 0
    eligible = 0

    for row in list(results or []):
        service_route_ids.add(str(row.service_route_id))
        if bool(row.launched):
            launched += 1
        if bool(row.eligible):
            eligible += 1
        status = str(row.search_status or "not_started").strip() or "not_started"
        search_status_counts[status] = int(search_status_counts.get(status, 0) or 0) + 1

    return {
        "rows_total": len(list(results or [])),
        "service_routes_total": len(service_route_ids),
        "eligible_rows": int(eligible),
        "launched_rows": int(launched),
        "search_status_counts": search_status_counts,
    }


def build_inverse_completion_surface_summary(rows: List[InverseCompletionRow]) -> Dict[str, Any]:
    service_route_ids: set[str] = set()
    inverse_status_counts: Dict[str, int] = {}
    search_status_counts: Dict[str, int] = {}
    proposal_strength_counts: Dict[str, int] = {}
    ready_rows = 0
    unresolved_rows = 0
    reliable_candidate_rows = 0
    plausible_candidate_rows = 0
    no_candidate_rows = 0
    manual_handoff_rows = 0

    for row in list(rows or []):
        service_route_ids.add(str(row.service_route_id))
        if bool(row.direction_ready):
            ready_rows += 1
        else:
            unresolved_rows += 1
        if bool(row.manual_handoff_recommended):
            manual_handoff_rows += 1

        inverse_status = str(row.inverse_status or "unknown").strip() or "unknown"
        search_status = str(row.search_status or "not_started").strip() or "not_started"
        proposal_strength = str(row.proposal_strength or "").strip() or "none"

        inverse_status_counts[inverse_status] = int(inverse_status_counts.get(inverse_status, 0) or 0) + 1
        search_status_counts[search_status] = int(search_status_counts.get(search_status, 0) or 0) + 1
        proposal_strength_counts[proposal_strength] = int(proposal_strength_counts.get(proposal_strength, 0) or 0) + 1

        if inverse_status == "reliable_opposite_candidate":
            reliable_candidate_rows += 1
        elif inverse_status == "plausible_opposite_candidate":
            plausible_candidate_rows += 1
        elif inverse_status == "no_candidate_found":
            no_candidate_rows += 1

    return {
        "rows_total": len(list(rows or [])),
        "service_routes_total": len(service_route_ids),
        "ready_rows": int(ready_rows),
        "unresolved_rows": int(unresolved_rows),
        "reliable_candidate_rows": int(reliable_candidate_rows),
        "plausible_candidate_rows": int(plausible_candidate_rows),
        "no_candidate_rows": int(no_candidate_rows),
        "manual_handoff_rows": int(manual_handoff_rows),
        "search_pending_rows": int(search_status_counts.get("pending", 0) or 0),
        "search_dispatched_rows": int(search_status_counts.get("dispatched", 0) or 0),
        "search_discovered_rows": int(search_status_counts.get("discovered", 0) or 0),
        "search_materialized_rows": int(search_status_counts.get("materialized", 0) or 0),
        "search_no_results_rows": int(search_status_counts.get("no_results", 0) or 0),
        "search_failed_rows": int(search_status_counts.get("failed", 0) or 0),
        "inverse_status_counts": inverse_status_counts,
        "proposal_strength_counts": proposal_strength_counts,
        "search_status_counts": search_status_counts,
    }
