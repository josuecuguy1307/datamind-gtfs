from __future__ import annotations

from dataclasses import dataclass
from typing import List, Set
import unicodedata
import re


# -----------------------------
# Alias model
# -----------------------------

@dataclass(frozen=True)
class Alias:
    value: str          # normalized alias
    source: str         # original name
    rule: str           # how it was produced


# -----------------------------
# Normalization helpers
# -----------------------------

def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = _strip_accents(text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _remove_stopwords(text: str) -> str:
    STOPWORDS = {
        "estacion", "station", "terminal", "parada",
        "stop", "bus", "metro", "de", "la", "el", "del",
    }

    tokens = [t for t in text.split() if t not in STOPWORDS]
    return " ".join(tokens)


# -----------------------------
# Alias builders
# -----------------------------

def build_aliases(name: str | None) -> List[Alias]:
    """
    Build alias variants from a raw place/stop name.

    Pure function:
    - no DB
    - no ML
    - deterministic
    """

    if not name:
        return []

    aliases: List[Alias] = []
    seen: Set[str] = set()

    base = _normalize(name)

    def _add(value: str, rule: str):
        if value and value not in seen:
            seen.add(value)
            aliases.append(Alias(value=value, source=name, rule=rule))

    # 1️⃣ Full normalized name
    _add(base, "normalized_full")

    # 2️⃣ Without stopwords
    no_stop = _remove_stopwords(base)
    _add(no_stop, "removed_stopwords")

    # 3️⃣ Token-level aliases
    for token in base.split():
        if len(token) >= 4:
            _add(token, "single_token")

    # 4️⃣ Prefixes (search-friendly)
    tokens = base.split()
    if len(tokens) >= 2:
        _add(tokens[0], "first_token")
        _add(tokens[-1], "last_token")

    return aliases
