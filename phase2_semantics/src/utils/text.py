from __future__ import annotations

import re
import unicodedata


# -----------------------------
# Core normalization
# -----------------------------

def normalize_text(text: str) -> str:
    """
    Normalize text for search / embeddings.

    Rules:
    - lowercase
    - trim whitespace
    - collapse repeated spaces
    - keep accents (important for Spanish)
    """

    if not text:
        return ""

    text = text.strip().lower()
    text = _collapse_spaces(text)

    return text


# -----------------------------
# Helpers
# -----------------------------

def _collapse_spaces(text: str) -> str:
    """Replace multiple spaces with a single space."""
    return re.sub(r"\s+", " ", text)


def remove_accents(text: str) -> str:
    """
    Remove accents from text (optional utility).
    Example: estación → estacion
    """

    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )
