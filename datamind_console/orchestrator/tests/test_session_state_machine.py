from __future__ import annotations

import unittest

from datamind_console.orchestrator.session_state_machine import (
    MANDATORY_NON_SKIPPABLE_STEPS,
    SESSION_STATES,
    STEP_CONTRACTS,
    STEP_STATUSES,
    assert_step_prerequisite,
    can_transition_state,
    is_valid_session_state,
    is_valid_step_status,
    new_step_records,
    to_db_step_status,
)


class SessionStateMachineTests(unittest.TestCase):
    def test_required_states_exist(self) -> None:
        required = {
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
        self.assertTrue(required.issubset(SESSION_STATES))

    def test_step_status_contract(self) -> None:
        self.assertTrue({"pending", "running", "success", "failed", "blocked", "waiting_for_operator"}.issubset(STEP_STATUSES))
        self.assertTrue(is_valid_step_status("success"))
        self.assertFalse(is_valid_step_status("completed"))

    def test_db_step_status_mapping(self) -> None:
        self.assertEqual(to_db_step_status("success"), "completed")
        self.assertEqual(to_db_step_status("waiting_for_operator"), "waiting_approval")

    def test_step_records_shape(self) -> None:
        rows = new_step_records()
        self.assertEqual(len(rows), len(STEP_CONTRACTS))
        first = rows[0]
        for key in {
            "step_id",
            "step_key",
            "status",
            "actor_role",
            "action",
            "state_before",
            "state_after_success",
            "requires_operator_confirmation",
            "started_at",
            "ended_at",
            "error_summary",
            "artifact_refs",
        }:
            self.assertIn(key, first)

    def test_state_validator(self) -> None:
        self.assertTrue(is_valid_session_state("waiting_for_operator"))
        self.assertFalse(is_valid_session_state("unknown"))

    def test_transition_validator(self) -> None:
        self.assertTrue(can_transition_state("patch_task_ready", "waiting_for_operator", allow_same=False))
        self.assertFalse(can_transition_state("mode_selected", "awaiting_patch_review", allow_same=False))

    def test_step_prerequisite_guard(self) -> None:
        assert_step_prerequisite("mode_selected", "load_ai_bot_snapshot")
        with self.assertRaises(ValueError):
            assert_step_prerequisite("session_created", "load_ai_bot_snapshot")

    def test_mandatory_steps_include_dispatch_and_apply(self) -> None:
        self.assertIn("operator_dispatch_checkpoint", MANDATORY_NON_SKIPPABLE_STEPS)
        self.assertIn("apply_patch_and_retest", MANDATORY_NON_SKIPPABLE_STEPS)


if __name__ == "__main__":
    unittest.main()
