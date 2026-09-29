from __future__ import annotations
from typing import List, Dict


# -----------------------------
# Weights (hand-tuned for now)
# -----------------------------

W_SEMANTIC = 0.60
W_ALIAS = 0.20
W_EVIDENCE = 0.15
W_TYPE = 0.05


# -----------------------------
# Helper scorers
# -----------------------------

def alias_score(query: str, aliases: List[str]) -> float:
    """
    Lexical matching between query and aliases.
    """
    q = query.lower()

    if not aliases:
        return 0.0

    for a in aliases:
        a = a.lower()
        if q == a:
            return 1.0
        if q in a or a in q:
            return 0.7

    return 0.0


def evidence_score(evidence: List[Dict]) -> float:
    """
    Sum evidence weights (clipped).
    """
    if not evidence:
        return 0.0

    score = sum(e.get("weight", 0.0) for e in evidence)
    return min(score, 1.0)


def type_score(query: str, kind: str) -> float:
    """
    Boost if query intent matches object kind.
    """
    q = query.lower()

    stop_words = {"estacion", "terminal", "parada", "stop", "station"}
    poi_words = {"parque", "hospital", "universidad", "mall"}

    if kind == "stop" and any(w in q for w in stop_words):
        return 1.0

    if kind == "poi" and any(w in q for w in poi_words):
        return 1.0

    return 0.0


# -----------------------------
# Main rerank function
# -----------------------------

def rerank(
    query: str,
    candidates: List[Dict],
) -> List[Dict]:
    """
    Rerank semantic search candidates using multiple signals.
    """

    results: List[Dict] = []

    for c in candidates:
        payload = c.get("payload", {})

        s_sem = float(c.get("score", 0.0))
        s_alias = alias_score(query, payload.get("aliases", []))
        s_evid = evidence_score(payload.get("evidence", []))
        s_type = type_score(query, payload.get("kind", ""))

        final_score = (
            W_SEMANTIC * s_sem +
            W_ALIAS * s_alias +
            W_EVIDENCE * s_evid +
            W_TYPE * s_type
        )

        results.append({
            **c,
            "final_score": final_score,
            "score_components": {
                "semantic": s_sem,
                "alias": s_alias,
                "evidence": s_evid,
                "type": s_type,
            },
        })

    # sort descending
    results.sort(key=lambda x: x["final_score"], reverse=True)

    return results
