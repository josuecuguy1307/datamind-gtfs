from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import config


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _parse_ts(v: Any) -> datetime:
    s = str(v or "").strip()
    if not s:
        return datetime.min
    try:
        # tolerate trailing Z
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except Exception:
        return datetime.min


def _feature_completeness(rows: List[Dict[str, Any]], required_keys: List[str]) -> float:
    if not rows:
        return 0.0
    if not required_keys:
        return 1.0
    total_slots = len(rows) * len(required_keys)
    ok_slots = 0
    for r in rows:
        for k in required_keys:
            if r.get(k) is not None:
                ok_slots += 1
    return float(ok_slots) / float(max(1, total_slots))


def _metric_series_for_task(metric_rows: List[Dict[str, Any]], task: str) -> List[Dict[str, Any]]:
    filtered = [r for r in metric_rows if str(r.get("task") or "").strip() == str(task)]
    filtered.sort(key=lambda r: _parse_ts(r.get("timestamp")))
    return filtered


def _plateau_signal(
    series: List[Dict[str, Any]],
    *,
    window: int,
    delta: float,
) -> Tuple[bool, Optional[float]]:
    if len(series) < max(2, window):
        return False, None
    tail = series[-int(window) :]
    vals = []
    for r in tail:
        v = _to_float(r.get("metric_primary_value"))
        if v is None:
            continue
        vals.append(v)
    if len(vals) < 2:
        return False, None
    diffs = [abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
    avg_change = sum(diffs) / float(len(diffs))
    return avg_change <= float(delta), float(avg_change)


def build_task_readiness(
    *,
    task: str,
    run_rows: List[Dict[str, Any]],
    metric_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    gate = dict(config.READINESS_GATES.get(task) or {})
    if not gate:
        return {
            "task": task,
            "recommendation": "insufficient_data",
            "reason": "missing_gate_config",
        }

    if task == "phase1_quality_score":
        required = list(config.PHASE1_REQUIRED_FEATURES)
        labeled_rows = [r for r in run_rows if r.get("phase") == "phase1" and r.get("quality_score") is not None]
        target_counts: Dict[str, int] = {}
        class_balance = None
    else:
        required = list(config.PHASE3_REQUIRED_FEATURES)
        labeled_rows = [
            r
            for r in run_rows
            if r.get("phase") == "phase3" and (r.get("sequence_quality_score") is not None or r.get("reorder_recommended") is not None)
        ]
        labels = []
        for r in labeled_rows:
            if r.get("reorder_recommended") is None:
                continue
            labels.append("risk" if bool(r.get("reorder_recommended")) else "stable")
        cnt = Counter(labels)
        target_counts = {k: int(v) for k, v in cnt.items()}
        if cnt and sum(cnt.values()) > 0:
            min_ratio = min(float(v) / float(sum(cnt.values())) for v in cnt.values())
            class_balance = min_ratio
        else:
            class_balance = None

    total_runs = len(run_rows)
    labeled_count = len(labeled_rows)
    feat_compl = _feature_completeness(labeled_rows, required)
    metrics_series = _metric_series_for_task(metric_rows, task)
    retrain_count = len([r for r in metrics_series if str(r.get("event_type") or "") == "train"])
    last_trained_at = None
    if metrics_series:
        last_trained_at = metrics_series[-1].get("timestamp")

    plateau, avg_change = _plateau_signal(
        metrics_series,
        window=int(gate.get("plateau_window") or 3),
        delta=float(gate.get("plateau_delta") or 0.02),
    )

    recommendation = "stay_on_baseline"
    reason = "baseline_stable"
    if total_runs < int(gate.get("min_logged_runs") or 0):
        recommendation = "insufficient_data"
        reason = "not_enough_logged_runs"
    elif labeled_count < int(gate.get("min_labeled_rows") or 0):
        recommendation = "insufficient_data"
        reason = "not_enough_labeled_rows"
    elif feat_compl < float(gate.get("min_feature_completeness") or 0.0):
        recommendation = "insufficient_data"
        reason = "feature_completeness_too_low"
    elif task == "phase3_sequence_risk" and class_balance is not None:
        min_ratio_needed = float(gate.get("min_minority_ratio") or 0.0)
        if class_balance < min_ratio_needed:
            recommendation = "stay_on_baseline"
            reason = "class_imbalance_high"
    elif retrain_count < int(gate.get("min_retrains") or 0):
        recommendation = "stay_on_baseline"
        reason = "insufficient_retrains"
    elif not plateau:
        recommendation = "stay_on_baseline"
        reason = "metric_not_plateaued"
    elif labeled_count >= int(gate.get("mlp_ready_rows") or 10**9):
        recommendation = "ready_to_experiment_mlp"
        reason = "data_volume_and_plateau_support_mlp"
    elif labeled_count >= int(gate.get("xgb_ready_rows") or 10**9):
        recommendation = "ready_to_test_xgboost_or_catboost"
        reason = "plateau_detected_try_tree_ensemble"

    return {
        "task": task,
        "total_logged_runs": int(total_runs),
        "labeled_rows_count": int(labeled_count),
        "feature_completeness_pct": round(feat_compl * 100.0, 2),
        "target_distribution": target_counts,
        "class_balance": (round(class_balance, 4) if class_balance is not None else None),
        "validation_metric_trend": [
            {
                "timestamp": r.get("timestamp"),
                "event_type": r.get("event_type"),
                "metric_primary_name": r.get("metric_primary_name"),
                "metric_primary_value": r.get("metric_primary_value"),
                "metric_higher_better": bool(r.get("metric_higher_better", True)),
            }
            for r in metrics_series[-30:]
        ],
        "metric_plateau": bool(plateau),
        "metric_avg_change": (round(avg_change, 6) if avg_change is not None else None),
        "retrain_count": int(retrain_count),
        "last_trained_at": last_trained_at,
        "recommendation": recommendation,
        "reason": reason,
        "gate_config": gate,
    }

