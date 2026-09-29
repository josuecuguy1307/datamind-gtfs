from __future__ import annotations

from typing import Any, Dict, Iterable, List

TYPE_LABELS = ["STOP", "POI", "STATION", "TERMINAL", "OTHER"]
FEATURE_KEYS = [
    "tag_stop_weight",
    "tag_poi_weight",
    "transit_density_300m",
    "poi_density_300m",
    "name_stop_kw",
    "name_poi_kw",
]

DEFAULT_WEIGHTS: Dict[str, Dict[str, float]] = {
    "STOP": {"tag_stop_weight": 0.9, "transit_density_300m": 0.03, "name_stop_kw": 0.6},
    "POI": {"tag_poi_weight": 0.9, "poi_density_300m": 0.03, "name_poi_kw": 0.6},
    "STATION": {"tag_stop_weight": 0.6, "name_stop_kw": 0.8},
    "TERMINAL": {"tag_stop_weight": 0.7, "name_stop_kw": 0.9},
    "OTHER": {"tag_stop_weight": 0.1, "tag_poi_weight": 0.1},
}


def _vec(f: Dict[str, Any]) -> Dict[str, float]:
    return {k: float(f.get(k) or 0.0) for k in FEATURE_KEYS}


def train_type_model(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    by_type: Dict[str, List[Dict[str, float]]] = {k: [] for k in TYPE_LABELS}
    for r in rows:
        y = str(r.get("label") or "").upper()
        if y not in by_type:
            continue
        by_type[y].append(_vec(r.get("features") or {}))

    if sum(len(v) for v in by_type.values()) == 0:
        return {"weights": DEFAULT_WEIGHTS, "labels": TYPE_LABELS, "method": "fallback_default"}

    global_mean = {k: 0.0 for k in FEATURE_KEYS}
    n_all = 0
    for arr in by_type.values():
        for x in arr:
            for k in FEATURE_KEYS:
                global_mean[k] += x.get(k, 0.0)
            n_all += 1
    for k in FEATURE_KEYS:
        global_mean[k] /= max(n_all, 1)

    weights: Dict[str, Dict[str, float]] = {}
    for t, arr in by_type.items():
        if not arr:
            weights[t] = DEFAULT_WEIGHTS.get(t, {})
            continue
        mean_t = {k: sum(x.get(k, 0.0) for x in arr) / max(len(arr), 1) for k in FEATURE_KEYS}
        weights[t] = {k: mean_t[k] - global_mean[k] for k in FEATURE_KEYS}

    return {
        "weights": weights,
        "labels": TYPE_LABELS,
        "method": "difference_of_means_multiclass",
        "n_rows": n_all,
    }


def predict_type(features: Dict[str, Any], artifact: Dict[str, Any]) -> Dict[str, Any]:
    f = _vec(features)
    w_all = artifact.get("weights") or DEFAULT_WEIGHTS
    labels = artifact.get("labels") or TYPE_LABELS

    best_label = "OTHER"
    best_score = -1e18
    raw: Dict[str, float] = {}
    for lab in labels:
        w = w_all.get(lab) or {}
        s = 0.0
        for k in FEATURE_KEYS:
            s += float(w.get(k) or 0.0) * float(f.get(k) or 0.0)
        raw[lab] = float(s)
        if s > best_score:
            best_score = s
            best_label = str(lab)

    vals = list(raw.values())
    lo = min(vals) if vals else 0.0
    hi = max(vals) if vals else 1.0
    denom = (hi - lo) if (hi - lo) > 1e-9 else 1.0
    conf = (best_score - lo) / denom

    return {
        "label": best_label,
        "score": float(conf),
        "raw": raw,
    }
