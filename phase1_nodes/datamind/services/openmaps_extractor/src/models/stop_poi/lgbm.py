# src/models/stop_poi/lgbm.py
"""LightGBM STOP/POI classifier — loads the trained v3 model."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Tuple

import lightgbm as lgb
import numpy as np

# Model resolution order:
#   1. STOP_POI_MODEL_PATH env var
#   2. v3 artifact in this directory
#   3. v2 artifact (fallback)
_MODEL_DIR = Path(__file__).resolve().parent

_MODEL_CANDIDATES = [
    os.environ.get("STOP_POI_MODEL_PATH", ""),
    str(_MODEL_DIR / "stop_poi_lgbm_v3.txt"),
    str(_MODEL_DIR / "stop_poi_lgbm_v2.txt"),
]

# v3 feature order (must match training script)
V3_FEATURES = [
    "has_name", "has_ref", "has_operator", "confidence_v0",
    "has_shelter", "has_bench", "has_route_ref",
    "tag_richness",
    "distance_to_nearest_road_m",
    "on_road_way",
]


@lru_cache(maxsize=1)
def _load_model() -> Tuple[lgb.Booster, str]:
    for path_str in _MODEL_CANDIDATES:
        if path_str and Path(path_str).exists():
            model = lgb.Booster(model_file=path_str)
            version = Path(path_str).stem  # e.g. "stop_poi_lgbm_v3"
            return model, version
    raise FileNotFoundError(
        f"STOP/POI model not found. Searched: {[p for p in _MODEL_CANDIDATES if p]}"
    )


def encode_features(row: Dict[str, Any]) -> np.ndarray:
    """Build feature vector matching V3_FEATURES order."""
    return np.array([
        int(bool(row.get("has_name", False))),
        int(bool(row.get("has_ref", False))),
        int(bool(row.get("has_operator", False))),
        float(row.get("confidence_v0", 0.0)),
        int(bool(row.get("has_shelter", False))),
        int(bool(row.get("has_bench", False))),
        int(bool(row.get("has_route_ref", False))),
        int(row.get("tag_richness", 0) or 0),
        float(row.get("distance_to_nearest_road_m", 0.0) or 0.0),
        int(bool(row.get("on_road_way", False))),
    ], dtype=np.float64)


def predict(features_row: Dict[str, Any]) -> Tuple[float, float, str, str]:
    """Predict STOP/POI probabilities for a single row.

    Returns: (prob_stop, prob_poi, prediction_label, model_version)
    """
    model, version = _load_model()
    x = encode_features(features_row).reshape(1, -1)

    prob_stop = float(model.predict(x)[0])
    prob_poi = 1.0 - prob_stop
    pred = "STOP" if prob_stop >= 0.5 else "POI"

    return prob_stop, prob_poi, pred, version
