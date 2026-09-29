from __future__ import annotations

from typing import Dict, Iterable, List, Optional
from dataclasses import dataclass


# -----------------------------
# Input model (alias-level hit)
# -----------------------------

@dataclass(frozen=True)
class AliasHit:
    """
    Alias match returned by search layer.

    DB-aligned IDs:
      - place_candidate_id: geo_work.place_candidates.place_candidate_id
      - alias_candidate_id: geo_work.alias_candidates.alias_candidate_id
    """
    place_candidate_id: str
    alias_candidate_id: str
    score: float
    alias: Optional[str] = None  # optional debug


# -----------------------------
# Output model (place-level)
# -----------------------------

@dataclass(frozen=True)
class PlaceCandidate:
    """
    Lightweight place hypothesis (DB-aligned).
    """
    place_candidate_id: str
    score: float
    best_alias_candidate_id: str
    best_alias: Optional[str] = None


# -----------------------------
# Public API
# -----------------------------

def build_place_candidates(
    alias_hits: Iterable[AliasHit],
    *,
    min_score: float = 0.0,
    max_places: int = 50,
) -> List[PlaceCandidate]:
    """
    Build flat place candidates from alias hits.

    Math:
      S(P) = max_{alias ∈ P} score(q, alias)

    This is EARLY PRUNING:
      - no evidence tracking (beyond best alias)
      - no geometry
      - no ML
    """

    best_by_place: Dict[str, PlaceCandidate] = {}

    for h in alias_hits:
        if not h.place_candidate_id or not h.alias_candidate_id:
            continue
        if h.score < min_score:
            continue

        prev = best_by_place.get(h.place_candidate_id)

        if prev is None or h.score > prev.score:
            best_by_place[h.place_candidate_id] = PlaceCandidate(
                place_candidate_id=h.place_candidate_id,
                score=float(h.score),
                best_alias_candidate_id=h.alias_candidate_id,
                best_alias=h.alias,
            )

    if not best_by_place:
        return []

    candidates = list(best_by_place.values())
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:max_places]
