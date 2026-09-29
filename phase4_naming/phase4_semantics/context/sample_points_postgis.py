from __future__ import annotations
from typing import List, Tuple


def sample_points_from_ewkt(cur, geometry_text: str, n_points: int = 50) -> List[Tuple[float, float]]:
    """
    Accepts EWKT, WKT, OR WKB-hex text, returns [(lat, lon), ...].
    NEVER returns None.
    """

    if not geometry_text:
        return []

    n = max(2, int(n_points))

    sql = """
    WITH g AS (
      SELECT
        CASE
          WHEN %(g)s LIKE 'SRID=%%' THEN ST_GeomFromEWKT(%(g)s)
          WHEN %(g)s ~ '^[0-9A-Fa-f]+$' THEN
            -- WKB/EWKB hex
            ST_GeomFromEWKB(decode(%(g)s, 'hex'))
          ELSE
            -- WKT
            ST_SetSRID(ST_GeomFromText(%(g)s), 4326)
        END AS geom
    ),
    l AS (
      SELECT
        -- ensure we end up with linework
        ST_LineMerge(ST_CollectionExtract(geom, 2)) AS geom
      FROM g
      WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
    ),
    pts AS (
      SELECT
        generate_series(0, %(n_minus_1)s) AS i,
        geom
      FROM l
      WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
    )
    SELECT
      ST_Y(ST_LineInterpolatePoint(geom, (i::double precision / %(n_minus_1)s))) AS lat,
      ST_X(ST_LineInterpolatePoint(geom, (i::double precision / %(n_minus_1)s))) AS lon
    FROM pts
    ORDER BY i;
    """

    cur.execute(sql, {"g": geometry_text.strip(), "n_minus_1": n - 1})
    rows = cur.fetchall() or []

    out: List[Tuple[float, float]] = []
    for r in rows:
        lat = r["lat"] if isinstance(r, dict) else r[0]
        lon = r["lon"] if isinstance(r, dict) else r[1]
        if lat is None or lon is None:
            continue
        out.append((float(lat), float(lon)))

    return out
