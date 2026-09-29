from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from src.constructor_v2.common import normalize_name


@dataclass(frozen=True, slots=True)
class RouteStopInput:
    seq: int
    stop_id: str
    stop_name: str
    lat: float
    lon: float
    path_fraction: float | None = None
    on_route_score: float | None = None
    is_known_anchor: bool = False
    is_known_intermediate: bool = False
    stop_source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def normalized_name(self) -> str:
        return normalize_name(self.stop_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "stop_id": self.stop_id,
            "stop_name": self.stop_name,
            "lat": self.lat,
            "lon": self.lon,
            "path_fraction": self.path_fraction,
            "on_route_score": self.on_route_score,
            "is_known_anchor": self.is_known_anchor,
            "is_known_intermediate": self.is_known_intermediate,
            "stop_source": self.stop_source,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RouteInput:
    route: str
    cooperative: str
    route_type: str
    corridor_km: float | None
    stops: tuple[RouteStopInput, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def start_stop(self) -> RouteStopInput:
        return self.stops[0]

    @property
    def end_stop(self) -> RouteStopInput:
        return self.stops[-1]

    @property
    def intermediate_stops(self) -> tuple[RouteStopInput, ...]:
        return self.stops[1:-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "cooperative": self.cooperative,
            "route_type": self.route_type,
            "corridor_km": self.corridor_km,
            "ordered_stops": [stop.to_dict() for stop in self.stops],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_artifact_route(cls, route_row: Mapping[str, Any]) -> "RouteInput":
        raw_stops = route_row.get("ordered_stops") or route_row.get("stops") or []
        if len(raw_stops) < 2:
            raise ValueError(f"Route '{route_row.get('route')}' needs at least two stops")

        stops = tuple(
            RouteStopInput(
                seq=int(raw_stop.get("seq") or (idx + 1)),
                stop_id=str(raw_stop["stop_id"]),
                stop_name=str(raw_stop.get("stop_name") or raw_stop.get("name") or raw_stop["stop_id"]),
                lat=float(raw_stop["lat"]),
                lon=float(raw_stop["lon"]),
                path_fraction=float(raw_stop["path_fraction"]) if raw_stop.get("path_fraction") is not None else None,
                on_route_score=float(raw_stop["on_route_score"]) if raw_stop.get("on_route_score") is not None else None,
                is_known_anchor=bool(raw_stop.get("is_known_anchor")),
                is_known_intermediate=bool(raw_stop.get("is_known_intermediate")),
                stop_source=str(raw_stop.get("stop_source") or ""),
                metadata={
                    key: value
                    for key, value in raw_stop.items()
                    if key
                    not in {
                        "seq",
                        "stop_id",
                        "stop_name",
                        "name",
                        "lat",
                        "lon",
                        "path_fraction",
                        "on_route_score",
                        "is_known_anchor",
                        "is_known_intermediate",
                        "stop_source",
                    }
                },
            )
            for idx, raw_stop in enumerate(raw_stops)
        )
        metadata = {
            key: value
            for key, value in route_row.items()
            if key
            not in {
                "route",
                "cooperative",
                "route_type",
                "corridor_km",
                "ordered_stops",
                "stops",
            }
        }
        return cls(
            route=str(route_row["route"]),
            cooperative=str(route_row.get("cooperative") or ""),
            route_type=str(route_row.get("route_type") or "lineal"),
            corridor_km=float(route_row["corridor_km"]) if route_row.get("corridor_km") is not None else None,
            stops=stops,
            metadata=metadata,
        )

    @classmethod
    def from_stop_rows(
        cls,
        *,
        route: str,
        cooperative: str,
        route_type: str,
        stop_rows: Iterable[Mapping[str, Any]],
        corridor_km: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "RouteInput":
        return cls.from_artifact_route(
            {
                "route": route,
                "cooperative": cooperative,
                "route_type": route_type,
                "corridor_km": corridor_km,
                "ordered_stops": list(stop_rows),
                **dict(metadata or {}),
            }
        )


@dataclass(slots=True)
class NormalizedStop:
    order_hint: int
    stop_id: str
    stop_name: str
    lat: float
    lon: float
    source_stop_ids: tuple[str, ...]
    source_indices: tuple[int, ...]
    source_names: tuple[str, ...]
    representative_score: float
    weak_candidate: bool
    optional_penalty: int | None
    duplicate_group_id: str | None = None
    is_fixed_start: bool = False
    is_fixed_end: bool = False
    is_known_anchor: bool = False
    is_known_intermediate: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def normalized_name(self) -> str:
        return normalize_name(self.stop_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_hint": self.order_hint,
            "stop_id": self.stop_id,
            "stop_name": self.stop_name,
            "lat": self.lat,
            "lon": self.lon,
            "source_stop_ids": list(self.source_stop_ids),
            "source_indices": list(self.source_indices),
            "source_names": list(self.source_names),
            "representative_score": self.representative_score,
            "weak_candidate": self.weak_candidate,
            "optional_penalty": self.optional_penalty,
            "duplicate_group_id": self.duplicate_group_id,
            "is_fixed_start": self.is_fixed_start,
            "is_fixed_end": self.is_fixed_end,
            "is_known_anchor": self.is_known_anchor,
            "is_known_intermediate": self.is_known_intermediate,
            "metadata": dict(self.metadata),
        }
