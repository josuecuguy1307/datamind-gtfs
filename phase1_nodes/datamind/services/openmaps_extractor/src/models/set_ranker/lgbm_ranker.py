# src/models/set_ranker/lgbm_ranker.py
from __future__ import annotations

from typing import Dict, Tuple
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

MODEL_VERSION = "lgbm_ranker_v1"
MODEL_PATH = Path("models_artifacts/set_ranker") / f"{MODEL_VERSION}.txt"


# -------------------------
# Feature encoding
# -------------------------

FEATURE_COLUMNS = [
    "n_candidates",
    "n_clusters",
    "mean_prob_stop",
    "duplication_rate",
]


def encode_features(row: Dict) -> np.ndarray:
    return np.array(
        [
            float(row["n_candidates"]),
            float(row["n_clusters"]),
            float(row["mean_prob_stop"]),
            float(row["duplication_rate"]),
        ],
        dtype=np.float32,
    )


# -------------------------
# Prediction
# -------------------------

def load_model() -> lgb.Booster:
    if not MODEL_PATH.exists():
        raise RuntimeError("Set ranker model not trained yet.")
    return lgb.Booster(model_file=str(MODEL_PATH))


def score(features: Dict) -> Tuple[float, str]:
    model = load_model()
    x = encode_features(features).reshape(1, -1)
    score = float(model.predict(x)[0])
    return score, MODEL_VERSION


# -------------------------
# Training
# -------------------------

def train(
    groups_df: pd.DataFrame,
    out_path: Path | None = None,
):
    """
    Required columns:
      - group_id
      - label
      - n_candidates
      - n_clusters
      - mean_prob_stop
      - duplication_rate
    """

    out_path = out_path or MODEL_PATH

    # Sort by group_id (LightGBM requirement)
    groups_df = groups_df.sort_values("group_id")

    X = np.vstack(groups_df.apply(encode_features, axis=1))
    y = groups_df["label"].astype(float).values

    # Group sizes
    group_sizes = (
        groups_df.groupby("group_id")
        .size()
        .values
        .astype(int)
    )

    dtrain = lgb.Dataset(
        X,
        label=y,
        group=group_sizes,
        feature_name=FEATURE_COLUMNS,
    )

    params = {
        "objective": "lambdarank",
        "metric": "ndcg",
        "ndcg_eval_at": [1, 3, 5],
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 10,
        "verbosity": -1,
    }

    model = lgb.train(
        params,
        dtrain,
        num_boost_round=300,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_path))

    return out_path
