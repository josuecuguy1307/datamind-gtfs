"""
Geographic Validator — Corridor + sequence validation against geography catalog.

Validates that built corridors and stop sequences comply with the geographic
constraints defined in the route geography catalog.
"""
from __future__ import annotations

import math
import logging
from typing import Any, Dict, List, Optional, Tuple

from datamind_console.phases.phase3_routes.stop_grounding.dual_catalog_loader import (
    AreaDefinition,
    DualCatalogContext,
    GeoValidationResult,
    RouteContext,
    RouteGeography,
)

_LOG = logging.getLogger(__name__)


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2.0 * r * math.asin(math.sqrt(min(1.0, a)))


def _linestring_length_km(coords: List[List[float]]) -> float:
    total = 0.0
    for i in range(len(coords) - 1):
        total += _haversine_m(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
    return total / 1000.0


def _point_in_area(lat: float, lon: float, area: AreaDefinition) -> bool:
    return area.contains_point(lat, lon)


def _corridor_intersects_area(
    corridor_geojson: Dict[str, Any],
    area: AreaDefinition,
) -> bool:
    """Check if any point of the corridor falls within the area bbox."""
    if not area.approx_bbox:
        return False
    coords = corridor_geojson.get("coordinates") or []
    if not coords:
        return False

    bbox = area.approx_bbox
    south = bbox.get("south", -90)
    north = bbox.get("north", 90)
    west = bbox.get("west", -180)
    east = bbox.get("east", 180)

    # Sample every Nth point for performance (corridors can have thousands of points)
    step = max(1, len(coords) // 200)
    for i in range(0, len(coords), step):
        lon, lat = coords[i][0], coords[i][1]
        if south <= lat <= north and west <= lon <= east:
            return True

    # Also check last point
    if coords:
        lon, lat = coords[-1][0], coords[-1][1]
        if south <= lat <= north and west <= lon <= east:
            return True

    return False


def _any_waypoint_in_area(
    waypoints: List[Dict[str, Any]],
    area: AreaDefinition,
) -> bool:
    """Check if any waypoint falls within the area bbox."""
    for wp in waypoints:
        lat = float(wp.get("lat") or 0)
        lon = float(wp.get("lon") or 0)
        if _point_in_area(lat, lon, area):
            return True
    return False


def validate_corridor_geography(
    corridor_geojson: Optional[Dict[str, Any]],
    route_context: RouteContext,
    *,
    corridor_length_km: float = 0.0,
) -> GeoValidationResult:
    """
    Validate a corridor against the geography catalog.

    Checks:
    1. Must-pass-through areas — corridor traverses required areas
    2. Must-NOT-enter areas — corridor avoids forbidden areas
    3. Expected distance — corridor length within expected range
    """
    result = GeoValidationResult()

    if not route_context.geography:
        # No geography constraints — pass by default
        result.passed = True
        result.score = 1.0
        result.pass_through_compliance = 1.0
        result.distance_score = 1.0
        return result

    geo = route_context.geography
    result.corridor_km = corridor_length_km

    # 1. Must-pass-through compliance
    required = route_context.required_areas
    if required and corridor_geojson:
        traversed = []
        missed = []
        for area in required:
            if _corridor_intersects_area(corridor_geojson, area):
                traversed.append(area.key)
            else:
                missed.append(area.key)
                result.issues.append(f"Corridor MISSES required area: {area.key}")
        result.areas_traversed = traversed
        result.areas_missed = missed
        result.pass_through_compliance = len(traversed) / max(len(required), 1)
    elif not required:
        result.pass_through_compliance = 1.0
    else:
        result.pass_through_compliance = 0.0

    # 2. Must-NOT-enter violations
    forbidden = route_context.forbidden_areas
    if forbidden and corridor_geojson:
        for area in forbidden:
            if _corridor_intersects_area(corridor_geojson, area):
                result.forbidden_violations.append(area.key)
                result.issues.append(f"Corridor ENTERS forbidden area: {area.key}")

    no_violation_score = 1.0 if not result.forbidden_violations else max(
        0.0, 1.0 - 0.3 * len(result.forbidden_violations)
    )

    # 3. Expected distance check
    expected = route_context.expected_distance
    if expected and corridor_length_km > 0:
        min_km = float(expected.get("min") or 0)
        max_km = float(expected.get("max") or 999)
        if min_km <= corridor_length_km <= max_km:
            result.distance_score = 1.0
        elif corridor_length_km > max_km * 2:
            result.distance_score = 0.1
            result.issues.append(
                f"Corridor {corridor_length_km:.1f}km exceeds max {max_km}km by 2x+"
            )
        elif corridor_length_km > max_km:
            result.distance_score = 0.5
            result.issues.append(
                f"Corridor {corridor_length_km:.1f}km exceeds max {max_km}km"
            )
        elif corridor_length_km < min_km * 0.5:
            result.distance_score = 0.3
            result.issues.append(
                f"Corridor {corridor_length_km:.1f}km below min {min_km}km"
            )
        else:
            result.distance_score = 0.7
    else:
        result.distance_score = 1.0

    # Combined score
    result.score = (
        0.40 * result.pass_through_compliance
        + 0.30 * no_violation_score
        + 0.30 * result.distance_score
    )

    result.passed = result.score >= 0.60 and not result.forbidden_violations
    return result


def validate_stops_geography(
    stops: List[Dict[str, Any]],
    route_context: RouteContext,
) -> Dict[str, Any]:
    """
    Check which stops are in required/forbidden areas.
    Returns per-stop geography flags for use in scoring.
    """
    if not route_context.geography:
        return {"stop_flags": {}, "has_constraints": False}

    stop_flags: Dict[str, Dict[str, Any]] = {}

    for stop in stops:
        stop_id = str(stop.get("stop_id") or stop.get("node_id") or "")
        lat = float(stop.get("lat") or 0)
        lon = float(stop.get("lon") or 0)

        in_required = False
        in_forbidden = False
        nearest_required_m = float("inf")

        for area in route_context.required_areas:
            if _point_in_area(lat, lon, area):
                in_required = True
            if area.approx_center:
                dist = _haversine_m(
                    lon, lat,
                    area.approx_center.get("lon", 0),
                    area.approx_center.get("lat", 0),
                )
                nearest_required_m = min(nearest_required_m, dist)

        for area in route_context.forbidden_areas:
            if _point_in_area(lat, lon, area):
                in_forbidden = True

        stop_flags[stop_id] = {
            "in_required_area": in_required,
            "in_forbidden_area": in_forbidden,
            "distance_to_nearest_required_area_m": (
                nearest_required_m if nearest_required_m < float("inf") else 0.0
            ),
        }

    return {"stop_flags": stop_flags, "has_constraints": True}


def score_hint_set_geography(
    waypoints: List[Dict[str, Any]],
    route_context: RouteContext,
) -> Tuple[float, float, List[str]]:
    """
    Score a hint set's geographic compliance.
    Returns (area_compliance_score, no_violation_score, issues).
    Used by hint_set_coherence to augment set scoring.
    """
    if not route_context.geography:
        return 1.0, 1.0, []

    issues: List[str] = []

    # Must-pass-through compliance
    required = route_context.required_areas
    area_compliance = 1.0
    if required:
        areas_hit = 0
        for area in required:
            if _any_waypoint_in_area(waypoints, area):
                areas_hit += 1
        area_compliance = areas_hit / len(required)

    # Must-NOT-enter compliance
    forbidden = route_context.forbidden_areas
    no_violation = 1.0
    if forbidden:
        violations = 0
        for area in forbidden:
            if _any_waypoint_in_area(waypoints, area):
                violations += 1
                issues.append(f"Waypoint in forbidden area: {area.key}")
        if violations:
            no_violation = max(0.0, 1.0 - 0.3 * violations)

    return area_compliance, no_violation, issues


def get_area_bbox_for_sector(
    sector_label: str,
    catalog_ctx: DualCatalogContext,
) -> Optional[Dict[str, float]]:
    """
    Look up the bbox for a sector/neighborhood label from the geography catalog.
    Used by typed_token_dispatch for sector resolution.
    """
    norm = sector_label.lower().strip()

    # Direct key match
    for key, area in catalog_ctx.area_definitions.items():
        if not area.approx_bbox:
            continue
        if norm in key.lower():
            return area.approx_bbox
        if norm in (area.description or "").lower():
            return area.approx_bbox
        # Check sub_sectors
        for sub in area.sub_sectors:
            if norm == sub.lower() or norm in sub.lower():
                return area.approx_bbox

    return None


def get_corridor_waypoints_for_arterial(
    arterial_name: str,
    catalog_ctx: DualCatalogContext,
) -> List[Dict[str, Any]]:
    """
    Get corridor waypoints for a named arterial from the geography catalog.
    Used by arterial_waypoints for injection.
    """
    norm = arterial_name.lower().strip()

    for key, area in catalog_ctx.area_definitions.items():
        if not area.corridor_waypoints:
            continue
        if norm in key.lower() or norm in (area.description or "").lower():
            return list(area.corridor_waypoints)

    return []
