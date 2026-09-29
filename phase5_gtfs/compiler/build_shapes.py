from __future__ import annotations

import math
from typing import Dict, Any, Optional, List, Tuple

from phase5_gtfs.common.config import db_conn

_EARTH_RADIUS_M = 6_371_000


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in **metres** between two WGS-84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _parse_linestring_ewkt(ewkt: str) -> List[Tuple[float, float]]:
    if not ewkt:
        return []
    s = ewkt.strip()
    if ";" in s:
        s = s.split(";", 1)[1]
    if not s.upper().startswith("LINESTRING"):
        return []
    raw = s[s.find("(") + 1 : s.rfind(")")]
    points: List[Tuple[float, float]] = []
    for part in raw.split(","):
        xy = [x for x in part.strip().split(" ") if x]
        if len(xy) < 2:
            continue
        lon = float(xy[0])
        lat = float(xy[1])
        points.append((lat, lon))
    return points


def build_shapes(
    export_run_id: str,
    *,
    route_id: Optional[str] = None,
    direction_id: Optional[int] = None,
) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            route_id_txt = str(route_id or "").strip()
            dir_filter = None if direction_id is None else int(direction_id)

            where = [
                "COALESCE(r.canonical_sequence_ready, FALSE) = TRUE",
                "r.chosen_geometry_candidate_id IS NOT NULL",
                "r.chosen_stop_sequence_candidate_id IS NOT NULL",
                "r.direction_id IS NOT NULL",
            ]
            params: List[Any] = []
            if route_id_txt:
                where.append("r.route_id::text = %s")
                params.append(route_id_txt)
            if dir_filter is not None:
                where.append("r.direction_id = %s")
                params.append(dir_filter)

            scope_sql = (
                """
                WITH candidates AS (
                  SELECT
                    r.route_id::text AS route_id,
                    r.direction_id::int AS direction_id,
                    COALESCE(r.service_route_id::text, r.route_id::text) AS shape_base_id,
                    ST_AsEWKT(gc.geom) AS geom_ewkt
                  FROM route_prod.routes r
                  JOIN route_work.geometry_candidates gc
                    ON gc.geometry_candidate_id = r.chosen_geometry_candidate_id
                  WHERE """
                + " AND ".join(where)
                + """
                ),
                ranked AS (
                  SELECT
                    c.*,
                    ROW_NUMBER() OVER (
                      PARTITION BY c.shape_base_id, c.direction_id
                      ORDER BY
                        CASE WHEN c.route_id = c.shape_base_id THEN 0 ELSE 1 END,
                        c.route_id
                    ) AS rn
                  FROM candidates c
                )
                SELECT route_id, direction_id, shape_base_id, geom_ewkt
                FROM ranked
                WHERE rn = 1
                """
            )

            cur.execute(
                f"""
                DELETE FROM gtfs_work.gtfs_shapes
                WHERE export_run_id = %s::uuid
                  AND shape_id IN (
                    SELECT 'shape_' || shape_base_id || '_d' || direction_id::text
                    FROM ({scope_sql}) s
                  )
                """ if route_id_txt or dir_filter is not None else
                "DELETE FROM gtfs_work.gtfs_shapes WHERE export_run_id = %s::uuid",
                (export_run_id, *params) if (route_id_txt or dir_filter is not None) else (export_run_id,),
            )

            cur.execute(scope_sql + " ORDER BY shape_base_id, direction_id", tuple(params))
            rows = list(cur.fetchall() or [])

            inserted = 0
            inserted_shapes: set[str] = set()
            for r in rows:
                dir_id = int(r.get("direction_id") or 0)
                shape_base_id = str(r.get("shape_base_id") or r["route_id"])
                shape_id = f"shape_{shape_base_id}_d{dir_id}"
                inserted_shapes.add(shape_id)
                # Polyline is the chosen geometry candidate as-is — already direction-correct.
                # No reversal: d0 and d1 each have their own chosen candidate, oriented in their natural direction.
                pts = _parse_linestring_ewkt(str(r.get("geom_ewkt") or ""))
                dist = 0.0
                prev = None
                seq = 1
                for lat, lon in pts:
                    if prev is not None:
                        step = _haversine_m(prev[0], prev[1], lat, lon)
                        if step <= 0.0:
                            continue
                        dist += step
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_shapes (
                          export_run_id, shape_id, shape_pt_lat, shape_pt_lon, shape_pt_sequence, shape_dist_traveled
                        ) VALUES (%s::uuid,%s,%s,%s,%s,%s)
                        """,
                        (export_run_id, shape_id, lat, lon, seq, dist),
                    )
                    inserted += 1
                    seq += 1
                    prev = (lat, lon)

    return {
        "shape_points": inserted,
        "shapes": int(len(inserted_shapes)),
        "route_id": (route_id_txt or None),
        "direction_id": (dir_filter if dir_filter is not None else None),
    }
