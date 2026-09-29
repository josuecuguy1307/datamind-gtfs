from __future__ import annotations

import logging

from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import (
    db_conn,
    exec_sql,
    fetchone,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.settings import (
    T_PROD_NODES,
    T_NODES_RESOLVED,
    T_NODE_CANDIDATES,
    PROMOTE_MIN_CONFIDENCE,
)

logger = logging.getLogger(__name__)


def run_promote(node_set_id: str) -> dict:
    """
    Phase 1 – PROMOTE (Nodes)
    Publish resolved nodes into prod (node_prod.nodes).
    """

    with db_conn() as conn:
        # ------------------------------------------------------------
        # Quick guard: does this node_set have resolved nodes?
        # ------------------------------------------------------------
        row = fetchone(
            conn,
            f"""
            SELECT COUNT(*) AS n
            FROM {T_NODES_RESOLVED}
            WHERE node_set_id = %s
            """,
            (node_set_id,),
        )
        n_resolved = int(row["n"])

        if n_resolved == 0:
            logger.info("promote.run.empty node_set_id=%s", node_set_id)
            return {"node_set_id": node_set_id, "prod_total": 0, "promoted": 0}

        logger.info("promote.run.start node_set_id=%s resolved=%s", node_set_id, n_resolved)

        # ------------------------------------------------------------
        # Cleanup rejected nodes for this node_set:
        # - ensure they are not present in node_prod.nodes
        # - remove them from node_work.nodes_resolved
        # ------------------------------------------------------------
        row_rejected = fetchone(
            conn,
            f"""
            SELECT COUNT(*)::int AS n
            FROM {T_NODES_RESOLVED}
            WHERE node_set_id = %s
              AND status = 'rejected'
            """,
            (node_set_id,),
        ) or {"n": 0}
        rejected_before = int(row_rejected.get("n") or 0)

        exec_sql(
            conn,
            f"""
            DELETE FROM {T_PROD_NODES}
            WHERE node_id IN (
              SELECT r.node_id
              FROM {T_NODES_RESOLVED} r
              WHERE r.node_set_id = %s
                AND r.status = 'rejected'
            )
            """,
            (node_set_id,),
        )

        exec_sql(
            conn,
            f"""
            DELETE FROM {T_NODES_RESOLVED}
            WHERE node_set_id = %s
              AND status = 'rejected'
            """,
            (node_set_id,),
        )

        # ------------------------------------------------------------
        # Publish approved resolved nodes into prod (idempotent)
        # ------------------------------------------------------------
        exec_sql(
            conn,
            f"""
            INSERT INTO {T_PROD_NODES} (
              node_id,
              geom,
              node_type,
              name,
              ref,
              operator,
              tag_kind,
              source,
              source_node_set_id,
              chosen_candidate_id,
              chosen_tags,
              confidence,
              approved_at,
              updated_at
            )
            SELECT
              r.node_id,
              CASE
                WHEN ST_SRID(r.geom) = 0 THEN ST_SetSRID(r.geom, 4326)
                ELSE r.geom
              END AS geom,

              CASE
                WHEN c.tag_kind = 'poi' THEN 'POI'
                ELSE 'STOP'
              END::text AS node_type,

              NULLIF(r.chosen_tags->>'name', '') AS name,
              NULLIF(r.chosen_tags->>'ref', '') AS ref,
              NULLIF(r.chosen_tags->>'operator', '') AS operator,

              c.tag_kind,
              'work_review' AS source,

              r.node_set_id AS source_node_set_id,
              r.chosen_candidate_id,
              r.chosen_tags,
              r.confidence,
              now() AS approved_at,
              now() AS updated_at
            FROM {T_NODES_RESOLVED} r
            JOIN {T_NODE_CANDIDATES} c
              ON c.node_candidate_id = r.chosen_candidate_id
            WHERE r.node_set_id = %s
              AND r.status = 'approved'
              AND COALESCE(r.confidence, 0) >= %s
            ON CONFLICT (node_id) DO UPDATE SET
              geom = EXCLUDED.geom,
              node_type = EXCLUDED.node_type,
              name = EXCLUDED.name,
              ref = EXCLUDED.ref,
              operator = EXCLUDED.operator,
              tag_kind = EXCLUDED.tag_kind,
              source = EXCLUDED.source,
              source_node_set_id = EXCLUDED.source_node_set_id,
              chosen_candidate_id = EXCLUDED.chosen_candidate_id,
              chosen_tags = EXCLUDED.chosen_tags,
              confidence = EXCLUDED.confidence,
              approved_at = EXCLUDED.approved_at,
              updated_at = now()
            """,
            (node_set_id, PROMOTE_MIN_CONFIDENCE),
        )

        # Count skipped low-confidence nodes
        row_skipped = fetchone(
            conn,
            f"""
            SELECT COUNT(*)::int AS n
            FROM {T_NODES_RESOLVED}
            WHERE node_set_id = %s
              AND status = 'approved'
              AND COALESCE(confidence, 0) < %s
            """,
            (node_set_id, PROMOTE_MIN_CONFIDENCE),
        )
        skipped_low_confidence = int((row_skipped or {}).get("n") or 0)
        if skipped_low_confidence > 0:
            logger.info(
                "promote.run.skipped_low_confidence node_set_id=%s count=%s threshold=%.3f",
                node_set_id, skipped_low_confidence, PROMOTE_MIN_CONFIDENCE,
            )

        # ✅ THIS is the commit you needed
        conn.commit()

        logger.info("promote.run.executed node_set_id=%s", node_set_id)

        # ------------------------------------------------------------
        # Counts for logs / UI
        # ------------------------------------------------------------
        row2 = fetchone(
            conn,
            f"""
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE source_node_set_id = %s) AS from_this_set
            FROM {T_PROD_NODES}
            """,
            (node_set_id,),
        )

        prod_total = int(row2["total"])
        promoted = int(row2["from_this_set"])

        logger.info(
            "promote.run.result node_set_id=%s promoted=%s prod_total=%s",
            node_set_id,
            promoted,
            prod_total,
        )

    return {
        "node_set_id": node_set_id,
        "promoted": promoted,
        "rejected_deleted": rejected_before,
        "skipped_low_confidence": skipped_low_confidence,
        "prod_total": prod_total,
    }
