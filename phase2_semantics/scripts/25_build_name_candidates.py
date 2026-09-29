from __future__ import annotations

import os

from src.db.conn import db_conn
from src.pipeline.naming.build_name_candidates import build_for_place_set
from src.settings import GEO_CONTEXT_KEY
from src.utils.jsonlog import get_logger

logger = get_logger("phase2.build_name_candidates")
MODEL_NAME = "phase2_name_ranker_v1"


def _resolve_place_set_id(conn) -> str:
    env_set = (os.getenv("PLACE_SET_ID") or "").strip()
    if env_set:
        return env_set

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT place_set_id::text
            FROM geo_work.place_candidate_sets
            WHERE (%s IS NULL OR context_key = %s)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (GEO_CONTEXT_KEY, GEO_CONTEXT_KEY),
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError("No place_set_id found for Step 25")
    return str(row[0] if not isinstance(row, dict) else row["place_set_id"])


def main() -> None:
    with db_conn() as conn:
        place_set_id = _resolve_place_set_id(conn)
        artifact = None
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT artifact
                FROM geo_work.model_registry
                WHERE model_name = %s
                LIMIT 1
                """,
                (MODEL_NAME,),
            )
            row = cur.fetchone()
            artifact = (row or {}).get("artifact") if isinstance(row, dict) else None

        out = build_for_place_set(conn, place_set_id=place_set_id, ranker_artifact=artifact)
        conn.commit()

    logger.info(
        "✓ Step 25 completed",
        extra={"place_set_id": place_set_id, **out},
    )


if __name__ == "__main__":
    main()
