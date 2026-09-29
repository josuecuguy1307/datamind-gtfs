from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Literal
import math


# -----------------------------
# Input models
# -----------------------------

@dataclass(frozen=True)
class NodeHit:
    """
    Evidence at the node level.

    score:
      - bigger = better (semantic similarity / proximity / whatever)
    """
    node_id: str
    score: float

  
# -----------------------------
# Output model (DB-aligned)
# -----------------------------

@dataclass(frozen=True)
class PlaceNodeSupport:
    """
    Aggregated node evidence for a place candidate (Phase 2 geo_work).
    """
    place_candidate_id: str
    score: float
    best_node_id: str
    best_node_score: float
    node_hits_count: int


AggMethod = Literal["max", "softmax"]


# -----------------------------
# Aggregation helpers
# -----------------------------

def _softmax_pool(scores: List[float], alpha: float = 10.0) -> float:
    """
    Soft aggregation (log-sum-exp pooling):

      S = (1/alpha) * log(sum(exp(alpha * s_i)))

    Properties:
      - alpha -> +inf approximates max
      - uses all evidence (but still dominated by big scores)
    """
    if not scores:
        return float("-inf")

    m = max(scores)
    s = 0.0
    for x in scores:
        s += math.exp(alpha * (x - m))

    return m + (1.0 / alpha) * math.log(s)


# -----------------------------
# Public API
# -----------------------------

def map_nodes_to_places(
    node_hits: Iterable[NodeHit],
    node_to_place_candidate: Dict[str, str],
    *,
    min_score: float = 0.0,
    max_places: int = 50,
    method: AggMethod = "max",
    softmax_alpha: float = 10.0,
) -> List[PlaceNodeSupport]:
    """
    Map node-level evidence → place-candidate-level evidence.

    Input:
      - node_hits: NodeHit(node_id, score)
      - node_to_place_candidate: mapping node_id -> place_candidate_id

    Output:
      - ranked PlaceNodeSupport list (best first)

    Math:
      For each place candidate C with nodes N(C):
        - MAX pooling:
            S(C) = max_{n in N(C)} score(n)
        - SOFTMAX pooling:
            S(C) = (1/alpha) * log(sum(exp(alpha * score(n))))
    """

    scores_by_place: Dict[str, List[float]] = {}
    best_node_by_place: Dict[str, tuple[str, float]] = {}
    count_by_place: Dict[str, int] = {}

    for h in node_hits:
        if h.score < min_score:
            continue

        place_candidate_id = node_to_place_candidate.get(h.node_id)
        if not place_candidate_id:
            continue

        scores_by_place.setdefault(place_candidate_id, []).append(float(h.score))
        count_by_place[place_candidate_id] = count_by_place.get(place_candidate_id, 0) + 1

        prev_best = best_node_by_place.get(place_candidate_id)
        if prev_best is None or h.score > prev_best[1]:
            best_node_by_place[place_candidate_id] = (h.node_id, float(h.score))

    if not scores_by_place:
        return []

    out: List[PlaceNodeSupport] = []

    for place_candidate_id, scores in scores_by_place.items():
        best_node_id, best_node_score = best_node_by_place[place_candidate_id]

        if method == "max":
            place_score = best_node_score
        elif method == "softmax":
            place_score = _softmax_pool(scores, alpha=softmax_alpha)
        else:
            raise ValueError(f"Unknown method: {method}")

        out.append(
            PlaceNodeSupport(
                place_candidate_id=place_candidate_id,
                score=float(place_score),
                best_node_id=best_node_id,
                best_node_score=float(best_node_score),
                node_hits_count=int(count_by_place[place_candidate_id]),
            )
        )

    out.sort(key=lambda x: x.score, reverse=True)
    return out[:max_places]
