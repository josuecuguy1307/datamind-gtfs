from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Any, Sequence
import json

import psycopg2
import psycopg2.extras
from psycopg2.extras import RealDictCursor, execute_values

from phase1_nodes.datamind.services.openmaps_extractor.src.settings import DB_DSN



# ------------------------------------------------------------
# Connection helper
# ------------------------------------------------------------

@contextmanager
def db_conn(dsn: str | None = None):
    """
    Context-managed psycopg2 connection.

    Key behavior:
    - UUID support (uuid.UUID params/results)
    - JSON/JSONB decoding into Python dict/list
    - Commits on success, rollbacks on exception.
    """
    active_dsn = str(dsn or DB_DSN or "").strip()
    if not active_dsn:
        if os.getenv("DATAMIND_LOCAL_ONLY_MODE", "").strip().lower() in {"1", "true", "t", "yes", "y", "on"}:
            raise RuntimeError(
                "Phase 1 data is unavailable: configure LOCAL_DB_DSN for the local-only session. "
                "Remote database fallbacks are disabled."
            )
        raise RuntimeError("Phase 1 data is unavailable: configure DB_DSN before running this operation.")

    conn = psycopg2.connect(active_dsn)

    # ✅ UUID adapter (fixes: can't adapt type 'UUID')
    psycopg2.extras.register_uuid(conn_or_curs=conn)

    # ✅ IMPORTANT: decode json/jsonb -> dict/list (fixes tags being str)
    psycopg2.extras.register_default_json(conn_or_curs=conn, loads=json.loads)
    psycopg2.extras.register_default_jsonb(conn_or_curs=conn, loads=json.loads)

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------------------------------------------
# Query helpers
# ------------------------------------------------------------

def fetchall(conn, sql: str, params: Any = None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetchone(conn, sql: str, params: Any = None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def exec_sql(conn, sql: str, params: Any = None) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)


def exec_values(
    conn,
    sql: str,
    rows: Sequence[Sequence[Any]],
    template: str | None = None,
    page_size: int = 1000,
) -> None:
    """
    Bulk execution helper using psycopg2.extras.execute_values.

    - sql should contain: VALUES %s
    - rows is a sequence of tuples/lists
    - template is optional
    """
    if not rows:
        return

    with conn.cursor() as cur:
        execute_values(cur, sql, rows, template=template, page_size=page_size)
