from __future__ import annotations

import json
from typing import Any, Optional

from datamind_console.db.db import db_conn, exec_sql, fetch_all, fetch_one


def _to_json(value: Optional[dict[str, Any]]) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def create_gtfs_artifact(
    *,
    zip_path: str,
    file_hash: str,
    summary_json: Optional[dict[str, Any]],
    approval_token: str,
    notes: Optional[str] = None,
) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO gtfs.gtfs_artifacts
              (status, zip_path, file_hash, summary_json, approval_token, notes)
            VALUES
              ('pending_approval', %s, %s, %s::jsonb, %s, %s)
            RETURNING
              artifact_id::text AS artifact_id,
              created_at,
              status,
              zip_path,
              file_hash,
              summary_json,
              approval_token,
              approved_at,
              approved_by,
              rejected_at,
              rejected_by,
              downloaded_at,
              notes
            """,
            (
                str(zip_path or "").strip(),
                str(file_hash or "").strip(),
                _to_json(summary_json),
                str(approval_token or "").strip(),
                notes,
            ),
        )
    return row or {}


def get_gtfs_artifact(artifact_id: str) -> Optional[dict]:
    with db_conn(readonly=True) as conn:
        return fetch_one(
            conn,
            """
            SELECT
              artifact_id::text AS artifact_id,
              created_at,
              status,
              zip_path,
              file_hash,
              summary_json,
              approval_token,
              approved_at,
              approved_by,
              rejected_at,
              rejected_by,
              downloaded_at,
              notes
            FROM gtfs.gtfs_artifacts
            WHERE artifact_id = %s
            """,
            (artifact_id,),
        )


def get_gtfs_artifact_by_token(*, approval_token: str, pending_only: bool = True) -> Optional[dict]:
    clauses = ["approval_token = %s"]
    params: list[Any] = [str(approval_token or "").strip()]
    if pending_only:
        clauses.append("status = 'pending_approval'")
    with db_conn(readonly=True) as conn:
        return fetch_one(
            conn,
            f"""
            SELECT
              artifact_id::text AS artifact_id,
              created_at,
              status,
              zip_path,
              file_hash,
              summary_json,
              approval_token,
              approved_at,
              approved_by,
              rejected_at,
              rejected_by,
              downloaded_at,
              notes
            FROM gtfs.gtfs_artifacts
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT 1
            """,
            tuple(params),
        )


def list_gtfs_artifacts(
    *,
    status: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if status and str(status).strip().lower() != "all":
        clauses.append("status = %s")
        params.append(str(status).strip().lower())
    params.append(max(1, int(limit)))
    with db_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            f"""
            SELECT
              artifact_id::text AS artifact_id,
              created_at,
              status,
              zip_path,
              file_hash,
              summary_json,
              approval_token,
              approved_at,
              approved_by,
              rejected_at,
              rejected_by,
              downloaded_at,
              notes
            FROM gtfs.gtfs_artifacts
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )


def approve_gtfs_artifact(*, artifact_id: str, approved_by: str, notes: Optional[str] = None) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            UPDATE gtfs.gtfs_artifacts
            SET
              status = 'approved',
              approved_at = NOW(),
              approved_by = %s,
              notes = COALESCE(%s, notes)
            WHERE artifact_id = %s
              AND status IN ('pending_approval', 'approved')
            RETURNING
              artifact_id::text AS artifact_id,
              created_at,
              status,
              zip_path,
              file_hash,
              summary_json,
              approval_token,
              approved_at,
              approved_by,
              rejected_at,
              rejected_by,
              downloaded_at,
              notes
            """,
            (str(approved_by or "").strip() or "system", notes, artifact_id),
        )
    return row or {}


def reject_gtfs_artifact(*, artifact_id: str, rejected_by: str, notes: Optional[str] = None) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            UPDATE gtfs.gtfs_artifacts
            SET
              status = 'rejected',
              rejected_at = NOW(),
              rejected_by = %s,
              notes = COALESCE(%s, notes)
            WHERE artifact_id = %s
              AND status IN ('pending_approval', 'rejected')
            RETURNING
              artifact_id::text AS artifact_id,
              created_at,
              status,
              zip_path,
              file_hash,
              summary_json,
              approval_token,
              approved_at,
              approved_by,
              rejected_at,
              rejected_by,
              downloaded_at,
              notes
            """,
            (str(rejected_by or "").strip() or "system", notes, artifact_id),
        )
    return row or {}


def mark_gtfs_artifact_downloaded(*, artifact_id: str, notes: Optional[str] = None) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            UPDATE gtfs.gtfs_artifacts
            SET
              status = 'downloaded',
              downloaded_at = NOW(),
              notes = COALESCE(%s, notes)
            WHERE artifact_id = %s
              AND status IN ('approved', 'downloaded')
            RETURNING
              artifact_id::text AS artifact_id,
              created_at,
              status,
              zip_path,
              file_hash,
              summary_json,
              approval_token,
              approved_at,
              approved_by,
              rejected_at,
              rejected_by,
              downloaded_at,
              notes
            """,
            (notes, artifact_id),
        )
    return row or {}


def create_gtfs_notification(
    *,
    artifact_id: str,
    channel: str,
    provider: str,
    to_address: str,
    status: str = "queued",
    provider_message_id: Optional[str] = None,
    error_text: Optional[str] = None,
) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO gtfs.gtfs_notifications
              (artifact_id, channel, provider, to_address, status, provider_message_id, error_text)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s)
            RETURNING
              notification_id::text AS notification_id,
              created_at,
              artifact_id::text AS artifact_id,
              channel,
              provider,
              to_address,
              status,
              provider_message_id,
              error_text
            """,
            (
                artifact_id,
                str(channel or "whatsapp"),
                str(provider or "meta_cloud_api"),
                str(to_address or "").strip(),
                str(status or "queued"),
                provider_message_id,
                error_text,
            ),
        )
    return row or {}


def update_gtfs_notification(
    *,
    notification_id: str,
    status: Optional[str] = None,
    provider_message_id: Optional[str] = None,
    error_text: Optional[str] = None,
) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            UPDATE gtfs.gtfs_notifications
            SET
              status = COALESCE(%s, status),
              provider_message_id = COALESCE(%s, provider_message_id),
              error_text = COALESCE(%s, error_text)
            WHERE notification_id = %s
            RETURNING
              notification_id::text AS notification_id,
              created_at,
              artifact_id::text AS artifact_id,
              channel,
              provider,
              to_address,
              status,
              provider_message_id,
              error_text
            """,
            (
                (str(status).strip() if status else None),
                provider_message_id,
                error_text,
                notification_id,
            ),
        )
    return row or {}


def list_gtfs_notifications(*, artifact_id: Optional[str] = None, limit: int = 200) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if artifact_id:
        clauses.append("artifact_id = %s")
        params.append(artifact_id)
    params.append(max(1, int(limit)))
    with db_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            f"""
            SELECT
              notification_id::text AS notification_id,
              created_at,
              artifact_id::text AS artifact_id,
              channel,
              provider,
              to_address,
              status,
              provider_message_id,
              error_text
            FROM gtfs.gtfs_notifications
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )


def log_gtfs_audit(
    *,
    action: str,
    actor: str,
    artifact_id: Optional[str] = None,
    payload_json: Optional[dict[str, Any]] = None,
) -> dict:
    with db_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO gtfs.gtfs_audit_log
              (artifact_id, action, actor, payload_json)
            VALUES
              (%s, %s, %s, %s::jsonb)
            RETURNING
              audit_id::text AS audit_id,
              created_at,
              artifact_id::text AS artifact_id,
              action,
              actor,
              payload_json
            """,
            (
                artifact_id,
                str(action or "").strip(),
                str(actor or "").strip() or "system",
                _to_json(payload_json),
            ),
        )
    return row or {}


def list_gtfs_audit_log(*, artifact_id: Optional[str] = None, limit: int = 200) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if artifact_id:
        clauses.append("artifact_id = %s")
        params.append(artifact_id)
    params.append(max(1, int(limit)))
    with db_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            f"""
            SELECT
              audit_id::text AS audit_id,
              created_at,
              artifact_id::text AS artifact_id,
              action,
              actor,
              payload_json
            FROM gtfs.gtfs_audit_log
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )


def get_latest_gtfs_artifact_by_zip_path(zip_path: str) -> Optional[dict]:
    with db_conn(readonly=True) as conn:
        return fetch_one(
            conn,
            """
            SELECT
              artifact_id::text AS artifact_id,
              created_at,
              status,
              zip_path,
              file_hash,
              summary_json,
              approval_token,
              approved_at,
              approved_by,
              rejected_at,
              rejected_by,
              downloaded_at,
              notes
            FROM gtfs.gtfs_artifacts
            WHERE zip_path = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (str(zip_path or "").strip(),),
        )
