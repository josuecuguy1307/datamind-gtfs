from __future__ import annotations

from typing import Iterable, Dict, List

from normalize.normalize_alias import normalize_alias
from embedder import embed
from upsert_opensearch import upsert_opensearch
from upsert_pgvector import upsert_pgvector
from utils.jsonlog import get_logger
from src.db.geo_prod_repo import fetch_all_aliases_for_embedding  


logger = get_logger(__name__)


# -----------------------------
# Core rebuild function
# -----------------------------

def rebuild_index(
    *,
    batch_size: int = 256,
) -> None:
    """
    Rebuild the full semantic index from aliases.
    """

    logger.info("Starting semantic index rebuild")

    aliases = fetch_all_aliases_for_embedding()
    total = len(aliases)

    logger.info(f"Found {total} aliases to index")

    batch: List[Dict] = []

    for idx, alias in enumerate(aliases, start=1):
        text = normalize_alias(alias["text"])

        if not text:
            continue

        vector = embed(text)

        record = {
            "id": alias["id"],
            "vector": vector,
            "payload": {
                "text": text,
                "place_id": alias["place_id"],
                "kind": alias.get("kind"),
                "region": alias.get("region"),
            },
        }

        batch.append(record)

        if len(batch) >= batch_size:
            _flush_batch(batch)
            logger.info(f"Indexed {idx}/{total}")
            batch.clear()

    if batch:
        _flush_batch(batch)

    logger.info("Semantic index rebuild complete")


# -----------------------------
# Batch flush
# -----------------------------

def _flush_batch(batch: List[Dict]) -> None:
    """
    Upsert a batch into all vector stores.
    """

    upsert_opensearch(batch)
    upsert_pgvector(batch)
