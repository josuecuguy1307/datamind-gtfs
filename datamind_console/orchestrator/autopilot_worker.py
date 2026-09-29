from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from datamind_console.orchestrator.autopilot_feature_flags import AutopilotFeatureFlags
from datamind_console.orchestrator.pipeline_autopilot import (
    AdvisoryChatGPTInterpreter,
    SupervisedPipelineAutopilot,
    build_ai_bot_telemetry_hook,
    build_default_pipeline_step_registry,
    build_phase_client_executor_bridge,
    build_phase_client_validator_bridge,
)


@dataclass
class WorkerResult:
    claimed: bool
    run_id: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None



def _try_build_live_clients() -> Tuple[Optional[Any], Optional[Any], Optional[Any]]:
    phase1_client = None
    phase2_client = None
    phase3_client = None

    try:
        from phases.phase1_nodes.client import _get_phase1_client

        phase1_client = _get_phase1_client("v3.newnodes.fix_json_approve2")
    except Exception:
        phase1_client = None

    try:
        from datamind_console.phases.phase2_semantics.client import Phase2Client

        phase2_client = Phase2Client()
    except Exception:
        phase2_client = None

    try:
        from datamind_console.phases.phase3_routes.client import _get_phase3_client

        phase3_client = _get_phase3_client()
    except Exception:
        phase3_client = None

    return phase1_client, phase2_client, phase3_client



def build_worker_engine() -> SupervisedPipelineAutopilot:
    flags = AutopilotFeatureFlags.from_env()
    registry = build_default_pipeline_step_registry()

    p1, p2, p3 = _try_build_live_clients()
    executors = build_phase_client_executor_bridge(
        phase1_client=p1,
        phase2_client=p2,
        phase3_client=p3,
    )
    validators = build_phase_client_validator_bridge()

    ai_hook = build_ai_bot_telemetry_hook()
    ai_hooks = {str(name): ai_hook for step in registry.values() for name in (step.ai_bot_hooks or [])}

    try:
        chatgpt = AdvisoryChatGPTInterpreter()
    except Exception:
        chatgpt = AdvisoryChatGPTInterpreter(endpoint_task="hades_pipeline_interpreter")

    return SupervisedPipelineAutopilot(
        step_registry=registry,
        executors=executors,
        validators=validators,
        ai_bot_hooks=ai_hooks,
        chatgpt_interpreter=chatgpt,
        enabled=True,
        persist_runs=True,
        persistence_backend="db",
        feature_flags=flags,
    )


class PipelineAutopilotWorker:
    def __init__(
        self,
        *,
        engine: Optional[SupervisedPipelineAutopilot] = None,
        worker_id: Optional[str] = None,
        burst_steps: int = 8,
        lease_seconds: int = 180,
    ) -> None:
        self.engine = engine or build_worker_engine()
        self.worker_id = str(worker_id or f"autopilot-worker@{socket.gethostname()}")
        self.burst_steps = max(1, int(burst_steps))
        self.lease_seconds = max(30, int(lease_seconds))

    def run_once(self) -> WorkerResult:
        db = self.engine.db_store
        if not db.available:
            return WorkerResult(claimed=False, error="db_store_unavailable")

        job = db.claim_next_queue_job(worker_id=self.worker_id, lease_seconds=self.lease_seconds)
        if not job:
            return WorkerResult(claimed=False)

        job_id = str(job.get("job_id") or "")
        run_id = str(job.get("run_id") or "")
        try:
            self.engine._load_persisted_runs()
            state = self.engine.get_run(run_id)

            loops = 0
            while state.status == "running" and state.current_step_id and loops < 20:
                db.heartbeat_queue_job(job_id=job_id, worker_id=self.worker_id, lease_seconds=self.lease_seconds)
                state = self.engine.advance_run(run_id, max_steps=self.burst_steps)
                loops += 1

            self.engine.evaluate_slo_alerts(run_id=run_id)
            db.complete_queue_job(job_id=job_id, status="completed")

            # Keep autonomous progress alive while the run is still active.
            if state.status == "running" and state.current_step_id:
                db.enqueue_run(
                    run_id=run_id,
                    requested_by=self.worker_id,
                    idempotency_key=f"{run_id}:worker-resume:{int(time.time())}",
                )

            return WorkerResult(claimed=True, run_id=run_id, status=state.status)
        except Exception as exc:
            db.complete_queue_job(job_id=job_id, status="failed", error_message=str(exc))
            return WorkerResult(claimed=True, run_id=run_id, status="failed", error=str(exc))

    def run_forever(self, *, poll_seconds: float = 2.0) -> None:
        delay = max(0.5, float(poll_seconds))
        while True:
            result = self.run_once()
            if not result.claimed:
                time.sleep(delay)


def main() -> None:
    worker = PipelineAutopilotWorker(
        burst_steps=int(os.getenv("HADES_AUTOPILOT_WORKER_BURST_STEPS", "8") or "8"),
        lease_seconds=int(os.getenv("HADES_AUTOPILOT_WORKER_LEASE_S", "180") or "180"),
    )
    worker.run_forever(poll_seconds=float(os.getenv("HADES_AUTOPILOT_WORKER_POLL_S", "2") or "2"))


if __name__ == "__main__":
    main()
