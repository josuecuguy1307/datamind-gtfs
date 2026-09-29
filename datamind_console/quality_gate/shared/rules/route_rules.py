"""Route quality rules — gaps C6, C8, geometry validation.

Each rule is a pure function: (Route) -> list[EntityIssue]. No DB writes.
"""
from __future__ import annotations

import re
from typing import Callable, List

from ..config import (
    MIN_STOPS_PER_ROUTE,
    ROUTE_GEOM_MIN_LENGTH_KM,
    ROUTE_GEOM_MIN_POINTS_PER_KM,
    ROUTE_GEOM_MIN_SINUOSITY,
    ROUTE_GEOM_STRAIGHT_LINE_RATIO,
    haversine_m,
)
from ..models import EntityIssue, Route, RouteSemantics, Severity


def rule_route_too_few_stops(route: Route) -> List[EntityIssue]:
    """C6: Route with fewer than 3 stops.

    phase_origin: 3 (Phase 3 stop grounding)
    rule_name: route_too_few_stops
    """
    n = len(route.stop_node_ids)
    if n < MIN_STOPS_PER_ROUTE:
        return [EntityIssue(
            entity_type="route", entity_id=route.route_id,
            rule_name="route_too_few_stops", severity=Severity.ERROR,
            description=f"Route has only {n} stops (minimum {MIN_STOPS_PER_ROUTE})",
            original_value=n, phase_origin=3,
        )]
    return []


def rule_route_no_schedule(route: Route) -> List[EntityIssue]:
    """C8 (additional): Route exists in route_prod but has no schedule profile.

    This is detected via the schedules list — routes without a matching schedule
    entry are flagged. Caller must pass route.extra["has_schedule"] = bool.

    phase_origin: 4 (Phase 4 catalog)
    rule_name: route_no_schedule
    """
    if not route.extra.get("has_schedule", True):
        return [EntityIssue(
            entity_type="route", entity_id=route.route_id,
            rule_name="route_no_schedule", severity=Severity.WARNING,
            description="Route has no schedule profile in catalog",
            original_value=None, phase_origin=4,
        )]
    return []


def _parse_linestring_wkt(wkt: str) -> list[tuple[float, float]]:
    """Parse WKT LINESTRING into [(lon, lat), ...]. Returns [] on failure."""
    if not wkt:
        return []
    m = re.search(r'LINESTRING\s*\((.+)\)', wkt, re.IGNORECASE)
    if not m:
        return []
    coords = []
    for pair in m.group(1).split(','):
        parts = pair.strip().split()
        if len(parts) >= 2:
            try:
                coords.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    return coords


def _compute_geometry_stats(coords: list[tuple[float, float]]) -> dict:
    """Compute path_length_km, direct_km, sinuosity, points_per_km."""
    if len(coords) < 2:
        return {}
    path_m = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        path_m += haversine_m(lat1, lon1, lat2, lon2)
    path_km = path_m / 1000.0
    first, last = coords[0], coords[-1]
    direct_m = haversine_m(first[1], first[0], last[1], last[0])
    direct_km = direct_m / 1000.0
    sinuosity = path_km / direct_km if direct_km > 0.01 else 999.0
    points_per_km = len(coords) / path_km if path_km > 0.01 else 999.0
    straight_ratio = direct_km / path_km if path_km > 0.01 else 1.0
    return {
        "path_km": round(path_km, 3),
        "direct_km": round(direct_km, 3),
        "sinuosity": round(sinuosity, 4),
        "points_per_km": round(points_per_km, 2),
        "straight_ratio": round(straight_ratio, 4),
        "n_points": len(coords),
    }


def rule_route_geometry_quality(route: Route) -> List[EntityIssue]:
    """Detect straight-line, low-detail, or low-sinuosity route geometries.

    Sub-rules:
      - route_geometry_straight_line: direct/path ratio > 0.98
      - route_geometry_low_detail: < 3 points per km
      - route_geometry_low_sinuosity: sinuosity < 1.05

    phase_origin: 3 (Valhalla routing)
    """
    coords = _parse_linestring_wkt(route.geom_wkt)
    if len(coords) < 2:
        return []

    stats = _compute_geometry_stats(coords)
    if not stats or stats["path_km"] < ROUTE_GEOM_MIN_LENGTH_KM:
        return []

    issues: List[EntityIssue] = []

    if stats["straight_ratio"] > ROUTE_GEOM_STRAIGHT_LINE_RATIO:
        issues.append(EntityIssue(
            entity_type="route", entity_id=route.route_id,
            rule_name="route_geometry_straight_line",
            severity=Severity.ERROR,
            description=(
                f"Route geometry is a near-straight line "
                f"(direct/path={stats['straight_ratio']:.3f}, "
                f"{stats['n_points']} points over {stats['path_km']:.1f}km)"
            ),
            original_value=stats,
            phase_origin=3,
        ))

    if stats["points_per_km"] < ROUTE_GEOM_MIN_POINTS_PER_KM and stats["n_points"] > 2:
        issues.append(EntityIssue(
            entity_type="route", entity_id=route.route_id,
            rule_name="route_geometry_low_detail",
            severity=Severity.WARNING,
            description=(
                f"Route geometry has only {stats['points_per_km']:.1f} points/km "
                f"({stats['n_points']} points over {stats['path_km']:.1f}km)"
            ),
            original_value=stats,
            phase_origin=3,
        ))

    if stats["sinuosity"] < ROUTE_GEOM_MIN_SINUOSITY and stats["straight_ratio"] <= ROUTE_GEOM_STRAIGHT_LINE_RATIO:
        issues.append(EntityIssue(
            entity_type="route", entity_id=route.route_id,
            rule_name="route_geometry_low_sinuosity",
            severity=Severity.WARNING,
            description=(
                f"Route sinuosity too low ({stats['sinuosity']:.3f}), "
                f"expected >= {ROUTE_GEOM_MIN_SINUOSITY} for a bus route"
            ),
            original_value=stats,
            phase_origin=3,
        ))

    return issues


RULES: List[Callable[[Route], List[EntityIssue]]] = [
    rule_route_too_few_stops,
    rule_route_no_schedule,
    rule_route_geometry_quality,
]
