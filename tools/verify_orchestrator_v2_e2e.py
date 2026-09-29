#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datamind_console.orchestrator.role_model_service import RoleModelService


def _step(session: dict, step_key: str) -> dict:
    for row in session.get("steps") or []:
        if str(row.get("step_key") or "") == step_key:
            return row
    raise RuntimeError(f"Step not found: {step_key}")


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        logs_dir = Path(tmp) / "orch_v2_verify"
        service = RoleModelService(logs_dir=logs_dir)

        session = service.start_guided_workflow(
            operator_id="verify-operator",
            template="AI_ASSISTED_REVIEW_PATCH_TASK",
            snapshot_source_mode="fixture",
            preferred_target="codex",
        )

        session = service.run_to_operator_checkpoint(session["session_id"])
        if session.get("state") != "waiting_for_operator":
            failures.append("Session did not pause at waiting_for_operator checkpoint.")

        if _step(session, "dispatch_to_assistant").get("status") != "pending":
            failures.append("Dispatch step advanced before operator approval.")

        pre_artifact_types = [str(a.get("artifact_type") or "") for a in session.get("artifacts") or []]
        if "runner_dispatch_meta" in pre_artifact_types:
            failures.append("Runner dispatch artifact exists before operator approval.")

        session = service.submit_dispatch_decision(
            session["session_id"],
            operator_id="verify-operator",
            decision="dispatch_to_codex",
        )
        if _step(session, "dispatch_to_assistant").get("status") != "success":
            failures.append("Dispatch did not complete after operator approval.")

        if _step(session, "apply_patch_and_retest").get("status") != "pending":
            failures.append("Apply step was auto-executed; expected pending/manual.")

        session = service.record_patch_review(
            session["session_id"],
            operator_id="verify-operator",
            outcome="applied",
            notes="verified review",
            commit_hash="verify123",
        )

        session = service.record_retest_results(
            session["session_id"],
            operator_id="verify-operator",
            tests_run=["orchestrator_v2_smoke"],
            passed=True,
            notes="fixture smoke pass",
        )

        session = service.capture_operator_labels(
            session["session_id"],
            operator_id="verify-operator",
            labels={
                "sequence_quality_label": "good",
                "warning_correctness_label": "acceptable",
                "merge_decision_quality_label": "good",
                "notes": "fixture labels",
            },
        )

        session = service.close_session(session["session_id"], operator_id="verify-operator")

        if session.get("state") != "session_closed":
            failures.append("Session did not close correctly.")

        events_file = logs_dir / "events_v2.jsonl"
        if not events_file.exists():
            failures.append("Event log file missing.")
        else:
            rows = []
            with events_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rows.append(json.loads(line))
            if not rows:
                failures.append("No events captured in event log.")

    if failures:
        print("ORCHESTRATOR_V2_E2E: FAIL")
        for i, err in enumerate(failures, start=1):
            print(f"{i}. {err}")
        return 1

    print("ORCHESTRATOR_V2_E2E: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
