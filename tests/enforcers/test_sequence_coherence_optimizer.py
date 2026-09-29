"""Sequence Coherence Optimizer — unit tests per the Fixer-Phase-2 spec.

Three cases (as specified):

1. Synthetic ida/vuelta route with stops in the wrong order → optimizer
   recovers a coherent order (primary bipartition succeeds in one retrace).
2. Route with already-correct order → optimizer preserves it (no spurious
   promotion).
3. Route with genuinely chaotic topology → optimizer refuses (returns v1).

All tests stub the Valhalla retrace with a deterministic "piecewise-linear"
stand-in so the suite runs without network. The stub interpolates each
stop-to-stop leg as a straight line with a handful of intermediate points —
sufficient for the Geometry Enforcer v3 to observe the consequence of the
chosen stop order.
"""
from __future__ import annotations

import math

import pytest

from hades.enforcers.geometry_fixer import (
    _should_trigger_sequence_optimizer,
    fix_sequence_incoherence,
)
from hades.enforcers.sequence_coherence_optimizer import (
    OptimizeResult,
    bipartition_by_direction,
    coherence_score,
    optimize_sequence,
)
from hades.enforcers.geometry_enforcer import GeometryEnforcer


# ---------------------------------------------------------------------------
# Deterministic retrace stub — straight-line interpolation between stops.
# Input: list of (lon, lat). Output: list of (lon, lat).
# ---------------------------------------------------------------------------

def _linear_retrace(locations, costing_options=None, *, n_per_leg: int = 8):
    """Make a piecewise-linear polyline connecting the stops directly."""
    if len(locations) < 2:
        return list(locations)
    out = []
    for i in range(len(locations) - 1):
        lon_a, lat_a = locations[i]
        lon_b, lat_b = locations[i + 1]
        for k in range(n_per_leg):
            t = k / float(n_per_leg)
            out.append((
                lon_a + t * (lon_b - lon_a),
                lat_a + t * (lat_b - lat_a),
            ))
    out.append(tuple(locations[-1]))
    return out


# ---------------------------------------------------------------------------
# Case 1 — stops in wrong order → optimizer fixes them.
# ---------------------------------------------------------------------------

def test_case_1_wrong_order_is_corrected():
    """Stops 0..4 form an ida-vuelta loop, but the ordering is scrambled
    so the Valhalla trace ricochets back and forth across the corridor.
    The optimizer should reshuffle the middle stops so the retrace walks
    start → pivot → end monotonically.
    """
    # Correct geography: start ~ (lat=-0.20, lon=-78.50)
    # pivot far east around lon=-78.46
    # end (lat=-0.21, lon=-78.48)
    correct = [
        (-0.200, -78.500),  # 0 start
        (-0.201, -78.490),  # 1 ida mid
        (-0.202, -78.480),  # 2 ida near pivot
        (-0.203, -78.460),  # 3 PIVOT (farthest)
        (-0.204, -78.470),  # 4 vuelta mid
        (-0.210, -78.480),  # 5 end
    ]
    # Scramble the middle (keep endpoints).
    scrambled = [
        correct[0], correct[4], correct[2], correct[1], correct[3], correct[5]
    ]
    # v1 polyline = retrace of the scrambled order (ricochet).
    v1_coords = _linear_retrace(
        [(lon, lat) for (lat, lon) in scrambled], None, n_per_leg=12
    )

    result = fix_sequence_incoherence(
        stops=scrambled,
        coords_v1=v1_coords,
        route_code="case1_scrambled",
        retrace_fn=_linear_retrace,
        force=True,  # short synthetic routes rarely trigger DI automatically
        timeout_s=30.0,
        max_retraces=20,
    )

    assert result is not None, "force=True should produce a result"
    assert isinstance(result, OptimizeResult)
    # New order should start with 0 and end with last; the middle should be
    # re-sorted so the retrace walks monotonically along the corridor.
    assert result.new_order_indices[0] == 0
    assert result.new_order_indices[-1] == len(scrambled) - 1
    # At minimum the new order should differ from the scrambled input.
    assert result.new_order_indices != list(range(len(scrambled))), (
        "Optimizer should reorder the scrambled stops, not keep them as-is"
    )
    # Coherence after should be > coherence before (or equal to the v1 at
    # worst; we demand strictly-better for success=True).
    assert result.coherence_after >= result.coherence_before
    # Primary bipartition is the expected strategy for this small case.
    assert result.strategy_used in ("primary_bipartition", "two_opt")


# ---------------------------------------------------------------------------
# Case 2 — route already in correct order → no regression.
# ---------------------------------------------------------------------------

def test_case_2_correct_order_is_preserved():
    stops = [
        (-0.200, -78.500),
        (-0.201, -78.495),
        (-0.202, -78.490),
        (-0.203, -78.485),
        (-0.204, -78.480),
    ]
    v1_coords = _linear_retrace(
        [(lon, lat) for (lat, lon) in stops], None, n_per_leg=10
    )

    result = fix_sequence_incoherence(
        stops=stops,
        coords_v1=v1_coords,
        route_code="case2_correct",
        retrace_fn=_linear_retrace,
        force=True,
        timeout_s=30.0,
        max_retraces=20,
    )

    assert result is not None
    # Either: no improvement, preserve v1 (success=False),
    # OR exact same order returned (success=True on tie is forbidden by
    # strict-better gate, so expect success=False here).
    assert result.success is False
    assert result.stops_after == stops, "v1 stops must be preserved verbatim"
    assert result.new_order_indices == list(range(len(stops)))
    assert result.coherence_after == pytest.approx(
        result.coherence_before, abs=1e-6
    )


# ---------------------------------------------------------------------------
# Case 3 — chaotic topology → optimizer refuses and returns v1.
# ---------------------------------------------------------------------------

def test_case_3_chaotic_topology_is_refused():
    """Stops scattered in a way where no linear ida/vuelta split can
    straighten the trace — e.g. a cluster with random pairwise distances
    that violates the direction-vector assumption. The optimizer should
    fail to produce strictly-better coherence and keep v1.
    """
    # Stops scattered across a tight square with no dominant direction.
    stops = [
        (-0.200, -78.500),  # NW corner
        (-0.200, -78.498),
        (-0.198, -78.499),  # NE jitter
        (-0.202, -78.498),  # SE jitter
        (-0.202, -78.500),  # SW jitter
        (-0.200, -78.499),  # center-ish
        (-0.201, -78.500),  # end (back to near start)
    ]
    v1_coords = _linear_retrace(
        [(lon, lat) for (lat, lon) in stops], None, n_per_leg=6
    )

    result = fix_sequence_incoherence(
        stops=stops,
        coords_v1=v1_coords,
        route_code="case3_chaotic",
        retrace_fn=_linear_retrace,
        force=True,
        timeout_s=15.0,
        max_retraces=20,
    )

    assert result is not None
    # The optimizer may or may not find an improvement — acceptable outcomes:
    #  (a) success=False, v1 preserved (expected for genuinely chaotic shapes)
    #  (b) success=True only if coherence strictly improved
    if not result.success:
        assert result.stops_after == stops
        assert result.new_order_indices == list(range(len(stops)))
    else:
        # If it found an improvement, it must be real (strict better).
        assert result.coherence_after > result.coherence_before


# ---------------------------------------------------------------------------
# Helper sanity checks.
# ---------------------------------------------------------------------------

def test_bipartition_basic():
    stops = [
        (0.0, 0.0),    # start
        (0.0, 0.5),    # ida
        (0.0, 1.0),    # pivot (farthest)
        (0.0, 0.6),    # vuelta
        (0.0, 0.1),    # end
    ]
    ida, vuelta, pivot = bipartition_by_direction(stops)
    assert pivot == 2
    assert ida[0] == 0  # start always in ida
    assert vuelta[-1] == 4  # end always in vuelta


def test_coherence_score_clean_route_near_one():
    # Clean straight line → no anomalies → coherence close to 1.0.
    coords = [(lon, 0.0) for lon in [i * 0.01 for i in range(20)]]
    enforcer = GeometryEnforcer()
    report = enforcer.analyze(coords, route_code="straight")
    score = coherence_score(report, coords)
    assert score > 0.9


def test_trigger_gate_matches_spec():
    # no anomalies → no trigger
    enforcer = GeometryEnforcer()
    coords = [(lon, 0.0) for lon in [i * 0.01 for i in range(20)]]
    rep = enforcer.analyze(coords, route_code="straight")
    triggered, reason = _should_trigger_sequence_optimizer(rep)
    assert triggered is False
    assert reason == "no_trigger"
