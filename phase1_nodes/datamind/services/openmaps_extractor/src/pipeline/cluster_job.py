from __future__ import annotations

from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import (
    db_conn,
    exec_sql,
    fetchone,
)
from phase1_nodes.datamind.services.openmaps_extractor.src.settings import (
    T_NODE_CLUSTERS,
    T_NODE_CANDIDATES,
)


def run_cluster(node_set_id: str, eps_m: float = 35.0, min_pts: int = 3) -> dict:
    """
    Phase 1 – CLUSTER (Nodes)

    DBSCAN clustering over candidates in ONE node_set.
    Writes node_work.node_clusters with:
      (node_set_id, node_candidate_id, cluster_id, eps_m, min_pts)

    Notes:
      - We cluster in EPS meters by projecting to EPSG:3857.
      - PostGIS ST_ClusterDBSCAN returns integer cluster labels per window.
      - Noise (cid IS NULL) gets unique cluster_id per point (singleton).
    """
    with db_conn() as conn:
        # Guard: ensure there are candidates in this node_set
        row0 = fetchone(
            conn,
            f"""
            SELECT COUNT(*) AS n
            FROM {T_NODE_CANDIDATES}
            WHERE node_set_id = %s
            """,
            (node_set_id,),
        )
        n_candidates = int(row0["n"])

        if n_candidates == 0:
            return {
                "node_set_id": node_set_id,
                "cluster_assignments": 0,
                "eps_m": eps_m,
                "min_pts": min_pts,
                "n_candidates": 0,
            }

        # Clear previous clusters for this node_set (idempotent)
        exec_sql(
            conn,
            f"DELETE FROM {T_NODE_CLUSTERS} WHERE node_set_id = %s",
            (node_set_id,),
        )

        # Cluster
        exec_sql(
            conn,
            f"""
            WITH base AS (
              SELECT
                c.node_candidate_id,
                ST_ClusterDBSCAN(
                  ST_Transform(c.geom, 3857),
                  eps := %s,
                  minpoints := %s
                ) OVER () AS cid
              FROM {T_NODE_CANDIDATES} c
              WHERE c.node_set_id = %s
            ),
            label_map AS (
              -- Map each integer cid to a stable UUID cluster_id
              SELECT cid, gen_random_uuid() AS cluster_id
              FROM (SELECT DISTINCT cid FROM base WHERE cid IS NOT NULL) d
            ),
            clustered AS (
              SELECT
                b.node_candidate_id,
                COALESCE(m.cluster_id, gen_random_uuid()) AS cluster_id
              FROM base b
              LEFT JOIN label_map m ON m.cid = b.cid
            )
            INSERT INTO {T_NODE_CLUSTERS}
              (node_set_id, node_candidate_id, cluster_id, eps_m, min_pts)
            SELECT
              %s AS node_set_id,
              node_candidate_id,
              cluster_id,
              %s AS eps_m,
              %s AS min_pts
            FROM clustered
            """,
            (eps_m, min_pts, node_set_id, node_set_id, eps_m, min_pts),
        )

        # Count assignments
        row = fetchone(
            conn,
            f"SELECT COUNT(*) AS n FROM {T_NODE_CLUSTERS} WHERE node_set_id = %s",
            (node_set_id,),
        )
        n = int(row["n"])

    return {
        "node_set_id": node_set_id,
        "cluster_assignments": n,
        "eps_m": eps_m,
        "min_pts": min_pts,
        "n_candidates": n_candidates,
    }
