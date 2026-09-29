"""
Integration: phase2_semantics/client.py write paths now route through
the universal stop-quality treater.

Covers:
- `_bulk_update_node_prod_names` → `name_repair` per row
- `update_node_prod_location` → `snap_align` (geom + name re-validation)

Each test rolls back its own transaction.
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
# phase2_semantics has internal `src.settings` imports that resolve only
# when phase2_semantics/ is on sys.path. Add it before importing the
# Phase2Client.
_P2_ROOT = REPO / "phase2_semantics"
if str(_P2_ROOT) not in sys.path:
    sys.path.insert(0, str(_P2_ROOT))

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


# ─── _bulk_update_node_prod_names → name_repair ─────────────────────────────

def test_bulk_rename_repairs_names_via_treater(conn):
    """The bulk rename helper now calls treat_stop(name_repair) per row.
    We invoke it directly (bypasses the global normalizer's data flow)
    so we can test it in isolation."""
    from datamind_console.phases.phase2_semantics.client import Phase2Client

    nid_a = _seed_node(conn, "Parada (cafebabe)")
    nid_b = _seed_node(conn, "Parada Sin Nombre")

    client = Phase2Client.__new__(Phase2Client)
    rename_rows = [
        {"node_id": str(nid_a), "new_name": "Plaza Foch"},
        {"node_id": str(nid_b), "new_name": "Mercado Iñaquito"},
    ]
    n = client._bulk_update_node_prod_names(conn, rename_rows)
    assert n == 2

    with conn.cursor() as cur:
        cur.execute(
            "SELECT node_id::text, name FROM node_prod.nodes "
            "WHERE node_id = ANY(ARRAY[%s, %s]::uuid[]) ORDER BY node_id",
            (str(nid_a), str(nid_b)),
        )
        rows = sorted([(r[0], r[1]) for r in cur.fetchall()])
    by_id = dict(rows)
    assert by_id[str(nid_a)] == "Plaza Foch"
    assert by_id[str(nid_b)] == "Mercado Iñaquito"


def test_bulk_rename_writes_audit_per_row(conn):
    from datamind_console.phases.phase2_semantics.client import Phase2Client

    nid = _seed_node(conn, "Parada Vieja")
    client = Phase2Client.__new__(Phase2Client)
    n = client._bulk_update_node_prod_names(
        conn, [{"node_id": str(nid), "new_name": "Universidad Central"}],
    )
    assert n == 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT operation, caller, success FROM node_prod.stop_treatment_log "
            "WHERE node_id = %s::uuid ORDER BY treated_at DESC LIMIT 1",
            (str(nid),),
        )
        row = cur.fetchone()
    assert row is not None
    op, caller, success = row
    assert op == "name_repair"
    assert caller == "phase2_client.bulk_rename"
    assert success is True


def test_bulk_rename_skips_blank_input_rows(conn):
    from datamind_console.phases.phase2_semantics.client import Phase2Client

    nid = _seed_node(conn, "Parada Real")
    client = Phase2Client.__new__(Phase2Client)
    # Mix of valid + invalid rows. Pre-existing helper drops invalids
    # before issuing UPDATEs; treater flow keeps the same filter.
    n = client._bulk_update_node_prod_names(
        conn,
        [
            {"node_id": "", "new_name": "X"},
            {"node_id": str(nid), "new_name": ""},
            {"node_id": str(nid), "new_name": "Plaza La Y"},
        ],
    )
    assert n == 1


# ─── update_node_prod_location → snap_align ─────────────────────────────────

def test_update_location_relocates_node_via_treater(conn):
    """Direct treater call mirrors what update_node_prod_location does
    (without invoking the method, which uses its own _conn_ctx)."""
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput,
        treat_stop,
    )

    nid = _seed_node(conn, "Av Naciones Unidas", lat=QUITO_LAT, lon=QUITO_LON)
    new_lat, new_lon = -0.20, -78.49
    res = treat_stop(
        StopTreatmentInput(
            operation="snap_align",
            caller="phase2_client.update_location",
            node_id=str(nid),
            proposed_name="Av Naciones Unidas",
            proposed_lat=new_lat,
            proposed_lon=new_lon,
        ),
        conn,
    )
    assert res.success
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ST_Y(geom::geometry), ST_X(geom::geometry), name "
            "FROM node_prod.nodes WHERE node_id = %s::uuid",
            (str(nid),),
        )
        lat, lon, name = cur.fetchone()
    assert abs(float(lat) - new_lat) < 1e-6
    assert abs(float(lon) - new_lon) < 1e-6
    # Clean name passes through; the normalizer expands "Av" → "Av." but
    # otherwise leaves it intact.
    assert name in {"Av Naciones Unidas", "Av. Naciones Unidas"}


def test_update_location_repairs_forbidden_name_on_relocate(conn):
    """A relocate against a forbidden-name node also fixes the name —
    the snap_align treater contract."""
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput,
        treat_stop,
    )

    nid = _seed_node(conn, "Parada (cafebabe)")
    res = treat_stop(
        StopTreatmentInput(
            operation="snap_align",
            caller="phase2_client.update_location",
            node_id=str(nid),
            proposed_name="Parada (cafebabe)",
            proposed_lat=-0.21, proposed_lon=-78.48,
        ),
        conn,
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
