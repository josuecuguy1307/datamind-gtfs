"""
Block 4.1 — Backfill geo-context for all nodes.

Currently only 5.8% coverage (25K/431K). This script runs the geo-context
computation for all nodes in batches.

Usage:
    python -m scripts.05_backfill_geo_context [--batch-size 5000]
"""
from __future__ import annotations

import argparse
import logging
import uuid
from time import perf_counter

from src.db.conn import db_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("phase2.backfill_geo_context")


# Uses a keyset cursor (last_node_id) instead of NOT IN subquery to avoid degradation.
_SQL_BACKFILL = """
WITH batch AS (
    SELECT n.node_id, n.geom
    FROM node_prod.nodes n
    LEFT JOIN geo_work.node_geo_context gc ON gc.node_id = n.node_id
    WHERE n.geom IS NOT NULL
      AND gc.node_id IS NULL
      AND n.node_id > %s
    ORDER BY n.node_id
    LIMIT %s
),
stop_counts AS (
    SELECT b.node_id,
           COUNT(n2.node_id)::int AS transit_density_300m
    FROM batch b
    LEFT JOIN node_prod.nodes n2
      ON n2.node_type = 'STOP'
      AND n2.node_id != b.node_id
      AND n2.geom && ST_Expand(b.geom, 0.003)
      AND ST_DWithin(b.geom::geography, n2.geom::geography, 300)
    GROUP BY b.node_id
),
poi_counts AS (
    SELECT b.node_id,
           COUNT(n2.node_id)::int AS poi_density_300m
    FROM batch b
    LEFT JOIN node_prod.nodes n2
      ON n2.node_type = 'POI'
      AND n2.node_id != b.node_id
      AND n2.geom && ST_Expand(b.geom, 0.003)
      AND ST_DWithin(b.geom::geography, n2.geom::geography, 300)
    GROUP BY b.node_id
)
INSERT INTO geo_work.node_geo_context (extract_run_id, context_key, node_id, lat, lon, transit_density_300m, poi_density_300m, geohash7, features, created_at)
SELECT
    %s::uuid AS extract_run_id,
    'sample_v1' AS context_key,
    b.node_id,
    ST_Y(b.geom) AS lat,
    ST_X(b.geom) AS lon,
    COALESCE(sc.transit_density_300m, 0),
    COALESCE(pc.poi_density_300m, 0),
    ST_GeoHash(b.geom, 7) AS geohash7,
    '{}'::jsonb AS features,
    now()
FROM batch b
LEFT JOIN stop_counts sc ON sc.node_id = b.node_id
LEFT JOIN poi_counts pc ON pc.node_id = b.node_id
ON CONFLICT (extract_run_id, node_id) DO UPDATE SET
    transit_density_300m = EXCLUDED.transit_density_300m,
    poi_density_300m = EXCLUDED.poi_density_300m,
    geohash7 = EXCLUDED.geohash7,
    features = EXCLUDED.features
RETURNING node_id
"""


def main():
    parser = argparse.ArgumentParser(description="Backfill geo-context for all nodes")
    parser.add_argument("--batch-size", type=int, default=5000, help="Nodes per batch")
    parser.add_argument("--max-batches", type=int, default=0, help="Max batches (0=until done)")
    args = parser.parse_args()

    total_inserted = 0
    batch_num = 0
    backfill_run_id = str(uuid.uuid4())

    with db_conn() as conn:
        # Count remaining
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*)::int AS cnt FROM node_prod.nodes n
                   LEFT JOIN geo_work.node_geo_context gc ON gc.node_id = n.node_id
                   WHERE n.geom IS NOT NULL AND gc.node_id IS NULL"""
            )
            remaining = cur.fetchone()["cnt"]

        logger.info("Nodes without geo-context: %d", remaining)

        if remaining == 0:
            logger.info("All nodes already have geo-context.")
            return

        # Create a dedicated extract run for this backfill
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO geo_raw.extract_runs
                   (extract_run_id, context_key, status, n_nodes_seen, extracted_at)
                   VALUES (%s::uuid, 'sample_v1', 'ok', %s, now())""",
                (backfill_run_id, remaining),
            )
        conn.commit()
        logger.info("Created backfill extract run: %s", backfill_run_id)

        # Use UUID zero as initial cursor
        last_node_id = '00000000-0000-0000-0000-000000000000'

        while True:
            if args.max_batches > 0 and batch_num >= args.max_batches:
                logger.info("Reached max batches (%d)", args.max_batches)
                break

            t0 = perf_counter()
            with conn.cursor() as cur:
                cur.execute(_SQL_BACKFILL, (last_node_id, args.batch_size, backfill_run_id))
                returned_ids = [row["node_id"] for row in cur.fetchall()]
                inserted = len(returned_ids)

            conn.commit()

            if inserted > 0:
                last_node_id = str(max(returned_ids))

            batch_num += 1
            total_inserted += inserted
            elapsed = perf_counter() - t0

            logger.info(
                "Batch %d: %d nodes in %.1fs (%.0f nodes/s) — total: %d",
                batch_num, inserted, elapsed,
                inserted / elapsed if elapsed > 0 else 0,
                total_inserted,
            )

            if inserted < args.batch_size:
                logger.info("All nodes processed.")
                break

    logger.info("Done. Total backfilled: %d nodes in %d batches.", total_inserted, batch_num)


if __name__ == "__main__":
    main()
