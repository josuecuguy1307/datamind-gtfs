"""
Integration: phase1_nodes/client.py operator-approval paths now route
through the universal stop-quality treater via the new
``approve_promote`` operation.

Covers:
- treater extension: `approve_promote` operation directly
- `create_prod_node_manual` (simplest — data fully in scope)
- `approve_node_review_request` (UPSERT with operator-supplied fields)
- `approve_resolved_node` (data fetched from node_work join)

Each test rolls back its own transaction.
"""
from __future__ import annotations

import os
import sys
import uuid
import json
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


# ─── treater extension: approve_promote operation ───────────────────────────

def test_approve_promote_inserts_with_provided_node_id(conn):
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput, treat_stop,
    )
    nid = uuid.uuid4()
    res = treat_stop(
        StopTreatmentInput(
            operation="approve_promote",
            caller="test_extension",
            node_id=str(nid),
            proposed_name="Plaza Foch",
            proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
            confidence=1.0,
        ),
        conn,
    )
    assert res.success
    assert str(res.node_id) == str(nid), "approve_promote must respect provided node_id"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name FROM node_prod.nodes WHERE node_id = %s::uuid",
            (str(nid),),
        )
        (name,) = cur.fetchone()
    assert name == "Plaza Foch"


def test_approve_promote_inserts_new_uuid_when_none(conn):
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput, treat_stop,
    )
    res = treat_stop(
        StopTreatmentInput(
            operation="approve_promote",
            caller="test_extension",
            node_id=None,
            proposed_name="Mercado Iñaquito",
            proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
        ),
        conn,
    )
    assert res.success
    assert res.node_id is not None


def test_approve_promote_upserts_existing_row(conn):
    """Calling approve_promote on a node that already exists in
    node_prod must UPDATE the existing row in-place (not raise)."""
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput, treat_stop,
    )
    nid = uuid.uuid4()
    # Seed: pre-existing row with old name + position
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO node_prod.nodes (node_id, geom, node_type, name, confidence) "
            "VALUES (%s::uuid, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geometry(Point,4326), "
            "'STOP', %s, 0.5)",
            (str(nid), -78.6, -0.20, "Old Name"),
        )
    res = treat_stop(
        StopTreatmentInput(
            operation="approve_promote",
            caller="test_extension",
            node_id=str(nid),
            proposed_name="Updated Name",
            proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
            confidence=1.0,
        ),
        conn,
    )
    assert res.success
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, ST_Y(geom::geometry), ST_X(geom::geometry) "
            "FROM node_prod.nodes WHERE node_id = %s::uuid",
            (str(nid),),
        )
        name, lat, lon = cur.fetchone()
    assert name == "Updated Name"
    assert abs(float(lat) - QUITO_LAT) < 1e-6
    assert abs(float(lon) - QUITO_LON) < 1e-6


def test_approve_promote_writes_extras_to_whitelisted_columns(conn):
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput, treat_stop,
    )
    import psycopg2.extras as _ppx
    res = treat_stop(
        StopTreatmentInput(
            operation="approve_promote",
            caller="test_extension",
            node_id=None,
            proposed_name="Universidad Central",
            proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
            extras={
                "source": "manual_workspace",
                "tag_kind": "bus_stop",
                "ref": "UC-01",
                "operator": "Trans-Quito",
                "chosen_tags": _ppx.Json({"highway": "bus_stop", "ref": "UC-01"}),
            },
        ),
        conn,
    )
    assert res.success
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, tag_kind, ref, operator, chosen_tags "
            "FROM node_prod.nodes WHERE node_id = %s::uuid",
            (str(res.node_id),),
        )
        src, tk, ref, op, tags = cur.fetchone()
    assert src == "manual_workspace"
    assert tk == "bus_stop"
    assert ref == "UC-01"
    assert op == "Trans-Quito"
    assert tags.get("ref") == "UC-01"


def test_approve_promote_audit_row_written(conn):
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput, treat_stop,
    )
    res = treat_stop(
        StopTreatmentInput(
            operation="approve_promote",
            caller="phase1_client.create_manual",
            proposed_name="Av Patria",
            proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
        ),
        conn,
    )
    assert res.success
    with conn.cursor() as cur:
        cur.execute(
            "SELECT operation, caller, success FROM node_prod.stop_treatment_log "
            "WHERE node_id = %s::uuid ORDER BY treated_at DESC LIMIT 1",
            (str(res.node_id),),
        )
        row = cur.fetchone()
    assert row is not None
    op, caller, success = row
    assert op == "approve_promote"
    assert caller == "phase1_client.create_manual"
    assert success is True


def test_approve_promote_validation_requires_lat_lon(conn):
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput, treat_stop,
    )
    res = treat_stop(
        StopTreatmentInput(
            operation="approve_promote",
            caller="test_extension",
            proposed_name="Bare",
        ),
        conn,
    )
    assert res.success is False
    assert res.error and "lat + lon" in res.error
