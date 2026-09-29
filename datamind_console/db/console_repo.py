from __future__ import annotations

import json
import secrets
import os
import math
from numbers import Number
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from typing import Optional, Any

import psycopg2
import psycopg2.extras
from psycopg2 import sql
from psycopg2.extras import execute_values

from datamind_console.db.db import db_conn, exec_sql, fetch_all, fetch_one
from datamind_console.common.config import CFG


# ============================================================
# Auth / Users
# ============================================================

def get_user_by_email(email: str) -> Optional[dict]:
    with db_conn(readonly=True) as conn:
        return fetch_one(
            conn,
            """
            SELECT
              user_id,
              email,
              display_name,
              role,
              password_hash,
              is_active,
              email_verified,
              email_verified_at,
              created_at,
              last_login_at
            FROM console.users
            WHERE email = %s
            """,
            (email,),
        )


def create_user(
    *,
    email: str,
    display_name: str,
    password: str,
    role: str = "viewer",
    is_active: bool = True,
) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO console.users
              (email, display_name, role, password_hash, is_active)
            VALUES
              (%s, %s, %s, crypt(%s, gen_salt('bf')), %s)
            RETURNING
              user_id,
              email,
              display_name,
              role,
              is_active,
              email_verified,
              created_at
            """,
            (email, display_name, role, password, is_active),
        )
        return row or {}


def list_users(*, limit: int = 100) -> list[dict]:
    with db_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            """
            SELECT
              user_id,
              email,
              display_name,
              role,
              is_active,
              email_verified,
              created_at,
              last_login_at
            FROM console.users
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (int(limit),),
        )


def verify_password(email: str, password: str) -> Optional[dict]:
    """
    Uses pgcrypto crypt() verification.
    Login identifier = email (CITEXT).
    """
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            SELECT
              user_id,
              email,
              display_name,
              role,
              is_active,
              email_verified
            FROM console.users
            WHERE email = %s
              AND is_active = TRUE
              AND password_hash = crypt(%s, password_hash)
            """,
            (email, password),
        )

        if not row:
            return None

        exec_sql(
            conn,
            """
            UPDATE console.users
            SET last_login_at = NOW()
            WHERE user_id = %s
            """,
            (row["user_id"],),
        )

        return row


def list_user_roles(user_id: str) -> list[str]:
    """
    Roles are stored directly in console.users.role.
    """
    with db_conn(readonly=True) as conn:
        row = fetch_one(
            conn,
            "SELECT role FROM console.users WHERE user_id = %s",
            (user_id,),
        )
        return [row["role"]] if row else []


def create_email_verification_token(
    *,
    user_id: str,
    email: str,
    ttl_hours: int = 24,
    requested_by: Optional[str] = None,
) -> dict:
    token = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=max(1, int(ttl_hours)))
    preview = (
        "Subject: Confirm your ML DATAMIND GTFS account\n\n"
        "Welcome to ML DATAMIND GTFS.\n"
        f"Use this confirmation code: {token}\n"
        f"Expires at: {expires_at.isoformat()}\n"
    )
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO console.email_verification_tokens
              (user_id, email, token, expires_at, requested_by)
            VALUES
              (%s, %s, %s, %s, %s)
            RETURNING
              token_id,
              user_id,
              email,
              token,
              created_at,
              expires_at,
              consumed_at
            """,
            (user_id, email, token, expires_at, requested_by),
        )
        fetch_one(
            conn,
            """
            INSERT INTO console.email_outbox
              (to_email, subject, body, provider, status)
            VALUES
              (%s, %s, %s, 'mock', 'queued')
            RETURNING email_id
            """,
            (email, "Confirm your DataMind account", preview),
        )
        return row or {}


def confirm_email_token(*, token: str) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            SELECT
              token_id,
              user_id,
              email,
              expires_at,
              consumed_at
            FROM console.email_verification_tokens
            WHERE token = %s
            """,
            (token,),
        )
        if not row:
            return {"ok": False, "error": "Token not found."}
        if row.get("consumed_at") is not None:
            return {"ok": False, "error": "Token already used."}
        if row.get("expires_at") and row["expires_at"] < datetime.now(timezone.utc):
            return {"ok": False, "error": "Token expired."}

        fetch_one(
            conn,
            """
            UPDATE console.email_verification_tokens
            SET consumed_at = NOW()
            WHERE token_id = %s
            RETURNING token_id
            """,
            (row["token_id"],),
        )
        user = fetch_one(
            conn,
            """
            UPDATE console.users
            SET email_verified = TRUE,
                email_verified_at = NOW(),
                updated_at = NOW()
            WHERE user_id = %s
            RETURNING
              user_id,
              email,
              display_name,
              role,
              is_active,
              email_verified,
              email_verified_at
            """,
            (row["user_id"],),
        )
        return {"ok": True, "user": user}


def list_pending_verifications(*, limit: int = 50) -> list[dict]:
    with db_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            """
            SELECT
              t.token_id,
              t.user_id,
              t.email,
              t.token,
              t.created_at,
              t.expires_at,
              t.requested_by,
              u.display_name
            FROM console.email_verification_tokens t
            JOIN console.users u
              ON u.user_id = t.user_id
            WHERE t.consumed_at IS NULL
            ORDER BY t.created_at DESC
            LIMIT %s
            """,
            (int(limit),),
        )


# ============================================================
# Sessions
# ============================================================

def create_session(
    user_id: str,
    *,
    ttl_hours: int = 24,
) -> dict:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)

    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO console.sessions (user_id, token_hash, expires_at)
            VALUES (
              %s,
              digest(gen_random_uuid()::text, 'sha256'),
              %s
            )
            RETURNING
              session_id,
              user_id,
              created_at,
              expires_at,
              revoked_at
            """,
            (user_id, expires_at),
        )
        return row or {}


def revoke_session(session_id: str) -> None:
    with db_conn() as conn:
        exec_sql(
            conn,
            """
            UPDATE console.sessions
            SET revoked_at = NOW()
            WHERE session_id = %s
              AND revoked_at IS NULL
            """,
            (session_id,),
        )


def get_session(session_id: str) -> Optional[dict]:
    with db_conn(readonly=True) as conn:
        return fetch_one(
            conn,
            """
            SELECT
              s.session_id,
              s.user_id,
              s.created_at,
              s.expires_at,
              s.revoked_at,
              u.email,
              u.display_name,
              u.role,
              u.is_active,
              u.email_verified
            FROM console.sessions s
            JOIN console.users u
              ON u.user_id = s.user_id
            WHERE s.session_id = %s
            """,
            (session_id,),
        )


# ============================================================
# Audit / Decisions
# ============================================================

def log_audit_event(
    *,
    user_id: Optional[str],
    action: str,
    phase: Optional[int] = None,
    item_id: Optional[str] = None,
    candidate_id: Optional[str] = None,
    payload: Optional[dict] = None,
) -> None:
    payload = payload or {}

    with db_conn() as conn:
        exec_sql(
            conn,
            """
            INSERT INTO console.audit_events
              (user_id, action, phase, item_id, candidate_id, payload)
            VALUES
              (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (user_id, action, phase, item_id, candidate_id, _to_json(payload)),
        )


def create_phase_decision(
    *,
    user_id: Optional[str],
    phase: int,
    item_id: str,
    decision: str,
    candidate_id: Optional[str] = None,
    score_at_decision: Optional[float] = None,
    reason_code: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO console.phase_decisions
              (phase, item_id, candidate_id, score_at_decision,
               decision, reason_code, notes, user_id)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING
              decision_id,
              phase,
              item_id,
              candidate_id,
              decision,
              reason_code,
              notes,
              created_at
            """,
            (
                phase,
                item_id,
                candidate_id,
                score_at_decision,
                decision,
                reason_code,
                notes,
                user_id,
            ),
        )
        return row or {}


# ============================================================
# Workspace State
# ============================================================

def upsert_workspace_state(
    *,
    user_id: str,
    current_phase: Optional[int] = None,
    current_subtab: Optional[str] = None,
    current_route_id: Optional[str] = None,
    current_stop_id: Optional[str] = None,
    last_candidate_id: Optional[str] = None,
) -> None:
    with db_conn() as conn:
        exec_sql(
            conn,
            """
            INSERT INTO console.workspace_state
              (user_id,
               current_phase,
               current_subtab,
               current_route_id,
               current_stop_id,
               last_candidate_id,
               updated_at)
            VALUES
              (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET
              current_phase      = EXCLUDED.current_phase,
              current_subtab     = EXCLUDED.current_subtab,
              current_route_id   = EXCLUDED.current_route_id,
              current_stop_id    = EXCLUDED.current_stop_id,
              last_candidate_id  = EXCLUDED.last_candidate_id,
              updated_at         = NOW()
            """,
            (
                user_id,
                current_phase,
                current_subtab,
                current_route_id,
                current_stop_id,
                last_candidate_id,
            ),
        )


def get_workspace_state(user_id: str) -> Optional[dict]:
    with db_conn(readonly=True) as conn:
        return fetch_one(
            conn,
            """
            SELECT
              user_id,
              current_phase,
              current_subtab,
              current_route_id,
              current_stop_id,
              last_candidate_id,
              updated_at
            FROM console.workspace_state
            WHERE user_id = %s
            """,
            (user_id,),
        )


# ============================================================
# Sync Events (server-side log)
# ============================================================

def _server_dsn() -> Optional[str]:
    if getattr(CFG, "local_only_mode", False):
        return None

    dsn = os.getenv("SERVER_DB_DSN") or os.getenv("DATAMIND_SERVER_DB_DSN") or CFG.db_dsn_server
    if dsn:
        return dsn
    host = os.getenv("SUPABASE_DB_HOST")
    if not host:
        return None
    port = os.getenv("SUPABASE_DB_PORT", "5432")
    name = os.getenv("SUPABASE_DB_NAME", "postgres")
    user = os.getenv("SUPABASE_DB_USER", "postgres")
    pw = os.getenv("SUPABASE_DB_PASSWORD", "")
    from urllib.parse import quote_plus

    return f"postgresql://{quote_plus(user)}:{quote_plus(pw)}@{host}:{port}/{name}?sslmode=require"


@contextmanager
def _server_conn():
    dsn = _server_dsn()
    if not dsn:
        raise RuntimeError("SERVER_DB_DSN/SUPABASE_DB_* not configured for sync log.")
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_sync_events_table() -> None:
    with _server_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS console;")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS console.sync_events (
                  sync_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  synced_by TEXT,
                  source_mode TEXT NOT NULL,
                  target_mode TEXT NOT NULL,
                  comment TEXT,
                  status TEXT NOT NULL DEFAULT 'logged',
                  tables_changed JSONB NOT NULL DEFAULT '[]'::jsonb,
                  row_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
                  payload JSONB NOT NULL DEFAULT '{}'::jsonb
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_console_sync_events_synced_at
                ON console.sync_events(synced_at DESC);
                """
            )


def log_sync_event(
    *,
    synced_by: Optional[str],
    source_mode: str,
    target_mode: str,
    comment: str,
    status: str = "logged",
    tables_changed: Optional[list[str]] = None,
    row_counts: Optional[dict] = None,
    payload: Optional[dict] = None,
) -> dict:
    ensure_sync_events_table()
    with _server_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO console.sync_events
                  (synced_by, source_mode, target_mode, comment, status, tables_changed, row_counts, payload)
                VALUES
                  (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
                RETURNING sync_id::text, synced_at, synced_by, source_mode, target_mode, comment, status;
                """,
                (
                    synced_by,
                    source_mode,
                    target_mode,
                    comment,
                    status,
                    json.dumps(tables_changed or [], ensure_ascii=False),
                    json.dumps(row_counts or {}, ensure_ascii=False),
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            return dict(cur.fetchone() or {})


def list_sync_events(*, limit: int = 20) -> list[dict]:
    ensure_sync_events_table()
    with _server_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  sync_id::text,
                  synced_at,
                  synced_by,
                  source_mode,
                  target_mode,
                  comment,
                  status,
                  tables_changed,
                  row_counts
                FROM console.sync_events
                ORDER BY synced_at DESC
                LIMIT %s
                """,
                (int(limit),),
            )
            return [dict(r) for r in (cur.fetchall() or [])]


# ============================================================
# Local -> Server sync engine (duplicate-safe upsert)
# ============================================================

DEFAULT_SYNC_SCHEMAS = (
    "node_prod",
    "geo_prod",
    "route_prod",
    "route_work",
    "semantics",
    "gtfs_work",
)
DEFAULT_SYNC_IGNORE_COLS = {
    "created_at",
    "updated_at",
    "inserted_at",
    "edited_at",
}
DEFAULT_SYNC_STRATEGY = "replace"
SYNC_FLOAT_REL_TOL = 1e-12
SYNC_FLOAT_ABS_TOL = 1e-9


def _local_dsn() -> Optional[str]:
    return (
        os.getenv("LOCAL_DB_DSN")
        or os.getenv("DATAMIND_LOCAL_DB_DSN")
        or os.getenv("DB_DSN_LOCAL")
        or (os.getenv("DB_DSN") if (os.getenv("DATA_MODE", "server").strip().lower() == "local") else None)
    )


@contextmanager
def _local_conn():
    dsn = _local_dsn()
    if not dsn:
        raise RuntimeError("LOCAL_DB_DSN is not configured.")
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _discover_tables(cur, schemas: tuple[str, ...]) -> list[tuple[str, str]]:
    cur.execute(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_type='BASE TABLE'
          AND table_schema = ANY(%s)
        ORDER BY table_schema, table_name
        """,
        (list(schemas),),
    )
    return [(str(r["table_schema"]), str(r["table_name"])) for r in (cur.fetchall() or [])]


def _table_pk_columns(cur, schema: str, table: str) -> list[str]:
    cur.execute(
        """
        SELECT a.attname AS column_name
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
        WHERE i.indisprimary = TRUE
          AND n.nspname = %s
          AND t.relname = %s
        ORDER BY k.ord
        """,
        (schema, table),
    )
    return [str(r["column_name"]) for r in (cur.fetchall() or [])]


def _table_columns(cur, schema: str, table: str) -> list[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    return [str(r["column_name"]) for r in (cur.fetchall() or [])]


def _pk_key(row: dict, pk_cols: list[str]) -> tuple:
    return tuple(row.get(c) for c in pk_cols)


def _values_equal(a: Any, b: Any) -> bool:
    # Treat numeric near-equality as equal to avoid float precision churn between DBs.
    if isinstance(a, Number) and isinstance(b, Number) and not isinstance(a, bool) and not isinstance(b, bool):
        af = float(a)
        bf = float(b)
        if math.isnan(af) and math.isnan(bf):
            return True
        return math.isclose(af, bf, rel_tol=SYNC_FLOAT_REL_TOL, abs_tol=SYNC_FLOAT_ABS_TOL)
    return a == b


def _adapt_value(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return psycopg2.extras.Json(v)
    return v


def _fetch_server_rows_by_pk(
    cur,
    *,
    schema: str,
    table: str,
    all_cols: list[str],
    pk_cols: list[str],
    pk_values: list[tuple],
) -> dict[tuple, dict]:
    if not pk_values:
        return {}

    t_ident = sql.Identifier(schema, table)
    col_sql = sql.SQL(", ").join(sql.Identifier(c) for c in all_cols)

    if len(pk_cols) == 1:
        in_placeholders = ", ".join(["%s"] * len(pk_values))
        q = sql.SQL("SELECT {cols} FROM {tbl} WHERE {pk} IN (" + in_placeholders + ")").format(
            cols=col_sql,
            tbl=t_ident,
            pk=sql.Identifier(pk_cols[0]),
        )
        cur.execute(q, tuple(v[0] for v in pk_values))
    else:
        in_placeholders = ", ".join(["(" + ", ".join(["%s"] * len(pk_cols)) + ")"] * len(pk_values))
        where_cols = sql.SQL(", ").join(sql.Identifier(c) for c in pk_cols)
        q = sql.SQL("SELECT {cols} FROM {tbl} WHERE ({pk_cols}) IN (" + in_placeholders + ")").format(
            cols=col_sql,
            tbl=t_ident,
            pk_cols=where_cols,
        )
        flat: list[Any] = []
        for v in pk_values:
            flat.extend(list(v))
        cur.execute(q, tuple(flat))

    out: dict[tuple, dict] = {}
    for r in (cur.fetchall() or []):
        rr = dict(r)
        out[_pk_key(rr, pk_cols)] = rr
    return out


def _upsert_rows(
    conn,
    *,
    schema: str,
    table: str,
    all_cols: list[str],
    pk_cols: list[str],
    rows: list[dict],
) -> int:
    if not rows:
        return 0
    non_pk = [c for c in all_cols if c not in pk_cols and c not in DEFAULT_SYNC_IGNORE_COLS]
    table_ident = sql.Identifier(schema, table)
    cols_ident = sql.SQL(", ").join(sql.Identifier(c) for c in all_cols)
    pk_ident = sql.SQL(", ").join(sql.Identifier(c) for c in pk_cols)

    if non_pk:
        set_sql = sql.SQL(", ").join(
            sql.SQL("{c}=EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in non_pk
        )
        diff_sql = sql.SQL(" OR ").join(
            sql.SQL("{tbl}.{c} IS DISTINCT FROM EXCLUDED.{c}").format(
                tbl=table_ident,
                c=sql.Identifier(c),
            )
            for c in non_pk
        )
        stmt = sql.SQL(
            "INSERT INTO {tbl} ({cols}) VALUES %s "
            "ON CONFLICT ({pk}) DO UPDATE SET {set_sql} "
            "WHERE {diff_sql}"
        ).format(
            tbl=table_ident,
            cols=cols_ident,
            pk=pk_ident,
            set_sql=set_sql,
            diff_sql=diff_sql,
        )
    else:
        stmt = sql.SQL(
            "INSERT INTO {tbl} ({cols}) VALUES %s "
            "ON CONFLICT ({pk}) DO NOTHING"
        ).format(
            tbl=table_ident,
            cols=cols_ident,
            pk=pk_ident,
        )

    values = [tuple(_adapt_value(r.get(c)) for c in all_cols) for r in rows]
    with conn.cursor() as cur:
        execute_values(cur, stmt.as_string(conn), values, page_size=500)
    return len(values)


def _delete_rows_by_pk(
    conn,
    *,
    schema: str,
    table: str,
    pk_cols: list[str],
    pk_values: list[tuple],
) -> int:
    if not pk_values:
        return 0
    table_ident = sql.Identifier(schema, table)
    with conn.cursor() as cur:
        if len(pk_cols) == 1:
            in_placeholders = ", ".join(["%s"] * len(pk_values))
            stmt = sql.SQL("DELETE FROM {tbl} WHERE {pk} IN (" + in_placeholders + ")").format(
                tbl=table_ident,
                pk=sql.Identifier(pk_cols[0]),
            )
            cur.execute(stmt, tuple(v[0] for v in pk_values))
        else:
            in_placeholders = ", ".join(["(" + ", ".join(["%s"] * len(pk_cols)) + ")"] * len(pk_values))
            pk_sql = sql.SQL(", ").join(sql.Identifier(c) for c in pk_cols)
            stmt = sql.SQL("DELETE FROM {tbl} WHERE ({pk_cols}) IN (" + in_placeholders + ")").format(
                tbl=table_ident,
                pk_cols=pk_sql,
            )
            flat: list[Any] = []
            for v in pk_values:
                flat.extend(list(v))
            cur.execute(stmt, tuple(flat))
        return int(cur.rowcount or 0)


def _insert_rows(
    conn,
    *,
    schema: str,
    table: str,
    all_cols: list[str],
    rows: list[dict],
) -> int:
    if not rows:
        return 0
    table_ident = sql.Identifier(schema, table)
    cols_ident = sql.SQL(", ").join(sql.Identifier(c) for c in all_cols)
    stmt = sql.SQL("INSERT INTO {tbl} ({cols}) VALUES %s").format(
        tbl=table_ident,
        cols=cols_ident,
    )
    values = [tuple(_adapt_value(r.get(c)) for c in all_cols) for r in rows]
    with conn.cursor() as cur:
        execute_values(cur, stmt.as_string(conn), values, page_size=500)
    return len(values)


def sync_local_to_server(
    *,
    synced_by: Optional[str],
    comment: str,
    schemas: Optional[tuple[str, ...]] = None,
    dry_run: bool = True,
    chunk_size: int = 500,
    sync_strategy: str = DEFAULT_SYNC_STRATEGY,
) -> dict:
    strategy = (sync_strategy or DEFAULT_SYNC_STRATEGY).strip().lower()
    if strategy not in {"replace", "upsert"}:
        raise RuntimeError(f"Unsupported sync_strategy: {sync_strategy}")

    use_schemas = schemas or DEFAULT_SYNC_SCHEMAS
    if not use_schemas:
        raise RuntimeError("No schemas selected for sync.")

    if not _local_dsn():
        raise RuntimeError("LOCAL_DB_DSN is not configured.")
    if not _server_dsn():
        raise RuntimeError("SERVER_DB_DSN/SUPABASE_DB_* is not configured.")

    report: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "sync_strategy": strategy,
        "schemas": list(use_schemas),
        "tables": [],
        "total_scanned": 0,
        "total_inserts": 0,
        "total_updates": 0,
        "total_applied": 0,
        "skipped_no_pk": [],
        "errors": [],
    }

    with _local_conn() as lconn, _server_conn() as sconn:
        with lconn.cursor() as lcur, sconn.cursor() as scur:
            tables = _discover_tables(lcur, tuple(use_schemas))
            for schema, table in tables:
                if not dry_run:
                    scur.execute("SAVEPOINT sp_sync_table")
                try:
                    pk_cols = _table_pk_columns(lcur, schema, table)
                    if not pk_cols:
                        report["skipped_no_pk"].append(f"{schema}.{table}")
                        continue
                    all_cols = _table_columns(lcur, schema, table)
                    if not all_cols:
                        continue

                    lcur.execute(
                        sql.SQL("SELECT {cols} FROM {tbl} ORDER BY {pk}").format(
                            cols=sql.SQL(", ").join(sql.Identifier(c) for c in all_cols),
                            tbl=sql.Identifier(schema, table),
                            pk=sql.SQL(", ").join(sql.Identifier(c) for c in pk_cols),
                        )
                    )

                    table_scanned = 0
                    table_inserts = 0
                    table_updates = 0
                    table_applied = 0
                    non_pk = [c for c in all_cols if c not in pk_cols and c not in DEFAULT_SYNC_IGNORE_COLS]

                    while True:
                        chunk = lcur.fetchmany(int(chunk_size))
                        if not chunk:
                            break
                        rows = [dict(r) for r in chunk]
                        table_scanned += len(rows)
                        pk_values = [_pk_key(r, pk_cols) for r in rows]
                        existing = _fetch_server_rows_by_pk(
                            scur,
                            schema=schema,
                            table=table,
                            all_cols=all_cols,
                            pk_cols=pk_cols,
                            pk_values=pk_values,
                        )

                        to_apply: list[dict] = []
                        for r in rows:
                            k = _pk_key(r, pk_cols)
                            ex = existing.get(k)
                            if ex is None:
                                table_inserts += 1
                                to_apply.append(r)
                                continue
                            changed = any(not _values_equal(r.get(c), ex.get(c)) for c in non_pk)
                            if changed:
                                table_updates += 1
                                to_apply.append(r)

                        if not dry_run and to_apply:
                            if strategy == "replace":
                                _delete_rows_by_pk(
                                    sconn,
                                    schema=schema,
                                    table=table,
                                    pk_cols=pk_cols,
                                    pk_values=[_pk_key(r, pk_cols) for r in to_apply],
                                )
                                table_applied += _insert_rows(
                                    sconn,
                                    schema=schema,
                                    table=table,
                                    all_cols=all_cols,
                                    rows=to_apply,
                                )
                            else:
                                table_applied += _upsert_rows(
                                    sconn,
                                    schema=schema,
                                    table=table,
                                    all_cols=all_cols,
                                    pk_cols=pk_cols,
                                    rows=to_apply,
                                )

                    report["tables"].append(
                        {
                            "table": f"{schema}.{table}",
                            "scanned": table_scanned,
                            "inserts": table_inserts,
                            "updates": table_updates,
                            "applied": table_applied,
                        }
                    )
                    report["total_scanned"] += table_scanned
                    report["total_inserts"] += table_inserts
                    report["total_updates"] += table_updates
                    report["total_applied"] += table_applied
                    if not dry_run:
                        scur.execute("RELEASE SAVEPOINT sp_sync_table")
                except Exception as e:
                    try:
                        if not dry_run:
                            scur.execute("ROLLBACK TO SAVEPOINT sp_sync_table")
                            scur.execute("RELEASE SAVEPOINT sp_sync_table")
                    except Exception:
                        pass
                    report["errors"].append(
                        {
                            "table": f"{schema}.{table}",
                            "error": str(e),
                        }
                    )
                    report["tables"].append(
                        {
                            "table": f"{schema}.{table}",
                            "scanned": 0,
                            "inserts": 0,
                            "updates": 0,
                            "applied": 0,
                            "error": str(e),
                        }
                    )
                    continue

    status = "dry_run" if dry_run else "applied"
    log_row = log_sync_event(
        synced_by=synced_by,
        source_mode="local",
        target_mode="server",
        comment=comment or ("dry-run sync" if dry_run else "sync"),
        status=status,
        tables_changed=[t["table"] for t in report["tables"] if (t["inserts"] or t["updates"])],
        row_counts={
            "scanned": report["total_scanned"],
            "inserts": report["total_inserts"],
            "updates": report["total_updates"],
            "applied": report["total_applied"],
        },
        payload=report,
    )
    report["sync_event"] = log_row
    return report


# ============================================================
# Helpers
# ============================================================

def _to_json(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
