"""Tests for the Stop Coverage Enforcer.

Nine scenarios per the Prompt 6 spec. Synthetic routes use a Quito-centred
equirectangular grid where 1 metre ≈ the deltas hard-coded in _xy; the
shared constants match the geometry-enforcer test file so cross-reading
is easy.

DB-backed tests (#8 and #9) skip cleanly if the local DataMind Postgres
is unreachable. They are not load-bearing for basic correctness — the
first seven tests fully exercise the module's logic.
"""
from __future__ import annotations

import math
import os
from typing import Any

import pytest

from hades.enforcers.geometry_enforcer import GeometryReport
from hades.enforcers.stop_coverage_enforcer import (
    DEFAULT_THRESHOLDS,
    ZONE_DEFAULTS,
    Gap,
    GapResolution,
    StopCoverageEnforcer,
    StopCoverageReport,
    ZoneThresholds,
    analyze_stop_coverage,
    infer_zone,
)


# Quito-ish centre; plenty far from the poles for equirectangular to be
# sub-percent-accurate over sub-metro distances.
LAT_0 = -0.18
LON_0 = -78.48
M_PER_DEG_LAT = 111_132.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT_0))


def _xy(x_m: float, y_m: float) -> tuple[float, float]:
    """Convert local metre offsets to (lon, lat) for route polylines."""
    lon = LON_0 + x_m / M_PER_DEG_LON
    lat = LAT_0 + y_m / M_PER_DEG_LAT
    return (lon, lat)


def _latlon(x_m: float, y_m: float) -> tuple[float, float]:
    """Convert local metre offsets to (lat, lon) for stop markers."""
    lon, lat = _xy(x_m, y_m)
    return (lat, lon)


# ---------------------------------------------------------------------------
# #1 — clean urban route (5 km, stops every 300 m) → good, 0 gaps.
# ---------------------------------------------------------------------------

def test_clean_urban_route_has_no_gaps():
    coords = [_xy(x, 0.0) for x in range(0, 5001, 25)]
    stops = [_latlon(x, 0.0) for x in range(150, 5000, 300)]

    report = analyze_stop_coverage(coords, stops, route_code="TEST-CLEAN")
    # 5 km falls in urban_dense per default bands, threshold good_gap = 500 m.
    assert report["zone"] == "urban_dense"
    assert report["classification"] == "good"
    assert report["summary"]["n_gaps_total"] == 0
    assert report["summary"]["n_gaps_unresolved"] == 0


# ---------------------------------------------------------------------------
# #2 — 5 km urban route with a 1.5 km hole → one gap detected, tier
#      attempted. No resolvers wired so it should fall to tier 4/5 prepared.
# ---------------------------------------------------------------------------

def test_urban_route_with_gap_records_attempt_and_prepares_dr():
    coords = [_xy(x, 0.0) for x in range(0, 5001, 25)]
    # Remove the block between 2 km and 3.5 km.
    stops = [_latlon(x, 0.0) for x in range(150, 5000, 300)
             if not (2000 <= x <= 3500)]

    report = analyze_stop_coverage(coords, stops, route_code="TEST-GAP")
    assert report["summary"]["n_gaps_total"] >= 1
    # No resolvers provided → every gap must wind up tier 4_prepared.
    assert report["summary"]["n_dr_queries_prepared"] >= 1
    # And each gap should carry a resolution record (even if unresolved).
    assert all(g["resolution"] is not None for g in report["gaps"])
    assert report["classification"] in ("degraded", "unroutable")


# ---------------------------------------------------------------------------
# #3 — Tier 1 cross-route borrow resolves the gap.
# ---------------------------------------------------------------------------

def test_tier1_cross_route_borrow_resolves_gap():
    coords = [_xy(x, 0.0) for x in range(0, 5001, 25)]
    stops = [_latlon(x, 0.0) for x in range(150, 5000, 300)
             if not (2000 <= x <= 3500)]

    # Mock cross-route resolver: any call returns a single candidate
    # exactly at the gap midpoint.
    seen_calls = []

    def mock_cross_route(lat, lon, buffer_m, route_corridor_coords,
                         corridor_buffer_m, exclude_stop_ids):
        seen_calls.append((lat, lon, buffer_m, corridor_buffer_m))
        return [{
            "node_id": "borrowed-node-uuid",
            "name": "Calle 42 frente a farmacia",
            "lat": lat,
            "lon": lon,
        }]

    def mock_overpass(lat, lon, buffer_m):
        raise AssertionError("overpass should not be called when tier 1 resolves")

    report = analyze_stop_coverage(
        coords, stops, route_code="TEST-TIER1",
        cross_route_resolver=mock_cross_route,
        overpass_resolver=mock_overpass,
    )
    assert seen_calls, "cross-route resolver must be called for at least one gap"
    for g in report["gaps"]:
        assert g["resolution"]["resolved"] is True
        assert g["resolution"]["tier"] == 1
        assert g["resolution"]["tier_label"] == "cross_route_borrow"
    assert report["summary"]["tier_usage"]["tier1_cross_route_borrow"] >= 1
    assert report["summary"]["n_dr_queries_prepared"] == 0


# ---------------------------------------------------------------------------
# #4 — Tier 2 Overpass POI resolves the gap when Tier 1 fails.
# ---------------------------------------------------------------------------

def test_tier2_overpass_resolves_when_tier1_empty():
    coords = [_xy(x, 0.0) for x in range(0, 5001, 25)]
    stops = [_latlon(x, 0.0) for x in range(150, 5000, 300)
             if not (2000 <= x <= 3500)]

    def empty_cross_route(**kwargs):
        return []

    def mock_overpass(lat, lon, buffer_m):
        return [
            {"osm_id": 111, "name": "Bus Stop A", "lat": lat, "lon": lon,
             "tags": {"highway": "bus_stop"}},
            {"osm_id": 222, "name": "Bus Stop B", "lat": lat + 0.0005,
             "lon": lon + 0.0005, "tags": {"highway": "bus_stop"}},
        ]

    report = analyze_stop_coverage(
        coords, stops, route_code="TEST-TIER2",
        cross_route_resolver=empty_cross_route,
        overpass_resolver=mock_overpass,
    )
    assert any(
        g["resolution"]["tier"] == 2 and g["resolution"]["resolved"]
        for g in report["gaps"]
    )
    assert report["summary"]["tier_usage"]["tier2_osm_poi"] >= 1


# ---------------------------------------------------------------------------
# #5 — No tier resolves → classification = unroutable / degraded + DR prepared.
# ---------------------------------------------------------------------------

def test_unresolvable_gap_prepares_dr_and_marks_unroutable():
    # Make the gap wide enough to hit the unroutable band.
    coords = [_xy(x, 0.0) for x in range(0, 5001, 25)]
    stops = [_latlon(x, 0.0) for x in [0, 150, 300, 4700, 4850, 5000]]

    def none_resolver(**kwargs):
        return []

    def none_overpass(lat, lon, buffer_m):
        return []

    report = analyze_stop_coverage(
        coords, stops, route_code="TEST-UNROUTABLE",
        cross_route_resolver=none_resolver,
        overpass_resolver=none_overpass,
    )
    assert report["classification"] == "unroutable"
    assert report["summary"]["n_dr_queries_prepared"] >= 1
    assert report["summary"]["n_synthetic_prepared"] >= 1
    for g in report["gaps"]:
        assert g["resolution"]["tier"] == 4
        assert g["resolution"]["resolved"] is False


# ---------------------------------------------------------------------------
# #6 — Zone inference: urban_dense vs urban_peripheral vs rural vs
#      interprovincial correctly classified from route length.
# ---------------------------------------------------------------------------

def test_zone_inference_length_bands():
    assert infer_zone(500.0) == "urban_dense"
    assert infer_zone(5500.0) == "urban_dense"
    assert infer_zone(6000.0) == "urban_dense"
    assert infer_zone(10_000.0) == "urban_peripheral"
    assert infer_zone(15_000.0) == "urban_peripheral"
    assert infer_zone(30_000.0) == "rural"
    assert infer_zone(50_000.0) == "rural"
    assert infer_zone(100_000.0) == "interprovincial"
    assert infer_zone(586_058.0) == "interprovincial"


def test_analyze_respects_explicit_zone_override():
    coords = [_xy(x, 0.0) for x in range(0, 2001, 25)]
    stops = [_latlon(x, 0.0) for x in range(100, 2000, 200)]
    # Calling with a rural override should use the rural (very loose)
    # thresholds so the tight 200 m spacing is still good.
    report = analyze_stop_coverage(coords, stops, route_code="Z", zone="rural")
    assert report["zone"] == "rural"
    assert report["classification"] == "good"


# ---------------------------------------------------------------------------
# #7 — Idempotency: same input → identical report dict.
# ---------------------------------------------------------------------------

def test_analyze_is_deterministic_for_same_input():
    coords = [_xy(x, 0.0) for x in range(0, 5001, 25)]
    stops = [_latlon(x, 0.0) for x in range(150, 5000, 300)
             if not (2000 <= x <= 3500)]

    def mock_cross_route(lat, lon, **_):
        return [{"node_id": "n", "lat": lat + 0.0001, "lon": lon,
                 "name": "fixed"}]

    a = analyze_stop_coverage(coords, stops, route_code="DET",
                              cross_route_resolver=mock_cross_route)
    b = analyze_stop_coverage(coords, stops, route_code="DET",
                              cross_route_resolver=mock_cross_route)
    assert a == b


# ---------------------------------------------------------------------------
# DB fixtures for #8 and #9.
# ---------------------------------------------------------------------------

LOCAL_DSN = os.getenv(
    "DB_DSN", "",
)


def _fetch_route_and_stops(route_id: str):
    try:
        import psycopg2
    except ImportError:  # pragma: no cover
        pytest.skip("psycopg2 not installed")
    try:
        conn = psycopg2.connect(LOCAL_DSN, connect_timeout=3)
    except Exception:
        pytest.skip("local DB unreachable")
    with conn:
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
              ST_AsGeoJSON(r.geom) AS geojson,
              r.stop_node_ids,
              r.province,
              r.source_type,
              r.route_name,
              ST_Length(r.geom::geography) AS length_m
            FROM route_prod.routes r
            WHERE r.route_id = %s::uuid
            """,
            (route_id,),
        )
        row = cur.fetchone()
        if row is None:
            pytest.skip(f"route {route_id} not in local DB")
        geojson_str, stop_ids, province, source_type, name, length_m = row
        import json as _json
        gj = _json.loads(geojson_str)
        if gj.get("type") != "LineString":
            pytest.skip("non-LineString route")
        coords = [(float(c[0]), float(c[1])) for c in gj["coordinates"]]
        if not stop_ids:
            return coords, [], [], province, source_type, name, length_m
        cur.execute(
            """
            SELECT node_id::text, ST_Y(geom) AS lat, ST_X(geom) AS lon
            FROM node_prod.nodes
            WHERE node_id = ANY(%s::uuid[])
            """,
            (stop_ids,),
        )
        stop_rows = cur.fetchall()
        stops_latlon = [(float(r[1]), float(r[2])) for r in stop_rows]
        ids = [r[0] for r in stop_rows]
    return coords, stops_latlon, ids, province, source_type, name, length_m


# ---------------------------------------------------------------------------
# #8 — Real clean route from DB (picked for dense urban stops) → good.
# ---------------------------------------------------------------------------

# A short, densely-stopped urban route: "Banco Internacional - Marin"
# (osm_relation_import), 4.9 km with 80 stops (~60 m average spacing).
# This is comfortably inside the urban_dense "good" band.
CLEAN_DB_ROUTE = "7aeaf916-9923-4f02-b315-cfe5624a500b"


def test_real_clean_db_route_is_good_or_acceptable():
    coords, stops, ids, province, source_type, name, length_m = _fetch_route_and_stops(CLEAN_DB_ROUTE)
    if not coords or not stops:
        pytest.skip("route missing geom or stops in this DB")
    report = analyze_stop_coverage(
        coords, stops, route_code=CLEAN_DB_ROUTE, stop_ids=ids,
    )
    assert report["classification"] in ("good", "acceptable"), (
        f"dense urban route expected clean, got {report['classification']} "
        f"(zone={report['zone']}, gaps={report['summary']['n_gaps_total']})"
    )
    assert report["classification"] != "unroutable"
    assert report["n_stops"] == len(stops)


# ---------------------------------------------------------------------------
# #9 — Real route with known sparse coverage → at least one gap. The
#      longest routes in the inventory (Cayambe-Aloag etc.) are a safe
#      choice because the interprovincial thresholds are loose *and* the
#      median stop spacing on these long routes exceeds them in patches.
# ---------------------------------------------------------------------------

SPARSE_DB_ROUTE = "da302a5b-b3ba-46d7-bb8b-dae1af67001f"  # Cayambe - Aloag, 150 km


def test_real_sparse_db_route_has_at_least_one_gap():
    coords, stops, ids, province, source_type, name, length_m = _fetch_route_and_stops(SPARSE_DB_ROUTE)
    if not coords or not stops:
        pytest.skip("route missing geom or stops in this DB")
    report = analyze_stop_coverage(
        coords, stops, route_code=SPARSE_DB_ROUTE, stop_ids=ids,
    )
    assert report["summary"]["n_gaps_total"] >= 1, (
        f"expected sparse-coverage route to show at least one gap, got "
        f"{report['summary']['n_gaps_total']} (zone={report['zone']}, "
        f"length_m={length_m:.0f}, n_stops={len(stops)})"
    )


# ---------------------------------------------------------------------------
# v2 classifier — direct unit tests on _classify.
#
# The taxonomy adds two classes (ship_pending_dr, degraded_minor), an 8-cap
# on tier-4 pending gaps, a synthetic-heavy red flag, and a geometry-severity
# veto. The cases below exercise each branch of the new decision tree against
# hand-built StopCoverageReport instances so the assertions are explicit
# about which rule fires.
# ---------------------------------------------------------------------------

_URBAN = ZONE_DEFAULTS["urban_dense"]


def _gap(idx: int, *, gap_m: float, severity: str,
         tier: int, resolved: bool) -> Gap:
    """Synthetic gap with a specified resolution tier + severity band."""
    return Gap(
        idx=idx,
        prev_stop_idx=idx,
        next_stop_idx=idx + 1,
        gap_m=gap_m,
        midpoint_cum_m=1000.0 + idx * 100.0,
        midpoint_coord=(LAT_0, LON_0),
        severity_band=severity,
        resolution=GapResolution(
            tier=tier,
            tier_label={1: "cross_route_borrow", 2: "osm_poi",
                         4: "dr_prepared", 5: "synthetic_prepared"}[tier],
            resolved=resolved,
        ),
    )


def _report(*, route_length_m: float, gaps: list[Gap],
            n_stops: int = 12, classification: str = "good") -> StopCoverageReport:
    return StopCoverageReport(
        route_code="X", version=1, zone="urban_dense",
        classification=classification, n_stops=n_stops,
        route_length_m=route_length_m, gaps=gaps,
    )


def _geom(classification: str) -> GeometryReport:
    return GeometryReport(
        route_code="X", version=3, anomalies=[],
        classification=classification, max_severity=0.0,
    )


def test_classifier_ship_pending_dr_happy_path():
    """3 tier-4 pending gaps, clean shape, 10 km → unres_per_km=0.3, count≤8."""
    enf = StopCoverageEnforcer()
    gaps = [_gap(i, gap_m=700.0, severity="acceptable", tier=4, resolved=False)
            for i in range(3)]
    report = _report(route_length_m=10_000.0, gaps=gaps)
    cls, reasoning = enf._classify(report, _URBAN, _geom("clean"))
    assert cls == "ship_pending_dr"
    assert reasoning["rule_fired"] == "ship_pending_dr"
    assert reasoning["tier4_pending"] == 3
    assert reasoning["unres_per_km"] == 0.3


def test_classifier_ship_pending_dr_blocked_by_8_cap():
    """9 tier-4 pending gaps on a 20 km route — density ≤0.5 but count >8."""
    enf = StopCoverageEnforcer()
    gaps = [_gap(i, gap_m=700.0, severity="acceptable", tier=4, resolved=False)
            for i in range(9)]
    report = _report(route_length_m=20_000.0, gaps=gaps)
    cls, reasoning = enf._classify(report, _URBAN, _geom("clean"))
    assert cls != "ship_pending_dr"
    # 9/20 = 0.45/km, in the (0.3, 0.8] band → degraded_minor.
    assert cls == "degraded_minor"
    assert reasoning["tier4_pending"] == 9


def test_classifier_ship_pending_dr_blocked_by_truly_broken():
    """1 tier-4 + 1 unresolved-no-resolution → truly_broken=1, blocks ship."""
    enf = StopCoverageEnforcer()
    g4 = _gap(0, gap_m=700.0, severity="acceptable", tier=4, resolved=False)
    bare = Gap(
        idx=1, prev_stop_idx=1, next_stop_idx=2,
        gap_m=700.0, midpoint_cum_m=1500.0,
        midpoint_coord=(LAT_0, LON_0),
        severity_band="acceptable", resolution=None,
    )
    report = _report(route_length_m=10_000.0, gaps=[g4, bare])
    cls, reasoning = enf._classify(report, _URBAN, _geom("clean"))
    assert cls != "ship_pending_dr"
    assert reasoning["truly_broken"] == 1


def test_classifier_degraded_minor_via_degraded_band():
    """1 truly-broken degraded-band gap on 10 km, clean shape → degraded_minor.

    Uses a bare unresolved gap (no GapResolution) so ship_pending_dr is
    blocked by truly_broken>0; falls through to the degraded-band rule,
    which downgrades to degraded_minor when geometry is clean and
    density is ≤0.5/km.
    """
    enf = StopCoverageEnforcer()
    bare = Gap(
        idx=0, prev_stop_idx=0, next_stop_idx=1,
        gap_m=1800.0, midpoint_cum_m=1500.0,
        midpoint_coord=(LAT_0, LON_0),
        severity_band="degraded", resolution=None,
    )
    report = _report(route_length_m=10_000.0, gaps=[bare])
    cls, reasoning = enf._classify(report, _URBAN, _geom("clean"))
    assert cls == "degraded_minor"
    assert reasoning["rule_fired"] == "degraded_band_minor_density"


def test_classifier_geometry_severity_vetoes_to_unroutable():
    """Severe geometry overrides everything — even a route with zero gaps."""
    enf = StopCoverageEnforcer()
    report = _report(route_length_m=10_000.0, gaps=[])
    cls, reasoning = enf._classify(report, _URBAN, _geom("severe"))
    assert cls == "unroutable"
    assert reasoning["rule_fired"] == "geometry_severe_veto"


def test_classifier_synthetic_heavy_is_degraded():
    """≥70 % tier-5 share of unresolved gaps → degraded regardless of density."""
    enf = StopCoverageEnforcer()
    # 4 tier-5 + 1 tier-4 = 80 % synthetic on a long route.
    gaps = (
        [_gap(i, gap_m=700.0, severity="acceptable", tier=5, resolved=False)
         for i in range(4)]
        + [_gap(4, gap_m=700.0, severity="acceptable", tier=4, resolved=False)]
    )
    report = _report(route_length_m=30_000.0, gaps=gaps)
    cls, reasoning = enf._classify(report, _URBAN, _geom("clean"))
    assert cls == "degraded"
    assert reasoning["synthetic_heavy"] is True
    assert reasoning["rule_fired"] == "synthetic_heavy"


def test_classifier_reasoning_populated_via_analyze():
    """Reasoning dict is exposed on StopCoverageReport.to_dict() output."""
    coords = [_xy(x, 0.0) for x in range(0, 5001, 25)]
    stops = [_latlon(x, 0.0) for x in range(150, 5000, 300)]
    report = analyze_stop_coverage(coords, stops, route_code="REASONING")
    assert "classification_reasoning" in report
    rsn = report["classification_reasoning"]
    assert rsn["rule_fired"] == "no_unresolved"
    assert rsn["n_stops"] == len(stops)
