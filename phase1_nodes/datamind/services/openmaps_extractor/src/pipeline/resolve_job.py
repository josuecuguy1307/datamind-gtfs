from __future__ import annotations

import logging

from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import (
    db_conn,
    exec_sql,
    fetchone,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.settings import (
    T_NODE_CANDIDATES,
    T_NODE_CLUSTERS,
    T_NODE_FEATURES,
    T_NODES_RESOLVED,
)

logger = logging.getLogger(__name__)


def run_resolve(node_set_id: str) -> dict:
    """
    Phase 1 – RESOLVE (Nodes)

    Resolve each cluster inside ONE node_set into a single representative candidate:
      - deletes previous resolutions for this node_set (idempotent)
      - selects best candidate per cluster using prob_stop / confidence_v0
      - sets representative geometry as centroid of cluster points
      - writes into node_work.nodes_resolved

    IMPORTANT:
      - We resolve by node_set_id (not by source_run_id).
    """

    with db_conn() as conn:
        # ------------------------------------------------------------
        # Guard: make sure this node_set has clusters
        # ------------------------------------------------------------
        row = fetchone(
            conn,
            f"""
            SELECT COUNT(*) AS n
            FROM {T_NODE_CLUSTERS}
            WHERE node_set_id = %s
            """,
            (node_set_id,),
        )
        n_clusters_assignments = int(row["n"])

        if n_clusters_assignments == 0:
            logger.info("resolve.run.empty node_set_id=%s", node_set_id)
            return {"node_set_id": node_set_id, "resolved_total": 0}

        logger.info(
            "resolve.run.start node_set_id=%s cluster_assignments=%s",
            node_set_id,
            n_clusters_assignments,
        )

        # ------------------------------------------------------------
        # Delete previous resolutions for this node_set (idempotent)
        # ------------------------------------------------------------
        exec_sql(
            conn,
            f"DELETE FROM {T_NODES_RESOLVED} WHERE node_set_id = %s",
            (node_set_id,),
        )

        logger.info("resolve.run.cleaned node_set_id=%s", node_set_id)

        # ------------------------------------------------------------
        # Resolve clusters → representative candidate
        # ------------------------------------------------------------
        exec_sql(
            conn,
            f"""
            WITH cluster_geom AS (
              SELECT
                sc.cluster_id,
                ST_Centroid(ST_Collect(c.geom)) AS geom
              FROM {T_NODE_CLUSTERS} sc
              JOIN {T_NODE_CANDIDATES} c
                ON c.node_candidate_id = sc.node_candidate_id
              WHERE sc.node_set_id = %s
              GROUP BY sc.cluster_id
            ),
            ranked AS (
              SELECT
                sc.cluster_id,
                c.node_candidate_id,
                c.tags,
                -- Confidence: prefer prob_stop, fallback to confidence_v0, else 0
                COALESCE(f.prob_stop, f.confidence_v0, 0.0) AS conf,
                ROW_NUMBER() OVER (
                  PARTITION BY sc.cluster_id
                  ORDER BY COALESCE(f.prob_stop, f.confidence_v0, 0.0) DESC
                ) AS rn
              FROM {T_NODE_CLUSTERS} sc
              JOIN {T_NODE_CANDIDATES} c
                ON c.node_candidate_id = sc.node_candidate_id
              LEFT JOIN {T_NODE_FEATURES} f
                ON f.node_candidate_id = c.node_candidate_id
              WHERE sc.node_set_id = %s
            )
            INSERT INTO {T_NODES_RESOLVED}
              (node_set_id, cluster_id, geom, chosen_candidate_id, chosen_tags, confidence, status)
            SELECT
              %s AS node_set_id,
              g.cluster_id,
              CASE
                WHEN ST_SRID(g.geom) = 0 THEN ST_SetSRID(g.geom, 4326)
                ELSE g.geom
              END AS geom,
              r.node_candidate_id AS chosen_candidate_id,
              r.tags AS chosen_tags,
              r.conf AS confidence,
              'work' AS status
            FROM cluster_geom g
            JOIN ranked r
              ON r.cluster_id = g.cluster_id AND r.rn = 1
            """,
            (node_set_id, node_set_id, node_set_id),
        )

        logger.info("resolve.run.executed node_set_id=%s", node_set_id)

        # ------------------------------------------------------------
        # Final count for this node_set
        # ------------------------------------------------------------
        row2 = fetchone(
            conn,
            f"""
            SELECT COUNT(*) AS n
            FROM {T_NODES_RESOLVED}
            WHERE node_set_id = %s
            """,
            (node_set_id,),
        )
        total = int(row2["n"])

        logger.info("resolve.run.result node_set_id=%s resolved_total=%s", node_set_id, total)

    return {
        "node_set_id": node_set_id,
        "resolved_total": total,
    }
