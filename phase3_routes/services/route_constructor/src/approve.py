from __future__ import annotations

import os
import uuid
from typing import Optional, Any

from src.db.conn import db_cursor
from datamind_console.persistence import write_to_route_prod

_PIPELINE_VERSION = "route_constructor.approve.approve_best_in_set"


def _parse_uuid_array_safe(v: Any) -> list[uuid.UUID]:
    """
    Robust UUID[] parser.
    Handles:
      - None
      - list[uuid.UUID | str | None]
      - Postgres uuid[] text: "{a,b,c}"
      - single uuid / string
    """
    if v is None:
        return []

    if isinstance(v, (list, tuple)):
        out: list[uuid.UUID] = []
        for x in v:
            if x is None:
                continue
            if isinstance(x, uuid.UUID):
                out.append(x)
            else:
                s = str(x).strip()
                if not s or s == "{}":
                    continue
                out.append(uuid.UUID(s))
        return out

    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        if s.startswith("{") and s.endswith("}"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            return [uuid.UUID(p.strip().strip('"')) for p in inner.split(",")]
        return [uuid.UUID(s)]

    return []


def approve_best_in_set(
    conn,
    route_id: uuid.UUID,
    geometry_set_id: uuid.UUID,
    *,
    approved_by: Optional[str] = None,
    notes: Optional[str] = None,
) -> uuid.UUID:
    """
    Picks best geometry candidate for a set and persists:
      1) route_work.route_approvals (UPSERT by route_id)
      2) route_prod.routes (UPSERT by route_id)
      3) route_raw.route_jobs.status = 'approved' (best-effort)

    Selection rule:
      - Prefer metrics->ml_rank ASC (rank 1 is best)
      - Fallback to score DESC
    """
    approved_by = approved_by or os.getenv("USER") or os.getenv("USERNAME") or "system"

    with db_cursor(conn) as cur:
        # ---- 1) Pick best candidate (ensures set belongs to route) ----
        cur.execute(
            """
            SELECT
              gc.geometry_candidate_id,
              gc.geom,
              gc.stop_sequence_candidate_id,
              gc.score,
              gc.metrics,
              gc.valhalla_request,
              ssc.stop_node_ids,
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
            LEFT JOIN route_work.stop_sequence_candidates ssc
              ON ssc.candidate_id = gc.stop_sequence_candidate_id
            LEFT JOIN route_work.sequence_approvals sa
              ON sa.route_id = gcs.route_id
            LEFT JOIN route_raw.route_jobs rj
              ON rj.route_id = gcs.route_id
            WHERE gc.set_id = %s
              AND gcs.route_id = %s
            ORDER BY
              (CASE
                 WHEN (gc.metrics ? 'ml_rank') THEN (gc.metrics->>'ml_rank')::int
                 ELSE NULL
               END) ASC NULLS LAST,
              gc.score DESC
            LIMIT 1
            """,
            (str(geometry_set_id), str(route_id)),
        )
        row = cur.fetchone()

        if not row:
            raise ValueError(
                f"No geometry candidates found for set_id={geometry_set_id} "
                f"(or set not owned by route_id={route_id})"
            )

        best_id = uuid.UUID(row["geometry_candidate_id"])
        chosen_seq_id = (
            uuid.UUID(row["stop_sequence_candidate_id"])
            if row.get("stop_sequence_candidate_id")
            else None
        )
        approved_seq_id = (
            uuid.UUID(str(row["approved_stop_sequence_candidate_id"]))
            if row.get("approved_stop_sequence_candidate_id")
            else None
        )
        approval_status = str(row.get("sequence_approval_status") or "").strip().lower()
        if approved_seq_id is None or approval_status != "approved":
            raise RuntimeError("Canonical stop sequence is not approved for this route.")
        if chosen_seq_id is None or chosen_seq_id != approved_seq_id:
            raise RuntimeError(
                "Best geometry candidate is not linked to the approved canonical stop sequence."
            )

        # ---- FIXED: robust stop_node_ids parsing ----
        stop_node_ids = _parse_uuid_array_safe(row.get("stop_node_ids"))

        # ---- Province propagation (Skill 11 §7): source of truth is
        # route_raw.route_jobs.province. If NULL the upstream INSERT violated
        # §7 — raise instead of silent-default to 'sample_region'. ----
        province_val = row.get("province")
        if province_val is None or not str(province_val).strip():
            raise RuntimeError(
                f"route_raw.route_jobs.province is NULL/empty for route_id={route_id}. "
                "Skill 11 §7 forbids falling back to the 'sample_region' DEFAULT. "
                "Fix the upstream INSERT to route_raw.route_jobs to set province explicitly."
            )
        province_val = str(province_val).strip().lower()

        # ---- 2) Upsert into approvals ----
        cur.execute(
            """
            INSERT INTO route_work.route_approvals
              (route_id, chosen_geometry_candidate_id, chosen_stop_sequence_candidate_id, approved_by, notes)
            VALUES
              (%s, %s, %s, %s, %s)
            ON CONFLICT (route_id)
            DO UPDATE SET
              chosen_geometry_candidate_id = EXCLUDED.chosen_geometry_candidate_id,
              chosen_stop_sequence_candidate_id = EXCLUDED.chosen_stop_sequence_candidate_id,
              approved_at = now(),
              approved_by = EXCLUDED.approved_by,
              notes = EXCLUDED.notes
            """,
            (
                str(route_id),
                str(best_id),
                str(chosen_seq_id) if chosen_seq_id else None,
                approved_by,
                notes,
            ),
        )

        # ---- 3) Upsert into route_prod.routes (via canonical wrapper) ----
        result = write_to_route_prod(
            route_code=str(route_id),
            route_data={
                "route_id": str(route_id),
                "province": province_val,
                "source": "route_constructor",
                "chosen_geometry_candidate_id": str(best_id),
                "chosen_stop_sequence_candidate_id": str(approved_seq_id),
                "canonical_sequence_ready": True,
                "sequence_approved_at": row.get("sequence_approved_at"),
                "sequence_approved_by": row.get("sequence_approved_by") or approved_by,
                "service_route_id": row.get("service_route_id"),
                "direction_id": row.get("direction_id"),
            },
            stops=[str(x) for x in stop_node_ids],
            shape={"raw": row["geom"]},
            source_type="route_constructor_best_in_set",
            pipeline_version=_PIPELINE_VERSION,
            conn=conn,
            mode="upsert",
            valhalla_request=row.get("valhalla_request"),
        )
        if not result.success:
            raise RuntimeError(f"route_prod write failed for {route_id}: {result.error}")

        # ---- 4) Best-effort: mark route job approved ----
        cur.execute(
            """
            UPDATE route_raw.route_jobs
            SET status = 'approved'
            WHERE route_id = %s
            """,
            (str(route_id),),
        )

    return best_id
