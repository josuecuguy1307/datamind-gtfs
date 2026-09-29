"""
Integration: pre_export_enforcer now routes name fixes through the treater.
Each test runs in a rolled-back transaction.
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

from datamind_console.phases.phase5_gtfs.pre_export_enforcer import (  # noqa: E402
    _treat_stops_for_name_repair,
)
from datamind_core.dsn import need_dsn

# INTEGRATION tests: they assume a database already populated with real data of a region.
# Skipped unless you set DB_DSN and DATAMIND_RUN_DB_TESTS=1 (see README, Tests section).
pytestmark = pytest.mark.skipif(
    not (os.environ.get("DB_DSN") and os.environ.get("DATAMIND_RUN_DB_TESTS") == "1"),
    reason="integration test: requires DB_DSN and DATAMIND_RUN_DB_TESTS=1",
)



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


def _seed_node(conn, name: str, lat: float = QUITO_LAT, lon: float = QUITO_LON):
    nid = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO node_prod.nodes (node_id, geom, node_type, name, confidence) "
            "VALUES (%s::uuid, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geometry(Point,4326), "
            "        'STOP', %s, 0.5)",
            (str(nid), lon, lat, name),
        )
    return nid


def test_treat_stops_for_name_repair_replaces_garbage(conn):
    seeded = _seed_node(conn, "Parada (cafebabe)")
    stops = [{"node_id": str(seeded), "name": "Parada (cafebabe)",
              "lat": QUITO_LAT, "lon": QUITO_LON}]
    fixed = _treat_stops_for_name_repair(
        stops, conn, caller="test_pre_export_enforcer_migration"
    )
    assert fixed == 1
    assert stops[0]["name"] != "Parada (cafebabe)"


def test_treat_stops_idempotent_on_clean_name(conn):
    seeded = _seed_node(conn, "Estación Plaza Foch")
    stops = [{"node_id": str(seeded), "name": "Estación Plaza Foch",
              "lat": QUITO_LAT, "lon": QUITO_LON}]
    fixed = _treat_stops_for_name_repair(
        stops, conn, caller="test_pre_export_enforcer_migration"
    )
    # Already-clean names produce no rewrite.
    assert fixed == 0
    assert stops[0]["name"] == "Estación Plaza Foch"


def test_audit_log_carries_caller_tag(conn):
    seeded = _seed_node(conn, "Parada (deadbeef)")
    stops = [{"node_id": str(seeded), "name": "Parada (deadbeef)",
              "lat": QUITO_LAT, "lon": QUITO_LON}]
    _treat_stops_for_name_repair(
        stops, conn, caller="pre_export_enforcer.garbage_names"
    )
    # Note: helper commits after fix; rollback restores everything except
    # the audit row we want to verify still got written within the same tx.
    # Re-issue queries after rollback won't see them. So check inside the
    # same connection before fixture rollback runs:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT operation, caller FROM node_prod.stop_treatment_log "
            "WHERE caller = %s AND treated_at >= NOW() - INTERVAL '5 minutes' "
            "ORDER BY treated_at DESC LIMIT 1",
            ("pre_export_enforcer.garbage_names",),
        )
        row = cur.fetchone()
        assert row is not None, "audit row missing for caller tag"
        assert row[0] == "name_repair"
        assert row[1] == "pre_export_enforcer.garbage_names"
