from __future__ import annotations

def baseline_set_score(
    n_candidates: int,
    n_clusters: int,
    mean_prob_stop: float,
    duplication_rate: float,
) -> float:
    """
    Simple heuristic scoring for Phase 1.
    """

    if n_candidates == 0:
        return 0.0

    cluster_ratio = n_clusters / max(n_candidates, 1)

    score = (
        0.4 * mean_prob_stop +
        0.3 * cluster_ratio +
        0.2 * (1.0 - duplication_rate) +
        0.1 * min(n_candidates / 500.0, 1.0)
    )

    return float(score)


# ✅ canonical public API (used by pipeline)
def score_set(
    *,
    n_candidates: int,
    n_clusters: int,
    mean_prob_stop: float,
    duplication_rate: float,
) -> float:
    return baseline_set_score(
        n_candidates=n_candidates,
        n_clusters=n_clusters,
        mean_prob_stop=mean_prob_stop,
        duplication_rate=duplication_rate,
    )
