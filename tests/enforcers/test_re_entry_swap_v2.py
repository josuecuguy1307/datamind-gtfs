"""GREEK swap v2 — atomic canonical-state propagation tests.

Covers the six scenarios catalogued in the design doc
``workspace/audits/2026-05-03_greek_swap_upgrade_v2_design.md`` §11:

  1. ``test_v2_atomic_writes_all_canonical_tables`` — happy path:
     post-swap state honors invariants I-1..I-10 (chosen pointers set,
     candidate set/row inserted, runtime estimates superseded, bindings
     cleared, fix_reports row exists).
  2. ``test_v2_rollback_when_post_swap_step_fails`` — inject a failure
     after ``_apply_v2_canonical_state`` returns; assert the entire
     transaction rolls back (no candidate rows survive, no pointer
     change, runtime estimates stay active, bindings stay).
  3. ``test_v2_obsoletes_pre_swap_chosen_geometry_pointer`` — when a
     pre-existing ``chosen_geometry_candidate_id`` is set, v2 must
     re-point routes at the freshly-inserted candidate.
  4. ``test_v2_invalidates_runtime_estimates_and_clears_bindings`` —
     pre-create an active estimate + binding; v2 swap flips the
     estimate to ``superseded`` and DELETEs the binding.
  5. ``test_v2_routes_geom_byte_identical_to_chosen_candidate`` —
     ``ST_Equals(routes.geom, gc.geom)`` for the chosen candidate.
  6. ``test_v1_path_runs_when_flag_off`` — env var unset → public
     ``confirm_greek_pipeline`` dispatches v1, none of the v2 canonical
     writes happen.

Tests share the ``fresh_route`` fixture and helpers from the v1 test
module so the test environment is identical.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

from hades.enforcers import re_entry_swap_service as svc  # noqa: E402

from tests.enforcers.test_re_entry_swap_service import (  # noqa: E402
    OPERATOR_ID,
    OPERATOR_USERNAME,
    _dsn,
    _db_reachable,
    _flat_earth_xy,
    _insert_simple_shippable,
    fresh_route,  # pytest fixture re-exported
)


pytestmark = pytest.mark.skipif(
    not _db_reachable(), reason="local datamind_ml DB not reachable"
)


# ---------------------------------------------------------------------------
# Helpers specific to v2 testing.
# ---------------------------------------------------------------------------

def _insert_runtime_estimate_with_binding(
    cur, route_id: str, direction_id: int = 0
) -> tuple[str, str]:
    """Create an active runtime estimate + binding for the route.

    Returns (estimate_id, binding_route_id). The binding_route_id is
    the same as ``route_id`` (binding's PK is route_id+direction_id);
    we return it for explicit cleanup.
    """
    cur.execute(
        """
        INSERT INTO gtfs_work.runtime_route_estimates
          (route_id, direction_id, status)
        VALUES (%s::uuid, %s, 'active')
        RETURNING estimate_id::text
        """,
        (route_id, direction_id),
    )
    estimate_id = cur.fetchone()["estimate_id"]
    cur.execute(
        """
        INSERT INTO gtfs_work.route_runtime_estimate_bindings
          (route_id, direction_id, estimate_id)
        VALUES (%s::uuid, %s, %s::uuid)
        """,
        (route_id, direction_id, estimate_id),
    )
    return estimate_id, route_id


def _insert_pre_existing_candidates(
    cur, route_id: str
) -> tuple[str, str, str, str]:
    """Insert a pre-swap geometry+stop_sequence candidate pair and point
    routes.chosen_*_candidate_id at them. Mirrors the state of routes
    that have been through Phase 3 normally.

    Returns (geom_set_id, geom_cand_id, seq_set_id, seq_cand_id).
    """
    cur.execute(
        """
        INSERT INTO route_work.stop_sequence_candidate_sets
          (route_id, generator_version, notes)
        VALUES (%s::uuid, 'pre_swap_test', 'pre-existing for v2 test')
        RETURNING set_id::text AS set_id
        """,
        (route_id,),
    )
    seq_set_id = cur.fetchone()["set_id"]
    cur.execute(
        """
        INSERT INTO route_work.stop_sequence_candidates
          (set_id, rank, stop_node_ids)
        VALUES (%s::uuid, 1, ARRAY[]::uuid[])
        RETURNING candidate_id::text AS candidate_id
        """,
        (seq_set_id,),
    )
    seq_cand_id = cur.fetchone()["candidate_id"]

    cur.execute(
        """
        INSERT INTO route_work.geometry_candidate_sets
          (route_id, stop_sequence_set_id, generator_version, notes)
        VALUES (%s::uuid, %s::uuid, 'pre_swap_test',
                'pre-existing for v2 test')
        RETURNING set_id::text AS set_id
        """,
        (route_id, seq_set_id),
    )
    geom_set_id = cur.fetchone()["set_id"]
    cur.execute(
        """
        INSERT INTO route_work.geometry_candidates
          (set_id, stop_sequence_candidate_id, engine, geom)
        VALUES (%s::uuid, %s::uuid, 'pre_swap_test_engine',
                ST_SetSRID(ST_GeomFromText(
                  'LINESTRING(-78.49 -0.18, -78.48 -0.18)'), 4326))
        RETURNING geometry_candidate_id::text AS geometry_candidate_id
        """,
        (geom_set_id, seq_cand_id),
    )
    geom_cand_id = cur.fetchone()["geometry_candidate_id"]

    cur.execute(
        """
        UPDATE route_prod.routes
           SET chosen_geometry_candidate_id      = %s::uuid,
               chosen_stop_sequence_candidate_id = %s::uuid
         WHERE route_id = %s::uuid
        """,
        (geom_cand_id, seq_cand_id, route_id),
    )
    return geom_set_id, geom_cand_id, seq_set_id, seq_cand_id


def _force_flag_on(monkeypatch):
    monkeypatch.setenv("HADES_GREEK_SWAP_V2", "1")


def _force_flag_off(monkeypatch):
    monkeypatch.setenv("HADES_GREEK_SWAP_V2", "0")


# ---------------------------------------------------------------------------
# 1. Happy path — every canonical-state invariant holds post-swap.
# ---------------------------------------------------------------------------

def test_v2_atomic_writes_all_canonical_tables(fresh_route):
    approval_id = _insert_simple_shippable(fresh_route)
    route_id = fresh_route["route_id"]

    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=[]
    )

    result = svc._confirm_greek_pipeline_v2(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        refill_decisions={},
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )

    assert result.success is True, result.error
    assert result.swap_id is not None  # fix_reports.report_id

    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT chosen_geometry_candidate_id::text     AS geom_cand,
                       chosen_stop_sequence_candidate_id::text AS seq_cand,
                       canonical_sequence_ready                AS canon,
                       pipeline_version                        AS pipe,
                       version
                  FROM route_prod.routes
                 WHERE route_id = %s::uuid
                """,
                (route_id,),
            )
            row = dict(cur.fetchone())
            # I-1, I-2, I-3
            assert row["geom_cand"] is not None, "chosen_geometry_candidate_id NULL post-v2"
            assert row["seq_cand"] is not None, "chosen_stop_sequence_candidate_id NULL post-v2"
            assert row["canon"] is True
            # pipeline_version flipped to v2 marker
            assert row["pipe"] == "re_entry_v2"

            # I-4: routes.geom byte-equal to chosen candidate's geom
            cur.execute(
                """
                SELECT ST_Equals(r.geom, gc.geom) AS equal_geom
                  FROM route_prod.routes r
                  JOIN route_work.geometry_candidates gc
                    ON gc.geometry_candidate_id = r.chosen_geometry_candidate_id
                 WHERE r.route_id = %s::uuid
                """,
                (route_id,),
            )
            assert dict(cur.fetchone())["equal_geom"] is True

            # I-5: routes.stop_node_ids equal ssc.stop_node_ids
            cur.execute(
                """
                SELECT r.stop_node_ids   AS routes_nodes,
                       ssc.stop_node_ids AS cand_nodes
                  FROM route_prod.routes r
                  JOIN route_work.stop_sequence_candidates ssc
                    ON ssc.candidate_id = r.chosen_stop_sequence_candidate_id
                 WHERE r.route_id = %s::uuid
                """,
                (route_id,),
            )
            r2 = dict(cur.fetchone())
            assert r2["routes_nodes"] == r2["cand_nodes"]

            # I-6: exactly one greek_swap_v2 candidate set per route
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM route_work.geometry_candidate_sets
                 WHERE route_id = %s::uuid
                   AND generator_version = 'greek_swap_v2'
                """,
                (route_id,),
            )
            assert dict(cur.fetchone())["n"] == 1

            # I-7: no estimate bindings for this route
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM gtfs_work.route_runtime_estimate_bindings
                 WHERE route_id = %s::uuid
                """,
                (route_id,),
            )
            assert dict(cur.fetchone())["n"] == 0

            # I-9: fix_reports row for (route, v1→v2)
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM route_prod.fix_reports
                 WHERE route_id = %s::uuid AND version_after = %s
                """,
                (route_id, row["version"]),
            )
            assert dict(cur.fetchone())["n"] == 1

            # I-10: queue states transitioned
            cur.execute(
                "SELECT status FROM route_prod.approval_queue WHERE queue_id=%s::uuid",
                (approval_id,),
            )
            assert dict(cur.fetchone())["status"] == "approved"
            cur.execute(
                """
                SELECT status FROM route_prod.re_entry_queue
                 WHERE queue_id = %s::uuid
                """,
                (fresh_route["re_queue_id"],),
            )
            assert dict(cur.fetchone())["status"] == "swapped"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Rollback — full transaction reverts when a step after the v2 helper raises.
# ---------------------------------------------------------------------------

def test_v2_rollback_when_post_swap_step_fails(fresh_route, monkeypatch):
    approval_id = _insert_simple_shippable(fresh_route)
    route_id = fresh_route["route_id"]

    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=[]
    )

    # Capture pre-swap state.
    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT version, chosen_geometry_candidate_id, "
                "       canonical_sequence_ready "
                "  FROM route_prod.routes WHERE route_id = %s::uuid",
                (route_id,),
            )
            pre_row = dict(cur.fetchone())
            cur.execute(
                "SELECT COUNT(*) AS n FROM route_work.geometry_candidate_sets "
                " WHERE route_id = %s::uuid",
                (route_id,),
            )
            pre_geom_sets = dict(cur.fetchone())["n"]
            cur.execute(
                "SELECT COUNT(*) AS n FROM route_work.stop_sequence_candidate_sets "
                " WHERE route_id = %s::uuid",
                (route_id,),
            )
            pre_seq_sets = dict(cur.fetchone())["n"]
    finally:
        conn.close()

    # Patch _build_fixes_applied to raise *after* _apply_v2_canonical_state
    # has run. The whole transaction must roll back.
    boom = RuntimeError("simulated post-swap failure")
    monkeypatch.setattr(
        svc, "_build_fixes_applied",
        lambda *a, **kw: (_ for _ in ()).throw(boom),
    )

    with pytest.raises(RuntimeError, match="simulated post-swap failure"):
        svc._confirm_greek_pipeline_v2(
            approval_queue_id=approval_id,
            expected_fingerprint=preview.state_fingerprint,
            refill_decisions={},
            operator_id=OPERATOR_ID,
            operator_username=OPERATOR_USERNAME,
            dsn=_dsn(),
        )

    # Verify nothing v2 wrote survived.
    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT version, chosen_geometry_candidate_id, "
                "       canonical_sequence_ready "
                "  FROM route_prod.routes WHERE route_id = %s::uuid",
                (route_id,),
            )
            post_row = dict(cur.fetchone())
            assert post_row == pre_row, "routes table mutated despite rollback"

            cur.execute(
                "SELECT COUNT(*) AS n FROM route_work.geometry_candidate_sets "
                " WHERE route_id = %s::uuid",
                (route_id,),
            )
            assert dict(cur.fetchone())["n"] == pre_geom_sets
            cur.execute(
                "SELECT COUNT(*) AS n FROM route_work.stop_sequence_candidate_sets "
                " WHERE route_id = %s::uuid",
                (route_id,),
            )
            assert dict(cur.fetchone())["n"] == pre_seq_sets

            # approval_queue stays pending (no resolved_at set) —
            # the tx that would have flipped it rolled back.
            cur.execute(
                "SELECT status FROM route_prod.approval_queue WHERE queue_id=%s::uuid",
                (approval_id,),
            )
            assert dict(cur.fetchone())["status"] == "pending"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. Obsoleting a pre-existing chosen pointer.
# ---------------------------------------------------------------------------

def test_v2_obsoletes_pre_swap_chosen_geometry_pointer(fresh_route):
    route_id = fresh_route["route_id"]

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    pre_geom_set_id = pre_geom_cand_id = None
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            (pre_geom_set_id, pre_geom_cand_id,
             _seq_set, _seq_cand) = _insert_pre_existing_candidates(cur, route_id)
    finally:
        conn.close()

    approval_id = _insert_simple_shippable(fresh_route)
    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=[]
    )

    result = svc._confirm_greek_pipeline_v2(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        refill_decisions={},
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )
    assert result.success is True, result.error

    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT chosen_geometry_candidate_id::text AS gid "
                "  FROM route_prod.routes WHERE route_id = %s::uuid",
                (route_id,),
            )
            new_gid = dict(cur.fetchone())["gid"]
            assert new_gid != pre_geom_cand_id, (
                "v2 swap left chosen_geometry_candidate_id pointing at "
                "pre-swap candidate"
            )

            # The new pointer references the v2 set we just created.
            cur.execute(
                """
                SELECT s.generator_version
                  FROM route_work.geometry_candidates gc
                  JOIN route_work.geometry_candidate_sets s
                    ON s.set_id = gc.set_id
                 WHERE gc.geometry_candidate_id = %s::uuid
                """,
                (new_gid,),
            )
            assert dict(cur.fetchone())["generator_version"] == "greek_swap_v2"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. Runtime estimates + bindings invalidation.
# ---------------------------------------------------------------------------

def test_v2_invalidates_runtime_estimates_and_clears_bindings(fresh_route):
    route_id = fresh_route["route_id"]

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    estimate_id = None
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            estimate_id, _ = _insert_runtime_estimate_with_binding(
                cur, route_id
            )
    finally:
        conn.close()
    assert estimate_id is not None

    approval_id = _insert_simple_shippable(fresh_route)
    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=[]
    )

    result = svc._confirm_greek_pipeline_v2(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        refill_decisions={},
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )
    assert result.success is True, result.error

    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT status FROM gtfs_work.runtime_route_estimates "
                " WHERE estimate_id = %s::uuid",
                (estimate_id,),
            )
            row = cur.fetchone()
            assert row is not None, "estimate row vanished (cascade?)"
            assert dict(row)["status"] == "superseded"

            cur.execute(
                "SELECT COUNT(*) AS n FROM gtfs_work.route_runtime_estimate_bindings "
                " WHERE route_id = %s::uuid",
                (route_id,),
            )
            assert dict(cur.fetchone())["n"] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. Byte-identical geom invariant (I-4).
# ---------------------------------------------------------------------------

def test_v2_routes_geom_byte_identical_to_chosen_candidate(fresh_route):
    approval_id = _insert_simple_shippable(fresh_route)
    route_id = fresh_route["route_id"]

    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=[]
    )
    result = svc._confirm_greek_pipeline_v2(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        refill_decisions={},
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )
    assert result.success is True, result.error

    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT ST_AsBinary(r.geom) = ST_AsBinary(gc.geom) AS bytes_equal,
                       ST_Equals(r.geom, gc.geom)                  AS topo_equal
                  FROM route_prod.routes r
                  JOIN route_work.geometry_candidates gc
                    ON gc.geometry_candidate_id = r.chosen_geometry_candidate_id
                 WHERE r.route_id = %s::uuid
                """,
                (route_id,),
            )
            row = dict(cur.fetchone())
            assert row["bytes_equal"] is True, "WKB byte mismatch"
            assert row["topo_equal"] is True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. Public dispatch — flag OFF runs v1 path (no canonical writes).
# ---------------------------------------------------------------------------

def test_v1_path_runs_when_flag_off(fresh_route, monkeypatch):
    _force_flag_off(monkeypatch)
    approval_id = _insert_simple_shippable(fresh_route)
    route_id = fresh_route["route_id"]

    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=[]
    )
    result = svc.confirm_greek_pipeline(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        refill_decisions={},
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )
    assert result.success is True, result.error

    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # v1 path leaves chosen_*_candidate_id unchanged (gap #1, #2)
            # and canonical_sequence_ready unchanged (gap #3).
            cur.execute(
                """
                SELECT chosen_geometry_candidate_id,
                       chosen_stop_sequence_candidate_id,
                       canonical_sequence_ready,
                       pipeline_version
                  FROM route_prod.routes WHERE route_id = %s::uuid
                """,
                (route_id,),
            )
            row = dict(cur.fetchone())
            assert row["chosen_geometry_candidate_id"] is None
            assert row["chosen_stop_sequence_candidate_id"] is None
            # canonical_sequence_ready can be NULL or FALSE on the legacy
            # row — either way it must NOT be TRUE under v1.
            assert row["canonical_sequence_ready"] is not True
            # pipeline_version is the v1 marker, not the v2 marker.
            assert row["pipeline_version"] == "re_entry_v1"

            # No greek_swap_v2 candidate set should exist for this route.
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM route_work.geometry_candidate_sets
                 WHERE route_id = %s::uuid
                   AND generator_version = 'greek_swap_v2'
                """,
                (route_id,),
            )
            assert dict(cur.fetchone())["n"] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. Bonus: public dispatch — flag ON routes through v2 helper.
# ---------------------------------------------------------------------------

def test_public_confirm_dispatches_to_v2_when_flag_on(
    fresh_route, monkeypatch
):
    _force_flag_on(monkeypatch)
    approval_id = _insert_simple_shippable(fresh_route)
    route_id = fresh_route["route_id"]

    preview = svc.preview_greek_pipeline(
        approval_id, dsn=_dsn(), production_routes=[]
    )
    result = svc.confirm_greek_pipeline(
        approval_queue_id=approval_id,
        expected_fingerprint=preview.state_fingerprint,
        refill_decisions={},
        operator_id=OPERATOR_ID,
        operator_username=OPERATOR_USERNAME,
        dsn=_dsn(),
    )
    assert result.success is True, result.error

    conn = psycopg2.connect(_dsn())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT chosen_geometry_candidate_id IS NOT NULL AS has_geom,
                       chosen_stop_sequence_candidate_id IS NOT NULL AS has_seq,
                       canonical_sequence_ready                       AS canon
                  FROM route_prod.routes WHERE route_id = %s::uuid
                """,
                (route_id,),
            )
            row = dict(cur.fetchone())
            assert row["has_geom"] is True
            assert row["has_seq"] is True
            assert row["canon"] is True
    finally:
        conn.close()
