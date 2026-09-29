from __future__ import annotations

import unicodedata
import re


def normalize_alias(text: str | None) -> str | None:
    """
    Normalize an alias or name into a canonical searchable form.

    This function is the SINGLE source of truth for alias normalization.

    Rules:
    - lowercase
    - remove accents
    - remove punctuation
    - collapse whitespace
    """

    if not text:
        return None

    # 1️⃣ lowercase
    text = text.lower().strip()

    # 2️⃣ remove accents
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )

    # 3️⃣ remove punctuation (keep letters & numbers)
    text = re.sub(r"[^\w\s]", " ", text)

    # 4️⃣ collapse whitespace
    text = re.sub(r"\s+", " ", text)

    return text.strip()
