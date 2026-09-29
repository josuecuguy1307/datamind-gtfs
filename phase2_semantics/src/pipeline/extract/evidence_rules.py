from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Iterable, Literal


# ============================================================
# Evidence model
# ============================================================

EvidenceKind = Literal["stop", "poi"]


@dataclass(frozen=True)
class Evidence:
    """
    One semantic evidence signal extracted from OSM tags.

    - kind   : semantic category
    - weight : confidence contribution (0–1)
    - source : rule identifier (debug / explainability)
    """
    kind: EvidenceKind
    weight: float
    source: str


# ============================================================
# Rule helpers
# ============================================================

def _has(tags: Dict[str, str], key: str, values: Iterable[str]) -> bool:
    v = tags.get(key)
    return v in values if v is not None else False


# ============================================================
# STOP evidence rules
# ============================================================

def stop_evidence(tags: Dict[str, str]) -> List[Evidence]:
    evidence: List[Evidence] = []

    # Strong stop signals
    if _has(tags, "highway", ["bus_stop", "platform"]):
        evidence.append(Evidence("stop", 1.0, "highway=bus_stop"))

    if _has(tags, "public_transport", ["platform", "stop_position"]):
        evidence.append(Evidence("stop", 0.9, "public_transport=platform"))

    # Medium signals
    if _has(tags, "bus", ["yes"]):
        evidence.append(Evidence("stop", 0.6, "bus=yes"))

    if _has(tags, "tram", ["yes"]):
        evidence.append(Evidence("stop", 0.6, "tram=yes"))

    if _has(tags, "trolleybus", ["yes"]):
        evidence.append(Evidence("stop", 0.6, "trolleybus=yes"))

    return evidence


# ============================================================
# POI evidence rules
# ============================================================

def poi_evidence(tags: Dict[str, str]) -> List[Evidence]:
    evidence: List[Evidence] = []

    # Educational / public places
    if _has(tags, "amenity", ["school", "university", "college"]):
        evidence.append(Evidence("poi", 0.7, "amenity=education"))

    # Commercial / services
    if _has(tags, "shop", ["supermarket", "mall"]):
        evidence.append(Evidence("poi", 0.6, "shop=commercial"))

    if _has(tags, "amenity", ["hospital", "clinic"]):
        evidence.append(Evidence("poi", 0.7, "amenity=healthcare"))

    # Landmarks
    if _has(tags, "tourism", ["attraction", "museum"]):
        evidence.append(Evidence("poi", 0.6, "tourism=attraction"))

    return evidence


# ============================================================
# Unified entrypoint (PURE)
# ============================================================

def collect_evidence(tags: Dict[str, str]) -> List[Evidence]:
    """
    Collect all semantic evidence signals for an OSM object.

    PURE FUNCTION:
    - No DB
    - No ML
    - No state
    - Deterministic

    Used by:
    - Step 10 (extract)
    - Step 20 (candidate scoring)
    - Reranking / explainability
    """

    evidence: List[Evidence] = []
    evidence.extend(stop_evidence(tags))
    evidence.extend(poi_evidence(tags))
    return evidence


# ============================================================
# Optional helpers (used downstream)
# ============================================================

def aggregate_evidence_score(
    evidence: Iterable[Evidence],
    *,
    clip: float = 1.0,
) -> float:
    """
    Aggregate evidence weights into a single score.

    Used later by:
    - candidate scoring
    - rerankers
    """
    score = sum(e.weight for e in evidence)
    return min(score, clip)
