"""End-to-end tests for Prompt 8's re-entry pipeline.

These exercise the full chain from a populated ``re_entry_queue``
row through the worker, into ``approval_queue``, and through the
swap service — or down the failure / quarantine paths. Each test
seeds its own synthetic route (route_jobs → routes → queue) with
a sentinel ``priority=0`` so the worker claims it before any
real backlog row.

Matrix covered (matches Prompt 8's H.1-H.4 plan):

  H.1  worker plans v2 → operator APPROVE → swap applied
  H.2  Fixer can't improve the route → worker records 'failed'
  H.3  worker plans v2 → operator QUARANTINE → no re-claim
  H.4  deadline_monitor returns a consistent, well-formed shape

Skipped if the local ``datamind_ml`` Postgres is not reachable.
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

from hades.enforcers import re_entry_swap_service as swap_svc  # noqa: E402
from hades.enforcers import re_entry_worker as worker  # noqa: E402


# ---------------------------------------------------------------------------
# DSN / skip.
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
# Fixture plumbing.
# ---------------------------------------------------------------------------

OPERATOR_ID = "00000000-0000-4000-8000-000000000001"  # operador de prueba
OPERATOR_USERNAME = "prompt8_e2e"


def _flat_earth_xy(x_m: float, y_m: float) -> tuple[float, float]:
    lat0 = -0.18
    lon0 = -78.48
    m_per_deg_lat = 111_132.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
    return (lon0 + x_m / m_per_deg_lon, lat0 + y_m / m_per_deg_lat)


def _insert_stop(cur, lat: float, lon: float) -> str:
    cur.execute(
        """
        INSERT INTO node_prod.nodes
            (node_id, geom, node_type, tag_kind, source, province,
             confidence, chosen_tags)
        VALUES
            (gen_random_uuid(),
             ST_SetSRID(ST_MakePoint(%s, %s), 4326),
             'STOP', 'bus_stop', 'prompt8_e2e', 'sample_region', 0.9, '{}'::jsonb)
        RETURNING node_id::text
        """,
        (float(lon), float(lat)),
    )
    return str(cur.fetchone()[0])


def _insert_route(
    cur,
    *,
    stop_ids: list[str],
    coords: list[tuple[float, float]],
    grandfathered: bool,
) -> str:
    route_uuid = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO route_raw.route_jobs
            (route_id, status, created_by, province)
        VALUES (%s::uuid, 'active', 'prompt8_e2e', 'sample_region')
        """,
        (route_uuid,),
    )
    wkt = "LINESTRING(" + ", ".join(f"{lon} {lat}" for (lon, lat) in coords) + ")"
    if grandfathered:
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
            """,
            (route_uuid, wkt, stop_ids),
        )
    else:
        cur.execute(
            """
            INSERT INTO route_prod.routes
                (route_id, geom, stop_node_ids, source, source_type, province,
                 legacy_grandfathered, version)
            VALUES
                (%s::uuid,
                 ST_SetSRID(ST_GeomFromText(%s), 4326),
                 %s::uuid[],
                 'route_constructor', 'manual_constructor', 'sample_region',
                 FALSE, 1)
            """,
            (route_uuid, wkt, stop_ids),
        )
    return route_uuid


def _insert_queue_row(cur, *, route_id: str, classification: str) -> str:
    """Insert a queue row at priority=0 so it beats the real backlog."""
    cur.execute(
        """
        INSERT INTO route_prod.re_entry_queue
            (route_id, current_version, priority, classification, status,
             priority_reason)
        VALUES (%s::uuid, 1, 0, %s, 'pending', 'prompt8_e2e_sentinel')
        RETURNING queue_id::text
        """,
        (route_id, classification),
    )
    return str(cur.fetchone()[0])


def _cleanup(cur, route_id: str, stop_ids: list[str]) -> None:
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
    cur.execute(
        "DELETE FROM route_prod.routes WHERE route_id = %s::uuid",
        (route_id,),
    )
    cur.execute(
        "DELETE FROM route_raw.route_jobs WHERE route_id = %s::uuid",
        (route_id,),
    )
    if stop_ids:
        cur.execute(
            "DELETE FROM node_prod.nodes WHERE node_id = ANY(%s::uuid[])",
            (stop_ids,),
        )
    cur.execute(
        """
        DELETE FROM node_prod.nodes
         WHERE source = 're_entry_swap'
           AND synthetic_created_by = %s
        """,
        (OPERATOR_USERNAME,),
    )


def _reachable_route(
    *,
    length_m: float = 2000.0,
    n_vertices: int = 60,
    n_stops: int = 2,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """A plain west→east corridor plus a stop at each end.

    With only 2 stops on a 2 km shape the coverage enforcer should
    see a large gap and the Fixer should propose synthetic fills.
    """
    coords = [_flat_earth_xy(i * (length_m / (n_vertices - 1)), 0.0)
              for i in range(n_vertices)]
    stop_positions: list[tuple[float, float]] = []
    if n_stops >= 2:
        stop_positions.append(_flat_earth_xy(0.0, 0.0))
        stop_positions.append(_flat_earth_xy(length_m, 0.0))
    extras = n_stops - 2
    for k in range(1, extras + 1):
        stop_positions.append(
            _flat_earth_xy(length_m * k / (extras + 1), 0.0)
        )
    return coords, stop_positions


@pytest.fixture
def seeded_queue():
    """Seed a priority=0 grandfathered route + queue row.

    Yields a dict with route_id, stop_ids, queue_id; tears everything
    down afterwards (including anything the swap-service inserted).
    """
    conn = psycopg2.connect(_dsn())
    conn.autocommit = False
    created: dict[str, Any] = {"stop_ids": [], "extras_stop_ids": []}
    try:
        with conn.cursor() as cur:
            coords, stop_positions = _reachable_route(
                length_m=2000.0, n_vertices=60, n_stops=2
            )
            stop_ids = [
                _insert_stop(cur, lat=lat, lon=lon)
                for (lon, lat) in stop_positions
            ]
            route_id = _insert_route(
                cur,
                stop_ids=stop_ids,
                coords=coords,
                grandfathered=True,
            )
            queue_id = _insert_queue_row(
                cur,
                route_id=route_id,
                classification="manual_constructor_legacy",
            )
            created.update(
                {
                    "route_id": route_id,
                    "stop_ids": stop_ids,
                    "queue_id": queue_id,
                    "coords": coords,
                }
            )
        conn.commit()
        yield created
    finally:
        try:
            with conn.cursor() as cur:
                _cleanup(
                    cur,
                    created.get(
                        "route_id",
                        "00000000-0000-0000-0000-000000000000",
                    ),
                    created.get("stop_ids", []) + created.get(
                        "extras_stop_ids", []
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# H.1  Happy path — worker plans, operator approves, swap applied.
# ---------------------------------------------------------------------------

def test_h1_happy_path_worker_then_approve(seeded_queue):
    route_id = seeded_queue["route_id"]
    queue_id = seeded_queue["queue_id"]

    summary = worker.run_batch(dsn=_dsn(), batch_size=1, dry_run=False)
    assert summary["claimed"] == 1, summary
    assert summary["v2_ready"] == 1, summary

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT status FROM route_prod.re_entry_queue "
            "WHERE queue_id = %s::uuid",
            (queue_id,),
        )
        assert dict(cur.fetchone())["status"] == "v2_ready"

        cur.execute(
            """
            SELECT queue_id::text AS queue_id, status, policy_flags
              FROM route_prod.approval_queue
             WHERE route_code = %s
               AND policy_flags ? 're_entry_queue_id'
             ORDER BY enqueued_at DESC
             LIMIT 1
            """,
            (route_id,),
        )
        ap_row = dict(cur.fetchone())
        assert ap_row["status"] == "pending"
        assert ap_row["policy_flags"]["re_entry_queue_id"] == queue_id
        assert ap_row["policy_flags"]["fix_category"] in {
            "geometry",
            "stop_coverage",
            "structural",
        }
        approval_id = ap_row["queue_id"]
    conn.close()

    result = swap_svc.approve_v2(
        approval_queue_id=approval_id,
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )
    assert result.action == "approved"
    assert result.version_before == 1
    assert result.version_after == 2

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT version, legacy_grandfathered, grandfathered_until,
                   pipeline_version, last_swap_at, last_swap_from_version
              FROM route_prod.routes WHERE route_id = %s::uuid
            """,
            (route_id,),
        )
        row = dict(cur.fetchone())
        assert row["version"] == 2
        assert row["legacy_grandfathered"] is False
        assert row["grandfathered_until"] is None
        assert row["pipeline_version"] == "re_entry_v1"
        assert row["last_swap_at"] is not None
        assert row["last_swap_from_version"] == 1

        cur.execute(
            "SELECT status FROM route_prod.re_entry_queue "
            "WHERE queue_id = %s::uuid",
            (queue_id,),
        )
        assert dict(cur.fetchone())["status"] == "swapped"

        cur.execute(
            "SELECT COUNT(*) AS n FROM route_prod.fix_reports "
            "WHERE route_id = %s::uuid",
            (route_id,),
        )
        assert dict(cur.fetchone())["n"] == 1
    conn.close()


# ---------------------------------------------------------------------------
# H.2  Worker records 'failed' when the Fixer can't improve.
# ---------------------------------------------------------------------------

def test_h2_worker_marks_failed_when_classification_unclassified():
    """An unclassified row short-circuits in the worker — no approval
    row is created and re_entry_queue lands on 'failed' with an
    explicit reason. This exercises the 'no playbook' branch of
    _process_route without needing to force the Fixer to regress.
    """
    conn = psycopg2.connect(_dsn())
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            coords, stop_positions = _reachable_route(
                length_m=1200.0, n_vertices=30, n_stops=2
            )
            stop_ids = [
                _insert_stop(cur, lat=lat, lon=lon)
                for (lon, lat) in stop_positions
            ]
            route_id = _insert_route(
                cur,
                stop_ids=stop_ids,
                coords=coords,
                grandfathered=True,
            )
            queue_id = _insert_queue_row(
                cur,
                route_id=route_id,
                classification="unclassified",
            )
        conn.commit()

        summary = worker.run_batch(
            dsn=_dsn(), batch_size=1, dry_run=False
        )
        assert summary["claimed"] == 1, summary
        assert summary["failed"] >= 1, summary
        assert summary["v2_ready"] == 0, summary

        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                "SELECT status, last_error FROM route_prod.re_entry_queue "
                "WHERE queue_id = %s::uuid",
                (queue_id,),
            )
            row = dict(cur.fetchone())
            assert row["status"] == "failed"
            assert "no playbook" in (row["last_error"] or "")

            cur.execute(
                "SELECT COUNT(*) AS n FROM route_prod.approval_queue "
                "WHERE route_code = %s",
                (route_id,),
            )
            assert dict(cur.fetchone())["n"] == 0
    finally:
        with conn.cursor() as cur:
            _cleanup(cur, route_id, stop_ids)
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# H.3  Quarantine — worker will not re-claim after operator locks the row.
# ---------------------------------------------------------------------------

def test_h3_quarantine_prevents_reclaim(seeded_queue):
    route_id = seeded_queue["route_id"]
    queue_id = seeded_queue["queue_id"]

    # First pass: worker plans v2.
    summary1 = worker.run_batch(dsn=_dsn(), batch_size=1, dry_run=False)
    assert summary1["v2_ready"] == 1

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT queue_id::text AS queue_id
              FROM route_prod.approval_queue
             WHERE route_code = %s
               AND status = 'pending'
               AND policy_flags ? 're_entry_queue_id'
             ORDER BY enqueued_at DESC LIMIT 1
            """,
            (route_id,),
        )
        approval_id = dict(cur.fetchone())["queue_id"]
    conn.close()

    # Operator quarantines the proposal.
    swap_svc.quarantine_v2(
        approval_queue_id=approval_id,
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        reason="h3 test: lock out auto-retry",
        dsn=_dsn(),
    )

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT status FROM route_prod.re_entry_queue "
            "WHERE queue_id = %s::uuid",
            (queue_id,),
        )
        assert dict(cur.fetchone())["status"] == "quarantined"

        cur.execute(
            "SELECT status, resolution_notes FROM route_prod.approval_queue "
            "WHERE queue_id = %s::uuid",
            (approval_id,),
        )
        row = dict(cur.fetchone())
        assert row["status"] == "rejected"
        assert "[QUARANTINED]" in (row["resolution_notes"] or "")

        # Routes untouched by quarantine.
        cur.execute(
            "SELECT version, legacy_grandfathered, last_swap_at "
            "FROM route_prod.routes WHERE route_id = %s::uuid",
            (route_id,),
        )
        row = dict(cur.fetchone())
        assert row["version"] == 1
        assert row["legacy_grandfathered"] is True
        assert row["last_swap_at"] is None
    conn.close()

    # Second pass: worker must NOT re-pick our row.
    # We use the private _claim_batch with a conn so we can verify
    # nothing matching our route_id gets claimed, even if other
    # priority=0 rows exist elsewhere on this DB.
    conn = psycopg2.connect(_dsn())
    conn.autocommit = False
    try:
        rows = worker._claim_batch(conn, batch_size=50)
        assert all(str(r["route_id"]) != route_id for r in rows), (
            "quarantined route was re-claimed by the worker"
        )
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# H.4  Deadline monitor — shape + totals consistency.
# ---------------------------------------------------------------------------

def test_h4_deadline_monitor_shape_and_totals():
    summary = worker.deadline_monitor(dsn=_dsn())

    # Hard contract.
    assert summary["deadline"] == "2026-07-20"
    for k in (
        "today",
        "days_remaining",
        "status_counts",
        "class_counts",
        "unfixed_grandfathered",
        "pending_v2_proposals",
    ):
        assert k in summary, f"missing key: {k}"

    # days_remaining == (deadline - today).
    import datetime
    deadline = datetime.date.fromisoformat(summary["deadline"])
    today = datetime.date.fromisoformat(summary["today"])
    assert summary["days_remaining"] == (deadline - today).days

    # Status/class counts are plain int maps.
    for v in summary["status_counts"].values():
        assert isinstance(v, int)
    for v in summary["class_counts"].values():
        assert isinstance(v, int)

    # Status counts sum equals the class counts sum (both iterate the
    # whole queue exactly once).
    assert sum(summary["status_counts"].values()) == sum(
        summary["class_counts"].values()
    )

    # unfixed_grandfathered <= total queue size (can't exceed).
    total = sum(summary["status_counts"].values())
    assert summary["unfixed_grandfathered"] <= total
    assert summary["pending_v2_proposals"] >= 0
