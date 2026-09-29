from __future__ import annotations

import math
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from datamind_console.ai_agent.feature_utils import flatten_numeric_features

LABEL_TO_SUGGESTION = {
    "approve": "approve",
    "reject": "reject",
    "promote": "promote",
    "dismiss": "investigate",
    "reviewed": "review",
}


@dataclass
class DecisionResult:
    suggestion_type: str
    confidence: float
    reason: str
    evidence: dict[str, Any]
    prediction: Optional[dict[str, Any]] = None
    model_name: Optional[str] = None
    model_version: Optional[str] = None


class _TorchMLP:
    def __init__(self, payload: dict[str, Any]) -> None:
        try:
            import torch  # type: ignore
            import torch.nn as nn  # type: ignore
        except Exception as e:  # pragma: no cover - optional dependency
            raise RuntimeError("PyTorch is required to load .pt model artifacts.") from e

        input_dim = int(payload.get("input_dim") or 0)
        hidden_dims = [int(x) for x in (payload.get("hidden_dims") or [64, 32])]
        output_dim = int(payload.get("output_dim") or 0)
        if input_dim <= 0 or output_dim <= 1:
            raise RuntimeError("Invalid MLP payload dimensions.")

        layers: list[Any] = []
        prev = input_dim
        for h in hidden_dims:
            layers.append(nn.Linear(prev, h))
            layers.append(nn.ReLU())
            prev = h
        layers.append(nn.Linear(prev, output_dim))
        net = nn.Sequential(*layers)
        net.load_state_dict(payload["state_dict"])
        net.eval()

        self._torch = torch
        self.net = net
        self.feature_keys = [str(x) for x in (payload.get("feature_keys") or [])]
        self.labels = [str(x) for x in (payload.get("labels") or [])]
        self.mean = np.asarray(payload.get("mean") or [0.0] * input_dim, dtype=np.float32)
        self.std = np.asarray(payload.get("std") or [1.0] * input_dim, dtype=np.float32)
        self.std[self.std == 0.0] = 1.0

    def predict(self, features: dict[str, Any]) -> dict[str, Any]:
        dense = np.asarray([float(features.get(k, 0.0)) for k in self.feature_keys], dtype=np.float32)
        dense = (dense - self.mean) / self.std
        with self._torch.no_grad():
            logits = self.net(self._torch.tensor(dense, dtype=self._torch.float32).unsqueeze(0))
            probs = self._torch.softmax(logits, dim=1).cpu().numpy()[0].tolist()
            raw_logits = logits.cpu().numpy()[0].tolist()
        pairs = list(zip(self.labels, probs))
        pairs.sort(key=lambda x: float(x[1]), reverse=True)
        return {
            "classes": self.labels,
            "probabilities": probs,
            "logits": raw_logits,
            "top_label": pairs[0][0] if pairs else "reviewed",
            "top_probability": float(pairs[0][1]) if pairs else 0.0,
        }


class LoadedModel:
    def __init__(self, *, model_name: str, model_version: str, artifact_path: str) -> None:
        self.model_name = model_name
        self.model_version = model_version
        self.artifact_path = self._resolve_artifact_path(artifact_path)
        self.kind = ""
        self.payload: dict[str, Any] = {}
        self._torch_model: Optional[_TorchMLP] = None
        self._load()

    @staticmethod
    def _resolve_artifact_path(path: str) -> Path:
        p = Path(path).expanduser()
        if p.is_absolute():
            return p
        repo_root = Path(__file__).resolve().parents[2]
        return (repo_root / p).resolve()

    def _load(self) -> None:
        if not self.artifact_path.exists():
            raise FileNotFoundError(f"Model artifact does not exist: {self.artifact_path}")

        if self.artifact_path.suffix.lower() == ".pt":
            try:
                import torch  # type: ignore
            except Exception as e:  # pragma: no cover - optional dependency
                raise RuntimeError("PyTorch is required for .pt model artifacts.") from e
            payload = torch.load(self.artifact_path, map_location="cpu")
            if not isinstance(payload, dict):
                raise RuntimeError(f"Invalid .pt payload: {self.artifact_path}")
            self.kind = str(payload.get("kind") or "torch_mlp")
            self.payload = payload
            self._torch_model = _TorchMLP(payload)
            return

        with self.artifact_path.open("rb") as f:
            payload = pickle.load(f)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Invalid model payload: {self.artifact_path}")
        self.kind = str(payload.get("kind") or "sklearn")
        self.payload = payload

    def predict(self, features: dict[str, Any]) -> dict[str, Any]:
        flat = flatten_numeric_features(features)
        if self.kind.startswith("torch") and self._torch_model is not None:
            return self._torch_model.predict(flat)

        model = self.payload.get("model")
        vectorizer = self.payload.get("vectorizer")
        if model is None or vectorizer is None:
            raise RuntimeError("Sklearn artifact missing model/vectorizer.")
        transformed = vectorizer.transform([flat])
        probs = model.predict_proba(transformed)[0]
        classes = self.payload.get("classes")
        if not classes:
            enc = self.payload.get("label_encoder")
            classes = list(enc.classes_) if enc is not None else [str(i) for i in range(len(probs))]
        classes = [str(x) for x in classes]
        pairs = list(zip(classes, [float(x) for x in probs]))
        pairs.sort(key=lambda x: x[1], reverse=True)
        return {
            "classes": classes,
            "probabilities": [float(x) for x in probs],
            "top_label": pairs[0][0] if pairs else "reviewed",
            "top_probability": float(pairs[0][1]) if pairs else 0.0,
        }


def decide_heuristics(features: dict[str, Any]) -> DecisionResult:
    total = float(features.get("total_decisions") or 0.0)
    n_approve = float(features.get("n_approve") or 0.0)
    n_reject = float(features.get("n_reject") or 0.0)
    n_publish = float(features.get("n_publish") or 0.0)
    age_h = float(features.get("hours_since_last_decision") or 9999.0)

    approve_rate = n_approve / total if total > 0 else 0.0
    reject_rate = n_reject / total if total > 0 else 0.0
    publish_rate = n_publish / total if total > 0 else 0.0

    thresholds = {
        "promote_total_min": 6.0,
        "promote_approve_rate_min": 0.75,
        "reject_total_min": 4.0,
        "reject_rate_min": 0.60,
        "review_staleness_hours": 96.0,
    }

    if total >= thresholds["promote_total_min"] and approve_rate >= thresholds["promote_approve_rate_min"]:
        conf = min(0.98, 0.65 + (approve_rate * 0.3) + (publish_rate * 0.2))
        return DecisionResult(
            suggestion_type="promote",
            confidence=conf,
            reason=f"High approve rate ({approve_rate:.2f}) over {int(total)} decisions.",
            evidence={"thresholds": thresholds, "approve_rate": approve_rate, "publish_rate": publish_rate},
        )

    if total >= thresholds["reject_total_min"] and reject_rate >= thresholds["reject_rate_min"]:
        conf = min(0.98, 0.60 + (reject_rate * 0.35))
        return DecisionResult(
            suggestion_type="reject",
            confidence=conf,
            reason=f"High reject rate ({reject_rate:.2f}) over {int(total)} decisions.",
            evidence={"thresholds": thresholds, "reject_rate": reject_rate},
        )

    if age_h >= thresholds["review_staleness_hours"] and total > 0:
        conf = min(0.9, 0.55 + min(0.3, math.log1p(age_h) / 10.0))
        return DecisionResult(
            suggestion_type="review",
            confidence=conf,
            reason=f"Entity has been idle for {age_h:.1f}h and needs review.",
            evidence={"thresholds": thresholds, "hours_since_last_decision": age_h},
        )

    conf = 0.45 if total <= 1 else 0.52
    return DecisionResult(
        suggestion_type="investigate",
        confidence=conf,
        reason="Not enough signal for direct action; investigate with a human.",
        evidence={"thresholds": thresholds, "total_decisions": total},
    )


def decide_model(*, features: dict[str, Any], loaded_model: LoadedModel) -> DecisionResult:
    prediction = loaded_model.predict(features)
    top_label = str(prediction.get("top_label") or "reviewed").strip().lower()
    suggestion_type = LABEL_TO_SUGGESTION.get(top_label, "investigate")
    confidence = float(prediction.get("top_probability") or 0.0)
    reason = f"Model `{loaded_model.model_name}` ({loaded_model.model_version}) predicted `{top_label}`."
    return DecisionResult(
        suggestion_type=suggestion_type,
        confidence=max(0.0, min(1.0, confidence)),
        reason=reason,
        evidence={"mode": "model"},
        prediction=prediction,
        model_name=loaded_model.model_name,
        model_version=loaded_model.model_version,
    )
