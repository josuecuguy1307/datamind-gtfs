from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from datamind_console.services.gtfs_exports_service import (
    approve_artifact,
    create_pending_artifact,
    get_artifact,
    get_artifact_download_if_allowed,
    list_artifacts,
    mark_artifact_downloaded,
    process_whatsapp_inbound,
    reject_artifact,
    verify_whatsapp_challenge,
)

router = APIRouter(tags=["GTFS Exports"])


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


@router.post("/api/gtfs/exports/create")
def api_create_gtfs_export(
    req: Request,
    zip_path: str = Body(..., embed=True),
    summary_json: Optional[dict[str, Any]] = Body(default=None, embed=True),
    notes: Optional[str] = Body(default=None, embed=True),
):
    _require_api_key_if_configured(req)
    actor = str(req.headers.get("X-Actor") or "api:create")
    try:
        out = create_pending_artifact(
            zip_path=str(zip_path),
            summary_json=(summary_json or {}),
            actor=actor,
            notes=notes,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "artifact": out}


@router.get("/api/gtfs/exports")
def api_list_gtfs_exports(
    req: Request,
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
):
    _require_api_key_if_configured(req)
    rows = list_artifacts(status=status, limit=int(limit))
    return {"ok": True, "items": rows}


@router.post("/api/gtfs/exports/{artifact_id}/approve")
def api_approve_gtfs_export(
    artifact_id: str,
    req: Request,
    approved_by: Optional[str] = Body(default=None, embed=True),
    notes: Optional[str] = Body(default=None, embed=True),
):
    _require_api_key_if_configured(req)
    actor = str(approved_by or req.headers.get("X-Actor") or "api:approve")
    out = approve_artifact(artifact_id=artifact_id, actor=actor, notes=notes)
    if not out:
        raise HTTPException(status_code=404, detail="Artifact not found or not approvable.")
    return {"ok": True, "artifact": out}


@router.post("/api/gtfs/exports/{artifact_id}/reject")
def api_reject_gtfs_export(
    artifact_id: str,
    req: Request,
    rejected_by: Optional[str] = Body(default=None, embed=True),
    notes: Optional[str] = Body(default=None, embed=True),
):
    _require_api_key_if_configured(req)
    actor = str(rejected_by or req.headers.get("X-Actor") or "api:reject")
    out = reject_artifact(artifact_id=artifact_id, actor=actor, notes=notes)
    if not out:
        raise HTTPException(status_code=404, detail="Artifact not found or not rejectable.")
    return {"ok": True, "artifact": out}


@router.get("/api/gtfs/exports/{artifact_id}/download")
def api_download_gtfs_export(artifact_id: str, req: Request):
    _require_api_key_if_configured(req)
    try:
        out = get_artifact_download_if_allowed(artifact_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))

    artifact = out["artifact"]
    p = Path(out["path"])
    return FileResponse(
        path=str(p),
        media_type="application/zip",
        filename=p.name,
        headers={"X-Artifact-Id": str(artifact.get("artifact_id") or "")},
    )


@router.post("/api/gtfs/exports/{artifact_id}/mark-downloaded")
def api_mark_downloaded(
    artifact_id: str,
    req: Request,
    actor: Optional[str] = Body(default=None, embed=True),
    notes: Optional[str] = Body(default=None, embed=True),
):
    _require_api_key_if_configured(req)
    who = str(actor or req.headers.get("X-Actor") or "api:mark_downloaded")
    out = mark_artifact_downloaded(artifact_id=artifact_id, actor=who, notes=notes)
    if not out:
        raise HTTPException(status_code=404, detail="Artifact not found or not downloadable.")
    return {"ok": True, "artifact": out}


@router.get("/api/whatsapp/inbound")
def api_whatsapp_verify(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
):
    challenge = verify_whatsapp_challenge(
        mode=str(hub_mode or ""),
        token=str(hub_verify_token or ""),
        challenge=str(hub_challenge or ""),
    )
    if challenge is None:
        raise HTTPException(status_code=403, detail="Webhook verify failed.")
    return PlainTextResponse(challenge, status_code=200)


@router.post("/api/whatsapp/inbound")
async def api_whatsapp_inbound(req: Request):
    try:
        payload = await req.json()
    except Exception:
        payload = {}
    try:
        out = process_whatsapp_inbound(payload if isinstance(payload, dict) else {})
    except Exception as e:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(e)})
    return {"ok": True, "result": out}


@router.get("/api/gtfs/exports/{artifact_id}")
def api_get_gtfs_export(artifact_id: str, req: Request):
    _require_api_key_if_configured(req)
    row = get_artifact(artifact_id)
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return {"ok": True, "artifact": row}
