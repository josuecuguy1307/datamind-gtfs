from __future__ import annotations

from typing import Dict, List, Optional
from dataclasses import dataclass


# -----------------------------
# Data models (DB-aligned)
# -----------------------------

@dataclass(frozen=True)
class AliasHit:
    """
    One alias match (from semantic search) pointing to a geo_work.alias_candidates row.

    DB alignment:
      - place_candidate_id  ->_unit being scored
      - alias_candidate_id  -> evidence / best alias tracking
    """
    place_candidate_id: str
    alias_candidate_id: str
    score: float
    alias: Optional[str] = None  # optional debug / UI text


@dataclass(frozen=True)
class PlaceCandidateScore:
    """
    Aggregated score for a geo_work.place_candidates row.
    """
    place_candidate_id: str
    score: float
    best_alias_candidate_id: str
    best_alias: Optional[str]


# -----------------------------
# Core logic
# -----------------------------

def score_place_candidates(
    alias_hits: List[AliasHit],
    *,
    min_score: float = 0.0,
    max_places: int = 50,
) -> List[PlaceCandidateScore]:
    """
    Aggregate alias hits into place-candidate scores.

    Math:
        S(C) = max_{alias ∈ C} score(q, alias)

    DB-aligned output:
      - place_candidate_id
      - best_alias_candidate_id (for provenance / UI)
    """

    if not alias_hits:
        return []

    best_by_place: Dict[str, PlaceCandidateScore] = {}

    for hit in alias_hits:
        if hit.score < min_score:
            continue

        existing = best_by_place.get(hit.place_candidate_id)

        if existing is None or hit.score > existing.score:
            best_by_place[hit.place_candidate_id] = PlaceCandidateScore(
                place_candidate_id=hit.place_candidate_id,
                score=float(hit.score),
                best_alias_candidate_id=hit.alias_candidate_id,
                best_alias=hit.alias,
            )

    ranked = sorted(
        best_by_place.values(),
        key=lambda p: p.score,
        reverse=True,
    )

    return ranked[:max_places]
