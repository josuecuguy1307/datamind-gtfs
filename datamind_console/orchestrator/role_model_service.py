"""LEGACY / REFERENCE — Original role-model service for authority validation.

This module is part of the v1 orchestration system (session_runner + service + role_model_service).
The active runtime is pipeline_autopilot.py + operator_orchestrator_view.py.
Retained because active tests verify authority-model invariants against this code.
Do NOT use for new features — all new orchestration goes through SupervisedPipelineAutopilot.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from datamind_console.orchestrator.artifact_registry import ArtifactRegistry
from datamind_console.orchestrator.operator_labels import validate_operator_labels
from datamind_console.orchestrator.role_policy import (
    ACTION_ADVISORY_ANALYZE,
    ACTION_APPLY_GENERATED_PATCH,
    ACTION_DESTRUCTIVE_CLEANUP_EXECUTE,
    ACTION_DISPATCH_PATCH_TASK,
    ACTION_GENERATE_PATCH_TASK,
    ACTION_MERGE_BIND,
    ACTION_PIPELINE_EXECUTE,
    ACTION_RUNNER_DISPATCH,
    ACTION_SEQUENCE_REORDER_APPLY,
    ACTION_TELEMETRY_SCORE,
    ACTION_TELEMETRY_SNAPSHOT,
    ROLE_AI_BOT,
    ROLE_ASSISTANT_RUNNER,
    ROLE_CHATGPT_API,
    ROLE_VALIDATORS,
    ROLE_OPERATOR,
    ROLE_RUNTIME,
    RolePolicyError,
    check_permission,
)
from datamind_console.orchestrator.session_event_log import SessionEventLogger
from datamind_console.orchestrator.session_state_machine import (
    STEP_CONTRACT_BY_KEY,
    assert_step_prerequisite,
    can_transition_state,
    new_step_records,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_dict(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_summary(payload: dict, *, max_chars: int = 220) -> dict:
    text = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    return {
        "preview": text[:max_chars],
        "size": len(text),
    }


@dataclass
class FixtureAiBotAdapter:
    def load_snapshot(self, *, snapshot_source_mode: str, session_context: dict) -> dict:
        del session_context
        mode = str(snapshot_source_mode or "fixture")
        return {
            "snapshot_id": f"snap_{uuid4().hex[:10]}",
            "snapshot_source_mode": mode,
            "telemetry": {
                "phase1": {"coverage": 0.93},
                "phase2": {"coverage": 0.81},
                "phase3": {"coverage": 0.56},
            },
            "warnings": ["low_phase3_label_coverage"],
        }

    def health_check(self, *, snapshot: dict) -> dict:
        phase3_cov = float((((snapshot or {}).get("telemetry") or {}).get("phase3") or {}).get("coverage") or 0.0)
        if phase3_cov < 0.2:
            return {
                "status": "blocked",
                "warnings": ["phase3_coverage_critical"],
                "readiness": {"phase3_label_coverage": phase3_cov},
            }
        if phase3_cov < 0.65:
            return {
                "status": "ok_with_warnings",
                "warnings": ["phase3_coverage_low"],
                "readiness": {"phase3_label_coverage": phase3_cov},
            }
        return {
            "status": "ok",
            "warnings": [],
            "readiness": {"phase3_label_coverage": phase3_cov},
        }


@dataclass
class FixtureAdvisoryAdapter:
    def analyze_latest_run(self, *, snapshot: dict, advisory_only: bool) -> dict:
        return {
            "advisory_only": bool(advisory_only),
            "summary": "Recent runs show sequence uncertainty concentrated in low-coverage corridors.",
            "top_issues": [
                "phase3_sequence_warning_density_high",
                "operator_labels_missing_recent_runs",
            ],
            "suggested_actions": [
                "prioritize_unmatched_stop_resolution",
                "capture_operator_labels_for_last_5_runs",
            ],
            "confidence": 0.67,
            "risk_flags": ["advisory_only", "no_runtime_execution"],
            "snapshot_ref": str(snapshot.get("snapshot_id") or ""),
        }

    def review_ai_bot_quality(self, *, snapshot: dict, advisory_only: bool) -> dict:
        del snapshot
        return {
            "advisory_only": bool(advisory_only),
            "quality_posture": "useful_but_incomplete",
            "gaps": [
                "phase3_warning_label_coverage",
                "merge_decision_ground_truth",
            ],
            "tuning_suggestions": [
                "expand_warning_correctness_labels",
                "capture_merge_decision_quality_label",
            ],
        }

    def generate_codex_patch_task(self, *, snapshot: dict, advisory_only: bool) -> dict:
        return {
            "advisory_only": bool(advisory_only),
            "problem": "Sequence warning explanations are too coarse for operator action.",
            "evidence": [
                "high warning volume in phase3 sequence step",
                "low confidence during reorder proposals",
            ],
            "constraints": [
                "no_gate_bypass",
                "no_auto_merge_bind",
                "no_auto_reorder_commit",
                "no_auto_apply_generated_code",
            ],
            "target_files": [
                "datamind_console/views/operator_orchestrator_view.py",
                "datamind_console/orchestrator/session_runner.py",
            ],
            "acceptance_criteria": [
                "warning subtypes are explicit",
                "operator review evidence is attached per warning",
            ],
            "prompt_text": (
                "Implement warning subtype classification and ensure operator-facing evidence references are "
                "available before any reorder/merge decision checkpoints."
            ),
            "snapshot_ref": str(snapshot.get("snapshot_id") or ""),
        }

    def hades_patch_task_generator(self, *, snapshot: dict, advisory_only: bool) -> dict:
        return self.generate_codex_patch_task(snapshot=snapshot, advisory_only=advisory_only)


@dataclass
class FixtureRunnerAdapter:
    logs_dir: Path

    def dispatch(self, *, target: str, prompt_text: str, session_id: str) -> dict:
        target_name = str(target or "none").strip().lower()
        out_dir = Path(self.logs_dir).resolve() / "runner_outputs"
        out_dir.mkdir(parents=True, exist_ok=True)

        prompt_path = out_dir / f"{session_id}_{target_name}.prompt.txt"
        stdout_path = out_dir / f"{session_id}_{target_name}.stdout.txt"
        stderr_path = out_dir / f"{session_id}_{target_name}.stderr.txt"

        prompt_path.write_text(str(prompt_text or ""), encoding="utf-8")
        stdout_path.write_text(f"DISPATCH_OK target={target_name}\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        return {
            "status": "success",
            "target": target_name,
            "exit_code": 0,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "prompt_path": str(prompt_path),
        }


class RoleModelService:
    """Role-based orchestrator backend with explicit policy and state guardrails."""

    def __init__(
        self,
        *,
        logs_dir: Optional[Path] = None,
        ai_bot_adapter: Any | None = None,
        advisory_adapter: Any | None = None,
        runner_adapter: Any | None = None,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        self.logs_dir = Path(logs_dir or (repo_root / "datamind_console" / "orchestrator_logs" / "v2_role_model")).resolve()
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.events = SessionEventLogger(base_dir=self.logs_dir)
        self.artifacts = ArtifactRegistry(base_dir=self.logs_dir / "artifacts")

        self.ai_bot = ai_bot_adapter or FixtureAiBotAdapter()
        self.advisory = advisory_adapter or FixtureAdvisoryAdapter()
        self.runner = runner_adapter or FixtureRunnerAdapter(logs_dir=self.logs_dir)

        self._sessions: Dict[str, dict] = {}

    # ---------------------------------------------------------------------
    # Session lifecycle
    # ---------------------------------------------------------------------
    def start_guided_workflow(
        self,
        *,
        operator_id: str,
        template: str = "AI_ASSISTED_REVIEW_PATCH_TASK",
        snapshot_source_mode: str = "fixture",
        preferred_target: str = "codex",
    ) -> dict:
        session_id = f"orchv2_{uuid4().hex[:12]}"
        now = _utc_now_iso()
        session = {
            "session_id": session_id,
            "template": str(template or "AI_ASSISTED_REVIEW_PATCH_TASK"),
            "mode": {
                "snapshot_source_mode": str(snapshot_source_mode or "fixture"),
                "preferred_target": str(preferred_target or "none").strip().lower(),
                "advisory_mode": True,
                "execution_mode": "runtime_validators_authority",
            },
            "status": "running",
            "state": "session_created",
            "operator_id": str(operator_id or "operator"),
            "created_at": now,
            "updated_at": now,
            "steps": new_step_records(),
            "artifacts": [],
            "context": {
                "dispatch_decision": None,
                "patch_task_hash": None,
                "advisory_only": True,
                "legacy_orchestrator_enabled": False,
                "health_blocked": False,
                "health_override": None,
                "phase_operations": [],
            },
        }
        self._sessions[session_id] = session
        self._log(
            session,
            step_key="session_start",
            event_type="session_created",
            actor_role=ROLE_OPERATOR,
            action=ACTION_DISPATCH_PATCH_TASK,
            status="ok",
            actor_id=operator_id,
            payload_summary={"template": session["template"]},
        )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict:
        sid = str(session_id or "").strip()
        if sid not in self._sessions:
            raise KeyError(f"session not found: {sid}")
        return copy.deepcopy(self._sessions[sid])

    def run_to_operator_checkpoint(self, session_id: str) -> dict:
        session = self._session(session_id)

        self._run_step(
            session,
            step_key="session_start",
            actor_role=ROLE_RUNTIME,
            action=ACTION_PIPELINE_EXECUTE,
            handler=lambda: {"message": "guided session initialized"},
        )

        self._run_step(
            session,
            step_key="define_work_mode",
            actor_role=ROLE_RUNTIME,
            action=ACTION_PIPELINE_EXECUTE,
            handler=lambda: {
                "template": session["template"],
                "mode": dict(session["mode"]),
            },
        )

        self._run_step(
            session,
            step_key="load_ai_bot_snapshot",
            actor_role=ROLE_AI_BOT,
            action=ACTION_TELEMETRY_SNAPSHOT,
            handler=lambda: self._handle_snapshot(session),
        )

        health_result = self._run_step(
            session,
            step_key="verify_ai_bot_health",
            actor_role=ROLE_AI_BOT,
            action=ACTION_TELEMETRY_SCORE,
            handler=lambda: self._handle_ai_bot_health(session),
        )
        if str((health_result or {}).get("status") or "") == "blocked":
            session["state"] = "blocked"
            session["status"] = "blocked"
            session.setdefault("context", {})["health_blocked"] = True
            session["updated_at"] = _utc_now_iso()
            return self.get_session(session["session_id"])

        self._run_step(
            session,
            step_key="analyze_latest_run",
            actor_role=ROLE_CHATGPT_API,
            action=ACTION_ADVISORY_ANALYZE,
            handler=lambda: self._handle_analyze_latest_run(session),
        )

        self._run_step(
            session,
            step_key="review_ai_bot_quality",
            actor_role=ROLE_CHATGPT_API,
            action=ACTION_ADVISORY_ANALYZE,
            handler=lambda: self._handle_review_ai_bot_quality(session),
        )

        self._run_step(
            session,
            step_key="generate_codex_patch_task",
            actor_role=ROLE_CHATGPT_API,
            action=ACTION_GENERATE_PATCH_TASK,
            handler=lambda: self._handle_generate_patch_task(session),
        )

        step = self._step(session, "operator_dispatch_checkpoint")
        step["status"] = "waiting_for_operator"
        step["started_at"] = _utc_now_iso()
        if not can_transition_state(str(session.get("state") or ""), "waiting_for_operator", allow_same=False):
            raise ValueError("Invalid transition to waiting_for_operator.")
        session["state"] = "waiting_for_operator"
        session["updated_at"] = _utc_now_iso()
        self._log(
            session,
            step_key="operator_dispatch_checkpoint",
            event_type="checkpoint_waiting",
            actor_role=ROLE_RUNTIME,
            action=ACTION_PIPELINE_EXECUTE,
            status="waiting_for_operator",
            payload_summary={
                "required_actions": [
                    "dispatch_to_codex",
                    "dispatch_to_claude",
                    "edit_prompt_first",
                    "stop_session",
                ]
            },
        )

        return self.get_session(session["session_id"])

    def override_blocked_health(self, session_id: str, *, operator_id: str, reason: str) -> dict:
        session = self._session(session_id)
        if str(session.get("state") or "") != "blocked":
            raise ValueError("Health override is only valid when session is blocked.")

        health_step = self._step(session, "verify_ai_bot_health")
        out = dict(health_step.get("output") or {})
        if str(out.get("status") or "").strip().lower() != "blocked":
            raise ValueError("Session is not blocked by AI Bot health check.")

        justification = str(reason or "").strip()
        if not justification:
            raise ValueError("Health override requires a non-empty operator reason.")

        session["context"]["health_override"] = {
            "operator_id": str(operator_id or "operator"),
            "reason": justification,
            "timestamp": _utc_now_iso(),
        }
        session["context"]["health_blocked"] = False
        session["state"] = "ai_bot_health_checked"
        session["status"] = "running"
        session["updated_at"] = _utc_now_iso()

        self._log(
            session,
            step_key="verify_ai_bot_health",
            event_type="operator_health_override",
            actor_role=ROLE_OPERATOR,
            actor_id=operator_id,
            action=ACTION_DISPATCH_PATCH_TASK,
            status="ok",
            payload_summary={
                "override_reason": justification,
                "blocked_status": "overridden",
            },
        )
        return self.get_session(session_id)

    def continue_after_health_override(self, session_id: str) -> dict:
        session = self._session(session_id)
        if str(session.get("state") or "") != "ai_bot_health_checked":
            raise ValueError("Session is not ready to continue after health override.")
        if not session.get("context", {}).get("health_override"):
            raise ValueError("Health override not recorded; cannot continue.")

        self._run_step(
            session,
            step_key="analyze_latest_run",
            actor_role=ROLE_CHATGPT_API,
            action=ACTION_ADVISORY_ANALYZE,
            handler=lambda: self._handle_analyze_latest_run(session),
        )
        self._run_step(
            session,
            step_key="review_ai_bot_quality",
            actor_role=ROLE_CHATGPT_API,
            action=ACTION_ADVISORY_ANALYZE,
            handler=lambda: self._handle_review_ai_bot_quality(session),
        )
        self._run_step(
            session,
            step_key="generate_codex_patch_task",
            actor_role=ROLE_CHATGPT_API,
            action=ACTION_GENERATE_PATCH_TASK,
            handler=lambda: self._handle_generate_patch_task(session),
        )

        step = self._step(session, "operator_dispatch_checkpoint")
        step["status"] = "waiting_for_operator"
        step["started_at"] = _utc_now_iso()
        if not can_transition_state(str(session.get("state") or ""), "waiting_for_operator", allow_same=False):
            raise ValueError("Invalid transition to waiting_for_operator.")
        session["state"] = "waiting_for_operator"
        session["updated_at"] = _utc_now_iso()
        self._log(
            session,
            step_key="operator_dispatch_checkpoint",
            event_type="checkpoint_waiting",
            actor_role=ROLE_RUNTIME,
            action=ACTION_PIPELINE_EXECUTE,
            status="waiting_for_operator",
            payload_summary={"path": "health_override"},
        )
        return self.get_session(session_id)

    def submit_dispatch_decision(
        self,
        session_id: str,
        *,
        operator_id: str,
        decision: str,
        edited_prompt: Optional[str] = None,
    ) -> dict:
        session = self._session(session_id)
        if session.get("state") != "waiting_for_operator":
            raise ValueError("Dispatch decision is only valid at waiting_for_operator state.")

        normalized = str(decision or "").strip().lower()
        if normalized not in {"dispatch_to_codex", "dispatch_to_claude", "edit_prompt_first", "stop_session"}:
            raise ValueError(f"Invalid decision: {decision}")

        policy = check_permission(
            ROLE_OPERATOR,
            ACTION_DISPATCH_PATCH_TASK,
            operator_confirmed=True,
        )
        self._log(
            session,
            step_key="operator_dispatch_checkpoint",
            event_type="operator_decision",
            actor_role=ROLE_OPERATOR,
            actor_id=operator_id,
            action=ACTION_DISPATCH_PATCH_TASK,
            status=("ok" if policy.allowed else "denied"),
            payload_summary={
                "decision": normalized,
                "patch_task_hash": session.get("context", {}).get("patch_task_hash"),
            },
            policy_check_result=policy.to_dict(),
            error_code=(None if policy.allowed else policy.code),
            error_message=(None if policy.allowed else policy.message),
        )
        if not policy.allowed:
            raise RolePolicyError(policy)

        step = self._step(session, "operator_dispatch_checkpoint")
        step["status"] = "success"
        step["ended_at"] = _utc_now_iso()
        session["context"]["dispatch_decision"] = normalized
        session["updated_at"] = _utc_now_iso()

        if normalized == "stop_session":
            for key in ["prepare_prompt_artifact", "dispatch_to_assistant", "review_assistant_output", "apply_patch_and_retest"]:
                s = self._step(session, key)
                if s["status"] == "pending":
                    s["status"] = "blocked"
                    s["error_summary"] = "operator_stop_session"
            if not can_transition_state(str(session.get("state") or ""), "session_closed", allow_same=False):
                raise ValueError("Invalid transition to session_closed.")
            session["state"] = "session_closed"
            session["status"] = "completed"
            return self.get_session(session_id)

        target = "codex"
        if normalized == "dispatch_to_claude":
            target = "claude"
        if normalized == "edit_prompt_first":
            target = str(session.get("mode", {}).get("preferred_target") or "codex")
        session["context"]["dispatch_target"] = target

        patch_task = dict(session.get("context", {}).get("patch_task") or {})
        prompt_text = str(patch_task.get("prompt_text") or "").strip()
        if edited_prompt is not None and str(edited_prompt).strip():
            prompt_text = str(edited_prompt).strip()
        if not prompt_text:
            raise ValueError("Patch prompt text is empty; cannot dispatch.")

        self._run_step(
            session,
            step_key="prepare_prompt_artifact",
            actor_role=ROLE_ASSISTANT_RUNNER,
            action=ACTION_RUNNER_DISPATCH,
            handler=lambda: self._handle_prepare_prompt(session, prompt_text),
        )

        self._run_step(
            session,
            step_key="dispatch_to_assistant",
            actor_role=ROLE_ASSISTANT_RUNNER,
            action=ACTION_RUNNER_DISPATCH,
            handler=lambda: self._handle_dispatch(session, target=target, prompt_text=prompt_text),
        )

        review_step = self._step(session, "review_assistant_output")
        review_step["status"] = "waiting_for_operator"
        review_step["started_at"] = _utc_now_iso()
        if not can_transition_state(str(session.get("state") or ""), "awaiting_patch_review", allow_same=False):
            raise ValueError("Invalid transition to awaiting_patch_review.")
        session["state"] = "awaiting_patch_review"
        session["updated_at"] = _utc_now_iso()
        self._log(
            session,
            step_key="review_assistant_output",
            event_type="checkpoint_waiting",
            actor_role=ROLE_RUNTIME,
            action=ACTION_PIPELINE_EXECUTE,
            status="waiting_for_operator",
            payload_summary={"target": target},
        )

        return self.get_session(session_id)

    def record_patch_review(
        self,
        session_id: str,
        *,
        operator_id: str,
        outcome: str,
        notes: str,
        commit_hash: Optional[str] = None,
    ) -> dict:
        session = self._session(session_id)
        if session.get("state") != "awaiting_patch_review":
            raise ValueError("Patch review can only be recorded at awaiting_patch_review state.")

        normalized = str(outcome or "").strip().lower()
        if normalized not in {"applied", "rejected", "needs_revision"}:
            raise ValueError(f"Invalid patch review outcome: {outcome}")

        policy = check_permission(ROLE_OPERATOR, ACTION_APPLY_GENERATED_PATCH, operator_confirmed=True)
        self._log(
            session,
            step_key="apply_patch_and_retest",
            event_type="operator_patch_review",
            actor_role=ROLE_OPERATOR,
            actor_id=operator_id,
            action=ACTION_APPLY_GENERATED_PATCH,
            status=("ok" if policy.allowed else "denied"),
            payload_summary={"outcome": normalized, "commit_hash": commit_hash},
            policy_check_result=policy.to_dict(),
            error_code=(None if policy.allowed else policy.code),
            error_message=(None if policy.allowed else policy.message),
        )
        if not policy.allowed:
            raise RolePolicyError(policy)

        review_step = self._step(session, "review_assistant_output")
        review_step["status"] = "success"
        review_step["ended_at"] = _utc_now_iso()

        self._run_step(
            session,
            step_key="apply_patch_and_retest",
            actor_role=ROLE_OPERATOR,
            action=ACTION_APPLY_GENERATED_PATCH,
            operator_confirmed=True,
            actor_id=operator_id,
            handler=lambda: {
                "outcome": normalized,
                "notes": str(notes or ""),
                "commit_hash": (str(commit_hash).strip() if commit_hash else None),
            },
        )

        review_artifact = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="operator_review_notes",
            content={
                "outcome": normalized,
                "notes": str(notes or ""),
                "commit_hash": (str(commit_hash).strip() if commit_hash else None),
            },
        )
        session["artifacts"].append(review_artifact)
        session["state"] = "patch_applied_or_rejected"
        session["updated_at"] = _utc_now_iso()

        return self.get_session(session_id)

    def record_retest_results(
        self,
        session_id: str,
        *,
        operator_id: str,
        tests_run: list[str],
        passed: bool,
        notes: Optional[str] = None,
    ) -> dict:
        session = self._session(session_id)
        if session.get("state") not in {"patch_applied_or_rejected", "retest_recorded"}:
            raise ValueError("Retest results require patch review completion first.")

        self._run_step(
            session,
            step_key="operate_phase_workflows",
            actor_role=ROLE_RUNTIME,
            action=ACTION_PIPELINE_EXECUTE,
            operator_confirmed=True,
            actor_id=operator_id,
            handler=lambda: {
                "tests_run": list(tests_run or []),
                "passed": bool(passed),
                "notes": str(notes or ""),
            },
        )

        test_artifact = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="test_results",
            content={
                "tests_run": list(tests_run or []),
                "passed": bool(passed),
                "notes": str(notes or ""),
            },
        )
        session["artifacts"].append(test_artifact)
        session["updated_at"] = _utc_now_iso()
        return self.get_session(session_id)

    def capture_operator_labels(
        self,
        session_id: str,
        *,
        operator_id: str,
        labels: dict,
    ) -> dict:
        session = self._session(session_id)
        validated = validate_operator_labels(dict(labels or {}))
        if not validated.valid:
            raise ValueError(validated.message)
        normalized = dict(validated.normalized)

        self._run_step(
            session,
            step_key="capture_operator_labels",
            actor_role=ROLE_OPERATOR,
            action=ACTION_APPLY_GENERATED_PATCH,
            operator_confirmed=True,
            actor_id=operator_id,
            handler=lambda: {"labels": dict(normalized)},
        )

        label_artifact = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="labels_jsonl_ref",
            content={
                "labels": dict(normalized),
                "operator_id": str(operator_id or "operator"),
            },
        )
        session["artifacts"].append(label_artifact)

        label_payload_artifact = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="operator_label",
            content={
                "labels": dict(normalized),
                "operator_id": str(operator_id or "operator"),
            },
        )
        session["artifacts"].append(label_payload_artifact)
        session["state"] = "retest_recorded"
        session["updated_at"] = _utc_now_iso()
        return self.get_session(session_id)

    def attach_gtfs_gold_refs(
        self,
        session_id: str,
        *,
        manifest_ref: str,
        dataset_jsonl_ref: str,
        combined_metadata: Optional[dict] = None,
    ) -> dict:
        session = self._session(session_id)
        a0 = self.artifacts.register(
            session_id=session_id,
            artifact_type="gtfs_gold_manifest",
            content={"manifest_ref": str(manifest_ref or "").strip()},
        )
        a1 = self.artifacts.register(
            session_id=session_id,
            artifact_type="gtfs_gold_manifest_ref",
            content={"manifest_ref": str(manifest_ref or "").strip()},
        )
        a2 = self.artifacts.register(
            session_id=session_id,
            artifact_type="gtfs_gold_jsonl_ref",
            content={"dataset_jsonl_ref": str(dataset_jsonl_ref or "").strip()},
        )
        a3 = self.artifacts.register(
            session_id=session_id,
            artifact_type="combined_labels_metadata",
            content=dict(combined_metadata or {}),
        )
        session["artifacts"].extend([a0, a1, a2, a3])
        session["updated_at"] = _utc_now_iso()
        return self.get_session(session_id)

    def attach_model_improvement_hooks(self, session_id: str, *, hooks: dict) -> dict:
        session = self._session(session_id)
        a = self.artifacts.register(
            session_id=session_id,
            artifact_type="model_improvement_hooks",
            content=dict(hooks or {}),
        )
        session["artifacts"].append(a)
        session["updated_at"] = _utc_now_iso()
        return self.get_session(session_id)

    def close_session(self, session_id: str, *, operator_id: str) -> dict:
        session = self._session(session_id)
        summary = self._run_step(
            session,
            step_key="session_summary",
            actor_role=ROLE_RUNTIME,
            action=ACTION_PIPELINE_EXECUTE,
            handler=lambda: self._build_session_summary(session),
            actor_id=operator_id,
        )
        summary_artifact = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="session_summary",
            content=summary,
        )
        session["artifacts"].append(summary_artifact)
        session["state"] = "session_closed"
        session["status"] = "completed"
        session["updated_at"] = _utc_now_iso()
        self._log(
            session,
            step_key="session_summary",
            event_type="session_closed",
            actor_role=ROLE_OPERATOR,
            actor_id=operator_id,
            action=ACTION_DISPATCH_PATCH_TASK,
            status="ok",
            payload_summary={"artifacts": len(session.get("artifacts") or [])},
        )
        return self.get_session(session_id)

    # ------------------------------------------------------------------
    # Critical-action wrappers (for boundary checks)
    # ------------------------------------------------------------------
    def request_merge_bind(self, *, actor_role: str, operator_confirmed: bool) -> dict:
        result = check_permission(actor_role, ACTION_MERGE_BIND, operator_confirmed=operator_confirmed)
        if not result.allowed:
            raise RolePolicyError(result)
        return result.to_dict()

    def request_sequence_reorder_apply(self, *, actor_role: str, operator_confirmed: bool) -> dict:
        result = check_permission(actor_role, ACTION_SEQUENCE_REORDER_APPLY, operator_confirmed=operator_confirmed)
        if not result.allowed:
            raise RolePolicyError(result)
        return result.to_dict()

    def request_destructive_cleanup(self, *, actor_role: str, operator_confirmed: bool) -> dict:
        result = check_permission(actor_role, ACTION_DESTRUCTIVE_CLEANUP_EXECUTE, operator_confirmed=operator_confirmed)
        if not result.allowed:
            raise RolePolicyError(result)
        return result.to_dict()

    def record_phase_operation(
        self,
        session_id: str,
        *,
        actor_role: str,
        phase: str,
        operation: str,
        operator_confirmed: bool,
        details: Optional[dict] = None,
    ) -> dict:
        session = self._session(session_id)
        if str(session.get("state") or "") not in {"patch_applied_or_rejected", "retest_recorded"}:
            raise ValueError("Phase operations can only be recorded after patch review.")

        phase_name = str(phase or "").strip().lower()
        if phase_name not in {"phase1", "phase2", "phase3"}:
            raise ValueError(f"Unsupported phase: {phase}")
        if actor_role not in {ROLE_RUNTIME, ROLE_VALIDATORS}:
            raise ValueError("Only runtime/validators may execute phase operations.")

        op = str(operation or "").strip().lower()
        action = ACTION_PIPELINE_EXECUTE
        if op == "merge_bind":
            action = ACTION_MERGE_BIND
        elif op == "sequence_reorder_apply":
            action = ACTION_SEQUENCE_REORDER_APPLY
        elif op == "destructive_cleanup_execute":
            action = ACTION_DESTRUCTIVE_CLEANUP_EXECUTE

        policy = check_permission(actor_role, action, operator_confirmed=operator_confirmed)
        if not policy.allowed:
            raise RolePolicyError(policy)

        payload = {
            "phase": phase_name,
            "operation": op,
            "actor_role": actor_role,
            "operator_confirmed": bool(operator_confirmed),
            "details": dict(details or {}),
            "timestamp": _utc_now_iso(),
        }
        session.setdefault("context", {}).setdefault("phase_operations", []).append(payload)
        session["updated_at"] = _utc_now_iso()

        self._log(
            session,
            step_key="operate_phase_workflows",
            event_type="phase_operation_recorded",
            actor_role=actor_role,
            action=action,
            status="ok",
            payload_summary=_safe_summary(payload),
            policy_check_result=policy.to_dict(),
        )
        return copy.deepcopy(payload)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _session(self, session_id: str) -> dict:
        sid = str(session_id or "").strip()
        if sid not in self._sessions:
            raise KeyError(f"session not found: {sid}")
        return self._sessions[sid]

    def _step(self, session: dict, step_key: str) -> dict:
        key = str(step_key or "").strip()
        for row in session.get("steps") or []:
            if str(row.get("step_key") or "") == key:
                return row
        raise KeyError(f"step not found: {key}")

    def _run_step(
        self,
        session: dict,
        *,
        step_key: str,
        actor_role: str,
        action: str,
        handler: Any,
        operator_confirmed: bool = False,
        actor_id: Optional[str] = None,
    ) -> dict:
        step = self._step(session, step_key)
        if step.get("status") == "success":
            return dict(step.get("output") or {})

        contract = STEP_CONTRACT_BY_KEY.get(step_key)
        current_state = str(session.get("state") or "")
        if contract is not None:
            try:
                assert_step_prerequisite(current_state, step_key)
            except Exception as exc:
                step["status"] = "blocked"
                step["error_summary"] = str(exc)
                step["ended_at"] = _utc_now_iso()
                session["state"] = "blocked"
                session["status"] = "blocked"
                session["updated_at"] = _utc_now_iso()
                self._log(
                    session,
                    step_key=step_key,
                    event_type="state_prerequisite_denied",
                    actor_role=actor_role,
                    actor_id=actor_id,
                    action=action,
                    status="blocked",
                    error_code="invalid_state_prerequisite",
                    error_message=str(exc),
                )
                raise

        policy = check_permission(actor_role, action, operator_confirmed=operator_confirmed)
        self._log(
            session,
            step_key=step_key,
            event_type="policy_check",
            actor_role=actor_role,
            actor_id=actor_id,
            action=action,
            status=("ok" if policy.allowed else "denied"),
            policy_check_result=policy.to_dict(),
            error_code=(None if policy.allowed else policy.code),
            error_message=(None if policy.allowed else policy.message),
        )

        if not policy.allowed:
            step["status"] = "blocked"
            step["error_summary"] = policy.message
            step["ended_at"] = _utc_now_iso()
            session["state"] = "blocked"
            session["status"] = "blocked"
            session["updated_at"] = _utc_now_iso()
            raise RolePolicyError(policy)

        step["status"] = "running"
        step["started_at"] = _utc_now_iso()
        session["updated_at"] = _utc_now_iso()

        self._log(
            session,
            step_key=step_key,
            event_type="step_started",
            actor_role=actor_role,
            actor_id=actor_id,
            action=action,
            status="running",
        )

        try:
            output = dict(handler() or {})
        except Exception as exc:
            step["status"] = "failed"
            step["error_summary"] = str(exc)
            step["ended_at"] = _utc_now_iso()
            session["state"] = "failed"
            session["status"] = "failed"
            session["updated_at"] = _utc_now_iso()
            self._log(
                session,
                step_key=step_key,
                event_type="step_failed",
                actor_role=actor_role,
                actor_id=actor_id,
                action=action,
                status="failed",
                error_code="step_execution_error",
                error_message=str(exc),
            )
            raise

        step["status"] = "success"
        step["ended_at"] = _utc_now_iso()
        step["error_summary"] = None
        step["output"] = dict(output)

        if contract is not None:
            next_state = str(contract.state_after_success)
            if not can_transition_state(current_state, next_state, allow_same=True):
                step["status"] = "failed"
                step["error_summary"] = (
                    f"Invalid state transition: {current_state} -> {next_state} for `{step_key}`."
                )
                session["state"] = "failed"
                session["status"] = "failed"
                session["updated_at"] = _utc_now_iso()
                self._log(
                    session,
                    step_key=step_key,
                    event_type="state_transition_denied",
                    actor_role=actor_role,
                    actor_id=actor_id,
                    action=action,
                    status="failed",
                    error_code="invalid_state_transition",
                    error_message=str(step["error_summary"]),
                )
                raise ValueError(str(step["error_summary"]))
            session["state"] = next_state
        session["updated_at"] = _utc_now_iso()

        self._log(
            session,
            step_key=step_key,
            event_type="step_completed",
            actor_role=actor_role,
            actor_id=actor_id,
            action=action,
            status="success",
            payload_summary=_safe_summary(output),
        )

        return output

    def _handle_snapshot(self, session: dict) -> dict:
        out = self.ai_bot.load_snapshot(
            snapshot_source_mode=str(session.get("mode", {}).get("snapshot_source_mode") or "fixture"),
            session_context={"session_id": session.get("session_id")},
        )
        artifact = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="snapshot_json",
            content=out,
        )
        session["artifacts"].append(artifact)
        session.setdefault("context", {})["snapshot"] = dict(out)
        return out

    def _handle_ai_bot_health(self, session: dict) -> dict:
        snapshot = dict((session.get("context") or {}).get("snapshot") or {})
        out = self.ai_bot.health_check(snapshot=snapshot)
        status = str(out.get("status") or "").strip().lower()
        if status not in {"ok", "ok_with_warnings", "blocked"}:
            raise ValueError("AI Bot health status must be one of ok|ok_with_warnings|blocked")
        artifact = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="ai_bot_health_report",
            content=out,
        )
        session["artifacts"].append(artifact)
        return out

    def _handle_analyze_latest_run(self, session: dict) -> dict:
        snapshot = dict((session.get("context") or {}).get("snapshot") or {})
        out = self.advisory.analyze_latest_run(snapshot=snapshot, advisory_only=True)
        self._validate_analysis_output(out)
        artifact = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="chatgpt_analysis",
            content=out,
        )
        session["artifacts"].append(artifact)
        return out

    def _handle_review_ai_bot_quality(self, session: dict) -> dict:
        snapshot = dict((session.get("context") or {}).get("snapshot") or {})
        out = self.advisory.review_ai_bot_quality(snapshot=snapshot, advisory_only=True)
        self._validate_quality_review_output(out)
        artifact = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="chatgpt_ai_bot_review",
            content=out,
        )
        session["artifacts"].append(artifact)
        return out

    def _handle_generate_patch_task(self, session: dict) -> dict:
        snapshot = dict((session.get("context") or {}).get("snapshot") or {})
        if hasattr(self.advisory, "hades_patch_task_generator"):
            out = self.advisory.hades_patch_task_generator(snapshot=snapshot, advisory_only=True)
        else:
            out = self.advisory.generate_codex_patch_task(snapshot=snapshot, advisory_only=True)
        self._validate_patch_task_output(out)
        patch_hash = _hash_dict(out)
        session.setdefault("context", {})["patch_task"] = dict(out)
        session["context"]["patch_task_hash"] = patch_hash
        artifact = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="patch_task_json",
            content=out,
            metadata={"patch_task_hash": patch_hash},
        )
        session["artifacts"].append(artifact)
        return {**dict(out), "patch_task_hash": patch_hash}

    def _handle_prepare_prompt(self, session: dict, prompt_text: str) -> dict:
        out = {
            "prompt_text": str(prompt_text or ""),
            "prompt_chars": len(str(prompt_text or "")),
        }
        artifact = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="patch_prompt_text",
            content=out,
            metadata={"target": str((session.get("context") or {}).get("dispatch_target") or "")},
        )
        session["artifacts"].append(artifact)
        return out

    def _handle_dispatch(self, session: dict, *, target: str, prompt_text: str) -> dict:
        out = self.runner.dispatch(
            target=target,
            prompt_text=prompt_text,
            session_id=str(session.get("session_id") or ""),
        )
        status = str(out.get("status") or "").strip().lower()
        if status != "success":
            raise RuntimeError(out.get("error") or "runner dispatch failed")

        meta_artifact = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="runner_dispatch_meta",
            content=out,
        )
        session["artifacts"].append(meta_artifact)

        stdout_ref = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="runner_stdout",
            source_path=str(out.get("stdout_path") or ""),
            metadata={"target": str(target)},
        )
        session["artifacts"].append(stdout_ref)

        stderr_ref = self.artifacts.register(
            session_id=session["session_id"],
            artifact_type="runner_stderr",
            source_path=str(out.get("stderr_path") or ""),
            metadata={"target": str(target)},
        )
        session["artifacts"].append(stderr_ref)

        return out

    def _build_session_summary(self, session: dict) -> dict:
        steps = list(session.get("steps") or [])
        phase_ops = list((session.get("context") or {}).get("phase_operations") or [])
        return {
            "session_id": session.get("session_id"),
            "state": session.get("state"),
            "status": session.get("status"),
            "steps_total": len(steps),
            "steps_success": sum(1 for s in steps if s.get("status") == "success"),
            "steps_failed": sum(1 for s in steps if s.get("status") == "failed"),
            "steps_blocked": sum(1 for s in steps if s.get("status") == "blocked"),
            "artifacts_total": len(session.get("artifacts") or []),
            "phase_operations_total": len(phase_ops),
            "phase_operations": phase_ops[-25:],
            "authority_chain": "runtime_executes_ai_observes_chatgpt_interprets_assistant_implements_operator_confirms",
        }

    def _validate_analysis_output(self, out: dict) -> None:
        required = {
            "advisory_only": bool,
            "summary": str,
            "top_issues": list,
            "suggested_actions": list,
            "risk_flags": list,
        }
        for key, typ in required.items():
            val = out.get(key)
            if not isinstance(val, typ):
                raise ValueError(f"analyze_latest_run invalid field `{key}`")
        try:
            float(out.get("confidence"))
        except Exception as exc:
            raise ValueError("analyze_latest_run invalid confidence") from exc
        if out.get("advisory_only") is not True:
            raise ValueError("analyze_latest_run must return advisory_only=true")

    def _validate_quality_review_output(self, out: dict) -> None:
        required = {
            "advisory_only": bool,
            "quality_posture": str,
            "gaps": list,
            "tuning_suggestions": list,
        }
        for key, typ in required.items():
            if not isinstance(out.get(key), typ):
                raise ValueError(f"review_ai_bot_quality invalid field `{key}`")
        if out.get("advisory_only") is not True:
            raise ValueError("review_ai_bot_quality must return advisory_only=true")

    def _validate_patch_task_output(self, out: dict) -> None:
        required = {
            "advisory_only": bool,
            "problem": str,
            "evidence": list,
            "constraints": list,
            "target_files": list,
            "acceptance_criteria": list,
            "prompt_text": str,
        }
        for key, typ in required.items():
            if not isinstance(out.get(key), typ):
                raise ValueError(f"generate_codex_patch_task invalid field `{key}`")
        if out.get("advisory_only") is not True:
            raise ValueError("generate_codex_patch_task must return advisory_only=true")

    def _log(
        self,
        session: dict,
        *,
        step_key: str,
        event_type: str,
        actor_role: str,
        action: str,
        status: str,
        actor_id: Optional[str] = None,
        payload_summary: Optional[dict] = None,
        artifact_refs: Optional[list[str]] = None,
        policy_check_result: Optional[dict] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> dict:
        return self.events.log_event(
            session_id=str(session.get("session_id") or ""),
            step_key=step_key,
            event_type=event_type,
            actor_role=actor_role,
            actor_id=actor_id,
            action=action,
            status=status,
            payload_summary=dict(payload_summary or {}),
            artifact_refs=list(artifact_refs or []),
            policy_check_result=dict(policy_check_result or {}),
            error_code=error_code,
            error_message=error_message,
        )
