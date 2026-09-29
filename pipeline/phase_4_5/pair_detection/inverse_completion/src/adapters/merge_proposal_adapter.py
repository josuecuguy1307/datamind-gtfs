from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from hades.geometry.canonical import PLAUSIBLE_PAIR_SCORE, RELIABLE_PAIR_SCORE
from pipeline.phase_4_5.pair_detection.merge_evidence import RoutePairEvidenceExtractor
from pipeline.phase_4_5.pair_detection.merge_scoring import score_route_pair_evidence

from ..core.models import DirectionReadinessResult, InverseProposalSnapshot
from ..db.inventory_repo import list_inventory_bound_route_ids


PROPOSAL_SOURCE = "merge_assist_v1"

_RELIABLE_THRESHOLDS = {
    "merge_readiness_score": 0.72,
    "opposite_direction_score": RELIABLE_PAIR_SCORE,
    "same_route_family_score": 0.55,
}

_PLAUSIBLE_THRESHOLDS = {
    "merge_readiness_score": 0.48,
    "opposite_direction_score": PLAUSIBLE_PAIR_SCORE,
    "same_route_family_score": 0.40,
}

_RELIABLE_BLOCKING_FLAGS = {
    "branch/loop suspicion",
    "low evidence coverage",
    "weak same-route-family signal",
    "weak opposite-direction signal",
}


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _target_slots(result: DirectionReadinessResult) -> List[int]:
    slots: List[int] = []
    for direction_id, route_id in ((0, result.route_id_0), (1, result.route_id_1)):
        if route_id:
            continue
        slots.append(int(direction_id))
    return slots


def _anchor_route_for_slot(result: DirectionReadinessResult, direction_id: int) -> Optional[str]:
    if int(direction_id) == 0:
        return result.route_id_1 or result.route_id_0
    return result.route_id_0 or result.route_id_1


def _candidate_scores_payload(scored: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "same_route_family_score": round(_as_float(scored.get("same_route_family_score")), 4),
        "opposite_direction_score": round(_as_float(scored.get("opposite_direction_score")), 4),
        "merge_readiness_score": round(_as_float(scored.get("merge_readiness_score")), 4),
        "gate_state": scored.get("gate_state"),
    }


def _classify_candidate(scored: Dict[str, Any], *, target_direction_id: int) -> Optional[str]:
    """Classify a candidate by gate state + score thresholds.

    The legacy ``target_direction_id`` argument used to filter candidates by
    a heuristic A=0/B=1 slot assignment. Phase 4.5 removes that heuristic —
    direction-id slotting is now done in stage 5 (``classification.py``)
    based on geographic convention. ``target_direction_id`` is retained for
    signature compatibility but no longer filters; callers will receive the
    same status regardless of which slot they ask for.
    """
    del target_direction_id  # no longer used; legacy heuristic removed
    same_family = _as_float(scored.get("same_route_family_score"))
    opposite = _as_float(scored.get("opposite_direction_score"))
    readiness = _as_float(scored.get("merge_readiness_score"))
    review_flags = {str(flag).strip().lower() for flag in (scored.get("review_flags") or []) if str(flag).strip()}

    reliable = (
        readiness >= float(_RELIABLE_THRESHOLDS["merge_readiness_score"])
        and opposite >= float(_RELIABLE_THRESHOLDS["opposite_direction_score"])
        and same_family >= float(_RELIABLE_THRESHOLDS["same_route_family_score"])
        and not review_flags.intersection({flag.lower() for flag in _RELIABLE_BLOCKING_FLAGS})
    )
    if reliable:
        return "reliable_opposite_candidate"

    plausible = (
        readiness >= float(_PLAUSIBLE_THRESHOLDS["merge_readiness_score"])
        and opposite >= float(_PLAUSIBLE_THRESHOLDS["opposite_direction_score"])
        and same_family >= float(_PLAUSIBLE_THRESHOLDS["same_route_family_score"])
    )
    if plausible:
        return "plausible_opposite_candidate"
    return None


def _best_candidate_for_slot(
    extractor: RoutePairEvidenceExtractor,
    *,
    anchor_route_id: str,
    target_direction_id: int,
    candidate_route_ids: Sequence[str],
) -> Dict[str, Any]:
    anchor_profile = extractor.load_route_profile(anchor_route_id)
    best_scored: Optional[Dict[str, Any]] = None
    best_status: Optional[str] = None
    scored_count = 0
    candidate_matches = 0

    for candidate_route_id in list(candidate_route_ids or []):
        candidate_id = str(candidate_route_id or "").strip()
        if not candidate_id or candidate_id == str(anchor_route_id):
            continue
        candidate_profile = extractor.load_route_profile(candidate_id)
        scored = score_route_pair_evidence(
            extractor.extract_route_pair_evidence_from_profiles(anchor_profile, candidate_profile)
        )
        scored_count += 1
        status = _classify_candidate(scored, target_direction_id=int(target_direction_id))
        if not status:
            continue
        candidate_matches += 1
        candidate_scores = _candidate_scores_payload(scored)
        ranking_key = (
            2 if status == "reliable_opposite_candidate" else 1,
            candidate_scores.get("merge_readiness_score") or 0.0,
            candidate_scores.get("opposite_direction_score") or 0.0,
            candidate_scores.get("same_route_family_score") or 0.0,
        )
        if best_scored is None:
            best_scored = dict(scored)
            best_scored["_ranking_key"] = ranking_key
            best_status = status
            continue
        if ranking_key > tuple(best_scored.get("_ranking_key") or (0, 0.0, 0.0, 0.0)):
            best_scored = dict(scored)
            best_scored["_ranking_key"] = ranking_key
            best_status = status

    return {
        "scored_count": int(scored_count),
        "candidate_matches": int(candidate_matches),
        "status": best_status,
        "scored": best_scored,
    }


def analyze_inverse_proposals_for_results(
    results: Iterable[DirectionReadinessResult],
) -> List[InverseProposalSnapshot]:
    readiness_results = [row for row in list(results or []) if isinstance(row, DirectionReadinessResult)]
    if not readiness_results:
        return []

    extractor = RoutePairEvidenceExtractor()
    proposal_evaluated_at = _generated_at()
    out: List[InverseProposalSnapshot] = []

    for result in readiness_results:
        service_route_id = str(result.service_route_id or "").strip()
        if not service_route_id:
            continue

        target_slots = _target_slots(result)
        blocked_by_proposal = not bool(result.is_direction_ready)
        if not blocked_by_proposal:
            continue

        inventory_route_ids = list_inventory_bound_route_ids(exclude_service_route_id=service_route_id)
        excluded = {
            rid
            for rid in (result.route_id_0, result.route_id_1)
            if str(rid or "").strip()
        }
        candidate_route_ids = [
            rid for rid in list(inventory_route_ids or [])
            if str(rid or "").strip() and str(rid) not in excluded
        ]

        for direction_id in target_slots:
            anchor_route_id = _anchor_route_for_slot(result, int(direction_id))
            if not anchor_route_id:
                out.append(
                    InverseProposalSnapshot(
                        service_route_id=service_route_id,
                        direction_id=int(direction_id),
                        anchor_route_id=None,
                        bound_route_id=None,
                        top_candidate_route_id=None,
                        proposal_status="no_candidate_found",
                        top_candidate_scores={},
                        proposal_payload={
                            "reason": "no_anchor_route",
                            "candidate_route_pool_size": len(candidate_route_ids),
                        },
                        proposal_source=PROPOSAL_SOURCE,
                        proposal_evaluated_at=proposal_evaluated_at,
                        direction_ready=bool(result.is_direction_ready),
                        blocker_codes=list(result.blocker_codes or []),
                        blocker_messages=list(result.blocker_messages or []),
                    )
                )
                continue

            best = _best_candidate_for_slot(
                extractor,
                anchor_route_id=str(anchor_route_id),
                target_direction_id=int(direction_id),
                candidate_route_ids=candidate_route_ids,
            )
            scored = dict(best.get("scored") or {})
            status = str(best.get("status") or "no_candidate_found")
            if status not in {"reliable_opposite_candidate", "plausible_opposite_candidate"}:
                status = "no_candidate_found"
                scored = {}

            top_candidate_route_id = None
            top_candidate_scores: Dict[str, Any] = {}
            proposal_payload: Dict[str, Any] = {
                "anchor_route_id": str(anchor_route_id),
                "target_direction_id": int(direction_id),
                "candidate_route_pool_size": len(candidate_route_ids),
                "pairs_evaluated": int(best.get("scored_count") or 0),
                "matching_candidates": int(best.get("candidate_matches") or 0),
            }
            if scored:
                top_candidate_route_id = str(scored.get("route_b_id") or "").strip() or None
                top_candidate_scores = _candidate_scores_payload(scored)
                proposal_payload["top_proposal"] = {
                    key: value
                    for key, value in scored.items()
                    if key != "_ranking_key"
                }

            out.append(
                InverseProposalSnapshot(
                    service_route_id=service_route_id,
                    direction_id=int(direction_id),
                    anchor_route_id=str(anchor_route_id),
                    bound_route_id=None,
                    top_candidate_route_id=top_candidate_route_id,
                    proposal_status=status,
                    top_candidate_scores=top_candidate_scores,
                    proposal_payload=proposal_payload,
                    proposal_source=PROPOSAL_SOURCE,
                    proposal_evaluated_at=proposal_evaluated_at,
                    direction_ready=bool(result.is_direction_ready),
                    blocker_codes=list(result.blocker_codes or []),
                    blocker_messages=list(result.blocker_messages or []),
                )
            )

    return out
