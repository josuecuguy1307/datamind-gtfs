# phase4_semantics/common/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------
# Phase 4 Evidence
# ---------------------------------------------------------------------

@dataclass
class EvidenceRecord:
    """
    Canonical evidence record (Phase 4).

    IMPORTANT ALIGNMENT:
    - The semantic compiler reads ev.payload (JSON-like dict)
      and expects payload["extracted"]["naming"]["label"] when available.
    - DB loaders often have created_at + match_score.
    - Your pipeline may still want raw_payload / extracted / normalized.

    This model keeps BOTH styles:
      - payload: the canonical blob used by compiler + DB
      - raw_payload/extracted/normalized: convenience fields for pipelines

    And it keeps them synchronized in __post_init__.
    """

    # DB identity / traceability
    record_id: Optional[str] = None            # DB primary key (uuid/text)
    route_id: Optional[str] = None             # optional (handy in pipelines/logs)

    # Source metadata
    source_type: Optional[str] = "unknown"     # e.g. "osm_overpass_seed", "gtfs_feed"
    source_id: Optional[str] = None            # stable identifier inside that source (relation id, doc row key, etc.)
    source_url: Optional[str] = None
    title: Optional[str] = None

    # Confidence / ranking metadata
    confidence_hint: Optional[float] = 0.5     # [0..1] or None
    created_at: Optional[datetime] = None      # DB timestamp if available
    match_score: Optional[float] = None        # from semantics.route_evidence_matches if present

    # Canonical JSON payload (what DB stores + what compiler reads)
    payload: Dict[str, Any] = field(default_factory=dict)

    # Optional “split view” fields (kept in sync with payload)
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    extracted: Dict[str, Any] = field(default_factory=dict)
    normalized: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Normalize confidence_hint to float or None
        if self.confidence_hint is not None:
            try:
                self.confidence_hint = float(self.confidence_hint)
            except Exception:
                self.confidence_hint = 0.5

        # Ensure payload is a dict
        if self.payload is None or not isinstance(self.payload, dict):
            self.payload = {}

        # If payload already contains extracted/raw/normalized, hydrate split-fields from it
        # (this helps when EvidenceRecord is built straight from DB rows)
        if "raw_payload" in self.payload and not self.raw_payload:
            v = self.payload.get("raw_payload")
            if isinstance(v, dict):
                self.raw_payload = v

        if "extracted" in self.payload and not self.extracted:
            v = self.payload.get("extracted")
            if isinstance(v, dict):
                self.extracted = v

        if "normalized" in self.payload and not self.normalized:
            v = self.payload.get("normalized")
            if isinstance(v, dict):
                self.normalized = v

        # If split-fields exist but payload is missing them, inject into payload
        # (this guarantees compiler can always read payload["extracted"] etc.)
        if self.raw_payload and "raw_payload" not in self.payload:
            self.payload["raw_payload"] = self.raw_payload
        if self.extracted and "extracted" not in self.payload:
            self.payload["extracted"] = self.extracted
        if self.normalized and "normalized" not in self.payload:
            self.payload["normalized"] = self.normalized

        # Final safety: extracted must be a dict if present
        if "extracted" in self.payload and not isinstance(self.payload["extracted"], dict):
            self.payload["extracted"] = {}

        # Keep extracted field consistent with payload
        if not self.extracted and isinstance(self.payload.get("extracted"), dict):
            self.extracted = self.payload["extracted"]


# ---------------------------------------------------------------------
# Phase 4 Route Context
# ---------------------------------------------------------------------

@dataclass
class StopContext:
    """
    Stop context for Phase 4 naming + direction + semantic alignment.
    Only .name is strictly required by semantic_compiler's _extract_stop_names,
    but we keep seq/lat/lon/raw for richer engines.
    """
    stop_id: str
    seq: Optional[int] = None
    name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteContext:
    """
    Context loaded from route_prod.routes (+ stop nodes).
    Must align with semantic_compiler expectations:

      - ctx.stops: list of StopContext objects (with .name)
      - ctx.start_latlon / ctx.end_latlon: (lat, lon) tuples for fallback direction
      - optional: ctx.stop_names convenience for faster extraction
    """
    route_id: str

    # Raw route row (row_to_json)
    route_row: Dict[str, Any] = field(default_factory=dict)

    # Geometry context (optional)
    geometry_ewkt: Optional[str] = None
    start_latlon: Optional[Tuple[float, float]] = None  # (lat, lon)
    end_latlon: Optional[Tuple[float, float]] = None    # (lat, lon)
    bbox: Optional[Tuple[float, float, float, float]] = None  # (minx, miny, maxx, maxy)

    # Stops
    stops: List[StopContext] = field(default_factory=list)

    # Optional direct list of stop names (semantic_compiler checks this first)
    stop_names: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # If stop_names not provided, derive from stops (non-empty names)
        if not self.stop_names and self.stops:
            out: List[str] = []
            for s in self.stops:
                n = (s.name or "").strip()
                if n:
                    out.append(n)
            self.stop_names = out


# ---------------------------------------------------------------------
# Phase 4 Ranking Candidate (optional, but exported by common/__init__.py)
# ---------------------------------------------------------------------

@dataclass
class RankerCandidate:
    """
    Lightweight container for ranking candidates (Phase 4).

    Used when you want to pass around candidates + features + score
    in a consistent way (ML ranker, deterministic scoring, hybrid ranking).

    It is intentionally generic:
      - candidate_id: stable identifier (name string, record_id, relation_id, etc.)
      - label: human-readable label (often same as candidate_id for name strings)
      - score: current score (deterministic or model output)
      - features: numeric feature map used for ML models (optional)
      - payload: extra raw metadata (optional)
    """
    candidate_id: str
    label: Optional[str] = None
    score: float = 0.0
    source_type: Optional[str] = None
    features: Dict[str, Any] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)
