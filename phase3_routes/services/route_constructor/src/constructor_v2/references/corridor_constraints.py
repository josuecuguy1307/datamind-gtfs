"""Corridor constraint loader: matches route stops against confirmed corridor orderings.

Uses confirmed GTFS routes to inject hard ordering constraints into the CP-SAT solver.
When a route's stops overlap with a confirmed corridor, the confirmed ordering is enforced.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from src.constructor_v2.common import haversine_m as _haversine_m
from src.constructor_v2.schemas.route_input import NormalizedStop

logger = logging.getLogger(__name__)

CORRIDOR_MATCH_RADIUS_M = 50.0  # max distance to match a route stop to a corridor stop
REFERENCES_DIR = Path(__file__).parent


@dataclass(slots=True)
class CorridorMatch:
    """A stop in the route that matches a corridor stop."""
    route_stop_index: int
    route_stop_id: str
    corridor_stop_id: str
    corridor_stop_name: str
    corridor_position: int  # position in the corridor ordering (0-indexed)
    match_distance_m: float


@dataclass(slots=True)
class CorridorConstraintSet:
    """Ordering constraints derived from confirmed corridor matches."""
    corridor_name: str
    source_route: str
    matches: list[CorridorMatch]
    ordering_pairs: list[tuple[int, int]]  # (route_stop_index_A, route_stop_index_B) where A must come before B
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CorridorConstraintResult:
    """All corridor constraints for a route."""
    route_name: str
    constraint_sets: list[CorridorConstraintSet]
    total_constrained_stops: int
    total_ordering_pairs: int
    diagnostics: dict[str, Any] = field(default_factory=dict)


class CorridorConstraintLoader:
    def __init__(self, corridors_path: Path | None = None, routes_path: Path | None = None) -> None:
        self._corridors_path = corridors_path or REFERENCES_DIR / "confirmed_corridors.json"
        self._routes_path = routes_path or REFERENCES_DIR / "confirmed_routes.json"
        self._corridors: dict[str, Any] | None = None
        self._confirmed_routes: dict[str, Any] | None = None
        self._benchmark_mapping: dict[str, Any] | None = None

    def _load(self) -> None:
        if self._corridors is not None:
            return
        with open(self._corridors_path) as f:
            data = json.load(f)
        self._corridors = data.get("corridors", {})
        self._benchmark_mapping = data.get("benchmark_mapping", {})
        with open(self._routes_path) as f:
            rdata = json.load(f)
        self._confirmed_routes = {
            r["route_code"]: r for r in rdata.get("routes", []) if r["direction_id"] == 0
        }

    def get_constraints(
        self,
        route_name: str,
        stops: Sequence[NormalizedStop],
        *,
        match_radius_m: float = CORRIDOR_MATCH_RADIUS_M,
    ) -> CorridorConstraintResult:
        """Find corridor constraints applicable to this route."""
        self._load()

        mapping = self._benchmark_mapping.get(route_name)
        if not mapping:
            return CorridorConstraintResult(
                route_name=route_name,
                constraint_sets=[],
                total_constrained_stops=0,
                total_ordering_pairs=0,
                diagnostics={"reason": "no_corridor_mapping_for_route"},
            )

        corridor_names = mapping.get("corridors", [])
        constraint_sets: list[CorridorConstraintSet] = []
        all_constrained_indices: set[int] = set()
        total_pairs = 0

        for corridor_name in corridor_names:
            corridor = self._corridors.get(corridor_name)
            if not corridor:
                continue

            corridor_stops = corridor["ordered_stops"]
            matches = self._match_stops(stops, corridor_stops, match_radius_m)

            if len(matches) < 2:
                continue

            # Generate ordering pairs: if corridor says A before B, and both match route stops,
            # then route_stop_A must come before route_stop_B
            ordering_pairs: list[tuple[int, int]] = []
            for i in range(len(matches)):
                for j in range(i + 1, len(matches)):
                    mi, mj = matches[i], matches[j]
                    if mi.corridor_position < mj.corridor_position:
                        ordering_pairs.append((mi.route_stop_index, mj.route_stop_index))
                    elif mi.corridor_position > mj.corridor_position:
                        ordering_pairs.append((mj.route_stop_index, mi.route_stop_index))

            for m in matches:
                all_constrained_indices.add(m.route_stop_index)
            total_pairs += len(ordering_pairs)

            cs = CorridorConstraintSet(
                corridor_name=corridor_name,
                source_route=corridor.get("source_route", ""),
                matches=matches,
                ordering_pairs=ordering_pairs,
                diagnostics={
                    "corridor_stop_count": len(corridor_stops),
                    "matched_count": len(matches),
                    "pair_count": len(ordering_pairs),
                },
            )
            constraint_sets.append(cs)
            logger.info(
                "Corridor %s: %d matches, %d ordering pairs for route '%s'",
                corridor_name, len(matches), len(ordering_pairs), route_name,
            )

        return CorridorConstraintResult(
            route_name=route_name,
            constraint_sets=constraint_sets,
            total_constrained_stops=len(all_constrained_indices),
            total_ordering_pairs=total_pairs,
            diagnostics={
                "corridors_checked": corridor_names,
                "corridors_matched": [cs.corridor_name for cs in constraint_sets],
                "confirmed_route_code": mapping.get("confirmed_route"),
            },
        )

    def get_confirmed_sequence(self, route_name: str) -> list[dict[str, Any]] | None:
        """If a benchmark route has a direct confirmed GTFS match, return its stop sequence."""
        self._load()
        mapping = self._benchmark_mapping.get(route_name)
        if not mapping:
            return None
        code = mapping.get("confirmed_route")
        if not code:
            return None
        route = self._confirmed_routes.get(code)
        if not route:
            return None
        return route.get("ordered_stops")

    def get_confirmed_shape(self, route_name: str) -> list[list[float]] | None:
        """If a benchmark route has a direct confirmed GTFS match, return its shape coords."""
        self._load()
        mapping = self._benchmark_mapping.get(route_name)
        if not mapping:
            return None
        code = mapping.get("confirmed_route")
        if not code:
            return None
        route = self._confirmed_routes.get(code)
        if not route:
            return None
        return route.get("shape_coords")

    def _match_stops(
        self,
        route_stops: Sequence[NormalizedStop],
        corridor_stops: list[dict[str, Any]],
        match_radius_m: float,
    ) -> list[CorridorMatch]:
        """Match route stops to corridor stops by proximity."""
        matches: list[CorridorMatch] = []
        used_corridor_indices: set[int] = set()

        for route_idx, rs in enumerate(route_stops):
            best_dist = match_radius_m + 1
            best_corridor_idx = -1
            for c_idx, cs in enumerate(corridor_stops):
                if c_idx in used_corridor_indices:
                    continue
                d = _haversine_m(rs.lat, rs.lon, cs["lat"], cs["lon"])
                if d < best_dist:
                    best_dist = d
                    best_corridor_idx = c_idx
            if best_corridor_idx >= 0 and best_dist <= match_radius_m:
                cs = corridor_stops[best_corridor_idx]
                used_corridor_indices.add(best_corridor_idx)
                matches.append(CorridorMatch(
                    route_stop_index=route_idx,
                    route_stop_id=rs.stop_id,
                    corridor_stop_id=cs["stop_id"],
                    corridor_stop_name=cs["stop_name"],
                    corridor_position=best_corridor_idx,
                    match_distance_m=best_dist,
                ))

        # Sort by corridor position
        matches.sort(key=lambda m: m.corridor_position)
        return matches
