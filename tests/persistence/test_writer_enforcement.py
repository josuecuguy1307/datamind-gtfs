"""Enforcement tests for datamind_console.persistence.route_prod_writer.

These tests pin the wrapper's safety contract:

  1. Mandatory fields (province, source_type, pipeline_version, route_id)
     are validated eagerly.
  2. "Skip if None" preserves the legacy COALESCE behaviour — any column
     whose value is None is absent from both the INSERT and the DO UPDATE
     clauses.
  3. shape={'raw'|'wkt'|'geojson'} each generate a sensible placeholder
     and parameter pairing.
  4. stops accepts either list[str] or list[dict{node_id/stop_id, order}].
  5. patch_route_prod_fields rejects non-allow-listed columns.
  6. patch_route_prod_fields is a silent no-op when the row is absent
     (rowcount=0, no error).

The wrapper does not own the connection; these tests use a fake psycopg
cursor that captures SQL + params so we can assert the UPSERT shape
without needing a live database.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import os
import pytest

from datamind_console.persistence import (
    BulkWriteResult,
    WriteResult,
    delete_route_prod,
    delete_routes_prod_bulk,
    patch_route_prod_fields,
    prune_stop_node_id_from_routes,
    write_to_route_prod,
)
from datamind_console.persistence.route_prod_writer import WriterValidationError

# INTEGRATION tests: they assume a database already populated with real data of a region.
# Skipped unless you set DB_DSN and DATAMIND_RUN_DB_TESTS=1 (see README, Tests section).
pytestmark = pytest.mark.skipif(
    not (os.environ.get("DB_DSN") and os.environ.get("DATAMIND_RUN_DB_TESTS") == "1"),
    reason="integration test: requires DB_DSN and DATAMIND_RUN_DB_TESTS=1",
)



# ---------------------------------------------------------------------------
# Fake cursor / connection
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, rowcount: int = 1, fetch_rows=None):
        self.sql: str | None = None
        self.params: object = None
        self.rowcount = rowcount
        self._fetch_rows = list(fetch_rows or [])

    def execute(self, sql: str, params):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return list(self._fetch_rows)

    def fetchone(self):
        return self._fetch_rows[0] if self._fetch_rows else None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _FakeConn:
    def __init__(self, rowcount: int = 1, fetch_rows=None):
        self.cur = _FakeCursor(rowcount=rowcount, fetch_rows=fetch_rows)

    def cursor(self):
        return self.cur


# ---------------------------------------------------------------------------
# write_to_route_prod validation
# ---------------------------------------------------------------------------

_ROUTE_ID = "11111111-1111-1111-1111-111111111111"
_STOP_A = "22222222-2222-2222-2222-222222222222"
_GEOM_WKT = "LINESTRING(-78.5 -0.2, -78.4 -0.2)"


def _minimal_kwargs(**overrides):
    kwargs = dict(
        route_code=_ROUTE_ID,
        route_data={"route_id": _ROUTE_ID, "province": "sample_region"},
        stops=[_STOP_A],
        shape={"wkt": _GEOM_WKT},
        source_type="unit_test",
        pipeline_version="tests.writer_enforcement",
        conn=_FakeConn(),
    )
    kwargs.update(overrides)
    return kwargs


def test_missing_province_raises():
    with pytest.raises(WriterValidationError, match="province"):
        write_to_route_prod(
            **_minimal_kwargs(route_data={"route_id": _ROUTE_ID})
        )


def test_missing_source_type_raises():
    with pytest.raises(WriterValidationError, match="source_type"):
        write_to_route_prod(**_minimal_kwargs(source_type=""))


def test_missing_pipeline_version_raises():
    with pytest.raises(WriterValidationError, match="pipeline_version"):
        write_to_route_prod(**_minimal_kwargs(pipeline_version=""))


def test_missing_route_id_raises():
    with pytest.raises(WriterValidationError, match="route_id"):
        write_to_route_prod(
            **_minimal_kwargs(route_data={"province": "sample_region"})
        )


def test_invalid_mode_raises():
    with pytest.raises(WriterValidationError, match="mode"):
        write_to_route_prod(**_minimal_kwargs(mode="replace"))


# ---------------------------------------------------------------------------
# Skip-if-None / COALESCE-preservation semantics
# ---------------------------------------------------------------------------

def test_skip_if_none_omits_column_from_upsert():
    conn = _FakeConn()
    write_to_route_prod(
        **_minimal_kwargs(
            route_data={
                "route_id": _ROUTE_ID,
                "province": "sample_region",
                "service_route_id": None,
                "direction_id": None,
                "route_name": "Route A",
            },
            conn=conn,
        )
    )
    sql = conn.cur.sql or ""
    assert "route_name" in sql
    assert "service_route_id" not in sql
    assert "direction_id" not in sql


def test_optional_column_present_when_set():
    conn = _FakeConn()
    write_to_route_prod(
        **_minimal_kwargs(
            route_data={
                "route_id": _ROUTE_ID,
                "province": "sample_region",
                "source": "route_constructor",
                "deploy_status": "active",
            },
            conn=conn,
        )
    )
    sql = conn.cur.sql or ""
    assert "source" in sql
    assert "deploy_status" in sql


# ---------------------------------------------------------------------------
# Shape forms
# ---------------------------------------------------------------------------

def test_shape_raw_uses_plain_placeholder():
    conn = _FakeConn()
    raw = object()  # a real psycopg wkb binding stand-in
    write_to_route_prod(**_minimal_kwargs(shape={"raw": raw}, conn=conn))
    sql = conn.cur.sql or ""
    assert "ST_GeomFromText" not in sql
    assert "ST_GeomFromGeoJSON" not in sql
    assert raw in conn.cur.params


def test_shape_wkt_uses_ST_GeomFromText():
    conn = _FakeConn()
    write_to_route_prod(**_minimal_kwargs(shape={"wkt": _GEOM_WKT}, conn=conn))
    sql = conn.cur.sql or ""
    assert "ST_GeomFromText(%s, 4326)" in sql
    assert _GEOM_WKT in conn.cur.params


def test_shape_geojson_str_uses_ST_GeomFromGeoJSON():
    conn = _FakeConn()
    gj_str = '{"type":"LineString","coordinates":[[-78.5,-0.2],[-78.4,-0.2]]}'
    write_to_route_prod(**_minimal_kwargs(shape={"geojson": gj_str}, conn=conn))
    sql = conn.cur.sql or ""
    assert "ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)" in sql
    assert gj_str in conn.cur.params


def test_shape_geojson_dict_is_serialized():
    conn = _FakeConn()
    gj = {
        "type": "LineString",
        "coordinates": [[-78.5, -0.2], [-78.4, -0.2]],
    }
    write_to_route_prod(**_minimal_kwargs(shape={"geojson": gj}, conn=conn))
    sql = conn.cur.sql or ""
    assert "ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)" in sql
    # dict should have been json.dumps-ed before binding
    assert any(
        isinstance(p, str) and p.startswith("{") and "LineString" in p
        for p in conn.cur.params
    )


def test_shape_missing_keys_raises():
    with pytest.raises(WriterValidationError, match="shape"):
        write_to_route_prod(**_minimal_kwargs(shape={"mystery": 1}))


# ---------------------------------------------------------------------------
# stops normalisation
# ---------------------------------------------------------------------------

def test_stops_list_of_strings_passes_through():
    conn = _FakeConn()
    write_to_route_prod(**_minimal_kwargs(stops=[_STOP_A], conn=conn))
    assert [_STOP_A] in conn.cur.params or _STOP_A in str(conn.cur.params)


def test_stops_list_of_dicts_sorts_by_order():
    conn = _FakeConn()
    b = "33333333-3333-3333-3333-333333333333"
    c = "44444444-4444-4444-4444-444444444444"
    write_to_route_prod(
        **_minimal_kwargs(
            stops=[
                {"node_id": b, "order": 2},
                {"node_id": _STOP_A, "order": 1},
                {"node_id": c, "order": 3},
            ],
            conn=conn,
        )
    )
    # The normalised stops param should be [A, B, C]
    flattened = [p for p in conn.cur.params if isinstance(p, list)]
    assert flattened, "no list parameter bound for stops"
    assert flattened[0] == [_STOP_A, b, c]


def test_stops_dict_without_node_id_key_raises():
    with pytest.raises(WriterValidationError, match="node_id"):
        write_to_route_prod(
            **_minimal_kwargs(
                stops=[{"wrong_key": _STOP_A}],
            )
        )


# ---------------------------------------------------------------------------
# direction_semantics JSON handling
# ---------------------------------------------------------------------------

def test_direction_semantics_dict_is_json_encoded():
    conn = _FakeConn()
    ds = {"cooperative": "Coop Y", "research_code": "x-1"}
    write_to_route_prod(
        **_minimal_kwargs(
            route_data={
                "route_id": _ROUTE_ID,
                "province": "sample_region",
                "direction_semantics": ds,
            },
            conn=conn,
        )
    )
    serialized = [
        p for p in conn.cur.params
        if isinstance(p, str) and p.startswith("{") and "cooperative" in p
    ]
    assert serialized, "direction_semantics dict was not json.dumps'd"


# ---------------------------------------------------------------------------
# MISSING_AUDIT_COLUMNS warning
# ---------------------------------------------------------------------------

def test_missing_audit_columns_emits_warning_but_does_not_block():
    result = write_to_route_prod(**_minimal_kwargs())
    assert result.success is True
    assert any("MISSING_AUDIT_COLUMNS" in w for w in result.warnings)


def test_legacy_grandfathered_suppresses_audit_warning():
    result = write_to_route_prod(
        **_minimal_kwargs(),
        legacy_grandfathered=True,
    )
    assert result.success is True
    assert not any("MISSING_AUDIT_COLUMNS" in w for w in result.warnings)


def test_all_audit_fields_present_suppresses_warning():
    conn = _FakeConn()
    kwargs = _minimal_kwargs(conn=conn)
    result = write_to_route_prod(
        **kwargs,
        valhalla_request={"costing": "bus"},
        geometry_enforcer_report={"pass": True},
        stop_coverage_report={"pass": True},
        quality_gate_passed_at=datetime.now(timezone.utc),
    )
    assert result.success is True
    assert not any("MISSING_AUDIT_COLUMNS" in w for w in result.warnings)
    # With full audit payload + USE_STORED_PROCEDURE=True, the wrapper
    # routes through route_prod_writer_sp(...). Pin that contract.
    sql = conn.cur.sql or ""
    assert "route_prod.route_prod_writer_sp(" in sql, (
        f"expected SP call when audit complete, got: {sql!r}"
    )


# ---------------------------------------------------------------------------
# patch_route_prod_fields
# ---------------------------------------------------------------------------

def test_patch_rejects_non_allowlisted_columns():
    with pytest.raises(WriterValidationError, match="columns not allowed"):
        patch_route_prod_fields(
            conn=_FakeConn(),
            route_id=_ROUTE_ID,
            fields={"geom": "LINESTRING(...)"},
            source_type="unit_test",
            pipeline_version="tests",
        )


def test_patch_builds_targeted_update_sql():
    conn = _FakeConn()
    patch_route_prod_fields(
        conn=conn,
        route_id=_ROUTE_ID,
        fields={"service_route_id": "abc", "direction_id": 0},
        source_type="unit_test",
        pipeline_version="tests",
    )
    sql = conn.cur.sql or ""
    assert sql.startswith("UPDATE route_prod.routes SET ")
    assert "service_route_id = %s" in sql
    assert "direction_id = %s" in sql
    assert "updated_at = now()" in sql
    assert sql.rstrip().endswith("WHERE route_id = %s::uuid")


def test_patch_noops_when_row_absent():
    conn = _FakeConn(rowcount=0)
    result = patch_route_prod_fields(
        conn=conn,
        route_id=_ROUTE_ID,
        fields={"service_route_id": "abc"},
        source_type="unit_test",
        pipeline_version="tests",
    )
    assert result.success is True
    assert result.rows_affected == {"route_prod.routes": 0}
    assert result.error is None


def test_patch_empty_fields_raises():
    with pytest.raises(WriterValidationError, match="fields"):
        patch_route_prod_fields(
            conn=_FakeConn(),
            route_id=_ROUTE_ID,
            fields={},
            source_type="unit_test",
            pipeline_version="tests",
        )


def test_patch_direction_semantics_dict_json_encoded():
    conn = _FakeConn()
    ds = {"cooperative": "Coop Y"}
    patch_route_prod_fields(
        conn=conn,
        route_id=_ROUTE_ID,
        fields={"direction_semantics": ds},
        source_type="unit_test",
        pipeline_version="tests",
    )
    serialized = [
        p for p in conn.cur.params
        if isinstance(p, str) and p.startswith("{") and "cooperative" in p
    ]
    assert serialized, "direction_semantics dict was not json.dumps'd"


# ---------------------------------------------------------------------------
# Gap 1 — on_conflict_preserve (selective DO UPDATE)
# ---------------------------------------------------------------------------

def _split_upsert(sql: str) -> tuple[str, str]:
    """Split INSERT ... ON CONFLICT ... DO UPDATE SET ... into (insert, update)."""
    marker = "ON CONFLICT (route_id, version) DO UPDATE SET"
    assert marker in sql, f"expected marker in sql, got: {sql!r}"
    head, tail = sql.split(marker, 1)
    return head, tail


def test_on_conflict_preserve_excludes_column_from_do_update():
    conn = _FakeConn()
    write_to_route_prod(
        **_minimal_kwargs(
            route_data={
                "route_id": _ROUTE_ID,
                "province": "sample_region",
                "source": "route_constructor",
                "deploy_status": "active",
            },
            conn=conn,
        ),
        on_conflict_preserve=frozenset({"source", "deploy_status"}),
    )
    insert_part, update_part = _split_upsert(conn.cur.sql or "")
    assert "source" in insert_part
    assert "deploy_status" in insert_part
    assert "source = EXCLUDED.source" not in update_part
    assert "deploy_status = EXCLUDED.deploy_status" not in update_part


def test_on_conflict_preserve_leaves_other_columns_updatable():
    conn = _FakeConn()
    write_to_route_prod(
        **_minimal_kwargs(
            route_data={
                "route_id": _ROUTE_ID,
                "province": "sample_region",
                "source": "route_constructor",
                "route_name": "Route A",
            },
            conn=conn,
        ),
        on_conflict_preserve=frozenset({"source"}),
    )
    _, update_part = _split_upsert(conn.cur.sql or "")
    assert "source = EXCLUDED.source" not in update_part
    assert "route_name = EXCLUDED.route_name" in update_part
    assert "geom = EXCLUDED.geom" in update_part
    assert "stop_node_ids = EXCLUDED.stop_node_ids" in update_part


def test_on_conflict_preserve_default_is_empty_backcompat():
    conn = _FakeConn()
    write_to_route_prod(
        **_minimal_kwargs(
            route_data={
                "route_id": _ROUTE_ID,
                "province": "sample_region",
                "source": "route_constructor",
                "deploy_status": "active",
            },
            conn=conn,
        ),
    )
    _, update_part = _split_upsert(conn.cur.sql or "")
    assert "source = EXCLUDED.source" in update_part
    assert "deploy_status = EXCLUDED.deploy_status" in update_part


# ---------------------------------------------------------------------------
# Gap 2 — delete_route_prod
# ---------------------------------------------------------------------------

def test_delete_route_prod_builds_targeted_delete():
    conn = _FakeConn()
    result = delete_route_prod(
        conn=conn,
        route_id=_ROUTE_ID,
        source_type="unit_test",
        pipeline_version="tests",
        reason="test cleanup",
    )
    sql = conn.cur.sql or ""
    assert sql == "DELETE FROM route_prod.routes WHERE route_id = %s::uuid"
    assert conn.cur.params == [_ROUTE_ID]
    assert result.success is True
    assert result.rows_affected == {"route_prod.routes": 1}


def test_delete_route_prod_noop_when_row_absent():
    conn = _FakeConn(rowcount=0)
    result = delete_route_prod(
        conn=conn,
        route_id=_ROUTE_ID,
        source_type="unit_test",
        pipeline_version="tests",
        reason="test cleanup",
    )
    assert result.success is True
    assert result.rows_affected == {"route_prod.routes": 0}
    assert result.error is None


def test_delete_route_prod_requires_reason():
    with pytest.raises(WriterValidationError, match="reason"):
        delete_route_prod(
            conn=_FakeConn(),
            route_id=_ROUTE_ID,
            source_type="unit_test",
            pipeline_version="tests",
            reason="",
        )


def test_delete_route_prod_requires_source_type():
    with pytest.raises(WriterValidationError, match="source_type"):
        delete_route_prod(
            conn=_FakeConn(),
            route_id=_ROUTE_ID,
            source_type="",
            pipeline_version="tests",
            reason="cleanup",
        )


def test_delete_route_prod_requires_pipeline_version():
    with pytest.raises(WriterValidationError, match="pipeline_version"):
        delete_route_prod(
            conn=_FakeConn(),
            route_id=_ROUTE_ID,
            source_type="unit_test",
            pipeline_version="",
            reason="cleanup",
        )


def test_delete_route_prod_requires_route_id():
    with pytest.raises(WriterValidationError, match="route_id"):
        delete_route_prod(
            conn=_FakeConn(),
            route_id="",
            source_type="unit_test",
            pipeline_version="tests",
            reason="cleanup",
        )


# ---------------------------------------------------------------------------
# Bulk operations — prune_stop_node_id_from_routes
# ---------------------------------------------------------------------------

_NODE_X = "55555555-5555-5555-5555-555555555555"
_ROUTE_B = "66666666-6666-6666-6666-666666666666"
_ROUTE_C = "77777777-7777-7777-7777-777777777777"


def test_prune_stop_requires_stop_node_id():
    with pytest.raises(WriterValidationError, match="stop_node_id"):
        prune_stop_node_id_from_routes(
            conn=_FakeConn(),
            stop_node_id="",
            reason="cascade cleanup",
            pipeline_version="tests",
            source_component="unit_test",
        )


def test_prune_stop_requires_reason():
    with pytest.raises(WriterValidationError, match="reason"):
        prune_stop_node_id_from_routes(
            conn=_FakeConn(),
            stop_node_id=_NODE_X,
            reason="",
            pipeline_version="tests",
            source_component="unit_test",
        )


def test_prune_stop_requires_pipeline_version():
    with pytest.raises(WriterValidationError, match="pipeline_version"):
        prune_stop_node_id_from_routes(
            conn=_FakeConn(),
            stop_node_id=_NODE_X,
            reason="cascade",
            pipeline_version="",
            source_component="unit_test",
        )


def test_prune_stop_requires_source_component():
    with pytest.raises(WriterValidationError, match="source_component"):
        prune_stop_node_id_from_routes(
            conn=_FakeConn(),
            stop_node_id=_NODE_X,
            reason="cascade",
            pipeline_version="tests",
            source_component="",
        )


def test_prune_stop_builds_array_remove_sql_with_returning():
    conn = _FakeConn(fetch_rows=[(_ROUTE_ID,), (_ROUTE_B,)])
    result = prune_stop_node_id_from_routes(
        conn=conn,
        stop_node_id=_NODE_X,
        reason="node deleted",
        pipeline_version="phase2_semantics.delete_node",
        source_component="phase2_semantics",
    )
    sql = conn.cur.sql or ""
    assert "UPDATE route_prod.routes" in sql
    assert "array_remove(" in sql
    assert "ANY(COALESCE(stop_node_ids" in sql
    assert "RETURNING route_id::text" in sql
    assert conn.cur.params == [_NODE_X, _NODE_X]
    assert result.success is True
    assert result.operation == "prune_stop_node_id_from_routes"
    assert result.rows_affected == {"route_prod.routes": 2}
    assert result.affected_route_codes == [_ROUTE_ID, _ROUTE_B]


def test_prune_stop_zero_affected_returns_empty_list():
    conn = _FakeConn(fetch_rows=[])
    result = prune_stop_node_id_from_routes(
        conn=conn,
        stop_node_id=_NODE_X,
        reason="no matches",
        pipeline_version="tests",
        source_component="unit_test",
    )
    assert result.success is True
    assert result.rows_affected == {"route_prod.routes": 0}
    assert result.affected_route_codes == []
    assert result.warnings == []


def test_prune_stop_accepts_dict_cursor_rows():
    conn = _FakeConn(fetch_rows=[{"route_id": _ROUTE_ID}])
    result = prune_stop_node_id_from_routes(
        conn=conn,
        stop_node_id=_NODE_X,
        reason="dict-cursor path",
        pipeline_version="tests",
        source_component="unit_test",
    )
    assert result.affected_route_codes == [_ROUTE_ID]


# ---------------------------------------------------------------------------
# Bulk operations — delete_routes_prod_bulk
# ---------------------------------------------------------------------------

def test_delete_bulk_empty_list_is_noop_with_warning():
    conn = _FakeConn()
    result = delete_routes_prod_bulk(
        conn=conn,
        route_ids=[],
        reason="nothing to do",
        pipeline_version="tests",
        source_component="unit_test",
    )
    assert result.success is True
    assert result.rows_affected == {"route_prod.routes": 0}
    assert len(result.warnings) == 1
    assert "empty" in result.warnings[0]
    # Cursor was never executed
    assert conn.cur.sql is None


def test_delete_bulk_builds_any_delete_sql_with_returning():
    conn = _FakeConn(fetch_rows=[(_ROUTE_ID,), (_ROUTE_B,), (_ROUTE_C,)])
    result = delete_routes_prod_bulk(
        conn=conn,
        route_ids=[_ROUTE_ID, _ROUTE_B, _ROUTE_C],
        reason="bulk cleanup",
        pipeline_version="phase2_semantics.bulk_delete",
        source_component="phase2_semantics",
    )
    sql = conn.cur.sql or ""
    assert "DELETE FROM route_prod.routes" in sql
    assert "WHERE route_id = ANY(%s::uuid[])" in sql
    assert "RETURNING route_id::text" in sql
    assert conn.cur.params == [[_ROUTE_ID, _ROUTE_B, _ROUTE_C]]
    assert result.success is True
    assert result.operation == "delete_routes_prod_bulk"
    assert result.rows_affected == {"route_prod.routes": 3}
    assert result.affected_route_codes == [_ROUTE_ID, _ROUTE_B, _ROUTE_C]
    assert result.warnings == []


def test_delete_bulk_reports_missing_ids_without_failing():
    # Requested 3, only 1 exists
    conn = _FakeConn(fetch_rows=[(_ROUTE_ID,)])
    result = delete_routes_prod_bulk(
        conn=conn,
        route_ids=[_ROUTE_ID, _ROUTE_B, _ROUTE_C],
        reason="bulk cleanup",
        pipeline_version="tests",
        source_component="unit_test",
    )
    assert result.success is True
    assert result.rows_affected == {"route_prod.routes": 1}
    assert result.affected_route_codes == [_ROUTE_ID]
    assert len(result.warnings) == 2
    assert any(_ROUTE_B in w for w in result.warnings)
    assert any(_ROUTE_C in w for w in result.warnings)


def test_delete_bulk_requires_reason():
    with pytest.raises(WriterValidationError, match="reason"):
        delete_routes_prod_bulk(
            conn=_FakeConn(),
            route_ids=[_ROUTE_ID],
            reason="",
            pipeline_version="tests",
            source_component="unit_test",
        )


def test_delete_bulk_requires_pipeline_version():
    with pytest.raises(WriterValidationError, match="pipeline_version"):
        delete_routes_prod_bulk(
            conn=_FakeConn(),
            route_ids=[_ROUTE_ID],
            reason="cleanup",
            pipeline_version="",
            source_component="unit_test",
        )


def test_delete_bulk_requires_source_component():
    with pytest.raises(WriterValidationError, match="source_component"):
        delete_routes_prod_bulk(
            conn=_FakeConn(),
            route_ids=[_ROUTE_ID],
            reason="cleanup",
            pipeline_version="tests",
            source_component="",
        )
