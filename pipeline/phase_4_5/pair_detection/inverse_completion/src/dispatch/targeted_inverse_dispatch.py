from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pipeline.phase_4_5.pair_detection.merge_evidence import RoutePairEvidenceExtractor

from ..core.models import (
    DirectionReadinessResult,
    PersistedDirectionReadinessRow,
    TargetedInverseSearchRequest,
    TargetedInverseSearchResult,
)


_ELIGIBLE_INVERSE_STATUSES = {
    "structurally_blocked",
    "plausible_opposite_candidate",
    "no_candidate_found",
}

_BLOCKED_SEARCH_STATUSES = {
    "pending",
    "dispatched",
    "discovered",
    "materialized",
}


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dedupe_texts(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        txt = str(raw or "").strip()
        if not txt or txt in seen:
            continue
        seen.add(txt)
        out.append(txt)
    return out


def _bbox_from_points(points: Iterable[Any], *, expand_pct: float = 0.15) -> Dict[str, float]:
    lats: List[float] = []
    lons: List[float] = []
    for point in list(points or []):
        lat = None
        lon = None
        if isinstance(point, dict):
            lat = point.get("lat")
            lon = point.get("lon")
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            lon = point[0]
            lat = point[1]
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except Exception:
            continue
        lats.append(lat_f)
        lons.append(lon_f)
    if not lats or not lons:
        return {}
    south = min(lats)
    north = max(lats)
    west = min(lons)
    east = max(lons)
    lat_pad = max(0.001, (north - south) * float(expand_pct or 0.0))
    lon_pad = max(0.001, (east - west) * float(expand_pct or 0.0))
    return {
        "south": float(south - lat_pad),
        "west": float(west - lon_pad),
        "north": float(north + lat_pad),
        "east": float(east + lon_pad),
    }


def _bbox_tuple(bbox: Dict[str, Any]) -> Tuple[float, float, float, float]:
    return (
        float(bbox["south"]),
        float(bbox["west"]),
        float(bbox["north"]),
        float(bbox["east"]),
    )


def _inverse_name_hint(profile: Dict[str, Any]) -> Optional[str]:
    from_name = str(profile.get("relation_from") or "").strip()
    to_name = str(profile.get("relation_to") or "").strip()
    if from_name and to_name:
        return f"{to_name} - {from_name}"
    return (
        str(profile.get("relation_name") or "").strip()
        or str(profile.get("route_name") or "").strip()
        or None
    )


def _anchor_route_id(result: DirectionReadinessResult, direction_id: int) -> Optional[str]:
    if int(direction_id) == 0:
        return result.route_id_1 or result.route_id_0
    return result.route_id_0 or result.route_id_1


def evaluate_targeted_inverse_search_eligibility(
    *,
    structural: DirectionReadinessResult,
    persisted_row: Optional[PersistedDirectionReadinessRow],
    direction_id: int,
    force: bool = False,
) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    if bool(structural.is_direction_ready):
        reasons.append("direction_already_ready")

    target_route_id = structural.route_id_0 if int(direction_id) == 0 else structural.route_id_1
    if target_route_id:
        reasons.append("direction_slot_already_bound")

    if int(direction_id) not in (0, 1):
        reasons.append("invalid_direction_id")

    inverse_status = str((persisted_row.inverse_status if persisted_row else "") or "").strip() or "unknown"
    if inverse_status not in _ELIGIBLE_INVERSE_STATUSES and not force:
        reasons.append("inverse_status_not_dispatchable")

    search_status = str((persisted_row.search_status if persisted_row else "") or "").strip() or "not_started"
    if search_status in _BLOCKED_SEARCH_STATUSES and not force:
        reasons.append("search_already_attempted_or_active")

    if not _anchor_route_id(structural, int(direction_id)):
        reasons.append("anchor_route_missing")

    return (len(reasons) == 0, reasons)


def build_targeted_inverse_search_request(
    *,
    phase3_client: Any,
    structural: DirectionReadinessResult,
    persisted_row: Optional[PersistedDirectionReadinessRow],
    service_route_id: str,
    direction_id: int,
) -> TargetedInverseSearchRequest:
    anchor_route_id = _anchor_route_id(structural, int(direction_id))
    if not anchor_route_id:
        raise RuntimeError("Targeted inverse search requires an anchor route.")

    anchor_job = phase3_client.get_route_job(anchor_route_id)
    extractor = RoutePairEvidenceExtractor()
    profile = extractor.load_route_profile(anchor_route_id)

    base_bbox = phase3_client.build_phase3_extract_bbox(
        bbox=dict(anchor_job.get("bbox") or {}),
        group_hint="inverse_completion",
        priority="high",
        extra_expand_pct=0.15,
    )
    if not base_bbox:
        point_seed = list(profile.get("prior_rows") or []) or list(profile.get("geometry_points") or [])
        base_bbox = _bbox_from_points(point_seed, expand_pct=0.15)
        if base_bbox:
            base_bbox = phase3_client.build_phase3_extract_bbox(
                bbox=base_bbox,
                group_hint="inverse_completion",
                priority="high",
                extra_expand_pct=0.0,
            ) or base_bbox
    if not base_bbox:
        raise RuntimeError("Targeted inverse search requires anchor bbox evidence.")

    refs = _dedupe_texts(
        [
            anchor_job.get("known_ref"),
            profile.get("route_ref"),
            profile.get("relation_ref"),
            (persisted_row.route_short_name if persisted_row else None),
        ]
    )
    operator = (
        str(profile.get("operator_name") or "").strip()
        or str(profile.get("relation_operator") or "").strip()
        or None
    )
    name_hint = _inverse_name_hint(profile)
    route_hint_raw = name_hint or (str(profile.get("route_name") or "").strip() or None)
    cooperative_hint = (
        str(profile.get("route_name") or "").strip()
        or str(profile.get("relation_name") or "").strip()
        or f"inverse:{anchor_route_id}"
    )

    return TargetedInverseSearchRequest(
        service_route_id=str(service_route_id),
        direction_id=int(direction_id),
        anchor_route_id=str(anchor_route_id),
        bbox=dict(base_bbox or {}),
        refs=refs,
        operator=operator,
        name=name_hint,
        route_hint_raw=route_hint_raw,
        cooperative_hint=cooperative_hint,
        target_group="inverse_completion",
        target_priority="inverse_search",
        target_attempt_type="targeted_inverse",
        target_seed_origin="inverse_search",
        source_document=f"inverse_completion:{service_route_id}:{int(direction_id)}",
        target_place_bundle=f"service_route:{service_route_id}",
    )


def dispatch_targeted_inverse_search(
    *,
    phase3_client: Any,
    structural: DirectionReadinessResult,
    persisted_row: Optional[PersistedDirectionReadinessRow],
    service_route_id: str,
    direction_id: int,
    force: bool = False,
) -> TargetedInverseSearchResult:
    eligible, reasons = evaluate_targeted_inverse_search_eligibility(
        structural=structural,
        persisted_row=persisted_row,
        direction_id=int(direction_id),
        force=bool(force),
    )
    current_inverse_status = str((persisted_row.inverse_status if persisted_row else "") or "").strip() or None
    current_search_status = str((persisted_row.search_status if persisted_row else "") or "").strip() or "not_started"
    if not eligible:
        return TargetedInverseSearchResult(
            service_route_id=str(service_route_id),
            direction_id=int(direction_id),
            eligible=False,
            launched=False,
            direction_ready=bool(structural.is_direction_ready),
            inverse_status=current_inverse_status,
            search_status=current_search_status,
            anchor_route_id=_anchor_route_id(structural, int(direction_id)),
            request_payload={},
            result_payload={"eligibility_reasons": reasons},
            dispatched_route_ids=list((persisted_row.dispatched_route_ids if persisted_row else []) or []),
            materialized_route_ids=list((persisted_row.materialized_route_ids if persisted_row else []) or []),
            search_error=None,
            search_started_at=None,
            search_finished_at=None,
            blocker_codes=list(structural.blocker_codes or []),
            blocker_messages=list(structural.blocker_messages or []),
        )

    started_at = _generated_at()
    try:
        request = build_targeted_inverse_search_request(
            phase3_client=phase3_client,
            structural=structural,
            persisted_row=persisted_row,
            service_route_id=str(service_route_id),
            direction_id=int(direction_id),
        )
        request_payload = asdict(request)
    except Exception as exc:
        finished_at = _generated_at()
        return TargetedInverseSearchResult(
            service_route_id=str(service_route_id),
            direction_id=int(direction_id),
            eligible=True,
            launched=False,
            direction_ready=bool(structural.is_direction_ready),
            inverse_status=current_inverse_status,
            search_status="failed",
            anchor_route_id=_anchor_route_id(structural, int(direction_id)),
            request_payload={},
            result_payload={},
            dispatched_route_ids=[],
            materialized_route_ids=[],
            search_error=str(exc),
            search_started_at=started_at,
            search_finished_at=finished_at,
            blocker_codes=list(structural.blocker_codes or []),
            blocker_messages=list(structural.blocker_messages or []),
        )

    try:
        discover = phase3_client.run_step_05_discover(
            route_id=None,
            bbox=_bbox_tuple(request.bbox),
            refs=list(request.refs or []) or None,
            operator=request.operator,
            name=request.name,
            store=True,
            route_hint_raw=request.route_hint_raw,
            cooperative_hint=request.cooperative_hint,
            source_document=request.source_document,
            target_group=request.target_group,
            target_priority=request.target_priority,
            target_attempt_type=request.target_attempt_type,
            target_place_bundle=request.target_place_bundle,
            target_seed_origin=request.target_seed_origin,
        )
    except Exception as exc:
        error_txt = str(exc)
        final_status = "no_results" if "No route relations found" in error_txt else "failed"
        finished_at = _generated_at()
        return TargetedInverseSearchResult(
            service_route_id=str(service_route_id),
            direction_id=int(direction_id),
            eligible=True,
            launched=False,
            direction_ready=bool(structural.is_direction_ready),
            inverse_status=current_inverse_status,
            search_status=final_status,
            anchor_route_id=request.anchor_route_id,
            request_payload=request_payload,
            result_payload={},
            dispatched_route_ids=[],
            materialized_route_ids=[],
            search_error=error_txt,
            search_started_at=started_at,
            search_finished_at=finished_at,
            blocker_codes=list(structural.blocker_codes or []),
            blocker_messages=list(structural.blocker_messages or []),
        )

    dispatched_route_id = str(discover.get("route_id") or "").strip() or None
    dispatched_route_ids = [dispatched_route_id] if dispatched_route_id else []

    try:
        fetch = phase3_client.run_step_10_fetch(
            route_id=dispatched_route_id,
            osm_relation_id=discover.get("chosen_osm_relation_id"),
        )
        finished_at = _generated_at()
        materialized = bool(fetch.get("stored")) or bool(fetch.get("already_stored")) or bool(fetch.get("fetch_relation_stored"))
        final_status = "materialized" if materialized else "discovered"
        materialized_route_ids = list(dispatched_route_ids) if materialized else []
        return TargetedInverseSearchResult(
            service_route_id=str(service_route_id),
            direction_id=int(direction_id),
            eligible=True,
            launched=True,
            direction_ready=bool(structural.is_direction_ready),
            inverse_status=current_inverse_status,
            search_status=final_status,
            anchor_route_id=request.anchor_route_id,
            request_payload=request_payload,
            result_payload={"step05": discover, "step10": fetch},
            dispatched_route_ids=dispatched_route_ids,
            materialized_route_ids=materialized_route_ids,
            search_error=None,
            search_started_at=started_at,
            search_finished_at=finished_at,
            blocker_codes=list(structural.blocker_codes or []),
            blocker_messages=list(structural.blocker_messages or []),
        )
    except Exception as exc:
        finished_at = _generated_at()
        return TargetedInverseSearchResult(
            service_route_id=str(service_route_id),
            direction_id=int(direction_id),
            eligible=True,
            launched=True,
            direction_ready=bool(structural.is_direction_ready),
            inverse_status=current_inverse_status,
            search_status="failed",
            anchor_route_id=request.anchor_route_id,
            request_payload=request_payload,
            result_payload={"step05": discover},
            dispatched_route_ids=dispatched_route_ids,
            materialized_route_ids=[],
            search_error=str(exc),
            search_started_at=started_at,
            search_finished_at=finished_at,
            blocker_codes=list(structural.blocker_codes or []),
            blocker_messages=list(structural.blocker_messages or []),
        )
