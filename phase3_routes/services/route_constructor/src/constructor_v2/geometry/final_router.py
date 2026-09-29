from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from src.constructor_v2.clients.valhalla_client import ValhallaClient
from src.constructor_v2.common import haversine_m
from src.constructor_v2.geometry.shape_exporter import export_linestring_geojson
from src.constructor_v2.schemas.route_input import NormalizedStop
from src.constructor_v2.schemas.route_output import OrderedLeg


@dataclass(slots=True)
class RoutedGeometry:
    geometry_geojson: dict[str, Any]
    geometry_coords: list[tuple[float, float]]
    legs: list[OrderedLeg]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # Full Valhalla request/response metadata — forward-only capture for
    # route_prod.routes.valhalla_request. Empty if the trace pre-dates capture.
    request_meta: dict[str, Any] = field(default_factory=dict)


class FinalRouter:
    def __init__(self, client: ValhallaClient) -> None:
        self.client = client

    def route(self, stops: Sequence[NormalizedStop]) -> RoutedGeometry:
        if len(stops) < 2:
            raise ValueError("Final router needs at least two stops")
        result = self.client.route([(stop.lon, stop.lat) for stop in stops])
        legs: list[OrderedLeg] = []
        for index, (left, right) in enumerate(zip(stops, stops[1:])):
            leg_payload = result.legs[index] if index < len(result.legs) else {}
            road_distance_m = float(leg_payload.get("distance_m", 0.0))
            road_duration_s = float(leg_payload.get("duration_s", 0.0))
            straight_distance_m = max(1.0, haversine_m(left.lat, left.lon, right.lat, right.lon))
            legs.append(
                OrderedLeg(
                    from_stop_id=left.stop_id,
                    to_stop_id=right.stop_id,
                    road_distance_m=road_distance_m,
                    road_duration_s=road_duration_s,
                    straight_distance_m=straight_distance_m,
                    detour_ratio=road_distance_m / straight_distance_m if road_distance_m else 0.0,
                    geometry_geojson=export_linestring_geojson(leg_payload.get("shape") or []),
                    metadata={"leg_index": index},
                )
            )
        return RoutedGeometry(
            geometry_geojson=export_linestring_geojson(result.coordinates),
            geometry_coords=result.coordinates,
            legs=legs,
            diagnostics={
                **result.diagnostics,
                "distance_m": result.distance_m,
                "duration_s": result.duration_s,
                "maneuver_count": len(result.maneuvers),
            },
            request_meta=dict(result.request_meta or {}),
        )
