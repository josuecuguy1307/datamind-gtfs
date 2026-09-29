from __future__ import annotations

import os
from typing import List, Dict

from opensearchpy import OpenSearch, helpers
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError
from src.utils.jsonlog import get_logger

logger = get_logger(__name__)

OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://127.0.0.1:9200")
OPENSEARCH_USER = os.getenv("OPENSEARCH_USER")
OPENSEARCH_PASS = os.getenv("OPENSEARCH_PASS")
INDEX_NAME = os.getenv("OPENSEARCH_INDEX", "places_alias_search")


def _get_client() -> OpenSearch:
    kwargs = {
        "hosts": [OPENSEARCH_URL],
        "http_compress": True,
        "timeout": 30,
        "max_retries": 3,
        "retry_on_timeout": True,
    }
    if OPENSEARCH_USER and OPENSEARCH_PASS:
        kwargs["http_auth"] = (OPENSEARCH_USER, OPENSEARCH_PASS)

    return OpenSearch(**kwargs)

def upsert_opensearch(records: List[Dict]) -> None:
    """
    Bulk upsert alias vectors into OpenSearch.
    """
    if not records:
        return
    actions = []

    client = _get_client()
    try:
        if not client.ping():
            raise RuntimeError(
                f"OpenSearch ping failed at {OPENSEARCH_URL}. "
                "Start OpenSearch or skip OpenSearch write for this run."
            )
    except OpenSearchConnectionError as e:
        raise RuntimeError(
            f"OpenSearch is unreachable at {OPENSEARCH_URL}. "
            "Start OpenSearch on that URL/port and retry."
        ) from e

    for r in records:
        actions.append({
             "_op_type": "index",   # idempotent upsert
            "_index": INDEX_NAME,
            "_id": r["id"],
            "_source": {
                "vector": r["vector"],
                **r["payload"],

        },
        })

    try:
        success, failed = helpers.bulk(
            client,
            actions,
            raise_on_error=False,
            stats_only=True,
        )
    except OpenSearchConnectionError as e:
        raise RuntimeError(
            f"OpenSearch bulk upsert failed (connection error) at {OPENSEARCH_URL}. "
            "Start OpenSearch and rerun Step 40."
        ) from e

    logger.info(f"Opensearc upsert: {success} success, {failed} failed")

    
