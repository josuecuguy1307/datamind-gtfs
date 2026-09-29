"""Tests for the pre-ship orphan cleanup helpers exposed by reclassifier.

NOTE (2026-04-27): the auto-trigger was removed. The
``_should_trigger_preship_cleanup`` predicate is gone; cleanup is now
gated by the operator-confirmed pre-swap flow in
``re_entry_swap_service``. The ``_apply_preship_cleanup`` helper is
kept callable for the UI's on-demand button (until Commit 5 of the
trigger refactor retires that button) and is exercised below.

Two compat checks:
  * the deprecated ``with_preship_cleanup`` keyword on
    ``reclassify_all_pending`` emits ``DeprecationWarning`` and is a
    no-op
  * the deprecated ``with_preship_cleanup`` keyword on
    ``reclassify_routes_affected_by_batch`` does the same
"""
from __future__ import annotations

import json
import warnings
from typing import Any
from unittest.mock import patch

import pytest

from hades.enforcers.reclassifier import (
    _apply_preship_cleanup,
    reclassify_all_pending,
    reclassify_routes_affected_by_batch,
)


# ---------------------------------------------------------------------------
# Deprecated auto-trigger flag — must warn and stay a no-op.
# ---------------------------------------------------------------------------

def test_reclassify_all_pending_emits_deprecation_when_flag_true():
    """Passing with_preship_cleanup=True must warn and not raise."""
    with patch(
        "hades.enforcers.reclassifier.psycopg2.connect"
    ) as mock_connect:
        # Stub out the DB so we exercise the warning path without a
        # real connection.
        mock_conn = mock_connect.return_value.__enter__.return_value
        mock_cur = mock_conn.cursor.return_value.__enter__.return_value
        mock_cur.fetchall.return_value = []

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = reclassify_all_pending(with_preship_cleanup=True)

        depr = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert depr, "expected a DeprecationWarning"
        assert "preship_cleanup" in str(depr[0].message).lower()

    # Result honours the no-op contract: cleanup never happened.
    assert result["with_preship_cleanup"] is False


def test_reclassify_batch_emits_deprecation_when_flag_true():
    with patch(
        "hades.enforcers.reclassifier.psycopg2.connect"
    ) as mock_connect:
        mock_conn = mock_connect.return_value.__enter__.return_value
        mock_cur = mock_conn.cursor.return_value.__enter__.return_value
        mock_cur.fetchall.return_value = []

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = reclassify_routes_affected_by_batch(
                "batch_42_quito_centro",
                with_preship_cleanup=True,
                with_live_landmarks=False,
            )

        depr = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert depr, "expected a DeprecationWarning"

    assert result["with_preship_cleanup"] is False


# ---------------------------------------------------------------------------
# _apply_preship_cleanup — uses a fake psycopg2 connection.
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, log: list[tuple[str, tuple]]):
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: tuple = ()):
        self._log.append((sql, params))


class _FakeConn:
    def __init__(self):
        self.executions: list[tuple[str, tuple]] = []

    def cursor(self):
        return _FakeCursor(self.executions)


# East-west polyline at lat = -0.200 in Quito. Shared with the alignment
# tests so the same offsets produce predictable classifications.
SHAPE = {
    "type": "LineString",
    "coordinates": [[-78.500, -0.200], [-78.400, -0.200]],
}
M_PER_DEG_LAT = 111_132.0


def _lat_offset(meters: float) -> float:
    return meters / M_PER_DEG_LAT


def test_apply_writes_cleaned_stops_when_validation_passes():
    stops = [
        {"stop_id": f"s{i}", "lat": -0.200, "lon": -78.490 + 0.005 * i}
        for i in range(10)
    ]
    # 1 of 10 = 10% removal — under the 20% safety threshold.
    stops[0] = {
        "stop_id": "s0",
        "lat": -0.200 + _lat_offset(200.0),
        "lon": -78.490,
    }

    conn = _FakeConn()
    outcome = _apply_preship_cleanup("queue-1", stops, SHAPE, conn)

    assert outcome["outcome"] == "applied"
    assert outcome["stops_before"] == 10
    assert outcome["stops_after"] == 9
    assert outcome["stops_removed"] == 1
    assert "stops_snapped" in outcome

    # Single UPDATE to approval_queue with all four cleanup columns.
    assert len(conn.executions) == 1
    sql, params = conn.executions[0]
    assert "pre_ship_cleanup_applied   = TRUE" in sql
    assert "proposed_stops" in sql
    assert "pre_ship_cleanup_at        = NOW()" in sql

    # First param is JSONB-serialized cleaned stops (9 items).
    assert json.loads(params[0]) == [
        s for s in stops if s["stop_id"] != "s0"
    ]
    # Second param is JSONB report with validate_ok=True.
    report = json.loads(params[1])
    assert report["validate_ok"] is True
    assert report["stops_removed"] == 1


def test_apply_rejects_when_safety_gate_hits():
    # 2 of 4 stops are orphans → 50% removal, over the 20% threshold.
    stops = [
        {"stop_id": "a", "lat": -0.200, "lon": -78.450},
        {"stop_id": "b", "lat": -0.200, "lon": -78.440},
        {"stop_id": "c", "lat": -0.200 + _lat_offset(200.0), "lon": -78.430},
        {"stop_id": "d", "lat": -0.200 + _lat_offset(200.0), "lon": -78.420},
    ]
    conn = _FakeConn()
    outcome = _apply_preship_cleanup("queue-2", stops, SHAPE, conn)

    assert outcome["outcome"] == "rejected"
    assert "50%" in outcome["reason"]

    # One UPDATE — report only, no flip on applied.
    assert len(conn.executions) == 1
    sql, params = conn.executions[0]
    assert "pre_ship_cleanup_report = %s::jsonb" in sql
    assert "pre_ship_cleanup_applied" not in sql
    report = json.loads(params[0])
    assert report["validate_ok"] is False
    assert report["stops_removed"] == 2


def test_apply_skips_when_polyline_is_empty_or_short():
    stops = [{"stop_id": "a", "lat": -0.200, "lon": -78.450}]

    conn = _FakeConn()
    outcome = _apply_preship_cleanup(
        "queue-3", stops, {"type": "LineString", "coordinates": []}, conn,
    )
    assert outcome["outcome"] == "skipped_no_polyline"
    assert conn.executions == []

    outcome = _apply_preship_cleanup("queue-3b", stops, None, conn)
    assert outcome["outcome"] == "skipped_no_polyline"
    assert conn.executions == []


# Idempotency check via the removed trigger predicate is no longer
# applicable — cleanup is now gated by the operator-confirmed pre-swap
# flow in re_entry_swap_service, which does its own guard against
# already-applied rows. See test_re_entry_swap_*.py for that coverage.
