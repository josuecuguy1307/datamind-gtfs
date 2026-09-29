from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from phase4_semantics.common.db import db_cursor, fetchone, execute_returning, jsonb
from phase4_semantics.naming.normalize import canonicalize_operator, cleanup_text


_TEXT_KEYS = ["stop_name", "name", "member_name", "label", "osm_name", "title"]
_UUID_RX = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _table_exists(schema: str, table: str) -> bool:
    row = fetchone(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        LIMIT 1
        """,
        (schema, table),
    )
    return bool(row)


def _extract_relation_tags(overpass_json: Dict[str, Any], relation_id: Optional[int]) -> Dict[str, Any]:
    elements = overpass_json.get("elements") or []
    if relation_id is not None:
        for el in elements:
            if el.get("type") == "relation" and int(el.get("id") or -1) == int(relation_id):
                return dict(el.get("tags") or {})
    for el in elements:
        if el.get("type") == "relation":
            return dict(el.get("tags") or {})
    return {}


def _load_chosen_relation_id(route_id: str) -> Optional[int]:
    row = fetchone(
        """
        SELECT chosen_osm_relation_id
        FROM route_raw.route_jobs
        WHERE route_id = %s
        LIMIT 1
        """,
        (route_id,),
    )
    if not row:
        return None
    value = row.get("chosen_osm_relation_id")
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _load_overpass_json(route_id: str, relation_id: Optional[int]) -> Optional[Dict[str, Any]]:
    if not _table_exists("route_raw", "osm_relations_raw"):
        return None

    row = None
    if relation_id is not None:
        row = fetchone(
            """
            SELECT overpass_json
            FROM route_raw.osm_relations_raw
            WHERE route_id = %s AND osm_relation_id = %s
            LIMIT 1
            """,
            (route_id, relation_id),
        )
    if not row:
        row = fetchone(
            """
            SELECT overpass_json
            FROM route_raw.osm_relations_raw
            WHERE route_id = %s
            LIMIT 1
            """,
            (route_id,),
        )
    if not row:
        return None
    payload = row.get("overpass_json")
    return payload if isinstance(payload, dict) else None


def _stop_name_from_row(row: Dict[str, Any]) -> str:
    for key in _TEXT_KEYS:
        value = row.get(key)
        if value:
            text = cleanup_text(str(value))
            if text:
                return text
    return ""


def _load_endpoint_names(route_id: str) -> Dict[str, str]:
    if not _table_exists("route_work", "relation_stop_prior"):
        return {"from": "", "to": ""}

    rows = []
    with db_cursor(readonly=True) as cur:
        try:
            cur.execute(
                """
                SELECT *
                FROM route_work.relation_stop_prior
                WHERE route_id = %s
                ORDER BY seq ASC
                """,
                (route_id,),
            )
            rows = [dict(r) for r in (cur.fetchall() or [])]
        except Exception:
            rows = []

    if not rows:
        return {"from": "", "to": ""}

    first = _stop_name_from_row(rows[0])
    last = _stop_name_from_row(rows[-1])
    return {"from": first, "to": last}


def _parse_gtfs_bridge_from_notes(notes: str) -> Dict[str, str]:
    text = str(notes or "")
    out: Dict[str, str] = {}

    m_run = re.search(rf"export_run_id=({_UUID_RX})", text)
    if m_run:
        out["export_run_id"] = str(m_run.group(1))

    # Bridge currently writes "... | route_id=<gtfs_route_id> | ..."
    m_route = re.search(r"route_id=([^|]+)", text)
    if m_route:
        rid = str(m_route.group(1) or "").strip()
        if rid and rid != "(all)":
            out["gtfs_route_id"] = rid

    return out


def _load_route_job_bridge(route_id: str) -> Dict[str, str]:
    row = fetchone(
        """
        SELECT notes, known_ref
        FROM route_raw.route_jobs
        WHERE route_id = %s
        LIMIT 1
        """,
        (route_id,),
    ) or {}
    notes = str(row.get("notes") or "")
    out = _parse_gtfs_bridge_from_notes(notes)
    known_ref = cleanup_text(str(row.get("known_ref") or ""))
    if known_ref:
        out["known_ref"] = known_ref
    return out


def _load_gtfs_route_row(*, export_run_id: str, gtfs_route_id: str = "", known_ref: str = "") -> Dict[str, Any]:
    if not _table_exists("gtfs_work", "gtfs_routes"):
        return {}

    run_id = str(export_run_id or "").strip()
    rid = str(gtfs_route_id or "").strip()
    ref = cleanup_text(str(known_ref or ""))
    if not run_id:
        return {}

    if rid:
        row = fetchone(
            """
            SELECT
              route_id::text AS gtfs_route_id,
              route_short_name,
              route_long_name
            FROM gtfs_work.gtfs_routes
            WHERE export_run_id = %s
              AND route_id = %s
            LIMIT 1
            """,
            (run_id, rid),
        )
        if row:
            return dict(row)

    if ref:
        row = fetchone(
            """
            SELECT
              route_id::text AS gtfs_route_id,
              route_short_name,
              route_long_name
            FROM gtfs_work.gtfs_routes
            WHERE export_run_id = %s
              AND (
                COALESCE(route_short_name, '') = %s
                OR route_id = %s
              )
            ORDER BY route_id
            LIMIT 1
            """,
            (run_id, ref, ref),
        )
        if row:
            return dict(row)

    return {}


def _persist_seed_run(route_id: str, seed: Dict[str, Any]) -> Optional[str]:
    if not _table_exists("semantics", "route_name_seed_runs"):
        return None

    row = execute_returning(
        """
        INSERT INTO semantics.route_name_seed_runs (
            route_id,
            seed_source,
            chosen_osm_relation_id,
            seed_route_name,
            seed_route_ref,
            seed_operator_name,
            seed_from_name,
            seed_to_name,
            seed_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING seed_run_id
        """,
        (
            route_id,
            seed.get("seed_source"),
            seed.get("osm_relation_id"),
            seed.get("route_name"),
            seed.get("route_ref"),
            seed.get("operator_name"),
            seed.get("from_name"),
            seed.get("to_name"),
            jsonb(seed.get("raw") or {}),
        ),
    )
    seed_run_id = row.get("seed_run_id")
    return str(seed_run_id) if seed_run_id else None


def extract_seed_for_route(route_id: str, *, persist: bool = True) -> Dict[str, Any]:
    chosen_relation_id = _load_chosen_relation_id(route_id)
    overpass_json = _load_overpass_json(route_id, chosen_relation_id)
    endpoints = _load_endpoint_names(route_id)

    bridge = _load_route_job_bridge(route_id)
    gtfs_route = _load_gtfs_route_row(
        export_run_id=str(bridge.get("export_run_id") or ""),
        gtfs_route_id=str(bridge.get("gtfs_route_id") or ""),
        known_ref=str(bridge.get("known_ref") or ""),
    )
    gtfs_short = cleanup_text(str(gtfs_route.get("route_short_name") or ""))
    gtfs_long = cleanup_text(str(gtfs_route.get("route_long_name") or ""))

    if overpass_json:
        tags = _extract_relation_tags(overpass_json, chosen_relation_id)
        route_name = gtfs_long or cleanup_text(str(tags.get("name") or ""))
        route_ref = gtfs_short or cleanup_text(str(tags.get("ref") or ""))
        if not route_name and route_ref:
            route_name = route_ref
        seed = {
            "route_id": route_id,
            "seed_source": "osm_relation",
            "osm_relation_id": chosen_relation_id,
            "route_name": route_name,
            "route_ref": route_ref,
            "operator_name": canonicalize_operator(str(tags.get("operator") or "")),
            "from_name": cleanup_text(str(tags.get("from") or "")) or endpoints.get("from", ""),
            "to_name": cleanup_text(str(tags.get("to") or "")) or endpoints.get("to", ""),
            "network": cleanup_text(str(tags.get("network") or "")),
            "raw": {
                "tags": tags,
                "gtfs_route": gtfs_route,
                "bridge": bridge,
            },
        }
    else:
        route_name = gtfs_long or cleanup_text(f"{endpoints.get('from', '')} - {endpoints.get('to', '')}")
        route_ref = gtfs_short or cleanup_text(str(bridge.get("known_ref") or ""))
        if not route_name and route_ref:
            route_name = route_ref
        seed = {
            "route_id": route_id,
            "seed_source": "endpoint_fallback",
            "osm_relation_id": chosen_relation_id,
            "route_name": route_name,
            "route_ref": route_ref,
            "operator_name": "",
            "from_name": endpoints.get("from", ""),
            "to_name": endpoints.get("to", ""),
            "network": "",
            "raw": {
                "fallback": "first_last_stop",
                "gtfs_route": gtfs_route,
                "bridge": bridge,
            },
        }

    seed_run_id = _persist_seed_run(route_id, seed) if persist else None
    out = dict(seed)
    out["seed_run_id"] = seed_run_id
    return out
