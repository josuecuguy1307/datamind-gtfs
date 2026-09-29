from __future__ import annotations

from typing import List, Optional

from ..core.models import DirectionBlocker, DirectionReadinessResult, ServiceRouteDirectionSummary


def _add_blocker(blockers: List[DirectionBlocker], *, code: str, message: str) -> None:
    if any(b.code == code for b in blockers):
        return
    blockers.append(DirectionBlocker(code=code, message=message))


def missing_service_route_context_result(*, route_id: Optional[str] = None) -> DirectionReadinessResult:
    blockers = [
        DirectionBlocker(
            code="service_route_context_missing",
            message="Route is not linked to a logical service_route_id / direction_id context.",
        )
    ]
    return DirectionReadinessResult(
        service_route_id=None,
        focus_route_id=route_id,
        is_direction_ready=False,
        blockers=blockers,
        blocker_codes=[b.code for b in blockers],
        blocker_messages=[b.message for b in blockers],
        notes=["Structural direction readiness cannot be evaluated without logical route context."],
        evidence_summary={"focus_route_id": route_id, "present_direction_ids": [], "missing_direction_ids": [0, 1]},
        service_route=None,
    )


def evaluate_direction_readiness(
    summary: Optional[ServiceRouteDirectionSummary],
    *,
    focus_route_id: Optional[str] = None,
) -> DirectionReadinessResult:
    if summary is None:
        return missing_service_route_context_result(route_id=focus_route_id)

    blockers: List[DirectionBlocker] = []
    notes = list(summary.notes or [])
    present = list(summary.present_direction_ids or [])
    missing = list(summary.missing_direction_ids or [])
    bound = list(summary.bound_direction_ids or [])

    if not present:
        _add_blocker(
            blockers,
            code="no_direction_rows",
            message="Logical route has no direction rows in route_raw.service_route_directions.",
        )
    elif missing:
        _add_blocker(
            blockers,
            code="one_direction_missing",
            message=f"Logical route is missing direction rows for {missing}.",
        )

    if not bound:
        _add_blocker(
            blockers,
            code="no_bound_routes",
            message="Logical route has no bound route_id in either direction slot.",
        )
        _add_blocker(
            blockers,
            code="incomplete_logical_route_binding",
            message="Logical route does not have enough bound route_ids for downstream direction-sensitive work.",
        )
    elif len(bound) == 1:
        _add_blocker(
            blockers,
            code="only_one_bound_route",
            message=f"Only one direction slot has a bound route_id: {bound}.",
        )
        _add_blocker(
            blockers,
            code="incomplete_logical_route_binding",
            message="Logical route does not yet have both direction slots bound.",
        )

    if summary.legacy_direction_context_suspect:
        _add_blocker(
            blockers,
            code="legacy_direction_context_untrusted",
            message="Underlying route_job / route_prod direction context does not cleanly match the logical slots.",
        )

    if not notes and not blockers:
        notes.append("Both direction slots are present and structurally bound.")

    evidence_summary = {
        "present_direction_ids": present,
        "missing_direction_ids": missing,
        "bound_direction_ids": bound,
        "legacy_direction_context_suspect": bool(summary.legacy_direction_context_suspect),
        "direction_row_count": len(present),
    }
    return DirectionReadinessResult(
        service_route_id=summary.service_route_id,
        route_short_name=summary.route_short_name,
        route_label=summary.route_label,
        route_name=summary.route_name,
        operator_name=summary.operator_name,
        focus_route_id=focus_route_id,
        route_id_0=summary.route_id_0,
        route_id_1=summary.route_id_1,
        present_direction_ids=present,
        missing_direction_ids=missing,
        is_direction_ready=(len(blockers) == 0),
        blockers=blockers,
        blocker_codes=[b.code for b in blockers],
        blocker_messages=[b.message for b in blockers],
        notes=notes,
        evidence_summary=evidence_summary,
        service_route=summary,
    )

