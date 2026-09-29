from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.constructor_v2.common import to_jsonable
from src.constructor_v2.schemas.route_input import NormalizedStop


@dataclass(slots=True)
class OrderedLeg:
    from_stop_id: str
    to_stop_id: str
    road_distance_m: float
    road_duration_s: float
    straight_distance_m: float
    detour_ratio: float
    geometry_geojson: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ValidationResult:
    name: str
    status: str
    score: float
    metrics: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConfidenceResult:
    label: str
    score: float
    auto_accept: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderProposal:
    method: str
    ordered_stop_ids: tuple[str, ...]
    objective_value: float
    objective_unit: str
    skipped_stop_ids: tuple[str, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RouteOutput:
    route: str
    cooperative: str
    route_type: str
    normalized_stops: list[NormalizedStop]
    selected_method: str
    baseline_order: OrderProposal | None
    matrix_order: OrderProposal | None
    refined_order: OrderProposal | None
    final_stops: list[NormalizedStop]
    dropped_stop_ids: list[str]
    legs: list[OrderedLeg]
    geometry_geojson: dict[str, Any]
    validation: dict[str, ValidationResult]
    confidence: ConfidenceResult
    diagnostics: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)
