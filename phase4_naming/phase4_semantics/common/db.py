# phase4_semantics/common/db.py
from __future__ import annotations

import os
import json
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Iterator
from urllib.parse import quote_plus

import psycopg2
from psycopg2.extras import RealDictCursor, Json, execute_values


# ============================================================
# Connection helpers
# ============================================================

def get_db_dsn() -> str:
    """
    Phase4 DSN priority:
      1) DB_DSN (matches Phase3)
      2) DATABASE_URL
      3) PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD
      4) final fallback = datamind_ml local
    """
    local_only = os.getenv("DATAMIND_LOCAL_ONLY_MODE", "").strip().lower() in {
        "1", "true", "t", "yes", "y", "on"
    }
    if local_only:
        dsn = (
            os.getenv("LOCAL_DB_DSN")
            or os.getenv("DATAMIND_LOCAL_DB_DSN")
            or os.getenv("DB_DSN_LOCAL")
        )
        if not dsn:
            raise RuntimeError(
                "Phase 4 data is unavailable: configure LOCAL_DB_DSN for the local-only session. "
                "Remote database fallbacks are disabled."
            )
        return dsn

    dsn = os.getenv("DB_DSN") or os.getenv("DATABASE_URL")
    if dsn:
        return dsn

    supabase_host = os.getenv("SUPABASE_DB_HOST")
    if supabase_host:
        supabase_port = os.getenv("SUPABASE_DB_PORT", "5432")
        supabase_db = os.getenv("SUPABASE_DB_NAME", "postgres")
        supabase_user = os.getenv("SUPABASE_DB_USER", "postgres")
        supabase_pw = os.getenv("SUPABASE_DB_PASSWORD", "")
        return (
            f"postgresql://{quote_plus(supabase_user)}:{quote_plus(supabase_pw)}"
            f"@{supabase_host}:{supabase_port}/{supabase_db}?sslmode=require"
        )

    host = os.getenv("PGHOST", "127.0.0.1")
    port = os.getenv("PGPORT", "5432")
    db = os.getenv("PGDATABASE", "postgres")
    user = os.getenv("PGUSER", "postgres")
    pw = os.getenv("PGPASSWORD", "")

    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def _connect_kwargs() -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "connect_timeout": int(os.getenv("PGCONNECT_TIMEOUT", "10")),
        "keepalives": int(os.getenv("PGKEEPALIVES", "1")),
        "keepalives_idle": int(os.getenv("PGKEEPALIVES_IDLE", "30")),
        "keepalives_interval": int(os.getenv("PGKEEPALIVES_INTERVAL", "10")),
        "keepalives_count": int(os.getenv("PGKEEPALIVES_COUNT", "5")),
    }

    statement_timeout_ms = int(os.getenv("PGSTATEMENT_TIMEOUT_MS", "0"))
    if statement_timeout_ms > 0:
        kwargs["options"] = f"-c statement_timeout={statement_timeout_ms}"

    return kwargs


# ============================================================
# Connection / cursor context managers
# ============================================================

@contextmanager
def get_conn(readonly: bool = False) -> Iterator["psycopg2.extensions.connection"]:
    conn = psycopg2.connect(get_db_dsn(), **_connect_kwargs())
    conn.autocommit = False
    try:
        if readonly:
            with conn.cursor() as cur:
                cur.execute("BEGIN; SET TRANSACTION READ ONLY;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def db_cursor(dict_rows: bool = True, readonly: bool = False):
    """
    Usage:
      with db_cursor() as cur:
          cur.execute("SELECT 1")
    """
    with get_conn(readonly=readonly) as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor) if dict_rows else conn.cursor()
        try:
            yield cur
        finally:
            cur.close()


# ============================================================
# Query helpers
# ============================================================

def fetchall(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    with db_cursor(readonly=True) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def fetchone(sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
    with db_cursor(readonly=True) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def execute(sql: str, params: Sequence[Any] = ()) -> None:
    with db_cursor(dict_rows=False) as cur:
        cur.execute(sql, params)


def execute_returning(sql: str, params: Sequence[Any] = ()) -> Dict[str, Any]:
    with db_cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Expected RETURNING row but got none.")
        return dict(row)


def executemany(sql: str, seq_of_params: Iterable[Sequence[Any]]) -> None:
    with db_cursor(dict_rows=False) as cur:
        cur.executemany(sql, list(seq_of_params))


def bulk_insert_values(sql_template: str, rows: List[Sequence[Any]], page_size: int = 1000) -> None:
    if not rows:
        return
    with db_cursor(dict_rows=False) as cur:
        execute_values(cur, sql_template, rows, page_size=page_size)


# ============================================================
# JSON helper
# ============================================================

def jsonb(value: Any) -> Json:
    return Json(value, dumps=json.dumps)
