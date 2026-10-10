"""Read-only schema verification, kept apart from explicit schema application.

Rule: rendering a page, or constructing a service client, must never run
migrations, DDL or data backfills.

Runtime code calls :func:`require`. It only reads the system catalog and
raises :class:`SchemaNotReadyError` with operator instructions when an object
is missing.

Applying the SQL files of a :class:`SchemaUnit` is an explicit administrative
action, exposed only through ``python -m datamind_console.db.schema_cli``.

Why this module exists: until 2026-10-08, several clients applied whole SQL
files lazily from read paths. Opening Phase 3, for example, re-ran
``012_route_direction_gate.sql`` and ``023_sequence_resolution.sql``, which
contain ``UPDATE route_prod.routes``, an unfiltered ``UPDATE
route_raw.service_routes`` and an upsert that re-approved invalidated sequence
approvals. See ``docs/DB_ACCESS.md``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterable, Optional

import psycopg2.extensions

LOGGER = logging.getLogger(__name__)

SCHEMA_CLI = "python -m datamind_console.db.schema_cli"

ConnectionFactory = Callable[[], ContextManager[Any]]


# ---------------------------------------------------------------------------
# Requirements parsed from SQL files
# ---------------------------------------------------------------------------

_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_QUALIFIED = rf"(?P<schema>{_IDENT})\s*\.\s*(?P<name>{_IDENT})"

_STATEMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "create_relation",
        re.compile(
            rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:UNLOGGED\s+)?(?:MATERIALIZED\s+)?"
            rf"(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?{_QUALIFIED}",
            re.IGNORECASE,
        ),
    ),
    (
        "drop_relation",
        re.compile(
            rf"\bDROP\s+(?:MATERIALIZED\s+)?(?:TABLE|VIEW)\s+(?:IF\s+EXISTS\s+)?{_QUALIFIED}",
            re.IGNORECASE,
        ),
    ),
    (
        "create_function",
        re.compile(
            rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+{_QUALIFIED}\s*\(",
            re.IGNORECASE,
        ),
    ),
    (
        "alter_table",
        re.compile(
            rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_QUALIFIED}(?P<body>[^;]*)",
            re.IGNORECASE,
        ),
    ),
)

_ADD_COLUMN = re.compile(rf"\bADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<col>{_IDENT})", re.IGNORECASE)
_DROP_COLUMN = re.compile(rf"\bDROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?(?P<col>{_IDENT})", re.IGNORECASE)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_DML_START = re.compile(r"(?im)^\s*(?:INSERT\s+INTO|UPDATE\s+\S|DELETE\s+FROM|TRUNCATE\b|MERGE\s+INTO)")
_DROP_START = re.compile(r"(?im)^\s*DROP\s+(TABLE|VIEW|MATERIALIZED|TRIGGER|FUNCTION|INDEX|SCHEMA)\b")


def _norm_ident(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    return raw.lower()


def _strip_comments(sql: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", sql))


@dataclass(frozen=True)
class SchemaRequirements:
    """Catalog objects that must exist once a unit has been applied."""

    relations: tuple[str, ...] = ()
    columns: tuple[tuple[str, str], ...] = ()
    functions: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (self.relations or self.columns or self.functions)


def parse_requirements(sql_texts: Iterable[str]) -> SchemaRequirements:
    """Return the objects a sequence of SQL scripts leaves behind.

    Statements are processed in file order, so a view that is dropped and
    recreated stays required, while a table dropped at the end does not.
    The parser understands only the subset of SQL used by DataMind's
    migration files (CREATE/DROP TABLE|VIEW, CREATE FUNCTION and
    ALTER TABLE ... ADD/DROP COLUMN). Tests compare its output with the
    real catalog.
    """
    relations: dict[str, None] = {}
    columns: dict[tuple[str, str], None] = {}
    functions: dict[str, None] = {}

    for text in sql_texts:
        clean = _strip_comments(text)
        events: list[tuple[int, str, re.Match[str]]] = []
        for kind, pattern in _STATEMENT_PATTERNS:
            events.extend((m.start(), kind, m) for m in pattern.finditer(clean))
        events.sort(key=lambda e: e[0])
        for _, kind, match in events:
            qualified = f"{_norm_ident(match.group('schema'))}.{_norm_ident(match.group('name'))}"
            if kind == "create_relation":
                relations[qualified] = None
            elif kind == "drop_relation":
                relations.pop(qualified, None)
                for key in [k for k in columns if k[0] == qualified]:
                    columns.pop(key, None)
            elif kind == "create_function":
                functions[qualified] = None
            elif kind == "alter_table":
                body = match.group("body") or ""
                for col in _ADD_COLUMN.finditer(body):
                    columns[(qualified, _norm_ident(col.group("col")))] = None
                for col in _DROP_COLUMN.finditer(body):
                    columns.pop((qualified, _norm_ident(col.group("col"))), None)

    return SchemaRequirements(
        relations=tuple(relations),
        columns=tuple(columns),
        functions=tuple(functions),
    )


def summarize_statements(sql_text: str) -> dict[str, int]:
    """Coarse statement profile, used by ``plan`` to warn before ``apply``."""
    clean = _strip_comments(sql_text)
    return {
        "data_changes": len(_DML_START.findall(clean)),
        "drops": len(_DROP_START.findall(clean)),
        "alters": len(re.findall(r"(?im)^\s*ALTER\s+TABLE\b", clean)),
        "creates": len(re.findall(r"(?im)^\s*CREATE\b", clean)),
    }


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaUnit:
    """A named group of SQL files that one runtime capability depends on."""

    key: str
    title: str
    sql_files: tuple[Path, ...]
    depends_on: tuple["SchemaUnit", ...] = ()
    pre_statements: tuple[str, ...] = ()
    single_transaction: bool = False
    extra_relations: tuple[str, ...] = ()
    notes: str = ""

    @cached_property
    def requirements(self) -> SchemaRequirements:
        texts = [p.read_text(encoding="utf-8") for p in self.sql_files if p.exists()]
        parsed = parse_requirements(texts)
        if not self.extra_relations:
            return parsed
        merged = dict.fromkeys(parsed.relations)
        merged.update(dict.fromkeys(self.extra_relations))
        return SchemaRequirements(
            relations=tuple(merged),
            columns=parsed.columns,
            functions=parsed.functions,
        )

    def missing_files(self) -> list[Path]:
        return [p for p in self.sql_files if not p.exists()]

    @property
    def plan_command(self) -> str:
        return f"{SCHEMA_CLI} plan {self.key}"

    @property
    def apply_command(self) -> str:
        return f"{SCHEMA_CLI} apply {self.key} --yes"


class SchemaNotReadyError(RuntimeError):
    """A runtime capability needs catalog objects that do not exist yet."""

    def __init__(self, unit: SchemaUnit, missing: list[str]) -> None:
        self.unit_key = unit.key
        self.unit_title = unit.title
        self.missing = list(missing)
        self.plan_command = unit.plan_command
        self.apply_command = unit.apply_command
        shown = ", ".join(self.missing[:8]) + (" …" if len(self.missing) > 8 else "")
        super().__init__(
            f"Database schema '{unit.title}' is not ready (missing: {shown}). "
            f"Nothing was changed. An administrator can review it with "
            f"`{self.plan_command}` and apply it explicitly with `{self.apply_command}`."
        )


@dataclass
class SchemaCheck:
    unit: SchemaUnit
    missing_relations: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    missing_functions: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing_relations or self.missing_columns or self.missing_functions)

    @property
    def missing(self) -> list[str]:
        return [*self.missing_relations, *self.missing_columns, *self.missing_functions]


# ---------------------------------------------------------------------------
# Verification (read-only)
# ---------------------------------------------------------------------------

_VERIFIED: set[tuple[str, str]] = set()
_VERIFIED_LOCK = threading.Lock()

# Environment variables that decide which database a connection factory
# reaches. Successful checks are cached per combination, so switching
# LOCAL/SERVER data mode re-verifies against the other database.
_SCOPE_ENV_VARS = (
    "DB_DSN",
    "DATABASE_URL",
    "LOCAL_DB_DSN",
    "SERVER_DB_DSN",
    "DATA_MODE",
    "DATAMIND_LOCAL_ONLY_MODE",
)


def _default_scope() -> str:
    raw = "\x1f".join(os.getenv(name, "") for name in _SCOPE_ENV_VARS)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def check_with_connection(unit: SchemaUnit, conn: Any, *, read_only: bool = False) -> SchemaCheck:
    """Verify *unit* on an open connection using catalog reads only.

    ``read_only=True`` marks the transaction read-only; only pass it for
    connections the guard owns (a caller's connection may need to write
    after the check).
    """
    req = unit.requirements
    result = SchemaCheck(unit=unit, missing_files=[str(p) for p in unit.missing_files()])
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        if read_only:
            cur.execute("SET TRANSACTION READ ONLY")
        if req.relations:
            cur.execute(
                "SELECT r FROM unnest(%s::text[]) AS r WHERE to_regclass(r) IS NULL",
                (list(req.relations),),
            )
            result.missing_relations = [row[0] for row in cur.fetchall()]
        if req.columns:
            cur.execute(
                """
                SELECT t, c
                FROM unnest(%s::text[], %s::text[]) AS x(t, c)
                WHERE to_regclass(t) IS NULL
                   OR NOT EXISTS (
                        SELECT 1
                        FROM pg_attribute a
                        WHERE a.attrelid = to_regclass(t)
                          AND a.attname = c
                          AND a.attnum > 0
                          AND NOT a.attisdropped
                   )
                """,
                ([t for t, _ in req.columns], [c for _, c in req.columns]),
            )
            result.missing_columns = [f"{t}.{c}" for t, c in cur.fetchall()]
        if req.functions:
            cur.execute(
                """
                SELECT f
                FROM unnest(%s::text[]) AS f
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM pg_proc p
                    JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = split_part(f, '.', 1)
                      AND p.proname = split_part(f, '.', 2)
                )
                """,
                (list(req.functions),),
            )
            result.missing_functions = [f"{row[0]}()" for row in cur.fetchall()]
    return result


def check(unit: SchemaUnit, connection_factory: ConnectionFactory) -> SchemaCheck:
    """Open a dedicated read-only connection and verify *unit*."""
    with connection_factory() as conn:
        return check_with_connection(unit, conn, read_only=True)


def require_on_connection(unit: SchemaUnit, conn: Any) -> None:
    """Verify *unit* (and dependencies) on a caller-owned connection.

    For engine code that already holds a connection. The caller's
    transaction mode is not changed.
    """
    for dependency in unit.depends_on:
        require_on_connection(dependency, conn)
    result = check_with_connection(unit, conn, read_only=False)
    if not result.ok:
        LOGGER.error("schema_guard.not_ready unit=%s missing=%s", unit.key, result.missing)
        raise SchemaNotReadyError(unit, result.missing)


def require(
    unit: SchemaUnit,
    connection_factory: ConnectionFactory,
    *,
    scope: Optional[str] = None,
) -> None:
    """Raise :class:`SchemaNotReadyError` unless *unit* (and dependencies) exist.

    Successful checks are cached per process and per database scope, so a
    normal page render costs one catalog query per unit, the first time only,
    and no connection at all afterwards.
    """
    active_scope = scope or _default_scope()
    for dependency in unit.depends_on:
        require(dependency, connection_factory, scope=active_scope)

    cache_key = (unit.key, active_scope)
    with _VERIFIED_LOCK:
        if cache_key in _VERIFIED:
            return
    result = check(unit, connection_factory)
    if not result.ok:
        LOGGER.error("schema_guard.not_ready unit=%s missing=%s", unit.key, result.missing)
        raise SchemaNotReadyError(unit, result.missing)
    with _VERIFIED_LOCK:
        _VERIFIED.add(cache_key)


def reset_cache() -> None:
    """Testing hook: forget successful verifications."""
    with _VERIFIED_LOCK:
        _VERIFIED.clear()


# ---------------------------------------------------------------------------
# Explicit application (administrative, never called by runtime code)
# ---------------------------------------------------------------------------


def plan(unit: SchemaUnit, *, include_dependencies: bool = True) -> list[dict[str, Any]]:
    """Describe what ``apply`` would execute, without touching the database."""
    steps: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _walk(u: SchemaUnit) -> None:
        if u.key in seen:
            return
        seen.add(u.key)
        if include_dependencies:
            for dep in u.depends_on:
                _walk(dep)
        for stmt in u.pre_statements:
            steps.append({"unit": u.key, "kind": "statement", "sql": stmt})
        for path in u.sql_files:
            profile = summarize_statements(path.read_text(encoding="utf-8")) if path.exists() else {}
            steps.append(
                {
                    "unit": u.key,
                    "kind": "file",
                    "path": str(path),
                    "exists": path.exists(),
                    "profile": profile,
                }
            )

    _walk(unit)
    return steps


def apply(
    unit: SchemaUnit,
    connection_factory: ConnectionFactory,
    *,
    include_dependencies: bool = True,
    echo: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """Execute the unit's SQL. Only the schema CLI may call this."""
    executed: list[str] = []
    say = echo or (lambda _msg: None)
    if include_dependencies:
        for dep in unit.depends_on:
            executed.extend(apply(dep, connection_factory, include_dependencies=True, echo=echo))

    texts: list[tuple[str, str]] = [(f"statement: {s}", s) for s in unit.pre_statements]
    texts.extend((str(p), p.read_text(encoding="utf-8")) for p in unit.sql_files if p.exists())

    if unit.single_transaction:
        with connection_factory() as conn:
            with conn.cursor() as cur:
                for label, sql in texts:
                    say(f"apply {unit.key}: {label}")
                    cur.execute(sql)
                    executed.append(label)
    else:
        for label, sql in texts:
            with connection_factory() as conn:
                with conn.cursor() as cur:
                    say(f"apply {unit.key}: {label}")
                    cur.execute(sql)
                    executed.append(label)
    with _VERIFIED_LOCK:
        for key in [k for k in _VERIFIED if k[0] == unit.key]:
            _VERIFIED.discard(key)
    return executed
