"""
Layer 1 — Training data builder for the on-route classifier.

Builds training samples from approved `route_prod.routes` records using the
same corridor-intersection and feature extraction logic as live inference.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from datamind_console.db.db import db_conn, fetch_all
from datamind_console.phases.phase3_routes.stop_grounding.corridor_builder import (
    rebuild_corridor_with_sequence,
)
from datamind_console.phases.phase3_routes.stop_grounding.corridor_stop_intersector import (
    intersect_corridor_with_stops,
)
from datamind_console.phases.phase3_routes.stop_grounding.geography_guardrails import (
    derive_expected_geographic_envelope,
)
from datamind_console.phases.phase3_routes.stop_grounding.on_route_classifier import (
    FEATURE_COLUMNS,
    _apply_bearing_alignment,
    _lgbm_features_for_candidate,
    heuristic_on_route_score,
)

_LOG = logging.getLogger(__name__)

LABEL_COLUMN = "is_on_route"

_PROD_ROUTES_SQL = """
SELECT
    r.route_id::text,
    r.route_name,
    r.stop_node_ids::text[] AS stop_node_ids,
    ST_AsGeoJSON(r.geom)::jsonb AS geom_geojson,
    ST_Length(r.geom::geography) / 1000.0 AS length_km,
    array_length(r.stop_node_ids, 1) AS n_stops,
    rs.operator_name
FROM route_prod.routes r
LEFT JOIN route_prod.route_semantics rs ON rs.route_id = r.route_id
WHERE r.stop_node_ids IS NOT NULL
  AND array_length(r.stop_node_ids, 1) >= 3
  AND r.geom IS NOT NULL
ORDER BY r.route_name
"""

_STOP_SEQUENCE_SQL = """
SELECT
    seq.ord,
    n.node_id::text AS stop_id,
    COALESCE(NULLIF(BTRIM(n.name), ''), NULLIF(BTRIM(n.ref), ''), 'stop_' || LEFT(n.node_id::text, 8)) AS stop_name,
    ST_Y(n.geom) AS lat,
    ST_X(n.geom) AS lon
FROM unnest(%(stop_ids)s::uuid[]) WITH ORDINALITY AS seq(stop_id, ord)
JOIN node_prod.nodes n ON n.node_id = seq.stop_id
ORDER BY seq.ord
"""


def _fetch_prod_routes(conn=None) -> List[Dict[str, Any]]:
    def _query(connection):
        return fetch_all(connection, _PROD_ROUTES_SQL)

    if conn is not None:
        return _query(conn)
    with db_conn(readonly=True) as connection:
        return _query(connection)


def _fetch_sequence_stops(stop_ids: List[str], *, conn) -> List[Dict[str, Any]]:
    if not stop_ids:
        return []
    return fetch_all(conn, _STOP_SEQUENCE_SQL, {"stop_ids": stop_ids})


def _build_corridor_geojson(
    route: Dict[str, Any],
    sequence_stops: List[Dict[str, Any]],
    *,
    expected_envelope: Dict[str, Any],
) -> Dict[str, Any]:
    if len(sequence_stops) >= 2:
        rebuilt = rebuild_corridor_with_sequence(
            [
                {"stop_id": stop["stop_id"], "lat": stop["lat"], "lon": stop["lon"]}
                for stop in sequence_stops
            ],
            expected_envelope=expected_envelope,
            timeout_s=45,
        )
        if rebuilt.corridor_geojson:
            return rebuilt.corridor_geojson

    geom_geojson = route.get("geom_geojson")
    if isinstance(geom_geojson, str):
        return json.loads(geom_geojson)
    return dict(geom_geojson or {})


def _build_samples_for_route(
    route: Dict[str, Any],
    *,
    buffer_passes: Optional[List[int]] = None,
    conn=None,
) -> List[Dict[str, Any]]:
    buffer_passes = buffer_passes or [50, 100, 200]
    stop_ids = [str(stop_id) for stop_id in list(route.get("stop_node_ids") or [])]
    prod_stop_ids = set(stop_ids)
    sequence_stops = _fetch_sequence_stops(stop_ids, conn=conn)
    if len(sequence_stops) < 2:
        return []

    route_name = str(route.get("route_name") or "")
    operator_name = str(route.get("operator_name") or "")
    locality_hints = []
    first_name = str(sequence_stops[0].get("stop_name") or "")
    last_name = str(sequence_stops[-1].get("stop_name") or "")
    expected_envelope = derive_expected_geographic_envelope(
        route_name=route_name,
        operator_name=operator_name,
        corridor_description=route_name,
        anchor_a_hint=first_name,
        anchor_b_hint=last_name,
        intermediate_hints=[str(item.get("stop_name") or "") for item in sequence_stops[1:-1]],
        locality_hints=locality_hints,
        sequence_seed_fragments=[str(item.get("stop_name") or "") for item in sequence_stops],
        source_notes=[],
        province="sample_region",  # LGBM training runs only on Sample Region route_prod.routes → Path 1 legacy
    )
    corridor_geojson = _build_corridor_geojson(route, sequence_stops, expected_envelope=expected_envelope)
    if not corridor_geojson:
        return []

    known_anchor_ids = {stop_ids[0], stop_ids[-1]}
    known_intermediate_ids = set(stop_ids[1:-1])
    # Camino D PIEZA 7: LGBM training is Sample Region-only by design (reads from
    # route_prod.routes which contains only Sample Region today). Hardcoded to keep
    # retrocompat explicit at the boundary; do not generalize this file without
    # also generalizing the training data source.
    intersection = intersect_corridor_with_stops(
        corridor_geojson,
        operator_name=operator_name,
        cooperative_name=operator_name,
        locality_hints=locality_hints,
        expected_envelope=expected_envelope,
        known_anchor_ids=known_anchor_ids,
        known_intermediate_ids=known_intermediate_ids,
        buffer_passes=buffer_passes,
        corridor_length_km=float(route.get("length_km") or 0.0),
        conn=conn,
        province="sample_region",
    )

    candidates = list(intersection.ordered_candidates)
    _apply_bearing_alignment(candidates, corridor_geojson)
    for candidate in candidates:
        candidate.on_route_score = heuristic_on_route_score(candidate)

    samples: List[Dict[str, Any]] = []
    for idx, candidate in enumerate(candidates):
        feature_values = _lgbm_features_for_candidate(candidate, idx, candidates)
        sample = {
            "route_id": route["route_id"],
            "route_name": route_name,
            "stop_id": candidate.stop_id,
            "stop_name": candidate.stop_name,
            LABEL_COLUMN: 1 if candidate.stop_id in prod_stop_ids else 0,
        }
        for column, value in zip(FEATURE_COLUMNS, feature_values):
            sample[column] = value
        samples.append(sample)
    return samples


def build_training_dataset(
    *,
    buffer_passes: Optional[List[int]] = None,
    max_routes: Optional[int] = None,
    conn=None,
) -> List[Dict[str, Any]]:
    buffer_passes = buffer_passes or [50, 100, 200]

    def _build(connection):
        routes = _fetch_prod_routes(conn=connection)
        if max_routes:
            routes = routes[:max_routes]
        _LOG.info("Building training data from %d approved prod routes", len(routes))
        all_samples: List[Dict[str, Any]] = []
        for idx, route in enumerate(routes):
            try:
                samples = _build_samples_for_route(route, buffer_passes=buffer_passes, conn=connection)
                pos = sum(1 for item in samples if item[LABEL_COLUMN] == 1)
                _LOG.info(
                    "[%d/%d] %s: %d samples (%d pos, %d neg)",
                    idx + 1,
                    len(routes),
                    route.get("route_name", "?"),
                    len(samples),
                    pos,
                    len(samples) - pos,
                )
                all_samples.extend(samples)
            except Exception as exc:
                _LOG.warning("[%d/%d] %s failed: %s", idx + 1, len(routes), route.get("route_name", "?"), exc)
        return all_samples

    if conn is not None:
        return _build(conn)
    with db_conn(readonly=True) as connection:
        return _build(connection)


def save_training_dataset(samples: List[Dict[str, Any]], output_path: str) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".csv":
        import csv

        fieldnames = list(samples[0].keys()) if samples else []
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if fieldnames:
                writer.writeheader()
                writer.writerows(samples)
    else:
        with path.open("w", encoding="utf-8") as handle:
            for row in samples:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return str(path)


def _ensemble_weights(trainable_routes: int, auc: float = 0.0) -> Dict[str, float]:
    # AUC-based weights take precedence if AUC is known
    if auc > 0:
        if auc >= 0.99:
            # Likely overfit — fallback to heuristic-heavy
            return {"heuristic": 0.80, "lgbm": 0.20}
        if auc > 0.95:
            return {"heuristic": 0.50, "lgbm": 0.50}
        if auc > 0.90:
            return {"heuristic": 0.35, "lgbm": 0.65}
        if auc > 0.80:
            return {"heuristic": 0.50, "lgbm": 0.50}
        if auc > 0.70:
            return {"heuristic": 0.65, "lgbm": 0.35}
        return {"heuristic": 1.0, "lgbm": 0.0}  # model not reliable

    if trainable_routes < 5:
        return {"heuristic": 0.65, "lgbm": 0.35}
    if trainable_routes <= 10:
        return {"heuristic": 0.50, "lgbm": 0.50}
    return {"heuristic": 0.35, "lgbm": 0.65}


def _calibrate_threshold(
    y_true,
    heuristic_scores,
    lgbm_scores,
    *,
    weights: Dict[str, float],
) -> Dict[str, Any]:
    import numpy as np
    from sklearn.metrics import f1_score, precision_score, recall_score, roc_curve

    ensemble_scores = (
        weights["heuristic"] * heuristic_scores
        + weights["lgbm"] * lgbm_scores
    )

    # Youden's J statistic: threshold = argmax(TPR - FPR)
    fpr, tpr, thresholds_roc = roc_curve(y_true, ensemble_scores)
    j_scores = tpr - fpr
    best_j_idx = int(np.argmax(j_scores))
    youden_threshold = float(thresholds_roc[best_j_idx]) if len(thresholds_roc) > best_j_idx else 0.50

    # Also sweep for best F1
    best = {"threshold": 0.50, "f1": -1.0, "precision": 0.0, "recall": 0.0}
    for threshold in np.arange(0.30, 0.71, 0.02):
        y_pred = (ensemble_scores >= threshold).astype(int)
        f1 = f1_score(y_true, y_pred, zero_division=0)
        precision = precision_score(y_true, y_pred, zero_division=0)
        recall = recall_score(y_true, y_pred, zero_division=0)
        if f1 > best["f1"]:
            best = {
                "threshold": round(float(threshold), 4),
                "f1": round(float(f1), 4),
                "precision": round(float(precision), 4),
                "recall": round(float(recall), 4),
            }

    # Use Youden's J as primary, F1-sweep as backup, floor at 0.40
    final_threshold = max(0.40, min(youden_threshold, 0.60))
    if best["f1"] > 0 and best["threshold"] >= 0.40:
        final_threshold = max(final_threshold, best["threshold"])

    best["threshold"] = round(final_threshold, 4)
    best["youden_threshold"] = round(youden_threshold, 4)
    best["marginal_threshold"] = round(max(0.25, best["threshold"] * 0.64), 4)
    best["ensemble_score_percentiles"] = {
        "p10": round(float(np.percentile(ensemble_scores, 10)), 4),
        "p50": round(float(np.percentile(ensemble_scores, 50)), 4),
        "p90": round(float(np.percentile(ensemble_scores, 90)), 4),
    }
    return best


def train_lightgbm_model(
    samples: List[Dict[str, Any]],
    *,
    output_model_path: Optional[str] = None,
    output_meta_path: Optional[str] = None,
    test_size: float = 0.2,
) -> Dict[str, Any]:
    import lightgbm as lgb
    import numpy as np
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split

    if len(samples) < 50:
        return {
            "status": "insufficient_data",
            "error": f"Need at least 50 samples, got {len(samples)}",
        }

    X = np.array([[float(row.get(column, 0.0)) for column in FEATURE_COLUMNS] for row in samples])
    y = np.array([int(row[LABEL_COLUMN]) for row in samples])
    route_ids = np.array([str(row.get("route_id") or "") for row in samples])

    # Route-aware split: entire routes go to train OR val, never both.
    # This prevents data leakage and forces the model to generalize.
    unique_routes = list(set(route_ids))
    if len(unique_routes) < 4:
        return {
            "status": "insufficient_routes",
            "error": f"Need at least 4 routes for route-aware split, got {len(unique_routes)}",
        }
    train_route_set, val_route_set = train_test_split(
        unique_routes, test_size=max(test_size, 0.20), random_state=42,
    )
    train_route_set = set(train_route_set)
    train_mask = np.array([rid in train_route_set for rid in route_ids])
    X_train, y_train = X[train_mask], y[train_mask]
    X_val, y_val = X[~train_mask], y[~train_mask]
    route_ids_train = route_ids[train_mask]
    route_ids_val = route_ids[~train_mask]

    params = {
        "objective": "binary",
        "metric": "auc",
        "num_leaves": 15,           # REDUCED from 31 (less overfit)
        "learning_rate": 0.03,      # REDUCED (slower, more general)
        "feature_fraction": 0.7,
        "bagging_fraction": 0.7,
        "bagging_freq": 5,
        "min_child_samples": 20,    # INCREASED (more regularization)
        "lambda_l1": 0.1,           # L1 regularization
        "lambda_l2": 1.0,           # L2 regularization
        "verbose": -1,
        "seed": 42,
    }

    train_data = lgb.Dataset(X_train, label=y_train, feature_name=FEATURE_COLUMNS)
    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data, feature_name=FEATURE_COLUMNS)
    model = lgb.train(
        params,
        train_data,
        num_boost_round=300,
        valid_sets=[val_data],
        callbacks=[lgb.early_stopping(30), lgb.log_evaluation(50)],
    )

    y_val_pred = model.predict(X_val)
    auc = float(roc_auc_score(y_val, y_val_pred))

    heuristic_idx = FEATURE_COLUMNS.index("heuristic_score")
    heuristic_val = X_val[:, heuristic_idx]
    trainable_routes = len({route_id for route_id in route_ids if route_id})
    weights = _ensemble_weights(trainable_routes, auc=auc)
    calibration = _calibrate_threshold(
        y_val,
        heuristic_val,
        y_val_pred,
        weights=weights,
    )

    meta = {
        "trainable_routes": trainable_routes,
        "ensemble_weights": weights,
        "on_route_threshold": calibration["threshold"],
        "marginal_threshold": calibration["marginal_threshold"],
        "feature_columns": FEATURE_COLUMNS,
        "validation": calibration,
    }

    if output_model_path:
        Path(output_model_path).parent.mkdir(parents=True, exist_ok=True)
        model.save_model(output_model_path)
    if output_meta_path:
        Path(output_meta_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_meta_path).write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "status": "trained",
        "auc": round(auc, 4),
        "train_samples": int(len(X_train)),
        "val_samples": int(len(X_val)),
        "positive_samples": int(y.sum()),
        "negative_samples": int(len(y) - y.sum()),
        "feature_importance": dict(zip(FEATURE_COLUMNS, model.feature_importance().tolist())),
        "feature_columns": FEATURE_COLUMNS,
        "ensemble_weights": weights,
        "on_route_threshold": calibration["threshold"],
        "marginal_threshold": calibration["marginal_threshold"],
        "validation": calibration,
        "model_path": output_model_path,
        "meta_path": output_meta_path,
        "model": model,
    }
