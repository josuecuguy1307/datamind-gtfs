from __future__ import annotations

from typing import Any, Dict

from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import db_conn
from phase1_nodes.datamind.services.openmaps_extractor.src.db.overpass_repo import (
    ensure_action,
    insert_query,
    insert_run,
    bulk_insert_elements,
)
from phase1_nodes.datamind.services.openmaps_extractor.src.overpass.actions import build_query
from phase1_nodes.datamind.services.openmaps_extractor.src.overpass.client import run_overpass


ACTIONS_JSON = "actions.json"


def _parse_bbox_from_string(bbox_str: str) -> Dict[str, float] | None:
    parts = [p.strip() for p in str(bbox_str).split(",") if p.strip()]
    if len(parts) != 4:
        return None
    try:
        south, west, north, east = [float(x) for x in parts]
        return {"south": south, "west": west, "north": north, "east": east}
    except (ValueError, TypeError):
        return None


def run_extract(
    action_id: str,
    params: Dict[str, Any],
    actions_path: str = ACTIONS_JSON,
    area_id: str | None = None,
) -> dict:
    # 1) Build query from actions.json + template
    spec, merged_params, query_text = build_query(actions_path, action_id, params)

    # 2) Run Overpass request
    data, http_status, response_bytes, runtime_ms = run_overpass(query_text)

    elements = (data or {}).get("elements", []) or []

    # Recover bbox as JSON dict from string or dict param
    bbox_val = merged_params.get("bbox")
    if isinstance(bbox_val, dict):
        bbox_json = bbox_val
    elif isinstance(bbox_val, str):
        bbox_json = _parse_bbox_from_string(bbox_val)
    else:
        bbox_json = None

    template_path_str = str(spec.template_path)

    # 3) Persist to DB
    with db_conn() as conn:
        ensure_action(
            conn,
            action_id=action_id,
            template_path=template_path_str,
            default_params=spec.default_params,
        )

        query_id = insert_query(
            conn,
            action_id=action_id,
            query_text=query_text,
            params=merged_params,
        )

        run_id = insert_run(
            conn,
            query_id=query_id,
            bbox=bbox_json,
            status="ok",
            runtime_ms=runtime_ms,
            element_count=len(elements),
            response_bytes=response_bytes,
            area_id=area_id,
        )

        bulk_insert_elements(conn, run_id, elements)

    # 4) Return metadata (safe for printing)
    return {
        "run_id": str(run_id),
        "query_id": str(query_id),
        "http_status": http_status,
        "elements": len(elements),
        "bytes": response_bytes,
        "runtime_ms": runtime_ms,
        "template": template_path_str,
        "merged_params": merged_params,
        "area_id": area_id,
        "bbox_stored": bbox_json,
    }
