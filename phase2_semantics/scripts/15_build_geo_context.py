from __future__ import annotations

import os
from typing import Optional

from src.db.conn import db_conn
from src.settings import GEO_CONTEXT_KEY
from src.utils.jsonlog import get_logger

logger = get_logger("phase2.build_geo_context")


def _latest_extract_run_id(conn, *, context_key: Optional[str]) -> str:
    with conn.cursor() as cur:
        if context_key:
            cur.execute(
                """
                SELECT extract_run_id::text
                FROM geo_raw.extract_runs
                WHERE context_key = %s
                ORDER BY extracted_at DESC
                LIMIT 1
                """,
                (context_key,),
            )
        else:
            cur.execute(
                """
                SELECT extract_run_id::text
                FROM geo_raw.extract_runs
                ORDER BY extracted_at DESC
                LIMIT 1
                """
            )
        row = cur.fetchone()
    if not row:
        raise RuntimeError("No extract_run found for Step 15")
    return str(row[0] if not isinstance(row, dict) else row["extract_run_id"])


def _resolve_extract_run_id(conn, *, context_key: Optional[str]) -> str:
    env_extract_run_id = str(os.getenv("EXTRACT_RUN_ID") or "").strip()
    source_node_set_id = str(os.getenv("SOURCE_NODE_SET_ID") or "").strip()

    with conn.cursor() as cur:
        if env_extract_run_id:
            cur.execute(
                """
                SELECT extract_run_id::text
                FROM geo_raw.extract_runs
                WHERE extract_run_id::text = %s
                LIMIT 1
                """,
                (env_extract_run_id,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"Unknown extract_run_id for Step 15: {env_extract_run_id}")
            return str(row[0] if not isinstance(row, dict) else row["extract_run_id"])

        if source_node_set_id:
            if context_key:
                cur.execute(
                    """
                    SELECT extract_run_id::text
                    FROM geo_raw.extract_runs
                    WHERE source_node_set_id::text = %s
                      AND context_key = %s
                    ORDER BY extracted_at DESC
                    LIMIT 1
                    """,
                    (source_node_set_id, context_key),
                )
            else:
                cur.execute(
                    """
                    SELECT extract_run_id::text
                    FROM geo_raw.extract_runs
                    WHERE source_node_set_id::text = %s
                    ORDER BY extracted_at DESC
                    LIMIT 1
                    """,
                    (source_node_set_id,),
                )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(
                    f"No extract_run found for Step 15 with source_node_set_id={source_node_set_id}"
                )
            return str(row[0] if not isinstance(row, dict) else row["extract_run_id"])

    return _latest_extract_run_id(conn, context_key=context_key)


def main() -> None:
    with db_conn() as conn:
        extract_run_id = _resolve_extract_run_id(conn, context_key=GEO_CONTEXT_KEY)

        with conn.cursor() as cur:
            # Large node sets need more time for spatial density queries
            cur.execute("SET statement_timeout = '600s'")
            cur.execute(
                """
                WITH ev_nodes AS (
                  SELECT DISTINCT ne.node_id
                  FROM geo_raw.name_evidence ne
                  WHERE ne.extract_run_id = %s
                ), base AS (
                  SELECT
                    n.node_id,
                    ST_Y(n.geom)::float8 AS lat,
                    ST_X(n.geom)::float8 AS lon,
                    ST_GeoHash(n.geom, 7) AS geohash7,
                    n.chosen_tags
                  FROM node_prod.nodes n
                  JOIN ev_nodes e ON e.node_id = n.node_id
                ), dens AS (
                  SELECT
                    b.node_id,
                    COALESCE((
                      SELECT COUNT(*)::int
                      FROM node_prod.nodes n2
                      WHERE ST_DWithin(b.geom::geography, n2.geom::geography, 300)
                        AND (
                          n2.node_type = 'STOP'
                          OR COALESCE(n2.chosen_tags->>'highway','') = 'bus_stop'
                          OR COALESCE(n2.chosen_tags->>'public_transport','') IN ('platform','stop_position')
                        )
                    ), 0) AS transit_density_300m,
                    COALESCE((
                      SELECT COUNT(*)::int
                      FROM node_prod.nodes n3
                      WHERE ST_DWithin(b.geom::geography, n3.geom::geography, 300)
                        AND (
                          n3.node_type = 'POI'
                          OR COALESCE(n3.chosen_tags->>'amenity','') <> ''
                          OR COALESCE(n3.chosen_tags->>'shop','') <> ''
                          OR COALESCE(n3.chosen_tags->>'tourism','') <> ''
                        )
                    ), 0) AS poi_density_300m
                  FROM (
                    SELECT n.node_id, n.geom
                    FROM node_prod.nodes n
                    JOIN ev_nodes e ON e.node_id = n.node_id
                  ) b
                )
                INSERT INTO geo_work.node_geo_context
                  (extract_run_id, context_key, node_id, lat, lon, geohash7,
                   transit_density_300m, poi_density_300m,
                   tag_stop_weight, tag_poi_weight, features)
                SELECT
                  %s::uuid,
                  %s,
                  b.node_id,
                  b.lat,
                  b.lon,
                  b.geohash7,
                  d.transit_density_300m,
                  d.poi_density_300m,
                  (
                    CASE WHEN COALESCE(b.chosen_tags->>'highway','') = 'bus_stop' THEN 1.0 ELSE 0.0 END +
                    CASE WHEN COALESCE(b.chosen_tags->>'public_transport','') IN ('platform','stop_position') THEN 0.9 ELSE 0.0 END +
                    CASE WHEN COALESCE(b.chosen_tags->>'bus','') = 'yes' THEN 0.6 ELSE 0.0 END
                  )::float8,
                  (
                    CASE WHEN COALESCE(b.chosen_tags->>'amenity','') IN ('school','university','college') THEN 0.7 ELSE 0.0 END +
                    CASE WHEN COALESCE(b.chosen_tags->>'amenity','') IN ('hospital','clinic') THEN 0.7 ELSE 0.0 END +
                    CASE WHEN COALESCE(b.chosen_tags->>'shop','') IN ('supermarket','mall') THEN 0.6 ELSE 0.0 END +
                    CASE WHEN COALESCE(b.chosen_tags->>'tourism','') IN ('attraction','museum') THEN 0.6 ELSE 0.0 END
                  )::float8,
                  jsonb_build_object(
                    'context_key', %s,
                    'extract_run_id', %s,
                    'geohash7', b.geohash7,
                    'transit_density_300m', d.transit_density_300m,
                    'poi_density_300m', d.poi_density_300m
                  )
                FROM base b
                JOIN dens d ON d.node_id = b.node_id
                ON CONFLICT (extract_run_id, node_id)
                DO UPDATE SET
                  context_key = EXCLUDED.context_key,
                  lat = EXCLUDED.lat,
                  lon = EXCLUDED.lon,
                  geohash7 = EXCLUDED.geohash7,
                  transit_density_300m = EXCLUDED.transit_density_300m,
                  poi_density_300m = EXCLUDED.poi_density_300m,
                  tag_stop_weight = EXCLUDED.tag_stop_weight,
                  tag_poi_weight = EXCLUDED.tag_poi_weight,
                  features = EXCLUDED.features,
                  created_at = now()
                """,
                (extract_run_id, extract_run_id, GEO_CONTEXT_KEY, GEO_CONTEXT_KEY, extract_run_id),
            )

        conn.commit()

    logger.info(
        "✓ Step 15 completed",
        extra={
            "extract_run_id": extract_run_id,
            "source_node_set_id": str(os.getenv("SOURCE_NODE_SET_ID") or "").strip() or None,
            "context_key": GEO_CONTEXT_KEY,
        },
    )


if __name__ == "__main__":
    main()
