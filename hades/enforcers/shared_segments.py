"""Shared-segments algorithm — geometric corridor detection between two polylines.

Used by ``stop_refill`` (GREEK pipeline phase γ) to identify stretches of
road where two routes geographically overlap *and* travel in compatible
directions. A pair of routes can have low total overlap yet share a long
corridor that matters for refill: this algorithm surfaces those corridors
explicitly instead of relying on a coarse "overlap percentage" metric.

Read-only and stateless. Returns dataclasses describing each shared
segment along route A; the caller decides what to do with them.

The algorithm
-------------

1. Discretize polyline A into uniformly spaced sample points (default 50 m).
2. For each sample, find the nearest point on polyline B; if the perpendicular
   distance is within ``proximity_threshold_m`` (30 m) AND the local bearings
   on A and B are within ``bearing_tolerance_deg`` (±30°), the sample is
   "shared".
3. Group consecutive shared samples into runs. Drop runs shorter than
   ``min_segment_length_m`` (500 m) — those are crossings or noise.

Distances and projections reuse :func:`stop_coverage_enforcer.cumulative_m`
and :func:`stop_coverage_enforcer.project_point_to_polyline` so the metre
frame is consistent with the rest of HADES geometry. Bearings reuse
:func:`geometry_enforcer.bearing_deg` and :func:`geometry_enforcer.angular_delta_deg`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from hades.geometry.canonical import (
    CORRIDOR_AGREE_DEG,
    angular_delta_deg,
    bearing_deg,
)
from hades.enforcers.stop_coverage_enforcer import (
    cumulative_m,
    project_point_to_polyline,
)


DEFAULT_SAMPLE_DISTANCE_M = 50.0
DEFAULT_PROXIMITY_THRESHOLD_M = 30.0
DEFAULT_BEARING_TOLERANCE_DEG = CORRIDOR_AGREE_DEG
DEFAULT_MIN_SEGMENT_LENGTH_M = 500.0


@dataclass(frozen=True)
class PolylinePoint:
    """A uniformly-sampled point on polyline A.

    ``cum_m`` is the arc-length on A. ``segment_idx`` is the index of the
    polyline segment that contains the sample (i.e. the sample lies between
    ``coords[segment_idx]`` and ``coords[segment_idx + 1]``).
    """
    lat: float
    lon: float
    cum_m: float
    segment_idx: int


@dataclass(frozen=True)
class SharedSegment:
    """A run of consecutive shared samples along polyline A.

    ``start_cum_m`` and ``end_cum_m`` are arc-lengths on A; ``length_m`` is
    ``end_cum_m - start_cum_m``. ``start_idx_a`` and ``end_idx_a`` are the
    polyline-segment indices on A that bracket the run.
    """
    start_idx_a: int
    end_idx_a: int
    start_cum_m: float
    end_cum_m: float
    length_m: float
    sample_count: int


def compute_bearing_deg(
    coords: Sequence[tuple[float, float]],
    idx: int,
) -> float:
    """Compass bearing of the polyline segment starting at ``coords[idx]``.

    Coordinates follow the GeoJSON convention: each entry is ``(lon, lat)``.
    For the last vertex (``idx == len(coords) - 1``) the bearing of the
    *previous* segment is returned, so callers do not have to special-case
    polyline endpoints.

    Returns 0.0 for empty or single-point polylines.
    """
    n = len(coords)
    if n < 2:
        return 0.0
    if idx >= n - 1:
        idx = n - 2
    if idx < 0:
        idx = 0
    lon_a, lat_a = coords[idx]
    lon_b, lat_b = coords[idx + 1]
    return bearing_deg(lat_a, lon_a, lat_b, lon_b)


def angular_diff_deg(a_deg: float, b_deg: float) -> float:
    """Smallest absolute angle between two compass bearings, in [0, 180].

    Thin wrapper over :func:`geometry_enforcer.angular_delta_deg` exposed
    here so callers of this module have a single import site.
    """
    return angular_delta_deg(a_deg, b_deg)


def sample_polyline_uniform(
    coords: Sequence[tuple[float, float]],
    sample_distance_m: float = DEFAULT_SAMPLE_DISTANCE_M,
    *,
    cum: Optional[Sequence[float]] = None,
) -> list[PolylinePoint]:
    """Discretize a polyline into evenly spaced sample points.

    Coordinates follow the GeoJSON convention: each entry is ``(lon, lat)``.
    The first vertex is always emitted as a sample; subsequent samples land
    every ``sample_distance_m`` of arc length until the polyline ends. The
    final sample is the closest one ≤ ``cum[-1]`` (the polyline end).

    Pass a precomputed ``cum`` array when sampling the same polyline
    repeatedly to avoid redundant work.
    """
    n = len(coords)
    if n == 0:
        return []
    if cum is None:
        cum = cumulative_m(coords)
    total_m = cum[-1] if cum else 0.0
    if total_m <= 0.0 or sample_distance_m <= 0.0:
        lon0, lat0 = coords[0]
        return [PolylinePoint(lat=lat0, lon=lon0, cum_m=0.0, segment_idx=0)]

    samples: list[PolylinePoint] = []
    target = 0.0
    seg_idx = 0
    while target <= total_m + 1e-6:
        while seg_idx < n - 1 and cum[seg_idx + 1] < target:
            seg_idx += 1
        seg_idx_eff = min(seg_idx, n - 2)
        seg_start = cum[seg_idx_eff]
        seg_end = cum[seg_idx_eff + 1]
        seg_len = seg_end - seg_start
        if seg_len < 1e-9:
            t = 0.0
        else:
            t = (target - seg_start) / seg_len
            t = max(0.0, min(1.0, t))
        lon_a, lat_a = coords[seg_idx_eff]
        lon_b, lat_b = coords[seg_idx_eff + 1]
        lat = lat_a + t * (lat_b - lat_a)
        lon = lon_a + t * (lon_b - lon_a)
        samples.append(
            PolylinePoint(lat=lat, lon=lon, cum_m=target, segment_idx=seg_idx_eff)
        )
        target += sample_distance_m
    return samples


def is_point_in_segment(
    point_lat: float,
    point_lon: float,
    coords: Sequence[tuple[float, float]],
    segment: SharedSegment,
    *,
    cum: Optional[Sequence[float]] = None,
    proximity_threshold_m: float = DEFAULT_PROXIMITY_THRESHOLD_M,
) -> bool:
    """Test whether a (lat, lon) point falls inside a shared segment of A.

    The point must project onto A within ``proximity_threshold_m`` AND its
    projected arc-length must lie inside ``[start_cum_m, end_cum_m]``. Used
    by :mod:`stop_refill` to test whether a candidate stop sits in a shared
    corridor.
    """
    if not coords:
        return False
    if cum is None:
        cum = cumulative_m(coords)
    cum_m, dist_m = project_point_to_polyline(point_lat, point_lon, coords, cum)
    if dist_m > proximity_threshold_m:
        return False
    return segment.start_cum_m - 1e-6 <= cum_m <= segment.end_cum_m + 1e-6


def find_shared_segments(
    polyline_a: Sequence[tuple[float, float]],
    polyline_b: Sequence[tuple[float, float]],
    *,
    sample_distance_m: float = DEFAULT_SAMPLE_DISTANCE_M,
    proximity_threshold_m: float = DEFAULT_PROXIMITY_THRESHOLD_M,
    bearing_tolerance_deg: float = DEFAULT_BEARING_TOLERANCE_DEG,
    min_segment_length_m: float = DEFAULT_MIN_SEGMENT_LENGTH_M,
) -> list[SharedSegment]:
    """Find runs along A where polyline B is geographically nearby and parallel.

    Both polylines follow the GeoJSON ``(lon, lat)`` convention. Returns a
    list of :class:`SharedSegment`, each describing a corridor along A
    longer than ``min_segment_length_m``.

    A sample point on A is considered "shared" with B when:

    * The perpendicular distance from the sample to B is ≤ ``proximity_threshold_m``.
    * The local bearings on A and B differ by ≤ ``bearing_tolerance_deg``
      (handles wrap-around at 0°/360°). Same compass heading → directionally
      compatible; opposite heading → filtered out.

    Consecutive shared samples form a run. Runs shorter than
    ``min_segment_length_m`` are dropped (those are crossings or coincidental
    blips, not corridors worth surfacing as refill candidates).
    """
    if len(polyline_a) < 2 or len(polyline_b) < 2:
        return []

    cum_a = cumulative_m(polyline_a)
    cum_b = cumulative_m(polyline_b)
    samples_a = sample_polyline_uniform(
        polyline_a, sample_distance_m=sample_distance_m, cum=cum_a
    )
    if not samples_a:
        return []

    runs: list[SharedSegment] = []
    run_start: Optional[PolylinePoint] = None
    run_end: Optional[PolylinePoint] = None
    run_count = 0

    for sample in samples_a:
        cum_b_m, dist_b = project_point_to_polyline(
            sample.lat, sample.lon, polyline_b, cum_b
        )
        is_shared = False
        if dist_b <= proximity_threshold_m:
            bearing_a = compute_bearing_deg(polyline_a, sample.segment_idx)
            b_seg_idx = _segment_index_at_cum(cum_b, cum_b_m)
            bearing_b = compute_bearing_deg(polyline_b, b_seg_idx)
            if angular_diff_deg(bearing_a, bearing_b) <= bearing_tolerance_deg:
                is_shared = True

        if is_shared:
            if run_start is None:
                run_start = sample
            run_end = sample
            run_count += 1
        else:
            if run_start is not None and run_end is not None:
                run = _finalize_run(run_start, run_end, run_count, min_segment_length_m)
                if run is not None:
                    runs.append(run)
            run_start = None
            run_end = None
            run_count = 0

    if run_start is not None and run_end is not None:
        run = _finalize_run(run_start, run_end, run_count, min_segment_length_m)
        if run is not None:
            runs.append(run)

    return runs


def _segment_index_at_cum(cum: Sequence[float], cum_m: float) -> int:
    """Index ``i`` such that ``cum[i] <= cum_m <= cum[i+1]`` (clamped)."""
    n = len(cum)
    if n < 2:
        return 0
    if cum_m <= cum[0]:
        return 0
    if cum_m >= cum[-1]:
        return n - 2
    lo, hi = 0, n - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if cum[mid] <= cum_m:
            lo = mid
        else:
            hi = mid
    return lo


def _finalize_run(
    start: PolylinePoint,
    end: PolylinePoint,
    sample_count: int,
    min_segment_length_m: float,
) -> Optional[SharedSegment]:
    length = end.cum_m - start.cum_m
    if length < min_segment_length_m:
        return None
    return SharedSegment(
        start_idx_a=start.segment_idx,
        end_idx_a=end.segment_idx,
        start_cum_m=start.cum_m,
        end_cum_m=end.cum_m,
        length_m=length,
        sample_count=sample_count,
    )
