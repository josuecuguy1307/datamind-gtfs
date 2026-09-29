from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from . import config

LonLat = Tuple[float, float]


def _haversine_m(a: LonLat, b: LonLat) -> float:
    lon1, lat1 = float(a[0]), float(a[1])
    lon2, lat2 = float(b[0]), float(b[1])
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.asin(math.sqrt(max(0.0, min(1.0, h))))


def _angle_deg(a: LonLat, b: LonLat, c: LonLat) -> float:
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1 = math.hypot(v1[0], v1[1])
    n2 = math.hypot(v2[0], v2[1])
    if n1 <= 0 or n2 <= 0:
        return 0.0
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    cosang = max(-1.0, min(1.0, dot / (n1 * n2)))
    return float(math.degrees(math.acos(cosang)))


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _threshold_float(
    thresholds: Dict[str, Any],
    key: str,
    fallback: float,
) -> float:
    raw = thresholds.get(key)
    if raw is None:
        return float(fallback)
    try:
        return float(raw)
    except Exception:
        return float(fallback)


def _classify_dominant_cause(
    *,
    unmatched_count: int,
    ambiguous_count: int,
    unmatched_ratio: float,
    ambiguous_ratio: float,
    large_jumps: int,
    very_large_jumps: int,
    backtracks: int,
    gap_count: int,
    reorder_recommended: bool,
    reorder_confidence: float,
    unmatched_ratio_blocking: float,
    ambiguous_ratio_blocking: float,
    sequence_quality_warning_score: float,
    sequence_quality_score: float,
    reorder_pressure_confidence: float,
) -> Dict[str, Any]:
    if ambiguous_count > 0 or ambiguous_ratio >= ambiguous_ratio_blocking:
        return {
            "dominant_cause": "matching_ambiguity",
            "confidence": "high",
            "triage_route": "phase1_new_nodes_resolution",
            "reason": "Ambiguity pressure is high in Step20 matching output.",
        }

    if unmatched_count > 0 or unmatched_ratio >= unmatched_ratio_blocking:
        return {
            "dominant_cause": "node_db_gap",
            "confidence": "high",
            "triage_route": "phase1_new_nodes_resolution",
            "reason": "Unmatched pressure dominates despite sequence analysis completion.",
        }

    if (
        sequence_quality_score < sequence_quality_warning_score
        and unmatched_count <= 0
        and ambiguous_count <= 0
        and (large_jumps + backtracks + gap_count) <= 1
    ):
        return {
            "dominant_cause": "detector_thresholds",
            "confidence": "medium",
            "triage_route": "review_step20_thresholds",
            "reason": "Low score with weak anomaly evidence suggests threshold noise.",
        }

    if reorder_recommended and reorder_confidence >= reorder_pressure_confidence and (
        very_large_jumps > 0 or backtracks > 1 or large_jumps > 1
    ):
        return {
            "dominant_cause": "merge_evidence",
            "confidence": "medium",
            "triage_route": "review_reorder_and_merge_evidence",
            "reason": "Ordering pressure is high from route-shape anomalies.",
        }

    return {
        "dominant_cause": "unknown",
        "confidence": "low",
        "triage_route": "operator_triage",
        "reason": "No dominant cause inferred from current Step20 diagnostics.",
    }


def _sorted_rows(prior_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted((dict(r) for r in (prior_rows or [])), key=lambda r: int(r.get("seq") or 0))


def evaluate_sequence_quality(
    *,
    prior_rows: List[Dict[str, Any]],
    matched_count: Optional[int] = None,
    unmatched_count: Optional[int] = None,
    ambiguous_count: Optional[int] = None,
    sequence_edit_count: Optional[int] = None,
) -> Dict[str, Any]:
    cfg = config.SEQUENCE_HEURISTICS
    diag_cfg = dict(getattr(config, "SEQUENCE_DIAGNOSTIC_THRESHOLDS", {}) or {})
    profile_version = str(getattr(config, "SEQUENCE_DIAGNOSTIC_PROFILE_VERSION", "step20_diag_unknown"))
    unmatched_ratio_warning = _threshold_float(diag_cfg, "unmatched_ratio_warning", 0.08)
    unmatched_ratio_blocking = _threshold_float(diag_cfg, "unmatched_ratio_blocking", 0.20)
    ambiguous_ratio_warning = _threshold_float(diag_cfg, "ambiguous_ratio_warning", 0.06)
    ambiguous_ratio_blocking = _threshold_float(diag_cfg, "ambiguous_ratio_blocking", 0.15)
    sequence_quality_warning_score = _threshold_float(diag_cfg, "sequence_quality_warning_score", 70.0)
    sequence_quality_critical_score = _threshold_float(diag_cfg, "sequence_quality_critical_score", 55.0)
    reorder_pressure_confidence = _threshold_float(diag_cfg, "reorder_pressure_confidence", 0.62)
    rows = _sorted_rows(prior_rows)
    total = len(rows)
    warnings: List[str] = []
    warning_tags: List[str] = []
    warning_subtypes: Counter[str] = Counter()

    if total == 0:
        return {
            "sequence_quality_score": None,
            "warnings": ["No stop-prior rows available for sequence analysis."],
            "warning_tags": ["missing_sequence_data"],
            "warning_subtypes": {"missing_sequence_data": 1},
            "reorder_recommended": False,
            "reorder_confidence": 0.0,
            "dominant_cause": "unknown",
            "dominant_cause_confidence": "low",
            "triage_route": "operator_triage",
            "rationale": "insufficient_sequence_data",
            "details": {},
            "threshold_profile": {
                "version": profile_version,
                "thresholds": {
                    "unmatched_ratio_warning": unmatched_ratio_warning,
                    "unmatched_ratio_blocking": unmatched_ratio_blocking,
                    "ambiguous_ratio_warning": ambiguous_ratio_warning,
                    "ambiguous_ratio_blocking": ambiguous_ratio_blocking,
                    "sequence_quality_warning_score": sequence_quality_warning_score,
                    "sequence_quality_critical_score": sequence_quality_critical_score,
                    "reorder_pressure_confidence": reorder_pressure_confidence,
                },
            },
        }

    seq_values = [int(r.get("seq") or 0) for r in rows]
    gap_count = 0
    for i in range(1, len(seq_values)):
        if (seq_values[i] - seq_values[i - 1]) > int(cfg["gap_seq_allowed"]):
            gap_count += 1
    if gap_count > 0:
        warnings.append(f"Detected {gap_count} sequence gap(s).")
        warning_tags.append("gap_anomaly")
        warning_subtypes["gap_anomaly"] += int(gap_count)

    dup_stops = 0
    seen_stop_ids: set[str] = set()
    for r in rows:
        sid = str(r.get("matched_stop_node_id") or "").strip()
        if not sid:
            continue
        if sid in seen_stop_ids:
            dup_stops += 1
        else:
            seen_stop_ids.add(sid)
    if dup_stops > 0:
        warnings.append(f"Detected {dup_stops} repeated matched stop(s).")
        warning_tags.append("repeated_stops")
        warning_subtypes["repeated_stops"] += int(dup_stops)

    coords: List[LonLat] = []
    for r in rows:
        lat = _safe_float(r.get("lat"))
        lon = _safe_float(r.get("lon"))
        if lat is None or lon is None:
            continue
        coords.append((lon, lat))

    segment_dists: List[float] = []
    large_jumps = 0
    very_large_jumps = 0
    for i in range(1, len(coords)):
        d = _haversine_m(coords[i - 1], coords[i])
        segment_dists.append(d)
        if d >= float(cfg["large_jump_m"]):
            large_jumps += 1
        if d >= float(cfg["very_large_jump_m"]):
            very_large_jumps += 1
    if large_jumps > 0:
        warnings.append(f"Detected {large_jumps} large stop-to-stop jump(s).")
        warning_tags.append("large_jump")
        warning_subtypes["large_jump"] += int(large_jumps)
    if very_large_jumps > 0:
        warning_tags.append("very_large_jump")
        warning_subtypes["very_large_jump"] += int(very_large_jumps)

    backtracks = 0
    for i in range(1, len(coords) - 1):
        a = coords[i - 1]
        b = coords[i]
        c = coords[i + 1]
        d1 = _haversine_m(a, b)
        d2 = _haversine_m(b, c)
        if d1 < float(cfg["backtrack_min_segment_m"]) or d2 < float(cfg["backtrack_min_segment_m"]):
            continue
        ang = _angle_deg(a, b, c)
        if ang >= float(cfg["backtrack_angle_deg"]):
            backtracks += 1
    if backtracks > 0:
        warnings.append(f"Detected {backtracks} potential backtracking turn(s).")
        warning_tags.append("backtracking")
        warning_subtypes["backtracking"] += int(backtracks)

    m = int(matched_count) if matched_count is not None else sum(1 for r in rows if r.get("matched_stop_node_id"))
    u = int(unmatched_count) if unmatched_count is not None else sum(
        1 for r in rows if str(r.get("match_state") or "").lower() == "unmatched"
    )
    a = int(ambiguous_count) if ambiguous_count is not None else sum(
        1 for r in rows if str(r.get("match_state") or "").lower() == "ambiguous"
    )
    denom = max(total, 1)
    unmatched_ratio = float(u) / float(denom)
    ambiguous_ratio = float(a) / float(denom)
    if unmatched_ratio >= unmatched_ratio_warning:
        warning_tags.append("unmatched_ratio_high")
        warning_subtypes["unmatched_ratio_high"] += 1
    if ambiguous_ratio >= ambiguous_ratio_warning:
        warning_tags.append("ambiguous_ratio_high")
        warning_subtypes["ambiguous_ratio_high"] += 1
    if unmatched_ratio >= unmatched_ratio_blocking:
        warning_tags.append("unmatched_blocking_pressure")
        warning_subtypes["unmatched_blocking_pressure"] += 1
    if ambiguous_ratio >= ambiguous_ratio_blocking:
        warning_tags.append("ambiguous_blocking_pressure")
        warning_subtypes["ambiguous_blocking_pressure"] += 1

    score = 100.0
    score -= float(dup_stops) * float(cfg["duplicate_stop_penalty"])
    score -= float(large_jumps) * float(cfg["large_jump_penalty"])
    score -= float(backtracks) * float(cfg["backtrack_penalty"])
    score -= float(gap_count) * float(cfg["gap_penalty"])
    score -= float(unmatched_ratio) * float(cfg["unmatched_ratio_penalty"]) * 100.0 / 100.0
    score -= float(ambiguous_ratio) * float(cfg["ambiguous_ratio_penalty"]) * 100.0 / 100.0
    if sequence_edit_count is not None and int(sequence_edit_count) >= 6:
        score -= float(cfg["edit_noise_penalty"])
        warnings.append("High volume of recent sequence edits suggests instability.")
        warning_tags.append("edit_pressure")
        warning_subtypes["edit_pressure"] += 1
    if very_large_jumps > 0:
        score -= 6.0 * float(very_large_jumps)
    score = max(0.0, min(100.0, score))

    reorder_recommended = bool(
        score < float(cfg["reorder_recommend_below"])
        or dup_stops > 0
        or large_jumps >= 2
        or backtracks >= 2
    )
    severity = max(0.0, min(1.0, (100.0 - score) / 100.0))
    reorder_confidence = 0.20 + 0.60 * severity
    if dup_stops > 0 or backtracks > 1 or very_large_jumps > 0:
        reorder_confidence += 0.15
    reorder_confidence = max(0.0, min(1.0, reorder_confidence))
    if reorder_recommended and reorder_confidence >= reorder_pressure_confidence:
        warning_tags.append("reorder_pressure_high")
        warning_subtypes["reorder_pressure_high"] += 1
    if score < sequence_quality_critical_score:
        warning_tags.append("sequence_quality_critical")
        warning_subtypes["sequence_quality_critical"] += 1
    elif score < sequence_quality_warning_score:
        warning_tags.append("sequence_quality_warning")
        warning_subtypes["sequence_quality_warning"] += 1
    if (
        score < sequence_quality_warning_score
        and unmatched_ratio < unmatched_ratio_warning
        and ambiguous_ratio < ambiguous_ratio_warning
        and (large_jumps + backtracks + gap_count) <= 1
    ):
        warning_tags.append("detector_threshold_noise_candidate")
        warning_subtypes["detector_threshold_noise_candidate"] += 1
    warning_tags = sorted(set(warning_tags))
    cause = _classify_dominant_cause(
        unmatched_count=u,
        ambiguous_count=a,
        unmatched_ratio=unmatched_ratio,
        ambiguous_ratio=ambiguous_ratio,
        large_jumps=large_jumps,
        very_large_jumps=very_large_jumps,
        backtracks=backtracks,
        gap_count=gap_count,
        reorder_recommended=reorder_recommended,
        reorder_confidence=reorder_confidence,
        unmatched_ratio_blocking=unmatched_ratio_blocking,
        ambiguous_ratio_blocking=ambiguous_ratio_blocking,
        sequence_quality_warning_score=sequence_quality_warning_score,
        sequence_quality_score=score,
        reorder_pressure_confidence=reorder_pressure_confidence,
    )

    return {
        "sequence_quality_score": round(score, 2),
        "warnings": warnings,
        "warning_tags": warning_tags,
        "warning_subtypes": {str(k): int(v) for k, v in warning_subtypes.items()},
        "reorder_recommended": reorder_recommended,
        "reorder_confidence": round(reorder_confidence, 3),
        "dominant_cause": cause.get("dominant_cause"),
        "dominant_cause_confidence": cause.get("confidence"),
        "triage_route": cause.get("triage_route"),
        "rationale": (
            "Sequence appears risky; review ordering before geometry." if reorder_recommended else "Sequence looks stable."
        ),
        "details": {
            "total_stops": total,
            "matched_count": m,
            "unmatched_count": u,
            "ambiguous_count": a,
            "duplicate_matched_stops": dup_stops,
            "large_jumps": large_jumps,
            "very_large_jumps": very_large_jumps,
            "backtracks": backtracks,
            "gap_count": gap_count,
            "max_segment_m": (max(segment_dists) if segment_dists else 0.0),
            "median_segment_m": (
                sorted(segment_dists)[len(segment_dists) // 2] if segment_dists else 0.0
            ),
            "unmatched_ratio": round(unmatched_ratio, 4),
            "ambiguous_ratio": round(ambiguous_ratio, 4),
            "dominant_cause_reason": cause.get("reason"),
        },
        "threshold_profile": {
            "version": profile_version,
            "thresholds": {
                "unmatched_ratio_warning": unmatched_ratio_warning,
                "unmatched_ratio_blocking": unmatched_ratio_blocking,
                "ambiguous_ratio_warning": ambiguous_ratio_warning,
                "ambiguous_ratio_blocking": ambiguous_ratio_blocking,
                "sequence_quality_warning_score": sequence_quality_warning_score,
                "sequence_quality_critical_score": sequence_quality_critical_score,
                "reorder_pressure_confidence": reorder_pressure_confidence,
            },
        },
    }
