from __future__ import annotations

from typing import Any, Dict, Iterable, List

FEATURE_KEYS = [
    "source_weight",
    "quality",
    "freq",
    "token_count",
    "has_digit",
    "is_generic",
    "length",
]

DEFAULT_WEIGHTS: Dict[str, float] = {
    "source_weight": 0.55,
    "quality": 0.35,
    "freq": 0.10,
    "token_count": -0.02,
    "has_digit": -0.10,
    "is_generic": -0.35,
    "length": 0.002,
}


def _vec(features: Dict[str, Any]) -> Dict[str, float]:
    return {k: float(features.get(k) or 0.0) for k in FEATURE_KEYS}


def train_weights(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    pos = []
    neg = []
    for r in rows:
        f = _vec(r.get("features") or {})
        y = int(r.get("label") or 0)
        if y == 1:
            pos.append(f)
        else:
            neg.append(f)

    if not pos or not neg:
        return {
            "feature_keys": FEATURE_KEYS,
            "weights": DEFAULT_WEIGHTS,
            "bias": 0.0,
            "n_pos": len(pos),
            "n_neg": len(neg),
            "method": "fallback_default",
        }

    def mean(key: str, arr: List[Dict[str, float]]) -> float:
        return sum(x.get(key, 0.0) for x in arr) / max(len(arr), 1)

    weights = {}
    for k in FEATURE_KEYS:
        weights[k] = mean(k, pos) - mean(k, neg)

    return {
        "feature_keys": FEATURE_KEYS,
        "weights": weights,
        "bias": 0.0,
        "n_pos": len(pos),
        "n_neg": len(neg),
        "method": "difference_of_means",
    }


def score(features: Dict[str, Any], artifact: Dict[str, Any]) -> float:
    f = _vec(features)
    w = artifact.get("weights") or DEFAULT_WEIGHTS
    bias = float(artifact.get("bias") or 0.0)
    s = bias
    for k in FEATURE_KEYS:
        s += float(w.get(k) or 0.0) * float(f.get(k) or 0.0)
    return float(s)
