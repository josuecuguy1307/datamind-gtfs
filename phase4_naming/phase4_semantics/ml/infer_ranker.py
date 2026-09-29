"""
Phase 4 – Inference ranker

Uses a trained ranking model to order candidate names by
learned human preference.

NO training
NO persistence
NO deterministic scoring
"""

from typing import List, Dict, Tuple, Any
import math


# -------------------------------------------------
# Public API
# -------------------------------------------------

def rank_candidates(
    candidates: List[str],
    features_by_candidate: Dict[str, Dict[str, float]],
    model,
) -> List[Tuple[str, float]]:
    """
    Rank candidates using a trained ranking model.

    Args:
        candidates: list of candidate identifiers (e.g. name strings)
        features_by_candidate: candidate -> feature dict
        model: trained ranker with a `predict(X)` method

    Returns:
        List of (candidate, score), sorted by descending preference
    """

    X = []
    valid_candidates = []

    for c in candidates:
        feats = features_by_candidate.get(c)
        if feats is None:
            continue

        X.append(list(feats.values()))
        valid_candidates.append(c)

    if not X:
        return []

    # Model inference
    scores = model.predict(X)

    # Pair candidates with scores
    ranked = list(zip(valid_candidates, scores))

    # Sort by descending score
    ranked.sort(key=lambda x: x[1], reverse=True)

    return ranked


# -------------------------------------------------
# Optional: score normalization
# -------------------------------------------------

def normalize_scores(
    ranked: List[Tuple[str, float]],
) -> List[Tuple[str, float]]:
    """
    Normalize scores to [0,1] using a logistic transform.
    Useful when combining with deterministic scores.
    """

    if not ranked:
        return []

    scores = [s for _, s in ranked]
    mean = sum(scores) / len(scores)

    def sigmoid(z):
        return 1.0 / (1.0 + math.exp(-z))

    normalized = [
        (c, sigmoid(s - mean))
        for c, s in ranked
    ]

    return normalized


