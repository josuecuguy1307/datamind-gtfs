from __future__ import annotations

import hashlib
import json
import os
import random
import re
from pathlib import Path
from typing import Any, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from datamind_console.db.gtfs_repo import (
    approve_gtfs_artifact,
    create_gtfs_artifact,
    create_gtfs_notification,
    get_gtfs_artifact,
    get_gtfs_artifact_by_token,
    get_latest_gtfs_artifact_by_zip_path,
    list_gtfs_artifacts,
    list_gtfs_audit_log,
    list_gtfs_notifications,
    log_gtfs_audit,
    mark_gtfs_artifact_downloaded,
    reject_gtfs_artifact,
    update_gtfs_notification,
)

APPROVAL_RE = re.compile(r"^APPROVED\s+([A-Z0-9]{4,10})$")


def _env_bool(key: str, default: bool = False) -> bool:
    value = str(os.getenv(key, "") or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def whatsapp_enabled() -> bool:
    return _env_bool("WHATSAPP_ENABLED", False)


def _normalize_phone(v: str) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def _meta_api_url(phone_number_id: str) -> str:
    return f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"


def _short_summary(summary: dict[str, Any]) -> str:
    if not summary:
        return "no-summary"
    keys = ["export_run_id", "routes", "trips", "stops", "feed_start", "feed_end"]
    parts: list[str] = []
    for k in keys:
        if k in summary and summary.get(k) not in {None, ""}:
            parts.append(f"{k}={summary.get(k)}")
    return ", ".join(parts[:5]) or "summary-available"


def _read_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _new_token(n: int = 6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alphabet) for _ in range(max(4, int(n))))


def send_whatsapp_text(*, to_number: str, text: str) -> dict[str, Any]:
    phone_number_id = str(os.getenv("META_WA_PHONE_NUMBER_ID", "") or "").strip()
    access_token = str(os.getenv("META_WA_ACCESS_TOKEN", "") or "").strip()
    if not phone_number_id or not access_token:
        raise RuntimeError("META_WA_PHONE_NUMBER_ID and META_WA_ACCESS_TOKEN must be configured.")

    payload = {
        "messaging_product": "whatsapp",
        "to": str(to_number).strip(),
        "type": "text",
        "text": {"body": str(text or "")},
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(_meta_api_url(phone_number_id), data=raw, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {access_token}")

    try:
        with urlrequest.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            code = int(resp.getcode() or 200)
    except urlerror.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        raise RuntimeError(f"Meta WhatsApp HTTP error {int(e.code)}: {body[:1200]}") from e
    except Exception as e:
        raise RuntimeError(f"Meta WhatsApp request failed: {e}") from e

    try:
        parsed = json.loads(body or "{}")
    except Exception:
        parsed = {"raw_text": body}
    return {"status_code": code, "response": parsed}


def create_pending_artifact(
    *,
    zip_path: str,
    summary_json: Optional[dict[str, Any]] = None,
    actor: str = "system",
    notes: Optional[str] = None,
) -> dict:
    p = Path(str(zip_path or "").strip())
    if not p.exists() or not p.is_file():
        raise RuntimeError(f"GTFS zip path does not exist: {p}")

    file_hash = _read_sha256(p)
    token = _new_token(6)
    artifact = create_gtfs_artifact(
        zip_path=str(p),
        file_hash=file_hash,
        summary_json=summary_json or {},
        approval_token=token,
        notes=notes,
    )
    artifact_id = str(artifact.get("artifact_id") or "")
    log_gtfs_audit(
        artifact_id=artifact_id or None,
        action="created",
        actor=str(actor or "system"),
        payload_json={"zip_path": str(p), "file_hash": file_hash, "summary": summary_json or {}},
    )
    if artifact_id:
        _maybe_send_whatsapp_notification(artifact=artifact, actor=actor)
    return artifact


def _maybe_send_whatsapp_notification(*, artifact: dict[str, Any], actor: str) -> None:
    artifact_id = str(artifact.get("artifact_id") or "")
    if not artifact_id:
        return
    if not whatsapp_enabled():
        log_gtfs_audit(
            artifact_id=artifact_id,
            action="whatsapp_skipped",
            actor=str(actor or "system"),
            payload_json={"reason": "WHATSAPP_ENABLED=false"},
        )
        return

    admin_number = str(os.getenv("WHATSAPP_ADMIN_NUMBER", "") or "").strip()
    if not admin_number:
        log_gtfs_audit(
            artifact_id=artifact_id,
            action="whatsapp_skipped",
            actor=str(actor or "system"),
            payload_json={"reason": "WHATSAPP_ADMIN_NUMBER missing"},
        )
        return

    summary = dict(artifact.get("summary_json") or {})
    msg = (
        f"GTFS READY: {artifact_id}\n"
        f"Summary: {_short_summary(summary)}\n"
        f"Reply: APPROVED {artifact.get('approval_token')}"
    )
    notif = create_gtfs_notification(
        artifact_id=artifact_id,
        channel="whatsapp",
        provider="meta_cloud_api",
        to_address=admin_number,
        status="queued",
    )
    notif_id = str(notif.get("notification_id") or "")
    try:
        sent = send_whatsapp_text(to_number=admin_number, text=msg)
        provider_message_id = None
        response = sent.get("response") if isinstance(sent, dict) else {}
        if isinstance(response, dict):
            messages = response.get("messages") or []
            if isinstance(messages, list) and messages:
                provider_message_id = str((messages[0] or {}).get("id") or "") or None
        if notif_id:
            update_gtfs_notification(
                notification_id=notif_id,
                status="sent",
                provider_message_id=provider_message_id,
            )
        log_gtfs_audit(
            artifact_id=artifact_id,
            action="whatsapp_sent",
            actor=str(actor or "system"),
            payload_json={"notification_id": notif_id, "provider_message_id": provider_message_id},
        )
    except Exception as e:
        if notif_id:
            update_gtfs_notification(
                notification_id=notif_id,
                status="failed",
                error_text=str(e),
            )
        log_gtfs_audit(
            artifact_id=artifact_id,
            action="whatsapp_failed",
            actor=str(actor or "system"),
            payload_json={"notification_id": notif_id, "error": str(e)},
        )


def approve_artifact(*, artifact_id: str, actor: str, notes: Optional[str] = None) -> dict:
    row = approve_gtfs_artifact(artifact_id=artifact_id, approved_by=actor, notes=notes)
    if row:
        log_gtfs_audit(
            artifact_id=artifact_id,
            action="approved",
            actor=str(actor or "system"),
            payload_json={"notes": notes},
        )
    return row


def reject_artifact(*, artifact_id: str, actor: str, notes: Optional[str] = None) -> dict:
    row = reject_gtfs_artifact(artifact_id=artifact_id, rejected_by=actor, notes=notes)
    if row:
        log_gtfs_audit(
            artifact_id=artifact_id,
            action="rejected",
            actor=str(actor or "system"),
            payload_json={"notes": notes},
        )
    return row


def mark_artifact_downloaded(*, artifact_id: str, actor: str, notes: Optional[str] = None) -> dict:
    row = mark_gtfs_artifact_downloaded(artifact_id=artifact_id, notes=notes)
    if row:
        log_gtfs_audit(
            artifact_id=artifact_id,
            action="downloaded",
            actor=str(actor or "system"),
            payload_json={"notes": notes},
        )
    return row


def list_artifacts(*, status: Optional[str] = None, limit: int = 200) -> list[dict]:
    return list_gtfs_artifacts(status=status, limit=limit)


def get_artifact(artifact_id: str) -> Optional[dict]:
    return get_gtfs_artifact(artifact_id)


def get_latest_artifact_for_zip(zip_path: str) -> Optional[dict]:
    return get_latest_gtfs_artifact_by_zip_path(zip_path)


def get_artifact_download_if_allowed(artifact_id: str) -> dict:
    row = get_gtfs_artifact(artifact_id)
    if not row:
        raise RuntimeError("Artifact not found.")
    status = str(row.get("status") or "").strip().lower()
    if status not in {"approved", "downloaded"}:
        raise PermissionError(f"Artifact status `{status}` is not downloadable.")
    zip_path = Path(str(row.get("zip_path") or "").strip())
    if not zip_path.exists() or not zip_path.is_file():
        raise RuntimeError(f"Artifact ZIP not found: {zip_path}")
    return {"artifact": row, "path": zip_path}


def list_artifact_notifications(*, artifact_id: Optional[str] = None, limit: int = 100) -> list[dict]:
    return list_gtfs_notifications(artifact_id=artifact_id, limit=limit)


def list_artifact_audit(*, artifact_id: Optional[str] = None, limit: int = 200) -> list[dict]:
    return list_gtfs_audit_log(artifact_id=artifact_id, limit=limit)


def verify_whatsapp_challenge(*, mode: str, token: str, challenge: str) -> Optional[str]:
    verify_token = str(os.getenv("META_WA_VERIFY_TOKEN", "") or "").strip()
    if mode == "subscribe" and verify_token and token == verify_token:
        return str(challenge or "")
    return None


def process_whatsapp_inbound(payload: dict[str, Any]) -> dict[str, Any]:
    admin_number = str(os.getenv("WHATSAPP_ADMIN_NUMBER", "") or "").strip()
    admin_normalized = _normalize_phone(admin_number)
    processed = 0
    approved = 0
    ignored = 0

    entries = payload.get("entry") or []
    for entry in entries if isinstance(entries, list) else []:
        changes = (entry or {}).get("changes") or []
        for change in changes if isinstance(changes, list) else []:
            value = (change or {}).get("value") or {}
            messages = value.get("messages") or []
            for msg in messages if isinstance(messages, list) else []:
                processed += 1
                sender = str(msg.get("from") or "").strip()
                text_body = str((((msg.get("text") or {}) or {}).get("body") or "")).strip()
                actor = f"whatsapp:{sender or 'unknown'}"
                log_gtfs_audit(
                    artifact_id=None,
                    action="whatsapp_received",
                    actor=actor,
                    payload_json={"text": text_body},
                )

                sender_normalized = _normalize_phone(sender)
                if not sender or (admin_normalized and sender_normalized != admin_normalized):
                    ignored += 1
                    log_gtfs_audit(
                        artifact_id=None,
                        action="whatsapp_ignored",
                        actor=actor,
                        payload_json={
                            "reason": "sender_not_authorized",
                            "sender": sender,
                            "sender_normalized": sender_normalized,
                        },
                    )
                    continue

                match = APPROVAL_RE.match(text_body or "")
                if not match:
                    ignored += 1
                    log_gtfs_audit(
                        artifact_id=None,
                        action="whatsapp_ignored",
                        actor=actor,
                        payload_json={"reason": "invalid_format"},
                    )
                    continue

                token = str(match.group(1) or "").strip().upper()
                artifact = get_gtfs_artifact_by_token(approval_token=token, pending_only=True)
                if not artifact:
                    ignored += 1
                    log_gtfs_audit(
                        artifact_id=None,
                        action="whatsapp_ignored",
                        actor=actor,
                        payload_json={"reason": "token_not_found_or_not_pending", "token": token},
                    )
                    continue

                artifact_id = str(artifact.get("artifact_id") or "")
                updated = approve_artifact(artifact_id=artifact_id, actor=actor, notes="Approved via WhatsApp.")
                if updated:
                    approved += 1
                else:
                    ignored += 1
                    log_gtfs_audit(
                        artifact_id=artifact_id,
                        action="whatsapp_ignored",
                        actor=actor,
                        payload_json={"reason": "approval_update_failed"},
                    )
    return {"ok": True, "processed": processed, "approved": approved, "ignored": ignored}
