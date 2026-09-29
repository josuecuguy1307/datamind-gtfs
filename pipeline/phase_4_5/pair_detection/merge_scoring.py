from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from hades.geometry.canonical import PLAUSIBLE_PAIR_SCORE

from datamind_console.ai_insights import config
from .route_pairing import clip01

_DEFAULT_WEIGHTS = {
    "same_route_family": {
        "exact_stop_overlap_ratio": 0.06,
        "paired_stop_alignment_score": 0.18,
        "shared_middle_corridor_alignment_score": 0.07,
        "shared_corridor_overlap_score": 0.14,
        "path_similarity_score": 0.11,
        "length_ratio_score": 0.06,
        "ref_match_score": 0.07,
        "operator_match_score": 0.05,
        "network_match_score": 0.04,
        "overpass_name_similarity_score": 0.05,
        "relation_tag_consistency_score": 0.05,
        "normalized_route_name_similarity": 0.07,
        "alias_match_score": 0.04,
        "name_family_match_score": 0.11,
    },
    "opposite_direction": {
        "reverse_exact_sequence_similarity": 0.06,
        "paired_stop_alignment_score": 0.12,
        "reverse_order_of_paired_stops_score": 0.22,
        "endpoint_region_swap_score": 0.15,
        "shared_corridor_overlap_score": 0.08,
        "reverse_corridor_progression_score": 0.20,
        "shape_direction_opposition_score": 0.10,
        "from_to_swapped_match_score": 0.07,
        "endpoint_name_swap_similarity": 0.10,
    },
    "penalties": {
        "sequence_quality_penalty_a": 0.18,
        "sequence_quality_penalty_b": 0.18,
        "unmatched_penalty_a": 0.10,
        "unmatched_penalty_b": 0.10,
        "ambiguous_penalty_a": 0.06,
        "ambiguous_penalty_b": 0.06,
        "loop_or_branch_suspicion_penalty": 0.20,
        "low_evidence_coverage_penalty": 0.12,
    },
    "readiness_blend": {
        "same_route_family": 0.42,
        "opposite_direction": 0.38,
        "penalty_inverse": 0.20,
    },
}

_DEFAULT_THRESHOLDS = {
    "low_evidence_coverage_penalty_flag": 0.42,
    "loop_or_branch_suspicion_penalty_flag": 0.35,
    "sequence_quality_penalty_flag": 0.35,
    "direction_word_conflict_penalty": 0.08,
    "minimum_family_for_merge": PLAUSIBLE_PAIR_SCORE,
    "minimum_opposite_for_merge": PLAUSIBLE_PAIR_SCORE,
}


def _weight_group(name: str) -> Dict[str, float]:
    configured = dict((config.MERGE_ASSIST_WEIGHTS or {}).get(name) or {})
    if configured:
        return {str(k): float(v) for k, v in configured.items()}
    return dict(_DEFAULT_WEIGHTS.get(name) or {})


def _thresholds() -> Dict[str, float]:
    configured = dict(getattr(config, "MERGE_ASSIST_THRESHOLDS", {}) or {})
    out = dict(_DEFAULT_THRESHOLDS)
    for key, val in configured.items():
        try:
            out[str(key)] = float(val)
        except Exception:
            continue
    return out


def _weighted_score(
    features: Dict[str, Any],
    weights: Dict[str, float],
) -> Tuple[Optional[float], List[Dict[str, Any]], float]:
    used = 0.0
    acc = 0.0
    breakdown: List[Dict[str, Any]] = []
    for key, weight in weights.items():
        raw = features.get(key)
        val: Optional[float]
        if isinstance(raw, bool):
            val = 1.0 if raw else 0.0
        elif raw is None:
            val = None
        else:
            try:
                val = float(raw)
            except Exception:
                val = None
        if val is None:
            breakdown.append({
                "component": key,
                "weight": float(weight),
                "value_0_1": None,
                "contribution_0_1": None,
            })
            continue
        vv = clip01(val)
        used += float(weight)
        contribution = vv * float(weight)
        acc += contribution
        breakdown.append({
            "component": key,
            "weight": float(weight),
            "value_0_1": round(vv, 4),
            "contribution_0_1": round(contribution, 4),
        })
    if used <= 0:
        return None, breakdown, 0.0
    return clip01(acc / used), breakdown, used


def _penalty_score(features: Dict[str, Any], weights: Dict[str, float]) -> Tuple[Optional[float], List[Dict[str, Any]], float]:
    return _weighted_score(features, weights)


# NOTE: The legacy ``_proposed_direction_assignment`` function (the A=0/B=1
# pair-opposition default) has been removed per Phase 4.5. Direction-id
# assignment is now owned exclusively by ``pipeline.phase_4_5.classification``;
# this module only emits a gate state via ``hard_gate.classify_by_score``.


def score_route_pair_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    features = dict(evidence.get("features") or {})
    thresholds = _thresholds()

    family_weights = _weight_group("same_route_family")
    opposite_weights = _weight_group("opposite_direction")
    penalty_weights = _weight_group("penalties")
    readiness_blend = _weight_group("readiness_blend")

    same_family, family_breakdown, family_used = _weighted_score(features, family_weights)
    opposite, opposite_breakdown, opposite_used = _weighted_score(features, opposite_weights)
    penalty, penalty_breakdown, penalty_used = _penalty_score(features, penalty_weights)

    same_family_score = float(same_family or 0.0)
    opposite_score = float(opposite or 0.0)
    penalty_score = float(penalty or 0.0)

    readiness = (
        float(readiness_blend.get("same_route_family", 0.42)) * same_family_score
        + float(readiness_blend.get("opposite_direction", 0.38)) * opposite_score
        + float(readiness_blend.get("penalty_inverse", 0.20)) * (1.0 - penalty_score)
    )

    if bool(features.get("direction_word_conflict_flag")):
        readiness -= float(thresholds.get("direction_word_conflict_penalty", 0.08))

    readiness = clip01(readiness)

    review_flags: List[str] = []

    seq_pen_a = float(features.get("sequence_quality_penalty_a") or 0.0)
    seq_pen_b = float(features.get("sequence_quality_penalty_b") or 0.0)
    if max(seq_pen_a, seq_pen_b) >= float(thresholds.get("sequence_quality_penalty_flag", 0.35)):
        review_flags.append("fix sequence quality first")

    low_cov_pen = float(features.get("low_evidence_coverage_penalty") or 0.0)
    if low_cov_pen >= float(thresholds.get("low_evidence_coverage_penalty_flag", 0.42)):
        review_flags.append("low evidence coverage")

    loop_pen = float(features.get("loop_or_branch_suspicion_penalty") or 0.0)
    if loop_pen >= float(thresholds.get("loop_or_branch_suspicion_penalty_flag", 0.35)):
        review_flags.append("branch/loop suspicion")

    if bool(features.get("direction_word_conflict_flag")):
        review_flags.append("direction wording conflict")

    if same_family_score < float(thresholds.get("minimum_family_for_merge", PLAUSIBLE_PAIR_SCORE)):
        review_flags.append("weak same-route-family signal")

    if opposite_score < float(thresholds.get("minimum_opposite_for_merge", PLAUSIBLE_PAIR_SCORE)):
        review_flags.append("weak opposite-direction signal")

    from pipeline.phase_4_5.hard_gate import classify_by_score
    gate_state = classify_by_score(opposite_score)

    out = {
        "route_a_id": str(evidence.get("route_a_id") or ""),
        "route_b_id": str(evidence.get("route_b_id") or ""),
        "same_route_family_score": round(same_family_score, 4),
        "opposite_direction_score": round(opposite_score, 4),
        "merge_readiness_score": round(readiness, 4),
        "gate_state": gate_state,
        "review_flags": review_flags,
        "requires_operator_confirmation": True,
        "proposal_only": True,
        "evidence_breakdown": {
            "features": {
                k: (round(float(v), 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                for k, v in features.items()
            },
            "component_scores": {
                "same_route_family": {
                    "score_0_1": (round(float(same_family), 4) if same_family is not None else None),
                    "used_weight": round(float(family_used), 4),
                    "breakdown": family_breakdown,
                },
                "opposite_direction": {
                    "score_0_1": (round(float(opposite), 4) if opposite is not None else None),
                    "used_weight": round(float(opposite_used), 4),
                    "breakdown": opposite_breakdown,
                },
                "penalties": {
                    "score_0_1": (round(float(penalty), 4) if penalty is not None else None),
                    "used_weight": round(float(penalty_used), 4),
                    "breakdown": penalty_breakdown,
                },
                "merge_readiness": {
                    "score_0_1": round(readiness, 4),
                    "blend_weights": readiness_blend,
                    "direction_word_conflict_applied": bool(features.get("direction_word_conflict_flag")),
                },
            },
            "coverage": dict(evidence.get("coverage") or {}),
            "diagnostics": dict(evidence.get("diagnostics") or {}),
        },
        "model_version": "merge_assist_v1",
    }

    return out
