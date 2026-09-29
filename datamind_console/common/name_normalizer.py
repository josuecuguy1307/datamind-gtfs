"""Permanent text normalizer for all stop and route names.

Applies Spanish-aware capitalization, deduplication, typo correction,
abbreviation standardization, and ID stripping. Idempotent — safe to
run multiple times on the same input.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional


# Spanish particles that are ALWAYS lowercased mid-name (true prepositions/conjunctions).
_LOWERCASE_ALWAYS = frozenset({
    "de", "del", "y", "en", "por",
    "con", "al", "e", "a", "o", "u", "and", "vía", "via",
})

# Articles only lowercased when the previous word is "de"/"del".
# In proper-noun contexts (e.g., "Terminal La Ofelia", "Quitumbe – La Merced",
# "Centro Comercial El Bosque", "Av. Los Cóndores") these articles are part of
# the place name and stay capitalized.
_LOWERCASE_AFTER_DE_DEL = frozenset({"la", "el", "los", "las"})

# Only single-word accent/spelling fixes. Multi-word entries handled separately.
_ACCENT_FIXES = {
    "ecovia": "Ecovía",
    "trolebus": "Trolebús",
    "carcelen": "Carcelén",
    "guamani": "Guamaní",
    "cumbaya": "Cumbayá",
    "sangolqui": "Sangolquí",
    "ruminahui": "Rumiñahui",
    "chaquiñan": "Chaquiñán",
    "guapulo": "Guápulo",
    "estacion": "Estación",
    "republica": "República",
    "america": "América",
    "colon": "Colón",
    "bolivar": "Bolívar",
    "pueste": "Puente",
}

# Multi-word typo corrections (applied as phrase replacements)
_PHRASE_FIXES = {
    "simon bolivar": "Simón Bolívar",
    "10 agosto": "10 de Agosto",
    "6 diciembre": "6 de Diciembre",
    "12 octubre": "12 de Octubre",
    "naciones unidas": "Naciones Unidas",
    "rio coca": "Río Coca",
    "av amazonas": "Av. Amazonas",
    "av america": "Av. América",
    "av colon": "Av. Colón",
}

_ABBREVIATION_MAP = {
    "avenida": "Av.",
    "av": "Av.",
    "avda": "Av.",
    "clle": "Calle",
    "cl": "Calle",
    "psje": "Pasaje",
    "pje": "Pasaje",
    "pasj": "Pasaje",
    "urb": "Urb.",
    "cdla": "Cdla.",
    "ciudadela": "Cdla.",
    "blvd": "Blvd.",
    "boulevard": "Blvd.",
    "km": "Km",
    "nro": "N.º",
    "nº": "N.º",
    "no.": "N.º",
    "num": "N.º",
    "esq": "Esq.",
    "esquina": "Esq.",
}

_ID_PATTERNS = [
    re.compile(r"\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b", re.IGNORECASE),
    re.compile(r"\brel\s*\d{5,}\b", re.IGNORECASE),
    re.compile(r"\brelation\s*\d+\b", re.IGNORECASE),
    re.compile(r"\bway\s*\d+\b", re.IGNORECASE),
    re.compile(r"\bnode\s*\d+\b", re.IGNORECASE),
    re.compile(r"\b[a-f0-9]{8}\b(?![\w-])"),
]

_MULTI_WS = re.compile(r"\s+")
_MULTI_DASH = re.compile(r"-{2,}")
_LEADING_TRAILING_PUNCT = re.compile(r"^[\s\-–—,;:]+|[\s\-–—,;:]+$")
_AFTER_OPEN_PAREN = re.compile(r"(\()\s*([a-záéíóúñü])")

# Pure operator/route codes (single token, all-caps + digits + hyphens).
# These are placeholders Phase 4 Pipeline A will replace; do not rewrite.
# Matches: EPMTPQ-SOL-RET, TRANSPLANETA-B03, DISUTRAN-B7, 7MAYO-B1, B412DF.
# Does NOT match: "IN70 Terminal Sur" (has space), "Tambillo - Sangolqui" (mixed case).
_CODE_ONLY = re.compile(r"^[A-Z0-9][A-Z0-9-]*$")
_DIGIT = re.compile(r"\d")


def _looks_like_code_only(name: str) -> bool:
    """True if *name* is a pure code placeholder, not a human-readable label.

    Skipped entirely by NameNormalizer: such inputs belong to Pipeline A,
    which generates a real candidate name from OSM/route_raw evidence.
    Also skips names with > 3 numeric chars (per project constraint:
    clean names should carry at most 3 digits of route/corridor coding).
    """
    stripped = name.strip()
    if not stripped:
        return False
    if " " not in stripped and _CODE_ONLY.fullmatch(stripped):
        return True
    if len(_DIGIT.findall(stripped)) > 3:
        return True
    return False


class NameNormalizer:
    """Idempotent Spanish-aware name normalizer for transit entities."""

    def normalize(self, name: Optional[str]) -> Optional[str]:
        if not name or not name.strip():
            return name
        if _looks_like_code_only(name):
            return name
        result = name
        result = self._trim_and_clean(result)
        result = self._strip_ids_and_codes(result)
        result = self._fix_phrase_typos(result)
        result = self._fix_accent_typos(result)
        result = self._standardize_abbreviations(result)
        result = self._fix_all_caps(result)
        result = self._fix_capitalization(result)
        result = self._fix_duplicate_words(result)
        result = self._fix_generic_numbered(result)
        result = self._capitalize_after_parens(result)
        result = self._trim_and_clean(result)
        if not result or not result.strip():
            return name
        return result

    def _fix_capitalization(self, text: str) -> str:
        words = text.split()
        if not words:
            return text
        result = []
        for i, word in enumerate(words):
            # Strip leading punctuation for analysis
            prefix = ""
            bare = word
            while bare and bare[0:1] in "([\"'¡¿":
                prefix += bare[0]
                bare = bare[1:]
            bare_lower = bare.lower()

            prev_lower = ""
            if i > 0:
                prev_lower = words[i - 1].lower().rstrip(".,;:")

            if bare_lower in _LOWERCASE_ALWAYS and i > 0 and not prefix:
                result.append(word.lower())
            elif (
                bare_lower in _LOWERCASE_AFTER_DE_DEL
                and i > 0
                and not prefix
                and prev_lower in {"de", "del"}
            ):
                # Lowercase only when preceded by "de"/"del" ("Plaza de la Independencia").
                # In other contexts the article is part of a proper place name
                # ("Terminal La Ofelia", "Centro Comercial El Bosque") — keep capitalized.
                result.append(word.lower())
            elif bare.isupper() and len(bare) <= 8:
                # Preserve short uppercase tokens including those with digits:
                # corridor codes (IN70, QT55, B03, R17), trolley/Ecovía suffixes
                # (E4, C6, A5), agency codes (EPMTPQ).
                result.append(word)
            elif "." in bare:
                result.append(word)
            elif bare[0:1].isupper() and not bare.isupper():
                result.append(word)
            else:
                result.append(prefix + bare.capitalize())
        if result and result[0][0:1].islower():
            result[0] = result[0][0].upper() + result[0][1:]
        return " ".join(result)

    def _fix_all_caps(self, text: str) -> str:
        words = text.split()
        upper_count = sum(1 for w in words if w.isupper() and len(w) > 1)
        if len(words) > 1 and upper_count > len(words) * 0.6:
            return text.title()
        return text

    def _fix_duplicate_words(self, text: str) -> str:
        words = text.split()
        if len(words) <= 2:
            return text
        seen = []
        for w in words:
            if not seen or w.lower() != seen[-1].lower():
                seen.append(w)
        return " ".join(seen)

    def _fix_accent_typos(self, text: str) -> str:
        words = text.split()
        result = []
        for w in words:
            prefix = ""
            suffix = ""
            bare = w
            while bare and bare[0] in "([\"'":
                prefix += bare[0]
                bare = bare[1:]
            while bare and bare[-1:] in ")]\"'":
                suffix = bare[-1] + suffix
                bare = bare[:-1]
            key = bare.lower()
            if key in _ACCENT_FIXES:
                result.append(prefix + _ACCENT_FIXES[key] + suffix)
            else:
                result.append(w)
        return " ".join(result)

    def _fix_phrase_typos(self, text: str) -> str:
        for typo, fix in _PHRASE_FIXES.items():
            pattern = re.compile(r"\b" + re.escape(typo) + r"\b", re.IGNORECASE)
            if pattern.search(text):
                text = pattern.sub(fix, text)
        return text

    def _fix_generic_numbered(self, text: str) -> str:
        m = re.match(r"^(Ruta|Route|Línea|Linea)\s+(\d+)$", text, re.IGNORECASE)
        if m:
            prefix = m.group(1).capitalize()
            if prefix.lower() in ("linea",):
                prefix = "Línea"
            return f"{prefix} {m.group(2)}"
        return text

    def _strip_ids_and_codes(self, text: str) -> str:
        result = text
        for pat in _ID_PATTERNS:
            result = pat.sub("", result)
        return result.strip()

    def _standardize_abbreviations(self, text: str) -> str:
        words = text.split()
        result = []
        for w in words:
            key = w.lower().rstrip(".")
            if key in _ABBREVIATION_MAP:
                result.append(_ABBREVIATION_MAP[key])
            else:
                result.append(w)
        return " ".join(result)

    def _capitalize_after_parens(self, text: str) -> str:
        return _AFTER_OPEN_PAREN.sub(lambda m: m.group(1) + m.group(2).upper(), text)

    def _trim_and_clean(self, text: str) -> str:
        text = _MULTI_WS.sub(" ", text)
        text = _MULTI_DASH.sub("-", text)
        text = _LEADING_TRAILING_PUNCT.sub("", text)
        text = text.strip()
        return text


# Module-level singleton for convenience
normalizer = NameNormalizer()


def normalize_name(name: Optional[str]) -> Optional[str]:
    """Convenience function — calls the module-level singleton."""
    return normalizer.normalize(name)
