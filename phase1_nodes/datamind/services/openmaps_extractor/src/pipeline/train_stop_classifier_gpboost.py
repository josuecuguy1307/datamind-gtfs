"""
GPBoost evaluation: LightGBM + spatial Gaussian Process for STOP/POI classification.

Uses the same training data and features as train_stop_classifier.py.
The spatial GP captures geographic correlation between nearby nodes.

Usage:
    python -m phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.train_stop_classifier_gpboost
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PHASE1_MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "stop_poi"


def _load_training_data_with_coords() -> pd.DataFrame:
    """Load features + labels + coordinates from DB."""
    from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import db_conn, fetchall

    with db_conn() as conn:
        rows = fetchall(conn, """
            SELECT
                f.node_candidate_id,
                c.tag_kind,
                ST_X(c.geom) AS lon,
                ST_Y(c.geom) AS lat,
                f.has_name,
                f.has_ref,
                f.has_operator,
                f.confidence_v0,
                f.has_shelter,
                f.has_bench,
                f.has_route_ref,
                f.primary_tag_category,
                f.tag_richness,
                f.distance_to_nearest_road_m,
                f.distance_to_nearest_stop_m,
                f.nearby_stop_density_100m,
                f.nearby_poi_density_100m,
                f.on_road_way
            FROM node_work.node_features f
            JOIN node_work.node_candidates c
              ON c.node_candidate_id = f.node_candidate_id
            WHERE c.tag_kind IN ('bus_stop', 'platform', 'station', 'stop_position', 'poi')
              AND c.geom IS NOT NULL
        """)

    df = pd.DataFrame(rows)
    logger.info("Loaded %d rows with coordinates", len(df))
    return df


def _prepare(df: pd.DataFrame) -> tuple:
    """Prepare features, labels, and coordinate arrays."""
    df["label"] = df["tag_kind"].apply(
        lambda tk: 1 if tk in ("bus_stop", "platform", "station", "stop_position") else 0
    )

    df["primary_tag_category"] = df["primary_tag_category"].fillna("UNKNOWN")
    category_map = {"TRANSIT": 0, "COMMERCIAL": 1, "CIVIC": 2, "RECREATION": 3, "UNKNOWN": 4}
    df["tag_category_code"] = df["primary_tag_category"].map(category_map).fillna(4).astype(int)

    # Excluded for label leakage:
    #   tag_category_code — derived from same OSM tags as tag_kind (the label)
    #   distance_to_nearest_stop_m — computed using tag_kind filter (= positive class)
    #   nearby_stop_density_100m — same
    #   nearby_poi_density_100m — computed using tag_kind='poi' (= negative class)
    feature_cols = [
        "has_name", "has_ref", "has_operator", "confidence_v0",
        "has_shelter", "has_bench", "has_route_ref",
        "tag_richness",
        "distance_to_nearest_road_m",
        "on_road_way",
    ]

    for col in feature_cols:
        if col in ("has_name", "has_ref", "has_operator", "has_shelter", "has_bench", "has_route_ref", "on_road_way"):
            df[col] = df[col].fillna(False).astype(int)
        elif col in ("tag_richness", "nearby_stop_density_100m", "nearby_poi_density_100m", "tag_category_code"):
            df[col] = df[col].fillna(0).astype(int)
        else:
            df[col] = df[col].fillna(df[col].median() if df[col].notna().any() else 0)

    available = [c for c in feature_cols if df[c].nunique() > 1 or df[c].notna().sum() > 0]

    X = df[available]
    y = df["label"].values
    coords = df[["lon", "lat"]].values

    return X, y, coords, available


def train_gpboost(X: pd.DataFrame, y: np.ndarray, coords: np.ndarray,
                  feature_names: list[str], sample_size: int = 10_000) -> Dict[str, Any]:
    """Train GPBoost (LightGBM + spatial GP) and return results."""
    import gpboost as gpb
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import roc_auc_score, precision_score, recall_score, f1_score, confusion_matrix

    # Sample if too large for GP (GP is O(n^2) or O(n*m) with Vecchia)
    if len(X) > sample_size:
        logger.info("Sampling %d from %d rows for GP tractability", sample_size, len(X))
        idx = np.random.RandomState(42).choice(len(X), sample_size, replace=False)
        X = X.iloc[idx].reset_index(drop=True)
        y = y[idx]
        coords = coords[idx]

    X_train, X_test, y_train, y_test, coords_train, coords_test = train_test_split(
        X, y, coords, test_size=0.2, random_state=42, stratify=y,
    )

    # GP model with Vecchia approximation for scalability
    gp_model = gpb.GPModel(
        gp_coords=coords_train,
        cov_function="exponential",
        likelihood="bernoulli_logit",
        gp_approx="vecchia",
        num_neighbors=20,
    )

    train_data = gpb.Dataset(X_train, label=y_train)

    params = {
        "objective": "binary",
        "num_leaves": 31,
        "learning_rate": 0.05,
        "min_data_in_leaf": 100,
        "verbose": 0,
    }

    t0 = time.time()
    gpboost_model = gpb.train(
        params=params,
        train_set=train_data,
        gp_model=gp_model,
        num_boost_round=200,
    )
    train_time = time.time() - t0

    # Predict
    t0 = time.time()
    pred_resp = gpboost_model.predict(
        data=X_test,
        gp_coords_pred=coords_test,
        pred_latent=False,
    )
    pred_time = time.time() - t0

    y_prob = pred_resp["response_mean"] if isinstance(pred_resp, dict) else pred_resp
    y_pred = (np.array(y_prob) >= 0.5).astype(int)

    auc = roc_auc_score(y_test, y_prob)
    precision = precision_score(y_test, y_pred)
    recall = recall_score(y_test, y_pred)
    f1 = f1_score(y_test, y_pred)
    cm = confusion_matrix(y_test, y_pred)

    # GP parameters
    gp_params = gp_model.get_cov_pars() if hasattr(gp_model, "get_cov_pars") else {}

    results = {
        "model_version": "gpboost_v1_spatial",
        "training_date": datetime.now(timezone.utc).isoformat(),
        "train_rows": len(X_train),
        "test_rows": len(X_test),
        "sample_size": len(X),
        "features": feature_names,
        "auc": round(auc, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "confusion_matrix": cm.tolist(),
        "train_time_s": round(train_time, 2),
        "predict_time_s": round(pred_time, 4),
        "gp_covariance_params": {k: float(v) for k, v in gp_params.items()} if isinstance(gp_params, dict) else str(gp_params),
    }

    print("\n" + "=" * 60)
    print("GPBOOST (LightGBM + Spatial GP) RESULTS")
    print("=" * 60)
    print(f"  AUC:       {auc:.4f}")
    print(f"  Precision: {precision:.4f}")
    print(f"  Recall:    {recall:.4f}")
    print(f"  F1:        {f1:.4f}")
    print(f"  Train time: {train_time:.2f}s")
    print(f"  Predict time: {pred_time:.4f}s")
    print(f"  Sample size: {len(X)}")
    print(f"\nConfusion Matrix:")
    print(f"  TN={cm[0][0]}  FP={cm[0][1]}")
    print(f"  FN={cm[1][0]}  TP={cm[1][1]}")
    print(f"\nGP Covariance parameters: {gp_params}")
    print("=" * 60)

    # Save results
    PHASE1_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = PHASE1_MODEL_DIR / "gpboost_v1_meta.json"
    with open(meta_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info("GPBoost metadata saved to %s", meta_path)

    return results


def main():
    logger.info("Loading training data with coordinates...")
    df = _load_training_data_with_coords()

    if len(df) < 100:
        logger.error("Not enough data: %d rows", len(df))
        return

    X, y, coords, feature_names = _prepare(df)

    logger.info("Training GPBoost (LightGBM + spatial GP)...")
    try:
        results = train_gpboost(X, y, coords, feature_names)
    except ImportError:
        logger.error("gpboost not installed. Run: pip install gpboost")
        return
    except Exception as e:
        logger.error("GPBoost training failed: %s", e, exc_info=True)
        return

    # Load LightGBM results for comparison
    lgbm_meta_path = PHASE1_MODEL_DIR / "stop_poi_lgbm_v2_meta.json"
    if lgbm_meta_path.exists():
        with open(lgbm_meta_path) as f:
            lgbm_results = json.load(f)

        print(f"\n{'':20s} | {'LightGBM v2':>12s} | {'GPBoost':>12s} |")
        print(f"{'-'*20}-+-{'-'*12}-+-{'-'*12}-+")
        for metric in ["auc", "f1", "precision", "recall"]:
            lgbm_val = lgbm_results.get(metric, "N/A")
            gpb_val = results.get(metric, "N/A")
            print(f"  {metric:18s} | {lgbm_val:>12} | {gpb_val:>12} |")
        for metric in ["train_time_s", "predict_time_s"]:
            lgbm_val = lgbm_results.get(metric, "N/A")
            gpb_val = results.get(metric, "N/A")
            print(f"  {metric:18s} | {lgbm_val:>12} | {gpb_val:>12} |")

        auc_diff = results["auc"] - lgbm_results["auc"]
        if auc_diff > 0.02:
            print(f"\n>>> GPBoost AUC improvement: +{auc_diff:.4f} — RECOMMEND using GPBoost")
        else:
            print(f"\n>>> GPBoost AUC delta: {auc_diff:+.4f} — LightGBM with spatial features is sufficient")


if __name__ == "__main__":
    main()
