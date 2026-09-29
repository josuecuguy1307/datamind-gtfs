from __future__ import annotations

from typing import Any, Dict, List, Optional
import uuid

from phase3_routes.services.route_constructor.src.db.conn import db_conn, db_cursor

from ..core.models import InverseDirectionSnapshot, ServiceRouteDirectionSummary


def _as_uuid_text(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except Exception:
        return raw


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _as_text(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    return raw or None


def _route_label(route_ref: Optional[str], route_name: Optional[str]) -> Optional[str]:
    ref_txt = _as_text(route_ref)
    name_txt = _as_text(route_name)
    if ref_txt and name_txt:
        return f"{ref_txt} | {name_txt}"
    return ref_txt or name_txt


def _inventory_sql(*, scoped_filter: str = "", scoped_limit: str = "") -> str:
    return """
        WITH scoped_service_routes AS (
          SELECT sr.service_route_id
          FROM route_raw.service_routes sr
    """ + scoped_filter + """
          ORDER BY sr.updated_at DESC NULLS LAST, sr.created_at DESC
    """ + scoped_limit + """
        )
        SELECT
          sr.service_route_id::text AS service_route_id,
          COALESCE(sr.route_ref, '') AS route_ref,
          COALESCE(sr.route_name, '') AS route_name,
          COALESCE(sr.operator_name, '') AS operator_name,
          d.direction_id::int AS direction_id,
          d.route_id::text AS route_id,
          COALESCE(d.phase3_progress_step, 0)::int AS phase3_progress_step,
          COALESCE(d.direction_approval_status, 'pending') AS direction_approval_status,
          COALESCE(d.geom_source, 'unknown') AS geom_source,
          rj.service_route_id::text AS route_job_service_route_id,
          rj.direction_id::int AS route_job_direction_id,
          rj.chosen_osm_relation_id,
          rp.service_route_id::text AS route_prod_service_route_id,
          rp.direction_id::int AS route_prod_direction_id,
          CASE WHEN rp.route_id IS NULL THEN FALSE ELSE TRUE END AS has_route_prod
        FROM route_raw.service_routes sr
        JOIN scoped_service_routes scoped
          ON scoped.service_route_id = sr.service_route_id
        LEFT JOIN route_raw.service_route_directions d
          ON d.service_route_id = sr.service_route_id
        LEFT JOIN route_raw.route_jobs rj
          ON rj.route_id = d.route_id
        LEFT JOIN route_prod.routes rp
          ON rp.route_id = d.route_id
    """


def _load_inventory_rows(
    *,
    service_route_id: Optional[str] = None,
    limit: Optional[int] = 100,
) -> List[Dict[str, Any]]:
    params: List[Any] = []
    scoped_filter = ""
    scoped_limit = ""
    if service_route_id:
        scoped_filter = "          WHERE sr.service_route_id = %s::uuid\n"
        params.append(str(service_route_id))
    elif limit is not None:
        scoped_limit = "          LIMIT %s\n"
        params.append(max(1, int(limit)))
    sql = _inventory_sql(scoped_filter=scoped_filter, scoped_limit=scoped_limit)
    sql += " ORDER BY sr.updated_at DESC NULLS LAST, sr.created_at DESC, d.direction_id NULLS LAST"

    with db_conn() as conn:
        with db_cursor(conn) as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or []
    return [dict(row or {}) for row in rows]


def _snapshot_context_suspect(snapshot: InverseDirectionSnapshot, *, service_route_id: str) -> bool:
    if not snapshot.route_id:
        return False
    job_sid = _as_uuid_text(snapshot.route_job_service_route_id)
    prod_sid = _as_uuid_text(snapshot.route_prod_service_route_id)
    if job_sid and job_sid != str(service_route_id):
        return True
    if prod_sid and prod_sid != str(service_route_id):
        return True

    job_dir = _as_int(snapshot.route_job_direction_id)
    prod_dir = _as_int(snapshot.route_prod_direction_id)
    if job_dir is not None and job_dir != int(snapshot.direction_id):
        return True
    if prod_dir is not None and prod_dir != int(snapshot.direction_id):
        return True

    # A bound route with no matching job/prod direction context is conservatively suspect.
    if job_sid is None and prod_sid is None:
        return True
    return False


def _build_summary(group_rows: List[Dict[str, Any]]) -> ServiceRouteDirectionSummary:
    first = dict(group_rows[0] or {})
    service_route_id = _as_uuid_text(first.get("service_route_id")) or ""
    route_ref = _as_text(first.get("route_ref"))
    route_name = _as_text(first.get("route_name"))
    operator_name = _as_text(first.get("operator_name"))

    directions_by_id: Dict[int, InverseDirectionSnapshot] = {}
    notes: List[str] = []
    for row in group_rows:
        direction_id = _as_int(row.get("direction_id"))
        if direction_id not in (0, 1):
            continue
        directions_by_id[int(direction_id)] = InverseDirectionSnapshot(
            direction_id=int(direction_id),
            route_id=_as_uuid_text(row.get("route_id")),
            phase3_progress_step=int(row.get("phase3_progress_step") or 0),
            direction_approval_status=str(row.get("direction_approval_status") or "pending"),
            geom_source=str(row.get("geom_source") or "unknown"),
            route_job_service_route_id=_as_uuid_text(row.get("route_job_service_route_id")),
            route_job_direction_id=_as_int(row.get("route_job_direction_id")),
            route_prod_service_route_id=_as_uuid_text(row.get("route_prod_service_route_id")),
            route_prod_direction_id=_as_int(row.get("route_prod_direction_id")),
            has_route_prod=bool(row.get("has_route_prod")),
            chosen_osm_relation_id=_as_int(row.get("chosen_osm_relation_id")),
        )

    present_direction_ids = sorted(directions_by_id.keys())
    missing_direction_ids = [did for did in (0, 1) if did not in directions_by_id]
    bound_direction_ids = [did for did in (0, 1) if directions_by_id.get(did) and directions_by_id[did].route_id]
    route_id_0 = directions_by_id.get(0).route_id if directions_by_id.get(0) else None
    route_id_1 = directions_by_id.get(1).route_id if directions_by_id.get(1) else None

    legacy_suspect = any(
        _snapshot_context_suspect(snapshot, service_route_id=service_route_id)
        for snapshot in directions_by_id.values()
    )
    if missing_direction_ids:
        notes.append("Logical route is missing one or more direction rows.")
    if legacy_suspect:
        notes.append("Bound route context does not cleanly match the logical direction slot.")

    return ServiceRouteDirectionSummary(
        service_route_id=service_route_id,
        route_short_name=route_ref,
        route_label=_route_label(route_ref, route_name),
        route_name=route_name,
        operator_name=operator_name,
        route_id_0=route_id_0,
        route_id_1=route_id_1,
        present_direction_ids=present_direction_ids,
        missing_direction_ids=missing_direction_ids,
        bound_direction_ids=bound_direction_ids,
        directions=[directions_by_id[did] for did in sorted(directions_by_id.keys())],
        legacy_direction_context_suspect=bool(legacy_suspect),
        notes=notes,
    )


def list_service_route_direction_summaries(
    *,
    limit: Optional[int] = 100,
) -> List[ServiceRouteDirectionSummary]:
    rows = _load_inventory_rows(limit=limit)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        service_route_id = _as_uuid_text(row.get("service_route_id")) or ""
        if not service_route_id:
            continue
        grouped.setdefault(service_route_id, []).append(row)
    return [_build_summary(group_rows) for group_rows in grouped.values()]


def get_service_route_direction_summary(service_route_id: str) -> Optional[ServiceRouteDirectionSummary]:
    sid = _as_uuid_text(service_route_id)
    if not sid:
        return None
    rows = _load_inventory_rows(service_route_id=sid, limit=None)
    if not rows:
        return None
    return _build_summary(rows)


def get_route_service_route_context(route_id: str) -> Dict[str, Any]:
    rid = _as_uuid_text(route_id)
    if not rid:
        return {}
    with db_conn() as conn:
        with db_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                  rj.route_id::text AS route_id,
                  COALESCE(d.service_route_id::text, rj.service_route_id::text) AS service_route_id,
                  COALESCE(d.direction_id::int, rj.direction_id::int) AS direction_id
                FROM route_raw.route_jobs rj
                LEFT JOIN route_raw.service_route_directions d
                  ON d.route_id = rj.route_id
                WHERE rj.route_id = %s::uuid
                LIMIT 1
                """,
                (rid,),
            )
            row = cur.fetchone() or {}
    return {
        "route_id": _as_uuid_text(row.get("route_id")),
        "service_route_id": _as_uuid_text(row.get("service_route_id")),
        "direction_id": _as_int(row.get("direction_id")),
    }


def list_inventory_bound_route_ids(
    *,
    exclude_service_route_id: Optional[str] = None,
) -> List[str]:
    params: List[Any] = []
    where = ["srd.route_id IS NOT NULL"]
    sid = _as_uuid_text(exclude_service_route_id)
    if sid:
        where.append("srd.service_route_id <> %s::uuid")
        params.append(sid)

    sql = """
        SELECT DISTINCT srd.route_id::text AS route_id
        FROM route_raw.service_route_directions srd
        WHERE
    """ + " AND ".join(where) + """
        ORDER BY 1
    """
    with db_conn() as conn:
        with db_cursor(conn) as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or []
    out: List[str] = []
    seen: set[str] = set()
    for row in rows:
        route_id = _as_uuid_text((row or {}).get("route_id"))
        if not route_id or route_id in seen:
            continue
        seen.add(route_id)
        out.append(route_id)
    return out
