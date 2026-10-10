"""Canonical database access for the console.

Use these patterns, and nothing else, in new or touched code (see
``docs/DB_ACCESS.md``):

* **Read-only query**: ``query_all(sql, params)``, ``query_one``,
  ``query_scalar``. Each call opens a connection, marks the transaction
  ``READ ONLY``, fetches, ends the transaction and closes the connection.
  Nothing can be left "idle in transaction".
* **Several reads that must see one snapshot**: ``with read_only_connection()
  as conn:`` plus ``fetch_all(conn, ...)``.
* **Transactional write**: ``with transaction() as conn:`` commits on
  success, rolls back on any exception, always closes.
* **Never** cache a connection (``st.cache_resource``) or keep one across
  Streamlit reruns. Cache *results* (``st.cache_data``) instead.

Parameter convention, unchanged from earlier releases: SQL is always
interpolated by psycopg2, so a literal ``%`` must be written as ``%%`` (for
example ``LIKE 'Cerca de %%'``).
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional, Sequence

import psycopg2
import psycopg2.extras

from datamind_console.common.config import CFG, local_only_mode_enabled, resolve_active_db_dsn


@dataclass(frozen=True)
class DBConfig:
    dsn: str
    connect_timeout_s: int = 10


def _get_db_config() -> DBConfig:
    dsn = resolve_active_db_dsn()
    if not dsn and local_only_mode_enabled():
        # Local-only sessions must never fall back to a remote/server DSN.
        raise RuntimeError(
            "Database unavailable: local-only mode is enabled but LOCAL_DB_DSN is not configured. "
            "Remote database fallbacks are disabled."
        )
    dsn = dsn or CFG.db_dsn
    if not dsn:
        raise RuntimeError(
            "No database DSN configured. Set DB_DSN (recommended), DATABASE_URL, "
            "DATAMIND_DB_DSN, or SUPABASE_DB_* env vars."
        )
    return DBConfig(dsn=dsn, connect_timeout_s=CFG.db_connect_timeout_s)


def active_dsn() -> str:
    """The DSN every console component should use (honours local-only mode).

    For services that manage their own transactions (e.g. the HADES swap
    service takes a ``dsn=`` argument). Never log or display the value.
    """
    return _get_db_config().dsn


@contextmanager
def db_conn(*, readonly: bool = False) -> Iterator[psycopg2.extensions.connection]:
    """
    Context manager returning a psycopg2 connection.

    - readonly=True uses transaction read-only where supported.
    - autocommit is False; we commit on success, rollback on error.
    """
    cfg = _get_db_config()
    conn = psycopg2.connect(
        cfg.dsn,
        connect_timeout=cfg.connect_timeout_s,
    )
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # best-effort: readonly transaction
            if readonly:
                cur.execute("SET TRANSACTION READ ONLY;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_one(conn, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return dict(row) if row else None


def fetch_all(conn, sql: str, params: Optional[Sequence[Any]] = None) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        return [dict(r) for r in rows]


def exec_sql(conn, sql: str, params: Optional[Sequence[Any]] = None) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.rowcount


def exec_many(conn, sql: str, rows: Iterable[Sequence[Any]]) -> int:
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, list(rows))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Standard patterns (Phase 2, 2026-10-08)
# ---------------------------------------------------------------------------


@contextmanager
def read_only_connection(*, statement_timeout_ms: Optional[int] = None) -> Iterator[psycopg2.extensions.connection]:
    """A short-lived connection whose transaction is READ ONLY.

    The transaction always ends (commit) and the connection is always
    closed on exit, including on error.
    """
    with db_conn(readonly=True) as conn:
        if statement_timeout_ms:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = %s", (int(statement_timeout_ms),))
        yield conn


@contextmanager
def transaction() -> Iterator[psycopg2.extensions.connection]:
    """Explicit write transaction: commit on success, rollback on error."""
    with db_conn(readonly=False) as conn:
        yield conn


def query_all(
    sql: str,
    params: Optional[Sequence[Any]] = None,
    *,
    statement_timeout_ms: Optional[int] = None,
) -> list[dict]:
    """Run one read-only query and return all rows as dicts."""
    with read_only_connection(statement_timeout_ms=statement_timeout_ms) as conn:
        return fetch_all(conn, sql, params)


def query_one(
    sql: str,
    params: Optional[Sequence[Any]] = None,
    *,
    statement_timeout_ms: Optional[int] = None,
) -> Optional[dict]:
    """Run one read-only query and return the first row (or ``None``)."""
    with read_only_connection(statement_timeout_ms=statement_timeout_ms) as conn:
        return fetch_one(conn, sql, params)


def query_scalar(
    sql: str,
    params: Optional[Sequence[Any]] = None,
    *,
    default: Any = None,
    statement_timeout_ms: Optional[int] = None,
) -> Any:
    """Run one read-only query and return the first column of the first row."""
    row = query_one(sql, params, statement_timeout_ms=statement_timeout_ms)
    if not row:
        return default
    return next(iter(row.values()), default)

