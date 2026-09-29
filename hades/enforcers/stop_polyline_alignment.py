"""Stop-polyline alignment classifier (Pre-Ship Orphan Cleanup utility).

Classifies how aligned a stop is with a route polyline. Used by the
``orphan_cleanup`` service to decide whether to keep each stop, snap
it onto the polyline, or drop it as an orphan before a route is
shipped.

Read-only and stateless. Returns dataclasses describing each stop's
alignment; the caller decides what to do with the classification.

Design principle (v2): the polyline is canonical and immutable. Stops
adapt to the polyline, never the other way around. After cleanup,
every preserved stop is literally on the polyline — no floating
stops. Distances use the same equirectangular metre frame as the rest
of HADES geometry — see ``stop_coverage_enforcer.project_point_to_polyline``
and ``cumulative_m``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

from hades.enforcers.stop_coverage_enforcer import (
    cumulative_m,
    project_point_to_polyline,
)


AlignmentClass = Literal["aligned", "snap", "orphan"]


@dataclass
class StopAlignment:
    stop_id: str
    original_coord: tuple[float, float]
    distance_to_polyline_m: float
    alignment_class: AlignmentClass
    projected_coord: Optional[tuple[float, float]]
    nearest_polyline_index: Optional[int]


def _foot_on_polyline(
    target_cum_m: float,
    coords: Sequence[tuple[float, float]],
    cum: Sequence[float],
) -> tuple[tuple[float, float], int]:
    """Return ``((lat, lon), segment_start_index)`` at the given cumulative arc length."""
    n = len(coords)
    if n == 0:
        return ((0.0, 0.0), 0)
    if target_cum_m <= 0.0:
        lon, lat = coords[0]
        return ((lat, lon), 0)
    if target_cum_m >= cum[-1]:
        lon, lat = coords[-1]
        return ((lat, lon), n - 1)
    for i in range(1, n):
        if cum[i] >= target_cum_m:
            seg_m = cum[i] - cum[i - 1]
            if seg_m < 1e-9:
                lon, lat = coords[i]
                return ((lat, lon), i - 1)
            t = (target_cum_m - cum[i - 1]) / seg_m
            lon_a, lat_a = coords[i - 1]
            lon_b, lat_b = coords[i]
            return (
                (lat_a + t * (lat_b - lat_a), lon_a + t * (lon_b - lon_a)),
                i - 1,
            )
    lon, lat = coords[-1]
    return ((lat, lon), n - 1)


def classify_alignment(
    stop_lat: float,
    stop_lon: float,
    polyline: Sequence[tuple[float, float]],
    *,
    stop_id: str = "",
    cum: Optional[Sequence[float]] = None,
    aligned_threshold_m: float = 10.0,
    snap_threshold_m: float = 60.0,
) -> StopAlignment:
    """Classify how aligned a single stop is with the route polyline.

    Polyline coordinates follow the GeoJSON convention: each entry is
    ``(lon, lat)``. Pass a precomputed ``cum`` array when classifying
    many stops against the same polyline to avoid recomputing.

    Classification bands (v2 — 3 categories)::

        aligned   d <= aligned_threshold_m         keep original coord
        snap      aligned_threshold_m < d <= snap_threshold_m
                                                    project onto polyline foot
        orphan    d > snap_threshold_m             remove

    ``snap`` returns a ``projected_coord`` (the foot of the
    perpendicular onto the polyline) and a ``nearest_polyline_index``
    (segment-start vertex). ``aligned`` and ``orphan`` leave both as
    ``None``.
    """
    if not polyline:
        return StopAlignment(
            stop_id=stop_id,
            original_coord=(stop_lat, stop_lon),
            distance_to_polyline_m=float("inf"),
            alignment_class="orphan",
            projected_coord=None,
            nearest_polyline_index=None,
        )

    if cum is None:
        cum = cumulative_m(polyline)

    cum_m, dist_m = project_point_to_polyline(stop_lat, stop_lon, polyline, cum)

    if dist_m <= aligned_threshold_m:
        cls: AlignmentClass = "aligned"
    elif dist_m <= snap_threshold_m:
        cls = "snap"
    else:
        cls = "orphan"

    projected: Optional[tuple[float, float]] = None
    nearest_idx: Optional[int] = None
    if cls == "snap":
        projected, nearest_idx = _foot_on_polyline(cum_m, polyline, cum)

    return StopAlignment(
        stop_id=stop_id,
        original_coord=(stop_lat, stop_lon),
        distance_to_polyline_m=dist_m,
        alignment_class=cls,
        projected_coord=projected,
        nearest_polyline_index=nearest_idx,
    )


def analyze_route_alignment(
    stops: Sequence[dict],
    polyline: Sequence[tuple[float, float]],
    *,
    aligned_threshold_m: float = 10.0,
    snap_threshold_m: float = 60.0,
) -> list[StopAlignment]:
    """Classify alignment for every stop in a route.

    ``stops`` is a list of dicts with keys ``lat``, ``lon`` and an
    optional ``stop_id`` (matches the shape of ``approval_queue.proposed_stops``).
    ``polyline`` follows the GeoJSON convention: ``(lon, lat)`` per entry.
    """
    cum = cumulative_m(polyline) if polyline else None
    return [
        classify_alignment(
            float(s["lat"]),
            float(s["lon"]),
            polyline,
            stop_id=str(s.get("stop_id", "")),
            cum=cum,
            aligned_threshold_m=aligned_threshold_m,
            snap_threshold_m=snap_threshold_m,
        )
        for s in stops
    ]
