from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid


Json = Dict[str, Any]


@dataclass(frozen=True)
class StopPriorRow:
    route_id: uuid.UUID
    seq: int
    osm_node_id: Optional[int]
    role: Optional[str]
    lat: float
    lon: float
    matched_stop_node_id: Optional[uuid.UUID] = None
    match_dist_m: Optional[float] = None


@dataclass(frozen=True)
class StopSequenceCandidate:
    candidate_id: uuid.UUID
    set_id: uuid.UUID
    rank: int
    stop_node_ids: List[uuid.UUID]
    metrics: Json = field(default_factory=dict)
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class GeometryCandidate:
    geometry_candidate_id: uuid.UUID
    set_id: uuid.UUID
    stop_sequence_candidate_id: uuid.UUID
    engine: str
    preset_id: Optional[uuid.UUID]
    params: Json = field(default_factory=dict)
    length_m: Optional[float] = None
    avg_stop_dist_m: Optional[float] = None
    max_stop_dist_m: Optional[float] = None
    score: float = 0.0
    metrics: Json = field(default_factory=dict)
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class RouteApproval:
    approval_id: uuid.UUID
    route_id: uuid.UUID
    chosen_geometry_candidate_id: uuid.UUID
    chosen_stop_sequence_candidate_id: Optional[uuid.UUID]
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    notes: Optional[str] = None


@dataclass(frozen=True)
class RouteContextFeatures:
    route_id: uuid.UUID
    computed_at: Optional[datetime] = None
    stop_count: Optional[int] = None
    avg_stop_spacing_m: Optional[float] = None
    stop_density_per_km2: Optional[float] = None
    features: Json = field(default_factory=dict)
