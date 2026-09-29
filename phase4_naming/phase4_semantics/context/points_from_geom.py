from __future__ import annotations

from typing import List, Tuple

from phase4_semantics.context.load_route import _qall


def sample_points_from_ewkt(cur, geometry_ewkt: str, n_points: int = 50) -> List[Tuple[float, float]]:
    """
    Samples points from an EWKT geometry using PostGIS.
    Requires DB connection. Returns [(lat, lon), ...].
    """

    if not geometry_ewkt:
        return []

    if n_points <= 1:
        n_points = 1

    if n_points == 1:
        frac_sql = "SELECT 0.5::double precision AS frac"
    else:
        frac_sql = f"""
        SELECT (gs::double precision / {n_points - 1}::double precision) AS frac
        FROM generate_series(0, {n_points - 1}) AS gs
        """

    rows = _qall(
        cur,
        f"""
        WITH g AS (
          SELECT ST_GeomFromEWKT(%s) AS geom
        ),
        fracs AS (
          {frac_sql}
        )
        SELECT
          ST_Y(p) AS lat,
          ST_X(p) AS lon
        FROM fracs
        CROSS JOIN LATERAL (
          SELECT ST_LineInterpolatePoint(ST_LineMerge(g.geom), fracs.frac) AS p
          FROM g
        ) q
        WHERE p IS NOT NULL
        """,
        (geometry_ewkt,),
    )

    out: List[Tuple[float, float]] = []
    for r in rows:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        out.append((float(r["lat"]), float(r["lon"])))

    return out
