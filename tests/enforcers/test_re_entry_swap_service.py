"""Integration tests for hades.enforcers.re_entry_swap_service.

Each test inserts a synthetic legacy route, a matching
``re_entry_queue`` row (status=``v2_ready``), and an
``approval_queue`` row (status=``pending``) with a concrete v2
proposal. It calls the service under test, asserts on every
side-effect surface (routes, routes_audit, fix_reports, approval_queue,
re_entry_queue, node_prod.nodes), and then cleans up.

These tests require the local ``datamind_ml`` Postgres to be
reachable; they are skipped otherwise.
"""
from __future__ import annotations

import json
import math
import os
import uuid
from typing import Any

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

from hades.enforcers import re_entry_swap_service as svc  # noqa: E402


# ---------------------------------------------------------------------------
# DSN / skip helpers.
# ---------------------------------------------------------------------------

def _dsn() -> str:
    return os.environ.get("DB_DSN", "")


def _db_reachable() -> bool:
    # integration test: only with DB_DSN and DATAMIND_RUN_DB_TESTS=1 (assumes a populated database)
    if not (os.environ.get("DB_DSN") and os.environ.get("DATAMIND_RUN_DB_TESTS") == "1"):
        return False
    try:
        conn = psycopg2.connect(_dsn(), connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(), reason="integration test: requires DB_DSN and DATAMIND_RUN_DB_TESTS=1"
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------

OPERATOR_ID = "00000000-0000-4000-8000-000000000001"  # operador de prueba
OPERATOR_USERNAME = "swap_service_test"


def _flat_earth_xy(x_m: float, y_m: float) -> tuple[float, float]:
    lat0 = -0.18
    lon0 = -78.48
    m_per_deg_lat = 111_132.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
    return (lon0 + x_m / m_per_deg_lon, lat0 + y_m / m_per_deg_lat)


def _insert_test_stop(cur, lat: float, lon: float) -> str:
    cur.execute(
        """
        INSERT INTO node_prod.nodes
            (node_id, geom, node_type, tag_kind, source, province, confidence, chosen_tags)
        VALUES
            (gen_random_uuid(),
             ST_SetSRID(ST_MakePoint(%s, %s), 4326),
             'STOP', 'bus_stop', 'swap_test', 'sample_region', 0.9, '{}'::jsonb)
        RETURNING node_id::text
        """,
        (float(lon), float(lat)),
    )
    return str(cur.fetchone()[0])


def _insert_grandfathered_route(
    cur, stop_ids: list[str], coords: list[tuple[float, float]]
) -> str:
    wkt = "LINESTRING(" + ", ".join(f"{lon} {lat}" for (lon, lat) in coords) + ")"
    route_uuid = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO route_raw.route_jobs (route_id, status, created_by, province)
        VALUES (%s::uuid, 'active', 'swap_service_test', 'sample_region')
        """,
        (route_uuid,),
    )
    cur.execute(
        """
        INSERT INTO route_prod.routes
            (route_id, geom, stop_node_ids, source, source_type, province,
             legacy_grandfathered, grandfathered_until, version)
        VALUES
            (%s::uuid,
             ST_SetSRID(ST_GeomFromText(%s), 4326),
             %s::uuid[],
             'route_constructor', 'manual_constructor', 'sample_region',
             TRUE, '2026-12-31'::timestamptz, 1)
        RETURNING route_id::text
        """,
        (route_uuid, wkt, stop_ids),
    )
    return str(cur.fetchone()[0])


def _insert_re_entry_queue(cur, route_id: str) -> str:
    cur.execute(
        """
        INSERT INTO route_prod.re_entry_queue
            (route_id, current_version, priority, classification, status)
        VALUES (%s::uuid, 1, 50, 'manual_constructor_legacy', 'v2_ready')
        RETURNING queue_id::text
        """,
        (route_id,),
    )
    return str(cur.fetchone()[0])


def _insert_approval_queue(
    cur,
    *,
    route_id: str,
    re_entry_queue_id: str,
    v2_coords: list[tuple[float, float]],
    v2_stops: list[dict[str, Any]],
    fix_category: str,
) -> str:
    proposed_shape = {
        "type": "LineString",
        "coordinates": [[lon, lat] for (lon, lat) in v2_coords],
    }
    policy_flags = {
        "mode": "enhance",
        "enhance_forced_queue": True,
        "re_entry_queue_id": re_entry_queue_id,
        "re_entry_classification": "manual_constructor_legacy",
        "fix_category": fix_category,
        "v1_n_coords": 3,
        "v2_n_coords": len(v2_coords),
        "v1_n_stops": 2,
        "v2_n_stops": len(v2_stops),
        "geom_fix_reason": "improved",
        "cov_fix_reason": "improved",
    }
    geometry_report = {
        "classification": "minor",
        "max_severity": 0.2,
        "anomalies": [],
    }
    stop_coverage_report = {
        "classification": "acceptable",
        "summary": {"n_gaps_total": 0, "tier_usage": {}},
        "n_gaps_unresolved": 0,
    }
    cur.execute(
        """
        INSERT INTO route_prod.approval_queue
            (route_code, version, policy_profile, decision_reasons,
             policy_flags, geometry_report, stop_coverage_report,
             proposed_stops, proposed_shape)
        VALUES (%s, 2, 'balanced', %s::jsonb, %s::jsonb,
                %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb)
        RETURNING queue_id::text
        """,
        (
            route_id,
            json.dumps(["enhance_mode_forced_queue"]),
            json.dumps(policy_flags),
            json.dumps(geometry_report),
            json.dumps(stop_coverage_report),
            json.dumps(v2_stops),
            json.dumps(proposed_shape),
        ),
    )
    return str(cur.fetchone()[0])


def _cleanup(cur, route_id: str, stop_ids: list[str]) -> None:
    # GREEK pipeline tests can leave rows in route_prod.refill_audit;
    # those reference approval_queue.queue_id and there is no FK cascade,
    # so wipe them by route_code BEFORE we drop the approval_queue rows.
    cur.execute(
        "DELETE FROM route_prod.refill_audit WHERE route_code = %s",
        (route_id,),
    )
    cur.execute(
        "DELETE FROM route_prod.approval_queue WHERE route_code = %s",
        (route_id,),
    )
    cur.execute(
        "DELETE FROM route_prod.fix_reports WHERE route_id = %s::uuid",
        (route_id,),
    )
    cur.execute(
        "DELETE FROM route_prod.routes_audit WHERE route_id = %s::uuid",
        (route_id,),
    )
    # re_entry_queue CASCADEs on route delete.
    cur.execute(
        "DELETE FROM route_prod.routes WHERE route_id = %s::uuid",
        (route_id,),
    )
    cur.execute(
        "DELETE FROM route_raw.route_jobs WHERE route_id = %s::uuid",
        (route_id,),
    )
    # Delete the synthetic-inserted nodes (from the swap) and the
    # fixture's own stop nodes together.
    if stop_ids:
        cur.execute(
            "DELETE FROM node_prod.nodes WHERE node_id = ANY(%s::uuid[])",
            (stop_ids,),
        )
    cur.execute(
        """
        DELETE FROM node_prod.nodes
         WHERE source IN ('swap_test','re_entry_swap')
           AND synthetic_created_by IN ('swap_service_test')
        """
    )


@pytest.fixture
def fresh_route():
    """Build a synthetic route + stops + queue rows. Tear down after."""
    conn = psycopg2.connect(_dsn())
    conn.autocommit = False
    created: dict[str, Any] = {}
    try:
        with conn.cursor() as cur:
            # Two real stops at x=0 and x=1000 on the Quito grid.
            s1_lon, s1_lat = _flat_earth_xy(0.0, 0.0)
            s2_lon, s2_lat = _flat_earth_xy(1000.0, 0.0)
            stop_ids = [
                _insert_test_stop(cur, lat=s1_lat, lon=s1_lon),
                _insert_test_stop(cur, lat=s2_lat, lon=s2_lon),
            ]
            # v1 coords: a 3-vertex corridor.
            v1_coords = [
                _flat_earth_xy(0.0, 0.0),
                _flat_earth_xy(500.0, 0.0),
                _flat_earth_xy(1000.0, 0.0),
            ]
            route_id = _insert_grandfathered_route(cur, stop_ids, v1_coords)
            re_queue_id = _insert_re_entry_queue(cur, route_id)
            created.update({
                "route_id": route_id,
                "stop_ids": stop_ids,
                "re_queue_id": re_queue_id,
                "v1_coords": v1_coords,
            })
        conn.commit()

        yield {"conn": conn, **created}

    finally:
        try:
            with conn.cursor() as cur:
                _cleanup(cur, created.get("route_id", "00000000-0000-0000-0000-000000000000"), created.get("stop_ids", []))
            conn.commit()
        except Exception:
            conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# APPROVE — happy path.
# ---------------------------------------------------------------------------

def test_approve_v2_swaps_in_place_and_transitions_queues(fresh_route):
    route_id = fresh_route["route_id"]
    re_queue_id = fresh_route["re_queue_id"]
    stop_ids = fresh_route["stop_ids"]

    # v2: shift midpoint + add a tier-5 synthetic fill between x=500 and x=1000.
    v2_coords = [
        _flat_earth_xy(0.0, 0.0),
        _flat_earth_xy(500.0, 5.0),
        _flat_earth_xy(1000.0, 0.0),
    ]
    synth_lon, synth_lat = _flat_earth_xy(750.0, 0.0)
    v2_stops = [
        {"stop_id": stop_ids[0], "lat": _flat_earth_xy(0.0, 0.0)[1],
         "lon": _flat_earth_xy(0.0, 0.0)[0]},
        {"stop_id": stop_ids[1], "lat": _flat_earth_xy(1000.0, 0.0)[1],
         "lon": _flat_earth_xy(1000.0, 0.0)[0]},
        {"stop_id": "fix_gap0_tier5", "lat": synth_lat, "lon": synth_lon},
    ]

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        approval_id = _insert_approval_queue(
            cur,
            route_id=route_id,
            re_entry_queue_id=re_queue_id,
            v2_coords=v2_coords,
            v2_stops=v2_stops,
            fix_category="structural",
        )
    conn.close()

    result = svc.approve_v2(
        approval_queue_id=approval_id,
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )

    assert result.action == "approved"
    assert result.version_before == 1
    assert result.version_after == 2
    assert result.fix_report_id is not None
    assert len(result.created_synthetic_node_ids) == 1  # the fix_gap id

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT version, last_swap_at, last_swap_from_version,
                   legacy_grandfathered, grandfathered_until, pipeline_version,
                   array_length(stop_node_ids, 1) AS n_stops
              FROM route_prod.routes WHERE route_id = %s::uuid
            """,
            (route_id,),
        )
        row = dict(cur.fetchone())
        assert row["version"] == 2
        assert row["last_swap_at"] is not None
        assert row["last_swap_from_version"] == 1
        assert row["legacy_grandfathered"] is False
        assert row["grandfathered_until"] is None
        assert row["pipeline_version"] == "re_entry_v1"
        assert row["n_stops"] == 3

        cur.execute(
            "SELECT status FROM route_prod.approval_queue WHERE queue_id = %s::uuid",
            (approval_id,),
        )
        assert dict(cur.fetchone())["status"] == "approved"

        cur.execute(
            "SELECT status FROM route_prod.re_entry_queue WHERE queue_id = %s::uuid",
            (re_queue_id,),
        )
        assert dict(cur.fetchone())["status"] == "swapped"

        cur.execute(
            "SELECT COUNT(*) AS n FROM route_prod.fix_reports WHERE route_id = %s::uuid",
            (route_id,),
        )
        assert dict(cur.fetchone())["n"] == 1

        cur.execute(
            """
            SELECT COUNT(*) AS n FROM route_prod.routes_audit
             WHERE route_id = %s::uuid AND action = 'UPDATE'
            """,
            (route_id,),
        )
        assert dict(cur.fetchone())["n"] >= 1

        cur.execute(
            """
            SELECT node_id::text, synthetic_created_by
              FROM node_prod.nodes
             WHERE node_id = %s::uuid
            """,
            (result.created_synthetic_node_ids[0],),
        )
        node = dict(cur.fetchone())
        assert node["synthetic_created_by"] == OPERATOR_USERNAME
    conn.close()


# ---------------------------------------------------------------------------
# APPROVE — guard rails.
# ---------------------------------------------------------------------------

def test_approve_v2_rejects_wrong_approval_status(fresh_route):
    route_id = fresh_route["route_id"]
    re_queue_id = fresh_route["re_queue_id"]
    v2_coords = [_flat_earth_xy(0.0, 0.0), _flat_earth_xy(1000.0, 0.0)]
    v2_stops = [
        {"stop_id": fresh_route["stop_ids"][0],
         "lat": v2_coords[0][1], "lon": v2_coords[0][0]},
        {"stop_id": fresh_route["stop_ids"][1],
         "lat": v2_coords[1][1], "lon": v2_coords[1][0]},
    ]
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        approval_id = _insert_approval_queue(
            cur,
            route_id=route_id,
            re_entry_queue_id=re_queue_id,
            v2_coords=v2_coords,
            v2_stops=v2_stops,
            fix_category="geometry",
        )
        cur.execute(
            "UPDATE route_prod.approval_queue SET status='rejected', "
            "resolved_at=NOW(), resolved_by='test', resolution_notes='x' "
            "WHERE queue_id=%s::uuid",
            (approval_id,),
        )
    conn.close()

    # Trigger refactor (2026-04-27): wrong-status now surfaces via the
    # preview's swap_blockers rather than a direct status check, so the
    # message format changed slightly. Match either form.
    with pytest.raises(
        svc.SwapError, match=r"approval_queue\.?status"
    ):
        svc.approve_v2(
            approval_queue_id=approval_id,
            operator_id=OPERATOR_ID,
            operator_username=OPERATOR_USERNAME,
            dsn=_dsn(),
        )


# ---------------------------------------------------------------------------
# REJECT — queue bounces to pending.
# ---------------------------------------------------------------------------

def test_reject_v2_resets_re_entry_queue_to_pending(fresh_route):
    route_id = fresh_route["route_id"]
    re_queue_id = fresh_route["re_queue_id"]
    v2_coords = [_flat_earth_xy(0.0, 0.0), _flat_earth_xy(1000.0, 0.0)]
    v2_stops = [
        {"stop_id": fresh_route["stop_ids"][0],
         "lat": v2_coords[0][1], "lon": v2_coords[0][0]},
    ]
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        approval_id = _insert_approval_queue(
            cur,
            route_id=route_id,
            re_entry_queue_id=re_queue_id,
            v2_coords=v2_coords,
            v2_stops=v2_stops,
            fix_category="geometry",
        )
        # Simulate a previous attempts count so we can assert the reset.
        cur.execute(
            "UPDATE route_prod.re_entry_queue SET attempts=2, last_error='prev' "
            "WHERE queue_id=%s::uuid",
            (re_queue_id,),
        )
    conn.close()

    svc.reject_v2(
        approval_queue_id=approval_id,
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        reason="v2 misaligned with real corridor",
        dsn=_dsn(),
    )

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT status, resolution_notes FROM route_prod.approval_queue "
            "WHERE queue_id=%s::uuid",
            (approval_id,),
        )
        row = dict(cur.fetchone())
        assert row["status"] == "rejected"
        assert "[REJECTED]" in row["resolution_notes"]

        cur.execute(
            "SELECT status, attempts, last_error FROM route_prod.re_entry_queue "
            "WHERE queue_id=%s::uuid",
            (re_queue_id,),
        )
        row = dict(cur.fetchone())
        assert row["status"] == "pending"
        assert row["attempts"] == 0
        assert row["last_error"] is None

        cur.execute(
            "SELECT version, last_swap_at FROM route_prod.routes "
            "WHERE route_id=%s::uuid",
            (route_id,),
        )
        row = dict(cur.fetchone())
        # No swap happened.
        assert row["version"] == 1
        assert row["last_swap_at"] is None
    conn.close()


# ---------------------------------------------------------------------------
# QUARANTINE — queue locked out of auto-retry.
# ---------------------------------------------------------------------------

def test_quarantine_v2_locks_out_worker(fresh_route):
    route_id = fresh_route["route_id"]
    re_queue_id = fresh_route["re_queue_id"]
    v2_coords = [_flat_earth_xy(0.0, 0.0), _flat_earth_xy(1000.0, 0.0)]
    v2_stops = [
        {"stop_id": fresh_route["stop_ids"][0],
         "lat": v2_coords[0][1], "lon": v2_coords[0][0]},
    ]
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        approval_id = _insert_approval_queue(
            cur,
            route_id=route_id,
            re_entry_queue_id=re_queue_id,
            v2_coords=v2_coords,
            v2_stops=v2_stops,
            fix_category="stop_coverage",
        )
    conn.close()

    svc.quarantine_v2(
        approval_queue_id=approval_id,
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        reason="needs hand-authored shape",
        dsn=_dsn(),
    )

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT status, resolution_notes FROM route_prod.approval_queue "
            "WHERE queue_id=%s::uuid",
            (approval_id,),
        )
        row = dict(cur.fetchone())
        assert row["status"] == "rejected"
        assert "[QUARANTINED]" in row["resolution_notes"]

        cur.execute(
            "SELECT status, last_error FROM route_prod.re_entry_queue "
            "WHERE queue_id=%s::uuid",
            (re_queue_id,),
        )
        row = dict(cur.fetchone())
        assert row["status"] == "quarantined"
        assert "needs hand-authored shape" in (row["last_error"] or "")
    conn.close()


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------

def test_is_uuid_accepts_valid_and_rejects_junk():
    assert svc._is_uuid("00000000-0000-4000-8000-000000000001")
    assert not svc._is_uuid("fix_gap0_tier5")
    assert not svc._is_uuid("")
    assert not svc._is_uuid(None)


# ---------------------------------------------------------------------------
# preview_swap_with_cleanup + confirm_swap_with_cleanup
# (trigger refactor 2026-04-27)
# ---------------------------------------------------------------------------

def _set_quality_class(approval_id: str, cls: str, *, pending_dr=None) -> None:
    """Mutate the just-inserted approval_queue row so cleanup-eligibility
    tests can exercise different code paths."""
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE route_prod.approval_queue
               SET quality_class = %s,
                   pending_dr_batches = %s::text[]
             WHERE queue_id = %s::uuid
            """,
            (cls, list(pending_dr or []), approval_id),
        )
    conn.close()


def test_preview_returns_dry_run_report_for_shippable_route(fresh_route):
    """Preview computes the cleanup math without writing anything."""
    route_id = fresh_route["route_id"]
    re_queue_id = fresh_route["re_queue_id"]
    stop_ids = fresh_route["stop_ids"]
    v2_coords = [
        _flat_earth_xy(0.0, 0.0),
        _flat_earth_xy(500.0, 0.0),
        _flat_earth_xy(1000.0, 0.0),
    ]
    # Both stops are exactly on the polyline → all aligned.
    v2_stops = [
        {"stop_id": stop_ids[0], "lat": v2_coords[0][1], "lon": v2_coords[0][0]},
        {"stop_id": stop_ids[1], "lat": v2_coords[2][1], "lon": v2_coords[2][0]},
    ]
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        approval_id = _insert_approval_queue(
            cur,
            route_id=route_id,
            re_entry_queue_id=re_queue_id,
            v2_coords=v2_coords,
            v2_stops=v2_stops,
            fix_category="structural",
        )
    conn.close()
    _set_quality_class(approval_id, "good")

    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())

    assert preview.cleanup_eligible is True
    assert preview.would_apply is True
    assert preview.safety_gate_status == "pass"
    assert preview.cleanup_report is not None
    assert preview.cleanup_report["stops_aligned"] == 2
    assert preview.cleanup_report["stops_removed"] == 0
    assert preview.swap_blockers == []

    # Verify NO writes happened — applied flag still FALSE.
    conn = psycopg2.connect(_dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT pre_ship_cleanup_applied FROM route_prod.approval_queue "
            "WHERE queue_id = %s::uuid",
            (approval_id,),
        )
        assert cur.fetchone()["pre_ship_cleanup_applied"] is False
    conn.close()


def test_preview_blocks_unfit_class(fresh_route):
    """Non-shippable class → cleanup_eligible=False, but swap can still proceed."""
    route_id = fresh_route["route_id"]
    re_queue_id = fresh_route["re_queue_id"]
    stop_ids = fresh_route["stop_ids"]
    v2_coords = [_flat_earth_xy(0.0, 0.0), _flat_earth_xy(1000.0, 0.0)]
    v2_stops = [
        {"stop_id": stop_ids[0], "lat": v2_coords[0][1], "lon": v2_coords[0][0]},
        {"stop_id": stop_ids[1], "lat": v2_coords[1][1], "lon": v2_coords[1][0]},
    ]
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        approval_id = _insert_approval_queue(
            cur, route_id=route_id, re_entry_queue_id=re_queue_id,
            v2_coords=v2_coords, v2_stops=v2_stops, fix_category="geometry",
        )
    conn.close()
    _set_quality_class(approval_id, "degraded")

    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())

    assert preview.cleanup_eligible is False
    assert "not shippable" in (preview.cleanup_skip_reason or "")
    assert preview.swap_blockers == []  # swap is still allowed


def test_preview_blocks_pending_dr(fresh_route):
    """Pending DR batches → cleanup deferred, but no swap_blockers."""
    route_id = fresh_route["route_id"]
    re_queue_id = fresh_route["re_queue_id"]
    stop_ids = fresh_route["stop_ids"]
    v2_coords = [_flat_earth_xy(0.0, 0.0), _flat_earth_xy(1000.0, 0.0)]
    v2_stops = [
        {"stop_id": stop_ids[0], "lat": v2_coords[0][1], "lon": v2_coords[0][0]},
        {"stop_id": stop_ids[1], "lat": v2_coords[1][1], "lon": v2_coords[1][0]},
    ]
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        approval_id = _insert_approval_queue(
            cur, route_id=route_id, re_entry_queue_id=re_queue_id,
            v2_coords=v2_coords, v2_stops=v2_stops, fix_category="structural",
        )
    conn.close()
    _set_quality_class(
        approval_id, "ship_pending_dr", pending_dr=["batch_42_quito_centro"],
    )

    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())

    assert preview.cleanup_eligible is False
    assert "DR" in (preview.cleanup_skip_reason or "")
    assert preview.swap_blockers == []


def test_confirm_swap_executes_atomically(fresh_route):
    """Confirm with eligible cleanup → applied flag flips, swap commits, all in one txn."""
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

    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())
    result = svc.confirm_swap_with_cleanup(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )

    assert result.action == "approved"
    assert result.cleanup_applied is True
    assert result.cleanup_report is not None
    assert result.override_used is False

    # Verify cleanup + swap both landed atomically.
    conn = psycopg2.connect(_dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT status, pre_ship_cleanup_applied, pre_ship_cleanup_at
              FROM route_prod.approval_queue
             WHERE queue_id = %s::uuid
            """,
            (approval_id,),
        )
        r = dict(cur.fetchone())
        assert r["status"] == "approved"
        assert r["pre_ship_cleanup_applied"] is True
        assert r["pre_ship_cleanup_at"] is not None
        cur.execute(
            "SELECT version FROM route_prod.routes WHERE route_id = %s::uuid",
            (route_id,),
        )
        assert dict(cur.fetchone())["version"] == 2
    conn.close()


def _make_unsafe_v2_stops(stop_ids, *, n_orphans: int):
    """Build v2_stops where ``n_orphans`` of them are >60 m off the polyline,
    forcing the safety gate to reject the cleanup. The polyline goes
    east-west along y=0, so we place orphans at y=200 m."""
    v2_coords = [
        _flat_earth_xy(0.0, 0.0),
        _flat_earth_xy(1000.0, 0.0),
    ]
    # The fixture only provides 2 stop_ids; we need extra synthetic ids
    # for the additional stops the safety-gate test demands.
    stops = [
        {"stop_id": stop_ids[0], "lat": v2_coords[0][1], "lon": v2_coords[0][0]},
        {"stop_id": stop_ids[1], "lat": v2_coords[1][1], "lon": v2_coords[1][0]},
    ]
    # Add 3 aligned synthetic stops on the polyline so the cohort is
    # 5 stops; with 2 orphans we exceed the 20% gate (2/5 = 40%).
    for i, x_m in enumerate([200.0, 500.0, 800.0]):
        lon, lat = _flat_earth_xy(x_m, 0.0)
        stops.append({"stop_id": f"fix_gap{i}_tier5", "lat": lat, "lon": lon})
    # Now add ``n_orphans`` orphan stops at y=200 m off polyline.
    for i in range(n_orphans):
        lon, lat = _flat_earth_xy(100.0 + 200.0 * i, 200.0)
        stops.append({"stop_id": f"fix_orphan{i}_tier5", "lat": lat, "lon": lon})
    return v2_coords, stops


def test_confirm_swap_without_override_rejected_at_safety_gate(fresh_route):
    """Excessive removal → SwapError; no DB writes; row stays pending."""
    route_id = fresh_route["route_id"]
    re_queue_id = fresh_route["re_queue_id"]
    stop_ids = fresh_route["stop_ids"]
    v2_coords, v2_stops = _make_unsafe_v2_stops(stop_ids, n_orphans=3)
    # 3 orphans / 8 total = 37.5% → above 20% gate.

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        approval_id = _insert_approval_queue(
            cur, route_id=route_id, re_entry_queue_id=re_queue_id,
            v2_coords=v2_coords, v2_stops=v2_stops, fix_category="structural",
        )
    conn.close()
    _set_quality_class(approval_id, "good")

    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())
    with pytest.raises(svc.SwapError, match="safety gate"):
        svc.confirm_swap_with_cleanup(
            approval_queue_id=approval_id,
            expected_fingerprint=preview.state_fingerprint,
            operator_id=OPERATOR_ID,
            operator_username=OPERATOR_USERNAME,
            dsn=_dsn(),
        )

    # Atomicity: nothing should have been written.
    conn = psycopg2.connect(_dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT status, pre_ship_cleanup_applied "
            "  FROM route_prod.approval_queue WHERE queue_id = %s::uuid",
            (approval_id,),
        )
        r = dict(cur.fetchone())
        assert r["status"] == "pending"
        assert r["pre_ship_cleanup_applied"] is False
        cur.execute(
            "SELECT version FROM route_prod.routes WHERE route_id = %s::uuid",
            (route_id,),
        )
        # Routes table untouched — still v1.
        assert dict(cur.fetchone())["version"] == 1
    conn.close()


def test_confirm_swap_with_override_logs_audit(fresh_route):
    """operator_override=True + reason → cleanup applies, swap completes,
    audit row written."""
    route_id = fresh_route["route_id"]
    re_queue_id = fresh_route["re_queue_id"]
    stop_ids = fresh_route["stop_ids"]
    v2_coords, v2_stops = _make_unsafe_v2_stops(stop_ids, n_orphans=3)

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        approval_id = _insert_approval_queue(
            cur, route_id=route_id, re_entry_queue_id=re_queue_id,
            v2_coords=v2_coords, v2_stops=v2_stops, fix_category="structural",
        )
    conn.close()
    _set_quality_class(approval_id, "good")

    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())
    result = svc.confirm_swap_with_cleanup(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
        operator_override=True,
        override_reason=(
            "Visual inspection confirms orphans are real bus stops on a "
            "side-road; polyline is correct."
        ),
    )

    assert result.cleanup_applied is True
    assert result.override_used is True

    conn = psycopg2.connect(_dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT override_reason, override_by, cleanup_report_at_override
              FROM route_prod.cleanup_override_audit
             WHERE queue_id = %s::uuid
            """,
            (approval_id,),
        )
        audit = cur.fetchall()
        assert len(audit) == 1
        a = dict(audit[0])
        assert "Visual inspection" in a["override_reason"]
        assert a["override_by"] == OPERATOR_USERNAME
        assert a["cleanup_report_at_override"] is not None
    conn.close()
    # cleanup_override_audit isn't FK'd to route, so the fresh_route
    # fixture won't clean it. Drop it ourselves so per-test isolation
    # holds.
    cleanup_conn = psycopg2.connect(_dsn())
    cleanup_conn.autocommit = True
    with cleanup_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM route_prod.cleanup_override_audit "
            "WHERE queue_id = %s::uuid",
            (approval_id,),
        )
    cleanup_conn.close()


def test_confirm_swap_requires_reason_with_override():
    """operator_override=True without override_reason → SwapError immediately."""
    with pytest.raises(svc.SwapError, match="override_reason"):
        svc.confirm_swap_with_cleanup(
            approval_queue_id="00000000-0000-0000-0000-000000000000",
            expected_fingerprint="0" * 64,
            operator_id=OPERATOR_ID,
            operator_username=OPERATOR_USERNAME,
            dsn=_dsn(),
            operator_override=True,
            override_reason="",
        )


# ---------------------------------------------------------------------------
# State fingerprint (GREEK pipeline phase β — last-step guarantee).
# ---------------------------------------------------------------------------

def _insert_simple_shippable(
    fresh_route: dict[str, Any],
    *,
    quality_class: str = "good",
) -> str:
    """Insert a basic shippable approval_queue row + return its queue_id.

    Used by the fingerprint tests below — they only need a row whose
    state can be mutated and re-hashed; the v2 geometry doesn't matter.
    """
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
    _set_quality_class(approval_id, quality_class)
    return approval_id


def test_preview_returns_state_fingerprint(fresh_route):
    """Fingerprint is a non-empty 64-character hex string."""
    approval_id = _insert_simple_shippable(fresh_route)
    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())

    assert preview.state_fingerprint
    assert len(preview.state_fingerprint) == 64
    int(preview.state_fingerprint, 16)  # raises if non-hex


def test_fingerprint_stable_across_calls(fresh_route):
    """Two previews of the same unchanged row yield identical fingerprints."""
    approval_id = _insert_simple_shippable(fresh_route)
    p1 = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())
    p2 = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())

    assert p1.state_fingerprint == p2.state_fingerprint


def test_fingerprint_changes_when_proposed_stops_modified(fresh_route):
    """Mutating proposed_stops produces a different fingerprint."""
    approval_id = _insert_simple_shippable(fresh_route)
    p1 = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE route_prod.approval_queue "
            "   SET proposed_stops = '[]'::jsonb "
            " WHERE queue_id = %s::uuid",
            (approval_id,),
        )
    conn.close()

    p2 = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())
    assert p1.state_fingerprint != p2.state_fingerprint


def test_fingerprint_changes_when_quality_class_modified(fresh_route):
    """Mutating quality_class produces a different fingerprint."""
    approval_id = _insert_simple_shippable(fresh_route, quality_class="good")
    p1 = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())

    _set_quality_class(approval_id, "acceptable")

    p2 = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())
    assert p1.state_fingerprint != p2.state_fingerprint


def test_fingerprint_changes_when_pending_dr_batches_modified(fresh_route):
    """Adding a pending DR batch produces a different fingerprint."""
    approval_id = _insert_simple_shippable(fresh_route)
    p1 = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE route_prod.approval_queue "
            "   SET pending_dr_batches = ARRAY['batch_xyz']::text[] "
            " WHERE queue_id = %s::uuid",
            (approval_id,),
        )
    conn.close()

    p2 = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())
    assert p1.state_fingerprint != p2.state_fingerprint


def test_fingerprint_unchanged_when_irrelevant_field_modified(fresh_route):
    """Mutating fields outside the hash leaves the fingerprint stable."""
    approval_id = _insert_simple_shippable(fresh_route)
    p1 = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        # classified_at, resolution_notes, pre_ship_cleanup_at — none of
        # these belong to the fingerprint composition.
        cur.execute(
            "UPDATE route_prod.approval_queue "
            "   SET classified_at = NOW(), "
            "       resolution_notes = 'irrelevant note' "
            " WHERE queue_id = %s::uuid",
            (approval_id,),
        )
    conn.close()

    p2 = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())
    assert p1.state_fingerprint == p2.state_fingerprint


def test_confirm_with_matching_fingerprint_succeeds(fresh_route):
    """Happy path: fingerprint from preview matches at confirm → swap proceeds."""
    approval_id = _insert_simple_shippable(fresh_route)
    preview = svc.preview_swap_with_cleanup(approval_id, dsn=_dsn())

    result = svc.confirm_swap_with_cleanup(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )

    assert result.action == "approved"
    assert result.cleanup_applied is True


def test_confirm_with_stale_fingerprint_aborts(fresh_route):
    """Wrong fingerprint → SwapError, no DB writes."""
    approval_id = _insert_simple_shippable(fresh_route)
    fake_fingerprint = "0" * 64

    with pytest.raises(svc.SwapError, match="State changed since preview"):
        svc.confirm_swap_with_cleanup(
            approval_queue_id=approval_id,
            expected_fingerprint=fake_fingerprint,
            operator_id=OPERATOR_ID,
            operator_username=OPERATOR_USERNAME,
            dsn=_dsn(),
        )

    # Atomicity: no cleanup applied, no swap executed.
    conn = psycopg2.connect(_dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT status, pre_ship_cleanup_applied "
            "  FROM route_prod.approval_queue WHERE queue_id = %s::uuid",
            (approval_id,),
        )
        r = dict(cur.fetchone())
        assert r["status"] == "pending"
        assert r["pre_ship_cleanup_applied"] is False
    conn.close()


def test_confirm_with_empty_fingerprint_raises():
    """Empty expected_fingerprint is a programmer error → ValueError."""
    with pytest.raises(ValueError, match="expected_fingerprint required"):
        svc.confirm_swap_with_cleanup(
            approval_queue_id="00000000-0000-0000-0000-000000000000",
            expected_fingerprint="",
            operator_id=OPERATOR_ID,
            operator_username=OPERATOR_USERNAME,
            dsn=_dsn(),
        )


def test_old_approve_v2_still_works_via_internal_fingerprint(fresh_route):
    """approve_v2 backward compat: emits DeprecationWarning, computes preview
    internally, swap completes."""
    import warnings as _warnings

    approval_id = _insert_simple_shippable(fresh_route)

    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        result = svc.approve_v2(
            approval_queue_id=approval_id,
            operator_id=OPERATOR_ID,
            operator_username=OPERATOR_USERNAME,
            dsn=_dsn(),
        )

    assert any(
        issubclass(w.category, DeprecationWarning) for w in caught
    ), "approve_v2 must emit DeprecationWarning"
    assert result.action == "approved"


# ---------------------------------------------------------------------------
# GREEK pipeline orchestrator (β → α → γ → δ).
# ---------------------------------------------------------------------------

from hades.enforcers.stop_refill import ProductionRoute  # noqa: E402


def _make_synthetic_pool_with_candidate(
    fresh_route: dict[str, Any],
    candidate_stop_id: str,
) -> list[ProductionRoute]:
    """Build a single ProductionRoute that overlaps with the fresh_route's
    polyline and contributes one refill candidate at the polyline midpoint.

    The fresh_route fixture goes east-west from x=0 to x=1000 along y=0.
    We mirror that polyline so find_shared_segments produces a >500 m
    corridor, and we put the candidate stop at x=500 (5 m off the line).
    """
    coords = [
        _flat_earth_xy(0.0, 0.0),
        _flat_earth_xy(1000.0, 0.0),
    ]
    cand_lon, cand_lat = _flat_earth_xy(500.0, 5.0)
    return [
        ProductionRoute(
            route_id="route_b_synthetic",
            polyline=coords,
            stops=[
                {
                    "stop_id": candidate_stop_id,
                    "lat": cand_lat,
                    "lon": cand_lon,
                }
            ],
        )
    ]


def test_preview_greek_pipeline_returns_combined_preview(fresh_route):
    """Preview includes cleanup report + refill candidates list."""
    approval_id = _insert_simple_shippable(fresh_route)

    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=[]
    )

    assert preview.cleanup_eligible is True
    assert preview.cleanup_report is not None
    assert preview.cleanup_safety_gate == "pass"
    assert preview.refill_eligible is True
    assert preview.refill_candidates == []
    assert preview.would_apply is True
    assert preview.state_fingerprint  # 64-char hex
    assert len(preview.state_fingerprint) == 64


def test_preview_greek_pipeline_caches_candidates_to_db(fresh_route):
    """Refill candidates are written to proposed_refill_candidates."""
    approval_id = _insert_simple_shippable(fresh_route)
    cand_id = str(uuid.uuid4())
    pool = _make_synthetic_pool_with_candidate(fresh_route, cand_id)

    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=pool
    )

    assert len(preview.refill_candidates) >= 1
    surfaced_ids = {c["stop_id"] for c in preview.refill_candidates}
    assert cand_id in surfaced_ids

    conn = psycopg2.connect(_dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT proposed_refill_candidates "
            "  FROM route_prod.approval_queue "
            " WHERE queue_id = %s::uuid",
            (approval_id,),
        )
        cached = dict(cur.fetchone())["proposed_refill_candidates"]
    conn.close()

    assert cached is not None
    cached_ids = {c["stop_id"] for c in cached}
    assert cand_id in cached_ids


def test_confirm_greek_pipeline_validates_decisions_completeness(fresh_route):
    """Missing per-candidate decisions: error, no DB writes."""
    approval_id = _insert_simple_shippable(fresh_route)
    cand_id = str(uuid.uuid4())
    pool = _make_synthetic_pool_with_candidate(fresh_route, cand_id)

    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=pool
    )
    assert len(preview.refill_candidates) == 1

    result = svc.confirm_greek_pipeline(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        refill_decisions={},  # missing the one surfaced candidate
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )

    assert result.success is False
    assert "Missing decisions" in (result.error or "")

    # Atomicity: row still pending, cleanup not applied.
    conn = psycopg2.connect(_dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT status, pre_ship_cleanup_applied, refill_applied "
            "  FROM route_prod.approval_queue WHERE queue_id = %s::uuid",
            (approval_id,),
        )
        r = dict(cur.fetchone())
    conn.close()
    assert r["status"] == "pending"
    assert r["pre_ship_cleanup_applied"] is False
    assert r["refill_applied"] is False


def test_confirm_greek_pipeline_applies_only_accepted_refills(fresh_route):
    """Only 'accepted' candidates land in proposed_stops; skipped/later don't."""
    approval_id = _insert_simple_shippable(fresh_route)
    accepted_id = str(uuid.uuid4())
    skipped_id = str(uuid.uuid4())
    later_id = str(uuid.uuid4())

    coords = [_flat_earth_xy(0.0, 0.0), _flat_earth_xy(1000.0, 0.0)]
    pool = [
        ProductionRoute(
            route_id="b_acc",
            polyline=coords,
            stops=[{
                "stop_id": accepted_id,
                "lat": _flat_earth_xy(300.0, 5.0)[1],
                "lon": _flat_earth_xy(300.0, 5.0)[0],
            }],
        ),
        ProductionRoute(
            route_id="b_skip",
            polyline=coords,
            stops=[{
                "stop_id": skipped_id,
                "lat": _flat_earth_xy(500.0, 5.0)[1],
                "lon": _flat_earth_xy(500.0, 5.0)[0],
            }],
        ),
        ProductionRoute(
            route_id="b_later",
            polyline=coords,
            stops=[{
                "stop_id": later_id,
                "lat": _flat_earth_xy(700.0, 5.0)[1],
                "lon": _flat_earth_xy(700.0, 5.0)[0],
            }],
        ),
    ]

    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=pool
    )
    surfaced = {c["stop_id"] for c in preview.refill_candidates}
    assert {accepted_id, skipped_id, later_id} <= surfaced

    decisions = {
        accepted_id: "accepted",
        skipped_id: "skipped",
        later_id: "reviewed_later",
    }
    result = svc.confirm_greek_pipeline(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        refill_decisions=decisions,
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )

    assert result.success is True
    assert result.cleanup_applied is True
    assert result.refill_applied is True
    assert result.refill_accepted_count == 1
    assert result.refill_decisions_count == 3

    conn = psycopg2.connect(_dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT proposed_stops FROM route_prod.approval_queue "
            " WHERE queue_id = %s::uuid",
            (approval_id,),
        )
        final_stops = dict(cur.fetchone())["proposed_stops"]
    conn.close()

    final_ids = {str(s.get("stop_id")) for s in final_stops}
    assert accepted_id in final_ids
    assert skipped_id not in final_ids
    assert later_id not in final_ids


def test_confirm_greek_pipeline_audit_logs_all_decisions(fresh_route):
    """One refill_audit row per candidate regardless of decision."""
    approval_id = _insert_simple_shippable(fresh_route)
    accepted_id = str(uuid.uuid4())
    skipped_id = str(uuid.uuid4())

    coords = [_flat_earth_xy(0.0, 0.0), _flat_earth_xy(1000.0, 0.0)]
    pool = [
        ProductionRoute(
            route_id="b_acc",
            polyline=coords,
            stops=[{
                "stop_id": accepted_id,
                "lat": _flat_earth_xy(300.0, 5.0)[1],
                "lon": _flat_earth_xy(300.0, 5.0)[0],
            }],
        ),
        ProductionRoute(
            route_id="b_skip",
            polyline=coords,
            stops=[{
                "stop_id": skipped_id,
                "lat": _flat_earth_xy(700.0, 5.0)[1],
                "lon": _flat_earth_xy(700.0, 5.0)[0],
            }],
        ),
    ]

    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=pool
    )

    decisions = {accepted_id: "accepted", skipped_id: "skipped"}
    result = svc.confirm_greek_pipeline(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        refill_decisions=decisions,
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )
    assert result.success is True

    try:
        conn = psycopg2.connect(_dsn())
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT candidate_stop_id::text AS sid, decision
                  FROM route_prod.refill_audit
                 WHERE queue_id = %s::uuid
                """,
                (approval_id,),
            )
            audit_rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        decisions_by_sid = {r["sid"]: r["decision"] for r in audit_rows}
        assert decisions_by_sid.get(accepted_id) == "accepted"
        assert decisions_by_sid.get(skipped_id) == "skipped"
        assert len(audit_rows) == 2
    finally:
        # refill_audit is not FK-cleaned by fresh_route fixture.
        cleanup_conn = psycopg2.connect(_dsn())
        cleanup_conn.autocommit = True
        with cleanup_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM route_prod.refill_audit WHERE queue_id = %s::uuid",
                (approval_id,),
            )
        cleanup_conn.close()


def test_confirm_greek_pipeline_atomic_on_swap_failure(fresh_route):
    """If swap step fails (e.g. re_entry_queue not v2_ready), cleanup +
    refill must roll back — neither lands on disk."""
    approval_id = _insert_simple_shippable(fresh_route)
    cand_id = str(uuid.uuid4())
    pool = _make_synthetic_pool_with_candidate(fresh_route, cand_id)

    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=pool
    )

    # Sabotage the swap step: flip re_entry_queue out of v2_ready.
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE route_prod.re_entry_queue "
            "   SET status = 'pending' "
            " WHERE queue_id = %s::uuid",
            (fresh_route["re_queue_id"],),
        )
    conn.close()

    result = svc.confirm_greek_pipeline(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        refill_decisions={cand_id: "accepted"},
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )

    assert result.success is False
    assert "v2_ready" in (result.error or "")

    # Atomicity: nothing landed.
    conn = psycopg2.connect(_dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT status, pre_ship_cleanup_applied, refill_applied "
            "  FROM route_prod.approval_queue WHERE queue_id = %s::uuid",
            (approval_id,),
        )
        r = dict(cur.fetchone())
        assert r["status"] == "pending"
        assert r["pre_ship_cleanup_applied"] is False
        assert r["refill_applied"] is False
        cur.execute(
            "SELECT COUNT(*) AS n FROM route_prod.refill_audit "
            " WHERE queue_id = %s::uuid",
            (approval_id,),
        )
        assert dict(cur.fetchone())["n"] == 0
    conn.close()


def test_confirm_greek_pipeline_fingerprint_mismatch_aborts(fresh_route):
    """β phase still works inside the GREEK orchestrator: stale fingerprint
    aborts before cleanup runs."""
    approval_id = _insert_simple_shippable(fresh_route)

    # Don't preview; just send a clearly-bogus fingerprint.
    result = svc.confirm_greek_pipeline(
        approval_queue_id=approval_id,
        expected_fingerprint="0" * 64,
        refill_decisions={},
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )

    assert result.success is False
    assert "State changed since preview" in (result.error or "")

    # Atomicity: row still pending.
    conn = psycopg2.connect(_dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT status, pre_ship_cleanup_applied "
            "  FROM route_prod.approval_queue WHERE queue_id = %s::uuid",
            (approval_id,),
        )
        r = dict(cur.fetchone())
        assert r["status"] == "pending"
        assert r["pre_ship_cleanup_applied"] is False
    conn.close()


def test_confirm_greek_pipeline_with_no_refill_candidates_succeeds(fresh_route):
    """Route with 0 surfaced candidates ships cleanly with refill_decisions={}."""
    approval_id = _insert_simple_shippable(fresh_route)

    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=[]
    )
    assert preview.refill_candidates == []

    result = svc.confirm_greek_pipeline(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        refill_decisions={},
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )

    assert result.success is True
    assert result.cleanup_applied is True
    assert result.refill_applied is False
    assert result.refill_accepted_count == 0
    assert result.refill_decisions_count == 0
    assert result.swap_id is not None
