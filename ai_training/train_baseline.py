from __future__ import annotations

import argparse
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from ai_training.common import (
    ensure_models_dir,
    label_distribution,
    load_training_dataset_rows,
    now_version,
    register_model,
    temporal_split_indices,
    to_learning_rows,
)


def _metrics(*, y_true: list[int], y_pred: list[int], labels: list[str], encoder) -> dict[str, Any]:
    try:
        from sklearn.metrics import accuracy_score, confusion_matrix, f1_score
    except Exception as e:  # pragma: no cover - dependency guard
        raise RuntimeError("scikit-learn is required for baseline training metrics.") from e

    acc = float(accuracy_score(y_true, y_pred)) if y_true else 0.0
    f1 = float(f1_score(y_true, y_pred, average="macro")) if y_true else 0.0
    cm = confusion_matrix(y_true, y_pred, labels=list(range(len(labels)))).tolist() if y_true else []
    return {
        "accuracy": acc,
        "f1_macro": f1,
        "labels": labels,
        "confusion_matrix": cm,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Train baseline logistic model from ai.ai_training_dataset.")
    ap.add_argument("--phase", default=None, help="Optional phase filter (text).")
    ap.add_argument("--model-name", default="baseline_logreg")
    ap.add_argument("--model-version", default=None, help="Optional explicit model version.")
    ap.add_argument("--activate", action="store_true", help="Mark this model as active in ai_model_registry.")
    ap.add_argument("--val-ratio", type=float, default=0.2)
    args = ap.parse_args()

    try:
        from sklearn.feature_extraction import DictVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import LabelEncoder
    except Exception as e:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "scikit-learn is required. Install with `pip install scikit-learn`."
        ) from e

    phase = str(args.phase).strip() if args.phase else None
    rows = load_training_dataset_rows(phase=phase)
    X_dict, y_labels, created_at = to_learning_rows(rows)
    if len(X_dict) < 2:
        print({"ok": False, "reason": "Not enough labeled rows to train.", "rows": len(X_dict)})
        return

    distinct_labels = sorted(set(y_labels))
    if len(distinct_labels) < 2:
        print({"ok": False, "reason": "Need at least two label classes to train.", "labels": distinct_labels})
        return

    n = len(X_dict)
    train_idx, val_idx = temporal_split_indices(n, val_ratio=float(args.val_ratio))
    X_train_dict = [X_dict[i] for i in train_idx]
    y_train_label = [y_labels[i] for i in train_idx]
    X_val_dict = [X_dict[i] for i in val_idx]
    y_val_label = [y_labels[i] for i in val_idx]

    encoder = LabelEncoder()
    y_train = encoder.fit_transform(y_train_label)
    y_all = encoder.transform(y_labels)
    class_names = [str(x) for x in encoder.classes_]

    vectorizer = DictVectorizer(sparse=True)
    X_train = vectorizer.fit_transform(X_train_dict)

    clf = LogisticRegression(max_iter=500, class_weight="balanced", multi_class="auto")
    clf.fit(X_train, y_train)

    pred_train = clf.predict(X_train)
    train_metrics = _metrics(y_true=y_train.tolist(), y_pred=pred_train.tolist(), labels=class_names, encoder=encoder)

    if val_idx:
        y_val = encoder.transform(y_val_label)
        X_val = vectorizer.transform(X_val_dict)
        pred_val = clf.predict(X_val)
        val_metrics = _metrics(y_true=y_val.tolist(), y_pred=pred_val.tolist(), labels=class_names, encoder=encoder)
    else:
        val_metrics = {"accuracy": None, "f1_macro": None, "labels": class_names, "confusion_matrix": []}

    model_name = str(args.model_name or "baseline_logreg").strip()
    model_version = str(args.model_version or now_version(model_name)).strip()
    models_dir = ensure_models_dir()
    artifact_path = models_dir / f"{model_name}__{model_version}.pkl"

    payload = {
        "kind": "sklearn_logreg",
        "model_name": model_name,
        "model_version": model_version,
        "trained_at": datetime.utcnow().isoformat(),
        "phase": phase,
        "classes": class_names,
        "vectorizer": vectorizer,
        "label_encoder": encoder,
        "model": clf,
    }
    with artifact_path.open("wb") as f:
        pickle.dump(payload, f)

    metrics = {
        "train": train_metrics,
        "val": val_metrics,
        "n_rows": n,
        "n_features": int(X_train.shape[1]),
    }
    trained_on = {
        "phase": phase,
        "rows": n,
        "label_distribution": label_distribution(y_labels),
        "date_min": created_at[0].isoformat() if created_at else None,
        "date_max": created_at[-1].isoformat() if created_at else None,
    }
    registry_row = register_model(
        phase=phase,
        model_name=model_name,
        model_version=model_version,
        artifact_path=artifact_path,
        metrics=metrics,
        trained_on=trained_on,
        activate=bool(args.activate),
    )

    print(
        {
            "ok": True,
            "model_name": model_name,
            "model_version": model_version,
            "artifact_path": str(Path(artifact_path).resolve()),
            "activate": bool(args.activate),
            "registry_model_id": registry_row.get("model_id"),
            "metrics": metrics,
        }
    )


if __name__ == "__main__":
    main()
