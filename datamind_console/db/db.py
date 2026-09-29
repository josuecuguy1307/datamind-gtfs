from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional, Sequence

import psycopg2
import psycopg2.extras

from datamind_console.common.config import CFG, resolve_active_db_dsn



@dataclass(frozen=True)
class DBConfig:
    dsn: str
    connect_timeout_s: int = 10


def _get_db_config() -> DBConfig:
    dsn = resolve_active_db_dsn() or CFG.db_dsn
    if not dsn:
        raise RuntimeError(
            "No database DSN configured. Set DB_DSN (recommended), DATABASE_URL, "
            "DATAMIND_DB_DSN, or SUPABASE_DB_* env vars."
        )
    return DBConfig(dsn=dsn, connect_timeout_s=CFG.db_connect_timeout_s)


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
