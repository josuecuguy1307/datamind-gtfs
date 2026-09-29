"""
Integration: GREEK α swap (`re_entry_swap._insert_synthetic_node`) routes
through the universal stop-quality treater.

Pre-migration: a raw INSERT into node_prod.nodes — no name cascade, no
geo_prod.places mapping, no audit row.

Each test seeds + rolls back its own state.
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

# Ensure psycopg2 returns python uuid.UUID for ::uuid columns.
psycopg2.extras.register_uuid()

from hades.enforcers.re_entry_swap_service import _insert_synthetic_node  # noqa: E402
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


# ─── basic insert + return shape ────────────────────────────────────────────

def test_insert_returns_uuid_string(conn):
    with conn.cursor() as cur:
        new_id = _insert_synthetic_node(
            cur,
            lat=QUITO_LAT, lon=QUITO_LON,
            province="Sample Region",
            tier=1,
            synthetic_confidence="medium",
            operator_username="op_test",
            note="swap fill source=fix_gap0_tier1",
        )
    assert isinstance(new_id, str)
    # 36-char canonical uuid
    assert len(new_id) == 36 and new_id.count("-") == 4


# ─── columns set correctly on the inserted row ──────────────────────────────

def test_inserted_row_has_synthesis_provenance(conn):
    with conn.cursor() as cur:
        new_id = _insert_synthetic_node(
            cur,
            lat=QUITO_LAT, lon=QUITO_LON,
            province="Sample Region",
            tier=2,
            synthetic_confidence="high",
            operator_username="alice",
            note="swap fill source=fix_gap3_tier2",
        )
        cur.execute(
            "SELECT source, source_type, synthetic_confidence, "
            "       synthetic_created_by, synthetic_review_state, "
            "       chosen_tags, node_type, province "
            "FROM node_prod.nodes WHERE node_id = %s::uuid",
            (new_id,),
        )
        row = cur.fetchone()
    assert row is not None
    src, src_type, sc, created_by, review_state, tags, node_type, province = row
    assert src == "re_entry_swap"
    assert src_type == "pure_synthesis"
    assert sc == "high"
    assert created_by == "alice"
    assert review_state == "pending"
    assert node_type == "STOP"
    assert province == "Sample Region"
    # chosen_tags is jsonb — psycopg2 returns dict
    assert isinstance(tags, dict)
    assert tags.get("note") == "swap fill source=fix_gap3_tier2"
    assert tags.get("tier") == 2


# ─── name cascade ran (treater contract) ────────────────────────────────────

def test_name_cascade_produces_non_blank(conn):
    """Pre-migration the row would have NULL name; the treater forces the
    contextual cascade so we get a real Spanish name."""
    with conn.cursor() as cur:
        new_id = _insert_synthetic_node(
            cur,
            lat=QUITO_LAT, lon=QUITO_LON,
            province="Sample Region",
            tier=1,
            synthetic_confidence="medium",
            operator_username="op_test",
            note="swap fill source=fix_gap0_tier1",
        )
        cur.execute(
            "SELECT name FROM node_prod.nodes WHERE node_id = %s::uuid",
            (new_id,),
        )
        (name,) = cur.fetchone()
    assert name is not None
    assert name.strip() != ""


# ─── place mapping created (treater contract) ───────────────────────────────

def test_place_mapping_created(conn):
    with conn.cursor() as cur:
        new_id = _insert_synthetic_node(
            cur,
            lat=QUITO_LAT, lon=QUITO_LON,
            province="Sample Region",
            tier=1,
            synthetic_confidence="medium",
            operator_username="op_test",
            note="swap fill source=synthesized_5",
        )
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
    assert place_id is not None
    assert status == "active"
    assert canonical and canonical.strip()


# ─── audit row written (treater contract) ───────────────────────────────────

def test_audit_row_written(conn):
    with conn.cursor() as cur:
        new_id = _insert_synthetic_node(
            cur,
            lat=QUITO_LAT, lon=QUITO_LON,
            province="Sample Region",
            tier=1,
            synthetic_confidence="medium",
            operator_username="op_test",
            note="swap fill source=fix_gap1_tier1",
        )
        cur.execute(
            "SELECT operation, caller, success "
            "FROM node_prod.stop_treatment_log "
            "WHERE node_id = %s::uuid ORDER BY treated_at DESC LIMIT 1",
            (new_id,),
        )
        row = cur.fetchone()
    assert row is not None
    op, caller, success = row
    assert op == "synthetic_insert"
    assert caller == "re_entry_swap.synthetic_node"
    assert success is True


# ─── tier=None is preserved in chosen_tags ──────────────────────────────────

def test_tier_none_serialises_as_null(conn):
    """When the synthetic stop_id doesn't encode a tier, the caller passes
    tier=None. Verify it round-trips through chosen_tags as JSON null."""
    with conn.cursor() as cur:
        new_id = _insert_synthetic_node(
            cur,
            lat=QUITO_LAT, lon=QUITO_LON,
            province="Sample Region",
            tier=None,
            synthetic_confidence="low",
            operator_username="op_test",
            note="swap fill source=synthesized_2",
        )
        cur.execute(
            "SELECT chosen_tags FROM node_prod.nodes WHERE node_id = %s::uuid",
            (new_id,),
        )
        (tags,) = cur.fetchone()
    assert tags.get("tier") is None
    assert tags.get("note") == "swap fill source=synthesized_2"
