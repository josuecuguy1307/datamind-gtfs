from __future__ import annotations

import uuid
from typing import Optional, Sequence, Tuple, List

from phase3_routes.services.route_constructor.src.db.conn import db_cursor
from phase3_routes.services.route_constructor.src.settings import (
    W_STOP_DIST,
    W_LENGTH,
)

# Optional: extra penalty weight (meters) per missing stop
W_MISSING_STOP_PENALTY_M = 500.0  # tune or set to 0.0


def score_geometry(
    conn,
    linestring_wkt: str,
    stop_node_ids: Optional[list[uuid.UUID]] = None,
    *,
    route_id: Optional[uuid.UUID] = None,
    stop_prior_seqs: Optional[list[int]] = None,
    stop_prior_points: Optional[Sequence[Tuple[float, float]]] = None,  # [(lat, lon), ...]
) -> tuple[float, float, float, float, dict]:
    """
    Returns: (score, length_m, avg_stop_dist_m, max_stop_dist_m, metrics)

    Base score = -(W_STOP_DIST*avg_stop_dist_m + W_LENGTH*length_m)
    Optional: penalize missing stops (when expected > found).
    Supports:
      A) canonical: stop_node_ids -> node_prod.nodes (node_type='STOP')
      B) raw-first: route_id + stop_prior_seqs -> route_work.relation_stop_prior (lat/lon points)
      C) direct: stop_prior_points -> (lat/lon points)
    """
    stop_node_ids = stop_node_ids or []
    stop_prior_seqs = stop_prior_seqs or []

    # Decide scoring source
    source = None
    expected = 0

    if stop_node_ids:
        source = "canonical_stop_node_ids"
        expected = len(stop_node_ids)
    elif stop_prior_points:
        source = "prior_points"
        expected = len(stop_prior_points)
    elif route_id and stop_prior_seqs:
        source = "relation_stop_prior_seqs"
        expected = len(stop_prior_seqs)
    else:
        # no stops => no meaningful scoring
        length_m = 0.0
        avg_d = 1e9
        max_d = 1e9
        score = -(W_STOP_DIST * avg_d + W_LENGTH * length_m)
        metrics = {
            "source": "none",
            "length_m": length_m,
            "avg_stop_dist_m": avg_d,
            "max_stop_dist_m": max_d,
            "stops_expected": 0,
            "stops_found": 0,
            "stops_missing": 0,
        }
        return score, length_m, avg_d, max_d, metrics

    with db_cursor(conn) as cur:
        if source == "canonical_stop_node_ids":
            # ✅ canonical STOP points from node_prod.nodes
            cur.execute(
                """
                WITH
                line AS (
                  SELECT ST_SetSRID(ST_GeomFromText(%s), 4326)::geography AS g
                ),
                stops AS (
                  SELECT geom::geography AS g
                  FROM node_prod.nodes
                  WHERE node_type = 'STOP'
                    AND node_id = ANY(%s::uuid[])
                )
                SELECT
                  (SELECT ST_Length(g) FROM line)                 AS length_m,
                  AVG(ST_Distance(stops.g, (SELECT g FROM line))) AS avg_stop_dist_m,
                  MAX(ST_Distance(stops.g, (SELECT g FROM line))) AS max_stop_dist_m,
                  COUNT(*)                                        AS stops_found
                FROM stops
                """,
                (linestring_wkt, [str(x) for x in stop_node_ids]),
            )
            row = cur.fetchone()

        elif source == "relation_stop_prior_seqs":
            # ✅ raw-first points from relation_stop_prior (lat/lon)
            # We keep the seq filter; order is not required for avg/max.
            cur.execute(
                """
                WITH
                line AS (
                  SELECT ST_SetSRID(ST_GeomFromText(%s), 4326)::geography AS g
                ),
                prior AS (
                  SELECT lat, lon
                  FROM route_work.relation_stop_prior
                  WHERE route_id = %s
                    AND seq = ANY(%s::int[])
                ),
                stops AS (
                  SELECT ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography AS g
                  FROM prior
                )
                SELECT
                  (SELECT ST_Length(g) FROM line)                 AS length_m,
                  AVG(ST_Distance(stops.g, (SELECT g FROM line))) AS avg_stop_dist_m,
                  MAX(ST_Distance(stops.g, (SELECT g FROM line))) AS max_stop_dist_m,
                  COUNT(*)                                        AS stops_found
                FROM stops
                """,
                (linestring_wkt, str(route_id), stop_prior_seqs),
            )
            row = cur.fetchone()

        else:
            # source == "prior_points"  (direct lat/lon list)
            lats = [float(p[0]) for p in stop_prior_points]  # type: ignore[arg-type]
            lons = [float(p[1]) for p in stop_prior_points]  # type: ignore[arg-type]

            cur.execute(
                """
                WITH
                line AS (
                  SELECT ST_SetSRID(ST_GeomFromText(%s), 4326)::geography AS g
                ),
                pts AS (
                  SELECT *
                  FROM unnest(%s::float8[], %s::float8[]) AS t(lat, lon)
                ),
                stops AS (
                  SELECT ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography AS g
                  FROM pts
                )
                SELECT
                  (SELECT ST_Length(g) FROM line)                 AS length_m,
                  AVG(ST_Distance(stops.g, (SELECT g FROM line))) AS avg_stop_dist_m,
                  MAX(ST_Distance(stops.g, (SELECT g FROM line))) AS max_stop_dist_m,
                  COUNT(*)                                        AS stops_found
                FROM stops
                """,
                (linestring_wkt, lats, lons),
            )
            row = cur.fetchone()

    length_m = float((row or {}).get("length_m") or 0.0)
    avg_d = float((row or {}).get("avg_stop_dist_m") or 1e9)
    max_d = float((row or {}).get("max_stop_dist_m") or 1e9)
    found = int((row or {}).get("stops_found") or 0)
    missing = max(0, expected - found)

    # Base
    score = -(W_STOP_DIST * avg_d + W_LENGTH * length_m)

    # Optional missing-stop penalty
    if W_MISSING_STOP_PENALTY_M > 0 and missing > 0:
        score -= (W_STOP_DIST * W_MISSING_STOP_PENALTY_M * missing)

    metrics = {
        "source": source,
        "length_m": length_m,
        "avg_stop_dist_m": avg_d,
        "max_stop_dist_m": max_d,
        "stops_expected": expected,
        "stops_found": found,
        "stops_missing": missing,
    }
    return score, length_m, avg_d, max_d, metrics
