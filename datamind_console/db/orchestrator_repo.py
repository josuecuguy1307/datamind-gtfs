from __future__ import annotations

import json
from typing import Any, Optional

from datamind_console.db.db import db_conn, exec_sql, fetch_all, fetch_one


# ============================================================
# Sessions
# ============================================================

def create_session(
    *,
    user_id: Optional[str] = None,
    profile: str = "balanced",
    dry_run: bool = False,
    metadata: Optional[dict] = None,
) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO console.orchestrator_sessions
              (user_id, profile, dry_run, metadata)
            VALUES
              (%s, %s, %s, %s::jsonb)
            RETURNING
              session_id, user_id, profile, status,
              current_step_idx, dry_run, metadata,
              started_at, ended_at, created_at, updated_at
            """,
            (user_id, profile, dry_run, _to_json(metadata or {})),
        )
        return row or {}


def get_session(session_id: str) -> Optional[dict]:
    with db_conn(readonly=True) as conn:
        return fetch_one(
            conn,
            """
            SELECT
              session_id, user_id, profile, status,
              current_step_idx, dry_run, metadata,
              started_at, ended_at, created_at, updated_at
            FROM console.orchestrator_sessions
            WHERE session_id = %s
            """,
            (session_id,),
        )


def list_sessions(*, limit: int = 50) -> list[dict]:
    with db_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            """
            SELECT
              session_id, user_id, profile, status,
              current_step_idx, dry_run,
              started_at, ended_at, created_at, updated_at
            FROM console.orchestrator_sessions
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (int(limit),),
        )


def update_session_status(
    session_id: str,
    *,
    status: str,
    current_step_idx: Optional[int] = None,
) -> Optional[dict]:
    with db_conn() as conn:
        if current_step_idx is not None:
            row = fetch_one(
                conn,
                """
                UPDATE console.orchestrator_sessions
                SET status = %s,
                    current_step_idx = %s,
                    started_at = CASE
                      WHEN started_at IS NULL AND %s = 'running' THEN NOW()
                      ELSE started_at
                    END
                WHERE session_id = %s
                RETURNING
                  session_id, user_id, profile, status,
                  current_step_idx, dry_run, metadata,
                  started_at, ended_at, created_at, updated_at
                """,
                (status, current_step_idx, status, session_id),
            )
        else:
            row = fetch_one(
                conn,
                """
                UPDATE console.orchestrator_sessions
                SET status = %s,
                    started_at = CASE
                      WHEN started_at IS NULL AND %s = 'running' THEN NOW()
                      ELSE started_at
                    END
                WHERE session_id = %s
                RETURNING
                  session_id, user_id, profile, status,
                  current_step_idx, dry_run, metadata,
                  started_at, ended_at, created_at, updated_at
                """,
                (status, status, session_id),
            )
        return row


def end_session(session_id: str, *, status: str) -> Optional[dict]:
    with db_conn() as conn:
        return fetch_one(
            conn,
            """
            UPDATE console.orchestrator_sessions
            SET status = %s,
                ended_at = NOW()
            WHERE session_id = %s
            RETURNING
              session_id, user_id, profile, status,
              current_step_idx, dry_run, metadata,
              started_at, ended_at, created_at, updated_at
            """,
            (status, session_id),
        )


def update_session_metadata(
    session_id: str,
    *,
    metadata_patch: Optional[dict] = None,
) -> Optional[dict]:
    patch = metadata_patch or {}
    with db_conn() as conn:
        return fetch_one(
            conn,
            """
            UPDATE console.orchestrator_sessions
            SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
            WHERE session_id = %s
            RETURNING
              session_id, user_id, profile, status,
              current_step_idx, dry_run, metadata,
              started_at, ended_at, created_at, updated_at
            """,
            (_to_json(patch), session_id),
        )


# ============================================================
# Steps
# ============================================================

def create_step(
    *,
    session_id: str,
    step_key: str,
    step_idx: int,
    policy: str = "gate",
) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO console.orchestrator_steps
              (session_id, step_key, step_idx, policy)
            VALUES
              (%s, %s, %s, %s)
            RETURNING
              step_id, session_id, step_key, step_idx,
              status, policy, started_at, ended_at,
              output, error, created_at
            """,
            (session_id, step_key, step_idx, policy),
        )
        return row or {}


def get_step(step_id: str) -> Optional[dict]:
    with db_conn(readonly=True) as conn:
        return fetch_one(
            conn,
            """
            SELECT
              step_id, session_id, step_key, step_idx,
              status, policy, started_at, ended_at,
              output, error, created_at
            FROM console.orchestrator_steps
            WHERE step_id = %s
            """,
            (step_id,),
        )


def list_steps_for_session(session_id: str) -> list[dict]:
    with db_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            """
            SELECT
              step_id, session_id, step_key, step_idx,
              status, policy, started_at, ended_at,
              output, error, created_at
            FROM console.orchestrator_steps
            WHERE session_id = %s
            ORDER BY step_idx ASC
            """,
            (session_id,),
        )


def update_step_status(
    step_id: str,
    *,
    status: str,
    output: Optional[dict] = None,
    error: Optional[str] = None,
) -> Optional[dict]:
    with db_conn() as conn:
        return fetch_one(
            conn,
            """
            UPDATE console.orchestrator_steps
            SET status = %s,
                output = COALESCE(%s::jsonb, output),
                error = COALESCE(%s, error),
                started_at = CASE
                  WHEN started_at IS NULL AND %s = 'running' THEN NOW()
                  ELSE started_at
                END,
                ended_at = CASE
                  WHEN %s IN ('auto_approved','approved','rejected','skipped','completed','failed')
                    THEN NOW()
                  ELSE ended_at
                END
            WHERE step_id = %s
            RETURNING
              step_id, session_id, step_key, step_idx,
              status, policy, started_at, ended_at,
              output, error, created_at
            """,
            (
                status,
                _to_json(output) if output is not None else None,
                error,
                status,
                status,
                step_id,
            ),
        )


# ============================================================
# Approvals
# ============================================================

def create_approval(*, step_id: str, session_id: str) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO console.approval_queue
              (step_id, session_id)
            VALUES
              (%s, %s)
            RETURNING
              approval_id, step_id, session_id,
              action, reason, decided_by, decided_at, created_at
            """,
            (step_id, session_id),
        )
        return row or {}


def get_pending_approvals(session_id: str) -> list[dict]:
    with db_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            """
            SELECT
              a.approval_id, a.step_id, a.session_id,
              a.action, a.reason, a.decided_by, a.decided_at,
              a.created_at,
              s.step_key, s.step_idx
            FROM console.approval_queue a
            JOIN console.orchestrator_steps s
              ON s.step_id = a.step_id
            WHERE a.session_id = %s
              AND a.action IS NULL
            ORDER BY s.step_idx ASC
            """,
            (session_id,),
        )


def resolve_approval(
    approval_id: str,
    *,
    action: str,
    reason: Optional[str] = None,
    decided_by: Optional[str] = None,
) -> Optional[dict]:
    with db_conn() as conn:
        return fetch_one(
            conn,
            """
            UPDATE console.approval_queue
            SET action = %s,
                reason = %s,
                decided_by = %s,
                decided_at = NOW()
            WHERE approval_id = %s
            RETURNING
              approval_id, step_id, session_id,
              action, reason, decided_by, decided_at, created_at
            """,
            (action, reason, decided_by, approval_id),
        )


# ============================================================
# Helpers
# ============================================================

def _to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
