from __future__ import annotations
import lightgbm as lgb
import numpy as np
from pathlib import Path

MODEL_VERSION = "lgbm_ranker_v1"
OUT_PATH = Path("models_artifacts/set_ranker") / f"{MODEL_VERSION}.txt"


def train(train_df):
    """
    train_df columns:
    - n_candidates
    - n_clusters
    - mean_prob_stop
    - duplication_rate
    - label   (higher = better set)
    """
    X = train_df[
        ["n_candidates", "n_clusters", "mean_prob_stop", "duplication_rate"]
    ].values
    y = train_df["label"].values

    dtrain = lgb.Dataset(X, label=y)

    params = {
        "objective": "regression",
        "metric": "rmse",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "verbosity": -1,

    }

    model = lgb.train(params, dtrain, num_boost_round=300)


    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(OUT_PATH))