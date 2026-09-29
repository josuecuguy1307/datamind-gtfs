from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

from phase5_gtfs.common.config import db_conn


_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_columns(columns: Sequence[str] | None) -> List[str]:
    cols = [str(c).strip() for c in (columns or []) if str(c).strip()]
    if not cols:
        cols = ["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"]
    out: List[str] = []
    for c in cols:
        if not _SAFE_IDENT.match(c):
            raise ValueError(f"Unsafe column name: {c}")
        out.append(c)
    return out


def _normalize_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{k: ("" if v is None else v) for k, v in dict(row).items()} for row in (rows or [])]


def _served_stop_exists_sql() -> str:
    return """
        EXISTS (
            SELECT 1
            FROM gtfs_work.gtfs_stop_times st
            JOIN gtfs_work.gtfs_trips t
              ON t.export_run_id = st.export_run_id
             AND t.trip_id = st.trip_id
            JOIN gtfs_work.gtfs_routes r
              ON r.export_run_id = t.export_run_id
             AND r.route_id = t.route_id
            WHERE st.export_run_id = s.export_run_id
              AND st.stop_id = s.stop_id
        )
    """


def list_served_stops(
    export_run_id: str,
    *,
    columns: Sequence[str] | None = None,
    limit: int | None = 5000,
) -> List[Dict[str, Any]]:
    cols = _safe_columns(columns)
    select_list = ", ".join([f"s.{c}" for c in cols])
    sql = f"""
        SELECT {select_list}
        FROM gtfs_work.gtfs_stops s
        WHERE s.export_run_id = %s
          AND {_served_stop_exists_sql()}
        ORDER BY s.stop_name, s.stop_id
    """
    params: List[Any] = [str(export_run_id)]
    if limit is not None and int(limit) > 0:
        sql += " LIMIT %s"
        params.append(int(limit))
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return _normalize_rows(list(cur.fetchall() or []))


def list_orphan_stops(
    export_run_id: str,
    *,
    columns: Sequence[str] | None = None,
    limit: int | None = 5000,
) -> List[Dict[str, Any]]:
    cols = _safe_columns(columns)
    select_list = ", ".join([f"s.{c}" for c in cols])
    sql = f"""
        SELECT {select_list}
        FROM gtfs_work.gtfs_stops s
        WHERE s.export_run_id = %s
          AND NOT {_served_stop_exists_sql()}
        ORDER BY s.stop_name, s.stop_id
    """
    params: List[Any] = [str(export_run_id)]
    if limit is not None and int(limit) > 0:
        sql += " LIMIT %s"
        params.append(int(limit))
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return _normalize_rows(list(cur.fetchall() or []))


def get_stop_coverage_metrics(
    export_run_id: str,
    *,
    include_duplicate_metrics: bool = True,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "export_run_id": str(export_run_id),
        "total_stops": 0,
        "served_stops_count": 0,
        "orphan_stops_count": 0,
        "orphan_stop_pct": 0.0,
    }
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH served_stop_ids AS (
                    SELECT DISTINCT st.stop_id
                    FROM gtfs_work.gtfs_stop_times st
                    JOIN gtfs_work.gtfs_trips t
                      ON t.export_run_id = st.export_run_id
                     AND t.trip_id = st.trip_id
                    JOIN gtfs_work.gtfs_routes r
                      ON r.export_run_id = t.export_run_id
                     AND r.route_id = t.route_id
                    WHERE st.export_run_id = %s
                ),
                totals AS (
                    SELECT COUNT(DISTINCT s.stop_id)::int AS total_stops
                    FROM gtfs_work.gtfs_stops s
                    WHERE s.export_run_id = %s
                ),
                served AS (
                    SELECT COUNT(*)::int AS served_stops_count
                    FROM served_stop_ids
                )
                SELECT
                    t.total_stops,
                    s.served_stops_count,
                    GREATEST(t.total_stops - s.served_stops_count, 0)::int AS orphan_stops_count,
                    CASE
                        WHEN t.total_stops > 0
                            THEN ROUND(((t.total_stops - s.served_stops_count)::numeric * 100.0) / t.total_stops, 2)
                        ELSE 0
                    END AS orphan_stop_pct
                FROM totals t
                CROSS JOIN served s
                """,
                (str(export_run_id), str(export_run_id)),
            )
            row = dict(cur.fetchone() or {})
            out.update(
                {
                    "total_stops": int(row.get("total_stops") or 0),
                    "served_stops_count": int(row.get("served_stops_count") or 0),
                    "orphan_stops_count": int(row.get("orphan_stops_count") or 0),
                    "orphan_stop_pct": float(row.get("orphan_stop_pct") or 0.0),
                }
            )

            if include_duplicate_metrics:
                cur.execute(
                    """
                    SELECT
                        COALESCE(NULLIF(TRIM(stop_name), ''), '(blank)')::text AS stop_name,
                        COUNT(*)::int AS count
                    FROM gtfs_work.gtfs_stops
                    WHERE export_run_id = %s
                    GROUP BY 1
                    HAVING COUNT(*) > 1
                    ORDER BY COUNT(*) DESC, 1
                    LIMIT 25
                    """,
                    (str(export_run_id),),
                )
                out["duplicate_stop_name_counts"] = [dict(r) for r in (cur.fetchall() or [])]

                cur.execute(
                    """
                    SELECT
                        ROUND((stop_lat)::numeric, 6)::float8 AS stop_lat_6dp,
                        ROUND((stop_lon)::numeric, 6)::float8 AS stop_lon_6dp,
                        COUNT(*)::int AS count,
                        ARRAY_AGG(stop_id::text ORDER BY stop_id)::text[] AS stop_ids
                    FROM gtfs_work.gtfs_stops
                    WHERE export_run_id = %s
                      AND stop_lat IS NOT NULL
                      AND stop_lon IS NOT NULL
                    GROUP BY 1, 2
                    HAVING COUNT(*) > 1
                    ORDER BY COUNT(*) DESC, 1, 2
                    LIMIT 25
                    """,
                    (str(export_run_id),),
                )
                clusters: List[Dict[str, Any]] = []
                for r in (cur.fetchall() or []):
                    rowd = dict(r)
                    ids = list(rowd.get("stop_ids") or [])
                    clusters.append(
                        {
                            "stop_lat_6dp": rowd.get("stop_lat_6dp"),
                            "stop_lon_6dp": rowd.get("stop_lon_6dp"),
                            "count": int(rowd.get("count") or 0),
                            "sample_stop_ids": ids[:10],
                        }
                    )
                out["duplicate_coordinate_clusters"] = clusters

    return out

