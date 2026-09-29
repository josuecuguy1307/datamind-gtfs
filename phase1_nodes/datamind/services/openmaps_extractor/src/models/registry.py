# src/models/registry.py
from __future__ import annotations
from typing import Callable, Tuple, Dict, Any

# STOP / POI models
from src.models.stop_poi.baseline import predict_from_tags as stop_poi_baseline
from src.models.stop_poi.lgbm import predict as stop_poi_lgbm

# SET RANKER models
from src.models.set_ranker.baseline import score_set as ranker_baseline
from src.models.set_ranker.lgbm_ranker import score as ranker_lgbm


# -----------------------------
# Model switches (CONFIG)
# -----------------------------

ACTIVE_STOP_POI_MODEL = "baseline"     # later: "lgbm"
ACTIVE_SET_RANKER = "baseline"         # later: "lgbm"


# -----------------------------
# STOP / POI routing
# -----------------------------

def predict_stop_poi(
    features: Dict[str, Any],
    tag_kind: str | None = None,
) -> Tuple[float, float, str, str]:
    """
    Returns:
      prob_stop, prob_poi, pred, model_version
    """

    if ACTIVE_STOP_POI_MODEL == "baseline":
        return stop_poi_baseline(features, tag_kind)

    if ACTIVE_STOP_POI_MODEL == "lgbm":
        return stop_poi_lgbm(features)

    raise ValueError(f"Unknown STOP/POI model: {ACTIVE_STOP_POI_MODEL}")


# -----------------------------
# SET RANKER routing
# -----------------------------

def score_candidate_set(
    features: Dict[str, Any],
) -> Tuple[float, str]:
    """
    Returns:
      score, model_version
    """

    if ACTIVE_SET_RANKER == "baseline":
        return ranker_baseline(
            features["n_candidates"],
            features["n_clusters"],
            features["mean_prob_stop"],
            features["duplication_rate"],
        )

    if ACTIVE_SET_RANKER == "lgbm":
        return ranker_lgbm(features)

    raise ValueError(f"Unknown SET RANKER model: {ACTIVE_SET_RANKER}")
