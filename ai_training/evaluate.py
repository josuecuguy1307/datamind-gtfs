from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import numpy as np

from ai_training.common import load_training_dataset_rows, temporal_split_indices, to_learning_rows
from datamind_console.ai_agent.decision_engine import LoadedModel
from datamind_console.db.ai_repo import get_active_ai_model


def _score_labels(y_true: list[str], y_pred: list[str]) -> dict[str, Any]:
    labels = sorted(set(y_true) | set(y_pred))
    if not labels:
        return {"accuracy": 0.0, "f1_macro": 0.0, "labels": [], "confusion_matrix": []}
    idx = {k: i for i, k in enumerate(labels)}
    cm = np.zeros((len(labels), len(labels)), dtype=np.int64)
    for t, p in zip(y_true, y_pred):
        cm[idx[t], idx[p]] += 1
    total = int(cm.sum())
    acc = float(np.trace(cm) / total) if total else 0.0
    f1_values: list[float] = []
    for i in range(len(labels)):
        tp = float(cm[i, i])
        fp = float(cm[:, i].sum() - tp)
        fn = float(cm[i, :].sum() - tp)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2.0 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        f1_values.append(f1)
    return {
        "accuracy": acc,
        "f1_macro": float(np.mean(f1_values)) if f1_values else 0.0,
        "labels": labels,
        "confusion_matrix": cm.tolist(),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Evaluate model artifact against ai.ai_training_dataset.")
    ap.add_argument("--phase", default=None, help="Optional phase filter (text).")
    ap.add_argument("--artifact-path", default=None, help="Path to model artifact (.pkl or .pt).")
    ap.add_argument("--model-name", default="adhoc_model", help="Used when artifact-path is provided.")
    ap.add_argument("--model-version", default=None, help="Registry model version lookup (optional).")
    ap.add_argument("--val-ratio", type=float, default=0.2)
    ap.add_argument("--use-all", action="store_true", help="Evaluate on all rows instead of temporal val split.")
    args = ap.parse_args()

    phase = str(args.phase).strip() if args.phase else None
    model_row = None
    if args.artifact_path:
        artifact = Path(str(args.artifact_path)).expanduser().resolve()
        model_name = str(args.model_name or "adhoc_model").strip()
        model_version = str(args.model_version or artifact.stem).strip()
    else:
        model_row = get_active_ai_model(phase=phase, model_version=args.model_version)
        if not model_row:
            print({"ok": False, "reason": "No model found in ai_model_registry."})
            return
        artifact = Path(str(model_row.get("artifact_path") or "")).expanduser()
        model_name = str(model_row.get("model_name") or "active_model")
        model_version = str(model_row.get("model_version") or "active")

    loaded = LoadedModel(model_name=model_name, model_version=model_version, artifact_path=str(artifact))

    rows = load_training_dataset_rows(phase=phase)
    X_dict, y_labels, _ = to_learning_rows(rows)
    if len(X_dict) == 0:
        print({"ok": False, "reason": "No rows in ai_training_dataset."})
        return

    if args.use_all:
        eval_idx = list(range(len(X_dict)))
    else:
        _, val_idx = temporal_split_indices(len(X_dict), val_ratio=float(args.val_ratio))
        eval_idx = val_idx if val_idx else list(range(len(X_dict)))

    y_true: list[str] = []
    y_pred: list[str] = []
    for i in eval_idx:
        pred = loaded.predict(X_dict[i])
        y_true.append(str(y_labels[i]))
        y_pred.append(str(pred.get("top_label") or "reviewed").strip().lower())

    metrics = _score_labels(y_true, y_pred)
    print(
        {
            "ok": True,
            "phase": phase,
            "artifact_path": str(loaded.artifact_path),
            "model_name": loaded.model_name,
            "model_version": loaded.model_version,
            "evaluated_rows": len(eval_idx),
            "metrics": metrics,
            "registry_model_id": (model_row or {}).get("model_id"),
        }
    )


if __name__ == "__main__":
    main()
