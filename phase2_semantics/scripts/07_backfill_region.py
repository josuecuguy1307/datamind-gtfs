"""
Block 5 — Backfill region column for active places.

Uses Phase 1 area_ids from node_candidate_sets as a region proxy.
Falls back to geohash-based sector assignment.

Usage:
    python -m scripts.07_backfill_region
"""
from __future__ import annotations

import logging
from time import perf_counter

from src.db.conn import db_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("phase2.backfill_region")


def main():
    with db_conn() as conn:
        # Count places without region
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*)::int AS cnt FROM geo_prod.places WHERE status = 'active' AND (region IS NULL OR region = '')"
            )
            n_missing = cur.fetchone()["cnt"]
            logger.info("Active places without region: %d", n_missing)

        if n_missing == 0:
            logger.info("All active places have region. Done.")
            return

        t0 = perf_counter()

        # Strategy 1: Use Phase 1 area_id from node_candidate_sets
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE geo_prod.places p
                SET region = sub.area_id, updated_at = now()
                FROM (
                    SELECT DISTINCT ON (npm.place_id)
                        npm.place_id,
                        ncs.area_id
                    FROM geo_prod.node_place_map npm
                    JOIN node_work.node_candidate_sets ncs ON ncs.node_set_id = (
                        SELECT nf.source_node_set_id
                        FROM node_prod.nodes np
                        LEFT JOIN node_work.node_features nf ON nf.node_id = np.node_id
                        WHERE np.node_id = npm.node_id
                        LIMIT 1
                    )
                    WHERE ncs.area_id IS NOT NULL AND ncs.area_id != ''
                    ORDER BY npm.place_id, ncs.created_at DESC
                ) sub
                WHERE p.place_id = sub.place_id
                  AND p.status = 'active'
                  AND (p.region IS NULL OR p.region = '')
            """)
            n_from_area = cur.rowcount
            logger.info("Region from Phase 1 area_id: %d places", n_from_area)

        conn.commit()

        # Strategy 2: Use geohash-7 prefix as region for remaining
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE geo_prod.places p
                SET region = 'geohash:' || ST_GeoHash(p.geom, 5), updated_at = now()
                WHERE p.status = 'active'
                  AND (p.region IS NULL OR p.region = '')
                  AND p.geom IS NOT NULL
            """)
            n_from_geohash = cur.rowcount
            logger.info("Region from geohash-5: %d places", n_from_geohash)

        conn.commit()

        elapsed = perf_counter() - t0
        logger.info(
            "Done in %.1fs. Filled: %d from area_id + %d from geohash = %d total",
            elapsed, n_from_area, n_from_geohash, n_from_area + n_from_geohash,
        )


if __name__ == "__main__":
    main()
