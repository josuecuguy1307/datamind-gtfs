"""
Phase 4 – Dataset construction for learning-to-rank

This module prepares feature matrices and preference pairs.
NO ML models. NO persistence. NO scoring.
"""

from typing import Dict, List, Tuple


# -------------------------------------------------
# Inference dataset (ranking candidates for a route)
# -------------------------------------------------

def build_inference_dataset(
    candidates: List[str],
    features_by_candidate: Dict[str, Dict[str, float]],
) -> Tuple[List[List[float]], List[str]]:
    """
    Builds an inference dataset for ranking.

    Returns:
        X  : list of feature vectors
        ids: list of candidate identifiers (names or IDs)
    """

    X = []
    ids = []

    for cid in candidates:
        feats = features_by_candidate.get(cid)
        if feats is None:
            continue

        X.append(list(feats.values()))
        ids.append(cid)

    return X, ids


# -------------------------------------------------
# Training dataset (pairwise ranking)
# -------------------------------------------------

def build_pairwise_dataset(
    preferences: List[Tuple[str, str, int]],
    features_by_candidate: Dict[str, Dict[str, float]],
) -> Tuple[List[List[float]], List[List[float]], List[int]]:
    """
    Builds a pairwise ranking dataset.

    preferences:
        [(candidate_i, candidate_j, y_ij)]
        y_ij = 1 if i preferred over j, else 0

    Returns:
        X_i, X_j, y
    """

    X_i = []
    X_j = []
    y = []

    for ci, cj, pref in preferences:
        fi = features_by_candidate.get(ci)
        fj = features_by_candidate.get(cj)

        if fi is None or fj is None:
            continue

        X_i.append(list(fi.values()))
        X_j.append(list(fj.values()))
        y.append(int(pref))

    return X_i, X_j, y
