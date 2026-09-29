"""Tests for hades.enforcers.stop_refill."""
from __future__ import annotations

import math

from hades.enforcers.stop_refill import (
    DISTANCE_CAP_M,
    HIGH_CONFIDENCE_THRESHOLD,
    MAX_CANDIDATES_PER_ROUTE,
    MIN_SCORE_SURFACE,
    ORIGIN_ALIGNED_THRESHOLD_M,
    POPULARITY_CAP_ROUTES,
    SEGMENT_CAP_M,
    ProductionRoute,
    RefillCandidate,
    compute_geometric_score,
    find_refill_candidates,
)


M_PER_DEG_LAT = 111_132.0
LAT_REF = -0.200
M_PER_DEG_LON_AT_REF = 111_320.0 * math.cos(math.radians(LAT_REF))


def _lat_offset_m(meters: float) -> float:
    return meters / M_PER_DEG_LAT


def _lon_offset_m(meters: float) -> float:
    return meters / M_PER_DEG_LON_AT_REF


# East-west polyline at LAT_REF, ~11 km long. Used as route A's polyline
# in most tests.
POLYLINE_A = [(-78.500, LAT_REF), (-78.400, LAT_REF)]


def _stop_on_a(stop_id: str, lon_offset_m: float, perp_offset_m: float = 0.0) -> dict:
    """Build a stop dict positioned on polyline A.

    ``lon_offset_m`` is metres east of A's start vertex; ``perp_offset_m``
    is metres north of the polyline (negative for south).
    """
    return {
        "stop_id": stop_id,
        "lat": LAT_REF + _lat_offset_m(perp_offset_m),
        "lon": -78.500 + _lon_offset_m(lon_offset_m),
    }


def test_no_shipped_routes_returns_empty():
    out = find_refill_candidates("route_a", POLYLINE_A, [], [])
    assert out == []


def test_finds_candidates_in_shared_segment():
    # Production route B is identical to A and contributes a stop ~5 m off A.
    candidate_stop = _stop_on_a("stop_x", lon_offset_m=5_000.0, perp_offset_m=5.0)
    prod = ProductionRoute(
        route_id="route_b",
        polyline=list(POLYLINE_A),
        stops=[candidate_stop],
    )
    out = find_refill_candidates("route_a", POLYLINE_A, [], [prod])
    assert len(out) == 1
    cand = out[0]
    assert cand.stop_id == "stop_x"
    assert cand.distance_to_polyline_m <= 6.0
    assert "route_b" in cand.source_route_ids
    assert cand.routes_count == 1


def test_excludes_stops_already_in_route():
    candidate_stop = _stop_on_a("stop_x", lon_offset_m=5_000.0, perp_offset_m=5.0)
    prod = ProductionRoute(
        route_id="route_b",
        polyline=list(POLYLINE_A),
        stops=[candidate_stop],
    )
    out = find_refill_candidates(
        "route_a",
        POLYLINE_A,
        existing_stops_a=[candidate_stop],
        production_routes=[prod],
    )
    assert out == []


def test_excludes_self_match():
    # If route A appears in the production pool (same route_id), skip it.
    candidate_stop = _stop_on_a("stop_x", lon_offset_m=5_000.0, perp_offset_m=5.0)
    self_pool = ProductionRoute(
        route_id="route_a",
        polyline=list(POLYLINE_A),
        stops=[candidate_stop],
    )
    out = find_refill_candidates("route_a", POLYLINE_A, [], [self_pool])
    assert out == []


def test_aggregates_same_stop_from_multiple_routes():
    candidate_stop = _stop_on_a("stop_x", lon_offset_m=5_000.0, perp_offset_m=5.0)
    prods = [
        ProductionRoute(
            route_id=f"route_b_{i}",
            polyline=list(POLYLINE_A),
            stops=[candidate_stop],
        )
        for i in range(3)
    ]
    out = find_refill_candidates("route_a", POLYLINE_A, [], prods)
    assert len(out) == 1
    cand = out[0]
    assert cand.routes_count == 3
    assert sorted(cand.source_route_ids) == ["route_b_0", "route_b_1", "route_b_2"]


def test_score_correlates_with_distance():
    # Same popularity (1 route), same shared-segment length, varying distance.
    high = compute_geometric_score(
        distance_m=2.0, routes_count=1, max_shared_segment_m=5_000.0
    )
    low = compute_geometric_score(
        distance_m=25.0, routes_count=1, max_shared_segment_m=5_000.0
    )
    assert high > low
    # Distance at the cap → distance_factor = 0; popularity 0.20 + segment 1.0.
    cap = compute_geometric_score(
        distance_m=DISTANCE_CAP_M,
        routes_count=1,
        max_shared_segment_m=5_000.0,
    )
    assert cap < low


def test_score_correlates_with_popularity():
    base = compute_geometric_score(
        distance_m=5.0, routes_count=1, max_shared_segment_m=5_000.0
    )
    more = compute_geometric_score(
        distance_m=5.0, routes_count=5, max_shared_segment_m=5_000.0
    )
    assert more > base
    # Beyond the cap, popularity factor saturates at 1.0.
    saturated = compute_geometric_score(
        distance_m=5.0,
        routes_count=POPULARITY_CAP_ROUTES + 10,
        max_shared_segment_m=5_000.0,
    )
    assert math.isclose(saturated, more, rel_tol=1e-9)


def test_score_correlates_with_segment_length():
    low = compute_geometric_score(
        distance_m=5.0, routes_count=1, max_shared_segment_m=600.0
    )
    high = compute_geometric_score(
        distance_m=5.0, routes_count=1, max_shared_segment_m=5_000.0
    )
    assert high > low
    # Beyond the cap, segment factor saturates.
    saturated = compute_geometric_score(
        distance_m=5.0,
        routes_count=1,
        max_shared_segment_m=SEGMENT_CAP_M * 2,
    )
    assert math.isclose(saturated, high, rel_tol=1e-9)


def test_min_score_filter():
    # A stop that's barely on the polyline (24 m off → distance_factor ≈ 0.20)
    # contributed by a single route with the minimum shared segment will
    # score below MIN_SCORE_SURFACE. With a higher threshold we get nothing.
    weak_stop = _stop_on_a("stop_weak", lon_offset_m=2_000.0, perp_offset_m=24.0)
    prod = ProductionRoute(
        route_id="route_b",
        polyline=list(POLYLINE_A),
        stops=[weak_stop],
    )
    # Default threshold may admit it; raise the threshold and it disappears.
    out = find_refill_candidates(
        "route_a",
        POLYLINE_A,
        [],
        [prod],
        min_score=HIGH_CONFIDENCE_THRESHOLD,
    )
    assert out == []


def test_cap_at_15():
    # 20 distinct candidates contributed by one production route — only
    # MAX_CANDIDATES_PER_ROUTE should come back.
    stops = [
        _stop_on_a(f"stop_{i:02d}", lon_offset_m=500.0 + 400.0 * i, perp_offset_m=2.0)
        for i in range(20)
    ]
    prod = ProductionRoute(
        route_id="route_b",
        polyline=list(POLYLINE_A),
        stops=stops,
    )
    out = find_refill_candidates(
        "route_a",
        POLYLINE_A,
        [],
        [prod],
        min_score=0.0,
    )
    assert len(out) == MAX_CANDIDATES_PER_ROUTE
    # Sorted by score descending.
    scores = [c.score for c in out]
    assert scores == sorted(scores, reverse=True)


def test_score_zero_distance_full_popularity_full_segment():
    # Best possible inputs → score of 1.0.
    s = compute_geometric_score(
        distance_m=0.0,
        routes_count=POPULARITY_CAP_ROUTES,
        max_shared_segment_m=SEGMENT_CAP_M,
    )
    assert math.isclose(s, 1.0, rel_tol=1e-9)


def test_score_clamped_to_zero_for_extreme_inputs():
    # Negative distance is clipped; very large distance → distance_factor = 0.
    s = compute_geometric_score(
        distance_m=10_000.0, routes_count=0, max_shared_segment_m=0.0
    )
    assert s == 0.0


def test_returns_refill_candidate_instances():
    candidate_stop = _stop_on_a("stop_x", lon_offset_m=5_000.0, perp_offset_m=5.0)
    prod = ProductionRoute(
        route_id="route_b",
        polyline=list(POLYLINE_A),
        stops=[candidate_stop],
    )
    out = find_refill_candidates("route_a", POLYLINE_A, [], [prod])
    assert all(isinstance(c, RefillCandidate) for c in out)


def test_candidate_outside_shared_corridor_excluded():
    # B is far from A → no shared segments → no candidates regardless of
    # how close the stop is to A.
    polyline_b = [
        (-78.500, LAT_REF + _lat_offset_m(2_000.0)),
        (-78.400, LAT_REF + _lat_offset_m(2_000.0)),
    ]
    stop_on_a_only = _stop_on_a("stop_x", lon_offset_m=5_000.0, perp_offset_m=2.0)
    prod = ProductionRoute(
        route_id="route_b",
        polyline=polyline_b,
        stops=[stop_on_a_only],
    )
    out = find_refill_candidates("route_a", POLYLINE_A, [], [prod])
    assert out == []


def test_candidate_too_far_from_polyline_excluded():
    # B coincides with A so the corridor is found, but the stop sits 80 m
    # off A — beyond STOP_PROXIMITY_THRESHOLD_M (25 m).
    far_stop = _stop_on_a("stop_far", lon_offset_m=5_000.0, perp_offset_m=80.0)
    prod = ProductionRoute(
        route_id="route_b",
        polyline=list(POLYLINE_A),
        stops=[far_stop],
    )
    out = find_refill_candidates("route_a", POLYLINE_A, [], [prod])
    assert out == []


# ---------------------------------------------------------------------------
# Origin-alignment filter (user spec 2026-04-27).
# ---------------------------------------------------------------------------

def _shifted_polyline(perp_offset_m: float) -> list[tuple[float, float]]:
    """Return POLYLINE_A shifted ``perp_offset_m`` north of LAT_REF.

    Used to construct origin polylines whose distance to a stop placed at
    LAT_REF (on top of the target polyline A) is exactly ``perp_offset_m``.
    """
    return [
        (-78.500, LAT_REF + _lat_offset_m(perp_offset_m)),
        (-78.400, LAT_REF + _lat_offset_m(perp_offset_m)),
    ]


def test_origin_alignment_constant_value():
    """Threshold must be 10.0 (matches α cleanup 'aligned' band)."""
    assert ORIGIN_ALIGNED_THRESHOLD_M == 10.0


def test_excludes_stop_not_aligned_in_origin_route():
    # B's polyline is shifted 18 m north of A's. The candidate stop sits
    # ON polyline A (0 m off A) but 18 m off polyline B → snap-required
    # in B → MUST be excluded by the origin-alignment filter.
    polyline_b = _shifted_polyline(perp_offset_m=18.0)
    candidate = _stop_on_a("stop_marginal", lon_offset_m=5_000.0, perp_offset_m=0.0)
    prod = ProductionRoute(
        route_id="route_b",
        polyline=polyline_b,
        stops=[candidate],
    )
    out = find_refill_candidates("route_a", POLYLINE_A, [], [prod])
    assert out == []


def test_includes_stop_fully_aligned_in_origin_route():
    # B's polyline coincides with A's; stop is 5 m off both polylines →
    # aligned in both → MUST be a candidate.
    aligned_stop = _stop_on_a("stop_aligned", lon_offset_m=5_000.0, perp_offset_m=5.0)
    prod = ProductionRoute(
        route_id="route_b",
        polyline=list(POLYLINE_A),
        stops=[aligned_stop],
    )
    out = find_refill_candidates("route_a", POLYLINE_A, [], [prod])
    assert len(out) == 1
    assert out[0].stop_id == "stop_aligned"
    assert out[0].origin_distance_m <= ORIGIN_ALIGNED_THRESHOLD_M


def test_origin_alignment_threshold_at_boundary():
    # Stop at exactly 10 m off origin → INCLUDED (≤10 m boundary inclusive).
    boundary_stop = _stop_on_a(
        "stop_boundary",
        lon_offset_m=5_000.0,
        perp_offset_m=10.0,
    )
    prod = ProductionRoute(
        route_id="route_b",
        polyline=list(POLYLINE_A),
        stops=[boundary_stop],
    )
    out = find_refill_candidates("route_a", POLYLINE_A, [], [prod])
    assert len(out) == 1
    assert abs(out[0].origin_distance_m - 10.0) < 0.5  # frame error <0.5 m

    # Stop at 10.5 m → EXCLUDED.
    over_stop = _stop_on_a(
        "stop_over",
        lon_offset_m=5_000.0,
        perp_offset_m=10.5,
    )
    prod_over = ProductionRoute(
        route_id="route_b2",
        polyline=list(POLYLINE_A),
        stops=[over_stop],
    )
    out_over = find_refill_candidates("route_a", POLYLINE_A, [], [prod_over])
    assert out_over == []


def test_origin_distance_exposed_in_candidate():
    # Candidate dataclass surfaces origin_distance_m so the operator UI
    # can display it.
    aligned_stop = _stop_on_a("stop_x", lon_offset_m=5_000.0, perp_offset_m=4.0)
    prod = ProductionRoute(
        route_id="route_b",
        polyline=list(POLYLINE_A),
        stops=[aligned_stop],
    )
    out = find_refill_candidates("route_a", POLYLINE_A, [], [prod])
    assert len(out) == 1
    cand = out[0]
    assert hasattr(cand, "origin_distance_m")
    # Approx 4 m ± frame error.
    assert 3.0 <= cand.origin_distance_m <= 5.5


def test_aggregates_origin_distance_minimum_across_sources():
    # Same stop in 3 routes; origin distances 8 m, 5 m, 9 m.
    # Aggregated origin_distance_m must be the minimum (5 m).
    candidate_lon_offset = 5_000.0
    candidate_perp = 5.0  # constant — defines lat of the stop

    # The stop sits at (LAT_REF + 5m, lon X) — 5 m north of POLYLINE_A.
    cand_lat = LAT_REF + _lat_offset_m(candidate_perp)
    cand_lon = -78.500 + _lon_offset_m(candidate_lon_offset)
    stop_dict = {"stop_id": "stop_multi", "lat": cand_lat, "lon": cand_lon}

    # Each "origin" polyline shares A's geometry but is shifted a bit
    # north so origin_distance varies. Polyline lat = LAT_REF + offset
    # → distance = |stop_lat - polyline_lat| in metres.
    prods = [
        ProductionRoute(
            route_id=f"route_b_{offset}",
            polyline=_shifted_polyline(perp_offset_m=offset),
            stops=[stop_dict],
        )
        # Shifts that put origin distance at 3, 0, 4 m respectively
        # (stop is at 5 m, polyline at 8/5/9 means |5-8|=3, |5-5|=0, |5-9|=4).
        for offset in (8.0, 5.0, 9.0)
    ]
    out = find_refill_candidates("route_a", POLYLINE_A, [], prods)
    assert len(out) == 1
    cand = out[0]
    # Min origin distance is 0 m (the polyline at LAT_REF + 5 m goes
    # right under the stop).
    assert cand.origin_distance_m < 1.0
    assert cand.routes_count == 3
