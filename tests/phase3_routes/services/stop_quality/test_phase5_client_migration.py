"""
Integration: phase5_gtfs/client.py `update_stop_and_optionally_sync_node`
now syncs node_prod via the universal stop-quality treater.

Pre-migration: a raw UPDATE that set name + geom directly. Post: routes
through `treat_stop(operation='snap_align')` so the synced name passes
the canonical cascade and the place mapping is refreshed.

We invoke the treater directly with the same inputs the migrated code
sends — the migrated method's only behavior change is wrapping the
treater call. Importing the full Phase5Client class drags large
dependencies that aren't worth pulling for a one-call test.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest
from datamind_core.dsn import need_dsn

# INTEGRATION tests: they assume a database already populated with real data of a region.
# Skipped unless you set DB_DSN and DATAMIND_RUN_DB_TESTS=1 (see README, Tests section).
pytestmark = pytest.mark.skipif(
    not (os.environ.get("DB_DSN") and os.environ.get("DATAMIND_RUN_DB_TESTS") == "1"),
    reason="integration test: requires DB_DSN and DATAMIND_RUN_DB_TESTS=1",
)


REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

psycopg2.extras.register_uuid()


DSN = os.environ.get("DB_DSN", "")
QUITO_LAT = -0.18
QUITO_LON = -78.5


@pytest.fixture
def conn():
    c = psycopg2.connect(need_dsn(DSN))
    c.autocommit = False
    yield c
    try:
        c.rollback()
    finally:
        c.close()


def _seed_node(conn, name: str, lat: float = QUITO_LAT, lon: float = QUITO_LON) -> uuid.UUID:
    nid = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO node_prod.nodes (node_id, geom, node_type, name, confidence) "
            "VALUES (%s::uuid, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geometry(Point,4326), "
            "        'STOP', %s, 0.5)",
            (str(nid), lon, lat, name),
        )
    return nid


def _sync_via_treater(conn, *, stop_id: str, stop_name: str, stop_lat: float, stop_lon: float):
    """Mirror what update_stop_and_optionally_sync_node does on
    sync_node_prod=True path."""
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput,
        treat_stop,
    )
    return treat_stop(
        StopTreatmentInput(
            operation="snap_align",
            caller="phase5_gtfs.export_sync",
            node_id=stop_id,
            proposed_name=stop_name,
            proposed_lat=stop_lat,
            proposed_lon=stop_lon,
        ),
        conn,
    )


# ─── happy path: sync updates name + geom ───────────────────────────────────

def test_sync_updates_node_name_and_geom(conn):
    nid = _seed_node(conn, "Old Name", lat=QUITO_LAT, lon=QUITO_LON)
    new_lat, new_lon = -0.21, -78.49
    res = _sync_via_treater(
        conn, stop_id=str(nid),
        stop_name="Plaza Foch", stop_lat=new_lat, stop_lon=new_lon,
    )
    assert res.success
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, ST_Y(geom::geometry), ST_X(geom::geometry) "
            "FROM node_prod.nodes WHERE node_id = %s::uuid",
            (str(nid),),
        )
        name, lat, lon = cur.fetchone()
    assert name == "Plaza Foch"
    assert abs(float(lat) - new_lat) < 1e-6
    assert abs(float(lon) - new_lon) < 1e-6


# ─── forbidden name from GTFS edit gets repaired ────────────────────────────

def test_sync_repairs_forbidden_name_from_gtfs(conn):
    """If an operator (or upstream) accidentally sends a forbidden name
    via GTFS edit, the treater repairs it instead of writing it through."""
    nid = _seed_node(conn, "Mercado Iñaquito")
    res = _sync_via_treater(
        conn, stop_id=str(nid),
        stop_name="Parada (cafebabe)",   # forbidden — cascade repairs
        stop_lat=QUITO_LAT, stop_lon=QUITO_LON,
    )
    assert res.success
    assert res.final_name != "Parada (cafebabe)"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name FROM node_prod.nodes WHERE node_id = %s::uuid",
            (str(nid),),
        )
        (name,) = cur.fetchone()
    assert name != "Parada (cafebabe)"
    assert name and name.strip()


# ─── audit row written with correct caller tag ──────────────────────────────

def test_sync_writes_audit_row(conn):
    nid = _seed_node(conn, "Av Patria")
    res = _sync_via_treater(
        conn, stop_id=str(nid),
        stop_name="Av Patria", stop_lat=QUITO_LAT, stop_lon=QUITO_LON,
    )
    assert res.success
    with conn.cursor() as cur:
        cur.execute(
            "SELECT operation, caller, success FROM node_prod.stop_treatment_log "
            "WHERE node_id = %s::uuid ORDER BY treated_at DESC LIMIT 1",
            (str(nid),),
        )
        row = cur.fetchone()
    assert row is not None
    op, caller, success = row
    assert op == "snap_align"
    assert caller == "phase5_gtfs.export_sync"
    assert success is True


# ─── unknown node_id fails cleanly ──────────────────────────────────────────

def test_sync_unknown_node_id_returns_failure(conn):
    bogus = uuid.uuid4()
    res = _sync_via_treater(
        conn, stop_id=str(bogus),
        stop_name="Whatever", stop_lat=QUITO_LAT, stop_lon=QUITO_LON,
    )
    assert res.success is False
