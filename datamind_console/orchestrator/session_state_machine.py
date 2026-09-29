from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


SESSION_STATES = {
    "session_created",
    "mode_selected",
    "snapshot_loading",
    "snapshot_ready",
    "ai_bot_health_checked",
    "advisory_analysis_ready",
    "patch_task_ready",
    "waiting_for_operator",
    "prompt_prepared",
    "runner_dispatching",
    "runner_output_ready",
    "awaiting_patch_review",
    "patch_applied_or_rejected",
    "retest_recorded",
    "session_closed",
    "failed",
    "blocked",
}

STEP_STATUSES = {
    "pending",
    "running",
    "success",
    "failed",
    "blocked",
    "waiting_for_operator",
}

TERMINAL_STATES = {
    "session_closed",
    "failed",
    "blocked",
}

DB_STEP_STATUS_MAP = {
    "pending": "pending",
    "running": "running",
    "success": "completed",
    "failed": "failed",
    "blocked": "failed",
    "waiting_for_operator": "waiting_approval",
}


@dataclass(frozen=True)
class StepContract:
    step_key: str
    state_before: str
    state_after_success: str
    actor_role: str
    action: str
    requires_operator_confirmation: bool = False


STEP_CONTRACTS: List[StepContract] = [
    StepContract(
        step_key="session_start",
        state_before="session_created",
        state_after_success="mode_selected",
        actor_role="runtime",
        action="pipeline_execute",
    ),
    StepContract(
        step_key="define_work_mode",
        state_before="mode_selected",
        state_after_success="mode_selected",
        actor_role="runtime",
        action="pipeline_execute",
        requires_operator_confirmation=False,
    ),
    StepContract(
        step_key="load_ai_bot_snapshot",
        state_before="mode_selected",
        state_after_success="snapshot_ready",
        actor_role="ai_bot",
        action="telemetry_snapshot",
    ),
    StepContract(
        step_key="verify_ai_bot_health",
        state_before="snapshot_ready",
        state_after_success="ai_bot_health_checked",
        actor_role="ai_bot",
        action="telemetry_score",
    ),
    StepContract(
        step_key="analyze_latest_run",
        state_before="ai_bot_health_checked",
        state_after_success="advisory_analysis_ready",
        actor_role="chatgpt_api",
        action="advisory_analyze",
    ),
    StepContract(
        step_key="review_ai_bot_quality",
        state_before="advisory_analysis_ready",
        state_after_success="advisory_analysis_ready",
        actor_role="chatgpt_api",
        action="advisory_analyze",
    ),
    StepContract(
        step_key="generate_codex_patch_task",
        state_before="advisory_analysis_ready",
        state_after_success="patch_task_ready",
        actor_role="chatgpt_api",
        action="generate_patch_task",
    ),
    StepContract(
        step_key="operator_dispatch_checkpoint",
        state_before="patch_task_ready",
        state_after_success="waiting_for_operator",
        actor_role="operator",
        action="dispatch_patch_task",
        requires_operator_confirmation=True,
    ),
    StepContract(
        step_key="prepare_prompt_artifact",
        state_before="waiting_for_operator",
        state_after_success="prompt_prepared",
        actor_role="assistant_runner",
        action="runner_dispatch",
    ),
    StepContract(
        step_key="dispatch_to_assistant",
        state_before="prompt_prepared",
        state_after_success="runner_output_ready",
        actor_role="assistant_runner",
        action="runner_dispatch",
    ),
    StepContract(
        step_key="review_assistant_output",
        state_before="runner_output_ready",
        state_after_success="awaiting_patch_review",
        actor_role="operator",
        action="dispatch_patch_task",
        requires_operator_confirmation=True,
    ),
    StepContract(
        step_key="apply_patch_and_retest",
        state_before="awaiting_patch_review",
        state_after_success="patch_applied_or_rejected",
        actor_role="operator",
        action="apply_generated_patch",
        requires_operator_confirmation=True,
    ),
    StepContract(
        step_key="operate_phase_workflows",
        state_before="patch_applied_or_rejected",
        state_after_success="patch_applied_or_rejected",
        actor_role="runtime",
        action="pipeline_execute",
    ),
    StepContract(
        step_key="capture_operator_labels",
        state_before="patch_applied_or_rejected",
        state_after_success="retest_recorded",
        actor_role="operator",
        action="apply_generated_patch",
        requires_operator_confirmation=True,
    ),
    StepContract(
        step_key="session_summary",
        state_before="retest_recorded",
        state_after_success="session_closed",
        actor_role="runtime",
        action="pipeline_execute",
    ),
]

STEP_CONTRACT_BY_KEY: Dict[str, StepContract] = {row.step_key: row for row in STEP_CONTRACTS}

HARD_CHECKPOINT_STEPS = {
    "operator_dispatch_checkpoint",
    "review_assistant_output",
}

MANDATORY_NON_SKIPPABLE_STEPS = {
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
}

ALLOWED_STATE_TRANSITIONS: Dict[str, set[str]] = {
    "session_created": {"mode_selected", "failed", "blocked"},
    "mode_selected": {"snapshot_loading", "snapshot_ready", "failed", "blocked"},
    "snapshot_loading": {"snapshot_ready", "failed", "blocked"},
    "snapshot_ready": {"ai_bot_health_checked", "failed", "blocked"},
    "ai_bot_health_checked": {"advisory_analysis_ready", "blocked", "failed"},
    "advisory_analysis_ready": {"patch_task_ready", "failed", "blocked"},
    "patch_task_ready": {"waiting_for_operator", "failed", "blocked"},
    "waiting_for_operator": {"prompt_prepared", "session_closed", "failed", "blocked"},
    "prompt_prepared": {"runner_dispatching", "runner_output_ready", "failed", "blocked"},
    "runner_dispatching": {"runner_output_ready", "failed", "blocked"},
    "runner_output_ready": {"awaiting_patch_review", "failed", "blocked"},
    "awaiting_patch_review": {"patch_applied_or_rejected", "failed", "blocked"},
    "patch_applied_or_rejected": {"retest_recorded", "failed", "blocked"},
    "retest_recorded": {"session_closed", "failed", "blocked"},
    "session_closed": set(),
    "failed": set(),
    "blocked": {"ai_bot_health_checked", "failed"},
}


def to_db_step_status(status: str) -> str:
    return DB_STEP_STATUS_MAP.get(str(status or ""), "pending")


def is_valid_session_state(state: str) -> bool:
    return str(state or "") in SESSION_STATES


def is_valid_step_status(status: str) -> bool:
    return str(status or "") in STEP_STATUSES


def can_transition_state(current_state: str, next_state: str, *, allow_same: bool = True) -> bool:
    current = str(current_state or "").strip()
    nxt = str(next_state or "").strip()
    if current not in SESSION_STATES or nxt not in SESSION_STATES:
        return False
    if allow_same and current == nxt:
        return True
    allowed = ALLOWED_STATE_TRANSITIONS.get(current) or set()
    return nxt in allowed


def assert_step_prerequisite(current_state: str, step_key: str) -> None:
    key = str(step_key or "").strip()
    contract = STEP_CONTRACT_BY_KEY.get(key)
    if contract is None:
        raise ValueError(f"Unknown step_key: {key}")
    state = str(current_state or "").strip()
    if state != str(contract.state_before):
        raise ValueError(
            f"Step `{key}` requires state `{contract.state_before}`, got `{state}`."
        )


def new_step_records() -> List[dict]:
    out: List[dict] = []
    for idx, row in enumerate(STEP_CONTRACTS):
        out.append(
            {
                "step_id": idx + 1,
                "step_key": row.step_key,
                "status": "pending",
                "actor_role": row.actor_role,
                "action": row.action,
                "state_before": row.state_before,
                "state_after_success": row.state_after_success,
                "requires_operator_confirmation": bool(row.requires_operator_confirmation),
                "started_at": None,
                "ended_at": None,
                "error_summary": None,
                "artifact_refs": [],
            }
        )
    return out


def snapshot_step(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "step_id": row.get("step_id"),
        "step_key": row.get("step_key"),
        "status": row.get("status"),
        "actor_role": row.get("actor_role"),
        "action": row.get("action"),
        "state_before": row.get("state_before"),
        "state_after_success": row.get("state_after_success"),
        "requires_operator_confirmation": bool(row.get("requires_operator_confirmation")),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "error_summary": row.get("error_summary"),
        "artifact_refs": list(row.get("artifact_refs") or []),
    }
