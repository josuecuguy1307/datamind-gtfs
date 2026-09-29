"""
Integration: synthesis/osm_route_fill `_insert_osm_fill_node` now
routes through the universal stop-quality treater.

Pre-migration: a raw INSERT that referenced non-existent columns
(lat, lon, route_id, anchor_name, poi_osm_id) — the SQL would fail at
runtime in the current schema. Post: routes through
``treat_stop(synthetic_insert)`` so the new node gets the contextual-
name cascade, a ``geo_prod.places`` row + mapping, and an audit row.

Each test rolls back its own transaction.
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

from datamind_console.phases.phase3_routes.synthesis.osm_route_fill import (  # noqa: E402
    _insert_osm_fill_node,
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


# ─── helper returns valid uuid string ───────────────────────────────────────

def test_returns_uuid_string(conn):
    new_id = _insert_osm_fill_node(
        conn,
        osm_id=-300001,
        lat=QUITO_LAT, lon=QUITO_LON,
        source="osm_route_fill:99:0",
        source_type="poi_anchored_path_projected",
        poi_osm_id=12345,
        poi_to_path_distance_m=8.5,
        osm_route_fill_context="relation_99_gap_0",
    )
    assert isinstance(new_id, str)
    assert len(new_id) == 36 and new_id.count("-") == 4


# ─── columns set on inserted row (real schema, not the broken pre-migration ones) ──

def test_inserted_row_has_osm_fill_provenance(conn):
    new_id = _insert_osm_fill_node(
        conn,
        osm_id=-300002,
        lat=QUITO_LAT, lon=QUITO_LON,
        source="osm_route_fill:42:1",
        source_type="poi_anchored_path_projected",
        poi_osm_id=98765,
        poi_to_path_distance_m=12.3,
        osm_route_fill_context="relation_42_gap_1",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT osm_id, source, source_type, synthetic_confidence, "
            "       poi_anchor_osm_id, poi_to_path_distance_m, "
            "       osm_route_fill_context "
            "FROM node_prod.nodes WHERE node_id = %s::uuid",
            (new_id,),
        )
        row = cur.fetchone()
    assert row is not None
    osm_id, src, src_type, sc, poi_anchor, ppd, ctx = row
    assert osm_id == -300002
    assert src == "osm_route_fill:42:1"
    assert src_type == "poi_anchored_path_projected"
    assert sc == "medium"
    assert poi_anchor == 98765
    assert abs(float(ppd) - 12.3) < 1e-3
    assert ctx == "relation_42_gap_1"


# ─── name cascade ran (treater contract) ────────────────────────────────────

def test_name_cascade_produces_non_blank(conn):
    """Pre-migration the row would have NULL name (and crash on the broken
    column refs). The treater forces the cascade so we get a real Spanish
    name."""
    new_id = _insert_osm_fill_node(
        conn,
        osm_id=-300003,
        lat=QUITO_LAT, lon=QUITO_LON,
        source="osm_route_fill:1:0",
        source_type="poi_anchored_path_projected",
        poi_osm_id=11111,
        poi_to_path_distance_m=5.0,
        osm_route_fill_context="relation_1_gap_0",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name FROM node_prod.nodes WHERE node_id = %s::uuid",
            (new_id,),
        )
        (name,) = cur.fetchone()
    assert name is not None and name.strip() != ""


# ─── place mapping created (treater contract) ───────────────────────────────

def test_place_mapping_created(conn):
    new_id = _insert_osm_fill_node(
        conn,
        osm_id=-300004,
        lat=QUITO_LAT, lon=QUITO_LON,
        source="osm_route_fill:2:0",
        source_type="poi_anchored_path_projected",
        poi_osm_id=22222,
        poi_to_path_distance_m=3.0,
        osm_route_fill_context="relation_2_gap_0",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT npm.place_id, p.status "
            "FROM geo_prod.node_place_map npm "
            "JOIN geo_prod.places p ON p.place_id = npm.place_id "
            "WHERE npm.node_id = %s::uuid",
            (new_id,),
        )
        row = cur.fetchone()
    assert row is not None, "place mapping missing"
    _, status = row
    assert status == "active"


# ─── audit row written with correct caller tag ──────────────────────────────

def test_audit_row_written(conn):
    new_id = _insert_osm_fill_node(
        conn,
        osm_id=-300005,
        lat=QUITO_LAT, lon=QUITO_LON,
        source="osm_route_fill:3:0",
        source_type="poi_anchored_path_projected",
        poi_osm_id=33333,
        poi_to_path_distance_m=7.7,
        osm_route_fill_context="relation_3_gap_0",
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
    assert caller == "osm_route_fill.gap_fill"
    assert success is True
