from __future__ import annotations

import json
import os
import subprocess
import traceback
from pathlib import Path
from typing import Any, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from datamind_console.db.ai_repo import (
    create_ai_escalation,
    get_ai_escalation,
    update_ai_escalation,
)


def _env_bool(key: str, default: bool = False) -> bool:
    value = str(os.getenv(key, "") or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def escalation_enabled() -> bool:
    return _env_bool("AI_ESCALATION_ENABLED", False)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_cmd(args: list[str]) -> str:
    try:
        out = subprocess.check_output(args, cwd=_repo_root(), stderr=subprocess.DEVNULL, text=True)
        return str(out or "").strip()
    except Exception:
        return ""


def _git_context() -> dict[str, Any]:
    head = _run_cmd(["git", "rev-parse", "--short", "HEAD"])
    status_raw = _run_cmd(["git", "status", "--short"])
    changed_files: list[str] = []
    if status_raw:
        for line in status_raw.splitlines()[:200]:
            path = line[3:].strip() if len(line) >= 4 else line.strip()
            if path:
                changed_files.append(path)
    return {"commit": head or None, "changed_files": changed_files}


def send_to_codex(payload: dict[str, Any]) -> dict[str, Any]:
    api_url = str(os.getenv("CODEX_API_URL", "") or "").strip()
    if not api_url:
        raise RuntimeError("CODEX_API_URL is not configured.")
    api_key = str(os.getenv("CODEX_API_KEY", "") or "").strip()

    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(api_url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("X-API-Key", api_key)

    try:
        with urlrequest.urlopen(req, timeout=35) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            status = int(resp.getcode() or 200)
    except urlerror.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        raise RuntimeError(f"Codex API HTTP error {int(e.code)}: {body[:1200]}") from e
    except Exception as e:
        raise RuntimeError(f"Codex API request failed: {e}") from e

    if "application/json" in ctype:
        try:
            parsed = json.loads(body or "{}")
            if isinstance(parsed, dict):
                return {"status_code": status, "response": parsed}
            return {"status_code": status, "response": {"data": parsed}}
        except Exception:
            return {"status_code": status, "response": {"raw_text": body}}
    return {"status_code": status, "response": {"raw_text": body}}


def _build_payload(
    *,
    error_type: str,
    error_message: str,
    stacktrace_text: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    codex_model = str(os.getenv("CODEX_MODEL", "") or "").strip() or None
    git = _git_context()
    modules = sorted(set([str(x) for x in (context.get("module_names") or []) if str(x).strip()]))[:50]
    return {
        "model": codex_model,
        "task_type": "runtime_error_escalation",
        "prompt": (
            "Diagnose the likely root cause and propose patch steps. "
            "Do not auto-apply code changes. Return actionable text only."
        ),
        "error": {
            "type": str(error_type or "RuntimeError"),
            "message": str(error_message or ""),
            "stacktrace": str(stacktrace_text or ""),
        },
        "runtime_context": context,
        "repo_context": {
            "git_commit": git.get("commit"),
            "changed_files": git.get("changed_files") or [],
            "module_pointers": modules,
        },
        "expected_output": {
            "format": "markdown_or_json",
            "include": [
                "root_cause_hypothesis",
                "debug_checks",
                "patch_plan",
                "risk_notes",
            ],
        },
    }


def escalate_worker_exception(
    *,
    exc: Exception,
    context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    ctx = dict(context or {})
    error_type = exc.__class__.__name__
    error_message = str(exc)
    stacktrace_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    row = create_ai_escalation(
        error_type=error_type,
        error_message=error_message,
        stacktrace=stacktrace_text,
        context=ctx,
        status="open",
    )
    escalation_id = str(row.get("escalation_id") or "")
    if not escalation_id:
        return {"ok": False, "error": "Could not create escalation row."}

    if not escalation_enabled():
        return {"ok": True, "escalation_id": escalation_id, "status": "open", "sent": False}

    payload = _build_payload(
        error_type=error_type,
        error_message=error_message,
        stacktrace_text=stacktrace_text,
        context=ctx,
    )

    update_ai_escalation(escalation_id=escalation_id, codex_request=payload)
    try:
        response = send_to_codex(payload)
        updated = update_ai_escalation(
            escalation_id=escalation_id,
            status="sent",
            codex_response=response,
        )
        return {"ok": True, "escalation_id": escalation_id, "status": updated.get("status"), "sent": True}
    except Exception as send_error:
        updated = update_ai_escalation(
            escalation_id=escalation_id,
            status="failed",
            resolution_notes=str(send_error),
        )
        return {
            "ok": False,
            "escalation_id": escalation_id,
            "status": updated.get("status") or "failed",
            "error": str(send_error),
        }


def retry_send_escalation(escalation_id: str) -> dict[str, Any]:
    row = get_ai_escalation(escalation_id)
    if not row:
        raise ValueError(f"Escalation not found: {escalation_id}")

    context = dict(row.get("context") or {})
    payload = dict(row.get("codex_request") or {})
    if not payload:
        payload = _build_payload(
            error_type=str(row.get("error_type") or "RuntimeError"),
            error_message=str(row.get("error_message") or ""),
            stacktrace_text=str(row.get("stacktrace") or ""),
            context=context,
        )

    if not escalation_enabled():
        raise RuntimeError("AI_ESCALATION_ENABLED is false.")

    update_ai_escalation(escalation_id=escalation_id, codex_request=payload)
    try:
        response = send_to_codex(payload)
        return update_ai_escalation(
            escalation_id=escalation_id,
            status="sent",
            codex_response=response,
            resolution_notes=None,
        )
    except Exception as e:
        return update_ai_escalation(
            escalation_id=escalation_id,
            status="failed",
            resolution_notes=str(e),
        )
