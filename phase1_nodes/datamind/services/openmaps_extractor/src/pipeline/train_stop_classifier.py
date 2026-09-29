"""
Train the STOP/POI classifier using expanded tag-based + spatial features.

Training labels come from tag_kind in node_work.node_candidates:
  - bus_stop, platform, station, stop_position → label = 1 (STOP)
  - poi → label = 0 (POI)

Feature vector (up to 14 features):
  Original:     has_name, has_ref, has_operator, confidence_v0
  Tag-based:    has_shelter, has_bench, has_route_ref, primary_tag_category (encoded), tag_richness
  Spatial:      distance_to_nearest_road_m, distance_to_nearest_stop_m,
                nearby_stop_density_100m, nearby_poi_density_100m, on_road_way

Usage:
    python -m phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.train_stop_classifier
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MODEL_OUTPUT_DIR = Path(__file__).resolve().parents[5] / "datamind_console" / "phases" / "phase3_routes" / "stop_grounding" / "models"
# Also save a copy in the phase1 models dir
PHASE1_MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "stop_poi"


def _load_training_data() -> pd.DataFrame:
    """Load features + labels from DB."""
    from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import db_conn, fetchall

    with db_conn() as conn:
        rows = fetchall(conn, """
            SELECT
                f.node_candidate_id,
                c.tag_kind,
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
        """)

    df = pd.DataFrame(rows)
    logger.info("Loaded %d rows from DB", len(df))
    return df


def _prepare_features(df: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray, list[str]]:
    """Prepare feature matrix X and label vector y."""
    # Label: STOP=1, POI=0
    df["label"] = df["tag_kind"].apply(
        lambda tk: 1 if tk in ("bus_stop", "platform", "station", "stop_position") else 0
    )

    # Encode primary_tag_category as categorical codes
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

    # Fill NaN with defaults
    for col in feature_cols:
        if col in ("has_name", "has_ref", "has_operator", "has_shelter", "has_bench", "has_route_ref", "on_road_way"):
            df[col] = df[col].fillna(False).astype(int)
        elif col in ("tag_richness", "nearby_stop_density_100m", "nearby_poi_density_100m", "tag_category_code"):
            df[col] = df[col].fillna(0).astype(int)
        else:
            df[col] = df[col].fillna(df[col].median() if df[col].notna().any() else 0)

    # Drop features that are all-null (spatial features if not yet computed)
    available_features = []
    for col in feature_cols:
        if df[col].nunique() > 1 or df[col].notna().sum() > 0:
            available_features.append(col)
        else:
            logger.warning("Dropping feature %s — all values are identical or null", col)

    X = df[available_features]
    y = df["label"].values

    logger.info("Feature matrix: %d rows x %d features", X.shape[0], X.shape[1])
    logger.info("Label distribution: STOP=%d (%.1f%%), POI=%d (%.1f%%)",
                y.sum(), 100 * y.mean(), len(y) - y.sum(), 100 * (1 - y.mean()))

    return X, y, available_features


def train_lightgbm(X: pd.DataFrame, y: np.ndarray, feature_names: list[str]) -> Dict[str, Any]:
    """Train LightGBM classifier and return results."""
    import lightgbm as lgb
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import (
        roc_auc_score, precision_score, recall_score, f1_score, confusion_matrix,
    )

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y,
    )

    train_data = lgb.Dataset(X_train, label=y_train, feature_name=feature_names)
    valid_data = lgb.Dataset(X_test, label=y_test, feature_name=feature_names, reference=train_data)

    params = {
        "objective": "binary",
        "metric": "auc",
        "num_leaves": 31,
        "learning_rate": 0.05,
        "min_data_in_leaf": 100,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "is_unbalance": True,
        "verbose": -1,
    }

    t0 = time.time()
    callbacks = [lgb.early_stopping(50), lgb.log_evaluation(100)]
    model = lgb.train(
        params,
        train_data,
        num_boost_round=2000,
        valid_sets=[valid_data],
        callbacks=callbacks,
    )
    train_time = time.time() - t0

    # Evaluate
    t0 = time.time()
    y_prob = model.predict(X_test)
    pred_time = time.time() - t0
    y_pred = (y_prob >= 0.5).astype(int)

    auc = roc_auc_score(y_test, y_prob)
    precision = precision_score(y_test, y_pred)
    recall = recall_score(y_test, y_pred)
    f1 = f1_score(y_test, y_pred)
    cm = confusion_matrix(y_test, y_pred)

    # Feature importance
    importance = dict(zip(feature_names, model.feature_importance(importance_type="gain").tolist()))
    sorted_importance = dict(sorted(importance.items(), key=lambda x: -x[1]))

    results = {
        "model_version": "v3_spatial",
        "training_date": datetime.now(timezone.utc).isoformat(),
        "train_rows": len(X_train),
        "test_rows": len(X_test),
        "features": feature_names,
        "n_features": len(feature_names),
        "params": params,
        "auc": round(auc, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "confusion_matrix": cm.tolist(),
        "feature_importance": sorted_importance,
        "train_time_s": round(train_time, 2),
        "predict_time_s": round(pred_time, 4),
        "best_iteration": model.best_iteration,
    }

    # Print results
    print("\n" + "=" * 60)
    print("LIGHTGBM v2 TRAINING RESULTS")
    print("=" * 60)
    print(f"  AUC:       {auc:.4f}")
    print(f"  Precision: {precision:.4f}")
    print(f"  Recall:    {recall:.4f}")
    print(f"  F1:        {f1:.4f}")
    print(f"  Train time: {train_time:.2f}s")
    print(f"  Predict time: {pred_time:.4f}s")
    print(f"\nConfusion Matrix:")
    print(f"  TN={cm[0][0]}  FP={cm[0][1]}")
    print(f"  FN={cm[1][0]}  TP={cm[1][1]}")
    print(f"\nTop features by gain:")
    for i, (feat, gain) in enumerate(sorted_importance.items()):
        if i >= 14:
            break
        print(f"  {i+1:2d}. {feat:35s} {gain:10.1f}")
    print("=" * 60)

    # Save model
    MODEL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PHASE1_MODEL_DIR.mkdir(parents=True, exist_ok=True)

    model_path = PHASE1_MODEL_DIR / "stop_poi_lgbm_v3.txt"
    model.save_model(str(model_path))
    logger.info("Model saved to %s", model_path)

    # Also save copy for phase3 stop grounding
    model_path2 = MODEL_OUTPUT_DIR / "stop_poi_lgbm_v3.txt"
    model.save_model(str(model_path2))

    meta_path = PHASE1_MODEL_DIR / "stop_poi_lgbm_v3_meta.json"
    with open(meta_path, "w") as f:
        json.dump(results, f, indent=2)
    logger.info("Metadata saved to %s", meta_path)

    return results


def compare_with_baseline(X: pd.DataFrame, y: np.ndarray, feature_names: list[str]) -> None:
    """Compare new model against the baseline heuristic on the same test split."""
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import roc_auc_score, f1_score

    _, X_test, _, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y,
    )

    # Baseline: confidence_v0 as probability
    if "confidence_v0" in feature_names:
        baseline_prob = X_test["confidence_v0"].values
        baseline_pred = (baseline_prob >= 0.5).astype(int)
        baseline_auc = roc_auc_score(y_test, baseline_prob)
        baseline_f1 = f1_score(y_test, baseline_pred)

        print(f"\n{'BASELINE (confidence_v0 only)':35s}: AUC={baseline_auc:.4f}  F1={baseline_f1:.4f}")


def main():
    logger.info("Loading training data...")
    df = _load_training_data()

    if len(df) < 100:
        logger.error("Not enough data to train: %d rows", len(df))
        return

    X, y, feature_names = _prepare_features(df)

    logger.info("Training LightGBM v2...")
    results = train_lightgbm(X, y, feature_names)

    logger.info("Comparing with baseline...")
    compare_with_baseline(X, y, feature_names)

    print(f"\nDone. Model version: {results['model_version']}")
    print(f"Saved to: {PHASE1_MODEL_DIR / 'stop_poi_lgbm_v3.txt'}")


if __name__ == "__main__":
    main()
