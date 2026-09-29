from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config

try:
    from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.extraction_policy import (
        score_extraction_quality as _score_phase1_extraction_policy,
    )
except Exception:  # pragma: no cover
    _score_phase1_extraction_policy = None


def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _ratio(a: Any, b: Any) -> Optional[float]:
    aa = _f(a)
    bb = _f(b)
    if aa is None or bb is None or bb <= 0:
        return None
    return aa / bb


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _to_score01(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    return _clip01(x)


def _weighted_score(
    *,
    component_values: Dict[str, Optional[float]],
    weights: Dict[str, float],
) -> Tuple[Optional[float], List[Dict[str, Any]]]:
    used = 0.0
    total = 0.0
    breakdown: List[Dict[str, Any]] = []
    for key, weight in weights.items():
        v = component_values.get(key)
        if v is None:
            breakdown.append(
                {
                    "component": key,
                    "weight": float(weight),
                    "value_0_1": None,
                    "contribution_0_100": None,
                }
            )
            continue
        vv = _clip01(v)
        used += float(weight)
        total += vv * float(weight)
        breakdown.append(
            {
                "component": key,
                "weight": float(weight),
                "value_0_1": round(vv, 4),
                "contribution_0_100": round(vv * float(weight) * 100.0, 2),
            }
        )
    if used <= 0:
        return None, breakdown
    score = (total / used) * 100.0
    return round(score, 2), breakdown


def to_float(value: Any) -> Optional[float]:
    return _f(value)


def to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    txt = str(value).strip().lower()
    if txt in {"1", "true", "yes", "y", "on"}:
        return True
    if txt in {"0", "false", "no", "n", "off"}:
        return False
    return None


def normalize_score_01(raw_score: Any) -> Optional[float]:
    parsed = _f(raw_score)
    if parsed is None:
        return None
    if 0.0 <= float(parsed) <= 1.0:
        return _clip01(float(parsed))
    if 0.0 <= float(parsed) <= 100.0:
        return _clip01(float(parsed) / 100.0)
    return _clip01(float(parsed))


def normalize_score_100(raw_score: Any) -> Optional[float]:
    parsed = _f(raw_score)
    if parsed is None:
        return None
    value = float(parsed)
    if 0.0 <= value <= 1.0:
        value = value * 100.0
    return max(0.0, min(100.0, value))


def weighted_score_01(values: Sequence[tuple[Optional[float], float]]) -> Optional[float]:
    total = 0.0
    used = 0.0
    for raw_value, weight in list(values or []):
        if raw_value is None:
            continue
        used += float(weight)
        total += _clip01(float(raw_value)) * float(weight)
    if used <= 0:
        return None
    return round(total / used, 4)


def pick_quality_score_01(
    candidates: Iterable[Any],
    *,
    fallback_validator_status: Optional[str] = None,
) -> Optional[float]:
    for raw in candidates:
        parsed = normalize_score_01(raw)
        if parsed is not None:
            return parsed
    if str(fallback_validator_status or "").strip().lower() == "pass":
        return 1.0
    if str(fallback_validator_status or "").strip():
        return 0.45
    return None


def pick_sequence_quality_score_100(candidates: Iterable[Any]) -> Optional[float]:
    for raw in candidates:
        parsed = normalize_score_100(raw)
        if parsed is not None:
            return round(parsed, 4)
    return None


def compute_extractor_efficiency_health_score(
    *,
    attempt_count_total: Any,
    retry_diversity_count: Any,
    same_config_repeat_count: Any,
    non_empty_attempt_count: Any,
    successful_attempt_count: Any,
    fallback_used: Any,
    fallback_rescue_success: Any,
) -> float:
    attempts = max(0, int(to_int(attempt_count_total) or 0))
    diversity = max(0, int(to_int(retry_diversity_count) or 0))
    repeats = max(0, int(to_int(same_config_repeat_count) or 0))
    non_empty = max(0, int(to_int(non_empty_attempt_count) or 0))
    successes = max(0, int(to_int(successful_attempt_count) or 0))
    fallback_enabled = bool(fallback_used)
    rescue_success = to_bool(fallback_rescue_success)

    score = 1.0
    score -= min(0.36, max(0, attempts - 1) * 0.09)
    score -= min(0.30, repeats * 0.10)
    if attempts > 1 and diversity <= 1:
        score -= 0.12
    if non_empty <= 0:
        score -= 0.20
    if successes <= 0:
        score -= 0.24
    if fallback_enabled and rescue_success is False:
        score -= 0.12
    elif fallback_enabled and rescue_success is True:
        score += 0.04
    return round(_clip01(score), 4)


def compute_completion_quality_score(
    *,
    phase: str,
    non_empty_extraction: Any = None,
    phase1_quality_score: Any = None,
    downstream_resolve_rate: Any = None,
    semantic_place_set_present: Any = None,
    semantic_substep_completion_rate: Any = None,
    semantic_failed_stage: Any = None,
) -> Optional[float]:
    phase_norm = str(phase or "").strip().lower()
    if phase_norm == "phase1":
        return weighted_score_01(
            [
                (1.0 if bool(non_empty_extraction) else 0.0, 0.30),
                (normalize_score_01(phase1_quality_score), 0.45),
                (normalize_score_01(downstream_resolve_rate), 0.25),
            ]
        )
    if phase_norm == "phase2":
        return weighted_score_01(
            [
                (1.0 if bool(semantic_place_set_present) else 0.0, 0.35),
                (normalize_score_01(semantic_substep_completion_rate), 0.40),
                (0.0 if str(semantic_failed_stage or "").strip() else 1.0, 0.25),
            ]
        )
    return None


def compute_order_completion_quality_score(
    *,
    step20_available: Any,
    sequence_quality_score: Any = None,
    matched_count: Any = None,
    unmatched_count: Any = None,
    ambiguous_count: Any = None,
    route_candidate_count: Any = None,
    prior_stop_count: Any = None,
    step20_gate_passed: Any = None,
) -> Optional[float]:
    if bool(step20_available):
        matched = to_int(matched_count)
        unmatched = to_int(unmatched_count)
        ambiguous = to_int(ambiguous_count)
        total = None
        if matched is not None or unmatched is not None or ambiguous is not None:
            total = int((matched or 0) + (unmatched or 0) + (ambiguous or 0))
        match_ratio = None
        if total is not None and total > 0 and matched is not None:
            match_ratio = float(matched) / float(total)
        gate_pass = to_bool(step20_gate_passed)
        return weighted_score_01(
            [
                (normalize_score_01(sequence_quality_score), 0.45),
                (normalize_score_01(match_ratio), 0.35),
                ((1.0 if gate_pass else 0.0) if gate_pass is not None else None, 0.20),
            ]
        )

    route_candidate_norm = None
    route_candidates = to_int(route_candidate_count)
    if route_candidates is not None:
        route_candidate_norm = _clip01(float(route_candidates) / 8.0)
    prior_stop_norm = None
    prior_stops = to_int(prior_stop_count)
    if prior_stops is not None:
        prior_stop_norm = _clip01(float(prior_stops) / 8.0)
    return weighted_score_01(
        [
            (route_candidate_norm, 0.65),
            (prior_stop_norm, 0.35),
        ]
    )


def validate_warnings(
    warnings: list[Any],
    evidence: dict[str, Any],
) -> list[str]:
    phase = str((evidence or {}).get("phase") or "").strip().lower()
    step_id = str((evidence or {}).get("step_id") or "").strip()
    attempt_count_total = int(to_int((evidence or {}).get("attempt_count_total")) or 0)
    same_config_repeat_count = int(to_int((evidence or {}).get("same_config_repeat_count")) or 0)
    non_empty_attempt_count = int(to_int((evidence or {}).get("non_empty_attempt_count")) or 0)
    target_option_received = bool((evidence or {}).get("target_option_received"))
    failed_stage = str((evidence or {}).get("failed_stage") or "").strip()

    allowed: list[str] = []
    for raw in list(warnings or []):
        code = str(raw or "").strip()
        if not code:
            continue
        if code == "repeated_empty_extraction" and (
            attempt_count_total < 2 or non_empty_attempt_count > 0
        ):
            continue
        if code in {"same_config_retry_loop", "same_config_loop_risk", "retry_not_diversified"} and (
            attempt_count_total < 2 or same_config_repeat_count < 1
        ):
            continue
        if code in {
            "spatial_interpretation_failed",
            "target_intent_ignored",
            "spatial_plan_reused_without_change",
        } and not target_option_received:
            continue
        if code in {
            "extractor_low_completion_usefulness",
            "extraction_success_but_step20_poor",
            "repeated_corridor_extractor_failure",
        } and phase not in {"phase1", "phase3"}:
            continue
        if code == "semantic_pipeline_failed" and (phase != "phase2" or not failed_stage):
            continue
        if code == "attempt_history_gap" and attempt_count_total < 2:
            continue
        allowed.append(code)
    return list(dict.fromkeys(allowed))


def score_phase1_quality(metrics: Dict[str, Any]) -> Dict[str, Any]:
    if callable(_score_phase1_extraction_policy):
        try:
            out = _score_phase1_extraction_policy(
                metrics,
                area_group=metrics.get("area_group"),
            )
            return {
                "score": out.get("score"),
                "score_ready": out.get("score_ready"),
                "score_provisional": out.get("score_provisional"),
                "breakdown": list(out.get("breakdown") or []),
                "warnings": list(out.get("warnings") or []),
                "diagnostics": dict(out.get("diagnostics") or {}),
                "components_raw": dict(out.get("components_raw") or {}),
            }
        except Exception:
            # Fall through to stable legacy scoring if policy module errors.
            pass

    raw_count = _f(metrics.get("raw_count"))
    candidate_count = _f(metrics.get("candidate_count"))
    resolved_count = _f(metrics.get("resolved_count"))
    approved_count = _f(metrics.get("approved_count"))
    stop_signal_count = _f(metrics.get("stop_signal_count"))
    poi_signal_count = _f(metrics.get("poi_signal_count"))
    cluster_count = _f(metrics.get("cluster_count"))
    singleton_count = _f(metrics.get("singleton_count"))
    ambiguity_proxy = _f(metrics.get("ambiguity_proxy_count"))

    candidate_yield = _ratio(candidate_count, raw_count)
    # Yield sweet spot around 0.18-0.40; saturate above it.
    candidate_yield_s = None if candidate_yield is None else _to_score01(candidate_yield / 0.40)

    resolve_rate = _ratio(resolved_count, candidate_count)
    approval_rate = _ratio(approved_count, resolved_count)

    total_signal = None
    if stop_signal_count is not None or poi_signal_count is not None:
        total_signal = float((stop_signal_count or 0.0) + (poi_signal_count or 0.0))
    stop_ratio = _ratio(stop_signal_count, total_signal)
    # Prefer STOP-heavy extraction but avoid degenerate extremes.
    stop_signal_quality = None
    if stop_ratio is not None:
        stop_signal_quality = _clip01(1.0 - abs(stop_ratio - 0.72) / 0.72)

    cluster_total = None
    if cluster_count is not None or singleton_count is not None:
        cluster_total = float((cluster_count or 0.0) + (singleton_count or 0.0))
    cluster_resolution = _ratio(resolved_count, cluster_total)

    ambiguity_ratio = _ratio(ambiguity_proxy, resolved_count if resolved_count else candidate_count)
    ambiguity_inverse = None if ambiguity_ratio is None else _clip01(1.0 - min(1.0, ambiguity_ratio))

    component_values = {
        "candidate_yield": candidate_yield_s,
        "resolve_rate": _to_score01(resolve_rate),
        "approval_rate": _to_score01(approval_rate),
        "stop_signal_quality": stop_signal_quality,
        "cluster_resolution": _to_score01(cluster_resolution),
        "ambiguity_inverse": ambiguity_inverse,
    }
    score, breakdown = _weighted_score(component_values=component_values, weights=config.PHASE1_SCORE_WEIGHTS)

    warnings: List[str] = []
    if candidate_yield is not None and candidate_yield < 0.03:
        warnings.append("Very low candidate yield from extraction.")
    if resolve_rate is not None and resolve_rate < 0.35:
        warnings.append("Low resolve rate; clustering/feature quality may be weak.")
    if approval_rate is not None and approval_rate < 0.45:
        warnings.append("Low approval rate after resolve.")
    if ambiguity_ratio is not None and ambiguity_ratio > 0.25:
        warnings.append("High ambiguity/work queue proxy; review burden likely elevated.")

    return {
        "score": score,
        "breakdown": breakdown,
        "warnings": warnings,
        "components_raw": {
            "candidate_yield": candidate_yield,
            "resolve_rate": resolve_rate,
            "approval_rate": approval_rate,
            "stop_ratio": stop_ratio,
            "cluster_resolution": cluster_resolution,
            "ambiguity_ratio": ambiguity_ratio,
        },
    }


def score_phase3_quality(metrics: Dict[str, Any]) -> Dict[str, Any]:
    prior_count = _f(metrics.get("prior_stop_count"))
    matched_count = _f(metrics.get("matched_count"))
    unmatched_count = _f(metrics.get("unmatched_count"))
    ambiguous_count = _f(metrics.get("ambiguous_count"))
    sequence_quality_score = _f(metrics.get("sequence_quality_score"))
    gate_pass = bool(metrics.get("sequence_gate_pass"))
    rerun_count = _f(metrics.get("rerun_count"))
    warning_subtypes = dict(metrics.get("warning_subtypes") or metrics.get("sequence_warning_subtypes") or {})
    dominant_cause = str(
        metrics.get("step20_dominant_cause")
        or metrics.get("blocker_origin_hint")
        or metrics.get("dominant_cause")
        or ""
    ).strip().lower()
    threshold_profile_version = str(
        metrics.get("sequence_diagnostic_profile_version")
        or ((metrics.get("threshold_profile") or {}) if isinstance(metrics.get("threshold_profile"), dict) else {}).get("version")
        or ""
    ).strip()
    quality_warning_score = float(config.SEQUENCE_DIAGNOSTIC_THRESHOLDS.get("sequence_quality_warning_score", 70.0))

    match_rate = _ratio(matched_count, prior_count)
    unmatched_ratio = _ratio(unmatched_count, prior_count)
    ambiguous_ratio = _ratio(ambiguous_count, prior_count)

    unmatched_inverse = None if unmatched_ratio is None else _clip01(1.0 - min(1.0, unmatched_ratio))
    ambiguous_inverse = None if ambiguous_ratio is None else _clip01(1.0 - min(1.0, ambiguous_ratio))
    sequence_quality = None
    if sequence_quality_score is not None:
        sequence_quality = _clip01(sequence_quality_score / 100.0)
    sequence_gate = 1.0 if gate_pass else 0.0
    rerun_inverse = None
    if rerun_count is not None:
        rerun_inverse = _clip01(1.0 - min(1.0, rerun_count / 4.0))

    component_values = {
        "match_rate": _to_score01(match_rate),
        "unmatched_inverse": unmatched_inverse,
        "ambiguous_inverse": ambiguous_inverse,
        "sequence_quality": sequence_quality,
        "sequence_gate": sequence_gate,
        "rerun_inverse": rerun_inverse,
    }
    score, breakdown = _weighted_score(component_values=component_values, weights=config.PHASE3_SCORE_WEIGHTS)

    warnings: List[str] = []
    if match_rate is not None and match_rate < 0.70:
        warnings.append("Low prior-stop match rate.")
    if unmatched_ratio is not None and unmatched_ratio > 0.20:
        warnings.append("High unmatched stop ratio.")
    if ambiguous_ratio is not None and ambiguous_ratio > 0.15:
        warnings.append("High ambiguous stop ratio.")
    if sequence_quality_score is not None and sequence_quality_score < quality_warning_score:
        warnings.append("Sequence quality detector flagged potential reorder risk.")
    if dominant_cause == "detector_thresholds":
        warnings.append("Detector threshold sensitivity likely contributed to warning pressure.")
    if warning_subtypes.get("detector_threshold_noise_candidate"):
        warnings.append("Potential threshold-noise candidate detected in Step20 diagnostics.")

    return {
        "score": score,
        "breakdown": breakdown,
        "warnings": warnings,
        "components_raw": {
            "match_rate": match_rate,
            "unmatched_ratio": unmatched_ratio,
            "ambiguous_ratio": ambiguous_ratio,
            "sequence_quality_score": sequence_quality_score,
            "rerun_count": rerun_count,
            "dominant_cause": (dominant_cause or None),
            "warning_subtypes": warning_subtypes,
            "sequence_diagnostic_profile_version": (threshold_profile_version or None),
        },
    }
