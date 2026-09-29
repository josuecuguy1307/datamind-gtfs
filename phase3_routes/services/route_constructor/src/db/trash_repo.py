"""
route_trash repository  --  recoverable deletion / papelera for Phase 3 routes.

Usage
-----
    from phase3_routes.services.route_constructor.src.db.conn import db_conn, db_cursor
    from phase3_routes.services.route_constructor.src.db.trash_repo import (
        trash_route, restore_route, list_trash, get_trash_item,
    )

    with db_conn() as conn:
        trash_id = trash_route(
            conn,
            route_id=some_uuid,
            reason="duplicate of canonical route X",
            workflow="dedupe_cleanup",
            actor="operator:juan",
        )

        # later ...
        restore_route(conn, trash_id=trash_id, actor="operator:juan")
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor

from datamind_console.persistence import delete_route_prod


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _uuid_str(v) -> str:
    return str(v) if v else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _table_exists(cur, qualified_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (qualified_name,))
    row = cur.fetchone() or {}
    return bool(row.get("ok"))


def _build_route_snapshot(cur, route_id: uuid.UUID) -> Dict[str, Any]:
    """Collect a JSON-serialisable snapshot of all key route data."""
    route_id_s = str(route_id)
    snapshot: Dict[str, Any] = {}

    # route_jobs (identity)
    cur.execute(
        "SELECT * FROM route_raw.route_jobs WHERE route_id = %s",
        (route_id_s,),
    )
    row = cur.fetchone()
    if row:
        snapshot["route_jobs"] = _serialise_row(row)

    service_route_id = (
        snapshot.get("route_jobs", {}).get("service_route_id")
        if isinstance(snapshot.get("route_jobs"), dict)
        else None
    )

    # relation_candidates
    if _table_exists(cur, "route_raw.relation_candidates"):
        cur.execute(
            "SELECT * FROM route_raw.relation_candidates WHERE route_id = %s",
            (route_id_s,),
        )
        snapshot["relation_candidates"] = [_serialise_row(r) for r in cur.fetchall()]

    if _table_exists(cur, "route_work.relation_stop_prior"):
        cur.execute(
            "SELECT * FROM route_work.relation_stop_prior WHERE route_id = %s ORDER BY seq",
            (route_id_s,),
        )
        snapshot["relation_stop_prior"] = [_serialise_row(r) for r in cur.fetchall()]

    if _table_exists(cur, "route_raw.unmatched_stop_points"):
        cur.execute(
            "SELECT * FROM route_raw.unmatched_stop_points WHERE route_id = %s ORDER BY seq",
            (route_id_s,),
        )
        snapshot["unmatched_stop_points"] = [_serialise_row(r) for r in cur.fetchall()]

    # osm_relations_raw
    if _table_exists(cur, "route_raw.osm_relations_raw"):
        cur.execute(
            "SELECT route_id, osm_relation_id, fetched_at, overpass_json, overpass_query, "
            "overpass_url, http_status, response_ms "
            "FROM route_raw.osm_relations_raw WHERE route_id = %s",
            (route_id_s,),
        )
        row = cur.fetchone()
        if row:
            snapshot["osm_relations_raw"] = _serialise_row(row)

    # sequence approvals
    if _table_exists(cur, "route_work.sequence_approvals"):
        cur.execute(
            "SELECT * FROM route_work.sequence_approvals WHERE route_id = %s",
            (route_id_s,),
        )
        row = cur.fetchone()
        if row:
            snapshot["sequence_approval"] = _serialise_row(row)

    # route approvals (legacy)
    if _table_exists(cur, "route_work.route_approvals"):
        cur.execute(
            "SELECT * FROM route_work.route_approvals WHERE route_id = %s",
            (route_id_s,),
        )
        row = cur.fetchone()
        if row:
            snapshot["route_approval"] = _serialise_row(row)

    if _table_exists(cur, "route_work.route_context_features"):
        cur.execute(
            "SELECT * FROM route_work.route_context_features WHERE route_id = %s",
            (route_id_s,),
        )
        row = cur.fetchone()
        if row:
            snapshot["route_context_features"] = _serialise_row(row)

    if _table_exists(cur, "route_work.valhalla_run_logs"):
        cur.execute(
            "SELECT * FROM route_work.valhalla_run_logs WHERE route_id = %s ORDER BY created_at DESC",
            (route_id_s,),
        )
        snapshot["valhalla_run_logs"] = [_serialise_row(r) for r in cur.fetchall()]

    # prod route
    if _table_exists(cur, "route_prod.routes"):
        cur.execute(
            "SELECT route_id, chosen_geometry_candidate_id, chosen_stop_sequence_candidate_id, "
            "canonical_sequence_ready, stop_node_ids, source, service_route_id, direction_id, "
            "route_name, route_aliases, landmark_tags, naming_confidence, human_verified, "
            "ST_AsGeoJSON(geom)::jsonb AS geom_geojson, "
            "created_at, updated_at "
            "FROM route_prod.routes WHERE route_id = %s",
            (route_id_s,),
        )
        row = cur.fetchone()
        if row:
            snapshot["prod_route"] = _serialise_row(row)

    # service_route_directions membership
    if _table_exists(cur, "route_raw.service_route_directions"):
        cur.execute(
            "SELECT * FROM route_raw.service_route_directions WHERE route_id = %s",
            (route_id_s,),
        )
        rows = cur.fetchall()
        if rows:
            snapshot["service_route_directions"] = [_serialise_row(r) for r in rows]
        if not service_route_id and rows:
            service_route_id = str(rows[0].get("service_route_id") or "").strip() or None

    if service_route_id and _table_exists(cur, "route_raw.service_routes"):
        cur.execute(
            "SELECT * FROM route_raw.service_routes WHERE service_route_id = %s",
            (service_route_id,),
        )
        row = cur.fetchone()
        if row:
            snapshot["service_route"] = _serialise_row(row)

    if service_route_id and _table_exists(cur, "route_work.service_route_approvals"):
        cur.execute(
            "SELECT * FROM route_work.service_route_approvals WHERE service_route_id = %s",
            (service_route_id,),
        )
        row = cur.fetchone()
        if row:
            snapshot["service_route_approval"] = _serialise_row(row)

    # dedupe memberships
    if _table_exists(cur, "route_review.route_job_dedupe_memberships"):
        cur.execute(
            "SELECT * FROM route_review.route_job_dedupe_memberships WHERE route_id = %s",
            (route_id_s,),
        )
        rows = cur.fetchall()
        if rows:
            snapshot["dedupe_memberships"] = [_serialise_row(r) for r in rows]

    if _table_exists(cur, "route_work.stop_sequence_candidate_sets"):
        cur.execute(
            "SELECT * FROM route_work.stop_sequence_candidate_sets WHERE route_id = %s",
            (route_id_s,),
        )
        stop_sets = cur.fetchall()
        if stop_sets:
            snapshot["stop_sequence_sets"] = [_serialise_row(r) for r in stop_sets]
            set_ids = [str(r.get("set_id")) for r in stop_sets if r.get("set_id")]
            if set_ids and _table_exists(cur, "route_work.stop_sequence_candidates"):
                cur.execute(
                    """
                    SELECT *
                    FROM route_work.stop_sequence_candidates
                    WHERE set_id::text = ANY(%s)
                    ORDER BY set_id, rank, created_at DESC
                    """,
                    (set_ids,),
                )
                snapshot["stop_sequence_candidates"] = [_serialise_row(r) for r in cur.fetchall()]

    if _table_exists(cur, "route_work.geometry_candidate_sets"):
        cur.execute(
            "SELECT * FROM route_work.geometry_candidate_sets WHERE route_id = %s",
            (route_id_s,),
        )
        geom_sets = cur.fetchall()
        if geom_sets:
            snapshot["geometry_sets"] = [_serialise_row(r) for r in geom_sets]
            geom_set_ids = [str(r.get("set_id")) for r in geom_sets if r.get("set_id")]
            if geom_set_ids and _table_exists(cur, "route_work.geometry_candidates"):
                cur.execute(
                    """
                    SELECT geometry_candidate_id, set_id, stop_sequence_candidate_id, engine,
                           preset_id, params, ST_AsGeoJSON(geom)::jsonb AS geom_geojson,
                           length_m, avg_stop_dist_m, max_stop_dist_m, score, metrics, created_at
                    FROM route_work.geometry_candidates
                    WHERE set_id::text = ANY(%s)
                    ORDER BY set_id, score DESC, created_at DESC
                    """,
                    (geom_set_ids,),
                )
                snapshot["geometry_candidates"] = [_serialise_row(r) for r in cur.fetchall()]

    if _table_exists(cur, "node_work.node_review_requests"):
        cur.execute(
            """
            SELECT *
            FROM node_work.node_review_requests
            WHERE route_id::text = %s
              AND source = 'phase3_route'
            ORDER BY created_at DESC
            """,
            (route_id_s,),
        )
        rows = cur.fetchall()
        if rows:
            snapshot["node_review_requests"] = [_serialise_row(r) for r in rows]

    return snapshot


def _serialise_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Make a RealDictRow JSON-friendly."""
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, (datetime, )):
            out[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            out[k] = str(v)
        elif isinstance(v, memoryview):
            out[k] = bytes(v).hex()
        else:
            out[k] = v
    return out


def _extract_route_identifiers(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Pull key identifiers from snapshot for quick-access columns."""
    rj = snapshot.get("route_jobs", {})
    prod = snapshot.get("prod_route", {})
    service_route = snapshot.get("service_route", {})
    return {
        "chosen_osm_relation_id": rj.get("chosen_osm_relation_id"),
        "route_ref": rj.get("known_ref"),
        "route_name": prod.get("route_name") or rj.get("notes"),
        "operator_name": service_route.get("operator_name"),
        "original_status": rj.get("status"),
    }


def _load_snapshot(item: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = item.get("full_snapshot_jsonb") or {}
    if isinstance(snapshot, str):
        try:
            return dict(json.loads(snapshot) or {})
        except Exception:
            return {}
    if isinstance(snapshot, dict):
        return dict(snapshot)
    return {}


def _log_delete_event(
    cur,
    *,
    route_id: str,
    original_table: str,
    original_primary_key: str,
    action_type: str,
    workflow_source: str,
    reason: Optional[str],
    actor: Optional[str],
    trash_id: Optional[str] = None,
    replacement_route_id: Optional[str] = None,
    canonical_route_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    cur.execute(
        """
        INSERT INTO route_trash.delete_events (
            route_id, original_table, original_primary_key,
            action_type, workflow_source, reason, actor,
            trash_id, replacement_route_id, canonical_route_id,
            metadata_jsonb
        ) VALUES (
            %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s::jsonb
        )
        """,
        (
            route_id,
            original_table,
            original_primary_key,
            action_type,
            workflow_source,
            reason,
            actor,
            trash_id,
            replacement_route_id,
            canonical_route_id,
            json.dumps(metadata or {}, default=str),
        ),
    )


def deactivate_trashed_route(
    conn,
    route_id: uuid.UUID,
    *,
    actor: str = "system",
    workflow: str = "manual_delete",
    reason: str = "route moved to trash",
    trash_id: Optional[uuid.UUID] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Remove a trashed route from active surfaces without destroying its raw history.

    This clears active approvals/prod state, invalidates sequence approval, deletes
    Phase 3 node review requests, and unbinds the route from any direction slot.
    """
    route_id_s = str(route_id)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        out: Dict[str, Any] = {
            "route_id": route_id_s,
            "deleted_route_prod_rows": 0,
            "deleted_route_approvals": 0,
            "invalidated_sequence_approvals": 0,
            "deleted_node_review_requests": 0,
            "reset_direction_rows": 0,
            "deleted_service_route_approvals": 0,
            "affected_service_route_ids": [],
        }

        affected_dirs: List[Dict[str, Any]] = []
        if _table_exists(cur, "route_raw.service_route_directions"):
            cur.execute(
                """
                SELECT service_route_id::text AS service_route_id, direction_id::int AS direction_id
                FROM route_raw.service_route_directions
                WHERE route_id = %s
                ORDER BY direction_id
                """,
                (route_id_s,),
            )
            affected_dirs = [dict(r or {}) for r in (cur.fetchall() or []) if r]

        service_route_ids = [
            str(row.get("service_route_id") or "").strip()
            for row in affected_dirs
            if str(row.get("service_route_id") or "").strip()
        ]
        service_route_ids = list(dict.fromkeys(service_route_ids))
        out["affected_service_route_ids"] = service_route_ids

        if _table_exists(cur, "node_work.node_review_requests"):
            cur.execute(
                """
                DELETE FROM node_work.node_review_requests
                WHERE route_id::text = %s
                  AND source = 'phase3_route'
                """,
                (route_id_s,),
            )
            out["deleted_node_review_requests"] = int(cur.rowcount or 0)

        if _table_exists(cur, "route_prod.routes"):
            _res = delete_route_prod(
                conn=conn,
                route_id=route_id_s,
                source_type="trash_repo.deactivate_trashed_route",
                pipeline_version=f"trash_repo/workflow={workflow}",
                reason=str(reason or "route moved to trash"),
            )
            out["deleted_route_prod_rows"] = int(
                _res.rows_affected.get("route_prod.routes", 0)
            )

        if _table_exists(cur, "route_work.route_approvals"):
            cur.execute(
                "DELETE FROM route_work.route_approvals WHERE route_id = %s",
                (route_id_s,),
            )
            out["deleted_route_approvals"] = int(cur.rowcount or 0)

        if _table_exists(cur, "route_work.sequence_approvals"):
            cur.execute(
                """
                UPDATE route_work.sequence_approvals
                SET approval_status = 'invalidated',
                    invalidated_at = now(),
                    invalidated_reason = %s,
                    updated_at = now()
                WHERE route_id = %s
                  AND approval_status <> 'invalidated'
                """,
                (f"route_trashed:{workflow}", route_id_s),
            )
            out["invalidated_sequence_approvals"] = int(cur.rowcount or 0)

        if affected_dirs and _table_exists(cur, "route_raw.service_route_directions"):
            for row in affected_dirs:
                cur.execute(
                    """
                    UPDATE route_raw.service_route_directions
                    SET route_id = NULL,
                        phase3_progress_step = 0,
                        direction_approval_status = 'pending',
                        geom_source = 'unknown',
                        progress_notes = %s,
                        approved_at = NULL,
                        approved_by = NULL,
                        updated_at = now()
                    WHERE service_route_id = %s::uuid
                      AND direction_id = %s
                    """,
                    (
                        f"route trashed via {workflow}",
                        str(row.get("service_route_id")),
                        int(row.get("direction_id") or 0),
                    ),
                )
                out["reset_direction_rows"] += int(cur.rowcount or 0)

        if service_route_ids and _table_exists(cur, "route_work.service_route_approvals"):
            cur.execute(
                """
                DELETE FROM route_work.service_route_approvals
                WHERE service_route_id::text = ANY(%s)
                """,
                (service_route_ids,),
            )
            out["deleted_service_route_approvals"] = int(cur.rowcount or 0)

        if service_route_ids and _table_exists(cur, "route_raw.service_routes"):
            for sid in service_route_ids:
                cur.execute(
                    """
                    UPDATE route_raw.service_routes
                    SET route_approval_status = 'pending',
                        updated_at = now()
                    WHERE service_route_id = %s::uuid
                    """,
                    (sid,),
                )

        _log_delete_event(
            cur,
            route_id=route_id_s,
            original_table="route_raw.route_jobs",
            original_primary_key=route_id_s,
            action_type="deactivate",
            workflow_source=workflow,
            reason=reason,
            actor=actor,
            trash_id=(str(trash_id) if trash_id else None),
            metadata={
                **(metadata or {}),
                **out,
            },
        )

        return out
    finally:
        cur.close()


def _restore_service_route_binding(
    cur,
    *,
    route_id: str,
    snapshot: Dict[str, Any],
    notes: Optional[str],
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "service_route_id": None,
        "direction_id": None,
        "service_route_recreated": False,
        "direction_rebound": False,
        "binding_conflict_route_id": None,
    }
    if not _table_exists(cur, "route_raw.service_routes"):
        return result

    route_job = snapshot.get("route_jobs") or {}
    service_route = snapshot.get("service_route") or {}
    bindings = list(snapshot.get("service_route_directions") or [])

    service_route_id = (
        str(service_route.get("service_route_id") or "").strip()
        or str(route_job.get("service_route_id") or "").strip()
        or None
    )
    direction_id_raw = route_job.get("direction_id")
    if bindings:
        matching_binding = next(
            (
                row
                for row in bindings
                if str(row.get("route_id") or "").strip() == route_id
                and row.get("direction_id") is not None
            ),
            bindings[0],
        )
        if matching_binding and matching_binding.get("direction_id") is not None:
            direction_id_raw = matching_binding.get("direction_id")
        if not service_route_id:
            service_route_id = str(matching_binding.get("service_route_id") or "").strip() or None

    if not service_route_id:
        return result

    direction_id = 0 if int(direction_id_raw or 0) <= 0 else 1
    result["service_route_id"] = service_route_id
    result["direction_id"] = direction_id

    cur.execute(
        """
        SELECT 1
        FROM route_raw.service_routes
        WHERE service_route_id = %s::uuid
        LIMIT 1
        """,
        (service_route_id,),
    )
    if not cur.fetchone():
        cur.execute(
            """
            INSERT INTO route_raw.service_routes (
                service_route_id,
                route_ref,
                route_name,
                operator_name,
                created_by,
                notes,
                route_approval_status
            ) VALUES (
                %s::uuid, %s, %s, %s, %s, %s, 'pending'
            )
            ON CONFLICT (service_route_id) DO NOTHING
            """,
            (
                service_route_id,
                service_route.get("route_ref") or route_job.get("known_ref"),
                service_route.get("route_name"),
                service_route.get("operator_name"),
                service_route.get("created_by") or route_job.get("created_by"),
                service_route.get("notes") or notes,
            ),
        )
        result["service_route_recreated"] = True

    if _table_exists(cur, "route_raw.service_route_directions"):
        cur.execute(
            """
            INSERT INTO route_raw.service_route_directions
              (service_route_id, direction_id, direction_approval_status, geom_source)
            VALUES
              (%s::uuid, 0, 'pending', 'unknown'),
              (%s::uuid, 1, 'pending', 'unknown')
            ON CONFLICT (service_route_id, direction_id) DO NOTHING
            """,
            (service_route_id, service_route_id),
        )
        cur.execute(
            """
            SELECT route_id::text AS route_id
            FROM route_raw.service_route_directions
            WHERE service_route_id = %s::uuid
              AND direction_id = %s
            LIMIT 1
            """,
            (service_route_id, direction_id),
        )
        existing = cur.fetchone() or {}
        existing_route_id = str(existing.get("route_id") or "").strip() or None
        if existing_route_id in (None, route_id):
            cur.execute(
                """
                UPDATE route_raw.service_route_directions
                SET route_id = %s::uuid,
                    phase3_progress_step = 0,
                    direction_approval_status = 'pending',
                    geom_source = 'unknown',
                    progress_notes = %s,
                    approved_at = NULL,
                    approved_by = NULL,
                    updated_at = now()
                WHERE service_route_id = %s::uuid
                  AND direction_id = %s
                """,
                (
                    route_id,
                    notes or "restored from trash",
                    service_route_id,
                    direction_id,
                ),
            )
            result["direction_rebound"] = True
        else:
            result["binding_conflict_route_id"] = existing_route_id

    cur.execute(
        """
        UPDATE route_raw.route_jobs
        SET service_route_id = %s::uuid,
            direction_id = %s
        WHERE route_id = %s::uuid
        """,
        (service_route_id, direction_id, route_id),
    )

    if _table_exists(cur, "route_work.service_route_approvals"):
        cur.execute(
            "DELETE FROM route_work.service_route_approvals WHERE service_route_id = %s::uuid",
            (service_route_id,),
        )
    cur.execute(
        """
        UPDATE route_raw.service_routes
        SET route_approval_status = 'pending',
            updated_at = now()
        WHERE service_route_id = %s::uuid
        """,
        (service_route_id,),
    )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def trash_route(
    conn,
    route_id: uuid.UUID,
    *,
    reason: str,
    workflow: str,
    actor: str = "system",
    replaced_by_route_id: Optional[uuid.UUID] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> uuid.UUID:
    """
    Move a route into the trash.

    1. Snapshots all key route data into JSONB.
    2. Inserts a trash_items record.
    3. Logs a delete_event.
    4. Marks route_jobs.is_trashed = TRUE.

    Returns the trash_id.
    """
    route_id_s = str(route_id)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Verify route exists and is not already trashed
        cur.execute(
            "SELECT route_id, is_trashed FROM route_raw.route_jobs WHERE route_id = %s",
            (route_id_s,),
        )
        job = cur.fetchone()
        if not job:
            raise ValueError(f"route_id {route_id_s} not found in route_raw.route_jobs")
        if job["is_trashed"]:
            # Already trashed -- return existing trash_id
            cur.execute(
                "SELECT trash_id FROM route_trash.trash_items "
                "WHERE route_id = %s AND restore_status = 'trashed' "
                "ORDER BY deleted_at DESC LIMIT 1",
                (route_id_s,),
            )
            existing = cur.fetchone()
            if existing:
                return uuid.UUID(str(existing["trash_id"]))
            # Flagged but no record -- fall through and create one

        # 1. Build snapshot
        snapshot = _build_route_snapshot(cur, route_id)

        # 2. Extract identifiers
        ids = _extract_route_identifiers(snapshot)
        trash_id = uuid.uuid4()

        # 3. Insert trash item
        cur.execute(
            """
            INSERT INTO route_trash.trash_items (
                trash_id, original_table, original_primary_key, route_id,
                chosen_osm_relation_id, route_ref, route_name, operator_name,
                original_status,
                deletion_reason, deletion_source_workflow, deleted_by,
                replaced_by_route_id,
                full_snapshot_jsonb, metadata_jsonb
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s,
                %s, %s, %s,
                %s,
                %s::jsonb, %s::jsonb
            )
            """,
            (
                str(trash_id),
                "route_raw.route_jobs",
                route_id_s,
                route_id_s,
                ids["chosen_osm_relation_id"],
                ids["route_ref"],
                ids["route_name"],
                ids["operator_name"],
                ids["original_status"],
                reason,
                workflow,
                actor,
                _uuid_str(replaced_by_route_id),
                json.dumps(snapshot, default=str),
                json.dumps(metadata or {}, default=str),
            ),
        )

        # 4. Log delete event
        _log_delete_event(
            cur,
            route_id=route_id_s,
            original_table="route_raw.route_jobs",
            original_primary_key=route_id_s,
            action_type="delete",
            workflow_source=workflow,
            reason=reason,
            actor=actor,
            trash_id=str(trash_id),
            replacement_route_id=_uuid_str(replaced_by_route_id),
            metadata=metadata,
        )

        # 5. Mark route as trashed (soft-delete)
        cur.execute(
            """
            UPDATE route_raw.route_jobs
            SET is_trashed = TRUE, trashed_at = now(), trash_id = %s
            WHERE route_id = %s
            """,
            (str(trash_id), route_id_s),
        )

        return trash_id

    finally:
        cur.close()


def restore_route(
    conn,
    trash_id: uuid.UUID,
    *,
    actor: str = "system",
    restore_status: str = "new",
    notes: Optional[str] = None,
) -> uuid.UUID:
    """
    Restore a trashed route back into active state.

    1. Verifies the trash item exists and is in 'trashed' state.
    2. Clears is_trashed flag on route_jobs.
    3. Optionally resets route_jobs.status to `restore_status`.
    4. Updates trash_items.restore_status = 'restored'.
    5. Logs a restore delete_event.

    Returns the route_id.
    """
    trash_id_s = str(trash_id)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Fetch trash item
        cur.execute(
            "SELECT * FROM route_trash.trash_items WHERE trash_id = %s",
            (trash_id_s,),
        )
        item = cur.fetchone()
        if not item:
            raise ValueError(f"trash_id {trash_id_s} not found")
        if item["restore_status"] == "restored":
            # Already restored -- idempotent
            return uuid.UUID(str(item["route_id"]))
        if item["restore_status"] == "purged":
            raise ValueError(f"trash_id {trash_id_s} has been purged and cannot be restored")

        route_id = uuid.UUID(str(item["route_id"]))
        route_id_s = str(route_id)
        snapshot = _load_snapshot(item)

        # Verify route still exists (not hard-deleted)
        cur.execute(
            "SELECT route_id FROM route_raw.route_jobs WHERE route_id = %s",
            (route_id_s,),
        )
        if not cur.fetchone():
            raise ValueError(
                f"route_id {route_id_s} no longer exists in route_raw.route_jobs. "
                "Manual reconstruction from snapshot may be needed."
            )

        # 1. Restore route_jobs
        cur.execute(
            """
            UPDATE route_raw.route_jobs
            SET is_trashed = FALSE,
                trashed_at = NULL,
                trash_id   = NULL,
                status     = %s
            WHERE route_id = %s
            """,
            (restore_status, route_id_s),
        )

        restore_binding = _restore_service_route_binding(
            cur,
            route_id=route_id_s,
            snapshot=snapshot,
            notes=notes,
        )

        # 2. Mark trash item as restored
        cur.execute(
            """
            UPDATE route_trash.trash_items
            SET restore_status = 'restored',
                restored_at    = now(),
                restored_by    = %s
            WHERE trash_id = %s
            """,
            (actor, trash_id_s),
        )

        # 3. Log restore event
        cur.execute(
            """
            INSERT INTO route_trash.delete_events (
                route_id, original_table, original_primary_key,
                action_type, workflow_source, reason, actor,
                trash_id,
                metadata_jsonb
            ) VALUES (
                %s, %s, %s,
                %s, %s, %s, %s,
                %s,
                %s::jsonb
            )
            """,
            (
                route_id_s,
                "route_raw.route_jobs",
                route_id_s,
                "restore",
                "manual_restore",
                notes or "Restored from trash",
                actor,
                trash_id_s,
                json.dumps(
                    {
                        "restore_status": restore_status,
                        **restore_binding,
                    },
                    default=str,
                ),
            ),
        )

        return route_id

    finally:
        cur.close()


def trash_route_for_merge(
    conn,
    route_id: uuid.UUID,
    canonical_route_id: uuid.UUID,
    *,
    reason: str = "merged into canonical route",
    workflow: str = "dedupe_merge",
    actor: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
) -> uuid.UUID:
    """
    Convenience: trash a route that is being merged/replaced by a canonical one.
    Records the replacement link and logs a 'merge' action type.
    """
    trash_id = trash_route(
        conn,
        route_id,
        reason=reason,
        workflow=workflow,
        actor=actor,
        replaced_by_route_id=canonical_route_id,
        metadata=metadata,
    )

    # Also log a merge-specific event
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        _log_delete_event(
            cur,
            route_id=str(route_id),
            original_table="route_raw.route_jobs",
            original_primary_key=str(route_id),
            action_type="merge",
            workflow_source=workflow,
            reason=reason,
            actor=actor,
            trash_id=str(trash_id),
            replacement_route_id=str(canonical_route_id),
            canonical_route_id=str(canonical_route_id),
            metadata=metadata,
        )
    finally:
        cur.close()

    return trash_id


def get_trash_item(conn, trash_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Fetch a single trash item by trash_id."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "SELECT * FROM route_trash.trash_items WHERE trash_id = %s",
            (str(trash_id),),
        )
        return cur.fetchone()
    finally:
        cur.close()


def list_trash(
    conn,
    *,
    route_id: Optional[uuid.UUID] = None,
    workflow: Optional[str] = None,
    restore_status: str = "trashed",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List trash items with optional filters."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        clauses = ["restore_status = %s"]
        params: list = [restore_status]

        if route_id:
            clauses.append("route_id = %s")
            params.append(str(route_id))
        if workflow:
            clauses.append("deletion_source_workflow = %s")
            params.append(workflow)

        params.append(limit)
        sql = (
            "SELECT trash_id, route_id, chosen_osm_relation_id, route_ref, "
            "route_name, original_status, deletion_reason, deletion_source_workflow, "
            "deleted_by, deleted_at, replaced_by_route_id, restore_status "
            f"FROM route_trash.trash_items WHERE {' AND '.join(clauses)} "
            "ORDER BY deleted_at DESC LIMIT %s"
        )
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        cur.close()


def list_delete_events(
    conn,
    *,
    route_id: Optional[uuid.UUID] = None,
    action_type: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List delete/audit events with optional filters."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        clauses = []
        params: list = []

        if route_id:
            clauses.append("route_id = %s")
            params.append(str(route_id))
        if action_type:
            clauses.append("action_type = %s")
            params.append(action_type)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        cur.execute(
            f"SELECT * FROM route_trash.delete_events {where} "
            "ORDER BY event_at DESC LIMIT %s",
            params,
        )
        return cur.fetchall()
    finally:
        cur.close()


def is_route_trashed(conn, route_id: uuid.UUID) -> bool:
    """Check if a route is currently in trash."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "SELECT is_trashed FROM route_raw.route_jobs WHERE route_id = %s",
            (str(route_id),),
        )
        row = cur.fetchone()
        return bool(row and row["is_trashed"])
    finally:
        cur.close()
