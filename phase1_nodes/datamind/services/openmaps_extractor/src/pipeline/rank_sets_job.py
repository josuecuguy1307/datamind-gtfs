from __future__ import annotations

import logging
import math
from typing import Any, Dict

from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import (
    db_conn,
    fetchone,
    exec_sql,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.db.phase1_repo import (
    update_node_set_rank,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.settings import (
    T_NODE_SETS,
    T_NODE_CANDIDATES,
    T_NODE_FEATURES,
    T_NODE_CLUSTERS,
    T_NODES_RESOLVED,
    T_NODE_SET_METRICS,
)


logger = logging.getLogger(__name__)


def run_rank_set(node_set_id: str) -> Dict[str, Any]:
    """
    Phase 1 – RANK (Node sets)

    Produces a numeric score for a node_set so you can compare node_sets
    (across actions/params) and later learn reward.

    IMPORTANT:
    - Uses node_set_id everywhere (NOT run_ids).
    - Uses node_candidate_id everywhere (NOT candidate_id).
    """
    rank_model_ver = "rank_v0_heuristic"

    with db_conn() as conn:
        # ------------------------------------------------------------
        # Guard: node_set exists?
        # ------------------------------------------------------------
        row_set = fetchone(
            conn,
            f"SELECT node_set_id FROM {T_NODE_SETS} WHERE node_set_id = %s::uuid",
            (node_set_id,),
        )
        if not row_set:
            raise ValueError(f"Unknown node_set_id={node_set_id}")

        # ------------------------------------------------------------
        # Stats we care about (simple + stable)
        # ------------------------------------------------------------
        sql_stats = f"""
            SELECT
              -- candidates
              (SELECT COUNT(*)::int
               FROM {T_NODE_CANDIDATES} c
               WHERE c.node_set_id = %s::uuid) AS n_candidates,

              -- cluster assignments + unique clusters
              (SELECT COUNT(*)::int
               FROM {T_NODE_CLUSTERS} sc
               WHERE sc.node_set_id = %s::uuid) AS n_cluster_assignments,

              (SELECT COUNT(DISTINCT sc.cluster_id)::int
               FROM {T_NODE_CLUSTERS} sc
               WHERE sc.node_set_id = %s::uuid) AS n_clusters,

              -- resolved rows
              (SELECT COUNT(*)::int
               FROM {T_NODES_RESOLVED} r
               WHERE r.node_set_id = %s::uuid) AS n_resolved,

              -- avg confidence (prefer prob_stop, fallback to confidence_v0)
              (SELECT COALESCE(AVG(COALESCE(f.prob_stop, f.confidence_v0, 0.0)), 0.0)
               FROM {T_NODE_FEATURES} f
               JOIN {T_NODE_CANDIDATES} c
                 ON c.node_candidate_id = f.node_candidate_id
               WHERE c.node_set_id = %s::uuid) AS avg_conf,

              -- pct with name/ref (quick quality proxy)
              (SELECT COALESCE(AVG((f.has_name)::int), 0.0)
               FROM {T_NODE_FEATURES} f
               JOIN {T_NODE_CANDIDATES} c
                 ON c.node_candidate_id = f.node_candidate_id
               WHERE c.node_set_id = %s::uuid) AS pct_has_name,

              (SELECT COALESCE(AVG((f.has_ref)::int), 0.0)
               FROM {T_NODE_FEATURES} f
               JOIN {T_NODE_CANDIDATES} c
                 ON c.node_candidate_id = f.node_candidate_id
               WHERE c.node_set_id = %s::uuid) AS pct_has_ref
        """

        # ✅ 7 placeholders → 7 params
        stats = fetchone(conn, sql_stats, (node_set_id,) * 7)

        if not stats:
            # keep pipeline safe
            stats = {
                "n_candidates": 0,
                "n_cluster_assignments": 0,
                "n_clusters": 0,
                "n_resolved": 0,
                "avg_conf": 0.0,
                "pct_has_name": 0.0,
                "pct_has_ref": 0.0,
            }

        n_candidates = int(stats["n_candidates"] or 0)
        n_clusters = int(stats["n_clusters"] or 0)
        n_resolved = int(stats["n_resolved"] or 0)
        avg_conf = float(stats["avg_conf"] or 0.0)
        pct_has_name = float(stats["pct_has_name"] or 0.0)
        pct_has_ref = float(stats["pct_has_ref"] or 0.0)

        # ------------------------------------------------------------
        # Rank score (heuristic v0)
        # - We want: more resolved, higher confidence, more naming metadata
        # - Very small sets shouldn’t look “perfect” just because avg_conf=1
        # ------------------------------------------------------------
        size_factor = math.log1p(n_resolved) / 4.0  # saturates gently
        meta_factor = 0.5 * pct_has_name + 0.5 * pct_has_ref

        rank_score = (
            0.65 * avg_conf
            + 0.25 * meta_factor
            + 0.10 * size_factor
        )

        rank_score = max(0.0, min(1.0, float(rank_score)))

        # Persist onto node_sets table
        update_node_set_rank(
            conn,
            node_set_id=node_set_id,
            rank_score=rank_score,
            rank_model_ver=rank_model_ver,
        )

        # Optional: store metrics if table exists (use savepoint to avoid
        # aborting the main transaction if the metrics table schema mismatches)
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT metrics_save")
            exec_sql(
                conn,
                f"""
                INSERT INTO {T_NODE_SET_METRICS}
                  (node_set_id, n_candidates, n_clusters, n_resolved, avg_conf, pct_has_name, pct_has_ref)
                VALUES
                  (%s::uuid, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (node_set_id) DO UPDATE SET
                  n_candidates = EXCLUDED.n_candidates,
                  n_clusters   = EXCLUDED.n_clusters,
                  n_resolved   = EXCLUDED.n_resolved,
                  avg_conf     = EXCLUDED.avg_conf,
                  pct_has_name = EXCLUDED.pct_has_name,
                  pct_has_ref  = EXCLUDED.pct_has_ref,
                  computed_at  = now()
                """,
                (node_set_id, n_candidates, n_clusters, n_resolved, avg_conf, pct_has_name, pct_has_ref),
            )
            with conn.cursor() as cur:
                cur.execute("RELEASE SAVEPOINT metrics_save")
        except Exception as e:
            # Rollback to savepoint so the rank update is preserved
            try:
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT metrics_save")
            except Exception:
                pass
            logger.warning("rank_sets.metrics_skip node_set_id=%s err=%s", node_set_id, str(e))

    return {
        "node_set_id": node_set_id,
        "rank_score": rank_score,
        "rank_model_ver": rank_model_ver,
        "n_candidates": n_candidates,
        "n_clusters": n_clusters,
        "n_resolved": n_resolved,
        "avg_conf": avg_conf,
        "pct_has_name": pct_has_name,
        "pct_has_ref": pct_has_ref,
    }
