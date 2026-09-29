from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from datamind_console.orchestrator.role_model_service import (
    FixtureAiBotAdapter,
    FixtureAdvisoryAdapter,
    RoleModelService,
)
from datamind_console.orchestrator.role_policy import (
    ROLE_AI_BOT,
    ROLE_N8N_EXTERNAL,
    ROLE_RUNTIME,
    ROLE_VALIDATORS,
    RolePolicyError,
)


def _step(session: dict, step_key: str) -> dict:
    for row in session.get("steps") or []:
        if str(row.get("step_key") or "") == step_key:
            return row
    raise AssertionError(f"step not found: {step_key}")


class _BrokenAdvisory(FixtureAdvisoryAdapter):
    def analyze_latest_run(self, *, snapshot: dict, advisory_only: bool) -> dict:  # type: ignore[override]
        del snapshot, advisory_only
        return {"invalid": True}


class _BrokenQualityReviewAdvisory(FixtureAdvisoryAdapter):
    def review_ai_bot_quality(self, *, snapshot: dict, advisory_only: bool) -> dict:  # type: ignore[override]
        del snapshot, advisory_only
        return {"advisory_only": True, "quality_posture": "bad_shape"}


class _BrokenPatchTaskAdvisory(FixtureAdvisoryAdapter):
    def generate_codex_patch_task(self, *, snapshot: dict, advisory_only: bool) -> dict:  # type: ignore[override]
        del snapshot, advisory_only
        return {
            "advisory_only": True,
            "problem": "x",
            "evidence": [],
            "constraints": [],
            "target_files": [],
            "acceptance_criteria": [],
            # missing prompt_text
        }


class _NotAdvisoryAdvisory(FixtureAdvisoryAdapter):
    def analyze_latest_run(self, *, snapshot: dict, advisory_only: bool) -> dict:  # type: ignore[override]
        out = super().analyze_latest_run(snapshot=snapshot, advisory_only=advisory_only)
        out["advisory_only"] = False
        return out


class _BlockedAiBot(FixtureAiBotAdapter):
    def load_snapshot(self, *, snapshot_source_mode: str, session_context: dict) -> dict:  # type: ignore[override]
        del snapshot_source_mode, session_context
        return {
            "snapshot_id": "blocked_snap",
            "snapshot_source_mode": "fixture",
            "telemetry": {"phase3": {"coverage": 0.0}},
            "warnings": ["phase3_coverage_critical"],
        }


class RoleModelServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.tmp.name) / "orch_v2"
        self.service = RoleModelService(logs_dir=self.logs_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_e2e_guided_flow_fixture(self) -> None:
        session = self.service.start_guided_workflow(
            operator_id="op-1",
            template="AI_ASSISTED_REVIEW_PATCH_TASK",
            snapshot_source_mode="fixture",
            preferred_target="codex",
        )

        session = self.service.run_to_operator_checkpoint(session["session_id"])
        self.assertEqual(session["state"], "waiting_for_operator")
        self.assertEqual(_step(session, "operator_dispatch_checkpoint")["status"], "waiting_for_operator")
        self.assertEqual(_step(session, "dispatch_to_assistant")["status"], "pending")

        # Regression guard: no auto-dispatch before operator decision.
        artifact_types = [str(a.get("artifact_type") or "") for a in session.get("artifacts") or []]
        self.assertNotIn("runner_dispatch_meta", artifact_types)

        session = self.service.submit_dispatch_decision(
            session["session_id"],
            operator_id="op-1",
            decision="dispatch_to_codex",
        )
        self.assertEqual(session["state"], "awaiting_patch_review")
        self.assertEqual(_step(session, "dispatch_to_assistant")["status"], "success")

        # Mandatory no-auto-apply boundary.
        self.assertEqual(_step(session, "apply_patch_and_retest")["status"], "pending")

        session = self.service.record_patch_review(
            session["session_id"],
            operator_id="op-1",
            outcome="applied",
            notes="Patch reviewed and accepted.",
            commit_hash="abc123",
        )
        self.assertEqual(session["state"], "patch_applied_or_rejected")

        session = self.service.record_retest_results(
            session["session_id"],
            operator_id="op-1",
            tests_run=["pytest datamind_console/orchestrator/tests/test_role_model_service.py"],
            passed=True,
            notes="smoke ok",
        )
        self.assertIn(session["state"], {"patch_applied_or_rejected", "retest_recorded"})

        session = self.service.capture_operator_labels(
            session["session_id"],
            operator_id="op-1",
            labels={
                "sequence_quality_label": "good",
                "warning_correctness_label": "acceptable",
                "merge_decision_quality_label": "good",
                "notes": "manual review",
            },
        )
        self.assertEqual(session["state"], "retest_recorded")

        session = self.service.close_session(session["session_id"], operator_id="op-1")
        self.assertEqual(session["state"], "session_closed")
        self.assertEqual(session["status"], "completed")

        artifact_types = [str(a.get("artifact_type") or "") for a in session.get("artifacts") or []]
        self.assertIn("snapshot_json", artifact_types)
        self.assertIn("chatgpt_analysis", artifact_types)
        self.assertIn("patch_task_json", artifact_types)
        self.assertIn("runner_dispatch_meta", artifact_types)
        self.assertIn("operator_review_notes", artifact_types)
        self.assertIn("test_results", artifact_types)
        self.assertIn("labels_jsonl_ref", artifact_types)
        self.assertIn("operator_label", artifact_types)
        self.assertIn("session_summary", artifact_types)

        events_path = self.logs_dir / "events_v2.jsonl"
        self.assertTrue(events_path.exists())

    def test_dispatch_before_checkpoint_is_rejected(self) -> None:
        session = self.service.start_guided_workflow(operator_id="op-2")
        with self.assertRaises(ValueError):
            self.service.submit_dispatch_decision(
                session["session_id"],
                operator_id="op-2",
                decision="dispatch_to_codex",
            )

    def test_boundary_denials(self) -> None:
        with self.assertRaises(RolePolicyError):
            self.service.request_merge_bind(actor_role=ROLE_AI_BOT, operator_confirmed=False)

        with self.assertRaises(RolePolicyError):
            self.service.request_sequence_reorder_apply(actor_role=ROLE_RUNTIME, operator_confirmed=False)

        with self.assertRaises(RolePolicyError):
            self.service.request_destructive_cleanup(actor_role=ROLE_N8N_EXTERNAL, operator_confirmed=False)

        with self.assertRaises(RolePolicyError):
            self.service.request_destructive_cleanup(actor_role=ROLE_RUNTIME, operator_confirmed=False)

        ok = self.service.request_destructive_cleanup(actor_role=ROLE_RUNTIME, operator_confirmed=True)
        self.assertTrue(bool(ok.get("allowed")))

    def test_advisory_schema_validation_failure(self) -> None:
        service = RoleModelService(logs_dir=self.logs_dir / "broken", advisory_adapter=_BrokenAdvisory())
        session = service.start_guided_workflow(operator_id="op-3")
        with self.assertRaises(ValueError):
            service.run_to_operator_checkpoint(session["session_id"])

        current = service.get_session(session["session_id"])
        self.assertEqual(current["state"], "failed")
        self.assertEqual(current["status"], "failed")

    def test_quality_review_schema_validation_failure(self) -> None:
        service = RoleModelService(
            logs_dir=self.logs_dir / "broken_quality",
            advisory_adapter=_BrokenQualityReviewAdvisory(),
        )
        session = service.start_guided_workflow(operator_id="op-4")
        with self.assertRaises(ValueError):
            service.run_to_operator_checkpoint(session["session_id"])

        current = service.get_session(session["session_id"])
        self.assertEqual(current["state"], "failed")
        self.assertEqual(current["status"], "failed")

    def test_patch_task_schema_validation_failure(self) -> None:
        service = RoleModelService(
            logs_dir=self.logs_dir / "broken_patch",
            advisory_adapter=_BrokenPatchTaskAdvisory(),
        )
        session = service.start_guided_workflow(operator_id="op-5")
        with self.assertRaises(ValueError):
            service.run_to_operator_checkpoint(session["session_id"])

        current = service.get_session(session["session_id"])
        self.assertEqual(current["state"], "failed")
        self.assertEqual(current["status"], "failed")

    def test_advisory_only_flag_is_enforced(self) -> None:
        service = RoleModelService(
            logs_dir=self.logs_dir / "not_advisory",
            advisory_adapter=_NotAdvisoryAdvisory(),
        )
        session = service.start_guided_workflow(operator_id="op-6")
        with self.assertRaises(ValueError):
            service.run_to_operator_checkpoint(session["session_id"])

    def test_health_blocked_requires_operator_override(self) -> None:
        service = RoleModelService(
            logs_dir=self.logs_dir / "blocked_health",
            ai_bot_adapter=_BlockedAiBot(),
        )
        session = service.start_guided_workflow(operator_id="op-7")
        blocked = service.run_to_operator_checkpoint(session["session_id"])
        self.assertEqual(blocked["state"], "blocked")

        with self.assertRaises(ValueError):
            service.continue_after_health_override(blocked["session_id"])

        with self.assertRaises(ValueError):
            service.override_blocked_health(blocked["session_id"], operator_id="op-7", reason="")

        overridden = service.override_blocked_health(
            blocked["session_id"],
            operator_id="op-7",
            reason="Proceed with manual caution and explicit review.",
        )
        self.assertEqual(overridden["state"], "ai_bot_health_checked")

        resumed = service.continue_after_health_override(blocked["session_id"])
        self.assertEqual(resumed["state"], "waiting_for_operator")

    def test_phase_operation_recording_enforces_runtime_and_confirmation(self) -> None:
        session = self.service.start_guided_workflow(operator_id="op-8")
        session = self.service.run_to_operator_checkpoint(session["session_id"])
        session = self.service.submit_dispatch_decision(
            session["session_id"],
            operator_id="op-8",
            decision="dispatch_to_codex",
        )
        session = self.service.record_patch_review(
            session["session_id"],
            operator_id="op-8",
            outcome="applied",
            notes="ok",
        )
        self.assertEqual(session["state"], "patch_applied_or_rejected")

        with self.assertRaises(RolePolicyError):
            self.service.record_phase_operation(
                session["session_id"],
                actor_role=ROLE_RUNTIME,
                phase="phase3",
                operation="merge_bind",
                operator_confirmed=False,
                details={"route_id": "r1"},
            )

        with self.assertRaises(ValueError):
            self.service.record_phase_operation(
                session["session_id"],
                actor_role=ROLE_AI_BOT,
                phase="phase3",
                operation="sequence_reorder_apply",
                operator_confirmed=True,
                details={"route_id": "r1"},
            )

        out = self.service.record_phase_operation(
            session["session_id"],
            actor_role=ROLE_VALIDATORS,
            phase="phase3",
            operation="merge_bind",
            operator_confirmed=True,
            details={"route_id": "r1"},
        )
        self.assertEqual(out["operation"], "merge_bind")


if __name__ == "__main__":
    unittest.main()
