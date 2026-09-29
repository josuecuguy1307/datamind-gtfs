"""Concurrency tests for confirm_swap_with_cleanup.

Real DB fixtures simulate the race-condition shapes that the GREEK
pipeline phase β fingerprint is supposed to catch:

* Concurrent DR response landing during operator decision time.
* Reclassifier run mid-flow.
* External UPDATE on proposed_stops between preview and confirm.
* Irrelevant-field changes that should NOT trigger a false abort.
* Idempotency: the same fingerprint cannot ship a row twice.

These are not mock tests — each test mutates the real
``route_prod.approval_queue`` row between preview and confirm and
asserts that the fingerprint mismatch is detected, no cleanup is
applied, and no swap rows land.
"""
from __future__ import annotations

import math
import os
import uuid
from typing import Any

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

from hades.enforcers import re_entry_swap_service as svc  # noqa: E402


# ---------------------------------------------------------------------------
# DSN / skip helpers — duplicated from test_re_entry_swap_service so this
# file does not depend on import-time test discovery order.
# ---------------------------------------------------------------------------

def _dsn() -> str:
    return os.environ.get("DB_DSN", "")


def _db_reachable() -> bool:
    try:
        conn = psycopg2.connect(_dsn(), connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(), reason="local datamind_ml DB not reachable"
)


# Reuse the shared fixture + helpers from the main swap_service test
# module so the data shape is identical.
from tests.enforcers.test_re_entry_swap_service import (  # noqa: E402
    OPERATOR_ID,
    OPERATOR_USERNAME,
    _flat_earth_xy,
    _insert_approval_queue,
    _set_quality_class,
    fresh_route,  # noqa: F401  (pytest fixture)
)


# ---------------------------------------------------------------------------
# Shared helpers.
# ---------------------------------------------------------------------------

def _insert_shippable(fresh_route: dict[str, Any]) -> str:
    """Insert a basic shippable approval_queue row at quality_class='good'."""
    route_id = fresh_route["route_id"]
    re_queue_id = fresh_route["re_queue_id"]
    stop_ids = fresh_route["stop_ids"]
    v2_coords = [
        _flat_earth_xy(0.0, 0.0),
        _flat_earth_xy(500.0, 0.0),
        _flat_earth_xy(1000.0, 0.0),
    ]
    v2_stops = [
        {"stop_id": stop_ids[0], "lat": v2_coords[0][1], "lon": v2_coords[0][0]},
        {"stop_id": stop_ids[1], "lat": v2_coords[2][1], "lon": v2_coords[2][0]},
    ]
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        approval_id = _insert_approval_queue(
            cur, route_id=route_id, re_entry_queue_id=re_queue_id,
            v2_coords=v2_coords, v2_stops=v2_stops, fix_category="structural",
        )
    conn.close()
    _set_quality_class(approval_id, "good")
    return approval_id


def _exec(sql: str, params: tuple) -> None:
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.close()


def _fetch_row(approval_id: str) -> dict[str, Any]:
    conn = psycopg2.connect(_dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT status, pre_ship_cleanup_applied, version "
            "  FROM route_prod.approval_queue "
            " WHERE queue_id = %s::uuid",
            (approval_id,),
        )
        out = dict(cur.fetchone())
    conn.close()
    return out


# ---------------------------------------------------------------------------
# Concurrency tests.
# ---------------------------------------------------------------------------

def test_concurrent_dr_response_during_preview_caught_at_confirm(fresh_route):
    """Concurrent DR response adds a pending batch between preview and
    confirm. Fingerprint mismatch must abort cleanly with no DB writes."""
    approval_id = _insert_shippable(fresh_route)

    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())
    captured = preview.state_fingerprint
    assert captured  # sanity

    # Simulate a DR response landing between preview and confirm: append
    # a new batch id to pending_dr_batches.
    _exec(
        "UPDATE route_prod.approval_queue "
        "   SET pending_dr_batches = ARRAY['dr_batch_concurrent']::text[] "
        " WHERE queue_id = %s::uuid",
        (approval_id,),
    )

    with pytest.raises(svc.SwapError, match="State changed since preview"):
        svc.confirm_swap_with_cleanup(
            approval_queue_id=approval_id,
            expected_fingerprint=captured,
            operator_id=OPERATOR_ID,
            operator_username=OPERATOR_USERNAME,
            dsn=_dsn(),
        )

    row = _fetch_row(approval_id)
    assert row["status"] == "pending"
    assert row["pre_ship_cleanup_applied"] is False


def test_concurrent_reclassifier_run_during_preview_caught(fresh_route):
    """Concurrent reclassifier flips quality_class from 'good' to
    'acceptable' between preview and confirm. Fingerprint must catch it."""
    approval_id = _insert_shippable(fresh_route)

    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())
    _set_quality_class(approval_id, "acceptable")

    with pytest.raises(svc.SwapError, match="State changed since preview"):
        svc.confirm_swap_with_cleanup(
            approval_queue_id=approval_id,
            expected_fingerprint=preview.state_fingerprint,
            operator_id=OPERATOR_ID,
            operator_username=OPERATOR_USERNAME,
            dsn=_dsn(),
        )

    row = _fetch_row(approval_id)
    assert row["status"] == "pending"
    assert row["pre_ship_cleanup_applied"] is False


def test_concurrent_proposed_stops_modification_caught(fresh_route):
    """External code edits proposed_stops between preview and confirm.
    Fingerprint must catch it."""
    approval_id = _insert_shippable(fresh_route)

    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())

    # Simulate an external UPDATE — for example a one-off SQL fix or a
    # parallel pipeline writing through.
    _exec(
        "UPDATE route_prod.approval_queue "
        "   SET proposed_stops = '[]'::jsonb "
        " WHERE queue_id = %s::uuid",
        (approval_id,),
    )

    with pytest.raises(svc.SwapError, match="State changed since preview"):
        svc.confirm_swap_with_cleanup(
            approval_queue_id=approval_id,
            expected_fingerprint=preview.state_fingerprint,
            operator_id=OPERATOR_ID,
            operator_username=OPERATOR_USERNAME,
            dsn=_dsn(),
        )

    row = _fetch_row(approval_id)
    assert row["status"] == "pending"
    assert row["pre_ship_cleanup_applied"] is False


def test_concurrent_irrelevant_field_modification_does_not_abort(fresh_route):
    """Mutating fields outside the hash (classified_at,
    resolution_notes) must NOT trigger a false abort."""
    approval_id = _insert_shippable(fresh_route)

    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())

    _exec(
        "UPDATE route_prod.approval_queue "
        "   SET classified_at = NOW(), "
        "       resolution_notes = 'noise from monitoring' "
        " WHERE queue_id = %s::uuid",
        (approval_id,),
    )

    result = svc.confirm_swap_with_cleanup(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )

    assert result.action == "approved"
    assert result.cleanup_applied is True

    row = _fetch_row(approval_id)
    assert row["status"] == "approved"
    assert row["pre_ship_cleanup_applied"] is True


def test_fingerprint_idempotency_for_no_state_change(fresh_route):
    """Two confirms with the same fingerprint: first succeeds, second
    rejects (the row's status is no longer 'pending', or the cleaned
    proposed_stops produce a different fingerprint than the original)."""
    approval_id = _insert_shippable(fresh_route)

    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())
    fp = preview.state_fingerprint

    r1 = svc.confirm_swap_with_cleanup(
        approval_queue_id=approval_id,
        expected_fingerprint=fp,
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )
    assert r1.action == "approved"

    # Second call must fail — either because cleanup rewrote
    # proposed_stops (fingerprint diverges) or because the swap blocker
    # check refuses non-pending status. Both are acceptable.
    with pytest.raises(svc.SwapError):
        svc.confirm_swap_with_cleanup(
            approval_queue_id=approval_id,
            expected_fingerprint=fp,
            operator_id=OPERATOR_ID,
            operator_username=OPERATOR_USERNAME,
            dsn=_dsn(),
        )
