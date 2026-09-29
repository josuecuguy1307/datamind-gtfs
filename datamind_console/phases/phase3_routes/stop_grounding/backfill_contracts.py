"""
Data contracts for missing-node backfill bridge.

Connects gap detection signals from the stop grounding pipeline to the
P1.3B backfill executor in the pipeline autopilot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SignalType(str, Enum):
    """Source signal that triggered a backfill candidate."""
    UNFILLED_GAP = "unfilled_gap"
    SYNTHETIC_TERMINUS = "synthetic_terminus"


class BackfillStatus(str, Enum):
    """Outcome of a backfill attempt."""
    PENDING = "pending"
    QUERYING = "querying"
    FOUND = "found"
    NOT_FOUND = "not_found"
    PROMOTED = "promoted"
    REJECTED = "rejected"


@dataclass
class MissingNodeCandidate:
    """A location where a stop should exist but doesn't in node_prod."""
    candidate_id: str
    route_id: str
    canton: str
    province: str
    signal_type: SignalType
    expected_lat: float
    expected_lon: float
    search_radius_m: float
    gap_distance_m: Optional[float] = None
    gap_start_stop_id: Optional[str] = None
    gap_end_stop_id: Optional[str] = None
    confidence: float = 0.0
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "route_id": self.route_id,
            "canton": self.canton,
            "province": self.province,
            "signal_type": self.signal_type.value,
            "expected_lat": round(self.expected_lat, 6),
            "expected_lon": round(self.expected_lon, 6),
            "search_radius_m": round(self.search_radius_m, 1),
            "gap_distance_m": round(self.gap_distance_m, 1) if self.gap_distance_m else None,
            "gap_start_stop_id": self.gap_start_stop_id,
            "gap_end_stop_id": self.gap_end_stop_id,
            "confidence": round(self.confidence, 4),
            "context": self.context,
        }


@dataclass
class BackfillResult:
    """Outcome of processing a single MissingNodeCandidate."""
    candidate: MissingNodeCandidate
    status: BackfillStatus
    overpass_hits: int = 0
    promoted_node_id: Optional[str] = None
    promoted_name: Optional[str] = None
    final_confidence: float = 0.0
    algorithm_used: str = ""
    log: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "status": self.status.value,
            "overpass_hits": self.overpass_hits,
            "promoted_node_id": self.promoted_node_id,
            "promoted_name": self.promoted_name,
            "final_confidence": round(self.final_confidence, 4),
            "algorithm_used": self.algorithm_used,
            "log": self.log,
        }
