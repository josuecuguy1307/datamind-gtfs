from __future__ import annotations

import logging
import uuid

from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import (
    db_conn,
    fetchone,
    exec_sql,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.db.phase1_repo import (
    get_node_set_run_ids,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.settings import (
    T_NODE_CANDIDATES,
    T_ELEMENTS,
)


logger = logging.getLogger(__name__)


_STOP_TAG_KINDS = ("bus_stop", "platform", "stop_position", "station", "tram_stop")


def run_normalize(node_set_id: str, *, include_other: bool = True) -> dict:
    with db_conn() as conn:
        run_ids = get_node_set_run_ids(conn, node_set_id)

        # ✅ MVP fallback: treat node_set_id itself as run_id if raw exists
        if not run_ids:
            raw = fetchone(
                conn,
                """
                SELECT COUNT(*) AS n
                FROM node_raw.overpass_elements
                WHERE run_id = %s::uuid
                """,
                (node_set_id,),
            )

            if raw and int(raw["n"]) > 0:
                logger.warning(
                    "normalize.run.fallback node_set_id=%s (no mapping) -> using node_set_id as source_run_id",
                    node_set_id,
                )
                run_ids = [uuid.UUID(node_set_id)]
            else:
                logger.info("normalize.run.empty node_set_id=%s", node_set_id)
                return {"node_set_id": node_set_id, "candidates_total": 0, "run_ids": []}

        # ------------------------------------------------------------
        # ✅ REQUIRED: parent row must exist AND source_run_ids NOT NULL
        # ------------------------------------------------------------
        exec_sql(
            conn,
            """
            INSERT INTO node_work.node_candidate_sets (node_set_id, source_run_ids)
            VALUES (%s::uuid, %s::uuid[])
            ON CONFLICT (node_set_id)
            DO UPDATE SET source_run_ids = EXCLUDED.source_run_ids
            """,
            (node_set_id, run_ids),
        )

        # ------------------------------------------------------------
        # Insert normalized candidates
        # ------------------------------------------------------------
        tag_kind_filter = "" if include_other else "AND tag_kind <> 'other'"

        exec_sql(
            conn,
            f"""
            WITH materialized AS (
              SELECT
                e.run_id AS source_run_id,
                e.osm_type,
                e.osm_id,
                COALESCE(
                  e.geom,
                  CASE
                    WHEN e.lon IS NOT NULL AND e.lat IS NOT NULL
                      THEN ST_SetSRID(ST_MakePoint(e.lon, e.lat), 4326)
                    WHEN e.center_lon IS NOT NULL AND e.center_lat IS NOT NULL
                      THEN ST_SetSRID(ST_MakePoint(e.center_lon, e.center_lat), 4326)
                    ELSE NULL
                  END
                ) AS geom,
                e.tags,
                CASE
                  WHEN LOWER(COALESCE(e.tags->>'highway', '')) = 'bus_stop' THEN 'bus_stop'
                  WHEN LOWER(COALESCE(e.tags->>'public_transport', '')) = 'platform' THEN 'platform'
                  WHEN LOWER(COALESCE(e.tags->>'public_transport', '')) = 'stop_position' THEN 'stop_position'
                  WHEN LOWER(COALESCE(e.tags->>'amenity', '')) = 'bus_station' THEN 'station'
                  WHEN LOWER(COALESCE(e.tags->>'public_transport', '')) = 'station' THEN 'station'
                  WHEN LOWER(COALESCE(e.tags->>'railway', '')) = 'tram_stop' THEN 'tram_stop'
                  WHEN LOWER(COALESCE(e.tags->>'railway', '')) IN ('station', 'halt') THEN 'station'
                  WHEN e.tags ?| ARRAY['amenity', 'shop', 'tourism', 'office', 'leisure'] THEN 'poi'
                  ELSE 'other'
                END AS tag_kind
              FROM {T_ELEMENTS} e
              WHERE e.run_id = ANY(%s::uuid[])
            )
            INSERT INTO {T_NODE_CANDIDATES}
              (node_set_id, source_run_id, osm_type, osm_id, geom, tags, tag_kind)
            SELECT
              %s::uuid AS node_set_id,
              source_run_id,
              osm_type,
              osm_id,
              geom,
              tags,
              tag_kind
            FROM materialized
            WHERE geom IS NOT NULL
              {tag_kind_filter}
            ON CONFLICT (node_set_id, osm_type, osm_id) DO NOTHING
            """,
            (run_ids, node_set_id),
        )

        row = fetchone(
            conn,
            f"""
            SELECT
              COUNT(*) AS n,
              COUNT(*) FILTER (
                WHERE COALESCE(tag_kind, '') = ANY(%s::text[])
              ) AS stop_like_count,
              COUNT(*) FILTER (
                WHERE COALESCE(tag_kind, '') <> ALL(%s::text[])
              ) AS poi_like_count
            FROM {T_NODE_CANDIDATES}
            WHERE node_set_id = %s::uuid
            """,
            (list(_STOP_TAG_KINDS), list(_STOP_TAG_KINDS), node_set_id),
        )

        n = int(row["n"])
        stop_like_count = int(row.get("stop_like_count") or 0)
        poi_like_count = int(row.get("poi_like_count") or 0)
        conn.commit()

    return {
        "node_set_id": node_set_id,
        "candidates_total": n,
        "stop_like_count": stop_like_count,
        "poi_like_count": poi_like_count,
        "include_other": bool(include_other),
        "run_ids": [str(r) for r in run_ids],
    }
