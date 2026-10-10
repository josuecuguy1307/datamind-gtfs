"""Canonical DB access patterns (datamind_console/db/db.py), using fake connections."""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

from datamind_console.db import db as canonical_db


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append((sql, params))
        if "boom" in sql:
            raise RuntimeError("query failed")

    def fetchall(self) -> list[dict]:
        return [{"value": 7}]

    def fetchone(self) -> dict:
        return {"value": 7}

    @property
    def rowcount(self) -> int:
        return 1


class _Conn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.autocommit = True
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def cursor(self, *args: Any, **kwargs: Any) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def conns(monkeypatch: pytest.MonkeyPatch) -> list[_Conn]:
    made: list[_Conn] = []

    def _connect(*args: Any, **kwargs: Any) -> _Conn:
        conn = _Conn()
        made.append(conn)
        return conn

    monkeypatch.setenv("DATAMIND_LOCAL_ONLY_MODE", "true")
    monkeypatch.setenv("LOCAL_DB_DSN", "postgresql://localhost/test")
    monkeypatch.setattr(canonical_db.psycopg2, "connect", _connect)
    return made


def test_query_all_is_read_only_and_closes(conns: list[_Conn]) -> None:
    rows = canonical_db.query_all("SELECT 7 AS value")
    assert rows == [{"value": 7}]
    conn = conns[0]
    assert conn.executed[0][0].startswith("SET TRANSACTION READ ONLY")
    assert conn.autocommit is False
    assert conn.committed == 1 and conn.closed


def test_query_scalar_and_timeout(conns: list[_Conn]) -> None:
    assert canonical_db.query_scalar("SELECT 7 AS value", statement_timeout_ms=1500) == 7
    timeout_sql, timeout_params = conns[0].executed[1]
    assert "statement_timeout" in timeout_sql and timeout_params == (1500,)


def test_failed_read_rolls_back_and_closes(conns: list[_Conn]) -> None:
    with pytest.raises(RuntimeError):
        canonical_db.query_one("SELECT boom")
    assert conns[0].rolled_back == 1 and conns[0].committed == 0 and conns[0].closed


def test_transaction_commits_writes_and_closes(conns: list[_Conn]) -> None:
    with canonical_db.transaction() as conn:
        canonical_db.exec_sql(conn, "UPDATE x SET y = 1")
    assert not any("READ ONLY" in sql for sql, _ in conns[0].executed)
    assert conns[0].committed == 1 and conns[0].closed


def test_active_dsn_never_falls_back_to_server_in_local_only_mode() -> None:
    env = {
        "DATAMIND_LOCAL_ONLY_MODE": "true",
        "LOCAL_DB_DSN": "",
        "DATAMIND_LOCAL_DB_DSN": "",
        "DB_DSN_LOCAL": "",
        "DB_DSN": "postgresql://server.example/nope",
    }
    with patch.dict(os.environ, env, clear=False):
        with pytest.raises(RuntimeError, match="Remote database fallbacks are disabled"):
            canonical_db.active_dsn()


def test_re_entry_review_reads_close_their_connections(conns: list[_Conn]) -> None:
    from datamind_console.views.phases.phase3.tabs import re_entry_review_tab as tab

    with tab._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    assert conns[0].closed, "re-entry reads must close their connection"
    assert conns[0].executed[0][0].startswith("SET TRANSACTION READ ONLY")


def test_current_feed_flags_missing_zip(tmp_path) -> None:
    from datamind_console.metrics import pipeline

    present = tmp_path / "feed.zip"
    present.write_bytes(b"PK")
    assert pipeline.current_feed(query=lambda sql, p: {"gtfs_zip_path": str(present)})["zip_exists"] is True
    assert pipeline.current_feed(query=lambda sql, p: {"gtfs_zip_path": str(tmp_path / "gone.zip")})["zip_exists"] is False
    assert pipeline.current_feed(query=lambda sql, p: None)["zip_exists"] is None
