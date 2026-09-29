from __future__ import annotations

import uuid
from typing import Any, Optional, Tuple

from phase3_routes.services.route_constructor.src.db.conn import db_cursor
from datamind_console.persistence import write_to_route_prod

_PIPELINE_VERSION = "route_prod_repo.approve_route_geometry"


def _to_uuid_str(x: Any) -> str:
    if isinstance(x, uuid.UUID):
        return str(x)
    return str(uuid.UUID(str(x)))


def approve_route_geometry(
    conn,
    route_id: uuid.UUID,
    geometry_candidate_id: uuid.UUID,
) -> None:
    """
    Approves one geometry_candidate into route_prod.routes for the given route_id.

    Safety improvements:
      - Validates geometry_candidate belongs to route_id
      - Works with dict-row OR tuple-row cursors
      - Handles UUID objects or strings
    """
    route_id_s = _to_uuid_str(route_id)
    geom_id_s = _to_uuid_str(geometry_candidate_id)

    sql_fetch = """
    SELECT
      gcs.route_id,
      gc.geom,
      s.stop_node_ids,
      gc.stop_sequence_candidate_id,
      sa.chosen_stop_sequence_candidate_id AS approved_stop_sequence_candidate_id,
      sa.approval_status AS sequence_approval_status,
      sa.approved_at AS sequence_approved_at,
      sa.approved_by AS sequence_approved_by,
      rj.service_route_id::text AS service_route_id,
      rj.direction_id::int AS direction_id,
      rj.province AS province
    FROM route_work.geometry_candidates gc
    JOIN route_work.geometry_candidate_sets gcs
      ON gcs.set_id = gc.set_id
    JOIN route_work.stop_sequence_candidates s
      ON s.candidate_id = gc.stop_sequence_candidate_id
    LEFT JOIN route_work.sequence_approvals sa
      ON sa.route_id = gcs.route_id
    LEFT JOIN route_raw.route_jobs rj
      ON rj.route_id = gcs.route_id
    WHERE gc.geometry_candidate_id = %s
    """

    with db_cursor(conn) as cur:
        cur.execute(sql_fetch, (geom_id_s,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"geometry_candidate_id not found: {geom_id_s}")

        # Support dict-like row OR tuple row
        if isinstance(row, dict):
            gc_route_id = row.get("route_id")
            geom = row.get("geom")
            stop_node_ids = row.get("stop_node_ids")
            stop_sequence_candidate_id = row.get("stop_sequence_candidate_id")
            approved_stop_sequence_candidate_id = row.get("approved_stop_sequence_candidate_id")
            sequence_approval_status = row.get("sequence_approval_status")
            sequence_approved_at = row.get("sequence_approved_at")
            sequence_approved_by = row.get("sequence_approved_by")
            service_route_id = row.get("service_route_id")
            direction_id = row.get("direction_id")
            province = row.get("province")
        else:
            # matches SELECT order above
            gc_route_id = row[0]
            geom = row[1]
            stop_node_ids = row[2]
            stop_sequence_candidate_id = row[3]
            approved_stop_sequence_candidate_id = row[4]
            sequence_approval_status = row[5]
            sequence_approved_at = row[6]
            sequence_approved_by = row[7]
            service_route_id = row[8]
            direction_id = row[9]
            province = row[10]

        if gc_route_id is None:
            raise ValueError("geometry_candidates.route_id is NULL (unexpected)")

        if _to_uuid_str(gc_route_id) != route_id_s:
            raise ValueError(
                f"geometry_candidate_id {geom_id_s} belongs to route_id={gc_route_id}, "
                f"not the requested route_id={route_id_s}"
            )

        if geom is None:
            raise ValueError("geometry_candidate has NULL geom (unexpected)")

        approved_seq_id_s = str(approved_stop_sequence_candidate_id or "").strip()
        if not approved_seq_id_s or str(sequence_approval_status or "").strip().lower() != "approved":
            raise ValueError("Canonical stop sequence is not approved for this route")
        if str(stop_sequence_candidate_id or "").strip() != approved_seq_id_s:
            raise ValueError("geometry candidate does not match the approved canonical sequence")

        # Province propagation (Skill 11 §7): source of truth is
        # route_raw.route_jobs.province. If NULL the upstream INSERT violated
        # §7 — raise instead of silent-default to 'sample_region'.
        if province is None or not str(province).strip():
            raise ValueError(
                f"route_raw.route_jobs.province is NULL/empty for route_id={route_id_s}. "
                "Skill 11 §7 forbids falling back to the 'sample_region' DEFAULT. "
                "Fix the upstream INSERT to route_raw.route_jobs to set province explicitly."
            )
        province_norm = str(province).strip().lower()

    # Preserving legacy semantics: `source` is intentionally NOT included in
    # route_data so the DB default ('route_constructor') applies on first
    # insert and existing `source` values are left untouched on upsert.
    result = write_to_route_prod(
        route_code=route_id_s,
        route_data={
            "route_id": route_id_s,
            "province": province_norm,
            "chosen_geometry_candidate_id": geom_id_s,
            "chosen_stop_sequence_candidate_id": approved_seq_id_s,
            "canonical_sequence_ready": True,
            "sequence_approved_at": sequence_approved_at,
            "sequence_approved_by": sequence_approved_by,
            "service_route_id": service_route_id,
            "direction_id": direction_id,
        },
        stops=list(stop_node_ids or []),
        shape={"raw": geom},
        source_type="route_constructor_geometry_approval",
        pipeline_version=_PIPELINE_VERSION,
        conn=conn,
        mode="upsert",
    )
    if not result.success:
        raise RuntimeError(f"route_prod write failed for {route_id_s}: {result.error}")
