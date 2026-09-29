"""Tests for hades.enforcers.stop_polyline_alignment (v2 — 3-category)."""
from __future__ import annotations

from hades.enforcers.stop_polyline_alignment import (
    analyze_route_alignment,
    classify_alignment,
)


# East-west polyline at lat = -0.200 in the Quito metro area, ~11 km long.
# Coords are (lon, lat) per GeoJSON convention.
POLYLINE = [(-78.500, -0.200), (-78.400, -0.200)]

# At this latitude one degree latitude is ~111,132 m.
M_PER_DEG_LAT = 111_132.0


def _lat_offset_m(meters: float) -> float:
    """Return the latitude delta (in degrees) corresponding to ``meters``."""
    return meters / M_PER_DEG_LAT


def test_stop_exactly_on_polyline_classifies_as_aligned():
    result = classify_alignment(-0.200, -78.450, POLYLINE)
    assert result.alignment_class == "aligned"
    assert result.distance_to_polyline_m < 1.0
    assert result.projected_coord is None
    assert result.nearest_polyline_index is None


def test_stop_20m_from_polyline_classifies_as_snap():
    stop_lat = -0.200 + _lat_offset_m(20.0)
    result = classify_alignment(stop_lat, -78.450, POLYLINE)
    assert result.alignment_class == "snap"
    assert 18.0 <= result.distance_to_polyline_m <= 22.0
    assert result.projected_coord is not None

    proj_lat, proj_lon = result.projected_coord
    assert abs(proj_lat - (-0.200)) < 1e-6
    assert abs(proj_lon - (-78.450)) < 1e-3
    assert result.nearest_polyline_index is not None


def test_stop_40m_from_polyline_classifies_as_snap():
    stop_lat = -0.200 + _lat_offset_m(40.0)
    result = classify_alignment(stop_lat, -78.450, POLYLINE, stop_id="s1")

    assert result.alignment_class == "snap"
    assert 38.0 <= result.distance_to_polyline_m <= 42.0
    assert result.projected_coord is not None

    proj_lat, proj_lon = result.projected_coord
    assert abs(proj_lat - (-0.200)) < 1e-6
    assert abs(proj_lon - (-78.450)) < 1e-3
    assert result.nearest_polyline_index is not None


def test_stop_100m_from_polyline_classifies_as_orphan():
    stop_lat = -0.200 + _lat_offset_m(100.0)
    result = classify_alignment(stop_lat, -78.450, POLYLINE)
    assert result.alignment_class == "orphan"
    assert 95.0 <= result.distance_to_polyline_m <= 105.0
    assert result.projected_coord is None


def test_empty_polyline_returns_orphan_with_inf_distance():
    result = classify_alignment(-0.200, -78.450, [], stop_id="orphan_x")
    assert result.alignment_class == "orphan"
    assert result.distance_to_polyline_m == float("inf")
    assert result.projected_coord is None
    assert result.nearest_polyline_index is None
    assert result.stop_id == "orphan_x"


def test_stop_at_polyline_endpoint_classifies_as_aligned():
    result = classify_alignment(-0.200, -78.500, POLYLINE)
    assert result.alignment_class == "aligned"
    assert result.distance_to_polyline_m < 1.0


def test_boundary_at_aligned_threshold_is_inclusive():
    # 10.0 m exactly should be aligned (band uses ``<= aligned_threshold_m``).
    stop_lat = -0.200 + _lat_offset_m(10.0)
    result = classify_alignment(stop_lat, -78.450, POLYLINE)
    assert result.alignment_class == "aligned"
    assert result.projected_coord is None


def test_boundary_at_snap_threshold_is_inclusive():
    # 60.0 m exactly should be snap (band uses ``<= snap_threshold_m``).
    stop_lat = -0.200 + _lat_offset_m(60.0)
    result = classify_alignment(stop_lat, -78.450, POLYLINE)
    assert result.alignment_class == "snap"
    assert result.projected_coord is not None


def test_just_above_snap_threshold_is_orphan():
    # A hair above 60 m crosses into orphan territory.
    stop_lat = -0.200 + _lat_offset_m(60.5)
    result = classify_alignment(stop_lat, -78.450, POLYLINE)
    assert result.alignment_class == "orphan"
    assert result.projected_coord is None


def test_analyze_route_alignment_multi_stop_mixed():
    stops = [
        {"stop_id": "a", "lat": -0.200, "lon": -78.450},
        {"stop_id": "b", "lat": -0.200 + _lat_offset_m(20.0), "lon": -78.430},
        {"stop_id": "c", "lat": -0.200 + _lat_offset_m(40.0), "lon": -78.420},
        {"stop_id": "d", "lat": -0.200 + _lat_offset_m(100.0), "lon": -78.410},
    ]

    results = analyze_route_alignment(stops, POLYLINE)

    assert [r.alignment_class for r in results] == [
        "aligned",
        "snap",
        "snap",
        "orphan",
    ]
    assert [r.stop_id for r in results] == ["a", "b", "c", "d"]
    assert results[0].projected_coord is None
    assert results[1].projected_coord is not None
    assert results[2].projected_coord is not None
    assert results[3].projected_coord is None
