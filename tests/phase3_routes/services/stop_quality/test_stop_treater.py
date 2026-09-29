"""
Integration tests for the universal stop quality treater.

Each test runs against the live DB ($DB_DSN) inside a SAVEPOINT-backed
transaction that is rolled back at the end. No persistent state.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from phase3_routes.services.stop_quality import (  # noqa: E402
    StopTreatmentInput,
    treat_stop,
)
from datamind_console.common.naming_patterns import is_stop_name_forbidden  # noqa: E402
from datamind_core.dsn import need_dsn

# INTEGRATION tests: they assume a database already populated with real data of a region.
# Skipped unless you set DB_DSN and DATAMIND_RUN_DB_TESTS=1 (see README, Tests section).
pytestmark = pytest.mark.skipif(
    not (os.environ.get("DB_DSN") and os.environ.get("DATAMIND_RUN_DB_TESTS") == "1"),
    reason="integration test: requires DB_DSN and DATAMIND_RUN_DB_TESTS=1",
)



DSN = os.environ.get("DB_DSN", "")


@pytest.fixture
def conn():
    """A connection that wraps each test in a single rolled-back transaction."""
    c = psycopg2.connect(need_dsn(DSN))
    c.autocommit = False
    yield c
    try:
        c.rollback()
    finally:
        c.close()


# Choose a coordinate inside Quito metro that has known landmarks/sectors so
# the contextual cascade can find something. Latacunga / Quito-Sur known to
# have rich landmark coverage.
QUITO_LAT = -0.18
QUITO_LON = -78.5


# ─── synthetic_insert ────────────────────────────────────────────────────────


def test_synthetic_insert_with_clean_name(conn):
    inp = StopTreatmentInput(
        operation="synthetic_insert",
        caller="test_treater",
        proposed_name="Mi Parada de Prueba",
        proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success, r.error
    assert r.final_name == "Mi Parada de Prueba"
    assert r.node_id is not None
    assert r.place_id is not None
    assert not r.context_name_applied
    assert is_stop_name_forbidden(r.final_name) is False

    # consistency: node_prod.nodes + geo_prod.places + node_place_map
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM node_prod.nodes WHERE node_id = %s", (r.node_id,))
        assert cur.fetchone()[0] == "Mi Parada de Prueba"
        cur.execute(
            "SELECT canonical_name, status FROM geo_prod.places WHERE place_id = %s",
            (r.place_id,),
        )
        canonical, status = cur.fetchone()
        assert canonical == "Mi Parada de Prueba"
        assert status == "active"
        cur.execute(
            "SELECT 1 FROM geo_prod.node_place_map WHERE node_id = %s AND place_id = %s",
            (r.node_id, r.place_id),
        )
        assert cur.fetchone() == (1,)


def test_synthetic_insert_with_forbidden_name_invokes_cascade(conn):
    inp = StopTreatmentInput(
        operation="synthetic_insert",
        caller="test_treater",
        proposed_name="Parada (deadbeef)",   # forbidden hash shape
        proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success, r.error
    assert r.name_was_forbidden_input is True
    assert r.context_name_applied is True
    assert r.final_name != "Parada (deadbeef)"
    assert is_stop_name_forbidden(r.final_name) is False


def test_synthetic_insert_with_blank_name_uses_extended_cascade(conn):
    """Far-from-Quito coord: extended cascade (TIER 1-4) must always
    produce a non-forbidden name. "Parada Aislada" is retired."""
    inp = StopTreatmentInput(
        operation="synthetic_insert",
        caller="test_treater",
        proposed_name="",
        proposed_lat=-1.5, proposed_lon=-79.5,   # somewhere far from Quito
    )
    r = treat_stop(inp, conn)
    assert r.success, r.error
    assert is_stop_name_forbidden(r.final_name) is False
    assert r.final_name.lower() != "parada aislada"


def test_synthetic_insert_rejects_node_id(conn):
    inp = StopTreatmentInput(
        operation="synthetic_insert",
        caller="test_treater",
        node_id=uuid.uuid4(),
        proposed_name="X", proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success is False
    assert "must NOT carry" in (r.error or "")


# ─── name_repair ─────────────────────────────────────────────────────────────


def test_name_repair_existing_node_with_forbidden_name(conn):
    # Seed: insert a node carrying a forbidden name directly.
    seeded_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO node_prod.nodes (node_id, geom, node_type, name, confidence) "
            "VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geometry(Point,4326), "
            "        'STOP', %s, 0.5)",
            (seeded_id, QUITO_LON, QUITO_LAT, "Parada (cafebabe)"),
        )

    inp = StopTreatmentInput(
        operation="name_repair",
        caller="test_treater",
        node_id=seeded_id,
        proposed_name="Parada (cafebabe)",
        proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success, r.error
    assert r.name_was_forbidden_input is True
    assert is_stop_name_forbidden(r.final_name) is False
    assert r.context_name_applied is True


def test_name_repair_requires_node_id(conn):
    inp = StopTreatmentInput(
        operation="name_repair",
        caller="test_treater",
        proposed_name="Anything",
        proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success is False
    assert "requires node_id" in (r.error or "")


# ─── snap_align ──────────────────────────────────────────────────────────────


def test_snap_align_updates_geom_and_name(conn):
    seeded_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO node_prod.nodes (node_id, geom, node_type, name, confidence) "
            "VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geometry(Point,4326), "
            "        'STOP', %s, 0.5)",
            (seeded_id, QUITO_LON, QUITO_LAT, "Old Name"),
        )

    new_lat = QUITO_LAT + 0.001
    new_lon = QUITO_LON + 0.001
    inp = StopTreatmentInput(
        operation="snap_align",
        caller="test_treater",
        node_id=seeded_id,
        proposed_name="Old Name",
        proposed_lat=new_lat, proposed_lon=new_lon,
    )
    r = treat_stop(inp, conn)
    assert r.success, r.error
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ST_Y(geom::geometry), ST_X(geom::geometry), name "
            "FROM node_prod.nodes WHERE node_id = %s",
            (seeded_id,),
        )
        lat, lon, name = cur.fetchone()
        assert abs(lat - new_lat) < 1e-9
        assert abs(lon - new_lon) < 1e-9


# ─── refill_adopt ────────────────────────────────────────────────────────────


def test_refill_adopt_validates_existing_node(conn):
    seeded_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO node_prod.nodes (node_id, geom, node_type, name, confidence) "
            "VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geometry(Point,4326), "
            "        'STOP', %s, 0.5)",
            (seeded_id, QUITO_LON, QUITO_LAT, "Original"),
        )

    inp = StopTreatmentInput(
        operation="refill_adopt",
        caller="test_treater",
        node_id=seeded_id,
        proposed_name="Original",   # already clean — should be unchanged
        proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success, r.error
    assert r.final_name == "Original"


# ─── ground_validate / cover_validate ────────────────────────────────────────


def test_ground_validate_inserts_when_no_node_id(conn):
    inp = StopTreatmentInput(
        operation="ground_validate",
        caller="test_treater",
        proposed_name="Estación X",
        proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success, r.error
    assert r.node_id is not None


def test_cover_validate_inserts_new_stop(conn):
    inp = StopTreatmentInput(
        operation="cover_validate",
        caller="test_treater",
        proposed_name="Stop on Gap",
        proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success, r.error
    assert r.node_id is not None


# ─── phase3_end_audit ────────────────────────────────────────────────────────


def test_phase3_end_audit_idempotent_when_clean(conn):
    seeded_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO node_prod.nodes (node_id, geom, node_type, name, confidence) "
            "VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geometry(Point,4326), "
            "        'STOP', %s, 0.5)",
            (seeded_id, QUITO_LON, QUITO_LAT, "Clean Name"),
        )
    inp = StopTreatmentInput(
        operation="phase3_end_audit",
        caller="test_treater",
        node_id=seeded_id,
        proposed_name="Clean Name",
        proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success, r.error
    assert r.final_name == "Clean Name"


# ─── unsupported / invalid ───────────────────────────────────────────────────


def test_unsupported_operation_rejected(conn):
    inp = StopTreatmentInput(
        operation="not_an_op",
        caller="test_treater",
        proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success is False
    assert "unsupported operation" in (r.error or "")


def test_caller_required(conn):
    inp = StopTreatmentInput(
        operation="synthetic_insert",
        caller="",
        proposed_name="X", proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success is False
    assert "caller" in (r.error or "")


# ─── audit-log writes always happen (success and failure) ────────────────────


def test_audit_log_written_on_success(conn):
    inp = StopTreatmentInput(
        operation="synthetic_insert",
        caller="test_treater_audit",
        proposed_name="Some Stop",
        proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success
    with conn.cursor() as cur:
        cur.execute(
            "SELECT operation, caller, success FROM node_prod.stop_treatment_log "
            "WHERE treatment_id = %s",
            (r.treatment_id,),
        )
        op, caller, success = cur.fetchone()
        assert op == "synthetic_insert"
        assert caller == "test_treater_audit"
        assert success is True


def test_audit_log_written_on_failure(conn):
    inp = StopTreatmentInput(
        operation="synthetic_insert",
        caller="test_treater_audit_fail",
        node_id=uuid.uuid4(),   # invalid for synthetic_insert
        proposed_name="X", proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
    )
    r = treat_stop(inp, conn)
    assert r.success is False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT success, error_reason FROM node_prod.stop_treatment_log "
            "WHERE treatment_id = %s",
            (r.treatment_id,),
        )
        success, reason = cur.fetchone()
        assert success is False
        assert "must NOT carry" in (reason or "")
