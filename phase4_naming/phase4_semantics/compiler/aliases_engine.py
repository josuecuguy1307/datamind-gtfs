# phase4_semantics/compiler/aliases_engine.py

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from phase4_semantics.common.text import (
    normalize_text,
    normalize_operator,
    normalize_route_ref,
    canonicalize_route_name,
)

# -----------------------------
# Config / heuristics
# -----------------------------

SOURCE_TRUST: Dict[str, float] = {
    "manual": 1.00,
    "pdf_extract": 0.95,
    "gis_layer": 0.90,
    "gtfs": 0.90,
    "osm_overpass_seed": 0.80,
    "osm": 0.70,
    "unknown": 0.60,
}

MAX_ALIASES_DEFAULT = 12

# Similarity threshold: if too close to an existing alias, skip.
REDUNDANCY_JACCARD_TH = 0.85

# Disallow junk aliases
_GENERIC_BAD = {
    "ruta",
    "linea",
    "línea",
    "bus",
    "buses",
    "autobus",
    "autobús",
    "transporte",
}


@dataclass(frozen=True)
class AliasCandidate:
    text: str
    key: str
    score: float
    reason: Dict[str, Any]


def _tokenize(norm: str) -> Set[str]:
    # keep alnum tokens, drop tiny tokens
    toks = [t for t in re.split(r"\s+", norm.strip()) if len(t) >= 2]
    return set(toks)


def _jaccard(a: str, b: str) -> float:
    A = _tokenize(a)
    B = _tokenize(b)
    if not A and not B:
        return 1.0
    if not A or not B:
        return 0.0
    return len(A & B) / float(len(A | B))


def _looks_like_ref(s: str) -> bool:
    # Examples: "E3", "E-3", "10", "T11", "R5"
    t = s.strip().replace(" ", "")
    return bool(re.fullmatch(r"[A-Z]?\d+[A-Z]?", t)) or bool(re.fullmatch(r"[A-Z]{1,3}\-?\d{1,4}", t))


def _is_too_generic(norm: str) -> bool:
    if not norm:
        return True
    toks = _tokenize(norm)
    if not toks:
        return True
    # if it's only generic words
    if toks.issubset(_GENERIC_BAD):
        return True
    # too short
    if len(norm) < 2:
        return True
    return False


def _fmt_alias(s: str) -> str:
    """
    Keep refs uppercase, otherwise return a clean, readable alias.
    We don't want to over-titlecase acronyms incorrectly, so we keep tokens <=4 uppercase.
    """
    s = (s or "").strip()
    if not s:
        return ""

    if _looks_like_ref(s):
        return s.strip().replace(" ", "").upper()

    # light formatting: collapse spaces, keep hyphens readable
    s = re.sub(r"\s+", " ", s)
    s = s.replace("–", "-")
    s = re.sub(r"\s*-\s*", " - ", s)
    s = re.sub(r"\s+", " ", s).strip()

    parts = []
    for tok in s.split(" "):
        if tok.isupper() and len(tok) <= 5:
            parts.append(tok)
        else:
            # preserve digits
            if any(ch.isdigit() for ch in tok):
                parts.append(tok.upper() if _looks_like_ref(tok) else tok.capitalize())
            else:
                parts.append(tok.capitalize())
    return " ".join(parts).strip()


def _source_weight(source_type: Optional[str], confidence_hint: Optional[float]) -> float:
    st = (source_type or "unknown").strip()
    trust = SOURCE_TRUST.get(st, 0.60)
    ch = float(confidence_hint) if confidence_hint is not None else 0.50
    # keep confidence bounded
    ch = max(0.0, min(1.0, ch))
    return 0.6 * trust + 0.4 * ch


def _mk_candidate(
    alias_text: str,
    base_name_norm: str,
    base_ref_norm: str,
    source_type: Optional[str],
    confidence_hint: Optional[float],
    reason_extra: Optional[Dict[str, Any]] = None,
) -> Optional[AliasCandidate]:
    alias_text = (alias_text or "").strip()
    if not alias_text:
        return None

    alias_fmt = _fmt_alias(alias_text)
    key = normalize_text(alias_fmt)

    if _is_too_generic(key):
        return None

    # Do not allow aliases that are literally "ruta" etc
    if key in _GENERIC_BAD:
        return None

    # Score
    w = _source_weight(source_type, confidence_hint)

    score_parts: Dict[str, float] = {
        "source_weight": w,
    }

    # bonus if contains base ref
    if base_ref_norm and base_ref_norm in normalize_text(alias_fmt).upper().replace(" ", ""):
        score_parts["has_ref_bonus"] = 0.20
    else:
        score_parts["has_ref_bonus"] = 0.0

    # bonus if overlaps base name tokens
    if base_name_norm:
        sim = _jaccard(normalize_text(alias_fmt), base_name_norm)
        score_parts["name_overlap"] = sim * 0.30
    else:
        score_parts["name_overlap"] = 0.0

    # length preference
    L = len(alias_fmt)
    if 4 <= L <= 60:
        score_parts["length_bonus"] = 0.10
    elif L > 80:
        score_parts["length_penalty"] = -0.25
    else:
        score_parts["length_bonus"] = 0.0
        score_parts["length_penalty"] = 0.0

    score = (
        score_parts["source_weight"]
        + score_parts["has_ref_bonus"]
        + score_parts["name_overlap"]
        + score_parts.get("length_bonus", 0.0)
        + score_parts.get("length_penalty", 0.0)
    )

    reason: Dict[str, Any] = {
        "score_parts": score_parts,
        "source_type": source_type or "unknown",
        "confidence_hint": float(confidence_hint) if confidence_hint is not None else None,
    }
    if reason_extra:
        reason.update(reason_extra)

    return AliasCandidate(text=alias_fmt, key=key, score=float(score), reason=reason)


def build_route_aliases(
    chosen_route_name: str,
    chosen_route_ref: Optional[str] = None,
    chosen_operator_name: Optional[str] = None,
    chosen_from: Optional[str] = None,
    chosen_to: Optional[str] = None,
    evidence: Optional[Sequence[Union[Dict[str, Any], Any]]] = None,
    existing_aliases: Optional[Sequence[str]] = None,
    max_aliases: int = MAX_ALIASES_DEFAULT,
) -> List[str]:
    """
    Main entry point.

    evidence: list of dicts or EvidenceRecord-like objects with fields:
      - source_type, confidence_hint, route_name, route_ref, operator_name, from_name, to_name, via, raw

    existing_aliases: existing route_prod.routes.route_aliases to keep (optional).
    """

    base_name = canonicalize_route_name(chosen_route_name)
    base_name_norm = normalize_text(base_name)

    base_ref_norm = ""
    if chosen_route_ref:
        base_ref_norm = normalize_route_ref(chosen_route_ref)

    op_norm = normalize_operator(chosen_operator_name) if chosen_operator_name else ""

    candidates: List[AliasCandidate] = []
    seen_keys: Set[str] = set()

    def add(alias: str, source_type: Optional[str], confidence_hint: Optional[float], why: str):
        nonlocal candidates, seen_keys
        c = _mk_candidate(
            alias_text=alias,
            base_name_norm=base_name_norm,
            base_ref_norm=base_ref_norm,
            source_type=source_type,
            confidence_hint=confidence_hint,
            reason_extra={"why": why},
        )
        if not c:
            return
        if c.key in seen_keys:
            return
        seen_keys.add(c.key)
        candidates.append(c)

    # 1) Start from existing aliases (keep them, but they are weakly trusted)
    if existing_aliases:
        for a in existing_aliases:
            add(a, source_type="existing", confidence_hint=0.55, why="existing_alias")

    # 2) Add base ref/name combos
    if base_ref_norm:
        add(base_ref_norm, source_type="compiler", confidence_hint=0.90, why="base_ref")
    if base_name:
        # we generally don't store route_name as alias, but allow variant combos
        # (we will later filter exact equality with route_name)
        add(base_name, source_type="compiler", confidence_hint=0.80, why="base_name")

    if base_ref_norm and base_name:
        add(f"{base_ref_norm} {base_name}", source_type="compiler", confidence_hint=0.90, why="ref_plus_name")
        add(f"{base_name} {base_ref_norm}", source_type="compiler", confidence_hint=0.80, why="name_plus_ref")

    # 3) Operator + ref
    if op_norm and base_ref_norm:
        add(f"{op_norm} {base_ref_norm}", source_type="compiler", confidence_hint=0.80, why="operator_plus_ref")

    # 4) From/To variants (if available)
    fr = canonicalize_route_name(chosen_from) if chosen_from else ""
    to = canonicalize_route_name(chosen_to) if chosen_to else ""
    if fr and to and fr != to:
        add(f"{fr} - {to}", source_type="compiler", confidence_hint=0.70, why="from_to")
        if base_ref_norm:
            add(f"{base_ref_norm} {fr} - {to}", source_type="compiler", confidence_hint=0.75, why="ref_from_to")

    # 5) Evidence-derived aliases
    if evidence:
        for e in evidence:
            # accept dicts or objects
            get = (lambda k, default=None: e.get(k, default)) if isinstance(e, dict) else (lambda k, default=None: getattr(e, k, default))

            st = get("source_type", "unknown")
            ch = get("confidence_hint", 0.50)

            e_name = get("route_name")
            e_ref = get("route_ref")
            e_op = get("operator_name")
            e_from = get("from_name")
            e_to = get("to_name")

            if e_name:
                add(canonicalize_route_name(e_name), st, ch, "evidence_route_name")

            if e_ref:
                refn = normalize_route_ref(e_ref)
                add(refn, st, ch, "evidence_ref")
                if e_name:
                    add(f"{refn} {canonicalize_route_name(e_name)}", st, ch, "evidence_ref_plus_name")

            if e_op and e_ref:
                add(f"{normalize_operator(e_op)} {normalize_route_ref(e_ref)}", st, ch, "evidence_operator_plus_ref")

            ef = canonicalize_route_name(e_from) if e_from else ""
            et = canonicalize_route_name(e_to) if e_to else ""
            if ef and et and ef != et:
                add(f"{ef} - {et}", st, ch, "evidence_from_to")
                if e_ref:
                    add(f"{normalize_route_ref(e_ref)} {ef} - {et}", st, ch, "evidence_ref_from_to")

            # If evidence raw has OSM tags with alt names, include them
            raw = get("raw", {}) or {}
            tags = raw.get("tags") if isinstance(raw, dict) else None
            if isinstance(tags, dict):
                for k in ("name", "alt_name", "official_name", "short_name", "name:es", "name:en", "ref"):
                    v = tags.get(k)
                    if v and isinstance(v, str):
                        if k == "ref":
                            add(normalize_route_ref(v), st, ch, "osm_tag_ref")
                        else:
                            add(canonicalize_route_name(v), st, ch, f"osm_tag_{k}")

    # 6) Remove aliases that are identical to chosen route name (normalized)
    filtered: List[AliasCandidate] = []
    for c in candidates:
        if base_name_norm and c.key == base_name_norm:
            # we keep the actual route_name in its own column; avoid duplication
            continue
        filtered.append(c)

    # 7) Sort by score desc
    filtered.sort(key=lambda x: x.score, reverse=True)

    # 8) Greedy selection with redundancy penalty
    selected: List[AliasCandidate] = []
    for c in filtered:
        if len(selected) >= max_aliases:
            break

        redundant = False
        for s in selected:
            if _jaccard(c.key, s.key) >= REDUNDANCY_JACCARD_TH:
                redundant = True
                break
        if redundant:
            continue

        selected.append(c)

    # 9) Return in stable order: keep score priority but deterministic formatting
    out = [c.text for c in selected]

    # Final guard: unique + no empties
    seen = set()
    clean: List[str] = []
    for a in out:
        aa = a.strip()
        if not aa:
            continue
        k = normalize_text(aa)
        if k in seen:
            continue
        seen.add(k)
        clean.append(aa)

    return clean
