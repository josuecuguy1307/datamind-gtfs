from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

try:
    # If you already have normalize_text in your project, use it.
    from src.utils.text import normalize_text  # type: ignore
except Exception:
    # Fallback: basic normalization (lower + remove accents + cleanup)
    import re
    import unicodedata

    def normalize_text(s: str) -> str:
        s = (s or "").strip().lower()
        s = unicodedata.normalize("NFKD", s)
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        s = re.sub(r"[^a-z0-9\s\-_/.:]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s


@dataclass(frozen=True)
class OperatorEntry:
    key: str                # canonical key used inside the pipeline
    display: str            # pretty name
    aliases: Tuple[str, ...]  # all known variants/strings


# =========================================================
# EDIT HERE: canonical operators + aliases/variants
# =========================================================
OPERATOR_CATALOG: Tuple[OperatorEntry, ...] = (
    OperatorEntry(
        key="ecovia",
        display="Ecovía",
        aliases=(
            "Ecovia",
            "Ecovía",
            "ECO VIA",
            "ECO-VIA",
            "Corredor Ecovia",
            "Corredor Ecovía",
            "Sistema Ecovia",
            "Sistema Ecovía",
            "Ecovía Quito",
            "Ecovia Quito",
        ),
    ),
    OperatorEntry(
        key="trolebus",
        display="Trolebús",
        aliases=(
            "Trolebus",
            "Trolebús",
            "TROLE",
            "Corredor Trole",
            "Sistema Trole",
            "Trole Quito",
        ),
    ),
    OperatorEntry(
        key="metrobusrq",
        display="Metrobús-Q",
        aliases=(
            "Metrobus",
            "Metrobús",
            "Metrobús-Q",
            "Metrobus-Q",
            "Corredor Central Norte",
            "CCN",
        ),
    ),
    OperatorEntry(
        key="libertadores_del_valle",
        display="Libertadores del Valle",
        aliases=(
            "Libertadores del Valle",
            "Libertadores deL Valle",
            "Libertadores del valle",
            "Libertadores",
            "L. del Valle",
            "Libertadores Valle",
            "Coop Libertadores del Valle",
            "Cooperativa Libertadores del Valle",
        ),
    ),
    OperatorEntry(
        key="calsig_express",
        display="CALSIG Express",
        aliases=(
            "CALSIG Express",
            "CALSIG",
            "Calsig",
            "Calsig Express",
            "Cooperativa CALSIG",
            "Coop CALSIG Express",
        ),
    ),
    OperatorEntry(
        key="condorvall",
        display="Condorvall",
        aliases=(
            "Condorvall",
            "Condor Vall",
            "Cóndorvall",
            "Cooperativa Condorvall",
            "Coop Condorvall",
        ),
    ),
    OperatorEntry(
        key="san_pedro_amaguana",
        display="San Pedro de Amaguaña",
        aliases=(
            "San Pedro de Amaguaña",
            "San Pedro Amaguaña",
            "Amaguaña",
            "Cooperativa Amaguaña",
            "Coop San Pedro de Amaguaña",
            "Cooperativa San Pedro de Amaguaña",
        ),
    ),
    OperatorEntry(
        key="general_pintag",
        display="General Pintag",
        aliases=(
            "General Pintag",
            "Pintag",
            "Cooperativa Pintag",
            "Coop General Pintag",
            "Cooperativa General Pintag",
        ),
    ),
    OperatorEntry(
        key="expreso_antisana",
        display="Expreso Antisana",
        aliases=(
            "Expreso Antisana",
            "Antisana",
            "ExpresoAntisana",
            "EXPRESANTISANA",
            "Cooperativa Expreso Antisana",
        ),
    ),
    OperatorEntry(
        key="los_chillos",
        display="Los Chillos",
        aliases=(
            "Los Chillos",
            "Cooperativa Los Chillos",
            "Coop Los Chillos",
        ),
    ),
    OperatorEntry(
        key="marco_polo",
        display="Marco Polo",
        aliases=(
            "Marco Polo",
            "Cooperativa Marco Polo",
            "Coop Marco Polo",
        ),
    ),
    OperatorEntry(
        key="turismo_chillos",
        display="Turismo Chillos",
        aliases=(
            "Turismo Chillos",
            "Turismo",
            "Ejecutivo Turismo Chillos",
            "Cooperativa Turismo Chillos",
        ),
    ),
    OperatorEntry(
        key="vingala",
        display="Vingala",
        aliases=(
            "Vingala",
            "Cooperativa Vingala",
            "Coop Vingala",
        ),
    ),
    OperatorEntry(
        key="capelo",
        display="Capelo",
        aliases=(
            "Capelo",
            "Cooperativa Capelo",
            "Coop Capelo",
        ),
    ),
)

# Optional generic noise words you want to ignore in matching
_STOPWORDS = {
    "coop", "cooperativa", "sa", "s.a", "s.a.", "cia", "c.a", "ltda", "transporte",
    "company", "compania", "compañia", "operador", "operator"
}


def _tokenize(s: str) -> List[str]:
    s = normalize_text(s)
    tokens = [t for t in s.replace("-", " ").split(" ") if t]
    return [t for t in tokens if t not in _STOPWORDS]


def _build_alias_index() -> Dict[str, str]:
    """
    Map normalized alias string -> operator key
    """
    idx: Dict[str, str] = {}
    for ent in OPERATOR_CATALOG:
        # include display and key as aliases too
        all_aliases = list(ent.aliases) + [ent.display, ent.key]
        for a in all_aliases:
            na = normalize_text(a)
            if na:
                idx[na] = ent.key
    return idx


_ALIAS_INDEX = _build_alias_index()


def canonicalize_operator_name(name: str) -> Optional[str]:
    """
    Given any input string (alias, display, messy string),
    return canonical operator key or None if unknown.
    """
    n = normalize_text(name)
    if not n:
        return None

    # exact alias match first
    if n in _ALIAS_INDEX:
        return _ALIAS_INDEX[n]

    # fallback: token overlap heuristic
    input_tokens = set(_tokenize(n))
    if not input_tokens:
        return None

    best_key = None
    best_score = 0

    for ent in OPERATOR_CATALOG:
        ent_tokens = set()
        for a in (list(ent.aliases) + [ent.display, ent.key]):
            ent_tokens |= set(_tokenize(a))

        # score = number of shared tokens
        score = len(input_tokens & ent_tokens)
        if score > best_score:
            best_score = score
            best_key = ent.key

    return best_key if best_score >= 2 else None  # require >=2 shared tokens


def expand_operator_keys_or_aliases(items: List[str]) -> List[str]:
    """
    Input: ["Ecovia", "Libertadores del Valle", ...]
    Output: a deduped list of canonical keys: ["ecovia", "libertadores_del_valle", ...]
    """
    out: List[str] = []
    seen: Set[str] = set()

    for x in items or []:
        k = canonicalize_operator_name(x) or normalize_text(x)
        if not k:
            continue
        if k not in seen:
            seen.add(k)
            out.append(k)

    return out


def operator_match_score(tags: Dict[str, str], desired_keys: List[str]) -> float:
    """
    Score how well this relation matches desired operators.
    Uses operator/brand/network fields, plus token overlap.
    """
    if not desired_keys:
        return 0.0

    op = (tags.get("operator") or "") + " " + (tags.get("operator:short") or "")
    brand = (tags.get("brand") or "")
    net = (tags.get("network") or "")
    hay = normalize_text(op + " " + brand + " " + net)

    hay_tokens = set(_tokenize(hay))
    if not hay_tokens:
        return 0.0

    best = 0.0
    for key in desired_keys:
        ent = next((e for e in OPERATOR_CATALOG if e.key == key), None)
        if not ent:
            # if unknown key, just try raw token match
            key_tokens = set(_tokenize(key))
        else:
            key_tokens = set()
            for a in (list(ent.aliases) + [ent.display, ent.key]):
                key_tokens |= set(_tokenize(a))

        # overlap ratio-ish
        overlap = len(hay_tokens & key_tokens)
        if overlap >= 2:
            best = max(best, 50.0 + 20.0 * overlap)  # strong boost
        elif overlap == 1:
            best = max(best, 15.0)  # weak boost

    return best
