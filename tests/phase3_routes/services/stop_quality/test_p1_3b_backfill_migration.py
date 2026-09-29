"""
Integration: P1.3B backfill executor's `_promote_to_node_prod` now
routes through the universal stop-quality treater.

Pre-migration: raw INSERT into node_prod.nodes — no name cascade, no
geo_prod.places mapping, no audit row.

Each test seeds + rolls back its own state. The executor commits on
success, so we use a savepoint pattern: open one outer transaction,
swap the connection's `commit` and `rollback` to be no-ops for the
duration of the test, then rollback at fixture teardown.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

psycopg2.extras.register_uuid()

from datamind_console.orchestrator.executors.p1_3b_backfill_executor import (  # noqa: E402
    BackfillExecutor,
)
from datamind_console.phases.phase3_routes.stop_grounding.backfill_contracts import (  # noqa: E402
    MissingNodeCandidate,
    SignalType,
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


class _CommitNoOpConn:
    """Proxy that no-ops commit/rollback; everything else delegates.

    psycopg2 connection attributes are read-only at the C level, so we
    can't monkeypatch the methods directly. Wrapping is the cleanest
    way to keep the executor's `commit()` from persisting test rows.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self):  # noqa: D401
        pass

    def rollback(self):
        pass


@pytest.fixture
def conn():
    """Yield a (proxy, real_conn) pair. The proxy is what the executor
    uses; commit/rollback are neutralised. Teardown rolls back the real
    connection so all work disappears."""
    real = psycopg2.connect(need_dsn(DSN))
    real.autocommit = False
    proxy = _CommitNoOpConn(real)
    try:
        yield proxy
    finally:
        try:
            real.rollback()
        finally:
            real.close()


def _make_candidate(province: str = "Sample Region") -> MissingNodeCandidate:
    return MissingNodeCandidate(
        candidate_id="cand_test_1",
        route_id="00000000-0000-0000-0000-000000000010",
        canton="Quito",
        province=province,
        signal_type=SignalType.UNFILLED_GAP,
        expected_lat=QUITO_LAT,
        expected_lon=QUITO_LON,
        search_radius_m=60.0,
    )


def _make_executor() -> BackfillExecutor:
    return BackfillExecutor(auto_promote_threshold=0.75)


# ─── basic insert + return shape ────────────────────────────────────────────

def test_promote_returns_uuid_string(conn):
    ex = _make_executor()
    hit = {
        "id": -200001,
        "lat": QUITO_LAT, "lon": QUITO_LON,
        "tags": {"highway": "bus_stop", "name": "Av Naciones Unidas"},
    }
    node_id = ex._promote_to_node_prod(
        hit, "Av Naciones Unidas", _make_candidate(), 0.85, conn,
    )
    assert isinstance(node_id, str)
    assert len(node_id) == 36 and node_id.count("-") == 4


# ─── source + osm_id + chosen_tags persisted ────────────────────────────────

def test_inserted_row_has_backfill_provenance(conn):
    ex = _make_executor()
    hit = {
        "id": -200002,
        "lat": QUITO_LAT, "lon": QUITO_LON,
        "tags": {"highway": "bus_stop", "name": "Plaza San Francisco"},
    }
    cand = _make_candidate()
    node_id = ex._promote_to_node_prod(hit, "Plaza San Francisco", cand, 0.80, conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, osm_id, chosen_tags, node_type, province, confidence "
            "FROM node_prod.nodes WHERE node_id = %s::uuid",
            (node_id,),
        )
        row = cur.fetchone()
    assert row is not None
    src, osm, tags, node_type, province, conf = row
    assert src == "backfill"
    assert osm == -200002
    assert node_type == "STOP"
    assert province == "Sample Region"
    assert abs(float(conf) - 0.80) < 1e-3
    # chosen_tags carries OSM tags + backfill metadata
    assert tags.get("highway") == "bus_stop"
    assert tags.get("_backfill_route_id") == cand.route_id
    assert tags.get("_backfill_signal_type") == "unfilled_gap"
    assert tags.get("_backfill_canton") == "Quito"


# ─── bus_station hits land as STOP (DB constraint only allows STOP|POI) ─────

def test_bus_station_promotes_as_stop(conn):
    """The DB CHECK constraint on `node_type` only accepts STOP or POI.
    Pre-migration the executor tried to insert STATION for `amenity=bus_station`,
    which silently failed; the migration drops that broken branch."""
    ex = _make_executor()
    hit = {
        "id": -200003,
        "lat": QUITO_LAT, "lon": QUITO_LON,
        "tags": {"amenity": "bus_station", "name": "Terminal Quitumbe"},
    }
    node_id = ex._promote_to_node_prod(
        hit, "Terminal Quitumbe", _make_candidate(), 0.92, conn,
    )
    assert node_id != ""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT node_type FROM node_prod.nodes WHERE node_id = %s::uuid",
            (node_id,),
        )
        (nt,) = cur.fetchone()
    assert nt == "STOP"


# ─── name cascade ran (treater contract) ────────────────────────────────────

def test_blank_name_triggers_contextual_cascade(conn):
    """Pre-migration a hit with no name would land with name='' or None.
    The treater forces the cascade to produce a real Spanish name."""
    ex = _make_executor()
    hit = {
        "id": -200004,
        "lat": QUITO_LAT, "lon": QUITO_LON,
        "tags": {"highway": "bus_stop"},   # no name tag
    }
    node_id = ex._promote_to_node_prod(hit, "", _make_candidate(), 0.65, conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name FROM node_prod.nodes WHERE node_id = %s::uuid",
            (node_id,),
        )
        (name,) = cur.fetchone()
    assert name is not None and name.strip() != ""


# ─── place mapping + audit row (treater contracts) ──────────────────────────

def test_place_mapping_and_audit_written(conn):
    ex = _make_executor()
    hit = {
        "id": -200005,
        "lat": QUITO_LAT, "lon": QUITO_LON,
        "tags": {"highway": "bus_stop", "name": "Hospital Eugenio Espejo"},
    }
    node_id = ex._promote_to_node_prod(
        hit, "Hospital Eugenio Espejo", _make_candidate(), 0.85, conn,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT npm.place_id, p.status "
            "FROM geo_prod.node_place_map npm "
            "JOIN geo_prod.places p ON p.place_id = npm.place_id "
            "WHERE npm.node_id = %s::uuid",
            (node_id,),
        )
        place_row = cur.fetchone()
        cur.execute(
            "SELECT operation, caller, success "
            "FROM node_prod.stop_treatment_log "
            "WHERE node_id = %s::uuid ORDER BY treated_at DESC LIMIT 1",
            (node_id,),
        )
        audit_row = cur.fetchone()
    assert place_row is not None, "place mapping missing"
    _, status = place_row
    assert status == "active"
    assert audit_row is not None
    op, caller, success = audit_row
    assert op == "synthetic_insert"
    assert caller == "p1_3b_backfill.promote"
    assert success is True


# ─── failure path returns "" and rolls back the inner txn ───────────────────

def test_failure_returns_empty_string(conn, monkeypatch):
    """If the treater raises, _promote_to_node_prod must return ''."""
    from phase3_routes.services.stop_quality import stop_treater

    def _boom(inp, c):
        raise RuntimeError("induced failure")

    monkeypatch.setattr(stop_treater, "treat_stop", _boom)
    # Re-import in the executor module's namespace too — the function does
    # a local import, so monkeypatching the source module is enough as long
    # as the function imports it fresh per call.
    import datamind_console.orchestrator.executors.p1_3b_backfill_executor as p13b
    monkeypatch.setattr(
        "phase3_routes.services.stop_quality.treat_stop", _boom, raising=False,
    )

    ex = _make_executor()
    hit = {"id": -200006, "lat": QUITO_LAT, "lon": QUITO_LON, "tags": {}}
    result = ex._promote_to_node_prod(hit, "x", _make_candidate(), 0.5, conn)
    # The function imports treat_stop locally — the monkeypatch above on
    # the package re-export catches it.
    assert result == "", f"expected empty string on failure, got {result!r}"
