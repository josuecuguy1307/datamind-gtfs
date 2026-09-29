from __future__ import annotations

from uuid import uuid4

from src.db.conn import db_conn
from src.settings import T_GEO_EXTRACT_RUNS


def create_extract_run(
    *,
    context_key: str | None,
    source_node_set_id: str | None = None,
) -> dict:
    """
    Register a new extraction run in geo_raw.extract_runs.

    ✅ Aligned EXACTLY with 001_geo_raw.sql:
      geo_raw.extract_runs(
        extract_run_id,
        source_node_set_id,
        context_key,
        status,
        runtime_ms,
        n_nodes_seen,
        n_evidence,
        extracted_at
      )

    We only insert the required/minimal fields.
    """
    extract_run_id = str(uuid4())

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {T_GEO_EXTRACT_RUNS} (
                    extract_run_id,
                    context_key,
                    source_node_set_id,
                    status
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    extract_run_id,
                    context_key,
                    source_node_set_id,
                    "ok",
                ),
            )
        conn.commit()

    return {
        "extract_run_id": extract_run_id,
        "context_key": context_key,
        "status": "ok",
    }
