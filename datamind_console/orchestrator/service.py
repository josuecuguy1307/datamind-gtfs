"""LEGACY / REFERENCE — Original orchestrator service layer.

This module is part of the v1 orchestration system (session_runner + service + role_model_service).
The active runtime is pipeline_autopilot.py + operator_orchestrator_view.py.
Retained because active tests verify authority-model invariants against this code.
Do NOT use for new features — all new orchestration goes through SupervisedPipelineAutopilot.
"""
from __future__ import annotations

import inspect
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from datamind_console.api_chatgpt.services.advisory_service import AdvisoryError, AdvisoryService
from datamind_console.api_chatgpt.services.openai_client import build_default_model_client
from datamind_console.api_chatgpt.services.snapshot_builder import SnapshotBuilder
from datamind_console.labels import operator_labels

try:
    from local_runner.scripts.runner_core import RunnerError, load_runner_config, run_prompt_file
except Exception:  # pragma: no cover
    RunnerError = RuntimeError  # type: ignore[assignment]
    load_runner_config = None  # type: ignore[assignment]
    run_prompt_file = None  # type: ignore[assignment]


OPERATOR_ORCHESTRATOR_TEMPLATE = "AI-Assisted Review + Patch Task Session"

SAFETY_CONTEXT = {
    "mode": "advisory_only",
    "execution_authority": "runtime_validators_operator",
    "destructive_actions_allowed": False,
}

TASK_ORDER = [
    "analyze_latest_run",
    "review_ai_bot_quality",
    "generate_codex_patch_task",
]

TERMINAL_SESSION_STATUSES = {"completed", "cancelled", "failed"}
SENSITIVE_KEYWORDS = {"api_key", "token", "password", "secret", "authorization", "access_key"}

STEP_DEFS = [
    {
        "step_id": "build_snapshot",
        "name": "Build Snapshot",
        "description": "Build normalized AI Bot snapshots (fixture/live, read-only).",
        "requires_operator_confirmation": False,
        "allow_skip": False,
    },
    {
        "step_id": "analyze_latest_run",
        "name": "Analyze Latest Run",
        "description": "Call api_chatgpt analyze endpoint logic with advisory safety context.",
        "requires_operator_confirmation": False,
        "allow_skip": False,
    },
    {
        "step_id": "review_ai_bot_quality",
        "name": "Review AI Bot Quality",
        "description": "Call api_chatgpt quality meta-review endpoint logic.",
        "requires_operator_confirmation": False,
        "allow_skip": False,
    },
    {
        "step_id": "generate_codex_patch_task",
        "name": "Generate Patch Task",
        "description": "Generate a scoped patch task prompt for coding assistants.",
        "requires_operator_confirmation": False,
        "allow_skip": False,
    },
    {
        "step_id": "operator_dispatch_decision",
        "name": "Operator Dispatch Decision",
        "description": "Pause and ask operator whether to send prompt to Codex/Claude or stop.",
        "requires_operator_confirmation": True,
        "allow_skip": True,
    },
    {
        "step_id": "dispatch_patch_prompt",
        "name": "Dispatch Prompt",
        "description": "Dispatch generated patch prompt via local runner to chosen assistant.",
        "requires_operator_confirmation": False,
        "allow_skip": True,
    },
    {
        "step_id": "operator_review_assistant_output",
        "name": "Operator Review Output",
        "description": "Pause after assistant output so operator can review artifacts.",
        "requires_operator_confirmation": True,
        "allow_skip": True,
    },
    {
        "step_id": "session_summary",
        "name": "Session Summary",
        "description": "Finalize summary, artifacts, and status.",
        "requires_operator_confirmation": False,
        "allow_skip": False,
    },
]

STEP_TO_TASK = {
    "analyze_latest_run": "analyze_latest_run",
    "review_ai_bot_quality": "review_ai_bot_quality",
    "generate_codex_patch_task": "generate_codex_patch_task",
}


class OrchestratorError(Exception):
    """Predictable orchestrator control errors with safe messages."""


class OrchestratorSessionStore:
    def __init__(self, *, base_dir: Path) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.sessions_dir = self.base_dir / "orchestrator_sessions"
        self.index_path = self.base_dir / "sessions_index.jsonl"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def save_session(self, session: Dict[str, Any]) -> Path:
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            raise OrchestratorError("Cannot persist session without session_id.")

        safe_session = _sanitize_session_for_persistence(session)
        session_path = self.sessions_dir / f"{session_id}.json"
        _atomic_write_text(session_path, json.dumps(safe_session, ensure_ascii=True, indent=2))

        index_row = {
            "session_id": session_id,
            "template_name": safe_session.get("template_name"),
            "status": safe_session.get("status"),
            "started_at": safe_session.get("started_at"),
            "ended_at": safe_session.get("ended_at"),
            "current_step_id": safe_session.get("current_step_id"),
            "updated_at": _utc_now().isoformat(),
            "session_path": str(session_path),
        }
        self._append_jsonl(self.index_path, index_row)
        return session_path

    def load_session(self, session_id: str) -> Dict[str, Any]:
        sid = str(session_id or "").strip()
        if not sid:
            raise OrchestratorError("Session ID is required for resume.")
        path = self.sessions_dir / f"{sid}.json"
        if not path.exists():
            raise OrchestratorError(f"Session file not found for id `{sid}`.")
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise OrchestratorError(f"Session file is invalid for id `{sid}`.")
        return data

    def list_sessions(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if self.index_path.exists():
            with self.index_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    text = str(line or "").strip()
                    if not text:
                        continue
                    try:
                        row = json.loads(text)
                    except Exception:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)

        latest_by_id: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            sid = str(row.get("session_id") or "").strip()
            if not sid:
                continue
            prev = latest_by_id.get(sid)
            if prev is None:
                latest_by_id[sid] = row
                continue
            prev_ts = _parse_iso_ts(prev.get("updated_at"))
            curr_ts = _parse_iso_ts(row.get("updated_at"))
            if curr_ts >= prev_ts:
                latest_by_id[sid] = row

        out = sorted(
            latest_by_id.values(),
            key=lambda item: _parse_iso_ts(item.get("updated_at")),
            reverse=True,
        )
        if int(limit) > 0:
            out = out[: int(limit)]
        return out

    @staticmethod
    def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n")


class OrchestratorFileLogger:
    def __init__(self, *, base_dir: Path) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.events_path = self.base_dir / "events.jsonl"
        self.sessions_dir = self.base_dir / "sessions"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def log_event(self, *, session_id: str, event: Dict[str, Any]) -> None:
        row = {"session_id": session_id, **dict(event)}
        self._append_jsonl(self.events_path, row)

    def log_session_snapshot(self, *, session: Dict[str, Any]) -> Path:
        out_path = self.sessions_dir / f"{session['session_id']}.json"
        out_path.write_text(json.dumps(session, ensure_ascii=True, indent=2), encoding="utf-8")
        return out_path

    @staticmethod
    def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n")


AdvisoryCaller = Callable[[str, Dict[str, Any], str], Dict[str, Any]]
LocalRunnerCaller = Callable[..., Dict[str, Any]]
SnapshotBuilderFactory = Callable[[str], SnapshotBuilder]


class OperatorOrchestratorService:
    def __init__(
        self,
        *,
        advisory_caller: AdvisoryCaller | None = None,
        local_runner_caller: LocalRunnerCaller | None = None,
        snapshot_builder_factory: SnapshotBuilderFactory | None = None,
        logs_dir: Path | None = None,
        runner_config_path: Path | None = None,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        self.logs_dir = Path(logs_dir or (repo_root / "datamind_console" / "orchestrator_logs")).resolve()
        self.runner_config_path = Path(
            runner_config_path or (repo_root / "local_runner" / "config" / "runner_config.yaml")
        ).resolve()
        self.logger = OrchestratorFileLogger(base_dir=self.logs_dir)
        self.session_store = OrchestratorSessionStore(base_dir=self.logs_dir)
        self._advisory_caller = advisory_caller or self._default_advisory_caller
        self._local_runner_caller = local_runner_caller or self._default_local_runner_caller
        self._snapshot_builder_factory = snapshot_builder_factory or self._default_snapshot_builder_factory

    def list_sessions(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        return self.session_store.list_sessions(limit=limit)

    def resume_session(self, session_id: str) -> Dict[str, Any]:
        session = self.session_store.load_session(session_id)
        self._persist(session)
        return session

    def start_session(
        self,
        *,
        template_name: str = OPERATOR_ORCHESTRATOR_TEMPLATE,
        snapshot_mode: str = "fixture",
        chatgpt_api_mode: str = "mock",
        operator_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = _utc_now()
        session_id = f"orch_{now.strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:8]}"
        steps = [_new_step(item) for item in STEP_DEFS]
        session = {
            "session_id": session_id,
            "template_name": template_name,
            "status": "running",
            "started_at": now.isoformat(),
            "ended_at": None,
            "current_step_id": steps[0]["step_id"],
            "steps": steps,
            "events": [],
            "artifacts": [],
            "mode_info": {
                "snapshot_mode": str(snapshot_mode or "fixture").strip().lower(),
                "chatgpt_api_mode": str(chatgpt_api_mode or "mock").strip().lower(),
                "assistant_target": "none",
            },
            "operator_context": dict(operator_context or {}),
            "operator_decisions": [],
            "task_snapshots": {},
            "task_responses": {},
            "step_details": {},
        }
        self._event(
            session,
            source="orchestrator",
            level="info",
            message="Operator orchestrator session started.",
            metadata={
                "template_name": template_name,
                "snapshot_mode": session["mode_info"]["snapshot_mode"],
                "chatgpt_api_mode": session["mode_info"]["chatgpt_api_mode"],
            },
        )
        self._persist(session)
        return session

    def cancel_session(self, session: Dict[str, Any], *, reason: str = "operator_stop") -> Dict[str, Any]:
        if session.get("status") in TERMINAL_SESSION_STATUSES:
            return session
        now = _utc_now().isoformat()
        session["status"] = "cancelled"
        session["ended_at"] = now
        step = self._current_step(session)
        if step and step.get("status") in {"pending", "running"}:
            self._mark_step_skipped(
                session,
                step,
                reason=f"Session cancelled ({reason}).",
            )
        self._event(
            session,
            source="operator",
            level="warn",
            message="Session cancelled by operator.",
            metadata={"reason": reason},
        )
        self._persist(session)
        return session

    def run_until_pause(self, session: Dict[str, Any]) -> Dict[str, Any]:
        if session.get("status") in TERMINAL_SESSION_STATUSES:
            return session
        if session.get("status") == "waiting_for_operator":
            return session

        session["status"] = "running"
        session["ended_at"] = None
        self._persist(session)
        iterations = 0
        while iterations < 32:
            iterations += 1
            step = self._current_step(session)
            if step is None:
                self._complete_session(session)
                self._persist(session)
                break

            status = str(step.get("status") or "pending")
            if status == "completed" or status == "skipped":
                self._advance_to_next_step(session)
                self._persist(session)
                continue
            if status == "waiting_for_operator":
                session["status"] = "waiting_for_operator"
                self._persist(session)
                break
            if status == "failed":
                session["status"] = "failed"
                self._persist(session)
                break
            if status == "running" and str(step.get("step_id") or "") == "dispatch_patch_prompt":
                session["status"] = "running"
                self._persist(session)
                break

            self._execute_step(session, step)
            self._persist(session)
            if session.get("status") in {"waiting_for_operator", "failed", "completed", "cancelled"}:
                break

            post = str(step.get("status") or "")
            if post in {"completed", "skipped"}:
                self._advance_to_next_step(session)
                self._persist(session)
                continue
            break

        if iterations >= 32 and session.get("status") == "running":
            step = self._current_step(session)
            if step is not None:
                self._mark_step_failed(
                    session,
                    step,
                    "Iteration guard reached while progressing workflow.",
                )
                self._persist(session)

        self._persist(session)
        return session

    def apply_dispatch_decision(self, session: Dict[str, Any], *, decision: str) -> Dict[str, Any]:
        self._ensure_session_mutable(session, action="apply dispatch decision")
        step = self._require_current_step(session)
        if step["step_id"] != "operator_dispatch_decision" or step["status"] != "waiting_for_operator":
            raise OrchestratorError("Dispatch decision is only valid at operator dispatch checkpoint.")

        chosen = str(decision or "").strip().lower()
        if chosen not in {"codex", "claude", "stop"}:
            raise OrchestratorError("Dispatch decision must be one of: codex, claude, stop.")

        session["operator_decisions"].append(
            {
                "timestamp": _utc_now().isoformat(),
                "step_id": step["step_id"],
                "decision": chosen,
            }
        )

        if chosen == "stop":
            session["mode_info"]["assistant_target"] = "none"
            self._mark_step_completed(
                session,
                step,
                details={"decision": "stop"},
            )
            self._skip_step_by_id(session, "dispatch_patch_prompt", reason="Operator chose to stop after patch-task generation.")
            self._skip_step_by_id(session, "operator_review_assistant_output", reason="No assistant dispatch executed.")
            self._event(
                session,
                source="operator",
                level="info",
                message="Operator chose to stop session before assistant dispatch.",
                metadata={},
            )
            session["status"] = "running"
            self._advance_to_next_step(session)
            return self.run_until_pause(session)

        session["mode_info"]["assistant_target"] = chosen
        self._mark_step_completed(
            session,
            step,
            details={"decision": chosen},
        )
        self._event(
            session,
            source="operator",
            level="info",
            message="Operator confirmed assistant dispatch target.",
            metadata={"target": chosen},
        )
        session["status"] = "running"
        self._advance_to_next_step(session)
        return self.run_until_pause(session)

    def continue_after_assistant_output(self, session: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_session_mutable(session, action="continue session")
        step = self._require_current_step(session)
        if step["step_id"] != "operator_review_assistant_output" or step["status"] != "waiting_for_operator":
            raise OrchestratorError("Continue is only valid at operator review output checkpoint.")

        self._mark_step_completed(
            session,
            step,
            details={"decision": "continue"},
        )
        session["operator_decisions"].append(
            {
                "timestamp": _utc_now().isoformat(),
                "step_id": step["step_id"],
                "decision": "continue",
            }
        )
        self._event(
            session,
            source="operator",
            level="info",
            message="Operator approved proceeding after assistant output review.",
            metadata={},
        )
        session["status"] = "running"
        self._advance_to_next_step(session)
        return self.run_until_pause(session)

    def retry_current_step(self, session: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_session_mutable(session, action="retry step")
        step = self._require_current_step(session)

        if step["status"] == "failed":
            step["retry_count"] = int(step.get("retry_count") or 0) + 1
            step["status"] = "pending"
            step["started_at"] = None
            step["ended_at"] = None
            step["duration_ms"] = None
            step["error_summary"] = None
            session["status"] = "running"
            session["ended_at"] = None
            self._event(
                session,
                source="operator",
                level="info",
                message="Retrying failed step.",
                metadata={"step_id": step["step_id"], "retry_count": step["retry_count"]},
            )
            return self.run_until_pause(session)

        if step["step_id"] == "operator_review_assistant_output" and step["status"] == "waiting_for_operator":
            dispatch_step = self._step_by_id(session, "dispatch_patch_prompt")
            if dispatch_step is None:
                raise OrchestratorError("Dispatch step not found in session.")
            dispatch_step["retry_count"] = int(dispatch_step.get("retry_count") or 0) + 1
            dispatch_step["status"] = "pending"
            dispatch_step["started_at"] = None
            dispatch_step["ended_at"] = None
            dispatch_step["duration_ms"] = None
            dispatch_step["error_summary"] = None
            step["status"] = "pending"
            step["started_at"] = None
            step["ended_at"] = None
            step["duration_ms"] = None
            session["status"] = "running"
            session["ended_at"] = None
            session["current_step_id"] = dispatch_step["step_id"]
            self._event(
                session,
                source="operator",
                level="info",
                message="Retrying assistant dispatch from operator review checkpoint.",
                metadata={"dispatch_retry_count": dispatch_step["retry_count"]},
            )
            return self.run_until_pause(session)

        raise OrchestratorError("Current step is not retryable.")

    def skip_current_step(self, session: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_session_mutable(session, action="skip step")
        step = self._require_current_step(session)
        if not bool(step.get("allow_skip")):
            raise OrchestratorError(f"Step `{step['step_id']}` cannot be skipped.")
        self._mark_step_skipped(session, step, reason="Operator requested skip.")
        self._event(
            session,
            source="operator",
            level="warn",
            message="Operator skipped current step.",
            metadata={"step_id": step["step_id"]},
        )
        session["status"] = "running"
        self._advance_to_next_step(session)
        return self.run_until_pause(session)

    def run_dispatch_step_async(
        self,
        session: Dict[str, Any],
        *,
        cancel_event: Any | None = None,
    ) -> Dict[str, Any]:
        self._ensure_session_mutable(session, action="run dispatch")
        step = self._require_current_step(session)
        if step.get("step_id") != "dispatch_patch_prompt":
            raise OrchestratorError("Async dispatch is only valid at `dispatch_patch_prompt` step.")
        if str(step.get("status") or "pending") not in {"pending", "running"}:
            raise OrchestratorError("Dispatch step is not in a runnable state.")

        if str(step.get("status") or "") != "running":
            self._mark_step_running(step)
        session["status"] = "running"
        session["ended_at"] = None
        self._event(
            session,
            source="local_runner",
            level="info",
            message="Background dispatch thread started.",
            metadata={"step_id": "dispatch_patch_prompt"},
        )
        self._persist(session)

        try:
            details = self._step_dispatch_patch_prompt(session, cancel_event=cancel_event)
            self._mark_step_completed(session, step, details=details)
            session["status"] = "running"
            self._advance_to_next_step(session)
            self._persist(session)
            return self.run_until_pause(session)
        except Exception as e:
            self._mark_step_failed(
                session,
                step,
                str(e),
                details=_extract_failure_details(e),
            )
            self._persist(session)
            return session

    def note_dispatch_cancel_requested(self, session: Dict[str, Any]) -> Dict[str, Any]:
        step = self._current_step(session)
        if not step or str(step.get("step_id") or "") != "dispatch_patch_prompt":
            return session
        if str(step.get("status") or "") != "running":
            return session
        self._event(
            session,
            source="operator",
            level="warn",
            message="Operator requested dispatch cancellation.",
            metadata={"step_id": "dispatch_patch_prompt"},
        )
        detail = dict((session.get("step_details") or {}).get("dispatch_patch_prompt") or {})
        detail["cancel_requested_at"] = _utc_now().isoformat()
        session.setdefault("step_details", {})
        session["step_details"]["dispatch_patch_prompt"] = detail
        self._persist(session)
        return session

    def submit_session_label(
        self,
        session: Dict[str, Any],
        *,
        operator_grade: int | str,
        operator_sequence_label: str | None,
        final_run_disposition: str,
        sequence_warning_correct: str | None,
        reorder_action_taken: str | None,
        reorder_helpful: str | None,
        notes: str | None,
        run_id: str | None = None,
        phase: str | None = None,
        stage: str | None = None,
        route_id: str | None = None,
    ) -> Dict[str, Any]:
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            raise OrchestratorError("Session ID missing; cannot write operator label.")
        payload = {
            "session_id": session_id,
            "phase": str(phase or "").strip().lower() or None,
            "stage": str(stage or "").strip().lower() or None,
            "run_id": str(run_id or "").strip() or None,
            "route_id": str(route_id or "").strip() or None,
            "operator_sequence_label": str(operator_sequence_label or "").strip().lower() or None,
            "operator_grade": operator_grade,
            "sequence_warning_correct": (str(sequence_warning_correct or "").strip().lower() or None),
            "reorder_action_taken": (str(reorder_action_taken or "").strip().lower() or None),
            "reorder_helpful": (str(reorder_helpful or "").strip().lower() or None),
            "final_run_disposition": str(final_run_disposition or "").strip().lower(),
            "notes": str(notes or "").strip(),
            "label_version": "2",
        }
        try:
            row = operator_labels.append_session_label(payload)
        except Exception as e:
            raise OrchestratorError(f"Unable to save session label: {e}")

        details = dict((session.get("step_details") or {}).get("session_summary") or {})
        labels = list(details.get("operator_labels") or [])
        labels.append(
            {
                "label_id": row.get("label_id"),
                "captured_at": row.get("captured_at"),
                "operator_grade": row.get("operator_grade"),
                "operator_sequence_label": row.get("operator_sequence_label"),
                "final_run_disposition": row.get("final_run_disposition"),
            }
        )
        details["operator_labels"] = labels[-25:]
        session.setdefault("step_details", {})
        session["step_details"]["session_summary"] = details

        self._event(
            session,
            source="operator",
            level="info",
            message="Operator session label captured.",
            metadata={
                "label_id": row.get("label_id"),
                "operator_grade": row.get("operator_grade"),
                "operator_sequence_label": row.get("operator_sequence_label"),
                "final_run_disposition": row.get("final_run_disposition"),
            },
        )
        self._persist(session)
        return row

    def _execute_step(self, session: Dict[str, Any], step: Dict[str, Any]) -> None:
        self._mark_step_running(step)
        step_id = step["step_id"]
        try:
            if step_id == "build_snapshot":
                details = self._step_build_snapshot(session)
                self._mark_step_completed(session, step, details=details)
                return

            if step_id in STEP_TO_TASK:
                details = self._step_call_advisory(session, task=STEP_TO_TASK[step_id])
                self._mark_step_completed(session, step, details=details)
                return

            if step_id == "operator_dispatch_decision":
                self._mark_step_waiting_for_operator(
                    session,
                    step,
                    details={
                        "options": ["send_to_codex", "send_to_claude", "stop_session"],
                        "note": "High-impact transition requires operator confirmation.",
                    },
                )
                return

            if step_id == "dispatch_patch_prompt":
                session.setdefault("step_details", {})
                existing = dict((session.get("step_details") or {}).get(step_id) or {})
                existing["dispatch_mode"] = "async_thread"
                existing.setdefault("dispatch_target", (session.get("mode_info") or {}).get("assistant_target"))
                session["step_details"][step_id] = existing
                self._event(
                    session,
                    source="local_runner",
                    level="info",
                    message="Dispatch step queued for background runner thread.",
                    metadata={"step_id": step_id},
                )
                return

            if step_id == "operator_review_assistant_output":
                self._mark_step_waiting_for_operator(
                    session,
                    step,
                    details={
                        "options": ["continue", "retry_dispatch", "stop_session"],
                        "note": "Operator must review assistant output artifacts before proceeding.",
                    },
                )
                return

            if step_id == "session_summary":
                details = self._step_session_summary(session)
                self._mark_step_completed(session, step, details=details)
                return

            raise OrchestratorError(f"Unknown step_id `{step_id}`")
        except Exception as e:
            self._mark_step_failed(
                session,
                step,
                str(e),
                details=_extract_failure_details(e),
            )

    def _step_build_snapshot(self, session: Dict[str, Any]) -> Dict[str, Any]:
        snapshot_mode = str(session.get("mode_info", {}).get("snapshot_mode") or "fixture")
        builder = self._snapshot_builder_factory(snapshot_mode)
        built: List[Dict[str, Any]] = []
        for task in TASK_ORDER:
            snapshot = builder.build(
                task=task,
                supplied_snapshot={},
                operator_context=dict(session.get("operator_context") or {}),
            )
            session["task_snapshots"][task] = snapshot
            artifact_id = self._artifact(
                session,
                step_id="build_snapshot",
                artifact_type="snapshot",
                path=None,
                metadata={
                    "task": task,
                    "snapshot_id": snapshot.get("snapshot_id"),
                    "snapshot_source": snapshot.get("snapshot_source"),
                    "insufficient_data_flags": snapshot.get("insufficient_data_flags") or [],
                },
            )
            built.append(
                {
                    "task": task,
                    "snapshot_id": snapshot.get("snapshot_id"),
                    "snapshot_source": snapshot.get("snapshot_source"),
                    "insufficient_data_flags": snapshot.get("insufficient_data_flags") or [],
                    "artifact_id": artifact_id,
                }
            )
        self._event(
            session,
            source="ai_bot",
            level="info",
            message="Snapshot bundle prepared for advisory tasks.",
            metadata={"snapshot_mode": snapshot_mode, "tasks": [x["task"] for x in built]},
        )
        return {"snapshot_mode": snapshot_mode, "snapshots": built}

    def _step_call_advisory(self, session: Dict[str, Any], *, task: str) -> Dict[str, Any]:
        snapshots = dict(session.get("task_snapshots", {}) or {})
        snapshot = dict(snapshots.get(task) or {})
        if (not snapshot) and task == "hades_patch_task_generator":
            snapshot = dict(snapshots.get("generate_codex_patch_task") or {})
        if snapshot and str(snapshot.get("task") or "").strip() != str(task):
            snapshot["task"] = str(task)
        if not snapshot:
            raise OrchestratorError(f"Missing snapshot for task `{task}`. Run build_snapshot first.")

        envelope = {
            "task": task,
            "snapshot": snapshot,
            "operator_context": dict(session.get("operator_context") or {}),
            "safety_context": dict(SAFETY_CONTEXT),
        }
        api_mode = str(session.get("mode_info", {}).get("chatgpt_api_mode") or "mock")
        response = self._advisory_caller(task, envelope, api_mode)
        session["task_responses"][task] = response
        if task == "hades_patch_task_generator":
            # Backward-compatible key for older readers.
            session["task_responses"]["generate_codex_patch_task"] = response

        artifact_id = self._artifact(
            session,
            step_id=task,
            artifact_type="advisory_response",
            path=None,
            metadata={
                "task": task,
                "mode": response.get("mode"),
                "confidence": (response.get("confidence") or {}).get("score"),
                "operator_confirmation_required": response.get("operator_confirmation_required"),
            },
        )
        req_summary = {
            "task": task,
            "snapshot_id": snapshot.get("snapshot_id"),
            "evidence_ref_count": len(list(snapshot.get("evidence_refs") or [])),
            "safety_context": dict(SAFETY_CONTEXT),
        }
        resp_summary = {
            "summary": response.get("summary"),
            "mode": response.get("mode"),
            "confidence": response.get("confidence"),
            "risk_flags": response.get("risk_flags") or [],
            "insufficient_data_flags": response.get("insufficient_data_flags") or [],
            "operator_confirmation_required": response.get("operator_confirmation_required"),
        }
        detail = {
            "request_summary": req_summary,
            "response_summary": resp_summary,
            "artifact_refs": [artifact_id],
        }
        if task in {"generate_codex_patch_task", "hades_patch_task_generator"}:
            prompt_text = str(((response.get("patch_task") or {}).get("prompt_text") or "")).strip()
            detail["prompt_preview"] = _truncate(prompt_text, limit=1800)

        self._event(
            session,
            source="api_chatgpt",
            level="info",
            message=f"Advisory task completed: {task}",
            metadata={
                "task": task,
                "api_mode": api_mode,
                "snapshot_id": snapshot.get("snapshot_id"),
                "response_mode": response.get("mode"),
            },
        )
        return detail

    def _step_dispatch_patch_prompt(self, session: Dict[str, Any], *, cancel_event: Any | None = None) -> Dict[str, Any]:
        target = str(session.get("mode_info", {}).get("assistant_target") or "").strip().lower()
        if target not in {"codex", "claude"}:
            raise OrchestratorError("Assistant target must be selected by operator before dispatch.")

        patch_response = dict(session.get("task_responses", {}).get("hades_patch_task_generator") or {})
        if not patch_response:
            patch_response = dict(session.get("task_responses", {}).get("generate_codex_patch_task") or {})
        patch_task = dict(patch_response.get("patch_task") or {})
        prompt_text = str(patch_task.get("prompt_text") or "").strip()
        if not prompt_text:
            raise OrchestratorError("Patch prompt text is empty; cannot dispatch to local runner.")

        result = self._call_local_runner(
            prompt_text=prompt_text,
            target=target,
            session_id=str(session["session_id"]),
            cancel_event=cancel_event,
        )
        result_status = str(result.get("status") or "failed")

        artifact_ids: List[str] = []
        for field, artifact_type in (
            ("output_file", "runner_output"),
            ("stderr_file", "runner_stderr"),
            ("prompt_file", "runner_prompt_original"),
            ("prompt_file_final", "runner_prompt_final"),
            ("log_file", "runner_log"),
        ):
            value = result.get(field)
            if value:
                artifact_ids.append(
                    self._artifact(
                        session,
                        step_id="dispatch_patch_prompt",
                        artifact_type=artifact_type,
                        path=str(value),
                        metadata={"target": target, "status": result_status},
                    )
                )

        detail = {
            "dispatch_target": target,
            "runner_result_summary": {
                "status": result_status,
                "target": target,
                "command_mode": result.get("command_mode"),
                "duration_ms": result.get("duration_ms"),
                "return_code": result.get("return_code"),
                "error_summary": result.get("error_summary"),
                "command_used": result.get("command_used"),
                "output_file": result.get("output_file"),
                "stderr_file": result.get("stderr_file"),
                "prompt_file_final": result.get("prompt_file_final"),
                "log_file": result.get("log_file"),
            },
            "prompt_preview": _truncate(prompt_text, limit=2000),
            "artifact_refs": artifact_ids,
        }
        session.setdefault("step_details", {})
        session["step_details"]["dispatch_patch_prompt"] = dict(detail)

        lvl = "info" if result_status == "success" else "error"
        self._event(
            session,
            source="local_runner",
            level=lvl,
            message=f"Local runner dispatch finished with status={result_status}.",
            metadata={
                "target": target,
                "status": result_status,
                "output_file": result.get("output_file"),
                "stderr_file": result.get("stderr_file"),
                "log_file": result.get("log_file"),
            },
        )
        if result_status != "success":
            error_summary = str(result.get("error_summary") or "").strip()
            if not error_summary:
                error_summary = (
                    f"return_code={result.get('return_code')} "
                    f"stderr_file={result.get('stderr_file') or '-'} "
                    f"output_file={result.get('output_file') or '-'}"
                )
            if result_status == "cancelled":
                raise OrchestratorError(
                    f"Local runner dispatch cancelled for target `{target}`: "
                    f"{error_summary or 'operator_cancelled'}"
                )
            raise OrchestratorError(
                f"Local runner dispatch failed for target `{target}`: "
                f"{error_summary or 'unknown error'}"
            )
        return detail

    def _step_session_summary(self, session: Dict[str, Any]) -> Dict[str, Any]:
        steps = list(session.get("steps") or [])
        status_counts: Dict[str, int] = {}
        for step in steps:
            st = str(step.get("status") or "pending")
            if str(step.get("step_id") or "") == "session_summary" and st == "running":
                st = "completed"
            status_counts[st] = int(status_counts.get(st) or 0) + 1

        summary = {
            "session_id": session.get("session_id"),
            "template_name": session.get("template_name"),
            "status_counts": status_counts,
            "events_count": len(list(session.get("events") or [])),
            "artifacts_count": len(list(session.get("artifacts") or [])),
            "assistant_target": (session.get("mode_info") or {}).get("assistant_target"),
            "api_mode": (session.get("mode_info") or {}).get("chatgpt_api_mode"),
            "snapshot_mode": (session.get("mode_info") or {}).get("snapshot_mode"),
        }
        self._event(
            session,
            source="orchestrator",
            level="info",
            message="Session summary generated.",
            metadata=summary,
        )
        return summary

    def _default_snapshot_builder_factory(self, snapshot_mode: str) -> SnapshotBuilder:
        return SnapshotBuilder(snapshot_mode=snapshot_mode)

    def _default_advisory_caller(self, task: str, envelope: Dict[str, Any], chatgpt_api_mode: str) -> Dict[str, Any]:
        use_mock = str(chatgpt_api_mode or "mock").strip().lower() != "real"
        model_client = build_default_model_client(mock_mode=use_mock)
        advisory = AdvisoryService(model_client=model_client)
        return advisory.run_task(endpoint_task=task, envelope=envelope)

    def _call_local_runner(
        self,
        *,
        prompt_text: str,
        target: str,
        session_id: str,
        cancel_event: Any | None = None,
    ) -> Dict[str, Any]:
        try:
            sig = inspect.signature(self._local_runner_caller)
            if "cancel_event" in sig.parameters:
                return _to_plain_dict(
                    self._local_runner_caller(
                        prompt_text,
                        target,
                        session_id,
                        cancel_event=cancel_event,
                    )
                )
        except Exception:
            pass
        return _to_plain_dict(self._local_runner_caller(prompt_text, target, session_id))

    def _default_local_runner_caller(
        self,
        prompt_text: str,
        target: str,
        session_id: str,
        *,
        cancel_event: Any | None = None,
    ) -> Dict[str, Any]:
        if load_runner_config is None or run_prompt_file is None:
            raise OrchestratorError("local_runner is unavailable in current environment.")
        if not self.runner_config_path.exists():
            raise OrchestratorError(f"Runner config not found: {self.runner_config_path}")

        try:
            cfg = load_runner_config(self.runner_config_path)
            stamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
            prompt_name = f"{target}_orchestrator_{session_id}_{stamp}.md"
            prompt_path = cfg.paths.inbox / prompt_name
            prompt_path.write_text(prompt_text, encoding="utf-8")
            result = run_prompt_file(
                file_path=prompt_path,
                target=target,
                config_path=self.runner_config_path,
                cancel_event=cancel_event,
            )
            out = _to_plain_dict(result)
            try:
                out["command_mode"] = cfg.targets.get(str(target).strip().lower()).input_mode
            except Exception:
                out["command_mode"] = None
            return out
        except RunnerError as e:
            raise OrchestratorError(str(e))
        except Exception as e:
            raise OrchestratorError(f"Local runner dispatch failed: {e}")

    def _require_current_step(self, session: Dict[str, Any]) -> Dict[str, Any]:
        step = self._current_step(session)
        if step is None:
            raise OrchestratorError("Session has no current step.")
        return step

    def _ensure_session_mutable(self, session: Dict[str, Any], *, action: str) -> None:
        status = str(session.get("status") or "").strip().lower()
        if status in TERMINAL_SESSION_STATUSES:
            raise OrchestratorError(
                f"Cannot {action} for terminal session status `{status}`. Terminal sessions are read-only."
            )

    def _current_step(self, session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        current_id = str(session.get("current_step_id") or "")
        if current_id:
            step = self._step_by_id(session, current_id)
            if step:
                return step
        for step in session.get("steps") or []:
            if str(step.get("status") or "pending") in {"pending", "running", "waiting_for_operator", "failed"}:
                session["current_step_id"] = step["step_id"]
                return step
        return None

    def _step_by_id(self, session: Dict[str, Any], step_id: str) -> Optional[Dict[str, Any]]:
        for step in session.get("steps") or []:
            if str(step.get("step_id") or "") == str(step_id):
                return step
        return None

    def _advance_to_next_step(self, session: Dict[str, Any]) -> None:
        steps = list(session.get("steps") or [])
        current_id = str(session.get("current_step_id") or "")
        if not steps:
            session["current_step_id"] = None
            self._complete_session(session)
            return

        start_idx = -1
        if current_id:
            for idx, item in enumerate(steps):
                if str(item.get("step_id")) == current_id:
                    start_idx = idx
                    break

        for idx in range(start_idx + 1, len(steps)):
            status = str(steps[idx].get("status") or "pending")
            if status in {"pending", "running", "waiting_for_operator", "failed"}:
                session["current_step_id"] = steps[idx]["step_id"]
                return

        session["current_step_id"] = None
        self._complete_session(session)

    def _complete_session(self, session: Dict[str, Any]) -> None:
        if session.get("status") in {"cancelled", "failed", "completed"}:
            return
        session["status"] = "completed"
        session["ended_at"] = _utc_now().isoformat()
        self._event(
            session,
            source="orchestrator",
            level="info",
            message="Session completed.",
            metadata={},
        )

    def _mark_step_running(self, step: Dict[str, Any]) -> None:
        if not step.get("started_at"):
            step["started_at"] = _utc_now().isoformat()
        step["status"] = "running"
        step["error_summary"] = None

    def _mark_step_completed(self, session: Dict[str, Any], step: Dict[str, Any], *, details: Dict[str, Any]) -> None:
        if not step.get("started_at"):
            step["started_at"] = _utc_now().isoformat()
        step["ended_at"] = _utc_now().isoformat()
        step["duration_ms"] = _duration_ms(step.get("started_at"), step.get("ended_at"))
        step["status"] = "completed"
        step["error_summary"] = None
        session["step_details"][step["step_id"]] = dict(details or {})
        self._event(
            session,
            source="orchestrator",
            level="info",
            message=f"Step completed: {step['step_id']}",
            metadata={"duration_ms": step.get("duration_ms")},
        )

    def _mark_step_waiting_for_operator(
        self,
        session: Dict[str, Any],
        step: Dict[str, Any],
        *,
        details: Dict[str, Any],
    ) -> None:
        if not step.get("started_at"):
            step["started_at"] = _utc_now().isoformat()
        step["status"] = "waiting_for_operator"
        step["error_summary"] = None
        session["status"] = "waiting_for_operator"
        session["step_details"][step["step_id"]] = dict(details or {})
        self._event(
            session,
            source="operator",
            level="info",
            message=f"Waiting for operator confirmation at step: {step['step_id']}",
            metadata={"step_id": step["step_id"]},
        )

    def _mark_step_failed(
        self,
        session: Dict[str, Any],
        step: Dict[str, Any],
        message: str,
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not step.get("started_at"):
            step["started_at"] = _utc_now().isoformat()
        step["ended_at"] = _utc_now().isoformat()
        step["duration_ms"] = _duration_ms(step.get("started_at"), step.get("ended_at"))
        step["status"] = "failed"
        step["error_summary"] = str(message or "Unknown error")
        session["status"] = "failed"
        session["ended_at"] = _utc_now().isoformat()
        session.setdefault("step_details", {})
        merged = dict((session.get("step_details") or {}).get(step["step_id"]) or {})
        merged["error_summary"] = step["error_summary"]
        if details:
            merged["failure_details"] = dict(details)
        session["step_details"][step["step_id"]] = merged
        self._event(
            session,
            source="orchestrator",
            level="error",
            message=f"Step failed: {step['step_id']}",
            metadata={
                "error_summary": step["error_summary"],
                "failure_details": dict(details or {}),
            },
        )

    def _mark_step_skipped(self, session: Dict[str, Any], step: Dict[str, Any], *, reason: str) -> None:
        if not step.get("started_at"):
            step["started_at"] = _utc_now().isoformat()
        step["ended_at"] = _utc_now().isoformat()
        step["duration_ms"] = _duration_ms(step.get("started_at"), step.get("ended_at"))
        step["status"] = "skipped"
        step["error_summary"] = str(reason or "")
        self._event(
            session,
            source="orchestrator",
            level="warn",
            message=f"Step skipped: {step['step_id']}",
            metadata={"reason": reason},
        )

    def _skip_step_by_id(self, session: Dict[str, Any], step_id: str, *, reason: str) -> None:
        step = self._step_by_id(session, step_id)
        if not step:
            return
        if step.get("status") in {"completed", "skipped"}:
            return
        self._mark_step_skipped(session, step, reason=reason)

    def _event(
        self,
        session: Dict[str, Any],
        *,
        source: str,
        level: str,
        message: str,
        metadata: Dict[str, Any],
    ) -> None:
        event = {
            "timestamp": _utc_now().isoformat(),
            "source": str(source),
            "level": str(level),
            "message": str(message),
            "metadata": dict(metadata or {}),
        }
        events = list(session.get("events") or [])
        events.append(event)
        session["events"] = events[-500:]
        self.logger.log_event(session_id=str(session.get("session_id") or ""), event=event)

    def _artifact(
        self,
        session: Dict[str, Any],
        *,
        step_id: str,
        artifact_type: str,
        path: Optional[str],
        metadata: Dict[str, Any],
    ) -> str:
        artifacts = list(session.get("artifacts") or [])
        artifact_id = f"art_{len(artifacts) + 1:04d}"
        item = {
            "artifact_id": artifact_id,
            "step_id": step_id,
            "artifact_type": artifact_type,
            "path": (str(path) if path else None),
            "metadata": dict(metadata or {}),
        }
        artifacts.append(item)
        session["artifacts"] = artifacts
        step = self._step_by_id(session, step_id)
        if step is not None:
            refs = list(step.get("artifact_refs") or [])
            refs.append(artifact_id)
            step["artifact_refs"] = refs
        return artifact_id

    def _persist(self, session: Dict[str, Any]) -> None:
        session_id = str(session.get("session_id") or "").strip()
        snapshot_path = self.session_store.sessions_dir / f"{session_id}.json"
        session.setdefault("mode_info", {})
        session["mode_info"]["session_log_path"] = str(snapshot_path)
        self.session_store.save_session(session=session)


def _new_step(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "step_id": str(item["step_id"]),
        "name": str(item["name"]),
        "description": str(item["description"]),
        "status": "pending",
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
        "requires_operator_confirmation": bool(item.get("requires_operator_confirmation")),
        "retry_count": 0,
        "error_summary": None,
        "artifact_refs": [],
        "allow_skip": bool(item.get("allow_skip")),
    }


def _sanitize_session_for_persistence(raw: Dict[str, Any]) -> Dict[str, Any]:
    def _sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for k, v in value.items():
                key = str(k or "")
                low = key.lower()
                if any(token in low for token in SENSITIVE_KEYWORDS):
                    continue
                out[key] = _sanitize(v)
            return out
        if isinstance(value, list):
            return [_sanitize(v) for v in value]
        if isinstance(value, tuple):
            return [_sanitize(v) for v in value]
        return value

    return _sanitize(dict(raw or {}))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_ts(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def _duration_ms(start: Optional[str], end: Optional[str]) -> Optional[int]:
    if not start or not end:
        return None
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
        return int((e - s).total_seconds() * 1000)
    except Exception:
        return None


def _to_plain_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {"value": str(value)}


def _truncate(text: str, *, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 12)] + "\n...[truncated]"


def _extract_failure_details(error: Exception) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "error_type": type(error).__name__,
        "error_message": str(error),
    }
    if isinstance(error, AdvisoryError):
        errs = [str(x) for x in list(error.errors or []) if str(x).strip()]
        if errs:
            out["validation_failures"] = errs
        out["status_code"] = int(error.status_code)
        return out

    raw_errors = getattr(error, "errors", None)
    if isinstance(raw_errors, list):
        errs = [str(x) for x in raw_errors if str(x).strip()]
        if errs:
            out["validation_failures"] = errs

    status_code = getattr(error, "status_code", None)
    try:
        if status_code is not None:
            out["status_code"] = int(status_code)
    except Exception:
        pass
    return out
