"""Regression tests: runtime code verifies schema objects and never migrates.

Background: until 2026-10-08 opening the Phase 3 page executed
``012_route_direction_gate.sql`` and ``023_sequence_resolution.sql``
(``UPDATE route_prod.routes``, an unfiltered ``UPDATE
route_raw.service_routes``, an upsert that re-approved invalidated sequence
approvals). Phase 5 re-ran every V5 migration on first use in each process.

All tests use fake connections; none touches a real database.
"""
from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from datamind_console.db import schema_guard
from datamind_console.db.schema_guard import (
    SchemaNotReadyError,
    SchemaUnit,
    parse_requirements,
    summarize_statements,
)

WRITE_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|GRANT|COMMENT)\b", re.IGNORECASE)


class RecordingCursor:
    """Cursor that records statements and answers catalog probes."""

    def __init__(self, conn: "RecordingConnection") -> None:
        self.conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append(sql)
        text = " ".join(sql.split())
        if "to_regclass(r) IS NULL" in text:
            wanted = list(params[0])
            self._rows = [(r,) for r in wanted if r in self.conn.missing_relations]
        elif "pg_attribute" in text:
            tables, cols = params
            self._rows = [(t, c) for t, c in zip(tables, cols) if f"{t}.{c}" in self.conn.missing_columns]
        elif "pg_proc" in text:
            self._rows = [(f,) for f in params[0] if f in self.conn.missing_functions]
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class RecordingConnection:
    def __init__(self, *, missing_relations=(), missing_columns=(), missing_functions=()) -> None:
        self.executed: list[str] = []
        self.missing_relations = set(missing_relations)
        self.missing_columns = set(missing_columns)
        self.missing_functions = set(missing_functions)
        self.opened = 0

    def cursor(self, *args: Any, **kwargs: Any) -> RecordingCursor:
        return RecordingCursor(self)


def factory_for(conn: RecordingConnection):
    @contextlib.contextmanager
    def _factory():
        conn.opened += 1
        yield conn

    return _factory


def assert_read_only(conn: RecordingConnection) -> None:
    for sql in conn.executed:
        stripped = sql.strip().upper()
        if stripped.startswith("SET TRANSACTION READ ONLY"):
            continue
        assert stripped.startswith("SELECT"), f"non-read statement executed: {sql[:120]}"
        assert not WRITE_SQL.search(sql.replace("NOT EXISTS", "")), f"write keyword in: {sql[:120]}"


@pytest.fixture(autouse=True)
def _reset_guard_cache():
    schema_guard.reset_cache()
    yield
    schema_guard.reset_cache()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_tracks_creates_drops_and_columns_in_order() -> None:
    sql = """
    -- comment mentioning CREATE TABLE fake.ignored (x int)
    CREATE TABLE IF NOT EXISTS s.keep (id int);
    CREATE TABLE s.temp_table (id int);
    DROP TABLE IF EXISTS s.temp_table;
    CREATE OR REPLACE VIEW s.v1 AS SELECT 1;
    DROP VIEW IF EXISTS s.v1;
    CREATE VIEW s.v1 AS SELECT 2;
    ALTER TABLE s.keep
      ADD COLUMN IF NOT EXISTS a TEXT,
      ADD COLUMN b INT;
    ALTER TABLE s.keep DROP COLUMN IF EXISTS b;
    CREATE OR REPLACE FUNCTION s.touch() RETURNS trigger AS $$ BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql;
    """
    req = parse_requirements([sql])
    assert req.relations == ("s.keep", "s.v1")
    assert req.columns == (("s.keep", "a"),)
    assert req.functions == ("s.touch",)


def test_statement_profile_counts_data_changes() -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS a.b (id int);
    UPDATE route_raw.service_routes sr SET x = 1;
    INSERT INTO a.b VALUES (1);
    DELETE FROM a.b;
    ALTER TABLE a.b ADD COLUMN IF NOT EXISTS c int;
    DROP VIEW IF EXISTS a.v;
    """
    profile = summarize_statements(sql)
    assert profile["data_changes"] == 3
    assert profile["drops"] == 1
    assert profile["alters"] == 1


# ---------------------------------------------------------------------------
# require(): read-only, cached, explicit errors
# ---------------------------------------------------------------------------


def _unit(tmp_path: Path, sql: str, key: str = "test.unit") -> SchemaUnit:
    path = tmp_path / f"{key}.sql"
    path.write_text(sql, encoding="utf-8")
    return SchemaUnit(key=key, title=f"Test {key}", sql_files=(path,))


def test_require_only_reads_the_catalog(tmp_path: Path) -> None:
    unit = _unit(
        tmp_path,
        "CREATE TABLE IF NOT EXISTS s.t (id int); ALTER TABLE s.t ADD COLUMN IF NOT EXISTS c int;"
        " UPDATE s.t SET c = 1;",
    )
    conn = RecordingConnection()
    schema_guard.require(unit, factory_for(conn))
    assert conn.executed, "the catalog must be probed"
    assert_read_only(conn)
    assert conn.executed[0].strip().upper().startswith("SET TRANSACTION READ ONLY")


def test_require_is_cached_without_opening_connections(tmp_path: Path) -> None:
    unit = _unit(tmp_path, "CREATE TABLE IF NOT EXISTS s.t (id int);")
    conn = RecordingConnection()
    schema_guard.require(unit, factory_for(conn))
    opened = conn.opened
    for _ in range(5):
        schema_guard.require(unit, factory_for(conn))
    assert conn.opened == opened


def test_missing_objects_raise_with_operator_instructions(tmp_path: Path) -> None:
    unit = _unit(tmp_path, "CREATE TABLE IF NOT EXISTS s.t (id int);", key="phase9.example")
    conn = RecordingConnection(missing_relations={"s.t"})
    with pytest.raises(SchemaNotReadyError) as excinfo:
        schema_guard.require(unit, factory_for(conn))
    err = excinfo.value
    assert err.missing == ["s.t"]
    assert "schema_cli apply phase9.example --yes" in str(err)
    assert "Nothing was changed" in str(err)
    assert_read_only(conn)


def test_dependencies_are_verified_first(tmp_path: Path) -> None:
    dep = _unit(tmp_path, "CREATE TABLE IF NOT EXISTS s.dep (id int);", key="dep")
    main_path = tmp_path / "main.sql"
    main_path.write_text("CREATE TABLE IF NOT EXISTS s.main (id int);", encoding="utf-8")
    unit = SchemaUnit(key="main", title="Main", sql_files=(main_path,), depends_on=(dep,))
    conn = RecordingConnection(missing_relations={"s.dep"})
    with pytest.raises(SchemaNotReadyError) as excinfo:
        schema_guard.require(unit, factory_for(conn))
    assert excinfo.value.unit_key == "dep"


def test_require_on_connection_does_not_force_read_only(tmp_path: Path) -> None:
    unit = _unit(tmp_path, "CREATE TABLE IF NOT EXISTS s.t (id int);")
    conn = RecordingConnection()
    schema_guard.require_on_connection(unit, conn)
    assert not any("READ ONLY" in sql.upper() for sql in conn.executed)
    assert_read_only(conn)


def test_apply_is_the_only_function_that_executes_sql_files(tmp_path: Path) -> None:
    unit = _unit(tmp_path, "CREATE TABLE IF NOT EXISTS s.t (id int);")
    conn = RecordingConnection()
    schema_guard.require(unit, factory_for(conn))
    assert not any("CREATE TABLE" in sql for sql in conn.executed)
    schema_guard.apply(unit, factory_for(conn))
    assert any("CREATE TABLE" in sql for sql in conn.executed)


# ---------------------------------------------------------------------------
# Phase 3 client
# ---------------------------------------------------------------------------


def test_phase3_client_construction_does_not_touch_the_database() -> None:
    from datamind_console.phases.phase3_routes import client as client_module

    def _forbidden_conn(*args: Any, **kwargs: Any):
        raise AssertionError("Phase3Client() must not open a database connection")

    with patch.object(client_module, "db_conn", _forbidden_conn):
        client_module.Phase3Client()


@pytest.mark.parametrize(
    "method",
    [
        "_ensure_direction_schema",
        "_ensure_manual_sequence_schema",
        "_ensure_trash_schema",
        "_ensure_sequence_resolution_schema",
        "_ensure_extractor_review_schema",
        "_ensure_route_review_schema",
        "_ensure_inverse_completion_schema",
        "_ensure_geometry_stop_recovery_schema",
    ],
)
def test_phase3_schema_checks_never_execute_migration_sql(method: str) -> None:
    from datamind_console.phases.phase3_routes import client as client_module

    conn = RecordingConnection()
    with patch.object(client_module, "db_conn", factory_for(conn)):
        client = client_module.Phase3Client()
        getattr(client, method)()
    assert conn.executed, f"{method} should verify the catalog"
    assert_read_only(conn)


def test_phase3_missing_schema_surfaces_instead_of_migrating() -> None:
    from datamind_console.phases.phase3_routes import client as client_module

    conn = RecordingConnection(missing_relations={"route_raw.service_routes"})
    with patch.object(client_module, "db_conn", factory_for(conn)):
        client = client_module.Phase3Client()
        with pytest.raises(SchemaNotReadyError) as excinfo:
            client._ensure_direction_schema()
    assert excinfo.value.unit_key == "phase3.direction_gate"
    assert_read_only(conn)


def test_phase3_client_no_longer_reads_migration_files() -> None:
    source = Path("datamind_console/phases/phase3_routes/client.py").read_text(encoding="utf-8")
    for name in (
        "012_route_direction_gate.sql",
        "023_sequence_resolution.sql",
        "030_route_trash.sql",
        "022_route_review_coverage.sql",
        "024_inverse_direction_status.sql",
    ):
        assert name not in source, f"{name} is referenced from runtime client code"


def test_phase3_units_cover_every_former_lazy_sql_file() -> None:
    from datamind_console.phases.phase3_routes import schema as phase3_schema

    covered = {p.name for unit in phase3_schema.UNITS for p in unit.sql_files}
    assert {
        "012_route_direction_gate.sql",
        "013_route_extract_review.sql",
        "021_manual_sequence_builder.sql",
        "022_route_review_coverage.sql",
        "023_sequence_resolution.sql",
        "024_inverse_direction_status.sql",
        "025_inverse_direction_proposals.sql",
        "026_inverse_direction_search.sql",
        "027_geometry_stop_recovery.sql",
        "030_route_trash.sql",
    } <= covered
    for unit in phase3_schema.UNITS:
        assert not unit.missing_files(), unit.key
        assert not unit.requirements.is_empty(), unit.key


# ---------------------------------------------------------------------------
# Phase 5 client and migration ordering
# ---------------------------------------------------------------------------


def test_phase5_migrations_sort_numerically() -> None:
    from phase5_gtfs.schedules.store import list_migrations

    names = [p.name for p in list_migrations(Path("phase5_gtfs/db/migrations"))]
    versions = [int(n.split("__", 1)[0].split("_")[1]) for n in names]
    assert versions == sorted(versions)
    assert names.index("V5_2__route_semantics_model_alias_score.sql") < names.index("V5_10__gtfs_builds.sql")


def test_phase5_ensure_schema_verifies_instead_of_running_migrations() -> None:
    from datamind_console.phases.phase5_gtfs import client as p5_client_module

    conn = RecordingConnection()
    client = p5_client_module.Phase5Client.__new__(p5_client_module.Phase5Client)
    client._schema_ready = False
    with patch.object(p5_client_module.Phase5Client, "_conn", lambda self: factory_for(conn)()):
        client._ensure_schema()
    assert client._schema_ready is True
    assert_read_only(conn)
    assert "run_all_migrations" not in Path("datamind_console/phases/phase5_gtfs/client.py").read_text()


def test_ai_repo_verifies_instead_of_bootstrapping() -> None:
    from datamind_console.db import ai_repo

    conn = RecordingConnection()
    ai_repo._AI_SCHEMA_READY = False
    try:
        with patch.object(ai_repo, "db_conn", factory_for(conn)):
            ai_repo._ensure_ai_schema()
        assert ai_repo._AI_SCHEMA_READY is True
        assert_read_only(conn)
    finally:
        ai_repo._AI_SCHEMA_READY = False
