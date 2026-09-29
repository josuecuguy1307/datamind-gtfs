"""
Phase 2 – Semantic Geocoder
PostgreSQL connection utilities.

This module defines the SINGLE canonical way to open database connections
for Phase 2 pipelines.

RULES:
- Always use `with db_conn() as conn:`
- Never call psycopg2.connect() directly outside this file
- All transactions are explicit and safe
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import quote_plus

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PGConnection
from psycopg2.extras import RealDictCursor


psycopg2.extras.register_uuid()

# ============================================================
# Configuration
# ============================================================

def _supabase_dsn_from_env() -> str | None:
    host = os.getenv("SUPABASE_DB_HOST")
    if not host:
        return None
    port = os.getenv("SUPABASE_DB_PORT", "5432")
    name = os.getenv("SUPABASE_DB_NAME", "postgres")
    user = os.getenv("SUPABASE_DB_USER", "postgres")
    password = os.getenv("SUPABASE_DB_PASSWORD", "")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{name}"
        "?sslmode=require"
    )


def _local_only_mode_enabled() -> bool:
    return os.getenv("DATAMIND_LOCAL_ONLY_MODE", "").strip().lower() in {
        "1", "true", "t", "yes", "y", "on"
    }


def _resolve_db_dsn() -> str | None:
    """Resolve a DSN at connection time, respecting the local-only boundary."""
    if _local_only_mode_enabled():
        return (
            os.getenv("LOCAL_DB_DSN")
            or os.getenv("DATAMIND_LOCAL_DB_DSN")
            or os.getenv("DB_DSN_LOCAL")
        )
    return os.getenv("DB_DSN") or os.getenv("DATABASE_URL") or _supabase_dsn_from_env()


# Compatibility export for callers that inspect the configured DSN.  Connection
# creation deliberately resolves again so a local-only console can never reuse
# an earlier server DSN imported from the environment.
DB_DSN: str | None = _resolve_db_dsn()


# ============================================================
# Low-level connection factory (PRIVATE)
# ============================================================

def _get_connection() -> PGConnection:
    """
    Create a new PostgreSQL connection.

    - No pooling (predictable, explicit)
    - Autocommit disabled
    - Dict-style cursors by default
    """
    dsn = _resolve_db_dsn()
    if not dsn:
        if _local_only_mode_enabled():
            raise RuntimeError(
                "Phase 2 data is unavailable: configure LOCAL_DB_DSN for the local-only session. "
                "Remote database fallbacks are disabled."
            )
        raise RuntimeError(
            "Phase 2 data is unavailable: configure DB_DSN before running this operation."
        )
    return psycopg2.connect(
        dsn,
        cursor_factory=RealDictCursor,
    )


# ============================================================
# Public transaction context manager (CANONICAL)
# ============================================================

@contextmanager
def db_conn() -> Iterator[PGConnection]:
    """
    Context-managed database connection.

    Guarantees:
    - commit on success
    - rollback on exception
    - connection always closed

    Usage:
        with db_conn() as conn:
            ...
    """
    conn = _get_connection()

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================
# Backward-compat / explicit alias
# ============================================================

# If someone mentally thinks "session", it still works,
# but db_conn is the ONE name used by the pipeline.
db_session = db_conn
