from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional
from phase3_routes.services.route_constructor.src.db.conn import db_cursor


def _to_uuid_str(x: Any) -> str:
    if isinstance(x, uuid.UUID):
        return str(x)
    return str(uuid.UUID(str(x)))


def _row_get(row: Any, key: str, idx: int) -> Any:
    """
    Supports dict-like rows OR tuple rows.
    If tuple, you must pass a stable index.
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    return row[idx]


def create_route_job(
    conn,
    created_by: str | None = None,
    notes: str | None = None,
    *,
    province: str,
) -> uuid.UUID:
    """
    Insert a new route_raw.route_jobs row.

    ``province`` is keyword-only and REQUIRED per Skill 11 §7 ("new INSERTs
    MUST set province explicitly"). Passing an empty/None value raises
    ValueError instead of silently falling back to the column DEFAULT
    ('sample_region'). The historical default-to-sample_region behavior produced
    cross-province data contamination (see workspace/_audit/
    route_jobs_province_fix_20260409.md).
    """
    if not province or not str(province).strip():
        raise ValueError(
            "create_route_job: province is required and must be non-empty. "
            "Skill 11 §7 forbids relying on the 'sample_region' DEFAULT for new INSERTs."
        )
    province_norm = str(province).strip().lower()
    with db_cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO route_raw.route_jobs(created_by, notes, province)
            VALUES (%s, %s, %s)
            RETURNING route_id
            """,
            (created_by, notes, province_norm),
        )
        row = cur.fetchone()

    if not row:
        raise RuntimeError("create_route_job: INSERT returned no row")

    # RETURNING route_id is column 0 if tuple, or ["route_id"] if dict
    route_id_val = _row_get(row, "route_id", 0)
    return uuid.UUID(str(route_id_val))


def upsert_relation_raw(
    conn,
    route_id: uuid.UUID,
    osm_relation_id: int,
    overpass_json: dict,
) -> None:
    route_id_s = _to_uuid_str(route_id)

    # ensure JSON is serializable (and stable)
    payload_json = json.dumps(overpass_json, ensure_ascii=False)

    with db_cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO route_raw.osm_relations_raw(route_id, osm_relation_id, overpass_json)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (route_id) DO UPDATE
              SET osm_relation_id = EXCLUDED.osm_relation_id,
                  fetched_at = now(),
                  overpass_json = EXCLUDED.overpass_json
            """,
            (route_id_s, int(osm_relation_id), payload_json),
        )


def fetch_relation_raw(
    conn,
    route_id: uuid.UUID,
) -> Optional[Dict[str, Any]]:
    """
    Returns a dict with the raw relation row, regardless of cursor row type.
    Keys: route_id, osm_relation_id, fetched_at, overpass_json
    """
    route_id_s = _to_uuid_str(route_id)

    with db_cursor(conn) as cur:
        cur.execute(
            """
            SELECT route_id, osm_relation_id, fetched_at, overpass_json
            FROM route_raw.osm_relations_raw
            WHERE route_id = %s
            """,
            (route_id_s,),
        )
        row = cur.fetchone()

    if not row:
        return None

    if isinstance(row, dict):
        # already dict-like
        return {
            "route_id": str(row.get("route_id")),
            "osm_relation_id": int(row.get("osm_relation_id")),
            "fetched_at": row.get("fetched_at"),
            "overpass_json": row.get("overpass_json"),
        }

    # tuple row in SELECT order
    return {
        "route_id": str(row[0]),
        "osm_relation_id": int(row[1]),
        "fetched_at": row[2],
        "overpass_json": row[3],
    }
