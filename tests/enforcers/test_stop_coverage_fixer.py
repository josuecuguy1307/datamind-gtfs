"""Unit tests for hades.enforcers.stop_coverage_fixer.

The Fixer consumes a pre-computed :class:`StopCoverageReport` and applies
its prescriptions (tier 1/2 resolutions, injected DR landmarks, or the
enforcer's prepared synthetic fills) to produce a v2 stop list. It
re-runs the enforcer on v2 and emits only when strictly better.

Shapes use the same flat-earth Quito grid as the enforcer tests.
"""

from __future__ import annotations

import math

from hades.enforcers.stop_coverage_enforcer import StopCoverageEnforcer
from hades.enforcers.stop_coverage_fixer import StopCoverageFixer


# ---------------------------------------------------------------------------
# Flat-earth metre grid helpers.
# ---------------------------------------------------------------------------

LAT_0 = -0.18
LON_0 = -78.48
M_PER_DEG_LAT = 111_132.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT_0))


def _xy(x_m: float, y_m: float) -> tuple[float, float]:
    return (LON_0 + x_m / M_PER_DEG_LON, LAT_0 + y_m / M_PER_DEG_LAT)


def _latlon(x_m: float, y_m: float) -> tuple[float, float]:
    lon, lat = _xy(x_m, y_m)
    return (lat, lon)


# A 5 km urban-dense corridor with stops every 300 m but with the stop
# at x=2250 removed — that leaves a single 600 m gap (1950 → 2550) in
# the "acceptable" band. One inserted stop at the midpoint is enough to
# restore good coverage, which is what tests 1–3 verify.

def _corridor_with_hole():
    coords = [_xy(x, 0.0) for x in range(0, 5001, 25)]
    stops = [_latlon(x, 0.0) for x in range(150, 5000, 300) if x != 2250]
    stop_ids = [f"S{i}" for i in range(len(stops))]
    return coords, stops, stop_ids


# ---------------------------------------------------------------------------
# Test 1 — tier 1 (cross-route borrow) resolution flows into v2.
# ---------------------------------------------------------------------------

def test_fix_applies_tier1_cross_route_borrow():
    coords, stops, stop_ids = _corridor_with_hole()

    def xrt_resolver(**kwargs):
        # Return a single borrowable node at the gap midpoint (x=2250 m).
        lat, lon = _latlon(2250.0, 0.0)
        return [
            {
                "node_id": "XROUTE-A1",
                "lat": lat,
                "lon": lon,
                "name": "Test Borrow Node",
            }
        ]

    enforcer = StopCoverageEnforcer(cross_route_resolver=xrt_resolver)
    fixer = StopCoverageFixer(enforcer=enforcer)

    result = fixer.fix(
        route_code="TEST-T1",
        coords=coords,
        stop_coords=stops,
        stop_ids=stop_ids,
    )

    assert result.success is True
    assert result.reason == "improved"
    # Exactly one stop was added, from tier 1.
    assert len(result.stops_added) == 1
    assert result.stops_added[0]["tier"] == 1
    assert result.stops_added[0]["source"] == "cross_route_borrow"
    # v2 has one more stop than v1.
    assert len(result.stops_after) == len(stops) + 1
    # v2 classification did not regress. Under the v2 classifier a route
    # with all gaps tier-1/2-resolved is already "good", so the splice
    # may not move the cov_class — but the strict-better gate fires on
    # fewer total gaps, and stops_added/stops_after assertions above
    # already prove the borrow was applied.
    ladder = ["good", "acceptable", "ship_pending_dr",
              "degraded_minor", "degraded", "unroutable"]
    assert ladder.index(result.report_after.classification) <= ladder.index(
        result.report_before.classification
    )
    # And the splice strictly reduced total gaps in v2.
    assert len(result.report_after.gaps) < len(result.report_before.gaps)


# ---------------------------------------------------------------------------
# Test 2 — DR landmark applied when enforcer left gap at tier 4 prepared.
# ---------------------------------------------------------------------------

def test_fix_uses_dr_landmark_when_provided():
    coords, stops, stop_ids = _corridor_with_hole()

    # No resolvers → every gap falls to tier 4 prepared.
    enforcer = StopCoverageEnforcer()
    fixer = StopCoverageFixer(enforcer=enforcer)

    # Build the v1 report so we can pick the right gap_idx for the landmark.
    report_v1 = enforcer.analyze(
        route_code="TEST-DR",
        coords=coords,
        stop_coords=stops,
        stop_ids=stop_ids,
    )
    assert report_v1.gaps, "precondition: at least one gap must exist"
    gap_idx = report_v1.gaps[0].idx

    dr_lat, dr_lon = _latlon(2250.0, 0.0)
    dr_landmarks = {
        gap_idx: {
            "lat": dr_lat,
            "lon": dr_lon,
            "name": "Plaza Central (DR)",
            "confidence": "high",
            "dr_response_id": "dr-abc-123",
        }
    }

    result = fixer.fix(
        route_code="TEST-DR",
        coords=coords,
        stop_coords=stops,
        stop_ids=stop_ids,
        report_before=report_v1,
        dr_landmarks=dr_landmarks,
    )

    assert result.success is True
    assert result.reason == "improved"
    # The DR landmark drove at least one insertion.
    dr_fills = [a for a in result.stops_added if a["source"] == "dr_landmark"]
    assert len(dr_fills) >= 1
    assert dr_fills[0]["tier"] == 4
    assert dr_fills[0]["metadata"]["dr_response_id"] == "dr-abc-123"


# ---------------------------------------------------------------------------
# Test 3 — synthetic-fill fallback closes gap when nothing else is available.
# ---------------------------------------------------------------------------

def test_fix_falls_through_to_synthetic_fill():
    coords, stops, stop_ids = _corridor_with_hole()

    # No resolvers, no DR landmarks → every fill is synthetic (tier 5).
    enforcer = StopCoverageEnforcer()
    fixer = StopCoverageFixer(enforcer=enforcer)

    result = fixer.fix(
        route_code="TEST-SYN",
        coords=coords,
        stop_coords=stops,
        stop_ids=stop_ids,
    )

    assert result.success is True
    assert result.reason == "improved"
    assert result.stops_added, "synthetic cascade must produce at least one fill"
    assert all(a["tier"] == 5 for a in result.stops_added)
    assert all(a["source"] == "synthetic" for a in result.stops_added)
    # v2 classification better than v1.
    # v2 taxonomy ladder (best → worst). ship_pending_dr ranks just
    # below acceptable (clean shape, queued for DR); degraded_minor
    # ranks just below it.
    ladder = ["good", "acceptable", "ship_pending_dr",
              "degraded_minor", "degraded", "unroutable"]
    assert ladder.index(result.report_after.classification) < ladder.index(
        result.report_before.classification
    )
    # Every synthetic stop has a deterministic fix_gap*_tier5 id.
    synth_ids = [sid for sid in result.stop_ids_after if sid.startswith("fix_gap")]
    assert synth_ids, "synthetic fills must be tagged with fix_gap* ids"


# ---------------------------------------------------------------------------
# Test 4 — refusal path when v1 is already clean.
# ---------------------------------------------------------------------------

def test_fix_refuses_when_already_good():
    # 5 km corridor, stops every 300 m — default enforcer classifies good.
    coords = [_xy(x, 0.0) for x in range(0, 5001, 25)]
    stops = [_latlon(x, 0.0) for x in range(150, 5000, 300)]
    stop_ids = [f"S{i}" for i in range(len(stops))]

    fixer = StopCoverageFixer()
    result = fixer.fix(
        route_code="TEST-GOOD",
        coords=coords,
        stop_coords=stops,
        stop_ids=stop_ids,
    )

    assert result.success is False
    assert result.reason == "already_good"
    assert result.stops_after == result.stops_before
    assert result.stop_ids_after == result.stop_ids_before
    assert not result.stops_added
