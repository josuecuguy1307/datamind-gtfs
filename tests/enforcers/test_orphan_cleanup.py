"""Tests for hades.enforcers.orphan_cleanup (v2 — 3-category design)."""
from __future__ import annotations

from hades.enforcers.orphan_cleanup import (
    cleanup_orphan_stops,
    validate_cleanup_not_destructive,
)


# Same east-west test polyline used in test_stop_polyline_alignment.
POLYLINE = [(-78.500, -0.200), (-78.400, -0.200)]
M_PER_DEG_LAT = 111_132.0


def _lat_offset(meters: float) -> float:
    return meters / M_PER_DEG_LAT


def test_all_aligned_route_no_changes():
    stops = [
        {"stop_id": f"s{i}", "lat": -0.200, "lon": -78.495 + 0.01 * i}
        for i in range(5)
    ]
    cleaned, report = cleanup_orphan_stops(stops, POLYLINE)

    assert len(cleaned) == 5
    assert report.stops_aligned == 5
    assert report.stops_snapped == 0
    assert report.stops_removed == 0
    assert report.snapped_stops == []
    assert report.orphans_removed == []
    for original, after in zip(stops, cleaned):
        assert after["lat"] == original["lat"]
        assert after["lon"] == original["lon"]
        assert "snapped_to_polyline" not in after


def test_mixed_route_classified_correctly():
    stops = [
        {"stop_id": "a", "lat": -0.200, "lon": -78.450},                       # aligned
        {"stop_id": "b", "lat": -0.200 + _lat_offset(20.0), "lon": -78.430},   # snap
        {"stop_id": "c", "lat": -0.200 + _lat_offset(40.0), "lon": -78.420},   # snap
        {"stop_id": "d", "lat": -0.200 + _lat_offset(40.0), "lon": -78.410},   # snap
        {"stop_id": "e", "lat": -0.200, "lon": -78.405},                       # aligned
    ]
    cleaned, report = cleanup_orphan_stops(stops, POLYLINE)

    assert len(cleaned) == 5
    assert report.stops_aligned == 2
    assert report.stops_snapped == 3
    assert report.stops_removed == 0
    assert len(report.snapped_stops) == 3

    snapped_in_cleaned = [s for s in cleaned if s.get("snapped_to_polyline")]
    assert len(snapped_in_cleaned) == 3
    # Snapped stops sit on the polyline (lat ~ -0.200).
    for s in snapped_in_cleaned:
        assert abs(s["lat"] - (-0.200)) < 1e-6


def test_safety_check_rejects_when_over_threshold():
    stops = [
        {"stop_id": "a", "lat": -0.200, "lon": -78.450},                       # aligned
        {"stop_id": "b", "lat": -0.200, "lon": -78.440},                       # aligned
        {"stop_id": "c", "lat": -0.200 + _lat_offset(200.0), "lon": -78.430},  # orphan
        {"stop_id": "d", "lat": -0.200 + _lat_offset(200.0), "lon": -78.420},  # orphan
    ]
    cleaned, report = cleanup_orphan_stops(stops, POLYLINE)

    assert report.stops_removed == 2
    assert report.total_stops_before == 4
    assert report.total_stops_after == 2

    ok, reason = validate_cleanup_not_destructive(stops, cleaned)
    assert ok is False
    assert "50%" in reason


def test_safety_check_allows_within_threshold():
    stops = [
        {"stop_id": f"s{i}", "lat": -0.200, "lon": -78.490 + 0.005 * i}
        for i in range(10)
    ]
    # Make 1 orphan = 10% removal (under 20% default).
    stops[0] = {"stop_id": "s0", "lat": -0.200 + _lat_offset(200.0), "lon": -78.490}

    cleaned, report = cleanup_orphan_stops(stops, POLYLINE)
    assert report.stops_removed == 1

    ok, reason = validate_cleanup_not_destructive(stops, cleaned)
    assert ok is True


def test_report_fields_populated():
    stops = [
        {"stop_id": "a", "lat": -0.200, "lon": -78.450},                       # aligned
        {"stop_id": "b", "lat": -0.200 + _lat_offset(40.0), "lon": -78.430},   # snap
        {"stop_id": "c", "lat": -0.200 + _lat_offset(200.0), "lon": -78.420},  # orphan
    ]
    cleaned, report = cleanup_orphan_stops(stops, POLYLINE)

    assert report.total_stops_before == 3
    assert report.total_stops_after == 2
    assert report.stops_aligned == 1
    assert report.stops_snapped == 1
    assert report.stops_removed == 1
    assert report.cleanup_version == 2
    assert report.thresholds_used["aligned_threshold_m"] == 10.0
    assert report.thresholds_used["snap_threshold_m"] == 60.0
    assert report.thresholds_used["max_removal_pct"] == 0.20

    assert report.orphans_removed[0]["stop_id"] == "c"
    assert "exceeds_snap_threshold" in report.orphans_removed[0]["reason"]
    assert report.snapped_stops[0]["stop_id"] == "b"
    assert "snapped_lat" in report.snapped_stops[0]
    assert "snapped_lon" in report.snapped_stops[0]


def test_stop_sequence_preserved_for_kept_stops():
    stops = [
        {"stop_id": "first", "lat": -0.200, "lon": -78.450, "marker": "alpha"},
        {"stop_id": "drop_me", "lat": -0.200 + _lat_offset(200.0), "lon": -78.440},
        {"stop_id": "third", "lat": -0.200, "lon": -78.430, "marker": "gamma"},
    ]
    cleaned, _ = cleanup_orphan_stops(stops, POLYLINE)

    assert [s["stop_id"] for s in cleaned] == ["first", "third"]
    assert cleaned[0]["marker"] == "alpha"
    assert cleaned[1]["marker"] == "gamma"


def test_validate_handles_empty_input():
    ok, reason = validate_cleanup_not_destructive([], [])
    assert ok is True
    assert "empty" in reason


def test_validate_no_removals_passes():
    stops = [
        {"stop_id": "a", "lat": 0.0, "lon": 0.0},
        {"stop_id": "b", "lat": 0.0, "lon": 0.0},
    ]
    ok, reason = validate_cleanup_not_destructive(stops, list(stops))
    assert ok is True


def test_near_band_now_snaps_under_v2():
    # A 18 m stop used to be classified as 'near' (kept original) under
    # v1; under v2 it is 'snap' and the cleaned coord lands on the polyline.
    stops = [
        {"stop_id": "near18", "lat": -0.200 + _lat_offset(18.0), "lon": -78.450},
    ]
    cleaned, report = cleanup_orphan_stops(stops, POLYLINE)
    assert report.stops_snapped == 1
    assert cleaned[0]["snapped_to_polyline"] is True
    assert abs(cleaned[0]["lat"] - (-0.200)) < 1e-6
