from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from phase4_semantics.common.models import RouteContext, StopContext


def _row_to_dict(cur, row) -> Dict[str, Any]:
    """
    Converts a psycopg2 row into a dict safely, regardless of cursor type.
    """
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    # fallback: map by description
    cols = [d[0] for d in (cur.description or [])]
    return {cols[i]: row[i] for i in range(min(len(cols), len(row)))}


def _q1(cur, sql: str, params: Tuple[Any, ...]) -> Optional[Dict[str, Any]]:
    cur.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return None
    return _row_to_dict(cur, row)


def _qall(cur, sql: str, params: Tuple[Any, ...]) -> List[Dict[str, Any]]:
    cur.execute(sql, params)
    rows = cur.fetchall() or []
    return [_row_to_dict(cur, r) for r in rows]


def _table_exists(cur, schema: str, table: str) -> bool:
    r = _q1(
        cur,
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        LIMIT 1
        """,
        (schema, table),
    )
    return bool(r)


def _columns(cur, schema: str, table: str) -> List[str]:
    rows = _qall(
        cur,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    return [r["column_name"] for r in rows if "column_name" in r]


def _pick_geom_column(cols: List[str]) -> Optional[str]:
    """
    We try the most common geometry column names.
    """
    candidates = ["geom", "geometry", "shape_geom", "route_geom", "line_geom"]
    for c in candidates:
        if c in cols:
            return c
    return None


def load_route(cur, route_id: str) -> RouteContext:
    """
    Loads Phase-4 context for a route from route_prod.routes
    (and joins stop nodes if available).
    """

    # --- 1) Load base route row
    if not _table_exists(cur, "route_prod", "routes"):
        raise RuntimeError("Missing table route_prod.routes. Phase 3 DB not ready?")

    route_cols = _columns(cur, "route_prod", "routes")
    geom_col = _pick_geom_column(route_cols)

    base = _q1(
        cur,
        """
        SELECT row_to_json(r) AS route_row
        FROM route_prod.routes r
        WHERE r.route_id = %s
        """,
        (route_id,),
    )
    if not base or not base.get("route_row"):
        raise ValueError(f"Route not found: {route_id}")

    route_row = base["route_row"]  # dict
    if "canonical_sequence_ready" in route_cols or "chosen_stop_sequence_candidate_id" in route_cols:
        canonical_sequence_ready = bool(route_row.get("canonical_sequence_ready"))
        chosen_stop_sequence_candidate_id = route_row.get("chosen_stop_sequence_candidate_id")
        if not canonical_sequence_ready or not chosen_stop_sequence_candidate_id:
            raise RuntimeError(
                f"Route {route_id} is not sequence-stabilized in route_prod.routes yet."
            )

    # --- 2) Load geometry context from PostGIS (EWKT + start/end/bbox)
    geometry_ewkt: Optional[str] = None
    start_latlon: Optional[Tuple[float, float]] = None
    end_latlon: Optional[Tuple[float, float]] = None
    bbox: Optional[Tuple[float, float, float, float]] = None

    if geom_col:
        spatial = _q1(
            cur,
            f"""
            SELECT
              ST_AsEWKT({geom_col}) AS geometry_ewkt,

              ST_X(ST_StartPoint(ST_LineMerge({geom_col}))) AS start_lon,
              ST_Y(ST_StartPoint(ST_LineMerge({geom_col}))) AS start_lat,

              ST_X(ST_EndPoint(ST_LineMerge({geom_col}))) AS end_lon,
              ST_Y(ST_EndPoint(ST_LineMerge({geom_col}))) AS end_lat,

              ST_XMin(ST_Envelope({geom_col})) AS minx,
              ST_YMin(ST_Envelope({geom_col})) AS miny,
              ST_XMax(ST_Envelope({geom_col})) AS maxx,
              ST_YMax(ST_Envelope({geom_col})) AS maxy
            FROM route_prod.routes
            WHERE route_id = %s
            """,
            (route_id,),
        )

        if spatial and spatial.get("geometry_ewkt"):
            geometry_ewkt = spatial["geometry_ewkt"]

            if spatial.get("start_lat") is not None and spatial.get("start_lon") is not None:
                start_latlon = (float(spatial["start_lat"]), float(spatial["start_lon"]))

            if spatial.get("end_lat") is not None and spatial.get("end_lon") is not None:
                end_latlon = (float(spatial["end_lat"]), float(spatial["end_lon"]))

            if all(spatial.get(k) is not None for k in ("minx", "miny", "maxx", "maxy")):
                bbox = (
                    float(spatial["minx"]),
                    float(spatial["miny"]),
                    float(spatial["maxx"]),
                    float(spatial["maxy"]),
                )

    # --- 3) Stops context: best path = stop_node_ids array
    stops = _load_stops_from_stop_node_ids(cur, route_id)

    # fallback: if stop_node_ids not present/empty, try mapping tables (older layouts)
    if not stops:
        stops = _load_route_stops_mapping_fallback(cur, route_id)

    return RouteContext(
        route_id=str(route_id),
        route_row=route_row,
        geometry_ewkt=geometry_ewkt,
        start_latlon=start_latlon,
        end_latlon=end_latlon,
        bbox=bbox,
        stops=stops,
    )


def _load_stops_from_stop_node_ids(cur, route_id: str) -> List[StopContext]:
    """
    Loads stops using route_prod.routes.stop_node_ids (uuid[]).

    Phase-3 compatible strategy:
    1) Always extract stop ids with correct order using LATERAL unnest(...) WITH ORDINALITY
    2) Prefer route_work.relation_stop_prior for lat/lon/osm_ref (no node_prod dependency)
    3) If relation_stop_prior doesn't exist, fallback to node_* tables if present
    """

    route_cols = _columns(cur, "route_prod", "routes")
    if "stop_node_ids" not in route_cols:
        return []

    # ------------------------------------------------------------
    # A) Best (Phase-3): use route_work.relation_stop_prior for lat/lon
    # ------------------------------------------------------------
    if _table_exists(cur, "route_work", "relation_stop_prior"):
        rows = _qall(
            cur,
            """
            WITH ordered AS (
              SELECT
                u.node_id::uuid AS node_id,
                u.ord::int      AS ord
              FROM route_prod.routes r
              JOIN LATERAL unnest(r.stop_node_ids) WITH ORDINALITY AS u(node_id, ord)
                ON TRUE
              WHERE r.route_id = %s
            )
            SELECT
              o.node_id::text AS stop_id,
              o.ord::int      AS seq,

              NULL::text      AS name,

              p.lat           AS lat,
              p.lon           AS lon,

              row_to_json(p)  AS node_raw
            FROM ordered o
            LEFT JOIN route_work.relation_stop_prior p
              ON p.route_id = %s
             AND p.seq = o.ord          -- ordinality starts at 1; Phase-3 prior seq is 1..N
            ORDER BY o.ord ASC
            """,
            (route_id, route_id),
        )

        out: List[StopContext] = []
        for r in rows:
            out.append(
                StopContext(
                    stop_id=str(r.get("stop_id")),
                    seq=int(r["seq"]) if r.get("seq") is not None else None,
                    name=r.get("name"),
                    lat=float(r["lat"]) if r.get("lat") is not None else None,
                    lon=float(r["lon"]) if r.get("lon") is not None else None,
                    raw={"prior": r.get("node_raw") or {}},
                )
            )
        return out

    # ------------------------------------------------------------
    # B) Fallback: join to a node table if it exists
    # ------------------------------------------------------------
    node_table = None
    node_cols = None
    for sch, tbl in [("node_prod", "nodes"), ("node_work", "nodes"), ("public", "nodes")]:
        if _table_exists(cur, sch, tbl):
            cols = _columns(cur, sch, tbl)
            if "node_id" in cols or "id" in cols:
                node_table = (sch, tbl)
                node_cols = cols
                break

    if node_table and node_cols:
        sch, tbl = node_table
        id_col = "node_id" if "node_id" in node_cols else "id"

        lat_expr = "n.lat" if "lat" in node_cols else ("ST_Y(n.geom)" if "geom" in node_cols else "NULL::double precision")
        lon_expr = "n.lon" if "lon" in node_cols else ("ST_X(n.geom)" if "geom" in node_cols else "NULL::double precision")
        name_expr = "n.name" if "name" in node_cols else "NULL::text"

        rows = _qall(
            cur,
            """
            WITH ordered AS (
            SELECT
                u.node_id::uuid AS node_id,
                u.ord::int      AS ord
            FROM route_prod.routes r
            JOIN LATERAL unnest(r.stop_node_ids) WITH ORDINALITY AS u(node_id, ord)
                ON TRUE
            WHERE r.route_id = %s
            )
            SELECT
            o.node_id::text AS stop_id,
            o.ord::int      AS seq,

            NULL::text      AS name,

            p.lat           AS lat,
            p.lon           AS lon,

            row_to_json(p)  AS node_raw
            FROM ordered o
            LEFT JOIN route_work.relation_stop_prior p
            ON p.route_id = %s
            AND p.matched_stop_node_id = o.node_id
            ORDER BY o.ord ASC
            """,
            (route_id, route_id),
        )

        out: List[StopContext] = []
        for r in rows:
            out.append(
                StopContext(
                    stop_id=str(r.get("stop_id")),
                    seq=int(r["seq"]) if r.get("seq") is not None else None,
                    name=r.get("name"),
                    lat=float(r["lat"]) if r.get("lat") is not None else None,
                    lon=float(r["lon"]) if r.get("lon") is not None else None,
                    raw={"node": r.get("node_raw") or {}},
                )
            )
        return out

    # ------------------------------------------------------------
    # C) Last fallback: return only IDs + order
    # ------------------------------------------------------------
    rows = _qall(
        cur,
        """
        SELECT
          u.node_id::text AS stop_id,
          u.ord::int      AS seq
        FROM route_prod.routes r
        JOIN LATERAL unnest(r.stop_node_ids) WITH ORDINALITY AS u(node_id, ord)
          ON TRUE
        WHERE r.route_id = %s
        ORDER BY u.ord ASC
        """,
        (route_id,),
    )

    return [
        StopContext(
            stop_id=str(r.get("stop_id")),
            seq=int(r["seq"]) if r.get("seq") is not None else None,
            name=None,
            lat=None,
            lon=None,
            raw={},
        )
        for r in rows
    ]



def _load_route_stops_mapping_fallback(cur, route_id: str) -> List[StopContext]:
    """
    Fallback if stop_node_ids isn't available.
    Tries older phase-3 mapping tables.
    Returns [] if nothing exists.
    """
    mapping_candidates = [
        ("route_prod", "route_stops", "route_id", "stop_id", "seq"),
        ("route_prod", "route_stop_sequence", "route_id", "stop_id", "seq"),
        ("route_prod", "routes_stops", "route_id", "stop_id", "seq"),
        ("route_work", "route_stops", "route_id", "stop_id", "seq"),
        ("route_work", "route_stop_sequence", "route_id", "stop_id", "seq"),
    ]

    mapping = None
    for sch, tbl, rcol, scol, qcol in mapping_candidates:
        if _table_exists(cur, sch, tbl):
            cols = set(_columns(cur, sch, tbl))
            if rcol in cols and scol in cols:
                mapping = (sch, tbl, rcol, scol, qcol if qcol in cols else None)
                break

    if not mapping:
        return []

    sch, tbl, rcol, scol, qcol = mapping

    order_clause = f"ORDER BY m.{qcol} ASC NULLS LAST" if qcol else "ORDER BY m.stop_id"

    rows = _qall(
        cur,
        f"""
        SELECT
          m.{scol}::text AS stop_id,
          {('m.' + qcol) if qcol else 'NULL::int'} AS seq,
          row_to_json(m) AS mapping_raw
        FROM {sch}.{tbl} m
        WHERE m.{rcol} = %s
        {order_clause}
        """,
        (route_id,),
    )

    return [
        StopContext(
            stop_id=str(r.get("stop_id")),
            seq=int(r["seq"]) if r.get("seq") is not None else None,
            name=None,
            lat=None,
            lon=None,
            raw={"mapping": r.get("mapping_raw") or {}},
        )
        for r in rows
    ]
