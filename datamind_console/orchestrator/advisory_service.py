from __future__ import annotations

from typing import Any, Callable, Dict, Optional

try:
    from datamind_console.api_chatgpt.services.advisory_service import AdvisoryService
    from datamind_console.api_chatgpt.services.openai_client import build_default_model_client
    from datamind_console.api_chatgpt.services.snapshot_builder import SnapshotBuilder
except Exception:  # pragma: no cover
    AdvisoryService = None  # type: ignore[assignment,misc]
    build_default_model_client = None  # type: ignore[assignment]
    SnapshotBuilder = None  # type: ignore[assignment]

from datamind_console.orchestrator.service import SAFETY_CONTEXT


def run_advisory_step(
    session_id: str,
    step: dict,
    *,
    task_name: str,
    snapshot_mode: str = "fixture",
    operator_context: Optional[dict] = None,
) -> dict:
    if AdvisoryService is None or build_default_model_client is None:
        return {
            "ok": True,
            "stub": True,
            "message": "AdvisoryService not available. Stub pass-through.",
        }

    try:
        client = build_default_model_client()
        advisory = AdvisoryService(model_client=client)
        result = advisory.run(
            task_name=task_name,
            snapshot_mode=snapshot_mode,
            safety_context=SAFETY_CONTEXT,
            operator_context=operator_context or {},
        )
        if hasattr(result, "to_dict"):
            return {"ok": True, **result.to_dict()}
        if isinstance(result, dict):
            return {"ok": True, **result}
        return {"ok": True, "raw": str(result)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def make_analyze_executor() -> Callable:
    def _executor(session_id: str, step: dict) -> dict:
        return run_advisory_step(
            session_id, step,
            task_name="analyze_latest_run",
            snapshot_mode="fixture",
        )
    return _executor


def make_quality_executor() -> Callable:
    def _executor(session_id: str, step: dict) -> dict:
        return run_advisory_step(
            session_id, step,
            task_name="review_ai_bot_quality",
            snapshot_mode="fixture",
        )
    return _executor


def make_patch_task_executor() -> Callable:
    def _executor(session_id: str, step: dict) -> dict:
        return run_advisory_step(
            session_id, step,
            task_name="hades_patch_task_generator",
            snapshot_mode="fixture",
        )
    return _executor
