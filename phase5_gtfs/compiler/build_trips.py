from __future__ import annotations

from typing import Dict, Any, Optional

from phase5_gtfs.common.config import db_conn
from phase5_gtfs.common.time_utils import hhmmss_to_seconds, seconds_to_hhmmss


def build_trips_and_frequencies(
    export_run_id: str,
    *,
    route_id: Optional[str] = None,
    direction_id: Optional[int] = None,
) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            route_id_txt = str(route_id or "").strip()
            dir_filter = None if direction_id is None else int(direction_id)
            gtfs_route_id_txt = route_id_txt
            if route_id_txt:
                cur.execute(
                    """
                    SELECT COALESCE(r.service_route_id::text, r.route_id::text) AS gtfs_route_id
                    FROM route_prod.routes r
                    WHERE r.route_id::text = %s
                    LIMIT 1
                    """,
                    (route_id_txt,),
                )
                row_gtfs = dict(cur.fetchone() or {})
                gtfs_route_id_txt = str(row_gtfs.get("gtfs_route_id") or route_id_txt)
            if route_id_txt:
                if dir_filter is None:
                    trip_scope_sql = "export_run_id = %s::uuid AND route_id = %s"
                    trip_scope_sql_t = "t.export_run_id = %s::uuid AND t.route_id = %s"
                    trip_scope_params = (export_run_id, gtfs_route_id_txt)
                else:
                    trip_scope_sql = "export_run_id = %s::uuid AND route_id = %s AND direction_id = %s"
                    trip_scope_sql_t = "t.export_run_id = %s::uuid AND t.route_id = %s AND t.direction_id = %s"
                    trip_scope_params = (export_run_id, gtfs_route_id_txt, dir_filter)
                cur.execute(
                    f"""
                    DELETE FROM gtfs_work.gtfs_frequencies f
                    USING gtfs_work.gtfs_trips t
                    WHERE f.export_run_id = %s::uuid
                      AND t.export_run_id = f.export_run_id
                      AND t.trip_id = f.trip_id
                      AND {trip_scope_sql_t}
                    """,
                    (export_run_id, *trip_scope_params),
                )
                cur.execute(
                    f"""
                    DELETE FROM gtfs_work.trip_departures d
                    USING gtfs_work.gtfs_trips t
                    WHERE d.export_run_id = %s::uuid
                      AND t.export_run_id = d.export_run_id
                      AND t.trip_id = d.trip_id
                      AND {trip_scope_sql_t}
                    """,
                    (export_run_id, *trip_scope_params),
                )
                cur.execute(
                    f"DELETE FROM gtfs_work.gtfs_trips WHERE {trip_scope_sql}",
                    trip_scope_params,
                )
            else:
                cur.execute("DELETE FROM gtfs_work.gtfs_trips WHERE export_run_id = %s::uuid", (export_run_id,))
                cur.execute("DELETE FROM gtfs_work.gtfs_frequencies WHERE export_run_id = %s::uuid", (export_run_id,))
                cur.execute("DELETE FROM gtfs_work.trip_departures WHERE export_run_id = %s::uuid", (export_run_id,))

            where_clauses = ["p.is_active = true", "w.is_active = true"]
            params = []
            if route_id_txt:
                where_clauses.append("p.route_id::text = %s")
                params.append(route_id_txt)
            if dir_filter is not None:
                where_clauses.append("p.direction_id = %s")
                params.append(dir_filter)

            cur.execute(
                """
                SELECT p.profile_id, p.route_id, p.direction_id, p.service_name,
                       COALESCE(p.n_blocks, 1) AS n_blocks,
                       COALESCE(rsrc.service_route_id::text, p.route_id::text) AS gtfs_route_id,
                       COALESCE(rsrc.service_route_id::text, p.route_id::text) AS shape_base_id,
                       v.route_name,
                       w.start_time, w.end_time, w.headway_secs, w.exact_departures
                FROM gtfs_work.route_schedule_profiles p
                JOIN gtfs_work.service_windows w ON w.profile_id = p.profile_id
                JOIN route_prod.routes rsrc
                  ON rsrc.route_id = p.route_id
                 AND COALESCE(rsrc.canonical_sequence_ready, FALSE) = TRUE
                 AND rsrc.chosen_stop_sequence_candidate_id IS NOT NULL
                LEFT JOIN gtfs_work.v_route_inputs v ON v.route_id = p.route_id
                WHERE %s
                ORDER BY p.route_id, p.direction_id, w.start_time
                """ % " AND ".join(where_clauses),
                tuple(params),
            )
            rows = list(cur.fetchall() or [])
            cur.execute(
                """
                SELECT
                  r.route_id::text AS route_id,
                  COALESCE(n_first.name, n_first.chosen_tags->>'name', 'Start') AS first_name,
                  COALESCE(n_last.name, n_last.chosen_tags->>'name', 'End') AS last_name
                FROM route_prod.routes r
                LEFT JOIN LATERAL (
                  SELECT sid.node_id
                  FROM unnest(r.stop_node_ids) WITH ORDINALITY AS sid(node_id, seq)
                  ORDER BY sid.seq ASC
                  LIMIT 1
                ) first_stop ON true
                LEFT JOIN LATERAL (
                  SELECT sid.node_id
                  FROM unnest(r.stop_node_ids) WITH ORDINALITY AS sid(node_id, seq)
                  ORDER BY sid.seq DESC
                  LIMIT 1
                ) last_stop ON true
                LEFT JOIN node_prod.nodes n_first ON n_first.node_id = first_stop.node_id
                LEFT JOIN node_prod.nodes n_last ON n_last.node_id = last_stop.node_id
                WHERE COALESCE(r.canonical_sequence_ready, FALSE) = TRUE
                  AND r.chosen_stop_sequence_candidate_id IS NOT NULL
                """
            )
            endpoint_rows = list(cur.fetchall() or [])
            endpoints = {
                str(x["route_id"]): (str(x.get("first_name") or "Start"), str(x.get("last_name") or "End"))
                for x in endpoint_rows
                if x.get("route_id")
            }

            n_trips = 0
            n_freq = 0
            trip_counter: Dict[tuple[str, int], int] = {}
            for r in rows:
                route_txt = str(r["route_id"])
                gtfs_route_txt = str(r.get("gtfs_route_id") or route_txt)
                service_id = f"svc_{str(r['profile_id'])[:8]}"
                dir_id = int(r["direction_id"])
                shape_base_id = str(r.get("shape_base_id") or route_txt)
                shape_id = f"shape_{shape_base_id}_d{dir_id}"
                base_name = str(r.get("route_name") or f"Route {route_txt[:8]}")
                first_name, last_name = endpoints.get(route_txt, ("Start", "End"))
                origin_name = first_name if dir_id == 0 else last_name
                dest_name = last_name if dir_id == 0 else first_name
                headsign = f"{origin_name} -> {dest_name} | {base_name} | d{dir_id}"
                n_blocks = max(1, int(r.get("n_blocks") or 1))

                departures = []
                if r.get("exact_departures"):
                    departures = [str(x) for x in (r.get("exact_departures") or []) if x]
                elif r.get("headway_secs"):
                    start_s = hhmmss_to_seconds(str(r["start_time"]))
                    end_s = hhmmss_to_seconds(str(r["end_time"]))
                    h = int(r["headway_secs"])
                    t = start_s
                    while t <= end_s:
                        departures.append(seconds_to_hhmmss(t))
                        t += h

                key = (route_txt, dir_id)
                seq = trip_counter.get(key, 0)
                first_trip_id_for_window: Optional[str] = None
                for dep in departures:
                    seq += 1
                    trip_id = f"trip_{route_txt[:8]}_{dir_id}_{seq:04d}"
                    if first_trip_id_for_window is None:
                        first_trip_id_for_window = trip_id
                    block_id = f"blk_{route_txt[:8]}_{dir_id}_{((seq - 1) % n_blocks) + 1:02d}"
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_trips (
                          export_run_id, route_id, service_id, trip_id, trip_headsign, direction_id, shape_id, block_id
                        ) VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (export_run_id, trip_id) DO UPDATE SET
                          route_id = EXCLUDED.route_id,
                          service_id = EXCLUDED.service_id,
                          trip_headsign = EXCLUDED.trip_headsign,
                          direction_id = EXCLUDED.direction_id,
                          shape_id = EXCLUDED.shape_id,
                          block_id = EXCLUDED.block_id
                        """,
                        (
                            export_run_id,
                            gtfs_route_txt,
                            service_id,
                            trip_id,
                            headsign,
                            dir_id,
                            shape_id,
                            block_id,
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.trip_departures (export_run_id, trip_id, departure_time)
                        VALUES (%s::uuid,%s,%s)
                        ON CONFLICT (export_run_id, trip_id) DO UPDATE SET
                          departure_time = EXCLUDED.departure_time
                        """,
                        (export_run_id, trip_id, dep),
                    )
                    n_trips += 1
                trip_counter[key] = seq

                if r.get("headway_secs") and first_trip_id_for_window:
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_frequencies (
                          export_run_id, trip_id, start_time, end_time, headway_secs, exact_times
                        ) VALUES (%s::uuid,%s,%s,%s,%s,0)
                        ON CONFLICT (export_run_id, trip_id, start_time) DO UPDATE SET
                          end_time = EXCLUDED.end_time,
                          headway_secs = EXCLUDED.headway_secs
                        """,
                        (
                            export_run_id,
                            first_trip_id_for_window,
                            str(r["start_time"]),
                            str(r["end_time"]),
                            int(r["headway_secs"]),
                        ),
                    )
                    n_freq += 1

    return {"trips": n_trips, "frequencies": n_freq}
