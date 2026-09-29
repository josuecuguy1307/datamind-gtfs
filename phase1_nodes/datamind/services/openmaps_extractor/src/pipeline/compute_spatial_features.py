"""
Compute spatial features for node_work.node_features using PostGIS.

Populates:
  - distance_to_nearest_road_m
  - distance_to_nearest_stop_m
  - nearby_stop_density_100m
  - nearby_poi_density_100m
  - road_type_nearest
  - on_road_way

Usage:
    python -m phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.compute_spatial_features
"""
from __future__ import annotations

import logging
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def compute_all():
    from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import db_conn, exec_sql, fetchone

    with db_conn() as conn:
        # ============================================================
        # Step 1: Distance to nearest transit stop
        # ============================================================
        logger.info("Computing distance_to_nearest_stop_m...")
        t0 = time.time()
        exec_sql(conn, """
            UPDATE node_work.node_features nf
            SET distance_to_nearest_stop_m = sub.dist_m
            FROM (
                SELECT nc1.node_candidate_id,
                    (SELECT ST_Distance(nc1.geom::geography, nc2.geom::geography)
                     FROM node_work.node_candidates nc2
                     WHERE nc2.tag_kind IN ('bus_stop', 'platform', 'station', 'stop_position')
                       AND nc2.node_candidate_id != nc1.node_candidate_id
                       AND nc2.geom IS NOT NULL
                     ORDER BY nc1.geom <-> nc2.geom
                     LIMIT 1) AS dist_m
                FROM node_work.node_candidates nc1
                WHERE nc1.geom IS NOT NULL
            ) sub
            WHERE nf.node_candidate_id = sub.node_candidate_id
              AND sub.dist_m IS NOT NULL
        """)
        conn.commit()
        logger.info("  Done in %.1fs", time.time() - t0)

        # ============================================================
        # Step 2: Nearby density counts (100m radius)
        # Use a grid-based approach for efficiency
        # ============================================================
        logger.info("Computing nearby densities (100m radius)...")
        t0 = time.time()
        exec_sql(conn, """
            UPDATE node_work.node_features nf
            SET nearby_stop_density_100m = sub.stop_count,
                nearby_poi_density_100m = sub.poi_count
            FROM (
                SELECT nc.node_candidate_id,
                    (SELECT COUNT(*) FROM node_work.node_candidates nc2
                     WHERE nc2.tag_kind IN ('bus_stop', 'platform', 'station', 'stop_position')
                       AND nc2.node_candidate_id != nc.node_candidate_id
                       AND nc2.geom IS NOT NULL
                       AND ST_DWithin(nc.geom::geography, nc2.geom::geography, 100)) AS stop_count,
                    (SELECT COUNT(*) FROM node_work.node_candidates nc2
                     WHERE nc2.tag_kind = 'poi'
                       AND nc2.node_candidate_id != nc.node_candidate_id
                       AND nc2.geom IS NOT NULL
                       AND ST_DWithin(nc.geom::geography, nc2.geom::geography, 100)) AS poi_count
                FROM node_work.node_candidates nc
                WHERE nc.geom IS NOT NULL
            ) sub
            WHERE nf.node_candidate_id = sub.node_candidate_id
        """)
        conn.commit()
        logger.info("  Done in %.1fs", time.time() - t0)

        # ============================================================
        # Step 3: Distance to nearest road (using overpass ways)
        # ============================================================
        logger.info("Computing distance_to_nearest_road_m...")
        t0 = time.time()

        # Check if we have road ways
        row = fetchone(conn, """
            SELECT COUNT(*) AS n FROM node_raw.overpass_elements
            WHERE osm_type = 'way' AND tags->>'highway' IS NOT NULL
        """)
        n_roads = int(row["n"] or 0)
        logger.info("  Found %d road way elements", n_roads)

        if n_roads > 0:
            # Create temp index if it doesn't exist
            try:
                exec_sql(conn, """
                    CREATE INDEX IF NOT EXISTS idx_oe_highway_geom
                    ON node_raw.overpass_elements USING GIST(geom)
                    WHERE osm_type = 'way' AND tags->>'highway' IS NOT NULL
                """)
                conn.commit()
            except Exception:
                conn.rollback()

            exec_sql(conn, """
                UPDATE node_work.node_features nf
                SET distance_to_nearest_road_m = sub.dist_m,
                    road_type_nearest = sub.road_type,
                    on_road_way = (sub.dist_m IS NOT NULL AND sub.dist_m <= 3.0)
                FROM (
                    SELECT nc.node_candidate_id,
                        (SELECT ST_Distance(nc.geom::geography, rl.geom::geography)
                         FROM node_raw.overpass_elements rl
                         WHERE rl.osm_type = 'way'
                           AND rl.tags->>'highway' IS NOT NULL
                           AND rl.geom IS NOT NULL
                         ORDER BY nc.geom <-> rl.geom
                         LIMIT 1) AS dist_m,
                        (SELECT rl.tags->>'highway'
                         FROM node_raw.overpass_elements rl
                         WHERE rl.osm_type = 'way'
                           AND rl.tags->>'highway' IS NOT NULL
                           AND rl.geom IS NOT NULL
                         ORDER BY nc.geom <-> rl.geom
                         LIMIT 1) AS road_type
                    FROM node_work.node_candidates nc
                    WHERE nc.geom IS NOT NULL
                ) sub
                WHERE nf.node_candidate_id = sub.node_candidate_id
                  AND sub.dist_m IS NOT NULL
            """)
            conn.commit()
        else:
            logger.warning("  No road ways found — skipping road distance features")

        logger.info("  Done in %.1fs", time.time() - t0)

        # ============================================================
        # Verify
        # ============================================================
        row = fetchone(conn, """
            SELECT
                COUNT(*) FILTER (WHERE distance_to_nearest_stop_m IS NOT NULL) AS has_stop_dist,
                COUNT(*) FILTER (WHERE nearby_stop_density_100m > 0) AS has_stop_density,
                COUNT(*) FILTER (WHERE distance_to_nearest_road_m IS NOT NULL) AS has_road_dist,
                COUNT(*) FILTER (WHERE on_road_way) AS on_road,
                COUNT(*) AS total
            FROM node_work.node_features
        """)
        logger.info("Spatial features computed:")
        logger.info("  distance_to_nearest_stop_m: %d / %d", row["has_stop_dist"], row["total"])
        logger.info("  nearby_stop_density_100m > 0: %d / %d", row["has_stop_density"], row["total"])
        logger.info("  distance_to_nearest_road_m: %d / %d", row["has_road_dist"], row["total"])
        logger.info("  on_road_way: %d / %d", row["on_road"], row["total"])


if __name__ == "__main__":
    compute_all()
