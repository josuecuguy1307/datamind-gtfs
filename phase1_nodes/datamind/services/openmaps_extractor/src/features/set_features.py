from __future__ import annotations
from typing import Dict

def baseline_set_score(
    n_candidates: int,
    n_clusters: int,
    mean_prob_stop: float,
    duplication_rate: float,
) -> float:
    # A simple weighted score. Tune later / replace by LightGBM ranker.
    # duplication_rate in [0,1] (1 = horrible duplicates)
    score = 0.0
    score += 0.35 * mean_prob_stop
    score += 0.25 * (1.0 - duplication_rate)
    score += 0.20 * min(1.0, n_candidates / 500.0)
    score += 0.20 * min(1.0, n_clusters / 300.0)
    return max(0.0, min(1.0, score))
