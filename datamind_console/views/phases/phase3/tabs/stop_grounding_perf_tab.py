"""
Stop Grounding Model Performance Tab

Evaluates the on-route classifier (heuristic + LightGBM) against
prod routes as ground truth. Shows:
  - Model metrics (AUC, precision, recall, F1)
  - Feature importance
  - Per-route breakdown
  - Score distribution charts
  - Training data summary
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st
import pandas as pd

_LOG = logging.getLogger(__name__)

_CONSOLE_ROOT = Path(__file__).resolve().parents[4]  # datamind_console/
_MODEL_PATH = str(
    _CONSOLE_ROOT / "phases" / "phase3_routes" / "stop_grounding" / "models" / "on_route_lgbm.txt"
)
_TRAINING_DATA_PATH = str(
    _CONSOLE_ROOT / "phases" / "phase3_routes" / "stop_grounding" / "training_data" / "on_route_training.csv"
)


def render_stop_grounding_perf_tab(ctx=None, client=None) -> None:
    st.subheader("Stop Grounding — Model Performance")
    st.caption(
        "Evaluate the on-route classifier against prod routes (ground truth). "
        "Prod routes with `stop_node_ids` + geometry provide labeled data."
    )

    tab_overview, tab_features, tab_routes, tab_retrain = st.tabs([
        "Overview", "Feature Importance", "Per-Route", "Retrain",
    ])

    with tab_overview:
        _render_overview()

    with tab_features:
        _render_feature_importance()

    with tab_routes:
        _render_per_route()

    with tab_retrain:
        _render_retrain()


# ---------------------------------------------------------------------------
# Overview tab
# ---------------------------------------------------------------------------

def _render_overview():
    # Check model exists
    model_exists = Path(_MODEL_PATH).exists()
    data_exists = Path(_TRAINING_DATA_PATH).exists()

    col1, col2 = st.columns(2)
    with col1:
        st.metric("Model file", "Available" if model_exists else "Not found")
    with col2:
        st.metric("Training data", "Available" if data_exists else "Not found")

    if not data_exists:
        st.warning("No training data found. Click 'Retrain' tab to build training data from prod routes.")
        return

    df = _load_training_data()
    if df is None or df.empty:
        st.warning("Training data is empty.")
        return

    # Dataset stats
    n_total = len(df)
    n_pos = int(df["is_on_route"].sum())
    n_neg = n_total - n_pos
    n_routes = df["route_id"].nunique()

    st.markdown("### Training Dataset")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total samples", f"{n_total:,}")
    c2.metric("Positive (on-route)", f"{n_pos:,}")
    c3.metric("Negative", f"{n_neg:,}")
    c4.metric("Routes used", n_routes)

    st.metric("Positive ratio", f"{n_pos / n_total * 100:.1f}%")

    # Run evaluation if model available
    if model_exists and st.button("Evaluate model on test split", type="primary"):
        with st.spinner("Evaluating..."):
            metrics = _evaluate_model(df)
        if metrics:
            _display_metrics(metrics)
            st.session_state["p3.grounding_perf.last_eval"] = metrics

    cached = st.session_state.get("p3.grounding_perf.last_eval")
    if cached:
        _display_metrics(cached)

    # Score distributions
    st.markdown("### Score Distributions")
    _render_score_distributions(df)


def _display_metrics(metrics: Dict[str, Any]):
    if metrics.get("error"):
        st.error(metrics["error"])
        return

    st.markdown("### Model Evaluation (test split)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("AUC", f"{metrics.get('auc', 0):.4f}")
    c2.metric("Precision", f"{metrics.get('precision', 0):.4f}")
    c3.metric("Recall", f"{metrics.get('recall', 0):.4f}")
    c4.metric("F1", f"{metrics.get('f1', 0):.4f}")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Train samples", metrics.get("train_samples", 0))
    c6.metric("Test samples", metrics.get("test_samples", 0))
    c7.metric("Pos (test)", metrics.get("pos_test", 0))
    c8.metric("Neg (test)", metrics.get("neg_test", 0))


def _render_score_distributions(df: pd.DataFrame):
    from datamind_console.phases.phase3_routes.stop_grounding.training_data_builder import (
        FEATURE_COLUMNS,
    )

    # Heuristic score distribution by label
    if "heuristic_score" in df.columns:
        st.markdown("#### Heuristic Score by Label")
        chart_data = pd.DataFrame({
            "On-route": df[df["is_on_route"] == 1]["heuristic_score"],
            "Not on-route": df[df["is_on_route"] == 0]["heuristic_score"],
        })
        st.bar_chart(
            pd.DataFrame({
                "On-route": df[df["is_on_route"] == 1]["heuristic_score"]
                .value_counts(bins=20, sort=False)
                .sort_index(),
                "Not on-route": df[df["is_on_route"] == 0]["heuristic_score"]
                .value_counts(bins=20, sort=False)
                .sort_index(),
            })
        )

    # Distance to corridor distribution
    if "distance_to_corridor_m" in df.columns:
        st.markdown("#### Distance to Corridor (m) by Label")
        st.bar_chart(
            pd.DataFrame({
                "On-route": df[df["is_on_route"] == 1]["distance_to_corridor_m"]
                .value_counts(bins=20, sort=False)
                .sort_index(),
                "Not on-route": df[df["is_on_route"] == 0]["distance_to_corridor_m"]
                .value_counts(bins=20, sort=False)
                .sort_index(),
            })
        )

    # Key feature stats
    st.markdown("#### Feature Statistics by Label")
    key_features = [
        "distance_to_corridor_m", "path_fraction", "gap_to_prev_m",
        "gap_to_next_m", "local_density_200m", "heuristic_score",
    ]
    available = [f for f in key_features if f in df.columns]
    if available:
        stats_rows = []
        for feat in available:
            on = df[df["is_on_route"] == 1][feat]
            off = df[df["is_on_route"] == 0][feat]
            stats_rows.append({
                "Feature": feat,
                "On-route mean": f"{on.mean():.2f}",
                "On-route median": f"{on.median():.2f}",
                "Not-on-route mean": f"{off.mean():.2f}",
                "Not-on-route median": f"{off.median():.2f}",
            })
        st.dataframe(pd.DataFrame(stats_rows), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Feature Importance tab
# ---------------------------------------------------------------------------

def _render_feature_importance():
    if not Path(_MODEL_PATH).exists():
        st.info("No trained model found. Train a model first.")
        return

    try:
        import lightgbm as lgb
        model = lgb.Booster(model_file=_MODEL_PATH)
    except ImportError:
        st.error("lightgbm not installed.")
        return
    except Exception as e:
        st.error(f"Failed to load model: {e}")
        return

    feature_names = model.feature_name()
    importance_split = model.feature_importance(importance_type="split").tolist()
    importance_gain = model.feature_importance(importance_type="gain").tolist()

    st.markdown("### Feature Importance")

    imp_df = pd.DataFrame({
        "Feature": feature_names,
        "Split count": importance_split,
        "Gain": [round(g, 1) for g in importance_gain],
    }).sort_values("Split count", ascending=False)

    # Bar chart
    st.markdown("#### By Split Count (how often used)")
    chart_df = imp_df.set_index("Feature")["Split count"].sort_values(ascending=True)
    st.bar_chart(chart_df)

    st.markdown("#### By Gain (how much it improves predictions)")
    chart_df2 = imp_df.set_index("Feature")["Gain"].sort_values(ascending=True)
    st.bar_chart(chart_df2)

    # Table
    st.markdown("#### Full Table")
    st.dataframe(imp_df, use_container_width=True, hide_index=True)

    # Model info
    st.markdown("### Model Info")
    st.json({
        "num_trees": model.num_trees(),
        "num_features": model.num_feature(),
        "model_path": _MODEL_PATH,
    })


# ---------------------------------------------------------------------------
# Per-Route tab
# ---------------------------------------------------------------------------

def _render_per_route():
    df = _load_training_data()
    if df is None or df.empty:
        st.info("No training data available.")
        return

    st.markdown("### Per-Route Breakdown")

    route_stats = []
    for route_id, group in df.groupby("route_id"):
        route_name = group["route_name"].iloc[0] if "route_name" in group.columns else "?"
        n = len(group)
        pos = int(group["is_on_route"].sum())
        neg = n - pos
        avg_dist_pos = group[group["is_on_route"] == 1]["distance_to_corridor_m"].mean() if pos > 0 else 0
        avg_dist_neg = group[group["is_on_route"] == 0]["distance_to_corridor_m"].mean() if neg > 0 else 0
        length_km = group["route_length_km"].iloc[0] if "route_length_km" in group.columns else 0
        n_stops = group["route_n_stops"].iloc[0] if "route_n_stops" in group.columns else 0

        route_stats.append({
            "Route": str(route_name)[:50] if route_name else str(route_id)[:12],
            "Samples": n,
            "Positive": pos,
            "Negative": neg,
            "Pos %": f"{pos / n * 100:.0f}%",
            "Avg dist (on-route)": f"{avg_dist_pos:.0f}m",
            "Avg dist (off-route)": f"{avg_dist_neg:.0f}m",
            "Length km": f"{length_km:.1f}",
            "Prod stops": int(n_stops),
        })

    stats_df = pd.DataFrame(route_stats)
    st.dataframe(stats_df, use_container_width=True, hide_index=True)

    # Summary
    total_pos = sum(r["Positive"] for r in route_stats)
    total_neg = sum(r["Negative"] for r in route_stats)
    st.caption(
        f"Total: {len(route_stats)} routes, {total_pos} positive, {total_neg} negative "
        f"({total_pos / (total_pos + total_neg) * 100:.1f}% positive rate)"
    )


# ---------------------------------------------------------------------------
# Retrain tab
# ---------------------------------------------------------------------------

def _render_retrain():
    st.markdown("### Retrain On-Route Classifier")
    st.caption(
        "Rebuilds training data from ALL prod routes and retrains the LightGBM model. "
        "Uses routes with `stop_node_ids` + geometry as ground truth."
    )

    col1, col2 = st.columns(2)
    with col1:
        max_routes = st.number_input("Max routes (0=all)", min_value=0, value=0, key="routes_perf_max_routes")
    with col2:
        test_size = st.slider("Test split %", 10, 40, 20, key="p3.perf.test_size") / 100.0

    if st.button("Build Training Data & Train Model", type="primary", use_container_width=True):
        with st.spinner("Building training data from prod routes..."):
            try:
                from datamind_console.phases.phase3_routes.stop_grounding.training_data_builder import (
                    build_training_dataset,
                    save_training_dataset,
                    train_lightgbm_model,
                )

                samples = build_training_dataset(
                    max_routes=max_routes if max_routes > 0 else None,
                )

                if not samples:
                    st.error("No training samples generated. Check that prod routes have stop_node_ids + geometry.")
                    return

                save_training_dataset(samples, _TRAINING_DATA_PATH)
                st.success(f"Training data: {len(samples)} samples saved.")

            except Exception as e:
                st.error(f"Failed to build training data: {e}")
                _LOG.exception("Training data build failed")
                return

        with st.spinner("Training LightGBM model..."):
            try:
                result = train_lightgbm_model(
                    samples,
                    output_model_path=_MODEL_PATH,
                    test_size=test_size,
                )

                if result.get("error"):
                    st.error(result["error"])
                    return

                # Reset cached model so next pipeline run uses new model
                import datamind_console.phases.phase3_routes.stop_grounding.on_route_classifier as mod
                mod._lgbm_model = None
                mod._lgbm_load_attempted = False

                st.success("Model trained successfully!")
                _display_metrics(result)
                st.session_state["p3.grounding_perf.last_eval"] = result

                # Show feature importance inline
                fi = result.get("feature_importance", {})
                if fi:
                    st.markdown("#### Feature Importance (split count)")
                    fi_df = pd.DataFrame(
                        sorted(fi.items(), key=lambda x: x[1], reverse=True),
                        columns=["Feature", "Importance"],
                    )
                    st.bar_chart(fi_df.set_index("Feature"))

            except Exception as e:
                st.error(f"Training failed: {e}")
                _LOG.exception("LightGBM training failed")

    # Quick eval without retraining
    st.divider()
    if st.button("Evaluate current model (no retrain)", use_container_width=True):
        df = _load_training_data()
        if df is None or df.empty:
            st.warning("No training data available. Build it first.")
            return
        with st.spinner("Evaluating..."):
            metrics = _evaluate_model(df)
        if metrics:
            _display_metrics(metrics)
            st.session_state["p3.grounding_perf.last_eval"] = metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def _load_training_data() -> Optional[pd.DataFrame]:
    if not Path(_TRAINING_DATA_PATH).exists():
        return None
    try:
        return pd.read_csv(_TRAINING_DATA_PATH)
    except Exception:
        return None


def _evaluate_model(df: pd.DataFrame) -> Dict[str, Any]:
    try:
        from datamind_console.phases.phase3_routes.stop_grounding.training_data_builder import (
            FEATURE_COLUMNS,
            LABEL_COLUMN,
        )
        import lightgbm as lgb
        import numpy as np
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import roc_auc_score, precision_score, recall_score, f1_score
    except ImportError as e:
        return {"error": f"Missing dependency: {e}"}

    if not Path(_MODEL_PATH).exists():
        return {"error": "Model file not found"}

    model = lgb.Booster(model_file=_MODEL_PATH)

    X = np.array([
        [float(row.get(col, 0)) for col in FEATURE_COLUMNS]
        for _, row in df.iterrows()
    ])
    y = np.array([int(row[LABEL_COLUMN]) for _, row in df.iterrows()])

    _, X_test, _, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y,
    )

    y_pred_prob = model.predict(X_test)
    y_pred = (y_pred_prob >= 0.5).astype(int)

    return {
        "auc": round(roc_auc_score(y_test, y_pred_prob), 4),
        "precision": round(precision_score(y_test, y_pred, zero_division=0), 4),
        "recall": round(recall_score(y_test, y_pred, zero_division=0), 4),
        "f1": round(f1_score(y_test, y_pred, zero_division=0), 4),
        "train_samples": len(X) - len(X_test),
        "test_samples": len(X_test),
        "pos_test": int(y_test.sum()),
        "neg_test": int(len(y_test) - y_test.sum()),
    }
