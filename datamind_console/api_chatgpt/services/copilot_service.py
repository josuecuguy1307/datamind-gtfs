from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from uuid import uuid4

from datamind_console.api_chatgpt.services.advisory_service import AdvisoryError, AdvisoryService
from datamind_console.db.db import db_conn, fetch_all, fetch_one


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _safe_json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _safe_json_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for row in value:
        if isinstance(row, dict):
            out.append(dict(row))
    return out


def _shorten(text: str, max_len: int) -> str:
    raw = str(text or "")
    if len(raw) <= int(max_len):
        return raw
    return raw[: max(0, int(max_len) - 3)] + "..."


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(default)


class CopilotSessionStore:
    _file_lock = threading.Lock()

    def __init__(
        self,
        *,
        schema: str = "console",
        db_enabled: Optional[bool] = None,
        data_dir: Optional[str] = None,
    ) -> None:
        self.schema = str(schema or "console")
        self.t_sessions = f"{self.schema}.pipeline_copilot_sessions"
        self.t_messages = f"{self.schema}.pipeline_copilot_messages"
        self.db_enabled = _env_bool("DATAMIND_COPILOT_DB_ENABLED", True) if db_enabled is None else bool(db_enabled)
        self._db_checked = False
        self._db_available = False
        base = (
            Path(data_dir).expanduser().resolve()
            if data_dir
            else (Path(__file__).resolve().parents[2] / "orchestrator_logs" / "pipeline_copilot")
        )
        self.base_dir = base
        self.sessions_dir = self.base_dir / "sessions"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    @property
    def db_available(self) -> bool:
        if not self.db_enabled:
            return False
        if self._db_checked:
            return bool(self._db_available)
        self._db_checked = True
        self._db_available = self._check_tables_exist()
        return bool(self._db_available)

    def _check_tables_exist(self) -> bool:
        try:
            with db_conn(readonly=True) as conn:
                row = fetch_one(
                    conn,
                    "SELECT to_regclass(%s) IS NOT NULL AS sessions_ok, to_regclass(%s) IS NOT NULL AS messages_ok",
                    (self.t_sessions, self.t_messages),
                )
            return bool((row or {}).get("sessions_ok")) and bool((row or {}).get("messages_ok"))
        except Exception:
            return False

    def create_session(
        self,
        *,
        title: str,
        mode: str,
        created_by: Optional[str],
        context_defaults: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        session = {
            "session_id": f"copilot_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:10]}",
            "title": _shorten(str(title or "Pipeline Copilot Session"), 180),
            "mode": str(mode or "pipeline_operator"),
            "status": "active",
            "pinned": False,
            "created_by": (str(created_by).strip() if created_by else None),
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
            "context_defaults": _safe_json_obj(context_defaults),
            "metadata": {},
        }
        if self.db_available:
            try:
                return self._db_create_session(session)
            except Exception:
                pass
        self._file_write_session(session=session, messages=[])
        return session

    def list_sessions(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        lim = max(1, min(int(limit), 300))
        if self.db_available:
            try:
                return self._db_list_sessions(limit=lim)
            except Exception:
                pass
        rows: List[Dict[str, Any]] = []
        for path in self.sessions_dir.glob("*.json"):
            payload = self._file_load_payload(path)
            if not payload:
                continue
            sess = _safe_json_obj(payload.get("session"))
            if not sess:
                continue
            rows.append(sess)
        rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        return rows[:lim]

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        if self.db_available:
            try:
                return self._db_get_session(sid)
            except Exception:
                pass
        payload = self._file_load_payload(self._file_session_path(sid))
        if not payload:
            return None
        return _safe_json_obj(payload.get("session"))

    def list_messages(self, session_id: str, *, limit: int = 500) -> List[Dict[str, Any]]:
        sid = str(session_id or "").strip()
        if not sid:
            return []
        lim = max(1, min(int(limit), 5000))
        if self.db_available:
            try:
                return self._db_list_messages(sid, limit=lim)
            except Exception:
                pass
        payload = self._file_load_payload(self._file_session_path(sid))
        if not payload:
            return []
        rows = _safe_json_list(payload.get("messages"))
        rows.sort(key=lambda row: str(row.get("created_at") or ""))
        if lim > 0 and len(rows) > lim:
            rows = rows[-lim:]
        return rows

    def append_message(self, session_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required")
        row = dict(message or {})
        row.setdefault("message_id", str(uuid4()))
        row.setdefault("session_id", sid)
        row.setdefault("role", "assistant")
        row.setdefault("content", "")
        row.setdefault("created_at", _utc_now_iso())
        row.setdefault("context_payload", {})
        row.setdefault("request_payload", {})
        row.setdefault("response_payload", {})
        row.setdefault("token_usage", {})

        if self.db_available:
            try:
                out = self._db_insert_message(row)
                self._db_touch_session(sid)
                return out
            except Exception:
                pass

        path = self._file_session_path(sid)
        with self._file_lock:
            payload = self._file_load_payload(path) or {"session": {"session_id": sid}, "messages": []}
            messages = _safe_json_list(payload.get("messages"))
            messages.append(row)
            session = _safe_json_obj(payload.get("session"))
            session.setdefault("session_id", sid)
            session.setdefault("title", "Pipeline Copilot Session")
            session.setdefault("mode", "pipeline_operator")
            session.setdefault("status", "active")
            session.setdefault("created_at", row["created_at"])
            session["updated_at"] = _utc_now_iso()
            payload = {"session": session, "messages": messages}
            self._file_write_payload(path, payload)
        return row

    # -----------------------------
    # DB implementation
    # -----------------------------
    def _db_create_session(self, row: Dict[str, Any]) -> Dict[str, Any]:
        with db_conn() as conn:
            db_row = fetch_one(
                conn,
                f"""
                INSERT INTO {self.t_sessions}
                  (session_id, title, mode, status, pinned, created_by, context_defaults, metadata_json, created_at, updated_at)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::timestamptz, %s::timestamptz)
                ON CONFLICT (session_id) DO UPDATE SET
                  title = EXCLUDED.title,
                  mode = EXCLUDED.mode,
                  status = EXCLUDED.status,
                  pinned = EXCLUDED.pinned,
                  created_by = EXCLUDED.created_by,
                  context_defaults = EXCLUDED.context_defaults,
                  metadata_json = EXCLUDED.metadata_json,
                  updated_at = EXCLUDED.updated_at
                RETURNING
                  session_id, title, mode, status, pinned, created_by,
                  context_defaults, metadata_json, created_at, updated_at
                """,
                (
                    row["session_id"],
                    row["title"],
                    row["mode"],
                    row["status"],
                    bool(row.get("pinned", False)),
                    row.get("created_by"),
                    _to_json(row.get("context_defaults") or {}),
                    _to_json(row.get("metadata") or {}),
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        out = {
            "session_id": str((db_row or {}).get("session_id") or row["session_id"]),
            "title": str((db_row or {}).get("title") or row["title"]),
            "mode": str((db_row or {}).get("mode") or row["mode"]),
            "status": str((db_row or {}).get("status") or row["status"]),
            "pinned": bool((db_row or {}).get("pinned") if (db_row or {}).get("pinned") is not None else row.get("pinned", False)),
            "created_by": (db_row or {}).get("created_by") or row.get("created_by"),
            "context_defaults": _safe_json_obj((db_row or {}).get("context_defaults") or row.get("context_defaults")),
            "metadata": _safe_json_obj((db_row or {}).get("metadata_json") or row.get("metadata")),
            "created_at": str((db_row or {}).get("created_at") or row["created_at"]),
            "updated_at": str((db_row or {}).get("updated_at") or row["updated_at"]),
        }
        return out

    def _db_list_sessions(self, *, limit: int) -> List[Dict[str, Any]]:
        with db_conn(readonly=True) as conn:
            rows = fetch_all(
                conn,
                f"""
                SELECT
                  session_id, title, mode, status, pinned, created_by,
                  context_defaults, metadata_json, created_at, updated_at
                FROM {self.t_sessions}
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (int(limit),),
            )
        out: List[Dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "session_id": str(row.get("session_id") or ""),
                    "title": str(row.get("title") or "Pipeline Copilot Session"),
                    "mode": str(row.get("mode") or "pipeline_operator"),
                    "status": str(row.get("status") or "active"),
                    "pinned": bool(row.get("pinned", False)),
                    "created_by": row.get("created_by"),
                    "context_defaults": _safe_json_obj(row.get("context_defaults")),
                    "metadata": _safe_json_obj(row.get("metadata_json")),
                    "created_at": str(row.get("created_at") or ""),
                    "updated_at": str(row.get("updated_at") or ""),
                }
            )
        return out

    def _db_get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with db_conn(readonly=True) as conn:
            row = fetch_one(
                conn,
                f"""
                SELECT
                  session_id, title, mode, status, pinned, created_by,
                  context_defaults, metadata_json, created_at, updated_at
                FROM {self.t_sessions}
                WHERE session_id = %s
                """,
                (str(session_id),),
            )
        if not row:
            return None
        return {
            "session_id": str(row.get("session_id") or ""),
            "title": str(row.get("title") or "Pipeline Copilot Session"),
            "mode": str(row.get("mode") or "pipeline_operator"),
            "status": str(row.get("status") or "active"),
            "pinned": bool(row.get("pinned", False)),
            "created_by": row.get("created_by"),
            "context_defaults": _safe_json_obj(row.get("context_defaults")),
            "metadata": _safe_json_obj(row.get("metadata_json")),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }

    def _db_insert_message(self, row: Dict[str, Any]) -> Dict[str, Any]:
        with db_conn() as conn:
            db_row = fetch_one(
                conn,
                f"""
                INSERT INTO {self.t_messages}
                  (message_id, session_id, role, content, task, model, latency_ms, token_usage,
                   event_type, phase, step_id, run_id, context_payload, request_payload,
                   response_payload, error_text, trace_id, created_at)
                VALUES
                  (%s::uuid, %s, %s, %s, %s, %s, %s, %s::jsonb,
                   %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                   %s::jsonb, %s, %s, %s::timestamptz)
                RETURNING
                  message_id::text AS message_id, session_id, role, content, task, model, latency_ms, token_usage,
                  event_type, phase, step_id, run_id, context_payload, request_payload, response_payload,
                  error_text, trace_id, created_at
                """,
                (
                    str(row.get("message_id") or str(uuid4())),
                    str(row.get("session_id") or ""),
                    str(row.get("role") or "assistant"),
                    str(row.get("content") or ""),
                    (str(row.get("task")) if row.get("task") else None),
                    (str(row.get("model")) if row.get("model") else None),
                    (int(row.get("latency_ms")) if row.get("latency_ms") is not None else None),
                    _to_json(row.get("token_usage") or {}),
                    (str(row.get("event_type")) if row.get("event_type") else None),
                    (str(row.get("phase")) if row.get("phase") else None),
                    (str(row.get("step_id")) if row.get("step_id") else None),
                    (str(row.get("run_id")) if row.get("run_id") else None),
                    _to_json(row.get("context_payload") or {}),
                    _to_json(row.get("request_payload") or {}),
                    _to_json(row.get("response_payload") or {}),
                    (str(row.get("error_text")) if row.get("error_text") else None),
                    (str(row.get("trace_id")) if row.get("trace_id") else None),
                    str(row.get("created_at") or _utc_now_iso()),
                ),
            )
        out = dict(db_row or {})
        out["token_usage"] = _safe_json_obj(out.get("token_usage"))
        out["context_payload"] = _safe_json_obj(out.get("context_payload"))
        out["request_payload"] = _safe_json_obj(out.get("request_payload"))
        out["response_payload"] = _safe_json_obj(out.get("response_payload"))
        out["created_at"] = str(out.get("created_at") or row.get("created_at") or _utc_now_iso())
        return out

    def _db_list_messages(self, session_id: str, *, limit: int) -> List[Dict[str, Any]]:
        with db_conn(readonly=True) as conn:
            rows = fetch_all(
                conn,
                f"""
                SELECT
                  message_id::text AS message_id, session_id, role, content, task, model, latency_ms, token_usage,
                  event_type, phase, step_id, run_id, context_payload, request_payload, response_payload,
                  error_text, trace_id, created_at
                FROM {self.t_messages}
                WHERE session_id = %s
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (str(session_id), int(limit)),
            )
        out: List[Dict[str, Any]] = []
        for row in rows:
            rec = dict(row)
            rec["token_usage"] = _safe_json_obj(rec.get("token_usage"))
            rec["context_payload"] = _safe_json_obj(rec.get("context_payload"))
            rec["request_payload"] = _safe_json_obj(rec.get("request_payload"))
            rec["response_payload"] = _safe_json_obj(rec.get("response_payload"))
            rec["created_at"] = str(rec.get("created_at") or "")
            out.append(rec)
        return out

    def _db_touch_session(self, session_id: str) -> None:
        with db_conn() as conn:
            fetch_one(
                conn,
                f"UPDATE {self.t_sessions} SET updated_at = NOW() WHERE session_id = %s RETURNING session_id",
                (str(session_id),),
            )

    # -----------------------------
    # JSON fallback implementation
    # -----------------------------
    def _file_session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{str(session_id)}.json"

    def _file_write_session(self, *, session: Dict[str, Any], messages: List[Dict[str, Any]]) -> None:
        path = self._file_session_path(str(session.get("session_id") or ""))
        payload = {"session": dict(session), "messages": list(messages)}
        with self._file_lock:
            self._file_write_payload(path, payload)

    def _file_write_payload(self, path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _file_load_payload(path: Path) -> Optional[Dict[str, Any]]:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        return data


class PipelineCopilotService:
    _TEMPLATE_TO_TASK = {
        "explain_step20_blocker": "hades_pipeline_interpreter",
        "prioritize_unmatched_ambiguous": "hades_pipeline_interpreter",
        "review_phase2_dedup_risk": "hades_pipeline_interpreter",
        "interpret_extraction_regression": "hades_pipeline_interpreter",
        "summarize_merge_evidence": "hades_pipeline_interpreter",
        "draft_codex_patch_task": "hades_patch_task_generator",
        "hades_patch_task_generator": "hades_patch_task_generator",
        "compare_retest_outcome": "hades_retest_comparator",
        "hades_retest_comparator": "hades_retest_comparator",
        # Explicit legacy templates (opt-in only).
        "legacy_interpret_pipeline_blocker": "interpret_pipeline_blocker",
        "legacy_prioritize_pipeline_resolution": "prioritize_pipeline_resolution",
        "legacy_generate_codex_patch_task": "generate_codex_patch_task",
    }

    def __init__(
        self,
        *,
        advisory_service: Optional[AdvisoryService] = None,
        store: Optional[CopilotSessionStore] = None,
    ) -> None:
        self.advisory_service = advisory_service or AdvisoryService()
        self.store = store or CopilotSessionStore(data_dir=os.getenv("DATAMIND_COPILOT_DATA_DIR"))
        self.autopilot_runs_dir = Path(__file__).resolve().parents[2] / "orchestrator_logs" / "autopilot_runs"

    def create_session(
        self,
        *,
        title: Optional[str] = None,
        mode: str = "pipeline_operator",
        created_by: Optional[str] = None,
        context_defaults: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.store.create_session(
            title=str(title or "Pipeline Copilot Session"),
            mode=str(mode or "pipeline_operator"),
            created_by=created_by,
            context_defaults=context_defaults,
        )

    def list_sessions(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        return self.store.list_sessions(limit=limit)

    def list_messages(self, *, session_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        return self.store.list_messages(session_id=session_id, limit=limit)

    def chat(
        self,
        *,
        session_id: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        template: Optional[str] = None,
        operator_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        sid = str(session_id or "").strip()
        text = str(message or "").strip()
        if not sid:
            raise ValueError("session_id is required")
        if not text:
            raise ValueError("message cannot be empty")
        if self.store.get_session(sid) is None:
            raise KeyError(f"session not found: {sid}")

        ctx = _safe_json_obj(context)
        operator = _safe_json_obj(operator_context)
        task = self._select_task(template=template, message=text, context=ctx)
        trace_id = str(uuid4())

        user_msg = self.store.append_message(
            sid,
            {
                "message_id": str(uuid4()),
                "session_id": sid,
                "role": "user",
                "content": text,
                "task": task,
                "event_type": "chat_user_message",
                "phase": (str(ctx.get("active_phase")) if ctx.get("active_phase") else None),
                "step_id": (str(ctx.get("active_step")) if ctx.get("active_step") else None),
                "run_id": (str(ctx.get("run_id")) if ctx.get("run_id") else None),
                "context_payload": ctx,
                "request_payload": {"template": template},
                "response_payload": {},
                "trace_id": trace_id,
                "created_at": _utc_now_iso(),
            },
        )

        conversation_tail = self._conversation_tail(session_id=sid, limit=8)
        snapshot = self._build_snapshot(task=task, context=ctx, conversation_tail=conversation_tail)
        envelope = {
            "task": task,
            "snapshot": snapshot,
            "operator_context": {
                "mode": "pipeline_copilot",
                "session_id": sid,
                "template": str(template or ""),
                "user_message": text,
                "active_phase": ctx.get("active_phase"),
                "active_step": ctx.get("active_step"),
                "run_id": ctx.get("run_id"),
                "entity_ids": _safe_json_obj(ctx.get("entity_ids")),
                "conversation_tail": conversation_tail,
                "operator": operator,
            },
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
        }

        try:
            detailed = self.advisory_service.run_task_detailed(endpoint_task=task, envelope=envelope)
            response_payload = _safe_json_obj(detailed.get("response"))
            meta = _safe_json_obj(detailed.get("meta"))
            assistant_text = self._render_assistant_markdown(task=task, payload=response_payload)

            assistant_msg = self.store.append_message(
                sid,
                {
                    "message_id": str(uuid4()),
                    "session_id": sid,
                    "role": "assistant",
                    "content": assistant_text,
                    "task": task,
                    "model": meta.get("model"),
                    "latency_ms": meta.get("latency_ms"),
                    "token_usage": _safe_json_obj(meta.get("token_usage")),
                    "event_type": "chat_assistant_message",
                    "phase": (str(ctx.get("active_phase")) if ctx.get("active_phase") else None),
                    "step_id": (str(ctx.get("active_step")) if ctx.get("active_step") else None),
                    "run_id": (str(ctx.get("run_id")) if ctx.get("run_id") else None),
                    "context_payload": ctx,
                    "request_payload": {
                        "template": template,
                        "task": task,
                    },
                    "response_payload": response_payload,
                    "trace_id": trace_id,
                    "created_at": _utc_now_iso(),
                },
            )

            return {
                "session_id": sid,
                "task": task,
                "trace_id": trace_id,
                "user_message": user_msg,
                "assistant_message": assistant_msg,
                "assistant_text": assistant_text,
                "assistant_payload": response_payload,
                "meta": meta,
                "advisory_only": True,
            }
        except AdvisoryError as exc:
            error_text = f"{exc.message} | " + "; ".join(list(exc.errors or []))
            self.store.append_message(
                sid,
                {
                    "message_id": str(uuid4()),
                    "session_id": sid,
                    "role": "assistant",
                    "content": f"Copilot advisory error: {error_text}",
                    "task": task,
                    "event_type": "chat_assistant_error",
                    "phase": (str(ctx.get("active_phase")) if ctx.get("active_phase") else None),
                    "step_id": (str(ctx.get("active_step")) if ctx.get("active_step") else None),
                    "run_id": (str(ctx.get("run_id")) if ctx.get("run_id") else None),
                    "context_payload": ctx,
                    "request_payload": {"template": template, "task": task},
                    "response_payload": {},
                    "error_text": error_text,
                    "trace_id": trace_id,
                    "created_at": _utc_now_iso(),
                },
            )
            raise

    @staticmethod
    def iter_text_chunks(text: str, *, chunk_size: int = 140) -> Iterable[str]:
        raw = str(text or "")
        if not raw:
            return []
        out: List[str] = []
        start = 0
        size = max(32, int(chunk_size))
        while start < len(raw):
            end = min(len(raw), start + size)
            out.append(raw[start:end])
            start = end
        return out

    def _select_task(self, *, template: Optional[str], message: str, context: Dict[str, Any]) -> str:
        tpl = str(template or "").strip().lower()
        if tpl in self._TEMPLATE_TO_TASK:
            return str(self._TEMPLATE_TO_TASK[tpl])

        phase = str(context.get("active_phase") or "").strip().lower()
        step = str(context.get("active_step") or "").strip().lower()
        lower = str(message or "").strip().lower()
        pipeline_context_present = bool(phase or step or str(context.get("run_id") or "").strip())

        if any(
            x in lower
            for x in [
                "retest compare",
                "compare retest",
                "retest comparator",
                "retest outcome",
                "baseline vs retest",
                "before after compare",
            ]
        ):
            return "hades_retest_comparator"
        if any(x in lower for x in ["codex", "patch task", "patch", "fix prompt"]):
            return "hades_patch_task_generator"
        if any(x in lower for x in ["consistency checker", "contradiction check", "consistency only"]):
            return "hades_evidence_consistency_checker"
        if any(x in lower for x in ["legacy blocker template", "legacy pipeline blocker", "legacy route"]):
            return "interpret_pipeline_blocker"
        if any(x in lower for x in ["legacy prioritize", "legacy resolution template"]):
            return "prioritize_pipeline_resolution"
        if pipeline_context_present or any(
            x in lower
            for x in [
                "merge",
                "opposite direction",
                "bind",
                "cleanup",
                "dedup",
                "global normalize",
                "unmatched",
                "ambiguous",
                "prioritize",
                "triage",
                "block",
                "blocker",
                "gate fail",
                "step20",
                "quality",
                "telemetry",
                "ai bot",
                "phase 1",
                "phase 2",
                "phase 3",
            ]
        ):
            return "hades_pipeline_interpreter"
        return "hades_pipeline_interpreter"

    def _conversation_tail(self, *, session_id: str, limit: int) -> List[Dict[str, str]]:
        messages = self.store.list_messages(session_id=session_id, limit=max(2, int(limit)))
        out: List[Dict[str, str]] = []
        for row in messages[-limit:]:
            role = str(row.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            content = _shorten(str(row.get("content") or ""), 1200)
            out.append({"role": role, "content": content})
        return out

    def _build_snapshot(
        self,
        *,
        task: str,
        context: Dict[str, Any],
        conversation_tail: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        toggles = _safe_json_obj(context.get("toggles"))
        include_logs = bool(toggles.get("include_logs", True))
        include_metrics = bool(toggles.get("include_metrics", True))
        include_block_reason = bool(toggles.get("include_block_reason", True))
        include_artifacts = bool(toggles.get("include_artifacts_summary", True))
        include_compare = bool(toggles.get("include_previous_runs_compare", False))

        run_id = str(context.get("run_id") or "").strip()
        run_state = self._load_run_state(run_id) if run_id else {}
        latest_record = self._latest_record(run_state)

        active_phase = str(context.get("active_phase") or run_state.get("current_phase") or "").strip()
        active_step = str(context.get("active_step") or run_state.get("current_step_id") or "").strip()
        validator_status = str(
            context.get("validator_status")
            or (latest_record.get("validator_result") or {}).get("status")
            or ""
        ).strip()

        block_reason = _safe_json_obj(context.get("block_reason"))
        if not block_reason:
            block_reason = _safe_json_obj((latest_record.get("block_reason") or {}))
        ai_bot_snapshot = _safe_json_obj(context.get("ai_bot_snapshot"))
        if not ai_bot_snapshot:
            ai_bot_snapshot = _safe_json_obj((latest_record.get("ai_bot_snapshot") or {}))
        approvals = _safe_json_list(context.get("approval_items"))
        if not approvals:
            approvals = _safe_json_list(run_state.get("approvals"))
        artifacts_summary = _safe_json_list(context.get("artifacts_summary"))
        if not artifacts_summary:
            artifacts_summary = self._artifact_summary(run_state)
        logs = _safe_json_list(context.get("logs"))
        if not logs:
            logs = self._event_excerpt(run_state, limit=30)
        previous_compare = _safe_json_obj(context.get("previous_runs_compare"))

        evidence_refs: List[Dict[str, Any]] = []
        if run_id:
            evidence_refs.append({"evidence_id": "ev_run_id", "kind": "run_ref", "description": f"Run {run_id}"})
        if include_block_reason and block_reason:
            evidence_refs.append({"evidence_id": "ev_block_reason", "kind": "validator_block", "description": "Structured block reason"})
        if include_metrics and ai_bot_snapshot:
            evidence_refs.append({"evidence_id": "ev_ai_metrics", "kind": "ai_bot_metrics", "description": "AI Bot telemetry snapshot"})
        if approvals:
            evidence_refs.append({"evidence_id": "ev_approvals", "kind": "approval_queue", "description": "Approval queue context"})
        if include_artifacts and artifacts_summary:
            evidence_refs.append({"evidence_id": "ev_artifacts", "kind": "artifacts", "description": "Recent artifacts summary"})
        if include_logs and logs:
            evidence_refs.append({"evidence_id": "ev_logs", "kind": "event_log_excerpt", "description": "Recent pipeline events"})
        if include_compare and previous_compare:
            evidence_refs.append({"evidence_id": "ev_compare", "kind": "run_compare", "description": "Comparison vs previous runs"})

        insufficient_flags: List[str] = []
        if not run_id:
            insufficient_flags.append("run_context_missing")
        if include_block_reason and not block_reason:
            insufficient_flags.append("block_reason_missing")
        if include_metrics and not ai_bot_snapshot:
            insufficient_flags.append("ai_bot_metrics_missing")
        if include_logs and not logs:
            insufficient_flags.append("logs_missing")

        return {
            "snapshot_id": f"snap_copilot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            "task": task,
            "snapshot_source": "pipeline_copilot_context",
            "generated_at": _utc_now_iso(),
            "context": {
                "phase": active_phase,
                "step": active_step,
                "run_id": run_id or None,
                "validator_status": validator_status or None,
                "entity_ids": _safe_json_obj(context.get("entity_ids")),
            },
            "block_reason": (block_reason if include_block_reason else {}),
            "ai_bot_snapshot": (ai_bot_snapshot if include_metrics else {}),
            "approval_items": approvals[:20],
            "artifacts_summary": artifacts_summary[:40] if include_artifacts else [],
            "logs_excerpt": logs[:30] if include_logs else [],
            "previous_runs_compare": previous_compare if include_compare else {},
            "conversation_tail": conversation_tail[-8:],
            "insufficient_data_flags": insufficient_flags,
            "evidence_refs": evidence_refs,
        }

    def _load_run_state(self, run_id: str) -> Dict[str, Any]:
        if not run_id:
            return {}
        path = self.autopilot_runs_dir / f"{run_id}.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    @staticmethod
    def _latest_record(run_state: Dict[str, Any]) -> Dict[str, Any]:
        rows = run_state.get("step_execution_records")
        if not isinstance(rows, list) or not rows:
            return {}
        for row in reversed(rows):
            if isinstance(row, dict):
                return row
        return {}

    @staticmethod
    def _artifact_summary(run_state: Dict[str, Any]) -> List[Dict[str, Any]]:
        artifacts = _safe_json_obj(run_state.get("artifacts"))
        out: List[Dict[str, Any]] = []
        for step_id, items in artifacts.items():
            if not isinstance(items, list):
                continue
            out.append(
                {
                    "step_id": str(step_id),
                    "artifact_count": len(items),
                }
            )
        out.sort(key=lambda row: str(row.get("step_id") or ""))
        return out

    @staticmethod
    def _event_excerpt(run_state: Dict[str, Any], *, limit: int) -> List[Dict[str, Any]]:
        events = run_state.get("events")
        if not isinstance(events, list):
            return []
        out: List[Dict[str, Any]] = []
        for row in events[-max(1, int(limit)) :]:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "event_type": row.get("event_type"),
                    "timestamp": row.get("timestamp"),
                    "phase": row.get("phase"),
                    "step_id": row.get("step_id"),
                    "payload": _safe_json_obj(row.get("payload")),
                }
            )
        return out

    @staticmethod
    def _render_assistant_markdown(*, task: str, payload: Dict[str, Any]) -> str:
        summary = str(payload.get("summary") or "").strip() or "No summary available."
        lines: List[str] = [summary, ""]

        confidence = _safe_json_obj(payload.get("confidence"))
        if confidence:
            band = str(confidence.get("band") or "unknown")
            score = confidence.get("score")
            lines.append(f"Confidence: `{band}` ({score})")
        elif task == "hades_pipeline_interpreter":
            score = payload.get("confidence")
            if score is not None:
                lines.append(f"Confidence: `{score}`")
        elif task == "hades_retest_comparator":
            score = payload.get("confidence")
            if score is not None:
                lines.append(f"Confidence: `{score}`")

        if task == "hades_pipeline_interpreter":
            cause = str(payload.get("dominant_cause_class") or "").strip()
            branch = str(payload.get("recommended_branch") or "").strip()
            if cause:
                lines.append(f"Dominant cause: `{cause}`")
            if branch:
                lines.append(f"Recommended branch: `{branch}`")
            consistency = _safe_json_obj(payload.get("evidence_consistency_checks"))
            contradictions = consistency.get("contradictions")
            if isinstance(contradictions, list):
                lines.append(f"Contradictions detected: `{len(contradictions)}`")
            patch = _safe_json_obj(payload.get("patch_task_recommendation"))
            if patch:
                if bool(patch.get("should_create_patch_task")):
                    lines.append(
                        "Patch task recommendation: "
                        f"`{patch.get('patch_type')}` via `{patch.get('suggested_target')}`"
                    )
                else:
                    lines.append("Patch task recommendation: none")
            next_actions = payload.get("recommended_next_actions")
            if isinstance(next_actions, list) and next_actions:
                lines.append("Next actions:")
                for idx, action in enumerate(next_actions[:8], start=1):
                    txt = str(action or "").strip()
                    if txt:
                        lines.append(f"{idx}. {txt}")
        elif task == "hades_retest_comparator":
            result = str(payload.get("result") or "").strip()
            rec = str(payload.get("recommendation") or "").strip()
            if result:
                lines.append(f"Retest result: `{result}`")
            if rec:
                lines.append(f"Recommendation: `{rec}`")
            comparability = _safe_json_obj(payload.get("comparability_assessment"))
            attribution = _safe_json_obj(payload.get("attribution_assessment"))
            if comparability:
                lines.append(f"Comparable baseline/retest: `{bool(comparability.get('is_comparable'))}`")
            if attribution:
                lines.append(f"Likely attributable: `{bool(attribution.get('likely_attributable'))}`")
                confounders = attribution.get("confounders")
                if isinstance(confounders, list) and confounders:
                    lines.append("Confounders:")
                    for idx, item in enumerate(confounders[:6], start=1):
                        lines.append(f"{idx}. {item}")
            deltas = payload.get("metric_deltas")
            if isinstance(deltas, list) and deltas:
                lines.append("Metric deltas:")
                for idx, row in enumerate(deltas[:8], start=1):
                    if not isinstance(row, dict):
                        continue
                    metric = str(row.get("metric") or "").strip()
                    before = row.get("before")
                    after = row.get("after")
                    delta = row.get("delta")
                    interp = str(row.get("interpretation") or "").strip()
                    if metric:
                        lines.append(
                            f"{idx}. `{metric}` before={before} after={after} delta={delta} ({interp or 'n/a'})"
                        )
            next_actions = payload.get("recommended_next_actions")
            if isinstance(next_actions, list) and next_actions:
                lines.append("Next actions:")
                for idx, action in enumerate(next_actions[:8], start=1):
                    txt = str(action or "").strip()
                    if txt:
                        lines.append(f"{idx}. {txt}")
        elif task == "hades_evidence_consistency_checker":
            contradictions = payload.get("contradictions")
            if isinstance(contradictions, list):
                lines.append(f"Contradictions detected: `{len(contradictions)}`")
                for idx, row in enumerate(contradictions[:5], start=1):
                    if not isinstance(row, dict):
                        continue
                    code = str(row.get("code") or "").strip()
                    details = str(row.get("details") or "").strip()
                    lines.append(f"{idx}. `{code}` {details}")
            summary2 = str(payload.get("consistency_summary") or "").strip()
            if summary2:
                lines.append(summary2)
        elif task == "interpret_pipeline_blocker":
            blocker = _safe_json_obj(payload.get("blocker_assessment"))
            if blocker:
                lines.append(
                    "Blocker assessment: "
                    f"`{blocker.get('blocker_code')}` / `{blocker.get('blocker_severity')}`"
                )
                rec = str(blocker.get("recommended_next_action") or "").strip()
                if rec:
                    lines.append(f"Recommended unblock route: {rec}")
        elif task == "prioritize_pipeline_resolution":
            priorities = payload.get("priority_order")
            if isinstance(priorities, list) and priorities:
                lines.append("Priority order:")
                for idx, item in enumerate(priorities[:8], start=1):
                    lines.append(f"{idx}. `{item}`")
        elif task == "explain_cleanup_risk":
            risk = _safe_json_obj(payload.get("cleanup_risk"))
            if risk:
                lines.append(
                    "Cleanup risk: "
                    f"`{risk.get('impact_level')}` impact, "
                    f"affected assets ~`{risk.get('affected_assets_estimate')}`"
                )
        elif task == "interpret_merge_evidence":
            merge = _safe_json_obj(payload.get("merge_assessment"))
            if merge:
                lines.append(
                    "Merge interpretation: "
                    f"confidence `{merge.get('confidence_label')}`, "
                    f"opposite-direction likelihood `{merge.get('opposite_direction_likelihood')}`"
                )
                rec2 = str(merge.get("recommendation") or "").strip()
                if rec2:
                    lines.append(rec2)
        elif task in {"hades_patch_task_generator", "generate_codex_patch_task"}:
            patch = _safe_json_obj(payload.get("patch_task"))
            prompt_text = str(patch.get("prompt_text") or "").strip()
            if prompt_text:
                lines.append("Codex patch task draft:")
                lines.append("```text")
                lines.append(_shorten(prompt_text, 6000))
                lines.append("```")

        actions = payload.get("recommended_actions")
        if isinstance(actions, list) and actions:
            lines.append("Next actions:")
            for idx, action in enumerate(actions[:5], start=1):
                if not isinstance(action, dict):
                    continue
                priority = str(action.get("priority") or "").strip()
                desc = str(action.get("description") or "").strip()
                lines.append(f"{idx}. ({priority}) {desc}")

        if bool(payload.get("operator_confirmation_required", False)):
            lines.append("")
            lines.append("Operator confirmation is still required for high-impact actions.")

        return "\n".join(lines).strip()
