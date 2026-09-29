"""
Phase 2 – Semantic Geocoder
Repository: node_prod (READ-ONLY)

Purpose:
- Read canonical nodes produced by Phase 1
- Provide input for semantic extraction (Step 10)

IMPORTANT:
- NO writes
- NO Phase 2 logic
- NO geo_* tables
"""

from __future__ import annotations

from typing import Any, Dict, List

from psycopg2.extensions import connection as PGConnection

from src.settings import (
    T_NODE_PROD_NODES,
    NODE_PROD_COLUMNS,
)


def fetch_nodes_for_semantic_extraction(
    conn: PGConnection,
    *,
    source_node_set_id: str | None = None,
) -> List[Dict[str, Any]]:
    """
    Fetch canonical nodes from Phase 1.

    Required columns:
      - node_id
      - node_type
      - geom
      - chosen_tags
      - confidence
    """
    with conn.cursor() as cur:
        if str(source_node_set_id or "").strip():
            cur.execute(
                f"""
                SELECT
                  node_id,
                  node_type,
                  geom,
                  chosen_tags,
                  confidence
                FROM {T_NODE_PROD_NODES}
                WHERE source_node_set_id::text = %s
                """,
                (str(source_node_set_id).strip(),),
            )
        else:
            cur.execute(
                f"""
                SELECT
                  node_id,
                  node_type,
                  geom,
                  chosen_tags,
                  confidence
                FROM {T_NODE_PROD_NODES}
                """
            )
        rows = cur.fetchall() or []

    # Defensive check (dev-time safety)
    if rows:
        missing = NODE_PROD_COLUMNS - rows[0].keys()
        if missing:
            raise RuntimeError(
                f"node_prod.nodes missing required columns: {missing}"
            )

    return rows
