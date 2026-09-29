from __future__ import annotations

from typing import Dict, List
import psycopg2
import psycopg2.extras

from src.settings import DB_DSN, EMBED_DIM, EMBED_MODEL_NAME
from src.utils.jsonlog import get_logger


logger = get_logger(__name__)


# -----------------------------
# Upsert logic
# -----------------------------

def _execute_upsert(sql: str, rows: List[Dict], *, label: str) -> None:
    if not rows:
        return
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                sql,
                rows,
                page_size=256,
            )

    logger.info(f"pgvector upsert: {label}={len(rows)}")


def upsert_place_alias_embeddings(records: List[Dict]) -> None:
    """
    Bulk upsert alias vectors into PostgreSQL (pgvector).
    Canonical target is geo_prod.place_alias_embeddings.
    """
    rows = []
    for r in records:
        payload = r.get("payload") or {}
        rows.append(
            {
                "alias_id": r["id"],
                "place_id": payload["place_id"],
                "model_name": EMBED_MODEL_NAME,
                "model_version": None,
                "dim": int(EMBED_DIM),
                "embedding": r["vector"],  # pgvector accepts Python lists
            }
        )

    sql = """
    INSERT INTO geo_prod.place_alias_embeddings (
        alias_id,
        place_id,
        model_name,
        model_version,
        dim,
        embedding,
        updated_at
    )
    VALUES (
        %(alias_id)s,
        %(place_id)s,
        %(model_name)s,
        %(model_version)s,
        %(dim)s,
        %(embedding)s,
        now()
    )
    ON CONFLICT (alias_id) DO UPDATE SET
        place_id = EXCLUDED.place_id,
        model_name = EXCLUDED.model_name,
        model_version = EXCLUDED.model_version,
        dim = EXCLUDED.dim,
        embedding = EXCLUDED.embedding,
        updated_at = now()
    """
    _execute_upsert(sql, rows, label="alias_embeddings")


def upsert_place_embeddings(records: List[Dict]) -> None:
    """
    Bulk upsert canonical place vectors into PostgreSQL (pgvector).
    Canonical target is geo_prod.place_embeddings.
    """
    rows = []
    for r in records:
        rows.append(
            {
                "place_id": r["id"],
                "model_name": EMBED_MODEL_NAME,
                "model_version": None,
                "dim": int(EMBED_DIM),
                "embedding": r["vector"],  # pgvector accepts Python lists
            }
        )

    sql = """
    INSERT INTO geo_prod.place_embeddings (
        place_id,
        model_name,
        model_version,
        dim,
        embedding,
        updated_at
    )
    VALUES (
        %(place_id)s,
        %(model_name)s,
        %(model_version)s,
        %(dim)s,
        %(embedding)s,
        now()
    )
    ON CONFLICT (place_id) DO UPDATE SET
        model_name = EXCLUDED.model_name,
        model_version = EXCLUDED.model_version,
        dim = EXCLUDED.dim,
        embedding = EXCLUDED.embedding,
        updated_at = now()
    """
    _execute_upsert(sql, rows, label="place_embeddings")


def upsert_pgvector(records: List[Dict]) -> None:
    upsert_place_alias_embeddings(records)
