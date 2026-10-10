"""Unit tests for tools/mcp_sql_guard.py (no database)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_GUARD_PATH = Path(__file__).resolve().parents[1] / "mcp_sql_guard.py"
_spec = importlib.util.spec_from_file_location("mcp_sql_guard_under_test", _GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(guard)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select count(*) from route_prod.routes;",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT ';' AS semicolon_in_string",
        "SELECT $$ DROP TABLE x; $$ AS dollar_body",
        "SELECT 'pg_sleep(5)' AS text_not_call",
        "EXPLAIN SELECT * FROM node_prod.nodes",
        "SHOW search_path",
        "VALUES (1), (2)",
        "TABLE console.users",
        "SELECT 1 -- trailing ; comment",
        "/* leading comment */ SELECT 2",
    ],
)
def test_plain_reads_are_accepted(sql: str) -> None:
    clean, _ = guard.validate_read_query(sql)
    assert not clean.endswith(";")


@pytest.mark.parametrize(
    "sql,fragment",
    [
        ("SELECT 1; COMMIT; DELETE FROM console.users", "one SQL statement"),
        ("SELECT 1; SELECT 2", "one SQL statement"),
        ("COPY (SELECT 1) TO PROGRAM 'touch /tmp/x'", "COPY"),
        ("DELETE FROM console.users", "DELETE"),
        ("UPDATE route_prod.routes SET route_name = 'x'", "UPDATE"),
        ("CREATE TABLE x (id int)", "CREATE"),
        ("SET default_transaction_read_only = off", "SET"),
        ("DO $$ BEGIN DELETE FROM x; END $$", "DO"),
        ("CALL some_procedure()", "CALL"),
        ("SELECT dblink_exec('dbname=x', 'DELETE FROM t')", "dblink_exec"),
        ('SELECT "dblink_exec"(\'dbname=x\', \'DELETE\')', "dblink_exec"),
        ("SELECT pg_catalog.pg_sleep(10)", "pg_sleep"),
        ("SELECT pg_terminate_backend(123)", "pg_terminate_backend"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT lo_export(1, '/tmp/x')", "lo_export"),
        ("SELECT set_config('default_transaction_read_only', 'off', false)", "set_config"),
        ("WITH a AS (SELECT pg_reload_conf()) SELECT * FROM a", "pg_reload_conf"),
        ("", "Empty"),
        ("   ;  ", "Empty"),
    ],
)
def test_unsafe_shapes_are_rejected(sql: str, fragment: str) -> None:
    with pytest.raises(guard.UnsafeSQLError) as excinfo:
        guard.validate_read_query(sql)
    assert fragment.lower() in str(excinfo.value).lower()


def test_limit_is_requested_only_for_row_returning_queries_without_limit() -> None:
    assert guard.validate_read_query("SELECT * FROM t")[1] is True
    assert guard.validate_read_query("SELECT * FROM t LIMIT 5")[1] is False
    assert guard.validate_read_query("SELECT * FROM t FETCH FIRST 3 ROWS ONLY")[1] is False
    assert guard.validate_read_query("SHOW search_path")[1] is False
    assert guard.validate_read_query("EXPLAIN SELECT 1")[1] is False
    # A LIMIT inside a string literal does not count.
    assert guard.validate_read_query("SELECT 'limit 5' AS s")[1] is True


def test_scrub_neutralises_literals_and_comments_but_keeps_identifiers() -> None:
    scrubbed = guard.scrub("SELECT 'x;y', $tag$a;b$tag$, \"MixedCase\" -- c;\n/* d; */ FROM t")
    assert ";" not in scrubbed
    assert "mixedcase" in scrubbed
