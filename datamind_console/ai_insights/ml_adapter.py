from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .scoring import score_phase1_quality, score_phase3_quality


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _load_feature_meta(model_path: Path) -> Dict[str, Any]:
    meta_path = model_path.with_suffix(".features.json")
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ordered_vector(features: Dict[str, Any], feature_names: List[str]) -> List[float]:
    return [_to_float(features.get(name), 0.0) for name in feature_names]


def _fallback_prediction(task: str, features: Dict[str, Any]) -> Dict[str, Any]:
    if task == "phase1_quality_score":
        s = score_phase1_quality(features)
        return {
            "task": task,
            "model_used": False,
            "prediction": {"score": s.get("score")},
            "confidence": None,
            "reason": "fallback_heuristic",
        }

    if task == "phase3_sequence_risk":
        seq_q = _to_float(features.get("sequence_quality_score"), 100.0)
        unmatched_ratio = 0.0
        ambiguous_ratio = 0.0
        prior = max(1.0, _to_float(features.get("prior_stop_count"), 0.0))
        unmatched = _to_float(features.get("unmatched_count"), 0.0)
        ambiguous = _to_float(features.get("ambiguous_count"), 0.0)
        if prior > 0:
            unmatched_ratio = unmatched / prior
            ambiguous_ratio = ambiguous / prior
        risk_prob = _clip01(((100.0 - seq_q) / 100.0) * 0.60 + unmatched_ratio * 0.25 + ambiguous_ratio * 0.20)
        return {
            "task": task,
            "model_used": False,
            "prediction": {
                "risk_probability": round(risk_prob, 4),
                "risk_label": int(risk_prob >= 0.5),
            },
            "confidence": round(abs(risk_prob - 0.5) * 2.0, 4),
            "reason": "fallback_heuristic",
        }

    return {
        "task": task,
        "model_used": False,
        "prediction": {},
        "confidence": None,
        "reason": "unknown_task",
    }


def predict(task: str, *, features: Dict[str, Any]) -> Dict[str, Any]:
    task_cfg = config.MODEL_TASKS.get(task) or {}
    if not task_cfg:
        out = _fallback_prediction(task, features)
        out["reason"] = "unknown_task"
        return out
    if not config.ENABLE_ML:
        out = _fallback_prediction(task, features)
        out["reason"] = "ml_disabled"
        return out

    model_path = Path(task_cfg.get("artifact") or "")
    if not model_path.exists():
        out = _fallback_prediction(task, features)
        out["reason"] = "model_artifact_missing"
        return out

    try:
        import lightgbm as lgb  # type: ignore
    except Exception:
        out = _fallback_prediction(task, features)
        out["reason"] = "lightgbm_not_available"
        return out

    try:
        booster = lgb.Booster(model_file=str(model_path))
        meta = _load_feature_meta(model_path)
        feature_names = list(meta.get("feature_names") or [])
        if not feature_names:
            feature_names = list(getattr(booster, "feature_name", lambda: [])() or [])
        if not feature_names:
            feature_names = sorted([str(k) for k in features.keys()])
        row = _ordered_vector(features, feature_names)
        pred_raw = booster.predict([row])
        val = float(pred_raw[0]) if len(pred_raw) > 0 else 0.0
    except Exception as e:
        out = _fallback_prediction(task, features)
        out["reason"] = f"model_predict_failed:{e}"
        return out

    kind = str(task_cfg.get("kind") or "").strip().lower()
    if kind == "classification":
        risk_prob = _clip01(val)
        return {
            "task": task,
            "model_used": True,
            "model_kind": "lightgbm",
            "model_path": str(model_path),
            "prediction": {
                "risk_probability": round(risk_prob, 5),
                "risk_label": int(risk_prob >= 0.5),
            },
            "confidence": round(abs(risk_prob - 0.5) * 2.0, 5),
            "reason": "model_prediction",
        }
    return {
        "task": task,
        "model_used": True,
        "model_kind": "lightgbm",
        "model_path": str(model_path),
        "prediction": {"score": round(float(val), 5)},
        "confidence": None,
        "reason": "model_prediction",
    }


def _prepare_xy(
    rows: List[Dict[str, Any]],
    *,
    feature_names: List[str],
    label_key: str,
) -> Tuple[List[List[float]], List[float]]:
    x: List[List[float]] = []
    y: List[float] = []
    for r in rows:
        if r.get(label_key) is None:
            continue
        x.append(_ordered_vector(r, feature_names))
        y.append(_to_float(r.get(label_key), 0.0))
    return x, y


def train_lightgbm(
    *,
    task: str,
    rows: List[Dict[str, Any]],
    feature_names: List[str],
    label_key: str,
) -> Dict[str, Any]:
    task_cfg = config.MODEL_TASKS.get(task) or {}
    if not task_cfg:
        return {"ok": False, "task": task, "reason": "unknown_task"}
    kind = str(task_cfg.get("kind") or "regression").strip().lower()
    model_path = Path(task_cfg.get("artifact") or "")

    if not config.ENABLE_ML:
        return {"ok": False, "task": task, "reason": "ml_disabled"}
    if not rows:
        return {"ok": False, "task": task, "reason": "no_rows"}
    try:
        import lightgbm as lgb  # type: ignore
    except Exception:
        return {"ok": False, "task": task, "reason": "lightgbm_not_available"}

    x, y = _prepare_xy(rows, feature_names=feature_names, label_key=label_key)
    n = len(x)
    if n < 20:
        return {"ok": False, "task": task, "reason": "insufficient_rows", "rows": n}

    split = max(1, int(round(n * 0.8)))
    split = min(split, n - 1)
    x_train = x[:split]
    y_train = y[:split]
    x_val = x[split:]
    y_val = y[split:]

    try:
        if kind == "classification":
            y_train_bin = [1 if float(v) >= 0.5 else 0 for v in y_train]
            y_val_bin = [1 if float(v) >= 0.5 else 0 for v in y_val]
            model = lgb.LGBMClassifier(
                n_estimators=150,
                learning_rate=0.05,
                num_leaves=31,
                random_state=42,
            )
            model.fit(x_train, y_train_bin)
            pred_prob = [float(v[1]) for v in model.predict_proba(x_val)]
            pred_cls = [1 if p >= 0.5 else 0 for p in pred_prob]
            acc = sum(1 for a, b in zip(y_val_bin, pred_cls) if int(a) == int(b)) / max(1, len(y_val_bin))
            metric_primary_name = "accuracy"
            metric_primary_value = float(acc)
            metric_higher_better = True
        else:
            model = lgb.LGBMRegressor(
                n_estimators=200,
                learning_rate=0.05,
                num_leaves=31,
                random_state=42,
            )
            model.fit(x_train, y_train)
            pred_val = [float(v) for v in model.predict(x_val)]
            mse = sum((a - b) ** 2 for a, b in zip(y_val, pred_val)) / max(1, len(y_val))
            rmse = mse ** 0.5
            metric_primary_name = "rmse"
            metric_primary_value = float(rmse)
            metric_higher_better = False
    except Exception as e:
        return {"ok": False, "task": task, "reason": f"train_failed:{e}"}

    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        booster = model.booster_
        booster.save_model(str(model_path))
        meta = {
            "task": task,
            "kind": kind,
            "feature_names": feature_names,
        }
        model_path.with_suffix(".features.json").write_text(
            json.dumps(meta, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        return {"ok": False, "task": task, "reason": f"save_failed:{e}"}

    return {
        "ok": True,
        "task": task,
        "kind": kind,
        "rows_total": n,
        "rows_train": len(x_train),
        "rows_val": len(x_val),
        "feature_names": feature_names,
        "label_key": label_key,
        "model_path": str(model_path),
        "metric_primary_name": metric_primary_name,
        "metric_primary_value": round(float(metric_primary_value), 6),
        "metric_higher_better": bool(metric_higher_better),
    }


def evaluate_lightgbm(
    *,
    task: str,
    rows: List[Dict[str, Any]],
    feature_names: List[str],
    label_key: str,
) -> Dict[str, Any]:
    task_cfg = config.MODEL_TASKS.get(task) or {}
    if not task_cfg:
        return {"ok": False, "task": task, "reason": "unknown_task"}
    kind = str(task_cfg.get("kind") or "regression").strip().lower()
    model_path = Path(task_cfg.get("artifact") or "")

    if not model_path.exists():
        return {"ok": False, "task": task, "reason": "model_artifact_missing"}
    if not rows:
        return {"ok": False, "task": task, "reason": "no_rows"}
    try:
        import lightgbm as lgb  # type: ignore
    except Exception:
        return {"ok": False, "task": task, "reason": "lightgbm_not_available"}

    x, y = _prepare_xy(rows, feature_names=feature_names, label_key=label_key)
    if len(x) < 10:
        return {"ok": False, "task": task, "reason": "insufficient_rows", "rows": len(x)}
    try:
        booster = lgb.Booster(model_file=str(model_path))
        pred = [float(v) for v in booster.predict(x)]
        if kind == "classification":
            yb = [1 if float(v) >= 0.5 else 0 for v in y]
            pb = [1 if float(v) >= 0.5 else 0 for v in pred]
            acc = sum(1 for a, b in zip(yb, pb) if int(a) == int(b)) / max(1, len(yb))
            metric_primary_name = "accuracy"
            metric_primary_value = float(acc)
            metric_higher_better = True
        else:
            mse = sum((a - b) ** 2 for a, b in zip(y, pred)) / max(1, len(y))
            rmse = mse ** 0.5
            metric_primary_name = "rmse"
            metric_primary_value = float(rmse)
            metric_higher_better = False
    except Exception as e:
        return {"ok": False, "task": task, "reason": f"evaluate_failed:{e}"}

    return {
        "ok": True,
        "task": task,
        "kind": kind,
        "rows_eval": len(x),
        "feature_names": feature_names,
        "label_key": label_key,
        "model_path": str(model_path),
        "metric_primary_name": metric_primary_name,
        "metric_primary_value": round(float(metric_primary_value), 6),
        "metric_higher_better": bool(metric_higher_better),
    }
