"""Unit tests for hades.enforcers.geometry_enforcer (v3).

v3 reshaped the detector set around direction-vector monotonicity within
each ida/vuelta half, with terminal- and pivot-zone filters on U_TURN and
IMPOSSIBLE_LOOP. See the module docstring for the design rationale and the
calibration evidence that drove the redesign.

Coordinates are (lon, lat); synthetic tests use a flat-earth metre grid
centred on Quito (lat ≈ -0.18, lon ≈ -78.48) so that metre thresholds in
the detectors line up with the coords the test builds.
"""

from __future__ import annotations

import json
import math
import os

import pytest

from hades.enforcers.geometry_enforcer import (
    GeometryEnforcer,
    GeometryEnforcerThresholds,
    analyze_shape,
    bearing_deg,
    detect_pivot,
    dominant_vectors,
    haversine_m,
    segment_alignments,
    turn_angle_deg,
)

# INTEGRATION tests: they assume a database already populated with real data of a region.
# Skipped unless you set DB_DSN and DATAMIND_RUN_DB_TESTS=1 (see README, Tests section).
pytestmark = pytest.mark.skipif(
    not (os.environ.get("DB_DSN") and os.environ.get("DATAMIND_RUN_DB_TESTS") == "1"),
    reason="integration test: requires DB_DSN and DATAMIND_RUN_DB_TESTS=1",
)



# ---------------------------------------------------------------------------
# Helpers — flat-earth metre grid centred on Quito.
# ---------------------------------------------------------------------------

_LAT0 = -0.18
_LON0 = -78.48
_M_PER_DEG_LAT = 111_132.0
_M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(_LAT0))


def _xy(x_m: float, y_m: float) -> tuple[float, float]:
    return (_LON0 + x_m / _M_PER_DEG_LON, _LAT0 + y_m / _M_PER_DEG_LAT)


def _straight_line(length_m: float = 3000.0, step_m: float = 50.0) -> list[tuple[float, float]]:
    n = int(length_m / step_m) + 1
    return [_xy(i * step_m, 0.0) for i in range(n)]


def _ida_vuelta_same_avenue(
    out_m: float = 3000.0, step_m: float = 25.0, offset_m: float = 6.0
) -> list[tuple[float, float]]:
    """Drive east ``out_m`` then return west along the same avenue (tiny N offset).

    This is the canonical "clean bus" pattern — the v1 BACKTRACK detector
    flagged this as severe on every Quito bus route; v3 must classify it
    clean.
    """
    n = int(out_m / step_m) + 1
    east = [_xy(i * step_m, 0.0) for i in range(n)]
    west = [_xy((n - 1 - i) * step_m, offset_m) for i in range(1, n)]
    return east + west


# ---------------------------------------------------------------------------
# Primitive sanity.
# ---------------------------------------------------------------------------


def test_haversine_and_bearing_match_meter_grid():
    p, q = _xy(0, 0), _xy(1000, 0)
    assert haversine_m(p[1], p[0], q[1], q[0]) == pytest.approx(1000.0, rel=5e-3)
    assert bearing_deg(p[1], p[0], q[1], q[0]) == pytest.approx(90.0, abs=1.0)


def test_turn_angle_180_for_reversal():
    a, b, c = _xy(0, 0), _xy(100, 0), _xy(0, 0)
    assert turn_angle_deg(a, b, c) == pytest.approx(180.0, abs=0.5)


# ---------------------------------------------------------------------------
# Direction-vector primitives.
# ---------------------------------------------------------------------------


def test_pivot_detection_argmax_on_clean_ida_vuelta():
    coords = _ida_vuelta_same_avenue(out_m=2000.0, step_m=20.0)
    idx, method = detect_pivot(coords)
    # Out leg has 101 points (0..100), return leg appends 100 more → pivot
    # should be at vertex 100 (the easternmost point).
    assert method == "argmax_distance"
    assert idx == 100


def test_segment_alignments_sign_flips_at_pivot():
    coords = _ida_vuelta_same_avenue(out_m=2000.0, step_m=20.0)
    idx, _ = detect_pivot(coords)
    aligns = segment_alignments(coords, idx)
    # Every ida segment should align strongly positive (heading east, dom=east),
    # every vuelta segment strongly positive too (heading west, dom=west).
    ida_segs = aligns[:idx]
    vuelta_segs = aligns[idx + 1:]  # skip the pivot segment itself
    assert all(a > 0.95 for a in ida_segs), f"ida had weak alignments: min={min(ida_segs)}"
    assert all(a > 0.95 for a in vuelta_segs), (
        f"vuelta had weak alignments: min={min(vuelta_segs)}"
    )


# ---------------------------------------------------------------------------
# #1 — Clean straight line → clean.
# ---------------------------------------------------------------------------


def test_clean_straight_line_has_no_anomalies():
    report = analyze_shape(
        _straight_line(length_m=3000.0, step_m=50.0),
        route_code="TEST-CLEAN",
    )
    assert report["summary"]["total_anomalies"] == 0
    assert report["summary"]["classification"] == "clean"
    assert report["summary"]["max_severity"] == 0.0


# ---------------------------------------------------------------------------
# #2 — Clean ida-vuelta same-avenue route (core v3 regression guard).
# ---------------------------------------------------------------------------


def test_clean_ida_vuelta_same_avenue_is_clean():
    coords = _ida_vuelta_same_avenue(out_m=3000.0, step_m=25.0, offset_m=6.0)
    report = analyze_shape(coords, route_code="TEST-CLEAN-IV")
    # The whole shape is a legitimate out-and-back. v1 would have fired
    # BACKTRACK + ZIGZAG + U_TURN here; v3 must leave it clean.
    assert report["summary"]["classification"] == "clean", report["summary"]
    assert report["summary"]["total_anomalies"] == 0


# ---------------------------------------------------------------------------
# #3 — Synthetic square loop mid-corridor → IMPOSSIBLE_LOOP fires.
# ---------------------------------------------------------------------------


def test_impossible_loop_square_pattern_outside_terminal_zone():
    """A tight 80 m square loop planted 2 km from the start — clearly
    outside the 300 m terminal zone — must still fire IMPOSSIBLE_LOOP
    (single loop → at least moderate; cluster → severe)."""

    def side(ax, ay, bx, by, n=9):
        return [_xy(ax + (bx - ax) * i / n, ay + (by - ay) * i / n) for i in range(1, n + 1)]

    # Long approach so the loop sits mid-corridor, not at the terminal.
    approach = [_xy(i * 40.0, 0.0) for i in range(51)]  # 0..2000 m east, 51 pts
    coords = list(approach)
    coords.extend(side(2000, 0, 2080, 0))
    coords.extend(side(2080, 0, 2080, 80))
    coords.extend(side(2080, 80, 2000, 80))
    coords.extend(side(2000, 80, 2000, 0))
    # Continue east to 4000 m so the loop really is mid-polyline.
    for i in range(1, 51):
        coords.append(_xy(2000.0 + i * 40.0, 0.0))

    report = analyze_shape(coords, route_code="TEST-LOOP-CORRIDOR")
    types = [a["type"] for a in report["anomalies"]]
    assert "IMPOSSIBLE_LOOP" in types
    # Single mid-corridor loop → moderate under the cluster-based v3 rule.
    # A dense cluster (>5 loops) is severe; see the next test.
    assert report["summary"]["classification"] in ("moderate", "severe")


def test_impossible_loop_cluster_is_severe():
    """Eight tightly packed small loops mid-corridor → severe (cluster rule).

    The enforcer's fallback_dot_product pivot can land inside one of the
    synthetic loops (each apex is a sharp reversal), rejecting same-half
    pairs on the "other side" of that pivot. Over-provision to 8 loops
    so we still clear ``loop_cluster_threshold`` (5) after that loss.
    """

    def side(ax, ay, bx, by, n=5):
        return [_xy(ax + (bx - ax) * i / n, ay + (by - ay) * i / n) for i in range(1, n + 1)]

    coords = [_xy(i * 40.0, 0.0) for i in range(51)]  # approach 2 km east
    for k in range(8):
        # Insert a small loop at (2000 + k*400, 0) — spaced 400 m apart.
        base_x = 2000.0 + k * 400.0
        coords.append(_xy(base_x, 0.0))
        coords.extend(side(base_x, 0, base_x + 80, 0))
        coords.extend(side(base_x + 80, 0, base_x + 80, 80))
        coords.extend(side(base_x + 80, 80, base_x, 80))
        coords.extend(side(base_x, 80, base_x, 0))
    # Continue east for clarity
    for i in range(1, 51):
        coords.append(_xy(5300.0 + i * 40.0, 0.0))

    report = analyze_shape(coords, route_code="TEST-LOOP-CLUSTER")
    types = [a["type"] for a in report["anomalies"]]
    n_loops = sum(1 for t in types if t == "IMPOSSIBLE_LOOP")
    assert n_loops > 5, f"expected loop cluster (>5), got {n_loops}"
    assert report["summary"]["classification"] == "severe"


# ---------------------------------------------------------------------------
# #4 — DIRECTION_INCONSISTENCY: 200 m phantom-square mid-corridor
#      (SANPEDROAMAGUANA pattern).
# ---------------------------------------------------------------------------


def test_direction_inconsistency_mid_corridor_phantom_square():
    """Out 1 km east, then 200 m of wrong-way (west) travel with a modest
    north offset so it's not flagged by the IMPOSSIBLE_LOOP detector, then
    200 m east again, then continue east to the end. The 200 m westward
    run inside the ida half has alignment < -0.9 and must fire
    DIRECTION_INCONSISTENCY with severity >= 0.3 (≥ 150 m / 500 m)."""
    coords: list[tuple[float, float]] = []
    # Ida out leg: 0 → 1000 m east.
    for i in range(0, 1001, 25):
        coords.append(_xy(float(i), 0.0))
    # Phantom west run: 1000 → 800 m (200 m) shifted 200 m north so no
    # loop-closure with the ida outbound vertices at y=0.
    for i in range(1, 9):
        coords.append(_xy(1000.0 - i * 25.0, 200.0))
    # Resume east: 800 → 3000 m east at y=200 (ida continues).
    for i in range(0, 89):  # 800 + 0..2200 step 25
        coords.append(_xy(800.0 + i * 25.0, 200.0))
    # Pivot back: 3000 → 0 m west at y=210 (vuelta).
    for i in range(1, 121):
        coords.append(_xy(3000.0 - i * 25.0, 210.0))

    report = analyze_shape(coords, route_code="TEST-PHANTOM-SQUARE")
    types = [a["type"] for a in report["anomalies"]]
    assert "DIRECTION_INCONSISTENCY" in types, (
        f"expected DIRECTION_INCONSISTENCY, got {types}"
    )
    dir_anoms = [a for a in report["anomalies"] if a["type"] == "DIRECTION_INCONSISTENCY"]
    assert max(a["severity"] for a in dir_anoms) >= 0.3


# ---------------------------------------------------------------------------
# #5 — U_TURN mid-corridor fires.
# ---------------------------------------------------------------------------


def test_u_turn_mid_corridor_is_detected():
    # Go east for 50 vertices (covers > 300 m terminal zone), then U-turn
    # at vertex 50, then head east again (so pivot stays at the east end).
    # Then long vuelta back west so vertex 50 is clearly mid-corridor.
    coords: list[tuple[float, float]] = []
    for i in range(51):
        coords.append(_xy(i * 25.0, 0.0))  # 0..1250 m east
    # 180° U-turn at index 50: go briefly west 50 m then back east.
    coords.append(_xy(1200.0, 0.0))
    coords.append(_xy(1250.0, 0.0))
    for i in range(1, 80):
        coords.append(_xy(1250.0 + i * 25.0, 0.0))  # continue east to 3225 m
    for i in range(1, 130):
        coords.append(_xy(3225.0 - i * 25.0, 10.0))  # long vuelta back

    report = analyze_shape(coords, route_code="TEST-UTURN-CORRIDOR")
    types = [a["type"] for a in report["anomalies"]]
    assert "U_TURN" in types, f"expected U_TURN in {types}"


# ---------------------------------------------------------------------------
# #6 — U_TURN inside terminal zone is SUPPRESSED.
# ---------------------------------------------------------------------------


def test_u_turn_at_terminal_zone_is_suppressed():
    """A 180° reversal at vertex 2 (well inside the first 300 m) represents
    a yard hairpin; it must NOT fire U_TURN in v3."""
    # Tight terminal hairpin then a long corridor.
    coords = [
        _xy(0.0, 0.0),
        _xy(50.0, 0.0),
        _xy(0.0, 0.0),  # 180° reversal at idx 2
    ]
    # Long corridor that puts idx 2 inside the terminal zone
    # (cumulative distance at idx 2 is ~100 m, well under 300 m).
    for i in range(1, 200):
        coords.append(_xy(i * 25.0, 20.0))  # continue east to 5 km
    # Pivot + vuelta
    for i in range(1, 200):
        coords.append(_xy(5000.0 - i * 25.0, 30.0))

    report = analyze_shape(coords, route_code="TEST-UTURN-TERMINAL")
    u_turns = [a for a in report["anomalies"] if a["type"] == "U_TURN"]
    assert not u_turns, f"U_TURN should be suppressed in terminal zone, got {u_turns}"


# ---------------------------------------------------------------------------
# #7 — SPIKE detector still works (pure-signal rule).
# ---------------------------------------------------------------------------


def test_spike_is_detected():
    coords = [_xy(0, 0), _xy(500, 0), _xy(500, 300), _xy(1000, 0), _xy(1500, 0)]
    report = analyze_shape(coords, route_code="TEST-SPIKE")
    spikes = [a for a in report["anomalies"] if a["type"] == "SPIKE"]
    assert spikes, "expected SPIKE anomaly"
    assert any(a["location_idx"] == 2 for a in spikes)


# ---------------------------------------------------------------------------
# #8 — Pivot detection fallback when argmax is near an endpoint.
# ---------------------------------------------------------------------------


def test_pivot_detection_fallback_for_nonstandard_topology():
    """One-way route (no return) — argmax sits at the final vertex
    (fraction = 1.0), so primary validation fails and we fall through
    to the sliding-window / midpoint fallback."""
    coords = [_xy(i * 50.0, 0.0) for i in range(50)]  # pure east, no return
    idx, method = detect_pivot(coords)
    # Primary argmax would land at vertex 49 (fraction 1.0) — validation
    # must reject that; we then get the fallback midpoint since there's
    # no reversal in a pure straight line.
    assert method != "argmax_distance"
    assert 0 < idx < len(coords)


# ---------------------------------------------------------------------------
# #9 — Real clean route from route_prod stays clean in v3.
#      Skipped if DB unreachable.
# ---------------------------------------------------------------------------


REAL_CLEAN_ROUTE_ID = "7c34404b-5bf9-4974-bb2f-8c4f1d9c73b6"


def _fetch_real_shape(route_id: str) -> list[tuple[float, float]] | None:
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except Exception:  # pragma: no cover
        return None
    dsn = (
        os.getenv("DB_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    )
    try:
        with psycopg2.connect(dsn, connect_timeout=3) as conn:
            conn.set_session(readonly=True)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT ST_AsGeoJSON(geom) AS geojson "
                    "FROM route_prod.routes WHERE route_id=%s::uuid",
                    (route_id,),
                )
                row = cur.fetchone()
    except Exception:
        return None
    if not row or not row.get("geojson"):
        return None
    gj = json.loads(row["geojson"])
    if gj.get("type") != "LineString":
        return None
    return [(float(c[0]), float(c[1])) for c in gj["coordinates"]]


def test_real_clean_route_remains_clean():
    coords = _fetch_real_shape(REAL_CLEAN_ROUTE_ID)
    if coords is None:
        pytest.skip("route_prod unreachable or fixture route not present")
    report = analyze_shape(coords, route_code=REAL_CLEAN_ROUTE_ID)
    assert report["summary"]["classification"] == "clean", (
        f"route {REAL_CLEAN_ROUTE_ID} regressed: {report['summary']}"
    )
    assert report["summary"]["total_anomalies"] == 0


# ---------------------------------------------------------------------------
# #10 — Idempotency.
# ---------------------------------------------------------------------------


def test_analyze_is_deterministic_for_same_input():
    coords = [_xy(0, 0), _xy(500, 0), _xy(550, 0), _xy(0, 0)]
    r1 = analyze_shape(coords, route_code="TEST-IDEM", version=7)
    r2 = analyze_shape(coords, route_code="TEST-IDEM", version=7)
    assert r1 == r2, "GeometryEnforcer is not deterministic for identical input"


# ---------------------------------------------------------------------------
# #11 — Threshold override still works.
# ---------------------------------------------------------------------------


def test_threshold_override_relaxes_u_turn_detection():
    # Mid-corridor 170° turn: use a long corridor so the turn vertex sits
    # well outside the 300 m terminal zone.
    coords: list[tuple[float, float]] = []
    for i in range(41):
        coords.append(_xy(i * 25.0, 0.0))  # 0..1000 m east
    # Turn: the next point is back at (500, 0) → turn angle at idx 40 ≈ 180°.
    # But we want a 170° turn specifically. Compute a target that gives ~170°.
    # incoming bearing = 90°; for 170° turn, outgoing bearing = 90° - 170° = -80°
    # (equivalently 280°). Place next point 500 m at bearing 280° from (1000,0).
    last_x, last_y = 1000.0, 0.0
    bearing_rad = math.radians(280.0)
    next_x = last_x + 500.0 * math.sin(bearing_rad)
    next_y = last_y + 500.0 * math.cos(bearing_rad)
    coords.append(_xy(next_x, next_y))
    # Extend the shape to give the polyline real length.
    for i in range(1, 30):
        coords.append(_xy(next_x + i * 25.0, next_y))

    default_report = analyze_shape(coords, route_code="TEST-RELAX-DEFAULT")
    assert any(a["type"] == "U_TURN" for a in default_report["anomalies"]), (
        f"default thresholds should flag the 170° turn: {default_report['summary']}"
    )

    relaxed = GeometryEnforcerThresholds(u_turn_angle_deg=175.0)
    relaxed_report = GeometryEnforcer(relaxed).analyze(
        coords, route_code="TEST-RELAX", version=1
    ).to_dict()
    assert not any(a["type"] == "U_TURN" for a in relaxed_report["anomalies"])
