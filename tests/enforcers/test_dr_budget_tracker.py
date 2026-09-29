"""Tests for hades.enforcers.dr_budget_tracker."""
from __future__ import annotations

import pytest

from hades.enforcers.dr_budget_tracker import DRBudgetTracker


def _gap(gap_m: float = 1500.0, zone: str = "urban_dense", idx: int = 0) -> dict:
    return {
        "idx": idx,
        "gap_m": gap_m,
        "zone": zone,
        "midpoint_coord": [-0.18, -78.48],
    }


def _query(route_code: str = "R-X", gap_idx: int = 0, zone: str = "urban_dense") -> dict:
    return {
        "route_code": route_code,
        "gap_idx": gap_idx,
        "zone": zone,
        "gap_m": 1500.0,
        "midpoint_lat": -0.18,
        "midpoint_lon": -78.48,
        "prompt_stub": "stub",
    }


def test_short_gap_denied():
    t = DRBudgetTracker("u1")
    ok, reason = t.should_queue_dr(_gap(gap_m=800), route_code="R-A")
    assert ok is False
    assert reason.startswith("gap_below_min")


def test_rural_zone_denied():
    t = DRBudgetTracker("u1")
    ok, reason = t.should_queue_dr(_gap(zone="rural"), route_code="R-A")
    assert ok is False
    assert reason.startswith("zone_not_allowed")


def test_per_route_cap():
    t = DRBudgetTracker("u1", max_per_route=2, max_queries_per_unit=10)
    for i in range(2):
        ok, _ = t.should_queue_dr(_gap(idx=i), route_code="R-X")
        assert ok
        t.record_queue(_gap(idx=i), _query(gap_idx=i), route_code="R-X")
    ok, reason = t.should_queue_dr(_gap(idx=2), route_code="R-X")
    assert ok is False
    assert reason == "route_cap_reached"


def test_unit_budget_exhausted():
    t = DRBudgetTracker("u1", max_per_route=1, max_queries_per_unit=2)
    for rc in ("R-A", "R-B"):
        ok, _ = t.should_queue_dr(_gap(), route_code=rc)
        assert ok
        t.record_queue(_gap(), _query(route_code=rc), route_code=rc)
    ok, reason = t.should_queue_dr(_gap(), route_code="R-C")
    assert ok is False
    assert reason == "unit_budget_exhausted"


def test_record_without_gate_raises():
    t = DRBudgetTracker("u1", max_per_route=1, max_queries_per_unit=1)
    t.record_queue(_gap(), _query(), route_code="R-A")
    with pytest.raises(RuntimeError):
        t.record_queue(_gap(), _query(), route_code="R-A")


def test_usage_reports_remaining():
    t = DRBudgetTracker("u1", max_queries_per_unit=5)
    t.record_queue(_gap(), _query(), route_code="R-A")
    usage = t.get_unit_usage()
    assert usage["total_queued"] == 1
    assert usage["remaining"] == 4
    assert usage["per_route"] == {"R-A": 1}
    assert usage["unit_id"] == "u1"
