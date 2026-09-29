from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from datamind_console.persistence import patch_route_prod_fields
from phase4_semantics.common.db import get_conn


def finalize_route_prod(route_id: str) -> Dict[str, Any]:
    """Copy the finalized naming payload from route_semantics → route_prod.routes.

    Was a single cross-table UPDATE ... FROM route_semantics; after the writer
    lockdown that direct UPDATE is no longer permitted. We now read the
    semantics row, then route the column-level writes through
    ``patch_route_prod_fields`` (all six target columns are in the patchable
    allow-list).
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT route_name,
                   route_aliases,
                   landmark_tags,
                   direction_semantics,
                   naming_confidence,
                   human_verified
            FROM route_prod.route_semantics
            WHERE route_id = %s
            LIMIT 1
            """,
            (route_id,),
        )
        sem = cur.fetchone()
        if sem is None:
            raise RuntimeError(
                f"route_prod.route_semantics missing for route_id={route_id}"
            )
        (sem_name, sem_aliases, sem_landmarks,
         sem_ds, sem_conf, sem_human) = sem

        fields: Dict[str, Any] = {
            "route_name": sem_name,
            "route_aliases": sem_aliases if sem_aliases is not None else [],
            "landmark_tags": sem_landmarks if sem_landmarks is not None else [],
            "direction_semantics": sem_ds if sem_ds is not None else {},
            "naming_confidence": sem_conf,
            "human_verified": bool(sem_human) if sem_human is not None else True,
            "semantics_updated_at": datetime.now(timezone.utc),
        }

        res = patch_route_prod_fields(
            conn=conn,
            route_id=route_id,
            fields=fields,
            source_type="phase4_semantics.finalize_route_prod",
            pipeline_version="phase4_semantics/review.finalize",
        )
        if not res.success:
            raise RuntimeError(f"route_prod patch failed: {res.error}")

        cur.execute(
            """
            SELECT route_id, route_name, naming_confidence,
                   human_verified, semantics_updated_at
            FROM route_prod.routes
            WHERE route_id = %s
            """,
            (route_id,),
        )
        row = cur.fetchone()
        cur.close()

    if row is None:
        raise RuntimeError(f"route_prod.routes not found after finalize for {route_id}")

    cols = ["route_id", "route_name", "naming_confidence",
            "human_verified", "semantics_updated_at"]
    return {
        "route_id": route_id,
        "finalized": True,
        "route_prod": dict(zip(cols, row)),
    }
