from __future__ import annotations

from typing import Any, Dict, Tuple


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def score_candidate(candidate: Dict[str, Any]) -> Tuple[float, Dict[str, float]]:
    route_name = str(candidate.get("route_name") or "").strip()
    route_ref = str(candidate.get("route_ref") or "").strip()
    operator = str(candidate.get("operator_name") or "").strip()
    from_name = str(candidate.get("from_name") or "").strip()
    to_name = str(candidate.get("to_name") or "").strip()

    name_len = len(route_name)
    len_score = 0.0
    if name_len > 0:
        if name_len < 8:
            len_score = 0.4
        elif name_len <= 64:
            len_score = 1.0
        else:
            len_score = 0.7

    has_ref = 1.0 if route_ref else 0.0
    has_operator = 1.0 if operator else 0.0
    has_from_to = 1.0 if from_name and to_name else 0.0

    source = str(candidate.get("source_type") or "")
    source_priority = 1.0 if source == "seed_osm" else 0.7
    if source == "user_input":
        source_priority = 0.8

    parts = {
        "length": len_score,
        "has_ref": has_ref,
        "has_operator": has_operator,
        "has_from_to": has_from_to,
        "source_priority": source_priority,
    }

    score = (
        0.35 * parts["length"]
        + 0.20 * parts["has_ref"]
        + 0.15 * parts["has_operator"]
        + 0.20 * parts["has_from_to"]
        + 0.10 * parts["source_priority"]
    )
    return (_clamp(score), parts)
