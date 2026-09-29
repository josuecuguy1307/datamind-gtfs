"""
Phase 2 – Semantic Geocoder
Step 30: Approve place candidate sets (geo_work → geo_prod)

Uses reviewed/custom names when available in geo_work.place_name_feedback.
"""

from __future__ import annotations

import os
from typing import List

from src.db.conn import db_conn
from src.utils.jsonlog import get_logger

logger = get_logger("phase2.approve")


def _target_sets(conn) -> List[str]:
    env_set = (os.getenv("PLACE_SET_ID") or "").strip()
    with conn.cursor() as cur:
        if env_set:
            return [env_set]
        cur.execute(
            """
            SELECT place_set_id::text
            FROM geo_work.place_candidate_sets
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
    return [str(row[0] if not isinstance(row, dict) else row["place_set_id"])] if row else []


def _promote_set(conn, place_set_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH latest_feedback AS (
              SELECT DISTINCT ON (f.place_candidate_id)
                f.place_candidate_id,
                f.chosen_name
              FROM geo_work.place_name_feedback f
              WHERE f.place_set_id::text = %s
              ORDER BY f.place_candidate_id, f.created_at DESC
            ), latest_type AS (
              SELECT DISTINCT ON (t.place_candidate_id)
                t.place_candidate_id,
                t.chosen_place_type
              FROM geo_work.poi_stop_feedback t
              WHERE t.place_set_id::text = %s
              ORDER BY t.place_candidate_id, t.created_at DESC
            )
            INSERT INTO geo_prod.places (place_id, canonical_name, place_type, geom, updated_at)
            SELECT
              pc.place_candidate_id,
              COALESCE(NULLIF(lf.chosen_name, ''), pc.proposed_canonical_name) AS canonical_name,
              COALESCE(lt.chosen_place_type, pc.model_place_type, pc.proposed_place_type),
              pc.center_geom,
              now()
            FROM geo_work.place_candidates pc
            LEFT JOIN latest_feedback lf
              ON lf.place_candidate_id = pc.place_candidate_id
            LEFT JOIN latest_type lt
              ON lt.place_candidate_id = pc.place_candidate_id
            WHERE pc.place_set_id::text = %s
            ON CONFLICT (place_id) DO UPDATE SET
              canonical_name = EXCLUDED.canonical_name,
              place_type = EXCLUDED.place_type,
              geom = EXCLUDED.geom,
              updated_at = now()
            """,
            (place_set_id, place_set_id, place_set_id),
        )

        cur.execute(
            """
            INSERT INTO geo_prod.place_aliases
              (place_id, alias, normalized_alias, alias_kind, lang, updated_at)
            SELECT
              pc.place_candidate_id,
              ac.alias,
              geo_work.normalize_alias(ac.alias),
              ac.alias_kind,
              ac.lang,
              now()
            FROM geo_work.alias_candidates ac
            JOIN geo_work.place_candidates pc
              ON pc.place_candidate_id = ac.place_candidate_id
            WHERE pc.place_set_id::text = %s
            ON CONFLICT (place_id, normalized_alias) DO UPDATE SET
              alias = EXCLUDED.alias,
              alias_kind = EXCLUDED.alias_kind,
              lang = EXCLUDED.lang,
              updated_at = now()
            """,
            (place_set_id,),
        )

        cur.execute(
            """
            WITH latest_feedback AS (
              SELECT DISTINCT ON (f.place_candidate_id)
                f.place_candidate_id,
                f.chosen_name
              FROM geo_work.place_name_feedback f
              WHERE f.place_set_id::text = %s
              ORDER BY f.place_candidate_id, f.created_at DESC
            )
            INSERT INTO geo_prod.place_aliases
              (place_id, alias, normalized_alias, alias_kind, lang, updated_at)
            SELECT
              pc.place_candidate_id,
              COALESCE(NULLIF(lf.chosen_name, ''), pc.proposed_canonical_name),
              geo_work.normalize_alias(COALESCE(NULLIF(lf.chosen_name, ''), pc.proposed_canonical_name)),
              'official',
              NULL,
              now()
            FROM geo_work.place_candidates pc
            LEFT JOIN latest_feedback lf
              ON lf.place_candidate_id = pc.place_candidate_id
            WHERE pc.place_set_id::text = %s
            ON CONFLICT (place_id, normalized_alias) DO UPDATE SET
              alias = EXCLUDED.alias,
              alias_kind = EXCLUDED.alias_kind,
              lang = EXCLUDED.lang,
              updated_at = now()
            """,
            (place_set_id, place_set_id),
        )

        cur.execute(
            """
            INSERT INTO geo_prod.node_place_map
              (node_id, place_id, confidence, mapping_source, updated_at)
            SELECT
              w.node_id,
              w.place_candidate_id,
              w.confidence,
              CASE w.mapping_source
                WHEN 'manual' THEN 'user_selected'
                ELSE 'phase2_auto'
              END,
              now()
            FROM geo_work.node_place_map_work w
            WHERE w.place_set_id::text = %s
            ON CONFLICT (node_id) DO UPDATE SET
              place_id = EXCLUDED.place_id,
              confidence = EXCLUDED.confidence,
              mapping_source = EXCLUDED.mapping_source,
              updated_at = now()
            """,
            (place_set_id,),
        )

        cur.execute(
            """
            SELECT
              (SELECT COUNT(*)::int FROM geo_work.place_candidates pc WHERE pc.place_set_id::text = %s) AS n_places,
              (SELECT COUNT(*)::int FROM geo_work.alias_candidates ac
                JOIN geo_work.place_candidates pc ON pc.place_candidate_id = ac.place_candidate_id
                WHERE pc.place_set_id::text = %s) AS n_aliases,
              (SELECT COUNT(*)::int FROM geo_work.node_place_map_work w WHERE w.place_set_id::text = %s) AS n_nodes
            """,
            (place_set_id, place_set_id, place_set_id),
        )
        counts = cur.fetchone() or {}

    return {
        "place_set_id": place_set_id,
        "n_places": int(counts.get("n_places") or 0),
        "n_aliases": int(counts.get("n_aliases") or 0),
        "n_nodes": int(counts.get("n_nodes") or 0),
    }


def main() -> None:
    with db_conn() as conn:
        target_ids = _target_sets(conn)
        if not target_ids:
            logger.info("No place sets found for Step 30")
            return

        out = []
        for pid in target_ids:
            out.append(_promote_set(conn, pid))
        conn.commit()

    logger.info("✓ Step 30 completed", extra={"approved_sets": out})


if __name__ == "__main__":
    main()
