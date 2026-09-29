"""
Data contracts for the Sequence Discovery & Geometry Constructor pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Stage A — Route Seed
# ---------------------------------------------------------------------------

@dataclass
class RouteSeed:
    route_name: str
    operator_name: Optional[str] = None
    cooperative_name: Optional[str] = None
    corridor_description: Optional[str] = None
    anchor_a_hint: str = ""
    anchor_b_hint: str = ""
    intermediate_hints: List[str] = field(default_factory=list)
    locality_hints: List[str] = field(default_factory=list)
    sequence_seed_fragments: List[str] = field(default_factory=list)
    source_notes: Optional[Any] = None
    raw_anchor_a_hint: Optional[str] = None
    raw_anchor_b_hint: Optional[str] = None
    seed_normalization_notes: List[str] = field(default_factory=list)
    expected_geographic_envelope: Optional[Dict[str, Any]] = None
    operator_id: Optional[int] = None
    cooperative_id: Optional[int] = None
    # Linkage
    route_job_id: Optional[str] = None
    service_route_id: Optional[str] = None
    direction_id: Optional[int] = None
    coverage_gap_id: Optional[str] = None
    sector_key: Optional[str] = None
    # Camino D PIEZA 7 (2026-04-09): province routing token for the N-province
    # catalog model. None == treat as sample_region (legacy default). Populated
    # by discovery_pipeline._normalize_route_seed from DualCatalogContext.
    province: Optional[str] = None

    def idempotency_key(self) -> str:
        parts = [
            (self.route_name or "").strip().lower(),
            str(self.operator_id or ""),
            str(self.direction_id or ""),
        ]
        return "|".join(parts)


# ---------------------------------------------------------------------------
# Stage B — Stop Grounding
# ---------------------------------------------------------------------------

@dataclass
class StopMatch:
    stop_id: str
    stop_name: str
    aliases: List[str] = field(default_factory=list)
    locality: str = ""
    operator_id: Optional[int] = None
    lat: float = 0.0
    lon: float = 0.0
    name_similarity: float = 0.0
    alias_match: bool = False
    locality_match: bool = False
    operator_match: bool = False
    composite_score: float = 0.0
    place_id: Optional[str] = None
    geography_score: float = 0.0
    in_expected_geography: bool = True
    distance_to_expected_bbox_m: float = 0.0
    locality_consistency_score: float = 0.0
    match_source: str = "db_stop"
    text_alignment_score: float = 0.0
    matched_locality_keys: List[str] = field(default_factory=list)
    route_count: int = 0
    distance_m: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stop_id": self.stop_id,
            "stop_name": self.stop_name,
            "aliases": self.aliases,
            "locality": self.locality,
            "operator_id": self.operator_id,
            "lat": self.lat,
            "lon": self.lon,
            "name_similarity": round(self.name_similarity, 4),
            "alias_match": self.alias_match,
            "locality_match": self.locality_match,
            "operator_match": self.operator_match,
            "composite_score": round(self.composite_score, 4),
            "place_id": self.place_id,
            "geography_score": round(self.geography_score, 4),
            "in_expected_geography": self.in_expected_geography,
            "distance_to_expected_bbox_m": round(self.distance_to_expected_bbox_m, 2),
            "locality_consistency_score": round(self.locality_consistency_score, 4),
            "match_source": self.match_source,
            "text_alignment_score": round(self.text_alignment_score, 4),
            "matched_locality_keys": self.matched_locality_keys,
            "route_count": self.route_count,
            "distance_m": round(self.distance_m, 2),
            "metadata": self.metadata,
        }


@dataclass
class StopGroundingResult:
    matched_anchor_a_candidates: List[StopMatch] = field(default_factory=list)
    matched_anchor_b_candidates: List[StopMatch] = field(default_factory=list)
    matched_intermediate_candidates: Dict[str, List[StopMatch]] = field(default_factory=dict)
    unmatched_hints: List[str] = field(default_factory=list)
    overall_grounding_confidence: float = 0.0
    grounding_notes: str = ""

    def best_anchor_a(self) -> Optional[StopMatch]:
        return self.matched_anchor_a_candidates[0] if self.matched_anchor_a_candidates else None

    def best_anchor_b(self) -> Optional[StopMatch]:
        return self.matched_anchor_b_candidates[0] if self.matched_anchor_b_candidates else None

    def best_intermediates_ordered(self, hint_order: List[str]) -> List[StopMatch]:
        out = []
        for hint in hint_order:
            matches = self.matched_intermediate_candidates.get(hint, [])
            if matches:
                out.append(matches[0])
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "matched_anchor_a_candidates": [m.to_dict() for m in self.matched_anchor_a_candidates],
            "matched_anchor_b_candidates": [m.to_dict() for m in self.matched_anchor_b_candidates],
            "matched_intermediate_candidates": {
                k: [m.to_dict() for m in v]
                for k, v in self.matched_intermediate_candidates.items()
            },
            "unmatched_hints": self.unmatched_hints,
            "overall_grounding_confidence": round(self.overall_grounding_confidence, 4),
            "grounding_notes": self.grounding_notes,
        }


# ---------------------------------------------------------------------------
# Stage C — Corridor
# ---------------------------------------------------------------------------

@dataclass
class CorridorResult:
    corridor_geojson: Optional[Dict[str, Any]] = None
    total_length_km: float = 0.0
    segment_count: int = 0
    failed_segments: List[str] = field(default_factory=list)
    waypoints_used: List[Dict[str, Any]] = field(default_factory=list)
    corridor_confidence: float = 0.0
    corridor_notes: str = ""
    expected_geographic_envelope: Optional[Dict[str, Any]] = None
    straight_line_km: float = 0.0
    corridor_inflation_ratio: float = 0.0
    in_bounds_fraction: float = 0.0
    out_of_bounds_reason: str = ""
    geography_plausibility_score: float = 0.0
    rejected_for_geographic_implausibility: bool = False
    route_locality_consistency_notes: List[str] = field(default_factory=list)
    valhalla_meta: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "corridor_geojson": self.corridor_geojson,
            "total_length_km": round(self.total_length_km, 3),
            "segment_count": self.segment_count,
            "failed_segments": self.failed_segments,
            "waypoints_used": self.waypoints_used,
            "corridor_confidence": round(self.corridor_confidence, 4),
            "corridor_notes": self.corridor_notes,
            "expected_geographic_envelope": self.expected_geographic_envelope,
            "straight_line_km": round(self.straight_line_km, 3),
            "corridor_inflation_ratio": round(self.corridor_inflation_ratio, 4),
            "in_bounds_fraction": round(self.in_bounds_fraction, 4),
            "out_of_bounds_reason": self.out_of_bounds_reason,
            "geography_plausibility_score": round(self.geography_plausibility_score, 4),
            "rejected_for_geographic_implausibility": self.rejected_for_geographic_implausibility,
            "route_locality_consistency_notes": self.route_locality_consistency_notes,
            "valhalla_meta": self.valhalla_meta,
        }


# ---------------------------------------------------------------------------
# Stage D — Corridor ∩ Stop DB
# ---------------------------------------------------------------------------

@dataclass
class CorridorStopCandidate:
    stop_id: str
    stop_name: str
    locality: str = ""
    lat: float = 0.0
    lon: float = 0.0
    distance_to_corridor_m: float = 0.0
    path_fraction: float = 0.0
    discovery_buffer_m: int = 50
    is_known_anchor: bool = False
    is_known_intermediate: bool = False
    operator_match: bool = False
    cooperative_match: bool = False
    locality_match: bool = False
    bearing_alignment_deg: float = 0.0
    on_route_score: float = 0.0
    lgbm_score: float = 0.0
    place_id: Optional[str] = None
    ref: Optional[str] = None
    in_expected_geography: bool = True
    distance_to_envelope_m: float = 0.0
    locality_consistency_score: float = 0.0
    stop_usage_frequency: float = 0.0
    rejection_reasons: List[str] = field(default_factory=list)
    in_required_area: bool = False
    in_forbidden_area: bool = False
    distance_to_nearest_required_area_m: float = 99999.0
    stop_source: Optional[str] = None  # "DMQ" | "ANT" | None (unknown)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "stop_id": self.stop_id,
            "stop_name": self.stop_name,
            "locality": self.locality,
            "lat": self.lat,
            "lon": self.lon,
            "distance_to_corridor_m": round(self.distance_to_corridor_m, 2),
            "path_fraction": round(self.path_fraction, 6),
            "discovery_buffer_m": self.discovery_buffer_m,
            "is_known_anchor": self.is_known_anchor,
            "is_known_intermediate": self.is_known_intermediate,
            "operator_match": self.operator_match,
            "cooperative_match": self.cooperative_match,
            "locality_match": self.locality_match,
            "bearing_alignment_deg": round(self.bearing_alignment_deg, 2),
            "on_route_score": round(self.on_route_score, 4),
            "place_id": self.place_id,
            "ref": self.ref,
            "in_expected_geography": self.in_expected_geography,
            "distance_to_envelope_m": round(self.distance_to_envelope_m, 2),
            "locality_consistency_score": round(self.locality_consistency_score, 4),
            "stop_usage_frequency": round(self.stop_usage_frequency, 4),
            "rejection_reasons": self.rejection_reasons,
            "in_required_area": self.in_required_area,
            "in_forbidden_area": self.in_forbidden_area,
            "distance_to_nearest_required_area_m": round(self.distance_to_nearest_required_area_m, 1),
        }
        if self.lgbm_score > 0:
            d["lgbm_score"] = round(self.lgbm_score, 4)
        if self.stop_source:
            d["stop_source"] = self.stop_source
        return d


# ---------------------------------------------------------------------------
# Typed seed / dispatch contracts
# ---------------------------------------------------------------------------

@dataclass
class TypedSeedToken:
    label: str
    kind: str
    role: str
    resolution_policy: str
    confidence: str
    position: int = 0
    note: Optional[str] = None
    terminus_type: Optional[str] = None  # "formal_terminal" | "street_terminus" | "neighborhood_endpoint"
    anchor_lat: Optional[float] = None
    anchor_lon: Optional[float] = None
    anchor_role: str = "waypoint"  # "terminus" | "waypoint" | "intermediate"

    @property
    def has_anchor_coords(self) -> bool:
        return self.anchor_lat is not None and self.anchor_lon is not None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "label": self.label,
            "kind": self.kind,
            "role": self.role,
            "resolution_policy": self.resolution_policy,
            "confidence": self.confidence,
            "position": self.position,
            "note": self.note,
        }
        if self.terminus_type:
            d["terminus_type"] = self.terminus_type
        if self.has_anchor_coords:
            d["anchor_lat"] = self.anchor_lat
            d["anchor_lon"] = self.anchor_lon
            d["anchor_role"] = self.anchor_role
        return d


@dataclass
class CorridorConstraint:
    label: str
    kind: str
    resolution_policy: str
    confidence: str
    usage: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "kind": self.kind,
            "resolution_policy": self.resolution_policy,
            "confidence": self.confidence,
            "usage": self.usage,
        }


@dataclass
class TypedRouteSeed:
    route_name: str
    cooperative: Optional[str]
    sequence_tokens: List[TypedSeedToken]
    corridor_constraints: List[CorridorConstraint]
    localities: List[str]
    notes: Optional[Any] = None
    route_status: Optional[str] = None
    source_entry: Optional[Dict[str, Any]] = None
    jurisdiction: str = "both"  # "DMQ" | "ANT" | "both"
    # Camino D PIEZA 7 (2026-04-09): province routing token. None == sample_region
    # (legacy default). Populated from DualCatalogContext.province at intake.
    province: Optional[str] = None

    def idempotency_key(self) -> str:
        return "|".join(
            [
                (self.route_name or "").strip().lower(),
                (self.cooperative or "").strip().lower(),
            ]
        )

    def non_corridor_tokens(self) -> List[TypedSeedToken]:
        return [
            token
            for token in self.sequence_tokens
            if token.resolution_policy != "use_as_corridor_constraint_only"
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route_name": self.route_name,
            "cooperative": self.cooperative,
            "sequence_tokens": [token.to_dict() for token in self.sequence_tokens],
            "corridor_constraints": [
                constraint.to_dict() for constraint in self.corridor_constraints
            ],
            "localities": self.localities,
            "notes": self.notes,
            "route_status": self.route_status,
        }


@dataclass
class TypedTokenGrounding:
    token: TypedSeedToken
    candidates: List[StopMatch]
    best_candidate: Optional[StopMatch]
    resolution_method: str
    is_resolved: bool
    resolved_from_sector: bool = False
    resolved_from_landmark: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token": self.token.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "best_candidate": self.best_candidate.to_dict() if self.best_candidate else None,
            "resolution_method": self.resolution_method,
            "is_resolved": self.is_resolved,
            "resolved_from_sector": self.resolved_from_sector,
            "resolved_from_landmark": self.resolved_from_landmark,
        }


@dataclass
class TypedGroundingResult:
    token_groundings: Dict[str, TypedTokenGrounding]
    corridor_constraints: List[CorridorConstraint]
    overall_confidence: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token_groundings": {
                label: grounding.to_dict()
                for label, grounding in self.token_groundings.items()
            },
            "corridor_constraints": [
                constraint.to_dict() for constraint in self.corridor_constraints
            ],
            "overall_confidence": round(self.overall_confidence, 4),
        }


@dataclass
class HintCandidateSet:
    anchor_a: StopMatch
    anchor_b: StopMatch
    intermediates: Dict[str, StopMatch]
    unresolved: List[str]
    straight_line_km: float
    waypoint_path_km: float
    path_inflation: float
    ordering_score: float
    envelope_containment: float
    set_coherence_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "anchor_a": self.anchor_a.to_dict(),
            "anchor_b": self.anchor_b.to_dict(),
            "intermediates": {
                label: match.to_dict() for label, match in self.intermediates.items()
            },
            "unresolved": self.unresolved,
            "straight_line_km": round(self.straight_line_km, 4),
            "waypoint_path_km": round(self.waypoint_path_km, 4),
            "path_inflation": round(self.path_inflation, 4),
            "ordering_score": round(self.ordering_score, 4),
            "envelope_containment": round(self.envelope_containment, 4),
            "set_coherence_score": round(self.set_coherence_score, 4),
        }


@dataclass
class CorridorIntersectionResult:
    ordered_candidates: List[CorridorStopCandidate] = field(default_factory=list)
    total_candidates_found: int = 0
    buffer_used_m: int = 50
    coverage_density: float = 0.0
    sparse_segments: List[Dict[str, Any]] = field(default_factory=list)
    intersection_notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ordered_candidates": [c.to_dict() for c in self.ordered_candidates],
            "total_candidates_found": self.total_candidates_found,
            "buffer_used_m": self.buffer_used_m,
            "coverage_density": round(self.coverage_density, 4),
            "sparse_segments": self.sparse_segments,
            "intersection_notes": self.intersection_notes,
        }


# ---------------------------------------------------------------------------
# Stage F — Sequence Skeleton
# ---------------------------------------------------------------------------

@dataclass
class SequenceSkeleton:
    ordered_stops: List[CorridorStopCandidate] = field(default_factory=list)
    gaps: List[Dict[str, Any]] = field(default_factory=list)
    marginal_stops: List[CorridorStopCandidate] = field(default_factory=list)
    rejected_stops: List[CorridorStopCandidate] = field(default_factory=list)
    total_stops: int = 0
    total_length_km: float = 0.0
    avg_stop_spacing_m: float = 0.0
    sequence_confidence: float = 0.0
    weak_segments: List[Dict[str, Any]] = field(default_factory=list)
    express_skip_candidates: List[Dict[str, Any]] = field(default_factory=list)
    branch_variant_risk: List[Dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    # Quality metrics (Block 4)
    stops_removed_by_spacing: int = 0
    stops_removed_by_density: int = 0
    marginals_promoted: int = 0

    def stop_ids(self) -> List[str]:
        return [s.stop_id for s in self.ordered_stops]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ordered_stops": [s.to_dict() for s in self.ordered_stops],
            "gaps": self.gaps,
            "marginal_stops": [s.to_dict() for s in self.marginal_stops],
            "rejected_stops": [s.to_dict() for s in self.rejected_stops],
            "total_stops": self.total_stops,
            "total_length_km": round(self.total_length_km, 3),
            "avg_stop_spacing_m": round(self.avg_stop_spacing_m, 1),
            "sequence_confidence": round(self.sequence_confidence, 4),
            "weak_segments": self.weak_segments,
            "express_skip_candidates": self.express_skip_candidates,
            "branch_variant_risk": self.branch_variant_risk,
            "notes": self.notes,
            "stops_removed_by_spacing": self.stops_removed_by_spacing,
            "stops_removed_by_density": self.stops_removed_by_density,
            "marginals_promoted": self.marginals_promoted,
        }


# ---------------------------------------------------------------------------
# Stage G — Geometry Candidate
# ---------------------------------------------------------------------------

@dataclass
class GeometryCandidate:
    geometry_geojson: Optional[Dict[str, Any]] = None
    derived_from: str = "valhalla_initial"
    geometry_confidence: float = 0.0
    sequence_to_geometry_consistency: float = 0.0
    total_length_km: float = 0.0
    waypoint_count: int = 0
    notes: str = ""
    straight_line_km: float = 0.0
    corridor_inflation_ratio: float = 0.0
    in_bounds_fraction: float = 0.0
    geography_plausibility_score: float = 0.0
    rejected_for_geographic_implausibility: bool = False
    valhalla_meta: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "geometry_geojson": self.geometry_geojson,
            "derived_from": self.derived_from,
            "geometry_confidence": round(self.geometry_confidence, 4),
            "sequence_to_geometry_consistency": round(self.sequence_to_geometry_consistency, 4),
            "total_length_km": round(self.total_length_km, 3),
            "waypoint_count": self.waypoint_count,
            "notes": self.notes,
            "straight_line_km": round(self.straight_line_km, 3),
            "corridor_inflation_ratio": round(self.corridor_inflation_ratio, 4),
            "in_bounds_fraction": round(self.in_bounds_fraction, 4),
            "geography_plausibility_score": round(self.geography_plausibility_score, 4),
            "rejected_for_geographic_implausibility": self.rejected_for_geographic_implausibility,
            "valhalla_meta": self.valhalla_meta,
        }


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------

@dataclass
class ConstructorRunSummary:
    route_seed: Optional[Dict[str, Any]] = None
    expected_geographic_envelope: Optional[Dict[str, Any]] = None
    grounding: Optional[Dict[str, Any]] = None
    corridor: Optional[Dict[str, Any]] = None
    intersection: Optional[Dict[str, Any]] = None
    skeleton: Optional[Dict[str, Any]] = None
    geometry: Optional[Dict[str, Any]] = None
    llm_refinement: Optional[Dict[str, Any]] = None
    geography_audit: Optional[Dict[str, Any]] = None
    status: str = "pending"
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    # Quality metrics (Block 4)
    stops_removed_by_spacing: int = 0
    stops_removed_by_density: int = 0
    marginals_promoted: int = 0
    endpoint_resolution_method: str = ""  # "db_stop", "osm_fallback", "proxy_waypoint"
    llm_advisory_confidence: float = 0.0
    corridor_length_km: float = 0.0
    # Territorial resolution (Stage B2)
    territorial_resolution: Optional[Dict[str, Any]] = None
    territorial_tokens_count: int = 0
    territorial_resolved_count: int = 0
    chain_confidence: float = 0.0
    # Layer 2: Hint set coherence
    hint_coherence: Optional[Dict[str, Any]] = None
    hint_coherence_score: float = 0.0
    # Layer 3: Valhalla feedback loop
    corridor_attempts: int = 1
    corridor_feedback_log: List[Dict[str, Any]] = field(default_factory=list)
    # Layer 4: Arterial waypoint injection
    arterial_waypoints_injected: int = 0
    arterial_injection_log: Optional[Dict[str, Any]] = None
    # Layer 5: Sequence validation
    sequence_validation: Optional[Dict[str, Any]] = None
    stops_removed_by_validation: int = 0
    # Backfill bridge: missing-node candidates for P1.3B
    backfill_candidates: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route_seed": self.route_seed,
            "expected_geographic_envelope": self.expected_geographic_envelope,
            "grounding": self.grounding,
            "corridor": self.corridor,
            "intersection": self.intersection,
            "skeleton": self.skeleton,
            "geometry": self.geometry,
            "llm_refinement": self.llm_refinement,
            "geography_audit": self.geography_audit,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "metrics": self.metrics,
            "stops_removed_by_spacing": self.stops_removed_by_spacing,
            "stops_removed_by_density": self.stops_removed_by_density,
            "marginals_promoted": self.marginals_promoted,
            "endpoint_resolution_method": self.endpoint_resolution_method,
            "llm_advisory_confidence": self.llm_advisory_confidence,
            "corridor_length_km": self.corridor_length_km,
            "territorial_resolution": self.territorial_resolution,
            "territorial_tokens_count": self.territorial_tokens_count,
            "territorial_resolved_count": self.territorial_resolved_count,
            "chain_confidence": self.chain_confidence,
            "hint_coherence": self.hint_coherence,
            "hint_coherence_score": self.hint_coherence_score,
            "corridor_attempts": self.corridor_attempts,
            "corridor_feedback_log": self.corridor_feedback_log,
            "arterial_waypoints_injected": self.arterial_waypoints_injected,
            "arterial_injection_log": self.arterial_injection_log,
            "sequence_validation": self.sequence_validation,
            "stops_removed_by_validation": self.stops_removed_by_validation,
            "backfill_candidates": self.backfill_candidates,
        }
