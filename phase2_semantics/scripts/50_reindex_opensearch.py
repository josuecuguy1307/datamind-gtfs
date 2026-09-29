"""
Phase 2 – Semantic Geocoder
Step 50: PostgreSQL vector index refresh (OpenSearch-free).

Reads/Writes:
- geo_prod.place_alias_embeddings (ANALYZE)
- geo_prod.place_embeddings (ANALYZE)

This step keeps historical filename compatibility but no longer requires OpenSearch.
"""

from __future__ import annotations

from src.db.conn import db_conn
from src.utils.jsonlog import get_logger


logger = get_logger("phase2.reindex_pgvector")


def _table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (table_name,))
        row = cur.fetchone() or {}
        return bool(row.get("ok"))


def main() -> None:
    logger.info("Starting Step 50: refresh PostgreSQL vector search stats")
    analyzed = []
    with db_conn() as conn:
        candidates = [
            "geo_prod.place_alias_embeddings",
            "geo_prod.place_embeddings",
            "geo_prod.place_aliases",
            "geo_prod.places",
        ]
        for table in candidates:
            if not _table_exists(conn, table):
                continue
            with conn.cursor() as cur:
                cur.execute(f"ANALYZE {table}")
            analyzed.append(table)

    logger.info("✓ Step 50 completed", extra={"analyzed_tables": analyzed, "count": len(analyzed)})


if __name__ == "__main__":
    main()
