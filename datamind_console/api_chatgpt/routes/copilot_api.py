from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from datamind_console.api_chatgpt.services.advisory_service import AdvisoryError
from datamind_console.api_chatgpt.services.copilot_service import PipelineCopilotService


router = APIRouter(tags=["Pipeline Copilot"])
_service = PipelineCopilotService()


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


def _sse_event(payload: Dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def _to_str(value: Any, default: str = "") -> str:
    out = str(value or "").strip()
    return out or default


@router.get("/api/copilot/sessions")
def api_list_sessions(req: Request, limit: int = Query(50, ge=1, le=300)) -> Dict[str, Any]:
    _require_api_key_if_configured(req)
    rows = _service.list_sessions(limit=int(limit))
    return {"sessions": rows, "count": len(rows)}


@router.post("/api/copilot/sessions")
def api_create_session(req: Request, payload: Dict[str, Any] = Body({})) -> Dict[str, Any]:
    _require_api_key_if_configured(req)
    body = dict(payload or {})
    session = _service.create_session(
        title=_to_str(body.get("title"), "Pipeline Copilot Session"),
        mode=_to_str(body.get("mode"), "pipeline_operator"),
        created_by=_to_str(body.get("created_by"), "") or None,
        context_defaults=(dict(body.get("context_defaults")) if isinstance(body.get("context_defaults"), dict) else {}),
    )
    return {"session": session}


@router.get("/api/copilot/sessions/{session_id}/messages")
def api_list_messages(
    req: Request,
    session_id: str,
    limit: int = Query(500, ge=1, le=5000),
) -> Dict[str, Any]:
    _require_api_key_if_configured(req)
    sid = _to_str(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")
    if _service.store.get_session(sid) is None:
        raise HTTPException(status_code=404, detail=f"session not found: {sid}")
    rows = _service.list_messages(session_id=sid, limit=int(limit))
    return {"session_id": sid, "messages": rows, "count": len(rows)}


def _stream_chat_events(result: Dict[str, Any]) -> Iterable[str]:
    assistant_text = str(result.get("assistant_text") or "")
    yield _sse_event({"type": "message_start", "session_id": result.get("session_id"), "task": result.get("task")})
    for chunk in _service.iter_text_chunks(assistant_text, chunk_size=140):
        yield _sse_event({"type": "delta", "content": str(chunk)})
    yield _sse_event(
        {
            "type": "message_end",
            "session_id": result.get("session_id"),
            "task": result.get("task"),
            "trace_id": result.get("trace_id"),
            "assistant_message": result.get("assistant_message"),
            "meta": result.get("meta"),
            "advisory_only": True,
        }
    )


@router.post("/api/copilot/chat")
def api_copilot_chat(req: Request, payload: Dict[str, Any] = Body(...)) -> Any:
    _require_api_key_if_configured(req)
    body = dict(payload or {})
    sid = _to_str(body.get("session_id"))
    message = _to_str(body.get("message"))
    stream = bool(body.get("stream", False))
    context = dict(body.get("context")) if isinstance(body.get("context"), dict) else {}
    operator_context = dict(body.get("operator_context")) if isinstance(body.get("operator_context"), dict) else {}
    template = _to_str(body.get("template")) or None

    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    if len(message) > 20000:
        raise HTTPException(status_code=413, detail="message exceeds 20,000 characters")

    try:
        result = _service.chat(
            session_id=sid,
            message=message,
            context=context,
            template=template,
            operator_context=operator_context,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except AdvisoryError as exc:
        raise HTTPException(
            status_code=int(exc.status_code),
            detail={
                "message": exc.message,
                "errors": list(exc.errors or []),
                "detail": dict(getattr(exc, "detail", {}) or {}),
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"message": "Copilot chat failed.", "error": str(exc)})

    if stream:
        return StreamingResponse(
            _stream_chat_events(result),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return result
