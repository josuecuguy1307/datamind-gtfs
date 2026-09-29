from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Any
from urllib.parse import quote_plus

import psycopg2
from psycopg2.extras import RealDictCursor

BASE_DIR = Path(__file__).resolve().parents[1]
GTFS_OUT_DIR = Path(os.getenv("GTFS_OUT_DIR", str(BASE_DIR / "out")))


def get_db_dsn() -> str:
    dsn = os.getenv("DB_DSN") or os.getenv("DATABASE_URL") or os.getenv("DB_DSN_PHASE5")
    if not dsn and os.getenv("SUPABASE_DB_HOST"):
        host = os.getenv("SUPABASE_DB_HOST")
        port = os.getenv("SUPABASE_DB_PORT", "5432")
        name = os.getenv("SUPABASE_DB_NAME", "postgres")
        user = os.getenv("SUPABASE_DB_USER", "postgres")
        password = os.getenv("SUPABASE_DB_PASSWORD", "")
        dsn = (
            f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{name}"
            "?sslmode=require"
        )
    if not dsn:
        raise RuntimeError(
            "DB_DSN is not set. Configure DB_DSN/DATABASE_URL or SUPABASE_DB_* vars."
        )
    return dsn


@contextmanager
def db_conn() -> Iterator[Any]:
    conn = psycopg2.connect(get_db_dsn(), cursor_factory=RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
