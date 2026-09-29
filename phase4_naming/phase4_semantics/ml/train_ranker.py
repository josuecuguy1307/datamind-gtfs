"""
Phase 4 – Training the preference ranker (improved)

Learns a query-aware ranking function fθ from human preferences.
"""

from typing import Dict, List, Tuple
from lightgbm import LGBMRanker

from .model_io import save_model


def train_ranker(
    routes: Dict[str, List[str]],
    features_by_candidate: Dict[str, Dict[str, float]],
    preferences_by_route: Dict[str, List[str]],
    output_dir: str,
    feature_names: List[str],
):
    """
    Args:
        routes:
            route_id -> list of candidate IDs

        features_by_candidate:
            candidate_id -> feature dict

        preferences_by_route:
            route_id -> ordered list of preferred candidates
            (best first)

    This trains a query-aware LambdaRank model.
    """

    X = []
    y = []
    group = []

    # -----------------------------
    # Build ranking dataset
    # -----------------------------

    for route_id, candidates in routes.items():
        prefs = preferences_by_route.get(route_id, [])
        if not prefs:
            continue

        # relevance: higher = better
        relevance = {
            cid: len(prefs) - i
            for i, cid in enumerate(prefs)
        }

        start_len = len(X)

        for cid in candidates:
            feats = features_by_candidate.get(cid)
            if feats is None:
                continue

            X.append([feats[f] for f in feature_names])
            y.append(relevance.get(cid, 0))

        group.append(len(X) - start_len)

    # -----------------------------
    # Train LambdaRank model
    # -----------------------------

    model = LGBMRanker(
        objective="lambdarank",
        metric="ndcg",
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=31,
        min_data_in_leaf=10,
    )

    model.fit(
        X,
        y,
        group=group,
        eval_at=[1, 3, 5],
    )

    # -----------------------------
    # Persist model
    # -----------------------------

    save_model(
        model=model,
        output_dir=output_dir,
        feature_names=feature_names,
        model_version="v2-lambdarank",
    )

    return model
