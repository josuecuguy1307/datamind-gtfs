from __future__ import annotations
from contextlib import contextmanager
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from phase3_routes.services.route_constructor.src.settings import DB_DSN

@contextmanager
def db_conn():
    local_only = os.getenv("DATAMIND_LOCAL_ONLY_MODE", "").strip().lower() in {"1", "true", "t", "yes", "y", "on"}
    local_dsn = os.getenv("LOCAL_DB_DSN") or os.getenv("DATAMIND_LOCAL_DB_DSN") or os.getenv("DB_DSN_LOCAL")
    active_dsn = local_dsn if local_only else DB_DSN
    if not active_dsn:
        if local_only:
            raise RuntimeError(
                "Phase 3 data is unavailable: configure LOCAL_DB_DSN for the local-only session. "
                "Remote database fallbacks are disabled."
            )
        raise RuntimeError(
            "Missing DB DSN. Set DB_DSN/DATABASE_URL or SUPABASE_DB_* env vars."
        )
    conn = psycopg2.connect(active_dsn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

@contextmanager
def db_cursor(conn):
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        yield cur
    finally:
        cur.close()
