"""End-of-Phase-3 sweep — exercises phase3_end_sweep_for_stops."""
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

from datamind_console.persistence.route_prod_writer import phase3_end_sweep_for_stops  # noqa: E402
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


def _seed(conn, name, lat=QUITO_LAT, lon=QUITO_LON):
    nid = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO node_prod.nodes (node_id, geom, node_type, name, confidence) "
            "VALUES (%s::uuid, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geometry(Point,4326), "
            "        'STOP', %s, 0.5)",
            (str(nid), lon, lat, name),
        )
    return nid


def test_sweep_repairs_only_contaminated(conn):
    clean = _seed(conn, "Estación Plaza Foch")
    garbage = _seed(conn, "Parada (cafef00d)")
    blank = _seed(conn, "")

    summary = phase3_end_sweep_for_stops(
        [clean, garbage, blank], conn, caller="test_phase3_end_sweep",
    )
    assert summary["total"] == 3
    assert summary["fixed"] >= 2  # at minimum, garbage + blank get repaired
    assert summary["failed"] == 0


def test_sweep_skips_unknown_node_ids(conn):
    summary = phase3_end_sweep_for_stops(
        [uuid.uuid4(), uuid.uuid4()], conn, caller="test_phase3_end_sweep_unknown",
    )
    # Both unknown — nothing to process; no failures (we just skip).
    assert summary == {"total": 0, "fixed": 0, "failed": 0}
