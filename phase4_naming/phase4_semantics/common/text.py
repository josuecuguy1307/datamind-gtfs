from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Optional

_WS_RE = re.compile(r"\s+")


# ------------------------------------------------------------
# Core helpers
# ------------------------------------------------------------

def normalize_ws(s: Optional[str]) -> Optional[str]:
    """Trim + collapse whitespace."""
    if s is None:
        return None
    s2 = _WS_RE.sub(" ", str(s)).strip()
    return s2 if s2 else None


def ascii_fold(s: Optional[str]) -> Optional[str]:
    """
    Remove accents/diacritics to make matching more robust.
    Example: 'Cristóbal' -> 'Cristobal'
    """
    if s is None:
        return None
    s = normalize_ws(s)
    if not s:
        return None
    nfkd = unicodedata.normalize("NFKD", s)
    folded = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return folded


def lower_clean(s: Optional[str]) -> Optional[str]:
    """Lowercase + normalize whitespace (no accent folding)."""
    s = normalize_ws(s)
    if not s:
        return None
    return s.lower()


def uniq_keep_order(xs: Iterable[str]) -> List[str]:
    """Unique items preserving order (case-sensitive)."""
    seen = set()
    out: List[str] = []
    for x in xs:
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def join_nonempty(parts: Iterable[Optional[str]], sep: str = " ") -> str:
    """Join non-empty parts after normalize_ws."""
    items = []
    for p in parts:
        v = normalize_ws(p)
        if v:
            items.append(v)
    return sep.join(items)


# ------------------------------------------------------------
# Phase 4 public normalization API (what __init__ imports expect)
# ------------------------------------------------------------

def normalize_text(s: Optional[str]) -> Optional[str]:
    """
    Matching-friendly normalization (aggressive):
      - trim/collapse whitespace
      - accent fold
      - lowercase
    """
    s = normalize_ws(s)
    if not s:
        return None
    s = ascii_fold(s)
    if not s:
        return None
    s = s.lower().strip()
    return s if s else None


def canonicalize_route_name(s: Optional[str]) -> Optional[str]:
    """
    Human-friendly canonicalization for display/storage (gentle):
      - trim/collapse whitespace
      - preserve original casing
    """
    return normalize_ws(s)


def normalize_name(s: Optional[str]) -> Optional[str]:
    """
    Backwards-compatible alias used by phase4_semantics/__init__.py.
    Keep this human-friendly (not aggressive).
    """
    return canonicalize_route_name(s)


def normalize_operator(s: Optional[str]) -> Optional[str]:
    """
    Operator normalization: human-friendly but stable.
    """
    return canonicalize_route_name(s)


def normalize_route_ref(s: Optional[str]) -> Optional[str]:
    """
    Normalize route refs like 'E1', ' e-1 ', 'E 1' -> 'E1'
    (Uppercase, remove separators.)
    """
    s = normalize_ws(s)
    if not s:
        return None
    s = ascii_fold(s) or ""
    s = re.sub(r"[^A-Za-z0-9]+", "", s)
    s = s.upper().strip()
    return s if s else None


def build_aliases(parts: Iterable[Optional[str]]) -> List[str]:
    """
    Build a unique alias list preserving order (case-insensitive uniqueness).
    """
    raw: List[str] = []
    for p in parts:
        v = canonicalize_route_name(p)
        if v:
            raw.append(v)

    seen = set()
    out: List[str] = []
    for x in raw:
        k = x.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    return out
