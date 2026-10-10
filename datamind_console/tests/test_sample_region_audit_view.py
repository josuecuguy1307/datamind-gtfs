"""Sample Region audit data access: local-only safety and transaction lifetime.

The view used to own its DSN helper and a cached psycopg2 connection that
stayed "idle in transaction". It now goes through
``datamind_console.db.db.query_all``, so the local-only guarantee is asserted
on the canonical helper, plus the connection-lifetime regression.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from datamind_console.db import db as canonical_db
from datamind_console.views import sample_region_audit_view as audit_view


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append((sql, params))
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("boom")

    def fetchall(self) -> list[dict]:
        return [{"cleanliness_status": "clean", "dirty_reason": None, "n": 3}]

    def fetchone(self) -> dict:
        return {"n": 1}


class _Conn:
    def __init__(self, fail_on: str | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.autocommit = False
        self.committed = 0
        self.rolled_back = 0
        self.closed = False
        self.fail_on = fail_on

    def cursor(self, *args: Any, **kwargs: Any) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed = True


class CanonicalDsnTests(unittest.TestCase):
    def test_local_only_mode_never_uses_server_dsn(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DATAMIND_LOCAL_ONLY_MODE": "true",
                "DB_DSN": "postgresql://server.example/should-not-be-used",
                "LOCAL_DB_DSN": "",
                "DATAMIND_LOCAL_DB_DSN": "",
                "DB_DSN_LOCAL": "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "Remote database fallbacks are disabled"):
                canonical_db._get_db_config()

    def test_local_only_mode_uses_explicit_local_dsn(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DATAMIND_LOCAL_ONLY_MODE": "true",
                "DB_DSN": "postgresql://server.example/should-not-be-used",
                "LOCAL_DB_DSN": "postgresql://localhost/local_audit",
            },
            clear=False,
        ):
            self.assertEqual(canonical_db._get_db_config().dsn, "postgresql://localhost/local_audit")


class AuditTransactionLifetimeTests(unittest.TestCase):
    def _patch_connect(self, conns: list[_Conn]):
        def _connect(*args: Any, **kwargs: Any) -> _Conn:
            conn = _Conn()
            conns.append(conn)
            return conn

        return patch.object(canonical_db.psycopg2, "connect", _connect)

    def test_every_audit_query_ends_its_transaction_and_closes(self) -> None:
        conns: list[_Conn] = []
        with patch.dict(os.environ, {"DATAMIND_LOCAL_ONLY_MODE": "true", "LOCAL_DB_DSN": "postgresql://localhost/x"}):
            with self._patch_connect(conns):
                rows = audit_view._fetch("SELECT 1 AS n")
                audit_view._fetch("SELECT 2 AS n")
        self.assertEqual(rows[0]["cleanliness_status"], "clean")
        self.assertEqual(len(conns), 2, "one short-lived connection per query")
        for conn in conns:
            self.assertTrue(conn.closed, "connection must be closed after the query")
            self.assertEqual(conn.committed, 1, "read-only transaction must be ended")
            self.assertTrue(conn.executed[0][0].startswith("SET TRANSACTION READ ONLY"))
            self.assertIn("statement_timeout", conn.executed[1][0])

    def test_failed_query_rolls_back_and_closes(self) -> None:
        conns: list[_Conn] = []

        def _connect(*args: Any, **kwargs: Any) -> _Conn:
            conn = _Conn(fail_on="SELECT broken")
            conns.append(conn)
            return conn

        with patch.dict(os.environ, {"DATAMIND_LOCAL_ONLY_MODE": "true", "LOCAL_DB_DSN": "postgresql://localhost/x"}):
            with patch.object(canonical_db.psycopg2, "connect", _connect):
                with self.assertRaises(RuntimeError):
                    audit_view._fetch("SELECT broken")
        self.assertTrue(conns[0].closed)
        self.assertEqual(conns[0].rolled_back, 1)
        self.assertEqual(conns[0].committed, 0)

    def test_view_no_longer_caches_a_connection(self) -> None:
        source = Path(audit_view.__file__).read_text(encoding="utf-8")
        self.assertNotIn("psycopg2.connect(", source)
        self.assertNotIn("def _conn(", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


def test_distinct_stops_are_not_references() -> None:
    import pandas as pd

    from datamind_console.views.sample_region_audit_view import _distinct_stop_count

    df = pd.DataFrame({"stop_ids": [["a", "b", "c"], ["c", "b", "d"], None]})
    assert _distinct_stop_count(df) == 4  # 6 references, 4 distinct stops
    assert _distinct_stop_count(pd.DataFrame({"n_stops": [3]})) == 0
