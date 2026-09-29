from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable


ROLE_RUNTIME = "runtime"
ROLE_VALIDATORS = "validators"
ROLE_AI_BOT = "ai_bot"
ROLE_CHATGPT_API = "chatgpt_api"
ROLE_ASSISTANT_RUNNER = "assistant_runner"
ROLE_OPERATOR = "operator"
ROLE_N8N_EXTERNAL = "n8n_external"

ROLES = {
    ROLE_RUNTIME,
    ROLE_VALIDATORS,
    ROLE_AI_BOT,
    ROLE_CHATGPT_API,
    ROLE_ASSISTANT_RUNNER,
    ROLE_OPERATOR,
    ROLE_N8N_EXTERNAL,
}

ACTION_PIPELINE_EXECUTE = "pipeline_execute"
ACTION_GATE_TRANSITION = "gate_transition"
ACTION_MERGE_BIND = "merge_bind"
ACTION_SEQUENCE_REORDER_APPLY = "sequence_reorder_apply"
ACTION_DESTRUCTIVE_CLEANUP_EXECUTE = "destructive_cleanup_execute"
ACTION_DISPATCH_PATCH_TASK = "dispatch_patch_task"
ACTION_APPLY_GENERATED_PATCH = "apply_generated_patch"
ACTION_ADVISORY_ANALYZE = "advisory_analyze"
ACTION_TELEMETRY_SNAPSHOT = "telemetry_snapshot"
ACTION_TELEMETRY_SCORE = "telemetry_score"
ACTION_GENERATE_PATCH_TASK = "generate_patch_task"
ACTION_RUNNER_DISPATCH = "runner_dispatch"
ACTION_NOTIFY_EXTERNAL = "notify_external"

ACTIONS = {
    ACTION_PIPELINE_EXECUTE,
    ACTION_GATE_TRANSITION,
    ACTION_MERGE_BIND,
    ACTION_SEQUENCE_REORDER_APPLY,
    ACTION_DESTRUCTIVE_CLEANUP_EXECUTE,
    ACTION_DISPATCH_PATCH_TASK,
    ACTION_APPLY_GENERATED_PATCH,
    ACTION_ADVISORY_ANALYZE,
    ACTION_TELEMETRY_SNAPSHOT,
    ACTION_TELEMETRY_SCORE,
    ACTION_GENERATE_PATCH_TASK,
    ACTION_RUNNER_DISPATCH,
    ACTION_NOTIFY_EXTERNAL,
}

CRITICAL_ACTIONS = {
    ACTION_GATE_TRANSITION,
    ACTION_MERGE_BIND,
    ACTION_SEQUENCE_REORDER_APPLY,
    ACTION_DESTRUCTIVE_CLEANUP_EXECUTE,
    ACTION_DISPATCH_PATCH_TASK,
    ACTION_APPLY_GENERATED_PATCH,
}

_RUNTIME_VALIDATOR_ACTIONS = {
    ACTION_PIPELINE_EXECUTE,
    ACTION_GATE_TRANSITION,
    ACTION_MERGE_BIND,
    ACTION_SEQUENCE_REORDER_APPLY,
    ACTION_DESTRUCTIVE_CLEANUP_EXECUTE,
}

_ROLE_ACTIONS: Dict[str, set[str]] = {
    ROLE_RUNTIME: set(_RUNTIME_VALIDATOR_ACTIONS),
    ROLE_VALIDATORS: set(_RUNTIME_VALIDATOR_ACTIONS),
    ROLE_AI_BOT: {
        ACTION_TELEMETRY_SNAPSHOT,
        ACTION_TELEMETRY_SCORE,
    },
    ROLE_CHATGPT_API: {
        ACTION_ADVISORY_ANALYZE,
        ACTION_GENERATE_PATCH_TASK,
    },
    ROLE_ASSISTANT_RUNNER: {
        ACTION_RUNNER_DISPATCH,
    },
    ROLE_OPERATOR: {
        ACTION_DISPATCH_PATCH_TASK,
        ACTION_APPLY_GENERATED_PATCH,
        ACTION_MERGE_BIND,
        ACTION_SEQUENCE_REORDER_APPLY,
        ACTION_DESTRUCTIVE_CLEANUP_EXECUTE,
        ACTION_NOTIFY_EXTERNAL,
    },
    ROLE_N8N_EXTERNAL: {
        ACTION_NOTIFY_EXTERNAL,
    },
}


@dataclass(frozen=True)
class PolicyCheckResult:
    allowed: bool
    role: str
    action: str
    code: str
    message: str

    def to_dict(self) -> dict:
        return {
            "allowed": bool(self.allowed),
            "role": self.role,
            "action": self.action,
            "code": self.code,
            "message": self.message,
        }


class RolePolicyError(PermissionError):
    def __init__(self, result: PolicyCheckResult) -> None:
        super().__init__(result.message)
        self.result = result


def _deny(role: str, action: str, code: str, message: str) -> PolicyCheckResult:
    return PolicyCheckResult(
        allowed=False,
        role=str(role or ""),
        action=str(action or ""),
        code=code,
        message=message,
    )


def _allow(role: str, action: str) -> PolicyCheckResult:
    return PolicyCheckResult(
        allowed=True,
        role=str(role or ""),
        action=str(action or ""),
        code="ok",
        message="allowed",
    )


def _requires_operator_confirmation(action: str) -> bool:
    return str(action or "") in {
        ACTION_MERGE_BIND,
        ACTION_SEQUENCE_REORDER_APPLY,
        ACTION_DESTRUCTIVE_CLEANUP_EXECUTE,
        ACTION_DISPATCH_PATCH_TASK,
        ACTION_APPLY_GENERATED_PATCH,
    }


def check_permission(
    role: str,
    action: str,
    *,
    operator_confirmed: bool = False,
) -> PolicyCheckResult:
    r = str(role or "").strip().lower()
    a = str(action or "").strip().lower()

    if r not in ROLES:
        return _deny(r, a, "unknown_role", f"Unknown role: `{r}`.")
    if a not in ACTIONS:
        return _deny(r, a, "unknown_action", f"Unknown action: `{a}`.")

    if r in {ROLE_AI_BOT, ROLE_CHATGPT_API, ROLE_ASSISTANT_RUNNER, ROLE_N8N_EXTERNAL} and a in CRITICAL_ACTIONS:
        return _deny(
            r,
            a,
            "advisory_layer_forbidden",
            f"Role `{r}` cannot execute critical action `{a}`.",
        )

    allowed_actions = _ROLE_ACTIONS.get(r) or set()
    if a not in allowed_actions:
        return _deny(r, a, "role_action_forbidden", f"Role `{r}` cannot execute `{a}`.")

    if r in {ROLE_RUNTIME, ROLE_VALIDATORS} and _requires_operator_confirmation(a) and not operator_confirmed:
        return _deny(
            r,
            a,
            "operator_confirmation_required",
            f"Action `{a}` requires operator confirmation before `{r}` execution.",
        )

    if r == ROLE_OPERATOR and a == ACTION_PIPELINE_EXECUTE:
        return _deny(
            r,
            a,
            "operator_not_execution_authority",
            "Operator is decision authority, not pipeline execution authority.",
        )

    return _allow(r, a)


def enforce_permission(role: str, action: str, *, operator_confirmed: bool = False) -> PolicyCheckResult:
    result = check_permission(role, action, operator_confirmed=operator_confirmed)
    if not result.allowed:
        raise RolePolicyError(result)
    return result


def is_critical_action(action: str) -> bool:
    return str(action or "") in CRITICAL_ACTIONS


def allowed_actions_for_role(role: str) -> Iterable[str]:
    return tuple(sorted(_ROLE_ACTIONS.get(str(role or "").strip().lower(), set())))
