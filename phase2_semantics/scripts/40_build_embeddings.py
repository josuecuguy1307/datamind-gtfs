"""
Phase 2 – Semantic Geocoder
Step 40: Build semantic indexes from geo_prod canonical truth

Reads:
- geo_prod.place_aliases
- geo_prod.places
- geo_prod.place_alias_embeddings (to skip already-embedded)
- geo_prod.place_embeddings (to skip already-embedded)

Writes:
- pgvector alias embeddings
- pgvector canonical place embeddings

This step is:
- Idempotent
- Rebuild-safe
- geo_prod READ-ONLY (except embeddings table)
"""

from __future__ import annotations

from typing import Dict, List

from src.db.conn import db_conn
from src.pipeline.embeddings.embedder import embed_many
from src.pipeline.embeddings.upsert_pgvector import (
    upsert_place_alias_embeddings,
    upsert_place_embeddings,
)
from src.utils.jsonlog import get_logger

logger = get_logger("phase2.build_embeddings")

EMBED_CHUNK = 512  # texts per embed_many call


def _upsert_alias_batches(rows: List[Dict]) -> int:
    total = len(rows)
    if not total:
        return 0

    done = 0
    for i in range(0, total, EMBED_CHUNK):
        chunk = rows[i : i + EMBED_CHUNK]
        texts = [(r["alias"] or "").strip() for r in chunk]
        vectors = embed_many(texts)

        records: List[Dict] = []
        for r, vec in zip(chunk, vectors):
            records.append(
                {
                    "id": r["alias_id"],
                    "vector": vec,
                    "payload": {
                        "text": (r["alias"] or "").strip(),
                        "place_id": r["place_id"],
                    },
                }
            )

        upsert_place_alias_embeddings(records)
        done += len(records)
        logger.info("Alias chunk upserted", extra={"done": done, "total": total})

    return done


def _upsert_place_batches(rows: List[Dict]) -> int:
    total = len(rows)
    if not total:
        return 0

    done = 0
    for i in range(0, total, EMBED_CHUNK):
        chunk = rows[i : i + EMBED_CHUNK]
        texts = [(r["canonical_name"] or "").strip() for r in chunk]
        vectors = embed_many(texts)

        records: List[Dict] = []
        for r, vec in zip(chunk, vectors):
            records.append(
                {
                    "id": r["place_id"],
                    "vector": vec,
                    "payload": {
                        "text": (r["canonical_name"] or "").strip(),
                    },
                }
            )

        upsert_place_embeddings(records)
        done += len(records)
        logger.info("Place chunk upserted", extra={"done": done, "total": total})

    return done


def main() -> None:
    logger.info("Starting Step 40: build semantic indexes")

    # --------------------------------------------------
    # 1) Fetch aliases that still need embeddings
    #    (short-lived connection — close immediately)
    # --------------------------------------------------
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.alias_id, a.place_id, a.alias
                FROM geo_prod.place_aliases a
                LEFT JOIN geo_prod.place_alias_embeddings e
                    ON e.alias_id = a.alias_id
                WHERE e.alias_id IS NULL
                  AND a.alias IS NOT NULL
                  AND trim(a.alias) <> ''
            """)
            alias_rows = cur.fetchall()

            cur.execute("""
                SELECT p.place_id, p.canonical_name
                FROM geo_prod.places p
                LEFT JOIN geo_prod.place_embeddings e
                    ON e.place_id = p.place_id
                WHERE e.place_id IS NULL
                  AND p.canonical_name IS NOT NULL
                  AND trim(p.canonical_name) <> ''
            """)
            place_rows = cur.fetchall()

    alias_total = len(alias_rows)
    place_total = len(place_rows)
    logger.info(
        "Embeddings needed",
        extra={
            "alias_count": alias_total,
            "place_count": place_total,
        },
    )

    if not alias_total and not place_total:
        logger.info("✓ Step 40 completed (nothing to do)")
        return

    alias_done = _upsert_alias_batches(alias_rows)
    place_done = _upsert_place_batches(place_rows)

    logger.info(
        "✓ Step 40 completed",
        extra={
            "alias_embedded": alias_done,
            "place_embedded": place_done,
        },
    )


if __name__ == "__main__":
    main()
