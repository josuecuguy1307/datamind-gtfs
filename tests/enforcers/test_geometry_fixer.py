"""Unit tests for hades.enforcers.geometry_fixer.

The Fixer is stateless and read-only on v1 — every test passes synthetic
coordinates to ``GeometryFixer.fix()`` and asserts on the returned
:class:`GeometryFixResult`. Shapes use a flat-earth metre grid centred on
Quito so that detector thresholds (which are in metres) line up with the
synthetic geometry.

Strict-better policy under test:
  - If v2 has fewer or lower-severity anomalies, the fixer emits v2
    (``success=True``, ``reason="improved"``).
  - If v2 would end up ≤ 2 vertices, the fixer refuses
    (``success=False``, ``reason="would_regress"``).
  - If v1 already has zero anomalies, the fixer never runs
    (``reason="no_anomalies"``).
  - Endpoints are preserved on every emitted v2.
"""

from __future__ import annotations

import math

import pytest

from hades.enforcers.geometry_enforcer import GeometryEnforcer
from hades.enforcers.geometry_fixer import GeometryFixer


# ---------------------------------------------------------------------------
# Flat-earth metre grid helpers (mirrors test_geometry_enforcer).
# ---------------------------------------------------------------------------

_LAT0 = -0.18
_LON0 = -78.48
_M_PER_DEG_LAT = 111_132.0
_M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(_LAT0))


def _xy(x_m: float, y_m: float) -> tuple[float, float]:
    return (_LON0 + x_m / _M_PER_DEG_LON, _LAT0 + y_m / _M_PER_DEG_LAT)


# ---------------------------------------------------------------------------
# Test 1 — SPIKE fix.
# ---------------------------------------------------------------------------

def test_fix_drops_single_spike_improves_report():
    """A single off-chord vertex is dropped; v2 is clean."""
    # Mostly-straight eastward shape, ~2 km long (clear of terminal zone).
    coords = [_xy(i * 50.0, 0.0) for i in range(41)]
    # Inject a ~500 m perpendicular spike at the middle.
    coords[20] = _xy(20 * 50.0, 500.0)

    result = GeometryFixer().fix(coords, route_code="TEST_SPIKE")

    assert result.success is True
    assert result.reason == "improved"
    assert 20 in result.dropped_indices
    # Spike must be gone; we may still have ~0 residual anomalies.
    assert not any(a.type == "SPIKE" for a in (result.report_after.anomalies or []))
    # Manifest records the spike fix.
    assert any(f["anomaly_type"] == "SPIKE" for f in result.fixes_applied)
    # v2 vertex count is exactly one less than v1.
    assert len(result.coords_after) == len(coords) - 1


# ---------------------------------------------------------------------------
# Test 2 — IMPOSSIBLE_LOOP (exact-chord repeat) fix via snip.
# ---------------------------------------------------------------------------

def test_fix_snips_exact_chord_repeat_loop():
    """A chord_m=0 loop in the middle of a monotonic run is snipped.

    The shape advances east in 100 m steps, executes a tight 500 m
    detour (west 250 m, east 250 m) in the middle, then continues east.
    The detour's final vertex lands exactly on a prior vertex → loop
    with chord_m=0.0, classified severe via ``loop_exact_repeat``.

    The eastward-monotonic spine and the detour's small span (500 m total,
    inside the 5-vertex fallback window) keep ``detect_pivot`` in
    ``fallback_midpoint`` mode, which disables the same-half filter and
    lets the loop fire.
    """
    east = [_xy(i * 100.0, 0.0) for i in range(15)]        # 0..14 → (1400, 0)
    detour = [_xy(1150.0, 0.0), _xy(1400.0, 0.0)]          # 15, 16
    east_tail = [_xy((15 + i) * 100.0, 0.0) for i in range(14)]  # 17..30
    coords = east + detour + east_tail

    # Sanity on v1: the enforcer must see a severe exact-chord loop here,
    # otherwise the test is vacuous.
    report_v1 = GeometryEnforcer().analyze(coords, route_code="TEST_LOOP")
    loop_exact = any(
        a.type == "IMPOSSIBLE_LOOP" and "chord_m=0.0" in a.context
        for a in report_v1.anomalies
    )
    assert loop_exact, (
        "Test precondition: synthetic loop must fire with chord_m=0.0; "
        f"got anomalies={[(a.type, a.context) for a in report_v1.anomalies]}"
    )
    assert report_v1.classification == "severe"

    result = GeometryFixer().fix(coords, route_code="TEST_LOOP", report_before=report_v1)

    assert result.success is True
    assert result.reason == "improved"
    # The apex vertex of the detour (15) should be dropped.
    assert 15 in result.dropped_indices
    assert any(
        f["anomaly_type"] == "IMPOSSIBLE_LOOP" for f in result.fixes_applied
    )
    # v2 must have strictly fewer loops than v1.
    v2_loops = [a for a in result.report_after.anomalies if a.type == "IMPOSSIBLE_LOOP"]
    v1_loops = [a for a in report_v1.anomalies if a.type == "IMPOSSIBLE_LOOP"]
    assert len(v2_loops) < len(v1_loops)


# ---------------------------------------------------------------------------
# Test 3 — DIRECTION_INCONSISTENCY snip.
# ---------------------------------------------------------------------------

def test_fix_snips_direction_inconsistency_run():
    """A backtracking run in the ida half is dropped; v2 has no DI."""
    # Ida: east 2.5 km with a 250 m westward backtrack in the middle (outside
    # terminal zone). Vuelta: straight west to origin with a tiny N offset.
    step = 50.0
    ida_out = [_xy(i * step, 0.0) for i in range(31)]         # (0..1500, 0)
    backtrack = [_xy(1500.0 - i * step, 0.0) for i in range(1, 6)]  # 250 m west
    ida_resume = [
        _xy(1250.0 + i * step, 0.0) for i in range(1, 31)     # (1300..2750, 0)
    ]
    vuelta = [
        _xy(2750.0 - i * step, 6.0) for i in range(1, 56)     # back to origin
    ]
    coords = ida_out + backtrack + ida_resume + vuelta

    report_v1 = GeometryEnforcer().analyze(coords, route_code="TEST_DI")
    di_before = [a for a in report_v1.anomalies if a.type == "DIRECTION_INCONSISTENCY"]
    assert di_before, (
        "Test precondition: synthetic backtrack must fire DI; "
        f"got anomalies={[(a.type, a.context) for a in report_v1.anomalies]}"
    )

    result = GeometryFixer().fix(coords, route_code="TEST_DI", report_before=report_v1)

    assert result.success is True
    assert result.reason == "improved"
    assert any(
        f["anomaly_type"] == "DIRECTION_INCONSISTENCY" for f in result.fixes_applied
    )
    di_after = [
        a for a in result.report_after.anomalies if a.type == "DIRECTION_INCONSISTENCY"
    ]
    assert len(di_after) < len(di_before)


# ---------------------------------------------------------------------------
# Test 4 — refusal when fix would leave too few vertices.
# ---------------------------------------------------------------------------

def test_fix_refuses_when_would_produce_fewer_than_three_vertices():
    """A 3-vertex shape whose middle is a severe SPIKE → would_regress.

    Dropping the spike leaves only 2 vertices (start + end), below the
    enforcer's minimum. The Fixer must refuse and return v1 unchanged
    with ``reason='would_regress'``.
    """
    coords = [_xy(0.0, 0.0), _xy(100.0, 500.0), _xy(200.0, 0.0)]

    result = GeometryFixer().fix(coords, route_code="TEST_REGRESS")

    assert result.success is False
    assert result.reason == "would_regress"
    # v1 returned unchanged.
    assert result.coords_after == coords
    # Drop set is recorded even though it was refused (for audit).
    assert 1 in result.dropped_indices


# ---------------------------------------------------------------------------
# Test 5 — endpoints preserved on every successful fix.
# ---------------------------------------------------------------------------

def test_fix_preserves_endpoints_on_success():
    """Every v2 must retain coords[0] and coords[-1] bit-exact."""
    # Reuse the spike scenario from test 1.
    coords = [_xy(i * 50.0, 0.0) for i in range(41)]
    coords[20] = _xy(20 * 50.0, 500.0)

    result = GeometryFixer().fix(coords, route_code="TEST_ENDPOINTS")

    assert result.success is True
    assert result.coords_after[0] == coords[0]
    assert result.coords_after[-1] == coords[-1]

    # And for the clean-no-op path: a straight line with no anomalies.
    clean = [_xy(i * 100.0, 0.0) for i in range(31)]
    no_op = GeometryFixer().fix(clean, route_code="TEST_CLEAN")
    assert no_op.success is False
    assert no_op.reason == "no_anomalies"
    assert no_op.coords_after[0] == clean[0]
    assert no_op.coords_after[-1] == clean[-1]
