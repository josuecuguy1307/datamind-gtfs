"""Regression tests for tools/mcp_server.py.

Unit tests use a fake psycopg2 connection. The live read-only check of all
18 tools runs only with ``DATAMIND_LIVE_DB_TESTS=1``; it opens read-only
sessions and never writes.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SERVER_PATH = ROOT / "tools" / "mcp_server.py"


def _load_server():
    sys.path.insert(0, str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location("datamind_mcp_server_under_test", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


server = _load_server()


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn
        self.description = [("col",)]

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, *args: Any) -> None:
        self.conn.calls.append(args)

    def fetchall(self) -> list[dict]:
        return [{"col": 1}]

    def fetchmany(self, n: int) -> list[dict]:
        self.conn.fetchmany_n = n
        return [{"col": 1}]

    def fetchone(self) -> dict:
        return {"n": 1}


class FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.autocommit = True
        self.rolled_back = 0
        self.committed = 0
        self.closed = False
        self.fetchmany_n: int | None = None

    def cursor(self, *args: Any, **kwargs: Any) -> FakeCursor:
        return FakeCursor(self)

    def rollback(self) -> None:
        self.rolled_back += 1

    def commit(self) -> None:
        self.committed += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_connect(monkeypatch: pytest.MonkeyPatch):
    made: list[tuple[FakeConn, dict]] = []

    def _connect(dsn: str, **kwargs: Any) -> FakeConn:
        conn = FakeConn()
        made.append((conn, kwargs))
        return conn

    monkeypatch.setattr(server, "resolve_active_db_dsn", lambda: "postgresql://localhost/test")
    monkeypatch.setattr(server.psycopg2, "connect", _connect)
    return made


def test_sessions_are_read_only_bounded_and_closed(fake_connect) -> None:
    server.list_schemas()
    conn, kwargs = fake_connect[0]
    assert "default_transaction_read_only=on" in kwargs["options"]
    assert "statement_timeout=" in kwargs["options"]
    assert conn.calls[0] == ("SET TRANSACTION READ ONLY;",)
    assert conn.closed and conn.rolled_back >= 1 and conn.committed == 0


def test_list_schemas_executes_without_parameter_interpolation(fake_connect) -> None:
    out = json.loads(server.list_schemas())
    conn, _ = fake_connect[0]
    sql_call = conn.calls[1]
    assert len(sql_call) == 1, "SQL containing '%' must be executed without a params tuple"
    assert "pg_temp%" in sql_call[0]
    assert out == [{"col": 1}]


def test_query_db_rejects_unsafe_sql_before_connecting(fake_connect) -> None:
    out = json.loads(server.query_db("SELECT 1; COMMIT; DELETE FROM console.users"))
    assert "error" in out
    assert fake_connect == [], "unsafe SQL must not reach the database"


def test_query_db_caps_rows_and_appends_limit(fake_connect) -> None:
    server.query_db("SELECT * FROM route_prod.routes")
    conn, _ = fake_connect[0]
    executed_sql = conn.calls[1][0]
    assert executed_sql.rstrip().endswith("LIMIT 500")
    assert conn.fetchmany_n == 500


def test_query_db_accepts_percent_literals(fake_connect) -> None:
    out = json.loads(server.query_db("SELECT 'a%' AS pct"))
    assert out == [{"col": 1}]


def test_ai_models_shape_and_columns(fake_connect) -> None:
    out = json.loads(server.ai_models())
    assert set(out) == {"ai_model_registry", "geo_work_model_registry", "note"}
    all_sql = " ".join(call[0] for conn, _ in fake_connect for call in conn.calls)
    assert "model_type" not in all_sql
    assert "model_name" in all_sql


def test_fixed_queries_reference_existing_columns() -> None:
    source = _SERVER_PATH.read_text(encoding="utf-8")
    assert "ORDER BY route_name ASC" in source  # alias defined via COALESCE(prod_route_name, ...)
    assert "WHERE job_id::text" not in source
    assert "SELECT run_id, action," not in source
    assert "SELECT set_id, area_id" not in source


@pytest.mark.skipif(os.getenv("DATAMIND_LIVE_DB_TESTS") != "1", reason="live read-only DB check is opt-in")
def test_all_tools_return_without_errors_against_live_db() -> None:
    live = _load_server()
    calls = {
        "dashboard_kpis": {},
        "health_snapshot": {},
        "phase_summary": {},
        "route_catalog": {"limit": 2},
        "routes_by_length": {"limit": 2},
        "routes_by_stops": {"limit": 2},
        "node_inventory": {"limit": 2},
        "place_inventory": {"limit": 2},
        "extraction_runs": {"limit": 2},
        "candidate_sets": {"limit": 2},
        "recent_decisions": {"limit": 2},
        "query_db": {"sql": "SELECT 'ok%' AS pct"},
        "list_schemas": {},
        "list_tables": {"schema": "console"},
        "describe_table": {"schema": "console", "table": "users"},
        "ai_models": {},
        "naming_confidence_distribution": {},
    }
    for name, kwargs in calls.items():
        out = getattr(live, name)(**kwargs)
        assert '"error"' not in out[:300], f"{name} failed: {out[:300]}"
    assert "route not found" in live.route_detail("00000000-0000-0000-0000-000000000000")
