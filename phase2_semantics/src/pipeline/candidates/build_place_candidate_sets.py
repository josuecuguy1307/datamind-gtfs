from __future__ import annotations

from typing import Dict, Any, List, Optional

from src.pipeline.extract.evidence_rules import collect_evidence
from src.pipeline.extract.extract_name_evidence import extract_name_evidence


def _pick_canonical_name(evidence_rows: List[Dict[str, Any]]) -> str:
    """
    Choose a canonical name for the place candidate.

    Priority (best → worst):
      official_name > name:es > name > short_name > alt_name > first raw_text
    """
    priority = [
        "official_name",
        "name:es",
        "name",
        "short_name",
        "alt_name",
    ]

    # Try priority sources
    by_source: Dict[str, List[str]] = {}
    for r in evidence_rows:
        s = (r.get("source") or "").strip()
        t = (r.get("raw_text") or "").strip()
        if not t:
            continue
        by_source.setdefault(s, []).append(t)

    for p in priority:
        vals = by_source.get(p)
        if vals:
            return vals[0].strip()

    # fallback: first non-empty raw_text
    for r in evidence_rows:
        t = (r.get("raw_text") or "").strip()
        if t:
            return t

    return "Unknown Place"


def _infer_place_type(tags: Dict[str, Any]) -> str:
    """
    Infer place type that satisfies DB CHECK constraint:
      ('STOP','POI','STATION','TERMINAL','OTHER')
    """
    name = (tags.get("name") or "")
    name_low = str(name).lower()

    # explicit station/terminal hints (strong)
    if "terminal" in name_low:
        return "TERMINAL"
    if "estación" in name_low or "estacion" in name_low:
        return "STATION"

    # evidence from tags + name-based semantic rules
    stop_score = 0.0
    poi_score = 0.0

    for ev in collect_evidence({k: str(v) for k, v in tags.items()}):
        if ev.kind == "stop":
            stop_score += ev.weight
        elif ev.kind == "poi":
            poi_score += ev.weight

    for ev in extract_name_evidence(tags.get("name")):
        if ev.kind == "stop":
            stop_score += ev.weight
        elif ev.kind == "poi":
            poi_score += ev.weight

    # Decision
    if stop_score >= 1.0 and stop_score >= poi_score:
        return "STOP"
    if poi_score >= 0.7 and poi_score > stop_score:
        return "POI"

    return "OTHER"


def build_place_candidate_row(
    *,
    place_set_id: str,
    node_id: str,
    tags_snapshot: Dict[str, Any],
    evidence_rows: List[Dict[str, Any]],
    canonical_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build a geo_work.place_candidates row (DB aligned).
    """

    name = canonical_name or _pick_canonical_name(evidence_rows)
    ptype = _infer_place_type(tags_snapshot)

    provenance = {
        "node_id": node_id,
        "canonical_name_source": "priority_rules",
        "evidence_sources": sorted(list({r.get("source") for r in evidence_rows if r.get("source")})),
        "n_evidence_rows": len(evidence_rows),
    }

    # Optional score = simple evidence count proxy (can be replaced later)
    score = float(min(len(evidence_rows) / 10.0, 1.0))

    return {
        "place_set_id": place_set_id,
        "proposed_canonical_name": name,
        "proposed_place_type": ptype,
        "center_geom": None,  # keep null for now (safe)
        "provenance": provenance,
        "score": score,
    }
