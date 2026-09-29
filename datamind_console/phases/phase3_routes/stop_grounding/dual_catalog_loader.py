"""
Dual Catalog Loader — Load + cross-reference seed catalog and geography catalog.

The pipeline reads from TWO input catalogs:
1. Typed Seed Catalog: WHAT each token is and HOW to resolve it
2. Route Geography Catalog: WHERE the route should go and HOW to validate it
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOG = logging.getLogger(__name__)


@dataclass
class AreaDefinition:
    key: str
    description: str
    approx_center: Optional[Dict[str, float]] = None
    approx_bbox: Optional[Dict[str, float]] = None
    sub_sectors: List[str] = field(default_factory=list)
    key_landmarks: List[str] = field(default_factory=list)
    corridor_waypoints: List[Dict[str, Any]] = field(default_factory=list)

    def contains_point(self, lat: float, lon: float) -> bool:
        if not self.approx_bbox:
            return False
        b = self.approx_bbox
        return (
            b.get("south", -90) <= lat <= b.get("north", 90)
            and b.get("west", -180) <= lon <= b.get("east", 180)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "description": self.description,
            "approx_center": self.approx_center,
            "approx_bbox": self.approx_bbox,
            "sub_sectors": self.sub_sectors,
            "key_landmarks": self.key_landmarks,
            "corridor_waypoints": self.corridor_waypoints,
        }


@dataclass
class RouteGeography:
    route_name: str
    cooperative: str = ""
    route_type: str = ""
    expected_distance_km: Optional[Dict[str, float]] = None
    must_pass_through_areas_ordered: List[str] = field(default_factory=list)
    must_NOT_enter: List[str] = field(default_factory=list)
    expected_key_waypoints: List[str] = field(default_factory=list)
    expected_arterials: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route_name": self.route_name,
            "cooperative": self.cooperative,
            "route_type": self.route_type,
            "expected_distance_km": self.expected_distance_km,
            "must_pass_through_areas_ordered": self.must_pass_through_areas_ordered,
            "must_NOT_enter": self.must_NOT_enter,
            "expected_key_waypoints": self.expected_key_waypoints,
            "expected_arterials": self.expected_arterials,
            "notes": self.notes,
        }


@dataclass
class RouteContext:
    """Combined context for a single route from both catalogs."""
    seed_entry: Optional[Dict[str, Any]] = None
    geography: Optional[RouteGeography] = None
    required_areas: List[AreaDefinition] = field(default_factory=list)
    forbidden_areas: List[AreaDefinition] = field(default_factory=list)
    expected_distance: Optional[Dict[str, float]] = None
    expected_arterials: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_seed": self.seed_entry is not None,
            "has_geography": self.geography is not None,
            "required_areas": [a.key for a in self.required_areas],
            "forbidden_areas": [a.key for a in self.forbidden_areas],
            "expected_distance": self.expected_distance,
            "expected_arterials": self.expected_arterials,
        }


@dataclass
class GeoValidationResult:
    passed: bool = False
    score: float = 0.0
    pass_through_compliance: float = 0.0
    areas_traversed: List[str] = field(default_factory=list)
    areas_missed: List[str] = field(default_factory=list)
    forbidden_violations: List[str] = field(default_factory=list)
    distance_score: float = 0.0
    corridor_km: float = 0.0
    issues: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "score": round(self.score, 4),
            "pass_through_compliance": round(self.pass_through_compliance, 4),
            "areas_traversed": self.areas_traversed,
            "areas_missed": self.areas_missed,
            "forbidden_violations": self.forbidden_violations,
            "distance_score": round(self.distance_score, 4),
            "corridor_km": round(self.corridor_km, 2),
            "issues": self.issues,
        }


@dataclass
class DualCatalogContext:
    seed_routes: List[Dict[str, Any]] = field(default_factory=list)
    resolution_rules: Dict[str, Any] = field(default_factory=dict)
    area_definitions: Dict[str, AreaDefinition] = field(default_factory=dict)
    route_geographies: Dict[str, RouteGeography] = field(default_factory=dict)
    seed_catalog_path: str = ""
    geography_catalog_path: str = ""
    province: Optional[str] = None  # from geography catalog top-level "province" field; defaults to None → "sample_region" downstream
    unit_name: Optional[str] = None  # from geography catalog top-level "unit_name", else derived from catalog_name stem

    def get_route_context(self, route_name: str) -> RouteContext:
        seed_entry = next(
            (r for r in self.seed_routes
             if (r.get("route_name") or r.get("route") or "") == route_name),
            None,
        )
        geo_entry = self.route_geographies.get(route_name)

        required_areas = []
        forbidden_areas = []
        if geo_entry:
            for area_key in geo_entry.must_pass_through_areas_ordered:
                area_def = self.area_definitions.get(area_key)
                if area_def:
                    required_areas.append(area_def)

            for area_key in geo_entry.must_NOT_enter:
                area_def = self.area_definitions.get(area_key)
                if area_def:
                    forbidden_areas.append(area_def)

        return RouteContext(
            seed_entry=seed_entry,
            geography=geo_entry,
            required_areas=required_areas,
            forbidden_areas=forbidden_areas,
            expected_distance=geo_entry.expected_distance_km if geo_entry else None,
            expected_arterials=geo_entry.expected_arterials if geo_entry else [],
        )

    def get_area_bbox(self, area_key: str) -> Optional[Dict[str, float]]:
        area = self.area_definitions.get(area_key)
        if area and area.approx_bbox:
            return area.approx_bbox
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seed_route_count": len(self.seed_routes),
            "area_definition_count": len(self.area_definitions),
            "route_geography_count": len(self.route_geographies),
            "seed_catalog_path": self.seed_catalog_path,
            "geography_catalog_path": self.geography_catalog_path,
        }


def _parse_area_definition(key: str, raw: Dict[str, Any]) -> AreaDefinition:
    return AreaDefinition(
        key=key,
        description=str(raw.get("description") or ""),
        approx_center=raw.get("approx_center"),
        approx_bbox=raw.get("approx_bbox"),
        sub_sectors=list(raw.get("sub_sectors") or []),
        key_landmarks=list(raw.get("key_landmarks") or []),
        corridor_waypoints=list(raw.get("corridor_waypoints") or []),
    )


def _parse_route_geography(raw: Dict[str, Any]) -> RouteGeography:
    return RouteGeography(
        route_name=str(raw.get("route_name") or ""),
        cooperative=str(raw.get("cooperative") or ""),
        route_type=str(raw.get("route_type") or ""),
        expected_distance_km=raw.get("expected_distance_km"),
        must_pass_through_areas_ordered=list(raw.get("must_pass_through_areas_ordered") or []),
        must_NOT_enter=list(raw.get("must_NOT_enter") or []),
        expected_key_waypoints=list(raw.get("expected_key_waypoints") or []),
        expected_arterials=list(raw.get("expected_arterials") or []),
        notes=str(raw.get("notes") or ""),
    )


def load_dual_catalogs(
    seed_catalog_path: str,
    geography_catalog_path: str,
) -> DualCatalogContext:
    """Load both catalogs and cross-reference them."""
    with open(seed_catalog_path, encoding="utf-8") as f:
        seed_catalog = json.load(f)

    with open(geography_catalog_path, encoding="utf-8") as f:
        geo_catalog = json.load(f)

    area_defs: Dict[str, AreaDefinition] = {}
    for key, raw in (geo_catalog.get("area_definitions") or {}).items():
        area_defs[key] = _parse_area_definition(key, raw)

    route_geos: Dict[str, RouteGeography] = {}
    for raw in geo_catalog.get("route_geographies") or []:
        rg = _parse_route_geography(raw)
        if rg.route_name:
            route_geos[rg.route_name] = rg

    routes = list(seed_catalog.get("routes") or [])
    rules = dict(seed_catalog.get("sequence_resolution_rules") or {})

    province = (geo_catalog.get("province") or seed_catalog.get("province") or None)
    if isinstance(province, str):
        province = province.strip().lower() or None
    unit_name = (
        geo_catalog.get("unit_name")
        or seed_catalog.get("unit_name")
        or Path(geography_catalog_path).stem.replace("_route_geography_catalog", "")
    )
    if isinstance(unit_name, str):
        unit_name = unit_name.strip() or None

    _LOG.info(
        "Loaded dual catalogs for province=%s, unit=%s: %d seed routes, %d area definitions, %d route geographies",
        province or "sample_region",
        unit_name or "unknown",
        len(routes),
        len(area_defs),
        len(route_geos),
    )

    return DualCatalogContext(
        seed_routes=routes,
        resolution_rules=rules,
        area_definitions=area_defs,
        route_geographies=route_geos,
        seed_catalog_path=seed_catalog_path,
        geography_catalog_path=geography_catalog_path,
        province=province,
        unit_name=unit_name,
    )


def load_geography_catalog_only(geography_catalog_path: str) -> DualCatalogContext:
    """Load only the geography catalog (when seed catalog is loaded separately)."""
    with open(geography_catalog_path, encoding="utf-8") as f:
        geo_catalog = json.load(f)

    area_defs: Dict[str, AreaDefinition] = {}
    for key, raw in (geo_catalog.get("area_definitions") or {}).items():
        area_defs[key] = _parse_area_definition(key, raw)

    route_geos: Dict[str, RouteGeography] = {}
    for raw in geo_catalog.get("route_geographies") or []:
        rg = _parse_route_geography(raw)
        if rg.route_name:
            route_geos[rg.route_name] = rg

    province = geo_catalog.get("province")
    if isinstance(province, str):
        province = province.strip().lower() or None
    unit_name = (
        geo_catalog.get("unit_name")
        or Path(geography_catalog_path).stem.replace("_route_geography_catalog", "")
    )
    if isinstance(unit_name, str):
        unit_name = unit_name.strip() or None

    return DualCatalogContext(
        area_definitions=area_defs,
        route_geographies=route_geos,
        geography_catalog_path=geography_catalog_path,
        province=province,
        unit_name=unit_name,
    )
