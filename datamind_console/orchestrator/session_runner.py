"""LEGACY / REFERENCE — Original orchestrator session runner.

This module is part of the v1 orchestration system (session_runner + service + role_model_service).
The active runtime is pipeline_autopilot.py + operator_orchestrator_view.py.
Retained because active tests verify authority-model invariants against this code.
Do NOT use for new features — all new orchestration goes through SupervisedPipelineAutopilot.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from datamind_console.db import orchestrator_repo as repo
from datamind_console.orchestrator.artifact_registry import ArtifactRegistry
from datamind_console.orchestrator.policy_engine import (
    get_effective_policy,
    get_risk_level,
    should_auto_advance,
)
from datamind_console.orchestrator.role_policy import (
    ACTION_ADVISORY_ANALYZE,
    ACTION_APPLY_GENERATED_PATCH,
    ACTION_DISPATCH_PATCH_TASK,
    ACTION_GENERATE_PATCH_TASK,
    ACTION_PIPELINE_EXECUTE,
    ACTION_RUNNER_DISPATCH,
    ACTION_TELEMETRY_SCORE,
    ACTION_TELEMETRY_SNAPSHOT,
    ROLE_AI_BOT,
    ROLE_ASSISTANT_RUNNER,
    ROLE_CHATGPT_API,
    ROLE_OPERATOR,
    ROLE_RUNTIME,
    RolePolicyError,
    check_permission,
)
from datamind_console.orchestrator.session_event_log import SessionEventLogger
from datamind_console.orchestrator.session_state_machine import (
    MANDATORY_NON_SKIPPABLE_STEPS,
    STEP_CONTRACT_BY_KEY,
)

# ============================================================
# Step keys — ordered for the role-based control tower
# ============================================================

STEP_KEYS: list[str] = [
    "session_start",
    "define_work_mode",
    "load_ai_bot_snapshot",
    "verify_ai_bot_health",
    "analyze_latest_run",
    "review_ai_bot_quality",
    "generate_codex_patch_task",
    "operator_dispatch_checkpoint",
    "prepare_prompt_artifact",
    "dispatch_to_assistant",
    "review_assistant_output",
    "apply_patch_and_retest",
    "operate_phase_workflows",
    "capture_operator_labels",
    "session_summary",
]

STEP_AUTHORITY: Dict[str, tuple[str, str, bool]] = {
    "session_start": (ROLE_RUNTIME, ACTION_PIPELINE_EXECUTE, False),
    "define_work_mode": (ROLE_RUNTIME, ACTION_PIPELINE_EXECUTE, False),
    "load_ai_bot_snapshot": (ROLE_AI_BOT, ACTION_TELEMETRY_SNAPSHOT, False),
    "verify_ai_bot_health": (ROLE_AI_BOT, ACTION_TELEMETRY_SCORE, False),
    "analyze_latest_run": (ROLE_CHATGPT_API, ACTION_ADVISORY_ANALYZE, False),
    "review_ai_bot_quality": (ROLE_CHATGPT_API, ACTION_ADVISORY_ANALYZE, False),
    "generate_codex_patch_task": (ROLE_CHATGPT_API, ACTION_GENERATE_PATCH_TASK, False),
    "operator_dispatch_checkpoint": (ROLE_OPERATOR, ACTION_DISPATCH_PATCH_TASK, True),
    "prepare_prompt_artifact": (ROLE_ASSISTANT_RUNNER, ACTION_RUNNER_DISPATCH, False),
    "dispatch_to_assistant": (ROLE_ASSISTANT_RUNNER, ACTION_RUNNER_DISPATCH, False),
    "review_assistant_output": (ROLE_OPERATOR, ACTION_DISPATCH_PATCH_TASK, True),
    "apply_patch_and_retest": (ROLE_OPERATOR, ACTION_APPLY_GENERATED_PATCH, True),
    "operate_phase_workflows": (ROLE_RUNTIME, ACTION_PIPELINE_EXECUTE, True),
    "capture_operator_labels": (ROLE_OPERATOR, ACTION_APPLY_GENERATED_PATCH, True),
    "session_summary": (ROLE_RUNTIME, ACTION_PIPELINE_EXECUTE, False),
}

# ============================================================
# Step executor registry
# ============================================================

_step_executors: Dict[str, Callable] = {}

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOG_ROOT = (_REPO_ROOT / "datamind_console" / "orchestrator_logs" / "v2_db_runner").resolve()
_EVENT_LOGGER = SessionEventLogger(base_dir=_LOG_ROOT)
_ARTIFACT_REGISTRY = ArtifactRegistry(base_dir=_LOG_ROOT / "artifacts")


def register_step_executor(step_key: str, executor: Callable) -> None:
    _step_executors[step_key] = executor


# ============================================================
# Session lifecycle
# ============================================================

def create_session(
    *,
    user_id: Optional[str] = None,
    profile: str = "balanced",
    dry_run: bool = False,
    metadata: Optional[dict] = None,
) -> dict:
    seed = dict(metadata or {})
    seed.setdefault("advisory_mode", True)
    seed.setdefault("execution_authority", "runtime_validators_operator")
    seed.setdefault("legacy_orchestrator_enabled", False)
    seed.setdefault("orchestrator_v2_role_model", True)
    seed.setdefault("session_state", "session_created")

    session = repo.create_session(
        user_id=user_id,
        profile=profile,
        dry_run=dry_run,
        metadata=seed,
    )
    session_id = str(session["session_id"])

    for idx, step_key in enumerate(STEP_KEYS):
        policy = get_effective_policy(step_key, profile)
        repo.create_step(
            session_id=session_id,
            step_key=step_key,
            step_idx=idx,
            policy=policy,
        )

    repo.update_session_status(session_id, status="running", current_step_idx=0)
    session["status"] = "running"
    session["current_step_idx"] = 0

    _log_event(
        session_id=session_id,
        step_key="session_start",
        event_type="session_created",
        actor_role=ROLE_OPERATOR,
        action=ACTION_DISPATCH_PATCH_TASK,
        status="ok",
        payload_summary={"profile": profile, "dry_run": bool(dry_run)},
    )
    return session


def advance_session(session_id: str) -> dict:
    session = repo.get_session(session_id)
    if not session:
        raise ValueError(f"Session not found: {session_id}")

    if session["status"] in ("completed", "cancelled", "failed"):
        return session

    steps = repo.list_steps_for_session(session_id)
    profile = session["profile"]
    dry_run = session["dry_run"]
    idx = session["current_step_idx"]

    while idx < len(steps):
        step = steps[idx]
        step_key = str(step["step_key"])
        step_id = str(step["step_id"])
        policy = str(step["policy"])

        if step["status"] in ("auto_approved", "approved", "completed", "skipped"):
            idx += 1
            continue

        if policy == "skip":
            if step_key in MANDATORY_NON_SKIPPABLE_STEPS:
                msg = f"Mandatory step `{step_key}` cannot be skipped by policy."
                denial = {
                    "ok": False,
                    "error": msg,
                    "error_code": "mandatory_step_cannot_skip",
                    "step_key": step_key,
                }
                repo.update_step_status(step_id, status="failed", output=denial, error=msg)
                _set_session_state(session_id, "failed")
                _log_event(
                    session_id=session_id,
                    step_key=step_key,
                    event_type="step_skip_denied",
                    actor_role=ROLE_RUNTIME,
                    action=ACTION_PIPELINE_EXECUTE,
                    status="failed",
                    error_code="mandatory_step_cannot_skip",
                    error_message=msg,
                )
                return repo.end_session(session_id, status="failed") or session
            repo.update_step_status(step_id, status="skipped")
            _log_event(
                session_id=session_id,
                step_key=step_key,
                event_type="step_skipped",
                actor_role=ROLE_RUNTIME,
                action=ACTION_PIPELINE_EXECUTE,
                status="skipped",
                payload_summary={"reason": "policy_skip"},
            )
            idx += 1
            repo.update_session_status(session_id, status="running", current_step_idx=idx)
            continue

        permission = _check_step_permission(session=session, step_key=step_key)
        _log_event(
            session_id=session_id,
            step_key=step_key,
            event_type="policy_check",
            actor_role=permission["role"],
            action=permission["action"],
            status=("ok" if permission["result"].allowed else "denied"),
            policy_check_result=permission["result"].to_dict(),
            error_code=(None if permission["result"].allowed else permission["result"].code),
            error_message=(None if permission["result"].allowed else permission["result"].message),
        )
        if not permission["result"].allowed:
            denial = {
                "ok": False,
                "error": permission["result"].message,
                "error_code": permission["result"].code,
                "step_key": step_key,
            }
            repo.update_step_status(step_id, status="failed", output=denial, error=permission["result"].message)
            _set_session_state(session_id, "blocked")
            _log_event(
                session_id=session_id,
                step_key=step_key,
                event_type="policy_denied",
                actor_role=permission["role"],
                action=permission["action"],
                status="failed",
                policy_check_result=permission["result"].to_dict(),
                error_code=permission["result"].code,
                error_message=permission["result"].message,
            )
            return repo.end_session(session_id, status="failed") or session

        if should_auto_advance(step_key, profile, dry_run):
            repo.update_step_status(step_id, status="running")
            _log_event(
                session_id=session_id,
                step_key=step_key,
                event_type="step_started",
                actor_role=permission["role"],
                action=permission["action"],
                status="running",
            )
            output = _execute_step(session_id, step, dry_run)
            error = output.get("error") if isinstance(output, dict) else None
            if error:
                repo.update_step_status(step_id, status="failed", output=output, error=str(error))
                _set_session_state(session_id, "failed")
                _log_event(
                    session_id=session_id,
                    step_key=step_key,
                    event_type="step_failed",
                    actor_role=permission["role"],
                    action=permission["action"],
                    status="failed",
                    payload_summary=_summary(output),
                    error_code=str(output.get("error_code") or "step_execution_error"),
                    error_message=str(error),
                )
                return repo.end_session(session_id, status="failed") or session

            refs = _register_step_artifacts(session_id=session_id, step_key=step_key, output=output)
            repo.update_step_status(step_id, status="auto_approved", output=output)
            _set_state_after_step_success(session_id, step_key)
            _log_event(
                session_id=session_id,
                step_key=step_key,
                event_type="step_completed",
                actor_role=permission["role"],
                action=permission["action"],
                status="success",
                payload_summary=_summary(output),
                artifact_refs=refs,
            )
            idx += 1
            repo.update_session_status(session_id, status="running", current_step_idx=idx)
            continue

        # Gate policy — execute then wait for approval
        repo.update_step_status(step_id, status="running")
        _log_event(
            session_id=session_id,
            step_key=step_key,
            event_type="step_started",
            actor_role=permission["role"],
            action=permission["action"],
            status="running",
        )
        output = _execute_step(session_id, step, dry_run)
        error = output.get("error") if isinstance(output, dict) else None
        if error:
            repo.update_step_status(step_id, status="failed", output=output, error=str(error))
            _set_session_state(session_id, "failed")
            _log_event(
                session_id=session_id,
                step_key=step_key,
                event_type="step_failed",
                actor_role=permission["role"],
                action=permission["action"],
                status="failed",
                payload_summary=_summary(output),
                error_code=str(output.get("error_code") or "step_execution_error"),
                error_message=str(error),
            )
            return repo.end_session(session_id, status="failed") or session

        refs = _register_step_artifacts(session_id=session_id, step_key=step_key, output=output)
        repo.update_step_status(step_id, status="waiting_approval", output=output)
        repo.create_approval(step_id=step_id, session_id=session_id)
        repo.update_session_status(session_id, status="paused", current_step_idx=idx)
        _set_session_state(session_id, "waiting_for_operator")
        _log_event(
            session_id=session_id,
            step_key=step_key,
            event_type="checkpoint_waiting",
            actor_role=permission["role"],
            action=permission["action"],
            status="waiting_for_operator",
            payload_summary=_summary(output),
            artifact_refs=refs,
        )
        return repo.get_session(session_id) or session

    _set_session_state(session_id, "session_closed")
    return repo.end_session(session_id, status="completed") or session


def handle_approval(
    approval_id: str,
    *,
    action: str,
    reason: Optional[str] = None,
    decided_by: Optional[str] = None,
) -> dict:
    approval = repo.resolve_approval(
        approval_id,
        action=action,
        reason=reason,
        decided_by=decided_by,
    )
    if not approval:
        raise ValueError(f"Approval not found: {approval_id}")

    session_id = str(approval["session_id"])
    step_id = str(approval["step_id"])
    step = repo.get_step(step_id) or {}
    step_key = str(step.get("step_key") or "")

    policy = check_permission(ROLE_OPERATOR, ACTION_DISPATCH_PATCH_TASK, operator_confirmed=True)
    _log_event(
        session_id=session_id,
        step_key=step_key,
        event_type="operator_approval_action",
        actor_role=ROLE_OPERATOR,
        actor_id=decided_by,
        action=ACTION_DISPATCH_PATCH_TASK,
        status=("ok" if policy.allowed else "denied"),
        payload_summary={"approval_action": action, "reason": reason},
        policy_check_result=policy.to_dict(),
        error_code=(None if policy.allowed else policy.code),
        error_message=(None if policy.allowed else policy.message),
    )
    if not policy.allowed:
        raise RolePolicyError(policy)

    if action == "approve":
        # Mandatory guard: dispatch requires explicit operator decision metadata.
        if step_key == "operator_dispatch_checkpoint":
            sess = repo.get_session(session_id) or {}
            meta = dict(sess.get("metadata") or {})
            dispatch_decision = str(meta.get("dispatch_decision") or "").strip().lower()
            if dispatch_decision not in {"send_to_codex", "send_to_claude", "edit_prompt_first", "stop_session"}:
                raise ValueError(
                    "dispatch_patch_task denied: missing operator dispatch decision. "
                    "Set metadata.dispatch_decision first."
                )

            _log_event(
                session_id=session_id,
                step_key=step_key,
                event_type="dispatch_decision_recorded",
                actor_role=ROLE_OPERATOR,
                actor_id=decided_by,
                action=ACTION_DISPATCH_PATCH_TASK,
                status="ok",
                payload_summary={
                    "dispatch_decision": dispatch_decision,
                    "patch_task_hash": meta.get("patch_task_hash"),
                    "prompt_hash": meta.get("patch_prompt_hash"),
                },
            )
            if dispatch_decision == "stop_session":
                repo.update_step_status(step_id, status="rejected", error="operator_stop_session")
                _set_session_state(session_id, "session_closed")
                return repo.end_session(session_id, status="cancelled") or {}

        repo.update_step_status(step_id, status="approved")
        session = repo.get_session(session_id)
        if session:
            next_idx = int(session["current_step_idx"] or 0) + 1
            repo.update_session_status(session_id, status="running", current_step_idx=next_idx)
        return advance_session(session_id)

    if action == "reject":
        repo.update_step_status(step_id, status="rejected", error=reason)
        _set_session_state(session_id, "session_closed")
        return repo.end_session(session_id, status="cancelled") or {}

    # defer — keep session paused
    _set_session_state(session_id, "waiting_for_operator")
    return repo.get_session(session_id) or {}


def run_step(session_id: str, step_key: str) -> dict:
    steps = repo.list_steps_for_session(session_id)
    target = None
    for s in steps:
        if s["step_key"] == step_key:
            target = s
            break
    if not target:
        raise ValueError(f"Step {step_key} not found in session {session_id}")

    session = repo.get_session(session_id)
    if not session:
        raise ValueError(f"Session not found: {session_id}")
    if str(session.get("status") or "").lower() in {"completed", "cancelled", "failed"}:
        raise ValueError(f"Session {session_id} is terminal; cannot run steps.")

    current_idx = int(session.get("current_step_idx") or 0)
    target_idx = int(target.get("step_idx") or -1)
    if current_idx != target_idx:
        raise ValueError(
            f"Out-of-order step execution denied: current_step_idx={current_idx}, target_step_idx={target_idx}."
        )

    permission = _check_step_permission(session=session, step_key=step_key)
    _log_event(
        session_id=session_id,
        step_key=step_key,
        event_type="policy_check",
        actor_role=permission["role"],
        action=permission["action"],
        status=("ok" if permission["result"].allowed else "denied"),
        policy_check_result=permission["result"].to_dict(),
        error_code=(None if permission["result"].allowed else permission["result"].code),
        error_message=(None if permission["result"].allowed else permission["result"].message),
    )
    if not permission["result"].allowed:
        raise RolePolicyError(permission["result"])

    meta = dict(session.get("metadata") or {})
    if step_key == "dispatch_to_assistant":
        dispatch_decision = str(meta.get("dispatch_decision") or "").strip().lower()
        if dispatch_decision not in {"send_to_codex", "send_to_claude", "edit_prompt_first"}:
            raise ValueError("dispatch_to_assistant denied: missing operator dispatch confirmation.")

    step_id = str(target["step_id"])
    repo.update_step_status(step_id, status="running")
    output = _execute_step(session_id, target, session["dry_run"])
    error = output.get("error") if isinstance(output, dict) else None

    if error:
        repo.update_step_status(step_id, status="failed", output=output, error=str(error))
        _set_session_state(session_id, "failed")
        repo.end_session(session_id, status="failed")
    else:
        refs = _register_step_artifacts(session_id=session_id, step_key=step_key, output=output)
        repo.update_step_status(step_id, status="completed", output=output)
        _set_state_after_step_success(session_id, step_key)
        repo.update_session_status(session_id, status="running", current_step_idx=target_idx + 1)
        _log_event(
            session_id=session_id,
            step_key=step_key,
            event_type="step_completed",
            actor_role=_role_for_step(step_key),
            action=_action_for_step(step_key),
            status="success",
            payload_summary=_summary(output),
            artifact_refs=refs,
        )
    return output


# ============================================================
# Internal
# ============================================================

def _execute_step(session_id: str, step: dict, dry_run: bool) -> dict:
    step_key = str(step["step_key"])
    if step_key == "dispatch_to_assistant":
        sess = repo.get_session(session_id) or {}
        meta = dict(sess.get("metadata") or {})
        dispatch_decision = str(meta.get("dispatch_decision") or "").strip().lower()
        if dispatch_decision not in {"send_to_codex", "send_to_claude", "edit_prompt_first"}:
            return {
                "ok": False,
                "step_key": step_key,
                "error": "operator_confirmation_required_before_dispatch",
                "error_code": "dispatch_confirmation_missing",
            }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "step_key": step_key,
            "message": f"Dry-run: {step_key} would execute here.",
            "risk_level": get_risk_level(step_key),
        }

    executor = _step_executors.get(step_key)
    if executor is not None:
        try:
            result = executor(session_id, step)
            if not isinstance(result, dict):
                result = {"ok": True, "raw": str(result)}
            return result
        except Exception as e:
            return {"ok": False, "error": str(e), "step_key": step_key, "error_code": "executor_error"}

    default_output = _default_step_output(session_id=session_id, step_key=step_key)
    if default_output is not None:
        return default_output

    return {
        "ok": True,
        "stub": True,
        "step_key": step_key,
        "message": f"No executor registered for {step_key}. Pass-through.",
    }


def _default_step_output(*, session_id: str, step_key: str) -> Optional[dict]:
    session = repo.get_session(session_id) or {}
    metadata = session.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    preferred_target = str(metadata.get("preferred_target") or "none").strip().lower()
    dispatch_decision = str(metadata.get("dispatch_decision") or "").strip().lower()
    target = preferred_target
    if dispatch_decision in {"send_to_codex", "send_to_claude"}:
        target = dispatch_decision.replace("send_to_", "")

    now = datetime.now(timezone.utc).isoformat()
    shared = {
        "ok": True,
        "step_key": step_key,
        "generated_at": now,
        "authority_chain": {
            "runtime_validators": "execution_authority",
            "ai_bot": "telemetry_and_scoring_only",
            "chatgpt_api": "analysis_interpretation_prioritization_only",
            "assistants": "implementation_only_with_operator_approval",
            "operator": "final_supervision_and_confirmation",
        },
        "non_negotiables": [
            "no_gate_bypass",
            "no_auto_merge_bind",
            "no_auto_reorder_commit",
            "no_silent_destructive_cleanup",
            "no_auto_dispatch_without_operator_confirmation",
            "no_auto_apply_generated_code",
        ],
    }

    if step_key == "session_start":
        return {
            **shared,
            "phase": "session_bootstrap",
            "status": "session_initialized",
            "session_state": "session_created",
            "message": "Operator guided workflow session initialized.",
        }

    if step_key == "define_work_mode":
        return {
            **shared,
            "phase": "session_bootstrap",
            "mode": {
                "template": metadata.get("template") or "AI-Assisted Review + Patch Task",
                "preferred_target": preferred_target,
                "snapshot_mode": metadata.get("snapshot_mode") or "fixture",
                "chatgpt_api_mode": metadata.get("chatgpt_api_mode") or "mock",
                "advisory_mode": True,
            },
            "session_state": "mode_selected",
            "message": "Execution mode badges persisted for this session.",
        }

    if step_key == "load_ai_bot_snapshot":
        snapshot_mode = str(metadata.get("snapshot_mode") or "fixture")
        snapshot_payload = {
            "mode": snapshot_mode,
            "status": "loaded",
            "source": ("live_runtime" if snapshot_mode.startswith("live") else "fixture"),
        }
        return {
            **shared,
            "phase": "observation",
            "snapshot": snapshot_payload,
            "session_state": "snapshot_ready",
            "message": "AI Bot snapshot loaded for advisory interpretation.",
        }

    if step_key == "verify_ai_bot_health":
        snapshot_mode = str(metadata.get("snapshot_mode") or "fixture")
        readiness = "ok_with_warnings" if snapshot_mode == "fixture" else "ok"
        warnings: list[str] = []
        if snapshot_mode == "fixture":
            warnings.append("fixture_snapshot_mode_limits_live_confidence")
        return {
            **shared,
            "phase": "observation",
            "status": readiness,
            "warnings": warnings,
            "session_state": "ai_bot_health_checked",
            "message": "Operator checkpoint: accept AI Bot readiness before advisory actions.",
        }

    if step_key == "analyze_latest_run":
        return {
            **shared,
            "phase": "interpretation",
            "summary": "Structured advisory interpretation of latest run prepared.",
            "top_issues": [
                "phase3_sequence_quality_needs_review",
                "operator_label_coverage_low",
            ],
            "risk_flags": ["advisory_only"],
            "confidence": 0.68,
            "suggested_actions": [
                "review_unmatched_and_ambiguous_stops",
                "prioritize_operator_labels_for_recent_runs",
            ],
            "session_state": "advisory_analysis_ready",
        }

    if step_key == "review_ai_bot_quality":
        return {
            **shared,
            "phase": "interpretation",
            "quality_posture": "useful_but_immature",
            "gaps": [
                "insufficient_phase3_labels",
                "limited_error_taxonomy_coverage",
            ],
            "recommendations": [
                "capture_operator_feedback_every_run",
                "expand_warning_correctness_labels",
            ],
            "session_state": "advisory_analysis_ready",
        }

    if step_key == "generate_codex_patch_task":
        task = {
            "problem": "Improve sequence warning classification and explainability.",
            "evidence": [
                "high rate of unresolved unmatched warnings",
                "low confidence in reorder suggestions for sparse corridors",
            ],
            "constraints": [
                "advisory_only",
                "no_automatic_merge_or_cleanup",
                "no_auto_apply_generated_code",
            ],
            "expected_files": [
                "datamind_console/orchestrator/*",
                "datamind_console/views/*",
            ],
            "acceptance_criteria": [
                "warnings grouped by subtype",
                "operator rationale captured in session output",
            ],
            "prompt_text": (
                "Prepare a patch that improves warning subtype explanations and maintains strict operator checkpoints "
                "for dispatch, apply, reorder, merge, and cleanup actions."
            ),
        }
        patch_hash = _hash_payload(task)
        repo.update_session_metadata(
            session_id,
            metadata_patch={
                "patch_task_hash": patch_hash,
                "patch_task": task,
                "patch_prompt_hash": _hash_payload({"prompt_text": task["prompt_text"]}),
            },
        )
        return {
            **shared,
            "phase": "interpretation",
            "task": task,
            "patch_task_hash": patch_hash,
            "session_state": "patch_task_ready",
            "message": "Patch task generated for optional assistant dispatch.",
        }

    if step_key == "operator_dispatch_checkpoint":
        return {
            **shared,
            "phase": "human_checkpoint",
            "checkpoint": "mandatory_before_dispatch",
            "options": [
                "send_to_codex",
                "send_to_claude",
                "edit_prompt_first",
                "stop_session",
            ],
            "session_state": "waiting_for_operator",
            "message": "Operator decision required before dispatching any prompt.",
        }

    if step_key == "prepare_prompt_artifact":
        prompt_text = str((metadata.get("patch_task") or {}).get("prompt_text") or "").strip()
        if not prompt_text:
            prompt_text = "Patch prompt placeholder."
        return {
            **shared,
            "phase": "dispatch",
            "target": target,
            "artifact": {
                "status": "prepared",
                "type": "prompt_preview",
                "prompt_text": prompt_text,
            },
            "session_state": "prompt_prepared",
            "message": "Prompt artifact prepared for local runner dispatch.",
        }

    if step_key == "dispatch_to_assistant":
        if dispatch_decision not in {"send_to_codex", "send_to_claude", "edit_prompt_first"}:
            return {
                "ok": False,
                "step_key": step_key,
                "error": "operator_confirmation_required_before_dispatch",
                "error_code": "dispatch_confirmation_missing",
            }
        if target in {"none", "", "stop_session"}:
            return {
                **shared,
                "phase": "dispatch",
                "target": "none",
                "skipped": True,
                "session_state": "runner_output_ready",
                "message": "Dispatch skipped because no assistant target was selected.",
            }
        return {
            **shared,
            "phase": "dispatch",
            "target": target,
            "runner": {
                "status": "captured",
                "stdout_artifact": "local_runner/outputs/<session_id>_stdout.log",
                "stderr_artifact": "local_runner/outputs/<session_id>_stderr.log",
                "exit_code": 0,
            },
            "session_state": "runner_output_ready",
            "message": f"Dispatch registered for target `{target}`.",
        }

    if step_key == "review_assistant_output":
        return {
            **shared,
            "phase": "human_checkpoint",
            "checkpoint": "no_auto_apply",
            "session_state": "awaiting_patch_review",
            "message": "Operator must review assistant artifacts before any code application.",
        }

    if step_key == "apply_patch_and_retest":
        return {
            **shared,
            "phase": "application",
            "checkpoint": "manual_apply_and_validation",
            "required_actions": [
                "apply_changes_under_operator_control",
                "run_runtime_validators_and_tests",
            ],
            "auto_apply": False,
            "session_state": "patch_applied_or_rejected",
            "message": "No generated code is auto-applied; operator + validators remain authority.",
        }

    if step_key == "operate_phase_workflows":
        return {
            **shared,
            "phase": "pipeline_operations",
            "scope": ["phase1_nodes", "phase2_semantics", "phase3_routes"],
            "critical_rules": [
                "runtime_validators_gate_authority",
                "no_automatic_destructive_cleanup",
                "manual_confirmation_for_merge_and_reorder",
            ],
            "session_state": "patch_applied_or_rejected",
            "message": "Pipeline operations remain runtime-driven with operator supervision.",
        }

    if step_key == "capture_operator_labels":
        labels = {
            "sequence_quality_label": "unlabeled",
            "warning_correctness_label": "unlabeled",
            "merge_decision_quality_label": "unlabeled",
            "notes": "Operator must provide labels; values shown are placeholders.",
        }
        return {
            **shared,
            "phase": "continuous_improvement",
            "labels": labels,
            "session_state": "retest_recorded",
            "message": "Operator feedback labels recorded as first-class outcome.",
        }

    if step_key == "session_summary":
        return {
            **shared,
            "phase": "closeout",
            "status": "completed",
            "session_state": "session_closed",
            "principle": "runtime_executes_ai_observes_chatgpt_interprets_assistant_implements_operator_confirms",
            "message": "Session closed with governance-safe chain of authority.",
        }

    return None


def _check_step_permission(*, session: dict, step_key: str) -> dict:
    role, action, confirmed = STEP_AUTHORITY.get(step_key, (ROLE_RUNTIME, ACTION_PIPELINE_EXECUTE, False))
    result = check_permission(role, action, operator_confirmed=bool(confirmed))
    return {
        "role": role,
        "action": action,
        "result": result,
    }


def _role_for_step(step_key: str) -> str:
    row = STEP_AUTHORITY.get(str(step_key or ""))
    return str(row[0]) if row else ROLE_RUNTIME


def _action_for_step(step_key: str) -> str:
    row = STEP_AUTHORITY.get(str(step_key or ""))
    return str(row[1]) if row else ACTION_PIPELINE_EXECUTE


def _set_state_after_step_success(session_id: str, step_key: str) -> None:
    contract = STEP_CONTRACT_BY_KEY.get(str(step_key or ""))
    if not contract:
        return
    _set_session_state(session_id, str(contract.state_after_success))


def _set_session_state(session_id: str, state: str) -> None:
    repo.update_session_metadata(session_id, metadata_patch={"session_state": str(state or "")})


def _hash_payload(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _register_step_artifacts(*, session_id: str, step_key: str, output: dict) -> list[str]:
    refs: list[str] = []
    try:
        if step_key == "load_ai_bot_snapshot":
            art = _ARTIFACT_REGISTRY.register(session_id=session_id, artifact_type="snapshot_json", content=output)
            refs.append(str(art.get("artifact_id") or ""))
        elif step_key == "verify_ai_bot_health":
            art = _ARTIFACT_REGISTRY.register(session_id=session_id, artifact_type="ai_bot_health_report", content=output)
            refs.append(str(art.get("artifact_id") or ""))
        elif step_key == "analyze_latest_run":
            art = _ARTIFACT_REGISTRY.register(session_id=session_id, artifact_type="chatgpt_analysis", content=output)
            refs.append(str(art.get("artifact_id") or ""))
        elif step_key == "review_ai_bot_quality":
            art = _ARTIFACT_REGISTRY.register(session_id=session_id, artifact_type="chatgpt_ai_bot_review", content=output)
            refs.append(str(art.get("artifact_id") or ""))
        elif step_key == "generate_codex_patch_task":
            art = _ARTIFACT_REGISTRY.register(session_id=session_id, artifact_type="patch_task_json", content=output)
            refs.append(str(art.get("artifact_id") or ""))
        elif step_key == "prepare_prompt_artifact":
            prompt = dict((output.get("artifact") or {})).get("prompt_text") or ""
            art = _ARTIFACT_REGISTRY.register(
                session_id=session_id,
                artifact_type="patch_prompt_text",
                content={"prompt_text": prompt},
            )
            refs.append(str(art.get("artifact_id") or ""))
        elif step_key == "dispatch_to_assistant":
            art = _ARTIFACT_REGISTRY.register(session_id=session_id, artifact_type="runner_dispatch_meta", content=output)
            refs.append(str(art.get("artifact_id") or ""))
            runner = dict(output.get("runner") or {})
            stdout = str(runner.get("stdout_artifact") or "")
            stderr = str(runner.get("stderr_artifact") or "")
            if stdout:
                r1 = _ARTIFACT_REGISTRY.register(
                    session_id=session_id,
                    artifact_type="runner_stdout",
                    source_path=stdout,
                )
                refs.append(str(r1.get("artifact_id") or ""))
            if stderr:
                r2 = _ARTIFACT_REGISTRY.register(
                    session_id=session_id,
                    artifact_type="runner_stderr",
                    source_path=stderr,
                )
                refs.append(str(r2.get("artifact_id") or ""))
        elif step_key == "capture_operator_labels":
            art = _ARTIFACT_REGISTRY.register(session_id=session_id, artifact_type="labels_jsonl_ref", content=output)
            refs.append(str(art.get("artifact_id") or ""))
    except Exception:
        return [r for r in refs if r]

    return [r for r in refs if r]


def _summary(payload: Any) -> dict:
    if isinstance(payload, dict):
        txt = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    else:
        txt = str(payload)
    return {
        "preview": txt[:220],
        "size": len(txt),
    }


def _log_event(
    *,
    session_id: str,
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
) -> None:
    _EVENT_LOGGER.log_event(
        session_id=session_id,
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
