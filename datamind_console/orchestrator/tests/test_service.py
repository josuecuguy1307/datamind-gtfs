from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict

from datamind_console.api_chatgpt.services.advisory_service import AdvisoryError
from datamind_console.orchestrator.service import OperatorOrchestratorService, OrchestratorError


class _FakeSnapshotBuilder:
    def __init__(self, snapshot_mode: str) -> None:
        self.snapshot_mode = snapshot_mode

    def build(self, *, task: str, supplied_snapshot: Dict[str, Any], operator_context: Dict[str, Any]) -> Dict[str, Any]:
        del supplied_snapshot, operator_context
        return {
            "snapshot_id": f"snap_{task}",
            "task": task,
            "snapshot_source": self.snapshot_mode,
            "insufficient_data_flags": ["phase3_live_evidence_low"],
            "evidence_refs": [
                {"evidence_id": "ev_primary", "kind": "mock", "description": "Primary evidence"},
                {"evidence_id": "ev_secondary", "kind": "mock", "description": "Secondary evidence"},
            ],
        }


def _fake_snapshot_builder_factory(snapshot_mode: str) -> _FakeSnapshotBuilder:
    return _FakeSnapshotBuilder(snapshot_mode=snapshot_mode)


def _fake_advisory_caller(task: str, envelope: Dict[str, Any], chatgpt_api_mode: str) -> Dict[str, Any]:
    snapshot = dict(envelope.get("snapshot") or {})
    base = {
        "mode": "advisory_only",
        "task": task,
        "summary": f"summary:{task}",
        "confidence": {"band": "low", "score": 0.42, "reasons": ["low evidence"]},
        "risk_flags": ["low_phase3_coverage"],
        "insufficient_data_flags": list(snapshot.get("insufficient_data_flags") or []),
        "operator_confirmation_required": True,
        "evidence_used": ["ev_primary"],
        "api_mode": chatgpt_api_mode,
    }
    if task == "analyze_latest_run":
        base["top_issues"] = [{"issue": "phase3_low", "severity": "medium"}]
        base["recommended_actions"] = [
            {
                "action": "collect_phase3_runs",
                "priority": "high",
                "high_impact": False,
                "requires_operator_confirmation": False,
            }
        ]
        return base
    if task == "review_ai_bot_quality":
        base["quality_assessment"] = {"signal_quality": "limited"}
        base["top_weaknesses"] = [{"weakness": "operator_labels_low"}]
        base["tuning_suggestions"] = [{"suggestion": "capture labels"}]
        base["logging_gaps"] = [{"gap": "phase3_samples"}]
        base["prioritized_improvements"] = [
            {
                "improvement": "improve label capture",
                "priority": "high",
                "high_impact": False,
                "requires_operator_confirmation": False,
            }
        ]
        return base
    if task == "generate_codex_patch_task":
        base["patch_task"] = {
            "title": "Patch sequence warning clarity",
            "scope": "small",
            "prompt_text": "Please update warning messaging for low phase3 coverage.",
        }
        return base
    raise AssertionError(f"Unexpected task: {task}")


def _fake_local_runner(
    prompt_text: str,
    target: str,
    session_id: str,
    *,
    cancel_event: Any | None = None,
) -> Dict[str, Any]:
    if cancel_event is not None and bool(getattr(cancel_event, "is_set", lambda: False)()):
        return {
            "prompt_source": "file",
            "status": "cancelled",
            "target": target,
            "prompt_char_count": len(prompt_text),
            "command_used": f"{target} -p <prompt>",
            "command_mode": "stdin",
            "output_file": f"/tmp/{session_id}_{target}.out.txt",
            "stderr_file": None,
            "prompt_file": f"/tmp/{session_id}_{target}.prompt.txt",
            "prompt_file_final": f"/tmp/{session_id}_{target}.prompt.done.txt",
            "log_file": f"/tmp/{session_id}_{target}.jsonl",
            "duration_ms": 1,
            "return_code": 130,
            "error_summary": "Command cancelled by operator.",
        }
    return {
        "prompt_source": "file",
        "status": "success",
        "target": target,
        "prompt_char_count": len(prompt_text),
        "command_used": f"{target} -p <prompt>",
        "command_mode": "stdin",
        "output_file": f"/tmp/{session_id}_{target}.out.txt",
        "stderr_file": None,
        "prompt_file": f"/tmp/{session_id}_{target}.prompt.txt",
        "prompt_file_final": f"/tmp/{session_id}_{target}.prompt.done.txt",
        "log_file": f"/tmp/{session_id}_{target}.jsonl",
        "duration_ms": 123,
        "return_code": 0,
        "error_summary": None,
    }


class OrchestratorServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.tmp.name) / "orchestrator_logs"
        self.service = OperatorOrchestratorService(
            advisory_caller=_fake_advisory_caller,
            local_runner_caller=_fake_local_runner,
            snapshot_builder_factory=_fake_snapshot_builder_factory,
            logs_dir=self.logs_dir,
            runner_config_path=Path(self.tmp.name) / "runner_config.yaml",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_stub_runner_config(self) -> Path:
        root = Path(self.tmp.name)
        inbox = root / "inbox"
        processing = root / "processing"
        done = root / "done"
        failed = root / "failed"
        results = root / "results"
        logs = root / "logs"
        for d in [inbox, processing, done, failed, results, logs]:
            d.mkdir(parents=True, exist_ok=True)

        cfg_path = root / "runner_config.json"
        cfg = {
            "runner": {
                "timeout_seconds": 5,
                "capture_stderr_separately": True,
                "infer_target_from_filename": True,
                "default_target": "codex",
                "log_filename": "runs.jsonl",
            },
            "clipboard": {
                "clipboard_enabled": True,
                "clipboard_prompt_filename_prefix": "clip_prompt",
                "clipboard_max_chars": 200000,
                "persist_clipboard_prompt": True,
                "require_target_explicit_for_clipboard": False,
                "clipboard_persist_destination": "inbox",
            },
            "paths": {
                "inbox": str(inbox),
                "processing": str(processing),
                "done": str(done),
                "failed": str(failed),
                "results": str(results),
                "logs": str(logs),
            },
            "targets": {
                "codex": {
                    "command": [
                        sys.executable,
                        "-c",
                        "import sys; data=sys.stdin.read(); print('ORCH_STUB:' + data.strip())",
                    ],
                    "input_mode": "stdin",
                },
                "claude": {
                    "command": [
                        sys.executable,
                        "-c",
                        "import sys; data=sys.stdin.read(); print('ORCH_STUB_CLAUDE:' + data.strip())",
                    ],
                    "input_mode": "stdin",
                },
            },
        }
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg_path

    def _step(self, session: Dict[str, Any], step_id: str) -> Dict[str, Any]:
        for step in session.get("steps") or []:
            if step.get("step_id") == step_id:
                return step
        raise AssertionError(f"Step not found: {step_id}")

    def test_full_flow_wait_dispatch_review_complete(self) -> None:
        session = self.service.start_session(snapshot_mode="fixture", chatgpt_api_mode="mock")
        session = self.service.run_until_pause(session)

        self.assertEqual(session["status"], "waiting_for_operator")
        self.assertEqual(session["current_step_id"], "operator_dispatch_decision")
        self.assertEqual(self._step(session, "build_snapshot")["status"], "completed")
        self.assertEqual(self._step(session, "generate_codex_patch_task")["status"], "completed")

        session = self.service.apply_dispatch_decision(session, decision="codex")
        self.assertEqual(session["status"], "running")
        self.assertEqual(session["current_step_id"], "dispatch_patch_prompt")
        self.assertEqual(self._step(session, "dispatch_patch_prompt")["status"], "running")

        session = self.service.run_dispatch_step_async(session)
        self.assertEqual(session["status"], "waiting_for_operator")
        self.assertEqual(session["current_step_id"], "operator_review_assistant_output")
        self.assertEqual(self._step(session, "dispatch_patch_prompt")["status"], "completed")

        session = self.service.continue_after_assistant_output(session)
        self.assertEqual(session["status"], "completed")
        self.assertEqual(self._step(session, "session_summary")["status"], "completed")
        self.assertIn("session_summary", session.get("step_details") or {})

        events_path = self.logs_dir / "events.jsonl"
        snapshot_path = self.logs_dir / "orchestrator_sessions" / f"{session['session_id']}.json"
        self.assertTrue(events_path.exists())
        self.assertTrue(snapshot_path.exists())

    def test_stop_before_dispatch_marks_downstream_steps_skipped(self) -> None:
        session = self.service.start_session(snapshot_mode="fixture", chatgpt_api_mode="mock")
        session = self.service.run_until_pause(session)
        session = self.service.apply_dispatch_decision(session, decision="stop")

        self.assertEqual(session["status"], "completed")
        self.assertEqual(self._step(session, "dispatch_patch_prompt")["status"], "skipped")
        self.assertEqual(self._step(session, "operator_review_assistant_output")["status"], "skipped")

    def test_failed_session_is_terminal_read_only(self) -> None:
        calls = {"analyze": 0}

        def flaky_advisory(task: str, envelope: Dict[str, Any], chatgpt_api_mode: str) -> Dict[str, Any]:
            if task == "analyze_latest_run":
                calls["analyze"] += 1
                if calls["analyze"] == 1:
                    raise RuntimeError("temporary analyze failure")
            return _fake_advisory_caller(task, envelope, chatgpt_api_mode)

        service = OperatorOrchestratorService(
            advisory_caller=flaky_advisory,
            local_runner_caller=_fake_local_runner,
            snapshot_builder_factory=_fake_snapshot_builder_factory,
            logs_dir=self.logs_dir,
            runner_config_path=Path(self.tmp.name) / "runner_config.yaml",
        )

        session = service.start_session(snapshot_mode="fixture", chatgpt_api_mode="mock")
        session = service.run_until_pause(session)
        self.assertEqual(session["status"], "failed")
        self.assertEqual(self._step(session, "analyze_latest_run")["status"], "failed")
        with self.assertRaises(OrchestratorError):
            service.retry_current_step(session)

    def test_default_local_runner_adapter_with_stub_command(self) -> None:
        cfg_path = self._write_stub_runner_config()
        service = OperatorOrchestratorService(
            advisory_caller=_fake_advisory_caller,
            snapshot_builder_factory=_fake_snapshot_builder_factory,
            logs_dir=self.logs_dir,
            runner_config_path=cfg_path,
        )

        session = service.start_session(snapshot_mode="fixture", chatgpt_api_mode="mock")
        session = service.run_until_pause(session)
        self.assertEqual(session["current_step_id"], "operator_dispatch_decision")

        session = service.apply_dispatch_decision(session, decision="codex")
        self.assertEqual(session["status"], "running")
        self.assertEqual(session["current_step_id"], "dispatch_patch_prompt")
        session = service.run_dispatch_step_async(session)
        self.assertEqual(session["status"], "waiting_for_operator")
        self.assertEqual(session["current_step_id"], "operator_review_assistant_output")
        self.assertEqual(self._step(session, "dispatch_patch_prompt")["status"], "completed")

        details = dict((session.get("step_details") or {}).get("dispatch_patch_prompt") or {})
        runner_summary = dict(details.get("runner_result_summary") or {})
        output_file = Path(str(runner_summary.get("output_file")))
        self.assertTrue(output_file.exists())
        output_text = output_file.read_text(encoding="utf-8")
        self.assertIn("ORCH_STUB:", output_text)

    def test_session_persistence_round_trip_and_resume(self) -> None:
        session = self.service.start_session(snapshot_mode="fixture", chatgpt_api_mode="mock")
        session = self.service.run_until_pause(session)
        session_id = str(session["session_id"])

        stored_path = self.logs_dir / "orchestrator_sessions" / f"{session_id}.json"
        self.assertTrue(stored_path.exists())

        resumed = self.service.resume_session(session_id)
        self.assertEqual(str(resumed.get("session_id")), session_id)
        self.assertEqual(str(resumed.get("current_step_id")), "operator_dispatch_decision")
        self.assertEqual(self._step(resumed, "build_snapshot")["status"], "completed")

        listed = self.service.list_sessions(limit=5)
        self.assertTrue(any(str(item.get("session_id")) == session_id for item in listed))

    def test_cancel_during_dispatch_marks_step_failed(self) -> None:
        def slow_runner(
            prompt_text: str,
            target: str,
            session_id: str,
            *,
            cancel_event: Any | None = None,
        ) -> Dict[str, Any]:
            del prompt_text
            for _ in range(50):
                if cancel_event is not None and bool(getattr(cancel_event, "is_set", lambda: False)()):
                    return {
                        "prompt_source": "file",
                        "status": "cancelled",
                        "target": target,
                        "prompt_char_count": 0,
                        "command_used": f"{target} -p <prompt>",
                        "command_mode": "stdin",
                        "output_file": f"/tmp/{session_id}_{target}.out.txt",
                        "stderr_file": None,
                        "prompt_file": f"/tmp/{session_id}_{target}.prompt.txt",
                        "prompt_file_final": f"/tmp/{session_id}_{target}.prompt.failed.txt",
                        "log_file": f"/tmp/{session_id}_{target}.jsonl",
                        "duration_ms": 10,
                        "return_code": 130,
                        "error_summary": "Command cancelled by operator.",
                    }
                time.sleep(0.01)
            return _fake_local_runner("x", target, session_id, cancel_event=cancel_event)

        service = OperatorOrchestratorService(
            advisory_caller=_fake_advisory_caller,
            local_runner_caller=slow_runner,
            snapshot_builder_factory=_fake_snapshot_builder_factory,
            logs_dir=self.logs_dir,
            runner_config_path=Path(self.tmp.name) / "runner_config.yaml",
        )

        session = service.start_session(snapshot_mode="fixture", chatgpt_api_mode="mock")
        session = service.run_until_pause(session)
        session = service.apply_dispatch_decision(session, decision="codex")
        self.assertEqual(session["current_step_id"], "dispatch_patch_prompt")

        cancel_event = threading.Event()
        holder: Dict[str, Any] = {}

        def _run() -> None:
            holder["session"] = service.run_dispatch_step_async(session, cancel_event=cancel_event)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        time.sleep(0.05)
        cancel_event.set()
        thread.join(timeout=2)

        updated = dict(holder.get("session") or {})
        self.assertEqual(str(updated.get("status")), "failed")
        self.assertEqual(self._step(updated, "dispatch_patch_prompt")["status"], "failed")
        self.assertIn("cancelled", str(self._step(updated, "dispatch_patch_prompt").get("error_summary") or "").lower())
        self.assertTrue(
            any("cancel" in str(ev.get("message") or "").lower() for ev in list(updated.get("events") or []))
        )

    def test_failure_details_capture_validation_failures(self) -> None:
        def bad_advisory(task: str, envelope: Dict[str, Any], chatgpt_api_mode: str) -> Dict[str, Any]:
            del envelope, chatgpt_api_mode
            if task == "analyze_latest_run":
                raise AdvisoryError(
                    "Model response failed schema/policy checks.",
                    errors=["high-impact suggestions require operator_confirmation_required=true"],
                    status_code=502,
                )
            return _fake_advisory_caller(task, {}, "mock")

        service = OperatorOrchestratorService(
            advisory_caller=bad_advisory,
            local_runner_caller=_fake_local_runner,
            snapshot_builder_factory=_fake_snapshot_builder_factory,
            logs_dir=self.logs_dir,
            runner_config_path=Path(self.tmp.name) / "runner_config.yaml",
        )

        session = service.start_session(snapshot_mode="fixture", chatgpt_api_mode="real")
        session = service.run_until_pause(session)
        self.assertEqual(session["status"], "failed")
        step = self._step(session, "analyze_latest_run")
        self.assertEqual(step["status"], "failed")
        details = dict((session.get("step_details") or {}).get("analyze_latest_run") or {})
        failure = dict(details.get("failure_details") or {})
        self.assertEqual(failure.get("status_code"), 502)
        self.assertIn(
            "high-impact suggestions require operator_confirmation_required=true",
            list(failure.get("validation_failures") or []),
        )


if __name__ == "__main__":
    unittest.main()
