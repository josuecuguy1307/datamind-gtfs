from __future__ import annotations

import uuid
from typing import Optional, Tuple, Any, List, Dict

from phase3_routes.services.route_constructor.src.db.conn import db_cursor


def _to_uuid(x: Any) -> uuid.UUID:
    if isinstance(x, uuid.UUID):
        return x
    return uuid.UUID(str(x))


def nearest_stop_node(
    conn,
    lat: float,
    lon: float,
    radius_m: float,
) -> Optional[Tuple[uuid.UUID, float]]:
    """
    Uses Phase 2 final prod mappings as canonical STOP source:
    geo_prod.node_place_map + geo_prod.places(active) + node_prod.nodes geometry.
    """
    sql = """
    SELECT
      m.node_id,
      ST_Distance(
        n.geom::geography,
        ST_SetSRID(ST_MakePoint(%s,%s), 4326)::geography
      ) AS dist_m
    FROM geo_prod.node_place_map m
    JOIN node_prod.nodes n
      ON n.node_id = m.node_id
    JOIN geo_prod.places p
      ON p.place_id = m.place_id
    WHERE n.node_type = 'STOP'
      AND p.status = 'active'
      AND ST_DWithin(
        n.geom::geography,
        ST_SetSRID(ST_MakePoint(%s,%s), 4326)::geography,
        %s
      )
    ORDER BY dist_m ASC, m.node_id ASC
    LIMIT 1
    """

    with db_cursor(conn) as cur:
        cur.execute(sql, (lon, lat, lon, lat, radius_m))
        row = cur.fetchone()
        if not row:
            return None

        if isinstance(row, dict):
            nid = row.get("node_id")
            dist = row.get("dist_m")
        else:
            nid = row[0]
            dist = row[1]

        if nid is None or dist is None:
            return None

        return (_to_uuid(nid), float(dist))


def stop_nodes_within_radius(
    conn,
    lat: float,
    lon: float,
    radius_m: float,
    *,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """
    Returns Phase 2 final-prod STOP nodes within radius ordered by distance asc.
    """
    sql = """
    SELECT
      m.node_id,
      ST_Distance(
        n.geom::geography,
        ST_SetSRID(ST_MakePoint(%s,%s), 4326)::geography
      ) AS dist_m
    FROM geo_prod.node_place_map m
    JOIN node_prod.nodes n
      ON n.node_id = m.node_id
    JOIN geo_prod.places p
      ON p.place_id = m.place_id
    WHERE n.node_type = 'STOP'
      AND p.status = 'active'
      AND ST_DWithin(
        n.geom::geography,
        ST_SetSRID(ST_MakePoint(%s,%s), 4326)::geography,
        %s
      )
    ORDER BY dist_m ASC, m.node_id ASC
    LIMIT %s
    """
    with db_cursor(conn) as cur:
        cur.execute(sql, (lon, lat, lon, lat, radius_m, int(limit)))
        rows = cur.fetchall() or []
    out: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            nid = row.get("node_id")
            dist = row.get("dist_m")
        else:
            nid = row[0]
            dist = row[1]
        if nid is None or dist is None:
            continue
        out.append({"node_id": _to_uuid(nid), "dist_m": float(dist)})
    return out
