from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple
import uuid


# -----------------------------
# Common aliases
# -----------------------------

UUID = uuid.UUID
Cost = float
JSON = Dict[str, Any]


# -----------------------------
# Viterbi / sequence models
# -----------------------------
# One "step" i has K_i candidates. Each candidate has an emission_cost.
# Transition costs are handled separately (matrix or function).

@dataclass(frozen=True, slots=True)
class StopCandidate:
    """
    Candidate stop for a single step i.

    stop_id:
        Canonical stop node id (UUID) from geo_prod.
    emission_cost:
        Local penalty for choosing this stop at this step.
        (Lower = better). Example: distance to prior point.
    meta:
        Any extra debug/feature info.
    """
    stop_id: UUID
    emission_cost: Cost
    meta: JSON = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DecodedPath:
    """
    Output of decoding (Viterbi best path, etc.)
    """
    stop_ids: List[UUID]
    total_cost: Cost
    emission_cost_sum: Cost
    transition_cost_sum: Cost
    meta: JSON = field(default_factory=dict)


# Optional: store transition matrices explicitly as float costs.
# transition_matrices[i] is a matrix from step i-1 candidates -> step i candidates
TransitionMatrix = List[List[Cost]]


# -----------------------------
# Route raw / work / prod domain models
# (These mirror your SQL tables so you can pass strongly-typed objects if you want.)
# Your repos can still return dicts; these are for clarity + optional use.
# -----------------------------

@dataclass(frozen=True, slots=True)
class RouteJob:
    route_id: UUID
    created_at: datetime
    created_by: Optional[str] = None
    status: str = "new"
    notes: Optional[str] = None


@dataclass(frozen=True, slots=True)
class RelationStopPriorRow:
    route_id: UUID
    seq: int
    osm_node_id: Optional[int]
    role: Optional[str]
    lat: float
    lon: float
    matched_stop_node_id: Optional[UUID] = None
    match_dist_m: Optional[float] = None


@dataclass(frozen=True, slots=True)
class StopSequenceCandidateSet:
    set_id: UUID
    route_id: UUID
    created_at: datetime
    created_by: Optional[str] = None
    generator_version: str = "v1"
    notes: Optional[str] = None


@dataclass(frozen=True, slots=True)
class StopSequenceCandidateRow:
    candidate_id: UUID
    set_id: UUID
    rank: int
    stop_node_ids: List[UUID]
    matched_stops: Optional[int] = None
    avg_match_dist_m: Optional[float] = None
    max_match_dist_m: Optional[float] = None
    metrics: JSON = field(default_factory=dict)
    created_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class ValhallaPreset:
    preset_id: UUID
    name: str
    is_active: bool
    params: JSON
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GeometryCandidateSet:
    set_id: UUID
    route_id: UUID
    stop_sequence_set_id: Optional[UUID] = None
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
    generator_version: str = "v1"
    notes: Optional[str] = None


@dataclass(frozen=True, slots=True)
class GeometryCandidateRow:
    geometry_candidate_id: UUID
    set_id: UUID
    stop_sequence_candidate_id: Optional[UUID]
    engine: str = "valhalla_route"
    preset_id: Optional[UUID] = None
    params: JSON = field(default_factory=dict)

    # We keep geometry as WKT for portability in Python code.
    # Repos can convert from/to PostGIS using ST_AsText / ST_GeomFromText.
    geom_wkt: Optional[str] = None

    length_m: Optional[float] = None
    avg_stop_dist_m: Optional[float] = None
    max_stop_dist_m: Optional[float] = None
    score: float = 0.0
    metrics: JSON = field(default_factory=dict)
    created_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class GeometryStopRecoveryRow:
    geometry_candidate_id: UUID
    set_id: UUID
    route_id: UUID
    stop_sequence_candidate_id: Optional[UUID] = None
    original_stop_ids: List[UUID] = field(default_factory=list)
    recovered_stop_ids: List[UUID] = field(default_factory=list)
    ambiguous_nearby_stop_ids: List[UUID] = field(default_factory=list)
    rejected_nearby_stop_ids: List[UUID] = field(default_factory=list)
    enriched_stop_ids: List[UUID] = field(default_factory=list)
    insertion_proposals: List[JSON] = field(default_factory=list)
    provenance: JSON = field(default_factory=dict)
    summary_metrics: JSON = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class RouteApproval:
    approval_id: UUID
    route_id: UUID
    chosen_geometry_candidate_id: UUID
    chosen_stop_sequence_candidate_id: UUID
    approved_at: datetime
    approved_by: Optional[str] = None
    notes: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ProdRoute:
    route_id: UUID
    chosen_geometry_candidate_id: UUID
    geom_wkt: str
    stop_node_ids: List[UUID] = field(default_factory=list)
    source: str = "route_constructor"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
