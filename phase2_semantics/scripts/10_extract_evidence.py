"""
Phase 2 – Semantic Geocoder
Step 10: Extract semantic evidence from node_prod → geo_raw.
"""

from __future__ import annotations

import os
from typing import Dict, Any, List

from src.db.conn import db_conn
from src.db.geo_raw_repo import insert_name_evidence_batch
from src.db.node_prod_repo import fetch_nodes_for_semantic_extraction

from src.pipeline.extract.create_extract_run import create_extract_run
from src.pipeline.extract.evidence_rules import collect_evidence
from src.pipeline.extract.extract_name_evidence import extract_name_evidence

from src.settings import GEO_CONTEXT_KEY
from src.utils.jsonlog import get_logger


logger = get_logger("phase2.extract_evidence")


# ------------------------------------------------------------
# Evidence adapters (PIPELINE → DB)
# ------------------------------------------------------------

def evidence_to_rows(
    *,
    extract_run_id: str,
    node_id: str,
    tags: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Convert pipeline Evidence objects into geo_raw.name_evidence rows.

    NOTE:
    - geo_raw.name_evidence.raw_text is NOT NULL in SQL,
      so we must always provide a string.
    """

    rows: List[Dict[str, Any]] = []

    # Convert tags to clean str->str for rule functions
    tags_str: Dict[str, str] = {
        str(k): str(v) for k, v in (tags or {}).items() if v is not None
    }

    # 1) Tag-based evidence (semantic signals like bus_stop, platform, etc.)
    for ev in collect_evidence(tags_str):
        rows.append({
            "extract_run_id": extract_run_id,
            "node_id": node_id,
            "source": ev.source,
            "raw_text": f"tag_signal:{ev.source}",   # ✅ NOT NULL (string)
            "lang": None,
            "weight_hint": float(ev.weight),
            "tags_snapshot": tags,
        })

    # 2) Name-based evidence (language rules on actual tag "name")
    name = tags_str.get("name")
    if name:
        for ev in extract_name_evidence(name):
            rows.append({
                "extract_run_id": extract_run_id,
                "node_id": node_id,
                "source": ev.source,
                "raw_text": name,                    # ✅ NOT NULL
                "lang": None,
                "weight_hint": float(ev.weight),
                "tags_snapshot": tags,
            })

    return rows


# ------------------------------------------------------------
# Main pipeline
# ------------------------------------------------------------

def main() -> None:
    logger.info("Starting semantic evidence extraction")
    source_node_set_id = str(os.getenv("SOURCE_NODE_SET_ID") or "").strip() or None

    # ✅ Correct signature now
    run = create_extract_run(
        context_key=GEO_CONTEXT_KEY,
        source_node_set_id=source_node_set_id,
    )

    extract_run_id = run["extract_run_id"]  # ✅ correct key

    with db_conn() as conn:
        nodes = fetch_nodes_for_semantic_extraction(conn, source_node_set_id=source_node_set_id)
        logger.info(
            "Fetched nodes",
            extra={"count": len(nodes), "source_node_set_id": source_node_set_id, "context_key": GEO_CONTEXT_KEY},
        )

        batch: List[Dict[str, Any]] = []

        for node in nodes:
            batch.extend(
                evidence_to_rows(
                    extract_run_id=extract_run_id,
                    node_id=node["node_id"],
                    tags=node["chosen_tags"],
                )
            )

        logger.info("Extracted evidence", extra={"rows": len(batch)})

        if batch:
            insert_name_evidence_batch(conn, batch)
            conn.commit()

    logger.info(
        "✓ Evidence extraction completed",
        extra={
            "extract_run_id": extract_run_id,
            "source_node_set_id": source_node_set_id,
            "context_key": GEO_CONTEXT_KEY,
        },
    )


if __name__ == "__main__":
    main()
