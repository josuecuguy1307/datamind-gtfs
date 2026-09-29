from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException, Request

from datamind_console.api_chatgpt.services.advisory_service import AdvisoryError, AdvisoryService


router = APIRouter(tags=["AI Bot Advisory"])
_service = AdvisoryService()


def _require_api_key_if_configured(req: Request) -> None:
    expected = str(os.getenv("DATAMIND_API_KEY", "") or "").strip()
    if not expected:
        return

    auth = str(req.headers.get("Authorization") or "").strip()
    x_key = str(req.headers.get("X-API-Key") or "").strip()

    token = ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()

    if token == expected or x_key == expected:
        return

    raise HTTPException(status_code=401, detail="Unauthorized.")


def _run_task(task: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return _service.run_task(endpoint_task=task, envelope=payload)
    except AdvisoryError as e:
        raise HTTPException(
            status_code=int(e.status_code),
            detail={
                "message": e.message,
                "errors": e.errors,
                "detail": dict(getattr(e, "detail", {}) or {}),
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"message": "Advisory task failed.", "error": str(e)})


def _legacy_pipeline_template_requested(payload: Dict[str, Any]) -> bool:
    body = dict(payload or {})
    for key in ("legacy_task", "use_legacy_template", "force_legacy_template"):
        if bool(body.get(key)):
            return True
    return False


@router.post("/api/ai-bot/analyze-run")
def api_analyze_run(req: Request, payload: Dict[str, Any] = Body(...)):
    _require_api_key_if_configured(req)
    body = dict(payload or {})
    body["task"] = "analyze_latest_run"
    return _run_task("analyze_latest_run", body)


@router.post("/api/ai-bot/review-bot-quality")
def api_review_bot_quality(req: Request, payload: Dict[str, Any] = Body(...)):
    _require_api_key_if_configured(req)
    body = dict(payload or {})
    body["task"] = "review_ai_bot_quality"
    return _run_task("review_ai_bot_quality", body)


@router.post("/api/ai-bot/generate-codex-task")
def api_generate_codex_task(req: Request, payload: Dict[str, Any] = Body(...)):
    _require_api_key_if_configured(req)
    body = dict(payload or {})
    task = "generate_codex_patch_task" if _legacy_pipeline_template_requested(body) else "hades_patch_task_generator"
    body["task"] = task
    return _run_task(task, body)


@router.post("/api/ai-bot/compare-retest")
def api_compare_retest(req: Request, payload: Dict[str, Any] = Body(...)):
    _require_api_key_if_configured(req)
    body = dict(payload or {})
    body["task"] = "hades_retest_comparator"
    return _run_task("hades_retest_comparator", body)


@router.post("/api/ai-bot/check-evidence-consistency")
def api_check_evidence_consistency(req: Request, payload: Dict[str, Any] = Body(...)):
    _require_api_key_if_configured(req)
    body = dict(payload or {})
    body["task"] = "hades_evidence_consistency_checker"
    return _run_task("hades_evidence_consistency_checker", body)


@router.post("/api/ai-bot/interpret-pipeline-blocker")
def api_interpret_pipeline_blocker(req: Request, payload: Dict[str, Any] = Body(...)):
    _require_api_key_if_configured(req)
    body = dict(payload or {})
    task = "interpret_pipeline_blocker" if _legacy_pipeline_template_requested(body) else "hades_pipeline_interpreter"
    body["task"] = task
    return _run_task(task, body)


@router.post("/api/ai-bot/prioritize-pipeline-resolution")
def api_prioritize_pipeline_resolution(req: Request, payload: Dict[str, Any] = Body(...)):
    _require_api_key_if_configured(req)
    body = dict(payload or {})
    task = "prioritize_pipeline_resolution" if _legacy_pipeline_template_requested(body) else "hades_pipeline_interpreter"
    body["task"] = task
    return _run_task(task, body)


@router.post("/api/ai-bot/explain-cleanup-risk")
def api_explain_cleanup_risk(req: Request, payload: Dict[str, Any] = Body(...)):
    _require_api_key_if_configured(req)
    body = dict(payload or {})
    task = "explain_cleanup_risk" if _legacy_pipeline_template_requested(body) else "hades_pipeline_interpreter"
    body["task"] = task
    return _run_task(task, body)


@router.post("/api/ai-bot/interpret-merge-evidence")
def api_interpret_merge_evidence(req: Request, payload: Dict[str, Any] = Body(...)):
    _require_api_key_if_configured(req)
    body = dict(payload or {})
    task = "interpret_merge_evidence" if _legacy_pipeline_template_requested(body) else "hades_pipeline_interpreter"
    body["task"] = task
    return _run_task(task, body)
