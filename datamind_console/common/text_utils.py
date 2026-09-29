from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Optional, Sequence


_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_ws(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").strip())


def strip_accents(s: str) -> str:
    s = s or ""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(ch)
    )


def slugify(s: str, *, max_len: int = 64) -> str:
    """
    'Ruta Ñuñoa – Centro' -> 'ruta-nunoa-centro'
    """
    s2 = strip_accents(normalize_ws(s)).lower()
    s2 = _NON_ALNUM_RE.sub("-", s2).strip("-")
    if max_len and len(s2) > max_len:
        s2 = s2[:max_len].rstrip("-")
    return s2


def truncate(s: str, n: int = 160, suffix: str = "…") -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[: max(0, n - len(suffix))].rstrip() + suffix


def dedupe_preserve_order(items: Sequence[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        x2 = (x or "").strip()
        if not x2 or x2 in seen:
            continue
        seen.add(x2)
        out.append(x2)
    return out


def safe_join(parts: Iterable[Optional[str]], sep: str = " · ") -> str:
    xs = [(p or "").strip() for p in parts]
    xs = [x for x in xs if x]
    return sep.join(xs)
