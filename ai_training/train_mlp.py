from __future__ import annotations

import argparse
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


def _confusion_and_scores(y_true: np.ndarray, y_pred: np.ndarray, n_classes: int) -> dict[str, Any]:
    cm = np.zeros((n_classes, n_classes), dtype=np.int64)
    for t, p in zip(y_true, y_pred):
        cm[int(t), int(p)] += 1

    total = int(cm.sum())
    accuracy = float(np.trace(cm) / total) if total > 0 else 0.0

    f1_list: list[float] = []
    for c in range(n_classes):
        tp = float(cm[c, c])
        fp = float(cm[:, c].sum() - tp)
        fn = float(cm[c, :].sum() - tp)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2.0 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        f1_list.append(f1)
    return {
        "accuracy": accuracy,
        "f1_macro": float(np.mean(f1_list)) if f1_list else 0.0,
        "confusion_matrix": cm.tolist(),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Train a small PyTorch MLP from ai.ai_training_dataset.")
    ap.add_argument("--phase", default=None, help="Optional phase filter (text).")
    ap.add_argument("--model-name", default="mlp_v1")
    ap.add_argument("--model-version", default=None)
    ap.add_argument("--activate", action="store_true")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--val-ratio", type=float, default=0.2)
    args = ap.parse_args()

    try:
        import torch  # type: ignore
        import torch.nn as nn  # type: ignore
    except Exception as e:  # pragma: no cover - dependency guard
        raise RuntimeError("PyTorch is required. Install with `pip install torch`.") from e

    phase = str(args.phase).strip() if args.phase else None
    rows = load_training_dataset_rows(phase=phase)
    X_dict, y_labels, created_at = to_learning_rows(rows)
    if len(X_dict) < 5:
        print({"ok": False, "reason": "Not enough labeled rows to train MLP.", "rows": len(X_dict)})
        return

    label_names = sorted(set(y_labels))
    if len(label_names) < 2:
        print({"ok": False, "reason": "Need at least two label classes.", "labels": label_names})
        return
    label_to_idx = {name: i for i, name in enumerate(label_names)}

    feature_keys = sorted({k for d in X_dict for k in d.keys()})
    if not feature_keys:
        print({"ok": False, "reason": "No numeric features found in dataset."})
        return

    X = np.asarray([[float(d.get(k, 0.0)) for k in feature_keys] for d in X_dict], dtype=np.float32)
    y = np.asarray([label_to_idx[v] for v in y_labels], dtype=np.int64)

    train_idx, val_idx = temporal_split_indices(len(X_dict), val_ratio=float(args.val_ratio))
    X_train = X[train_idx]
    y_train = y[train_idx]
    X_val = X[val_idx] if val_idx else np.zeros((0, X.shape[1]), dtype=np.float32)
    y_val = y[val_idx] if val_idx else np.zeros((0,), dtype=np.int64)

    mean = X_train.mean(axis=0) if len(X_train) > 0 else np.zeros((X.shape[1],), dtype=np.float32)
    std = X_train.std(axis=0) if len(X_train) > 0 else np.ones((X.shape[1],), dtype=np.float32)
    std[std == 0.0] = 1.0

    X_train = (X_train - mean) / std
    X_val = (X_val - mean) / std if len(X_val) else X_val

    class MLP(nn.Module):
        def __init__(self, input_dim: int, output_dim: int) -> None:
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(input_dim, 64),
                nn.ReLU(),
                nn.Linear(64, 32),
                nn.ReLU(),
                nn.Linear(32, output_dim),
            )

        def forward(self, x):
            return self.net(x)

    model = MLP(input_dim=X.shape[1], output_dim=len(label_names))
    optimizer = torch.optim.Adam(model.parameters(), lr=float(args.lr))
    loss_fn = nn.CrossEntropyLoss()

    x_train_t = torch.tensor(X_train, dtype=torch.float32)
    y_train_t = torch.tensor(y_train, dtype=torch.long)

    model.train()
    batch_size = max(8, int(args.batch_size))
    epochs = max(1, int(args.epochs))
    for _ in range(epochs):
        perm = torch.randperm(x_train_t.shape[0])
        for i in range(0, x_train_t.shape[0], batch_size):
            idx = perm[i : i + batch_size]
            xb = x_train_t[idx]
            yb = y_train_t[idx]
            optimizer.zero_grad(set_to_none=True)
            logits = model(xb)
            loss = loss_fn(logits, yb)
            loss.backward()
            optimizer.step()

    model.eval()
    with torch.no_grad():
        train_logits = model(x_train_t)
        train_pred = torch.argmax(train_logits, dim=1).cpu().numpy()
    train_metrics = _confusion_and_scores(y_train, train_pred, len(label_names))

    if len(X_val):
        with torch.no_grad():
            val_logits = model(torch.tensor(X_val, dtype=torch.float32))
            val_pred = torch.argmax(val_logits, dim=1).cpu().numpy()
        val_metrics = _confusion_and_scores(y_val, val_pred, len(label_names))
    else:
        val_metrics = {"accuracy": None, "f1_macro": None, "confusion_matrix": []}

    model_name = str(args.model_name or "mlp_v1").strip()
    model_version = str(args.model_version or now_version(model_name)).strip()
    artifact_path = ensure_models_dir() / f"{model_name}__{model_version}.pt"

    torch.save(
        {
            "kind": "torch_mlp",
            "model_name": model_name,
            "model_version": model_version,
            "trained_at": datetime.utcnow().isoformat(),
            "phase": phase,
            "input_dim": int(X.shape[1]),
            "hidden_dims": [64, 32],
            "output_dim": int(len(label_names)),
            "feature_keys": feature_keys,
            "labels": label_names,
            "mean": mean.tolist(),
            "std": std.tolist(),
            "state_dict": model.state_dict(),
        },
        artifact_path,
    )

    metrics = {
        "train": {**train_metrics, "labels": label_names},
        "val": {**val_metrics, "labels": label_names},
        "n_rows": int(len(X_dict)),
        "n_features": int(len(feature_keys)),
        "epochs": int(epochs),
    }
    trained_on = {
        "phase": phase,
        "rows": int(len(X_dict)),
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
            "registry_model_id": registry_row.get("model_id"),
            "activate": bool(args.activate),
            "metrics": metrics,
        }
    )


if __name__ == "__main__":
    main()
