"""Tests for hades.enforcers.phase3_coordinator.

Uses the same Quito-centred equirectangular grid as the stop coverage
tests so cross-reading is easy. Every test is fully hermetic: the
coordinator takes no DB and no network by default, and the DR queue
writer is injected so nothing hits disk.
"""
from __future__ import annotations

import math
from typing import Any

import pytest

from hades.enforcers.dr_budget_tracker import DRBudgetTracker
from hades.enforcers.phase3_coordinator import (
    EnforcerResult,
    RouteContext,
    run_phase3_enforcers,
)


LAT_0 = -0.18
LON_0 = -78.48
M_PER_DEG_LAT = 111_132.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT_0))


def _xy(x_m: float, y_m: float = 0.0) -> tuple[float, float]:
    return (LON_0 + x_m / M_PER_DEG_LON, LAT_0 + y_m / M_PER_DEG_LAT)


def _latlon(x_m: float, y_m: float = 0.0) -> tuple[float, float]:
    lon, lat = _xy(x_m, y_m)
    return (lat, lon)


# ---------------------------------------------------------------------------
# Clean happy path
# ---------------------------------------------------------------------------

def test_clean_route_auto_accepts():
    coords = [_xy(x) for x in range(0, 5001, 25)]
    stops = [_latlon(x) for x in range(150, 5000, 300)]
    ctx = RouteContext(
        route_code="T-CLEAN",
        coords=coords,
        stop_coords=stops,
        unit_id="unit-test",
    )

    sink: list[tuple[str, dict]] = []
    result = run_phase3_enforcers(
        ctx,
        policy_profile="balanced",
        dr_queue_writer=lambda uid, q: sink.append((uid, q)),
    )
    assert isinstance(result, EnforcerResult)
    assert result.policy_decision == "auto_accept"
    assert result.crashed is False
    geom_cls = (
        result.geometry_report.get("summary", {}).get("classification")
        or result.geometry_report.get("classification")
    )
    assert geom_cls == "clean"
    assert result.stop_coverage_report["classification"] in {"good", "acceptable"}
    assert sink == []  # clean route never queues DR


# ---------------------------------------------------------------------------
# Unroutable → queue; conservative profile never queues DR pipeline-side.
# ---------------------------------------------------------------------------

def test_conservative_unroutable_queues_and_skips_dr_pipeline():
    coords = [_xy(x) for x in range(0, 3001, 25)]
    # Zero stops → unroutable dead-head across the whole shape.
    ctx = RouteContext(
        route_code="T-EMPTY",
        coords=coords,
        stop_coords=[],
        unit_id="unit-conservative",
    )
    sink: list[tuple[str, dict]] = []
    result = run_phase3_enforcers(
        ctx,
        policy_profile="conservative",
        dr_queue_writer=lambda uid, q: sink.append((uid, q)),
    )
    assert result.policy_decision == "queue_for_approval"
    assert result.stop_coverage_report["classification"] == "unroutable"
    assert result.dr_queries_queued == []        # conservative skips pipeline DR
    assert sink == []                            # writer not called
    # Prepared queries from the enforcer, if any, are deferred for a reason.
    for q in result.dr_queries_deferred:
        assert q.get("deferred_reason", "").startswith("profile_conservative")


# ---------------------------------------------------------------------------
# Balanced profile routes DR through the budget + writer.
# ---------------------------------------------------------------------------

def test_balanced_degraded_queues_dr_via_writer():
    # Straight 3 km route with two stops at the very ends to force a big
    # middle gap — gives the enforcer a DR query to prepare.
    coords = [_xy(x) for x in range(0, 3001, 25)]
    stops = [_latlon(50.0), _latlon(2950.0)]
    ctx = RouteContext(
        route_code="T-GAPPY",
        coords=coords,
        stop_coords=stops,
        zone="urban_dense",
        unit_id="unit-balanced",
    )
    sink: list[tuple[str, dict]] = []
    tracker = DRBudgetTracker(
        unit_id="unit-balanced",
        max_queries_per_unit=15,
        max_per_route=2,
    )
    result = run_phase3_enforcers(
        ctx,
        policy_profile="balanced",
        unit_dr_budget=tracker,
        dr_queue_writer=lambda uid, q: sink.append((uid, q)),
    )
    # The single 2.9 km gap + ends is classified degraded or unroutable.
    assert result.stop_coverage_report["classification"] in {"degraded", "unroutable"}
    # Coordinator should have at least one DR query queued and written.
    assert len(result.dr_queries_queued) >= 1
    assert len(sink) == len(result.dr_queries_queued)
    assert result.budget_usage["total_queued"] == len(result.dr_queries_queued)
    assert result.budget_usage["unit_id"] == "unit-balanced"


# ---------------------------------------------------------------------------
# Per-route cap: tracker deferring extra queries is visible in the result.
# ---------------------------------------------------------------------------

def test_per_route_cap_defers_extras():
    coords = [_xy(x) for x in range(0, 5001, 25)]
    # Three widely-spaced stops create multiple big gaps on one route.
    stops = [_latlon(100.0), _latlon(2500.0), _latlon(4900.0)]
    ctx = RouteContext(
        route_code="T-MULTIGAP",
        coords=coords,
        stop_coords=stops,
        zone="urban_dense",
        unit_id="unit-cap",
    )
    # Force the cap low to provoke deferrals even on modest gap counts.
    tracker = DRBudgetTracker(
        unit_id="unit-cap", max_queries_per_unit=15, max_per_route=1
    )
    result = run_phase3_enforcers(
        ctx,
        policy_profile="balanced",
        unit_dr_budget=tracker,
        dr_queue_writer=lambda uid, q: None,
    )
    # At most one queued for this route; any extras must be deferred
    # with route_cap_reached.
    assert len(result.dr_queries_queued) <= 1
    cap_deferrals = [
        q for q in result.dr_queries_deferred
        if q.get("deferred_reason") == "route_cap_reached"
    ]
    # Only assert the invariant: if more than one DR query was prepared,
    # the extras must be cap-deferred.
    prepared = result.stop_coverage_report.get("dr_queries_prepared", [])
    if len(prepared) > 1:
        assert cap_deferrals, (
            "extra DR queries past per_route=1 must be deferred with cap reason"
        )


# ---------------------------------------------------------------------------
# Crash containment.
# ---------------------------------------------------------------------------

def test_crashing_writer_does_not_sink_promotion():
    """A blowing-up DR queue writer must not mask a policy decision."""
    coords = [_xy(x) for x in range(0, 3001, 25)]
    stops = [_latlon(50.0), _latlon(2950.0)]
    ctx = RouteContext(
        route_code="T-CRASH-WRITER",
        coords=coords,
        stop_coords=stops,
        zone="urban_dense",
        unit_id="unit-crash",
    )

    def boom(_uid: str, _q: dict) -> None:
        raise RuntimeError("pretend disk full")

    result = run_phase3_enforcers(
        ctx, policy_profile="balanced", dr_queue_writer=boom
    )
    # Writer failed → those queries land in deferred with a writer reason,
    # NOT in queued. Policy decision still returns cleanly.
    assert any(
        q.get("deferred_reason", "").startswith("dr_queue_writer_failed")
        for q in result.dr_queries_deferred
    )
    assert result.policy_decision in {"auto_accept", "queue_for_approval"}
    assert result.crashed is False  # writer-level crash != enforcer crash


def test_enforcer_crash_yields_queue_for_approval():
    """Crash inside an enforcer → coordinator returns queue_for_approval
    with ``crashed=True`` and the original route_code preserved.
    """
    # Pass bad input shape (non-iterable of pairs) — passes through to
    # the enforcer which will raise when indexing.
    class Explode:
        def __len__(self):
            raise RuntimeError("synthetic coord failure")

        def __iter__(self):
            raise RuntimeError("synthetic coord failure")

    ctx = RouteContext(
        route_code="T-BLOWUP",
        coords=Explode(),  # type: ignore[arg-type]
        stop_coords=[],
        unit_id="unit-crash",
    )
    result = run_phase3_enforcers(ctx, policy_profile="balanced")
    assert result.crashed is True
    assert result.policy_decision == "queue_for_approval"
    assert any(
        "enforcer_crashed" in r or r.startswith("exception:")
        for r in result.decision_reasons
    )
    assert result.crash_payload is not None
    assert result.crash_payload["exception_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Aggressive supervised skips pipeline DR even on degraded.
# ---------------------------------------------------------------------------

def test_aggressive_supervised_skips_pipeline_dr():
    coords = [_xy(x) for x in range(0, 3001, 25)]
    stops = [_latlon(50.0), _latlon(2950.0)]
    ctx = RouteContext(
        route_code="T-AGGRO",
        coords=coords,
        stop_coords=stops,
        zone="urban_dense",
        unit_id="unit-aggressive",
    )
    sink: list[Any] = []
    result = run_phase3_enforcers(
        ctx,
        policy_profile="aggressive_supervised",
        dr_queue_writer=lambda uid, q: sink.append(q),
    )
    assert result.dr_queries_queued == []
    assert sink == []


# ---------------------------------------------------------------------------
# Enhance mode — v1→v2 swap decisions never auto-accept.
# ---------------------------------------------------------------------------

def test_enhance_mode_forces_auto_accept_to_queue():
    """A route that would auto_accept under publish mode must queue
    under enhance mode — operators always confirm v1→v2 swaps visually.
    """
    coords = [_xy(x) for x in range(0, 5001, 25)]
    stops = [_latlon(x) for x in range(150, 5000, 300)]
    ctx = RouteContext(
        route_code="T-ENHANCE-CLEAN",
        coords=coords,
        stop_coords=stops,
        unit_id="unit-enhance",
    )

    publish = run_phase3_enforcers(
        ctx, policy_profile="balanced", dr_queue_writer=lambda uid, q: None
    )
    enhance = run_phase3_enforcers(
        ctx,
        policy_profile="balanced",
        mode="enhance",
        dr_queue_writer=lambda uid, q: None,
    )

    # Sanity: the fixture is the one that auto-accepts on publish.
    assert publish.policy_decision == "auto_accept"
    # Enhance mode collapses it to queue_for_approval with a forced flag.
    assert enhance.policy_decision == "queue_for_approval"
    assert enhance.policy_flags.get("mode") == "enhance"
    assert enhance.policy_flags.get("enhance_forced_queue") is True
    assert "enhance_mode_forced_queue" in enhance.decision_reasons
    # The underlying geometry / stop-coverage reports are unchanged.
    assert enhance.geometry_report == publish.geometry_report
    assert enhance.stop_coverage_report == publish.stop_coverage_report


def test_enhance_mode_preserves_reject_verdict():
    """reject_send_to_phase2 survives enhance-mode override. It signals
    the Fixer produced a worse v2 — the worker must NOT write it, and
    the policy engine's rejection is the right signal for that.
    """
    # Forge a severe geometry by reusing the injection pattern from the
    # 7b smoke — ida-vuelta with exact-chord loop at idx 38 ↔ idx 61.
    pivot_east = 2000.0
    def build() -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for m in range(0, int(pivot_east) + 1, 50):
            out.append(_xy(m))
        for d in range(10, 101, 10):
            out.append(_xy(pivot_east, d))
        for d in range(10, 101, 10):
            out.append(_xy(pivot_east - d, 100))
        out.append(_xy(pivot_east - 100, 0))  # exact (1900, 0)
        for m in range(2050, 5001, 50):
            out.append(_xy(m))
        for m in range(4950, -1, -50):
            out.append(_xy(m))
        return out

    coords = build()
    stops = [_latlon(x) for x in range(100, 4900, 600)]
    ctx = RouteContext(
        route_code="T-ENHANCE-SEVERE",
        coords=coords,
        stop_coords=stops,
        unit_id="unit-enhance-severe",
    )

    # Conservative rejects any severe, regardless of corridor tagging —
    # which is exactly what we want to test: enhance mode does NOT
    # convert reject to queue.
    result = run_phase3_enforcers(
        ctx,
        policy_profile="conservative",
        mode="enhance",
        dr_queue_writer=lambda uid, q: None,
    )
    assert result.policy_decision == "reject_send_to_phase2"
    assert result.policy_flags.get("mode") == "enhance"
    # The enhance_forced_queue flag must NOT appear on a rejection —
    # it would mislead the worker into writing a v2 that shouldn't land.
    assert result.policy_flags.get("enhance_forced_queue") is not True
    assert "enhance_mode_forced_queue" not in result.decision_reasons


def test_publish_mode_is_default_and_unchanged():
    """Backward compat: callers that don't pass mode still get publish.
    """
    coords = [_xy(x) for x in range(0, 5001, 25)]
    stops = [_latlon(x) for x in range(150, 5000, 300)]
    ctx = RouteContext(
        route_code="T-DEFAULT",
        coords=coords,
        stop_coords=stops,
        unit_id="unit-default",
    )

    default = run_phase3_enforcers(
        ctx, policy_profile="balanced", dr_queue_writer=lambda uid, q: None
    )
    explicit = run_phase3_enforcers(
        ctx,
        policy_profile="balanced",
        mode="publish",
        dr_queue_writer=lambda uid, q: None,
    )
    assert default.policy_decision == explicit.policy_decision == "auto_accept"
    assert default.policy_flags.get("mode") == "publish"
    assert explicit.policy_flags.get("mode") == "publish"
