from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple

from src.evidence.overpass import search_route_relations, fetch_relation_overpass_json
from src.evidence.parse_relation import extract_stop_prior

BBox = Tuple[float, float, float, float]

def pick_best_relation(
    bbox: BBox,
    *,
    refs: Optional[List[str]] = None,
    operator_contains: Optional[str] = None,
    name_contains: Optional[str] = None,
    max_candidates: int = 20,
) -> Dict[str, Any]:
    """
    Returns best candidate:
      {
        "osm_relation_id": int,
        "tags": dict,
        "stop_prior_count": int,
      }
    """
    cands = search_route_relations(
        bbox,
        refs=refs,
        operator_contains=operator_contains,
        name_contains=name_contains,
        limit=max_candidates,
    )

    if not cands:
        raise RuntimeError("No route relations found in bbox with given filters.")

    best = None
    best_score = -1

    for c in cands:
        rid = c["id"]
        tags = c["tags"]

        # fetch full relation and count extracted stops
        rel_json = fetch_relation_overpass_json(rid)
        prior = extract_stop_prior(rel_json)
        prior_count = len(prior)

        # scoring: prefer route over route_master + more priors
        t = (tags.get("type") or "").lower()
        is_route = 1 if t == "route" else 0
        score = (1000 * is_route) + (10 * prior_count)

        if score > best_score:
            best_score = score
            best = {
                "osm_relation_id": rid,
                "tags": tags,
                "stop_prior_count": prior_count,
            }

    if not best:
        raise RuntimeError("No viable relation candidate scored.")

    return best
