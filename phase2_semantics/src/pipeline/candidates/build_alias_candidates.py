from __future__ import annotations

from typing import Iterable, List, Dict, Any, Optional, Union
from dataclasses import dataclass
import hashlib
import uuid

from src.utils.text import normalize_text
from src.pipeline.embeddings.embedder import embed


# ============================================================
# geo_work.alias_candidates constraints
# ============================================================

ALIAS_KINDS = {"official", "short", "alt", "abbr", "historic", "typo_common"}

_KIND_PRIORITY = {
    "official": 5,
    "short": 4,
    "abbr": 3,
    "alt": 2,
    "historic": 1,
    "typo_common": 0,
}


def alias_kind_from_source(source: Optional[str]) -> str:
    """
    Map geo_raw.name_evidence.source -> geo_work.alias_candidates.alias_kind
    Must match SQL CHECK constraint.
    """
    if not source:
        return "alt"

    mapping = {
        "name": "official",
        "name:es": "official",
        "official_name": "official",
        "short_name": "short",
        "alt_name": "alt",
        "old_name": "historic",
        "loc_name": "alt",
        "ref": "abbr",
        "operator": "alt",
        "network": "alt",
        "wikipedia": "alt",
        "wikidata": "alt",
    }
    return mapping.get(source, "alt")


def normalize_alias_score(weight_hint: Optional[float]) -> Optional[float]:
    """
    Normalize weight_hint (often 0–3+) into a clean [0,1] score for alias_candidates.score.
    If missing -> None.
    """
    if weight_hint is None:
        return None
    try:
        w = float(weight_hint)
    except Exception:
        return None
    if w <= 0:
        return 0.0
    return min(w / 3.0, 1.0)


def _should_skip_alias(text: str) -> bool:
    """
    Step 10 sometimes stores tag signals like:
        tag_signal:highway=bus_stop
    Those must NOT become alias strings.
    """
    if not text:
        return True
    if text.startswith("tag_signal:"):
        return True
    return False


def _alias_uuid(place_candidate_id: str, alias_norm: str) -> str:
    """
    Deterministic UUID (stable across runs).
    DB expects UUID, not sha1 text.
    """
    # uuid5 produces deterministic UUID from a namespace + name
    u = uuid.uuid5(uuid.NAMESPACE_DNS, f"{place_candidate_id}:{alias_norm}")
    return str(u)


# ============================================================
# Public API
# ============================================================

def build_alias_candidates(
    *,
    place_candidate_id: str,
    raw_aliases: Optional[Iterable[str]] = None,
    evidence_rows: Optional[Iterable[Dict[str, Any]]] = None,
    include_embedding: bool = True,
) -> List[Dict[str, Any]]:
    """
    Build alias candidates aligned with geo_work.alias_candidates.

    OUTPUT DICT KEYS (DB-aligned):
      - alias_candidate_id (UUID string)
      - place_candidate_id (UUID string)
      - alias (normalized text)
      - alias_kind (official/short/alt/abbr/historic/typo_common)
      - lang (nullable)
      - score (nullable float in [0,1])

    EXTRA (non-DB fields for later pipeline):
      - vector (optional)
      - features (cheap lexical features)

    You can call it in two ways:

    1) Just strings:
        build_alias_candidates(place_candidate_id=..., raw_aliases=[...])

    2) Evidence rows from geo_raw.name_evidence:
        build_alias_candidates(place_candidate_id=..., evidence_rows=[{"raw_text","source","lang","weight_hint"}])
    """

    if not place_candidate_id:
        raise ValueError("place_candidate_id is required")

    # -----------------------------
    # Collect raw items
    # -----------------------------
    items: List[Dict[str, Any]] = []

    if evidence_rows is not None:
        for r in evidence_rows:
            txt = str(r.get("raw_text") or "")
            if _should_skip_alias(txt):
                continue

            items.append({
                "raw_text": txt,
                "source": r.get("source"),
                "lang": r.get("lang"),
                "weight_hint": r.get("weight_hint"),
            })

    elif raw_aliases is not None:
        for a in raw_aliases:
            txt = str(a or "")
            if _should_skip_alias(txt):
                continue

            items.append({
                "raw_text": txt,
                "source": None,
                "lang": None,
                "weight_hint": None,
            })

    else:
        return []

    if not items:
        return []

    # -----------------------------
    # Normalize + dedup (keep best)
    # -----------------------------
    best_by_alias: Dict[str, Dict[str, Any]] = {}

    for it in items:
        raw = it["raw_text"]
        alias_norm = normalize_text(raw)
        if not alias_norm:
            continue

        kind = alias_kind_from_source(it.get("source"))
        if kind not in ALIAS_KINDS:
            kind = "alt"

        score = normalize_alias_score(it.get("weight_hint"))

        candidate = {
            "alias_candidate_id": _alias_uuid(place_candidate_id, alias_norm),
            "place_candidate_id": place_candidate_id,
            "alias": alias_norm,
            "alias_kind": kind,
            "lang": it.get("lang"),
            "score": score,
        }

        prev = best_by_alias.get(alias_norm)
        if prev is None:
            best_by_alias[alias_norm] = candidate
            continue

        # tie-break duplicates:
        # 1) higher score wins (if both exist)
        # 2) higher kind priority wins
        prev_score = prev.get("score")
        new_score = candidate.get("score")

        if prev_score is not None and new_score is not None:
            if float(new_score) > float(prev_score):
                best_by_alias[alias_norm] = candidate
            continue

        # if score missing, prioritize by kind strength
        if _KIND_PRIORITY[kind] > _KIND_PRIORITY.get(prev.get("alias_kind", "alt"), 0):
            best_by_alias[alias_norm] = candidate

    if not best_by_alias:
        return []

    # -----------------------------
    # Add embeddings + features (optional)
    # -----------------------------
    out: List[Dict[str, Any]] = []

    for alias_norm, row in best_by_alias.items():
        features = {
            "length": len(alias_norm),
            "token_count": len(alias_norm.split()),
            "has_digit": any(c.isdigit() for c in alias_norm),
        }

        if include_embedding:
            row["vector"] = embed(alias_norm)  # ⚠️ NOT DB — for OpenSearch / geo_prod embeddings
        row["features"] = features            # ⚠️ NOT DB — for reranking / debug

        out.append(row)

    return out
