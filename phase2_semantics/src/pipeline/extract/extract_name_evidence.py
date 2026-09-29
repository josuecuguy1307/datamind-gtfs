from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal
import re


# ============================================================
# Evidence model
# ============================================================

EvidenceKind = Literal["stop", "poi"]


@dataclass(frozen=True)
class Evidence:
    """
    Semantic evidence extracted from an object's *name*.

    - kind   : semantic category
    - weight : confidence contribution (0–1)
    - source : rule identifier (debug / explainability)
    """
    kind: EvidenceKind
    weight: float
    source: str


# ============================================================
# Normalization helpers
# ============================================================

def _normalize(name: str) -> str:
    return name.lower().strip()


def _contains_any(text: str, keywords: List[str]) -> bool:
    return any(k in text for k in keywords)


# ============================================================
# STOP name rules
# ============================================================

STOP_KEYWORDS = [
    "terminal",
    "estación",
    "estacion",
    "parada",
    "paradero",
    "stop",
    "station",
    "bus",
]

def stop_name_evidence(name: str) -> List[Evidence]:
    evidence: List[Evidence] = []
    text = _normalize(name)

    # Soft lexical match
    if _contains_any(text, STOP_KEYWORDS):
        evidence.append(
            Evidence(
                kind="stop",
                weight=0.8,
                source="name_contains_stop_keyword",
            )
        )

    # Strong explicit patterns
    if re.search(r"\bestación\b|\bterminal\b", text):
        evidence.append(
            Evidence(
                kind="stop",
                weight=1.0,
                source="name_explicit_station_terminal",
            )
        )

    return evidence


# ============================================================
# POI name rules
# ============================================================

POI_KEYWORDS = [
    "parque",
    "hospital",
    "clínica",
    "clinica",
    "universidad",
    "colegio",
    "escuela",
    "mall",
    "centro comercial",
    "plaza",
    "mercado",
    "museum",
    "museo",
]

def poi_name_evidence(name: str) -> List[Evidence]:
    evidence: List[Evidence] = []
    text = _normalize(name)

    if _contains_any(text, POI_KEYWORDS):
        evidence.append(
            Evidence(
                kind="poi",
                weight=0.7,
                source="name_contains_poi_keyword",
            )
        )

    return evidence


# ============================================================
# Unified entrypoint (PURE)
# ============================================================

def extract_name_evidence(name: str | None) -> List[Evidence]:
    """
    Extract semantic evidence from an object's name.

    PURE FUNCTION:
    - No DB
    - No ML
    - Deterministic
    - Language-based

    Used by:
    - Step 10 (evidence extraction)
    - Step 20 (candidate scoring)
    - Reranking / explainability
    """

    if not name:
        return []

    evidence: List[Evidence] = []
    evidence.extend(stop_name_evidence(name))
    evidence.extend(poi_name_evidence(name))
    return evidence
