"""
Integration: synthesis/core._insert_node now routes through the treater.

This single migration covers three pipelines that share synthesis as
their exit:
  - Stage A2 grounding (a2_synthesis_bridge)
  - DR Type 2 stop coverage
  - osm_route_fill (when synthesis is invoked from rural gap)

Each test seeds + rolls back its own state.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from datamind_console.phases.phase3_routes.synthesis.core import _insert_node  # noqa: E402
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


# ─── stage_3a (poi_anchored_path_projected) ─────────────────────────────────

def test_stage_3a_synthesis_via_treater(conn):
    new_id = _insert_node(
        conn,
        node_id="",
        osm_id=-100001,
        lat=QUITO_LAT, lon=QUITO_LON,
        source="3a_poi_on_path",
        source_type="poi_anchored_path_projected",
        synthetic_confidence="high",
        route_id="00000000-0000-0000-0000-000000000001",
        anchor_name="Universidad Central",
        poi_osm_id=12345,
        poi_to_path_distance_m=8.5,
        path_projection_distance_m=2.1,
    )
    assert new_id
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, source_type, osm_id, poi_anchor_osm_id, "
            "       poi_to_path_distance_m, ST_Y(geom::geometry), ST_X(geom::geometry) "
            "FROM node_prod.nodes WHERE node_id = %s::uuid",
            (new_id,),
        )
        row = cur.fetchone()
    assert row is not None
    name, src_type, osm_id, poi_id, ppd, lat, lon = row
    assert src_type == "poi_anchored_path_projected"
    assert osm_id == -100001
    assert poi_id == 12345
    assert abs(float(ppd) - 8.5) < 1e-3
    # name went through the cascade (anchor_name was already clean → kept)
    assert name == "Universidad Central"


# ─── stage_3b (path_corridor_projected) ──────────────────────────────────────

def test_stage_3b_synthesis_via_treater(conn):
    new_id = _insert_node(
        conn,
        node_id="",
        osm_id=-100002,
        lat=QUITO_LAT, lon=QUITO_LON,
        source="3b_path_corridor",
        source_type="path_corridor_projected",
        synthetic_confidence="medium",
        route_id="00000000-0000-0000-0000-000000000002",
        anchor_name="Mercado Santa Clara",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_type, synthetic_confidence FROM node_prod.nodes "
            "WHERE node_id = %s::uuid",
            (new_id,),
        )
        st, sc = cur.fetchone()
    assert st == "path_corridor_projected"
    assert sc == "medium"


# ─── stage_3c (path_intersection) ───────────────────────────────────────────

def test_stage_3c_synthesis_via_treater(conn):
    new_id = _insert_node(
        conn,
        node_id="",
        osm_id=-100003,
        lat=QUITO_LAT, lon=QUITO_LON,
        source="3c_path_intersection",
        source_type="path_intersection",
        synthetic_confidence="medium",
        route_id="00000000-0000-0000-0000-000000000003",
        anchor_name="Av Amazonas y Av Patria",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_type FROM node_prod.nodes WHERE node_id = %s::uuid",
            (new_id,),
        )
        assert cur.fetchone()[0] == "path_intersection"


# ─── stage_4 (pure_synthesis) ───────────────────────────────────────────────

def test_stage_4_pure_synthesis_via_treater(conn):
    new_id = _insert_node(
        conn,
        node_id="",
        osm_id=-100004,
        lat=QUITO_LAT, lon=QUITO_LON,
        source="4_pure_synthesis",
        source_type="pure_synthesis",
        synthetic_confidence="low",
        route_id="00000000-0000-0000-0000-000000000004",
        anchor_name="Plaza Foch",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_type, synthetic_confidence FROM node_prod.nodes "
            "WHERE node_id = %s::uuid",
            (new_id,),
        )
        st, sc = cur.fetchone()
    assert st == "pure_synthesis"
    assert sc == "low"


# ─── place mapping created (treater contract) ───────────────────────────────

def test_synthesis_creates_place_mapping_via_treater(conn):
    new_id = _insert_node(
        conn,
        node_id="",
        osm_id=-100005,
        lat=QUITO_LAT, lon=QUITO_LON,
        source="3a_poi_on_path",
        source_type="poi_anchored_path_projected",
        synthetic_confidence="high",
        route_id="00000000-0000-0000-0000-000000000005",
        anchor_name="Hospital Eugenio Espejo",
    )
    with conn.cursor() as cur:
        # node_place_map row exists
        cur.execute(
            "SELECT npm.place_id, p.canonical_name, p.status "
            "FROM geo_prod.node_place_map npm "
            "JOIN geo_prod.places p ON p.place_id = npm.place_id "
            "WHERE npm.node_id = %s::uuid",
            (new_id,),
        )
        row = cur.fetchone()
    assert row is not None, "node_place_map row missing — treater contract violated"
    place_id, canonical, status = row
    assert status == "active"
    assert canonical  # non-empty


# ─── audit row written (treater contract) ───────────────────────────────────

def test_synthesis_writes_audit_row(conn):
    new_id = _insert_node(
        conn,
        node_id="",
        osm_id=-100006,
        lat=QUITO_LAT, lon=QUITO_LON,
        source="3a_poi_on_path",
        source_type="poi_anchored_path_projected",
        synthetic_confidence="high",
        route_id="00000000-0000-0000-0000-000000000006",
        anchor_name="Estacion La Y",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT operation, caller, success FROM node_prod.stop_treatment_log "
            "WHERE node_id = %s::uuid ORDER BY treated_at DESC LIMIT 1",
            (new_id,),
        )
        row = cur.fetchone()
    assert row is not None
    op, caller, success = row
    assert op == "synthetic_insert"
    assert caller.startswith("synthesis.core:")
    assert success is True


# ─── extras whitelist drops unknown keys ────────────────────────────────────

def test_extras_whitelist_drops_route_id_anchor_name(conn):
    """The pre-migration code referenced columns route_id and anchor_name
    that don't exist on node_prod.nodes. Verify the whitelist drops these
    silently (instead of raising column-does-not-exist)."""
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput,
        treat_stop,
    )
    res = treat_stop(
        StopTreatmentInput(
            operation="synthetic_insert",
            caller="test_extras_whitelist",
            proposed_name="Some Stop",
            proposed_lat=QUITO_LAT, proposed_lon=QUITO_LON,
            extras={
                "osm_id": -100007,                      # whitelisted ✓
                "route_id": "deadbeef",                 # NOT whitelisted — drop
                "anchor_name": "Some Anchor",           # NOT whitelisted — drop
                "poi_anchor_osm_id": 9999,              # whitelisted ✓
            },
        ),
        conn,
    )
    assert res.success
    with conn.cursor() as cur:
        cur.execute(
            "SELECT osm_id, poi_anchor_osm_id FROM node_prod.nodes "
            "WHERE node_id = %s::uuid",
            (res.node_id,),
        )
        osm, poi = cur.fetchone()
    assert osm == -100007
    assert poi == 9999
