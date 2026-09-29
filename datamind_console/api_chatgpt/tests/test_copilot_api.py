from __future__ import annotations

import importlib
import os
import tempfile
import unittest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception:
    FastAPI = None  # type: ignore
    TestClient = None  # type: ignore


@unittest.skipUnless(TestClient is not None and FastAPI is not None, "fastapi.testclient unavailable")
class CopilotRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="copilot_api_tests_")
        os.environ["DATAMIND_CHATGPT_MOCK_MODE"] = "true"
        os.environ["DATAMIND_CHATGPT_SNAPSHOT_MODE"] = "fixture"
        os.environ["DATAMIND_COPILOT_DB_ENABLED"] = "false"
        os.environ["DATAMIND_COPILOT_DATA_DIR"] = cls._tmp.name

        import datamind_console.api_chatgpt.routes.copilot_api as copilot_api

        importlib.reload(copilot_api)
        app = FastAPI()
        app.include_router(copilot_api.router)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._tmp.cleanup()
        except Exception:
            pass

    def test_session_lifecycle_and_chat(self) -> None:
        r_create = self.client.post(
            "/api/copilot/sessions",
            json={
                "title": "Phase Ops Session",
                "mode": "pipeline_operator",
                "created_by": "tester",
            },
        )
        self.assertEqual(r_create.status_code, 200)
        session = (r_create.json() or {}).get("session") or {}
        sid = str(session.get("session_id") or "")
        self.assertTrue(sid)

        r_list = self.client.get("/api/copilot/sessions", params={"limit": 20})
        self.assertEqual(r_list.status_code, 200)
        sessions = (r_list.json() or {}).get("sessions") or []
        self.assertTrue(any(str(x.get("session_id") or "") == sid for x in sessions if isinstance(x, dict)))

        chat_payload = {
            "session_id": sid,
            "message": "Explain Step20 blocker and prioritize unmatched first.",
            "template": "explain_step20_blocker",
            "context": {
                "active_phase": "phase3",
                "active_step": "P3.2_SEQUENCE_STEP20",
                "run_id": "test-run-1",
                "validator_status": "blocked",
                "block_reason": {"code": "STEP20_UNMATCHED_BLOCKING", "severity": "critical"},
                "toggles": {
                    "include_logs": True,
                    "include_metrics": True,
                    "include_block_reason": True,
                    "include_artifacts_summary": True,
                    "include_previous_runs_compare": False,
                },
            },
            "operator_context": {"operator_id": "u-1", "operator_roles": ["admin"]},
        }
        r_chat = self.client.post("/api/copilot/chat", json=chat_payload)
        self.assertEqual(r_chat.status_code, 200)
        body = r_chat.json() or {}
        self.assertEqual(body.get("task"), "hades_pipeline_interpreter")
        self.assertIn("assistant_text", body)
        payload = body.get("assistant_payload") or {}
        self.assertIn("dominant_cause_class", payload)
        self.assertIn("recommended_branch", payload)

        r_msgs = self.client.get(f"/api/copilot/sessions/{sid}/messages", params={"limit": 50})
        self.assertEqual(r_msgs.status_code, 200)
        messages = (r_msgs.json() or {}).get("messages") or []
        self.assertGreaterEqual(len(messages), 2)
        self.assertEqual(messages[-2].get("role"), "user")
        self.assertEqual(messages[-1].get("role"), "assistant")

    def test_stream_chat_returns_sse(self) -> None:
        r_create = self.client.post("/api/copilot/sessions", json={"title": "Streaming Session"})
        self.assertEqual(r_create.status_code, 200)
        sid = str(((r_create.json() or {}).get("session") or {}).get("session_id") or "")
        self.assertTrue(sid)

        r_chat = self.client.post(
            "/api/copilot/chat",
            json={
                "session_id": sid,
                "message": "Draft codex patch task",
                "template": "draft_codex_patch_task",
                "stream": True,
                "context": {"active_phase": "phase1", "active_step": "P1.2_NORMALIZE_FEATURES_CLUSTER_RESOLVE"},
            },
        )
        self.assertEqual(r_chat.status_code, 200)
        self.assertIn("data:", r_chat.text)
        self.assertIn('"type":"message_start"', r_chat.text)
        self.assertIn('"type":"message_end"', r_chat.text)
        self.assertIn('"task":"hades_patch_task_generator"', r_chat.text)

    def test_missing_session_returns_404(self) -> None:
        r_chat = self.client.post(
            "/api/copilot/chat",
            json={
                "session_id": "missing_session",
                "message": "hello",
                "context": {},
            },
        )
        self.assertEqual(r_chat.status_code, 404)

    def test_pipeline_context_defaults_to_hades_interpreter_routing(self) -> None:
        r_create = self.client.post("/api/copilot/sessions", json={"title": "Routing Session"})
        self.assertEqual(r_create.status_code, 200)
        sid = str(((r_create.json() or {}).get("session") or {}).get("session_id") or "")
        self.assertTrue(sid)

        r_chat = self.client.post(
            "/api/copilot/chat",
            json={
                "session_id": sid,
                "message": "we have a Step20 gate fail with unmatched stops",
                "context": {
                    "active_phase": "phase3",
                    "active_step": "P3.2_SEQUENCE_STEP20",
                    "run_id": "test-route-99",
                },
            },
        )
        self.assertEqual(r_chat.status_code, 200)
        body = r_chat.json() or {}
        self.assertEqual(body.get("task"), "hades_pipeline_interpreter")

    def test_retest_compare_message_routes_to_hades_retest_comparator(self) -> None:
        r_create = self.client.post("/api/copilot/sessions", json={"title": "Retest Session"})
        self.assertEqual(r_create.status_code, 200)
        sid = str(((r_create.json() or {}).get("session") or {}).get("session_id") or "")
        self.assertTrue(sid)

        r_chat = self.client.post(
            "/api/copilot/chat",
            json={
                "session_id": sid,
                "message": "Please compare retest outcome baseline vs retest after patch",
                "context": {
                    "active_phase": "phase3",
                    "active_step": "P3.2_SEQUENCE_STEP20",
                    "run_id": "test-retest-1",
                },
            },
        )
        self.assertEqual(r_chat.status_code, 200)
        body = r_chat.json() or {}
        self.assertEqual(body.get("task"), "hades_retest_comparator")
        payload = body.get("assistant_payload") or {}
        self.assertIn("result", payload)
        self.assertIn("recommendation", payload)


if __name__ == "__main__":
    unittest.main()
