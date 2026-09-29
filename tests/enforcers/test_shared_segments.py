"""Tests for hades.enforcers.shared_segments."""
from __future__ import annotations

import math

from hades.enforcers.shared_segments import (
    DEFAULT_BEARING_TOLERANCE_DEG,
    DEFAULT_MIN_SEGMENT_LENGTH_M,
    DEFAULT_PROXIMITY_THRESHOLD_M,
    DEFAULT_SAMPLE_DISTANCE_M,
    PolylinePoint,
    SharedSegment,
    angular_diff_deg,
    compute_bearing_deg,
    find_shared_segments,
    is_point_in_segment,
    sample_polyline_uniform,
)


# At Quito's latitude one degree of latitude is ~111,132 m and one degree
# of longitude is ~111,320 * cos(lat) ≈ 111,283 m at lat = 0. Test polylines
# live near the equator so the longitude/latitude metre frames are nearly
# isotropic and easy to reason about.

M_PER_DEG_LAT = 111_132.0
LAT_REF = -0.200
M_PER_DEG_LON_AT_REF = 111_320.0 * math.cos(math.radians(LAT_REF))


def _lat_offset_m(meters: float) -> float:
    return meters / M_PER_DEG_LAT


def _lon_offset_m(meters: float) -> float:
    return meters / M_PER_DEG_LON_AT_REF


# Long east-west polyline (~11 km) for shared-corridor tests.
EW_LONG = [(-78.500, LAT_REF), (-78.400, LAT_REF)]


def test_perfect_overlap_returns_full_segment():
    # B is identical to A — every sample on A should be shared.
    a = list(EW_LONG)
    b = list(EW_LONG)
    segments = find_shared_segments(a, b)

    assert len(segments) == 1
    seg = segments[0]
    # Polyline length is ~11 km — segment should cover most of it.
    assert seg.length_m >= 10_000.0
    assert seg.start_cum_m < 100.0
    assert seg.sample_count >= 200  # ~11 km / 50 m


def test_no_overlap_returns_empty():
    a = list(EW_LONG)
    # B is parallel ~500 m to the north — well outside the 30 m proximity band.
    b = [
        (-78.500, LAT_REF + _lat_offset_m(500.0)),
        (-78.400, LAT_REF + _lat_offset_m(500.0)),
    ]
    segments = find_shared_segments(a, b)
    assert segments == []


def test_short_overlap_below_min_length_filtered():
    # B coincides with A only over a ~300 m stretch — below the 500 m floor.
    a = list(EW_LONG)
    overlap_start_lon = -78.450
    overlap_end_lon = overlap_start_lon + _lon_offset_m(300.0)
    b = [
        # Approach A from far north …
        (overlap_start_lon, LAT_REF + _lat_offset_m(1000.0)),
        # … drop onto A for ~300 m …
        (overlap_start_lon, LAT_REF),
        (overlap_end_lon, LAT_REF),
        # … then climb back north.
        (overlap_end_lon, LAT_REF + _lat_offset_m(1000.0)),
    ]
    segments = find_shared_segments(a, b)
    assert segments == []


def test_opposite_direction_filtered_by_bearing():
    # B traces the same ground as A but in the opposite direction.
    # Proximity is fine but the bearing differs by 180° → no shared segment.
    a = list(EW_LONG)
    b = list(reversed(EW_LONG))
    segments = find_shared_segments(a, b)
    assert segments == []


def test_multiple_disjoint_segments():
    # B coincides with A over two ~700 m stretches separated by a long gap.
    a = list(EW_LONG)
    seg1_lon_start = -78.490
    seg1_lon_end = seg1_lon_start + _lon_offset_m(700.0)
    seg2_lon_start = -78.430
    seg2_lon_end = seg2_lon_start + _lon_offset_m(700.0)
    far_north_lat = LAT_REF + _lat_offset_m(1000.0)
    b = [
        (seg1_lon_start, far_north_lat),
        (seg1_lon_start, LAT_REF),
        (seg1_lon_end, LAT_REF),
        (seg1_lon_end, far_north_lat),
        (seg2_lon_start, far_north_lat),
        (seg2_lon_start, LAT_REF),
        (seg2_lon_end, LAT_REF),
        (seg2_lon_end, far_north_lat),
    ]
    segments = find_shared_segments(a, b)
    assert len(segments) == 2
    for seg in segments:
        assert seg.length_m >= DEFAULT_MIN_SEGMENT_LENGTH_M
    # Segments are returned in order along A.
    assert segments[0].start_cum_m < segments[1].start_cum_m


def test_partial_overlap_at_polyline_start():
    # B coincides with the first ~3 km of A only.
    a = list(EW_LONG)
    overlap_end_lon = -78.500 + _lon_offset_m(3000.0)
    b = [
        (-78.500, LAT_REF),
        (overlap_end_lon, LAT_REF),
        (overlap_end_lon, LAT_REF + _lat_offset_m(2000.0)),
    ]
    segments = find_shared_segments(a, b)
    assert len(segments) == 1
    seg = segments[0]
    assert seg.start_cum_m < 100.0
    assert 2_500.0 <= seg.length_m <= 3_500.0


def test_partial_overlap_at_polyline_end():
    # B coincides with the last ~3 km of A only.
    a = list(EW_LONG)
    overlap_start_lon = -78.400 - _lon_offset_m(3000.0)
    b = [
        (overlap_start_lon, LAT_REF + _lat_offset_m(2000.0)),
        (overlap_start_lon, LAT_REF),
        (-78.400, LAT_REF),
    ]
    segments = find_shared_segments(a, b)
    assert len(segments) == 1
    seg = segments[0]
    # Polyline A is ~11 km long; the overlap should sit near the end.
    assert seg.end_cum_m >= 10_500.0
    assert 2_500.0 <= seg.length_m <= 3_500.0


def test_compute_bearing_known_directions():
    # East-going segment: bearing should be ~90°.
    east = [(-78.500, LAT_REF), (-78.400, LAT_REF)]
    assert abs(compute_bearing_deg(east, 0) - 90.0) < 1.0

    # North-going segment: bearing should be ~0°.
    north = [(-78.500, LAT_REF), (-78.500, LAT_REF + 0.1)]
    assert abs(compute_bearing_deg(north, 0)) < 1.0

    # Last-vertex idx falls back to the previous segment.
    assert abs(compute_bearing_deg(east, 1) - 90.0) < 1.0

    # Empty / single-point polylines return 0.0.
    assert compute_bearing_deg([], 0) == 0.0
    assert compute_bearing_deg([(-78.5, LAT_REF)], 0) == 0.0


def test_angular_diff_handles_wraparound():
    # 350° and 10° are 20° apart, not 340°.
    assert abs(angular_diff_deg(350.0, 10.0) - 20.0) < 1e-6
    assert abs(angular_diff_deg(10.0, 350.0) - 20.0) < 1e-6
    # Exact match.
    assert angular_diff_deg(45.0, 45.0) == 0.0
    # Maximum difference is 180°.
    assert abs(angular_diff_deg(0.0, 180.0) - 180.0) < 1e-6
    # Default tolerance band sanity check.
    assert angular_diff_deg(80.0, 100.0) <= DEFAULT_BEARING_TOLERANCE_DEG


def test_sample_polyline_returns_evenly_spaced():
    # 11 km polyline at 50 m spacing → ~221 samples (incl. start).
    samples = sample_polyline_uniform(EW_LONG, sample_distance_m=50.0)
    assert len(samples) >= 200
    # All samples should be PolylinePoint instances.
    assert all(isinstance(s, PolylinePoint) for s in samples)
    # First sample is at the polyline start.
    assert samples[0].cum_m == 0.0
    # Spacings should be exactly 50 m apart (within float tolerance).
    spacings = [samples[i].cum_m - samples[i - 1].cum_m for i in range(1, len(samples))]
    for s in spacings:
        assert abs(s - 50.0) < 1e-3
    # No sample exceeds the polyline length.
    assert samples[-1].cum_m <= 11_200.0


def test_is_point_in_segment_true_for_close():
    a = list(EW_LONG)
    seg = SharedSegment(
        start_idx_a=0,
        end_idx_a=0,
        start_cum_m=2_000.0,
        end_cum_m=4_000.0,
        length_m=2_000.0,
        sample_count=40,
    )
    # Pick a lon that sits at ~3 km arc-length on A.
    target_lon = -78.500 + _lon_offset_m(3_000.0)
    # Place the point 5 m off the polyline (well inside the 30 m band).
    pt_lat = LAT_REF + _lat_offset_m(5.0)
    assert is_point_in_segment(pt_lat, target_lon, a, seg) is True


def test_is_point_in_segment_false_for_far():
    a = list(EW_LONG)
    seg = SharedSegment(
        start_idx_a=0,
        end_idx_a=0,
        start_cum_m=2_000.0,
        end_cum_m=4_000.0,
        length_m=2_000.0,
        sample_count=40,
    )
    target_lon = -78.500 + _lon_offset_m(3_000.0)
    # 100 m off the polyline → outside the proximity band.
    pt_lat_far = LAT_REF + _lat_offset_m(100.0)
    assert is_point_in_segment(pt_lat_far, target_lon, a, seg) is False

    # Inside the proximity band but outside the segment's arc-length window.
    outside_lon = -78.500 + _lon_offset_m(6_000.0)
    pt_lat_close = LAT_REF + _lat_offset_m(5.0)
    assert is_point_in_segment(pt_lat_close, outside_lon, a, seg) is False
