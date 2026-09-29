from __future__ import annotations

from typing import Dict, Any, List, Tuple, Optional

from phase5_gtfs.common.config import db_conn
from phase5_gtfs.common.time_utils import hhmmss_to_seconds, seconds_to_hhmmss


def build_stop_times(
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
                trip_scope = ["t.export_run_id = %s::uuid", "t.route_id = %s"]
                trip_scope_params: List[Any] = [export_run_id, gtfs_route_id_txt]
                if dir_filter is not None:
                    trip_scope.append("t.direction_id = %s")
                    trip_scope_params.append(dir_filter)
                cur.execute(
                    f"""
                    DELETE FROM gtfs_work.gtfs_stop_times st
                    USING gtfs_work.gtfs_trips t
                    WHERE st.export_run_id = %s::uuid
                      AND t.export_run_id = st.export_run_id
                      AND t.trip_id = st.trip_id
                      AND {' AND '.join(trip_scope)}
                    """,
                    (export_run_id, *trip_scope_params),
                )
            else:
                cur.execute("DELETE FROM gtfs_work.gtfs_stop_times WHERE export_run_id = %s::uuid", (export_run_id,))

            trip_where = ["t.export_run_id = %s::uuid"]
            trip_params: List[Any] = [export_run_id]
            if route_id_txt:
                trip_where.append("t.route_id = %s")
                trip_params.append(gtfs_route_id_txt)
            if dir_filter is not None:
                trip_where.append("t.direction_id = %s")
                trip_params.append(dir_filter)

            cur.execute(
                """
                SELECT
                       t.trip_id,
                       t.route_id,
                       p.route_id::text AS source_route_id,
                       t.direction_id AS direction_id,
                       p.profile_id::text AS profile_id,
                       td.departure_time AS first_departure
                FROM gtfs_work.gtfs_trips t
                JOIN gtfs_work.route_schedule_profiles p
                  ON p.direction_id = t.direction_id
                 AND t.service_id = ('svc_' || LEFT(p.profile_id::text, 8))
                LEFT JOIN gtfs_work.trip_departures td
                  ON td.export_run_id = t.export_run_id
                 AND td.trip_id = t.trip_id
                WHERE """
                + " AND ".join(trip_where),
                tuple(trip_params),
            )
            trips = list(cur.fetchall() or [])

            cur.execute(
                """
                SELECT profile_id::text AS profile_id,
                       start_time::text AS start_time,
                       end_time::text AS end_time
                FROM gtfs_work.service_windows
                WHERE is_active = true
                  AND COALESCE(is_peak, false) = true
                """
            )
            peak_rows = list(cur.fetchall() or [])
            peak_windows: Dict[str, List[Tuple[int, int]]] = {}
            for r in peak_rows:
                pid = str(r.get("profile_id") or "")
                if not pid:
                    continue
                try:
                    s0 = hhmmss_to_seconds(str(r.get("start_time") or "00:00:00"))
                    s1 = hhmmss_to_seconds(str(r.get("end_time") or "00:00:00"))
                except Exception:
                    continue
                if s1 > s0:
                    peak_windows.setdefault(pid, []).append((int(s0), int(s1)))

            bind_where = ["1=1"]
            bind_params: List[Any] = []
            if route_id_txt:
                bind_where.append("b.route_id::text = %s")
                bind_params.append(route_id_txt)
            if dir_filter is not None:
                bind_where.append("b.direction_id = %s")
                bind_params.append(dir_filter)
            cur.execute(
                (
                    """
                    SELECT b.route_id::text AS route_id,
                           b.direction_id::int AS direction_id,
                           b.estimate_id::text AS estimate_id
                    FROM gtfs_work.route_runtime_estimate_bindings b
                    WHERE """
                    + " AND ".join(bind_where)
                ),
                tuple(bind_params),
            )
            binding_rows = list(cur.fetchall() or [])
            bound_estimate: Dict[Tuple[str, int], str] = {}
            for r in binding_rows:
                rid = str(r.get("route_id") or "")
                did = int(r.get("direction_id") or 0)
                eid = str(r.get("estimate_id") or "")
                if rid and eid:
                    bound_estimate[(rid, did)] = eid

            # estimate_id -> ordered leg timing rows (includes travel + dwell split when available)
            estimate_legs: Dict[str, List[Dict[str, int]]] = {}
            if bound_estimate:
                est_ids = sorted({eid for eid in bound_estimate.values() if eid})
                cur.execute(
                    """
                    SELECT l.estimate_id::text AS estimate_id,
                           l.leg_idx::int AS leg_idx,
                           COALESCE(l.offpeak_secs, 0)::float8 AS offpeak_secs,
                           COALESCE(l.peak_secs, 0)::float8 AS peak_secs,
                           l.attrs AS attrs
                    FROM gtfs_work.runtime_route_leg_features l
                    WHERE l.estimate_id = ANY(%s::uuid[])
                    ORDER BY l.estimate_id, l.leg_idx
                    """,
                    (est_ids,),
                )
                for r in (cur.fetchall() or []):
                    eid = str(r.get("estimate_id") or "")
                    if not eid:
                        continue
                    attrs = r.get("attrs") or {}
                    if not isinstance(attrs, dict):
                        attrs = {}
                    off_total = max(1, int(round(float(r.get("offpeak_secs") or 0.0))))
                    peak_total = max(1, int(round(float(r.get("peak_secs") or 0.0))))
                    off_dwell = max(0, int(round(float(attrs.get("dwell_offpeak_secs") or 0.0))))
                    peak_dwell = max(0, int(round(float(attrs.get("dwell_peak_secs") or 0.0))))
                    off_travel = int(round(float(attrs.get("travel_offpeak_secs") or max(1, off_total - off_dwell))))
                    peak_travel = int(round(float(attrs.get("travel_peak_secs") or max(1, peak_total - peak_dwell))))
                    off_travel = max(1, min(off_total, off_travel))
                    peak_travel = max(1, min(peak_total, peak_travel))
                    off_dwell = max(0, off_total - off_travel) if off_dwell <= 0 and off_total > off_travel else min(off_dwell, max(0, off_total - 1))
                    peak_dwell = max(0, peak_total - peak_travel) if peak_dwell <= 0 and peak_total > peak_travel else min(peak_dwell, max(0, peak_total - 1))
                    estimate_legs.setdefault(eid, []).append(
                        {
                            "off_total": off_total,
                            "peak_total": peak_total,
                            "off_travel": off_travel,
                            "peak_travel": peak_travel,
                            "off_dwell": off_dwell,
                            "peak_dwell": peak_dwell,
                        }
                    )

            # Pre-fetch profile runtime_secs for uniform fallback
            cur.execute(
                """
                SELECT profile_id::text, route_id::text, direction_id::int,
                       COALESCE(runtime_secs, 3600) AS runtime_secs,
                       COALESCE(dwell_secs, 20) AS dwell_secs
                FROM gtfs_work.route_schedule_profiles
                WHERE is_active = true
                """
            )
            profile_runtimes: Dict[str, Dict[str, int]] = {}
            for pr in (cur.fetchall() or []):
                pid = str(pr.get("profile_id") or "")
                if pid:
                    profile_runtimes[pid] = {
                        "runtime_secs": int(pr.get("runtime_secs") or 3600),
                        "dwell_secs": int(pr.get("dwell_secs") or 20),
                    }

            used_leg_runtime_trips = 0
            leg_binding_missing = 0
            leg_count_mismatch = 0
            fallback_uniform_trips = 0
            missing_binding_keys: set[tuple[str, int]] = set()
            mismatch_binding_keys: set[tuple[str, int, str, int, int]] = set()

            inserted = 0
            explicit_dwell_rows = 0
            for tr in trips:
                # tr["route_id"] is the GTFS route_id = service_route_id (shared across directions).
                # Pick the per-direction route in route_prod by (service_route_id, direction_id),
                # falling back to (route_id) for routes whose service_route_id == route_id.
                cur.execute(
                    """
                    SELECT n.node_id::text AS stop_id
                    FROM route_prod.routes r
                    JOIN LATERAL unnest(r.stop_node_ids) WITH ORDINALITY AS sid(node_id, seq) ON true
                    JOIN node_prod.nodes n ON n.node_id = sid.node_id
                    WHERE COALESCE(r.service_route_id::text, r.route_id::text) = %s
                      AND r.direction_id = %s
                      AND COALESCE(r.canonical_sequence_ready, FALSE) = TRUE
                      AND r.chosen_stop_sequence_candidate_id IS NOT NULL
                    ORDER BY sid.seq
                    """,
                    (tr["route_id"], int(tr.get("direction_id") or 0)),
                )
                stops = list(cur.fetchall() or [])
                if not stops:
                    continue
                # stops are loaded from r.stop_node_ids of THIS direction's chosen sequence —
                # already direction-correct. No reversal: d0 and d1 each have their own
                # chosen_stop_sequence_candidate stored in their natural order.

                first_dep = hhmmss_to_seconds(str(tr.get("first_departure") or "06:00:00"))
                rid = str(tr.get("source_route_id") or tr.get("route_id") or "")
                did = int(tr.get("direction_id") or 0)
                pid = str(tr.get("profile_id") or "")
                dep_s = int(first_dep)

                is_peak_trip = False
                for s0, s1 in (peak_windows.get(pid) or []):
                    if dep_s >= s0 and dep_s < s1:
                        is_peak_trip = True
                        break

                legs_for_trip: List[Dict[str, int]] = []
                est_id = bound_estimate.get((rid, did))
                if est_id:
                    pair_list = [dict(x) for x in (estimate_legs.get(est_id) or [])]
                    if int(did) == 1 and pair_list:
                        pair_list = list(reversed(pair_list))
                    if pair_list and len(pair_list) == max(len(stops) - 1, 0):
                        if is_peak_trip:
                            legs_for_trip = [
                                {
                                    "travel": int(p.get("peak_travel") or 1),
                                    "dwell": int(p.get("peak_dwell") or 0),
                                    "total": int(p.get("peak_total") or 1),
                                }
                                for p in pair_list
                            ]
                        else:
                            legs_for_trip = [
                                {
                                    "travel": int(p.get("off_travel") or 1),
                                    "dwell": int(p.get("off_dwell") or 0),
                                    "total": int(p.get("off_total") or 1),
                                }
                                for p in pair_list
                            ]
                    else:
                        leg_count_mismatch += 1
                        mismatch_binding_keys.add(
                            (
                                rid,
                                did,
                                str(est_id),
                                int(len(pair_list)),
                                int(max(len(stops) - 1, 0)),
                            )
                        )
                else:
                    leg_binding_missing += 1
                    missing_binding_keys.add((rid, did))

                if not legs_for_trip:
                    # Uniform distribution fallback using profile runtime_secs
                    n_legs = max(len(stops) - 1, 1)
                    pr = profile_runtimes.get(pid) or {}
                    total_runtime = int(pr.get("runtime_secs") or 3600)
                    default_dwell = int(pr.get("dwell_secs") or 20)
                    total_per_leg = max(1, total_runtime // n_legs)
                    leg_dwell = min(default_dwell, max(0, total_per_leg - 1))
                    leg_travel = max(1, total_per_leg - leg_dwell)
                    legs_for_trip = [
                        {"travel": leg_travel, "dwell": leg_dwell, "total": total_per_leg}
                    ] * n_legs
                    fallback_uniform_trips += 1

                t0 = first_dep
                current_dep_time = int(t0)
                for i, s in enumerate(stops, start=1):
                    if i == 1:
                        arr = int(current_dep_time)
                        dep = int(current_dep_time)
                    else:
                        prev_leg = dict(legs_for_trip[i - 2])
                        prev_total = max(1, int(prev_leg.get("total") or 1))
                        prev_travel = max(1, min(prev_total, int(prev_leg.get("travel") or prev_total)))
                        prev_dwell = max(0, min(max(0, prev_total - prev_travel), int(prev_leg.get("dwell") or 0)))
                        arr = int(current_dep_time) + prev_travel
                        dep = int(arr) + prev_dwell
                    current_dep_time = int(dep)
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_stop_times (
                          export_run_id, trip_id, arrival_time, departure_time, stop_id, stop_sequence, timepoint
                        ) VALUES (%s::uuid,%s,%s,%s,%s,%s,1)
                        ON CONFLICT (export_run_id, trip_id, stop_sequence) DO UPDATE SET
                          arrival_time = EXCLUDED.arrival_time,
                          departure_time = EXCLUDED.departure_time,
                          stop_id = EXCLUDED.stop_id
                        """,
                        (
                            export_run_id,
                            tr["trip_id"],
                            seconds_to_hhmmss(arr),
                            seconds_to_hhmmss(dep),
                            s["stop_id"],
                            i,
                        ),
                    )
                    inserted += 1
                    if int(dep) > int(arr):
                        explicit_dwell_rows += 1

                used_leg_runtime_trips += 1

            # Note: leg_binding_missing/leg_count_mismatch are now handled by
            # the uniform fallback above. No longer a fatal error.

    return {
        "stop_times": inserted,
        "used_leg_runtime_trips": int(used_leg_runtime_trips),
        "explicit_dwell_rows": int(explicit_dwell_rows),
        "fallback_uniform_trips": int(fallback_uniform_trips),
        "leg_binding_missing": int(leg_binding_missing),
        "leg_count_mismatch": int(leg_count_mismatch),
    }
