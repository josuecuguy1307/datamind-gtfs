from __future__ import annotations

import os
import unittest

try:
    from fastapi.testclient import TestClient
except Exception:
    TestClient = None  # type: ignore


@unittest.skipUnless(TestClient is not None, "fastapi.testclient unavailable (install httpx)")
class RouteSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["DATAMIND_CHATGPT_MOCK_MODE"] = "true"
        os.environ["DATAMIND_CHATGPT_SNAPSHOT_MODE"] = "fixture"
        from fastapi import FastAPI
        from datamind_console.api_chatgpt.routes.ai_bot_api import router as ai_bot_api_router

        app = FastAPI()
        app.include_router(ai_bot_api_router)
        cls.client = TestClient(app)

    @staticmethod
    def _payload(task: str) -> dict:
        return {
            "task": task,
            "snapshot": {},
            "operator_context": {"from": "smoke_test"},
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
        }

    def test_analyze_run_endpoint(self) -> None:
        r = self.client.post("/api/ai-bot/analyze-run", json=self._payload("analyze_latest_run"))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("task"), "analyze_latest_run")

    def test_review_quality_endpoint(self) -> None:
        r = self.client.post(
            "/api/ai-bot/review-bot-quality",
            json=self._payload("review_ai_bot_quality"),
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("task"), "review_ai_bot_quality")

    def test_generate_codex_task_endpoint(self) -> None:
        r = self.client.post(
            "/api/ai-bot/generate-codex-task",
            json=self._payload("hades_patch_task_generator"),
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("task"), "hades_patch_task_generator")

    def test_generate_codex_task_endpoint_supports_explicit_legacy_template(self) -> None:
        payload = self._payload("generate_codex_patch_task")
        payload["legacy_task"] = True
        r = self.client.post("/api/ai-bot/generate-codex-task", json=payload)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("task"), "generate_codex_patch_task")

    def test_compare_retest_endpoint(self) -> None:
        payload = self._payload("hades_retest_comparator")
        payload["snapshot"] = {
            "change_type": "patch_detector_scoring",
            "phase": "phase3",
            "step_focus": "P3.2_SEQUENCE_STEP20",
            "baseline_run_snapshot": {
                "run_id": "run-base",
                "phase": "phase3",
                "step_id": "P3.2_SEQUENCE_STEP20",
                "route_id": "route-1",
                "metrics": {"quality_score": 0.61, "warning_count": 6, "unmatched_count": 8},
            },
            "retest_run_snapshot": {
                "run_id": "run-retest",
                "phase": "phase3",
                "step_id": "P3.2_SEQUENCE_STEP20",
                "route_id": "route-1",
                "metrics": {"quality_score": 0.75, "warning_count": 3, "unmatched_count": 3},
            },
            "extra_context": {"threshold_config_changed": False},
        }
        r = self.client.post("/api/ai-bot/compare-retest", json=payload)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("task"), "hades_retest_comparator")
        self.assertIn(body.get("result"), {"improved", "regressed", "inconclusive"})
        self.assertIn("comparability_assessment", body)
        self.assertIn("attribution_assessment", body)
        self.assertIn(body.get("recommendation"), {"accept", "rollback", "observe_more", "manual_review"})

    def test_compare_retest_endpoint_confounded_case_prefers_observe_or_manual_review(self) -> None:
        payload = self._payload("hades_retest_comparator")
        payload["snapshot"] = {
            "change_type": "patch_detector_scoring",
            "phase": "phase3",
            "step_focus": "P3.2_SEQUENCE_STEP20",
            "baseline_run_snapshot": {
                "run_id": "run-base-conf",
                "phase": "phase3",
                "step_id": "P3.2_SEQUENCE_STEP20",
                "route_id": "route-2",
                "metrics": {"quality_score": 0.63, "warning_count": 5},
            },
            "retest_run_snapshot": {
                "run_id": "run-retest-conf",
                "phase": "phase3",
                "step_id": "P3.2_SEQUENCE_STEP20",
                "route_id": "route-2",
                "metrics": {"quality_score": 0.80, "warning_count": 2},
            },
            "extra_context": {
                "threshold_config_changed": True,
                "manual_changes_applied": True,
            },
        }
        r = self.client.post("/api/ai-bot/compare-retest", json=payload)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("task"), "hades_retest_comparator")
        self.assertIn(body.get("recommendation"), {"observe_more", "manual_review"})

    def test_check_evidence_consistency_endpoint(self) -> None:
        r = self.client.post(
            "/api/ai-bot/check-evidence-consistency",
            json=self._payload("hades_evidence_consistency_checker"),
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("contradictions_found", body)
        self.assertIn("consistency_summary", body)

    def test_interpret_pipeline_blocker_endpoint(self) -> None:
        r = self.client.post(
            "/api/ai-bot/interpret-pipeline-blocker",
            json=self._payload("hades_pipeline_interpreter"),
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("dominant_cause_class", body)
        self.assertIn("recommended_branch", body)

    def test_interpret_pipeline_blocker_endpoint_supports_explicit_legacy_template(self) -> None:
        payload = self._payload("interpret_pipeline_blocker")
        payload["legacy_task"] = True
        r = self.client.post("/api/ai-bot/interpret-pipeline-blocker", json=payload)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("task"), "interpret_pipeline_blocker")

    def test_prioritize_pipeline_resolution_endpoint_defaults_to_hades_interpreter(self) -> None:
        r = self.client.post(
            "/api/ai-bot/prioritize-pipeline-resolution",
            json=self._payload("hades_pipeline_interpreter"),
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("dominant_cause_class", body)
        self.assertIn("recommended_branch", body)

    def test_explain_cleanup_risk_endpoint_defaults_to_hades_interpreter(self) -> None:
        r = self.client.post(
            "/api/ai-bot/explain-cleanup-risk",
            json=self._payload("hades_pipeline_interpreter"),
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("dominant_cause_class", body)
        self.assertIn("recommended_branch", body)

    def test_explain_cleanup_risk_endpoint_supports_explicit_legacy_template(self) -> None:
        payload = self._payload("explain_cleanup_risk")
        payload["legacy_task"] = True
        r = self.client.post("/api/ai-bot/explain-cleanup-risk", json=payload)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("task"), "explain_cleanup_risk")

    def test_interpret_merge_evidence_endpoint_defaults_to_hades_interpreter(self) -> None:
        r = self.client.post(
            "/api/ai-bot/interpret-merge-evidence",
            json=self._payload("hades_pipeline_interpreter"),
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("dominant_cause_class", body)
        self.assertIn("recommended_branch", body)

    def test_interpret_merge_evidence_endpoint_supports_explicit_legacy_template(self) -> None:
        payload = self._payload("interpret_merge_evidence")
        payload["legacy_task"] = True
        r = self.client.post("/api/ai-bot/interpret-merge-evidence", json=payload)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("task"), "interpret_merge_evidence")


if __name__ == "__main__":
    unittest.main()
