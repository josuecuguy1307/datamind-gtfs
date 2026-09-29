"""
Block 4.2 — Backfill place-level embeddings.

Only 8 of 532K places have place-level embeddings. This script runs the
embedding pipeline specifically for place canonical names.

Usage:
    python -m scripts.06_backfill_place_embeddings [--batch-size 512]
"""
from __future__ import annotations

import argparse
import logging
from time import perf_counter

from src.db.conn import db_conn
from src.pipeline.embeddings.embedder import embed_many
from src.pipeline.embeddings.upsert_pgvector import upsert_place_embeddings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("phase2.backfill_place_embeddings")


def main():
    parser = argparse.ArgumentParser(description="Backfill place-level embeddings")
    parser.add_argument("--batch-size", type=int, default=512, help="Places per embedding batch")
    parser.add_argument("--active-only", action="store_true", default=True, help="Only active places")
    args = parser.parse_args()

    total_done = 0
    batch_num = 0

    with db_conn() as conn:
        # Count missing
        status_filter = "AND p.status = 'active'" if args.active_only else ""
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT COUNT(*)::int AS cnt
                    FROM geo_prod.places p
                    LEFT JOIN geo_prod.place_embeddings e ON e.place_id = p.place_id
                    WHERE e.place_id IS NULL
                      AND p.canonical_name IS NOT NULL
                      AND trim(p.canonical_name) != ''
                      {status_filter}"""
            )
            remaining = cur.fetchone()["cnt"]

        logger.info("Places without embeddings: %d", remaining)

        while True:
            t0 = perf_counter()

            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT p.place_id::text, p.canonical_name
                        FROM geo_prod.places p
                        LEFT JOIN geo_prod.place_embeddings e ON e.place_id = p.place_id
                        WHERE e.place_id IS NULL
                          AND p.canonical_name IS NOT NULL
                          AND trim(p.canonical_name) != ''
                          {status_filter}
                        LIMIT %s""",
                    (args.batch_size,),
                )
                rows = list(cur.fetchall() or [])

            if not rows:
                logger.info("All places have embeddings.")
                break

            texts = [r["canonical_name"] for r in rows]
            vectors = embed_many(texts)

            records = []
            for row, vec in zip(rows, vectors):
                records.append({
                    "id": row["place_id"],
                    "vector": vec,
                    "payload": {"place_id": row["place_id"]},
                })

            upsert_place_embeddings(records)

            batch_num += 1
            total_done += len(rows)
            elapsed = perf_counter() - t0

            logger.info(
                "Batch %d: %d places in %.1fs (%.0f/s) — total: %d/%d",
                batch_num, len(rows), elapsed,
                len(rows) / elapsed if elapsed > 0 else 0,
                total_done, remaining,
            )

    logger.info("Done. Total embedded: %d places in %d batches.", total_done, batch_num)


if __name__ == "__main__":
    main()
