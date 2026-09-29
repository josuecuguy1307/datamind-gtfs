"""Data models for the HADES Quality Gate.

All shared dataclasses used across rules, fixers, strategies, and benchmarks.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Severity(str, enum.Enum):
    ERROR = "error"
    WARNING = "warning"


class DecisionType(str, enum.Enum):
    COMMIT = "commit"
    REJECT = "reject"
    DEFER = "defer"


# ---------------------------------------------------------------------------
# Entity wrappers (lightweight, DB-agnostic)
# ---------------------------------------------------------------------------

@dataclass
class Stop:
    """A node_prod stop or GTFS stop."""
    stop_id: str
    name: Optional[str]
    ref: Optional[str]
    lat: float
    lon: float
    operator: Optional[str] = None
    confidence: float = 0.0
    node_type: str = "STOP"
    source: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Route:
    """A route_prod route."""
    route_id: str
    route_name: Optional[str]
    service_route_id: Optional[str]
    direction_id: int = 0
    stop_node_ids: List[str] = field(default_factory=list)
    operator_name: Optional[str] = None
    geom_wkt: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Shape:
    """A shape from gtfs_work.gtfs_shapes."""
    shape_id: str
    points: List[ShapePoint] = field(default_factory=list)


@dataclass
class ShapePoint:
    """Single point in a shape."""
    lat: float
    lon: float
    sequence: int
    dist_traveled: float = 0.0


@dataclass
class RouteSemantics:
    """Naming / catalog entry for a route."""
    route_id: str
    operator: Optional[str] = None
    route_short_name: Optional[str] = None
    route_long_name: Optional[str] = None
    route_type: int = 3
    public_origin: Optional[str] = None
    public_destination: Optional[str] = None
    jurisdiction: Optional[str] = None
    confidence: float = 0.0
    approved: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ScheduleProfile:
    """Schedule data for a route."""
    route_id: str
    peak_runtime_min: Optional[float] = None
    offpeak_runtime_min: Optional[float] = None
    service_days: Optional[Dict[str, bool]] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Fare:
    """Fare record."""
    fare_id: str
    route_id: Optional[str] = None
    agency_id: Optional[str] = None
    price: float = 0.0
    currency_type: str = "USD"
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Issue / Fix / Decision dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EntityIssue:
    """A quality issue detected by a rule."""
    entity_type: str          # "stop" | "route" | "shape" | "naming" | "timing" | "fare"
    entity_id: str
    rule_name: str            # e.g. "stop_name_placeholder"
    severity: Severity
    description: str
    original_value: Any
    phase_origin: int         # 1–5


@dataclass
class FixAttempt:
    """Result of an auto-fix attempt."""
    success: bool
    new_value: Any
    confidence: float         # 0.0–1.0
    log: str


@dataclass
class RouteBack:
    """An entity that must be rerouted to an earlier phase."""
    entity_id: str
    entity_type: str
    target_phase: int         # 1, 2, 3, or 4
    action: str               # e.g. "re_geocode", "re_name"
    reason: str
    priority: str = "medium"  # "critical" | "high" | "medium"


@dataclass
class StrategyDecision:
    """A strategy's decision for a single issue."""
    decision: DecisionType
    reason: str
    confidence_threshold_used: Optional[float] = None


@dataclass
class QualityVerdict:
    """Aggregate verdict for a canton gate run."""
    status: str               # "pass" | "pass_with_fixes" | "fail"
    entities_checked: int = 0
    issues_found: int = 0
    issues_auto_fixed: int = 0
    issues_requiring_review: int = 0


@dataclass
class GateReport:
    """Full quality gate report for a canton."""
    canton: str
    province: str
    export_run_id: str
    timestamp: str            # ISO 8601
    verdict: QualityVerdict
    issues: List[EntityIssue] = field(default_factory=list)
    fixes_attempted: List[FixAttempt] = field(default_factory=list)
    route_backs: List[RouteBack] = field(default_factory=list)
    pass_to_phase5: bool = False
    strategy_name: str = ""
    decisions: List[StrategyDecision] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Quality Gate Input
# ---------------------------------------------------------------------------

@dataclass
class QualityGateInput:
    """Everything a strategy needs to run the gate."""
    canton: str
    province: str
    export_run_id: str = ""
    stops: List[Stop] = field(default_factory=list)
    routes: List[Route] = field(default_factory=list)
    shapes: List[Shape] = field(default_factory=list)
    semantics: List[RouteSemantics] = field(default_factory=list)
    schedules: List[ScheduleProfile] = field(default_factory=list)
    fares: List[Fare] = field(default_factory=list)
    db_dsn: Optional[str] = None

    @property
    def all_entities(self) -> Dict[str, list]:
        return {
            "stops": self.stops,
            "routes": self.routes,
            "shapes": self.shapes,
            "semantics": self.semantics,
            "schedules": self.schedules,
            "fares": self.fares,
        }
