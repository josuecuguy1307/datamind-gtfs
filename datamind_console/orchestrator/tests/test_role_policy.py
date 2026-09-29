from __future__ import annotations

import unittest

from datamind_console.orchestrator.role_policy import (
    ACTION_DESTRUCTIVE_CLEANUP_EXECUTE,
    ACTION_DISPATCH_PATCH_TASK,
    ACTION_APPLY_GENERATED_PATCH,
    ACTION_GATE_TRANSITION,
    ACTION_MERGE_BIND,
    ACTION_NOTIFY_EXTERNAL,
    ACTION_PIPELINE_EXECUTE,
    ACTION_SEQUENCE_REORDER_APPLY,
    ROLE_AI_BOT,
    ROLE_ASSISTANT_RUNNER,
    ROLE_CHATGPT_API,
    ROLE_N8N_EXTERNAL,
    ROLE_RUNTIME,
    check_permission,
)


class RolePolicyTests(unittest.TestCase):
    def test_runtime_can_execute_pipeline(self) -> None:
        result = check_permission(ROLE_RUNTIME, ACTION_PIPELINE_EXECUTE)
        self.assertTrue(result.allowed)

    def test_ai_bot_cannot_merge_bind(self) -> None:
        result = check_permission(ROLE_AI_BOT, ACTION_MERGE_BIND)
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, "advisory_layer_forbidden")

    def test_ai_bot_cannot_execute_critical_actions(self) -> None:
        for action in (
            ACTION_PIPELINE_EXECUTE,
            ACTION_GATE_TRANSITION,
            ACTION_APPLY_GENERATED_PATCH,
            ACTION_SEQUENCE_REORDER_APPLY,
            ACTION_DESTRUCTIVE_CLEANUP_EXECUTE,
        ):
            with self.subTest(action=action):
                result = check_permission(ROLE_AI_BOT, action)
                self.assertFalse(result.allowed)

    def test_chatgpt_api_cannot_transition_gate(self) -> None:
        result = check_permission(ROLE_CHATGPT_API, ACTION_GATE_TRANSITION)
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, "advisory_layer_forbidden")

    def test_chatgpt_api_cannot_execute_or_dispatch(self) -> None:
        for action in (
            ACTION_PIPELINE_EXECUTE,
            ACTION_MERGE_BIND,
            ACTION_APPLY_GENERATED_PATCH,
            ACTION_DISPATCH_PATCH_TASK,
        ):
            with self.subTest(action=action):
                result = check_permission(ROLE_CHATGPT_API, action)
                self.assertFalse(result.allowed)

    def test_runner_cannot_apply_generated_patch(self) -> None:
        result = check_permission(ROLE_ASSISTANT_RUNNER, ACTION_APPLY_GENERATED_PATCH)
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, "advisory_layer_forbidden")

    def test_runner_cannot_execute_pipeline_or_merge(self) -> None:
        for action in (ACTION_PIPELINE_EXECUTE, ACTION_MERGE_BIND):
            with self.subTest(action=action):
                result = check_permission(ROLE_ASSISTANT_RUNNER, action)
                self.assertFalse(result.allowed)

    def test_runtime_critical_action_requires_operator_confirmation(self) -> None:
        denied = check_permission(ROLE_RUNTIME, ACTION_SEQUENCE_REORDER_APPLY, operator_confirmed=False)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.code, "operator_confirmation_required")

        allowed = check_permission(ROLE_RUNTIME, ACTION_SEQUENCE_REORDER_APPLY, operator_confirmed=True)
        self.assertTrue(allowed.allowed)

    def test_n8n_cannot_execute_critical_actions(self) -> None:
        for action in (
            ACTION_GATE_TRANSITION,
            ACTION_MERGE_BIND,
            ACTION_PIPELINE_EXECUTE,
            ACTION_SEQUENCE_REORDER_APPLY,
            ACTION_DESTRUCTIVE_CLEANUP_EXECUTE,
            ACTION_DISPATCH_PATCH_TASK,
        ):
            with self.subTest(action=action):
                result = check_permission(ROLE_N8N_EXTERNAL, action)
                self.assertFalse(result.allowed)

    def test_n8n_notify_external_allowed(self) -> None:
        result = check_permission(ROLE_N8N_EXTERNAL, ACTION_NOTIFY_EXTERNAL)
        self.assertTrue(result.allowed)


if __name__ == "__main__":
    unittest.main()
