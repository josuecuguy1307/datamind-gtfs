from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.constructor_v2.common import haversine_m, stable_hash
from src.constructor_v2.constants import (
    DUPLICATE_DISTANCE_M,
    VERY_CLOSE_DISTANCE_M,
    WEAK_STOP_DEFAULT_PENALTY,
    WEAK_STOP_SCORE_THRESHOLD,
)
from src.constructor_v2.schemas.route_input import NormalizedStop, RouteInput, RouteStopInput


@dataclass(slots=True)
class NormalizationResult:
    route_input: RouteInput
    normalized_stops: list[NormalizedStop]
    diagnostics: dict[str, Any]


def _is_same_stop(left: RouteStopInput, right: RouteStopInput, *, route_type: str) -> bool:
    if "loop" in route_type and {left.seq, right.seq} == {1, max(left.seq, right.seq)}:
        return False
    if left.stop_id == right.stop_id:
        return True
    distance_m = haversine_m(left.lat, left.lon, right.lat, right.lon)
    if left.normalized_name and left.normalized_name == right.normalized_name:
        return distance_m <= DUPLICATE_DISTANCE_M
    return distance_m <= VERY_CLOSE_DISTANCE_M and left.normalized_name == right.normalized_name


def _choose_representative(stops: list[RouteStopInput]) -> RouteStopInput:
    return max(
        stops,
        key=lambda stop: (
            int(stop.is_known_anchor),
            int(stop.is_known_intermediate),
            float(stop.on_route_score or 0.0),
            -int(stop.seq),
        ),
    )


def normalize_route_input(route_input: RouteInput) -> NormalizationResult:
    clusters: list[list[RouteStopInput]] = []
    boundary_stop_ids = {route_input.start_stop.stop_id, route_input.end_stop.stop_id}

    for stop in route_input.stops:
        assigned = False
        for cluster in clusters:
            if _is_same_stop(cluster[0], stop, route_type=route_input.route_type):
                cluster.append(stop)
                assigned = True
                break
        if not assigned:
            clusters.append([stop])

    normalized_stops: list[NormalizedStop] = []
    collapsed_groups: list[dict[str, Any]] = []

    for cluster in clusters:
        representative = _choose_representative(cluster)
        source_indices = tuple(sorted(stop.seq for stop in cluster))
        source_stop_ids = tuple(stop.stop_id for stop in cluster)
        weak_candidate = (
            not representative.is_known_anchor
            and not representative.is_known_intermediate
            and float(representative.on_route_score or 0.0) < WEAK_STOP_SCORE_THRESHOLD
        )
        optional_penalty = WEAK_STOP_DEFAULT_PENALTY if weak_candidate else None
        duplicate_group_id = None
        if len(cluster) > 1:
            duplicate_group_id = stable_hash(
                [route_input.route, representative.stop_name, representative.lat, representative.lon, source_stop_ids]
            )[:12]
            collapsed_groups.append(
                {
                    "duplicate_group_id": duplicate_group_id,
                    "representative_stop_id": representative.stop_id,
                    "source_stop_ids": list(source_stop_ids),
                    "source_names": [stop.stop_name for stop in cluster],
                    "source_indices": list(source_indices),
                }
            )

        normalized_stops.append(
            NormalizedStop(
                order_hint=min(source_indices),
                stop_id=representative.stop_id,
                stop_name=representative.stop_name,
                lat=representative.lat,
                lon=representative.lon,
                source_stop_ids=source_stop_ids,
                source_indices=source_indices,
                source_names=tuple(stop.stop_name for stop in cluster),
                representative_score=float(representative.on_route_score or 0.0),
                weak_candidate=weak_candidate,
                optional_penalty=optional_penalty,
                duplicate_group_id=duplicate_group_id,
                is_fixed_start=representative.stop_id == route_input.start_stop.stop_id and representative.seq == route_input.start_stop.seq,
                is_fixed_end=representative.stop_id == route_input.end_stop.stop_id and representative.seq == route_input.end_stop.seq,
                is_known_anchor=representative.is_known_anchor or representative.stop_id in boundary_stop_ids,
                is_known_intermediate=representative.is_known_intermediate,
                metadata={
                    "stop_source": representative.stop_source,
                    "cluster_size": len(cluster),
                    "path_fraction": representative.path_fraction,
                    "route_name": route_input.route,
                    "cooperative": route_input.cooperative,
                    "route_type": route_input.route_type,
                },
            )
        )

    normalized_stops.sort(key=lambda stop: (stop.order_hint, stop.stop_name))
    if normalized_stops:
        normalized_stops[0].is_fixed_start = True
        normalized_stops[-1].is_fixed_end = True
        normalized_stops[0].is_known_anchor = True
        normalized_stops[-1].is_known_anchor = True

    diagnostics = {
        "original_stop_count": len(route_input.stops),
        "normalized_stop_count": len(normalized_stops),
        "collapsed_duplicate_groups": collapsed_groups,
        "weak_stop_ids": [stop.stop_id for stop in normalized_stops if stop.weak_candidate],
    }
    return NormalizationResult(route_input=route_input, normalized_stops=normalized_stops, diagnostics=diagnostics)
