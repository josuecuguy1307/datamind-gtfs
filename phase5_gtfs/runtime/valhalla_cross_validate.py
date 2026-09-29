"""Cross-validate Runtime Lab estimates against Valhalla free-flow bus times."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import psycopg2.extras
import requests

from phase5_gtfs.common.config import db_conn

VALHALLA_URL = "http://127.0.0.1:8003/route"
VALHALLA_TIMEOUT = 10


def _get_ordered_stops(
    route_id: str, direction_id: int, cur: Any
) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT n.node_id::text AS stop_id,
               n.lat, n.lon, sid.seq::int AS seq
        FROM route_prod.routes r
        JOIN LATERAL unnest(r.stop_node_ids) WITH ORDINALITY AS sid(node_id, seq) ON true
        JOIN node_prod.nodes n ON n.node_id = sid.node_id
        WHERE r.route_id::text = %s
          AND COALESCE(r.canonical_sequence_ready, FALSE) = TRUE
        ORDER BY sid.seq
        """,
        (route_id,),
    )
    stops = [dict(r) for r in (cur.fetchall() or [])]
    if direction_id == 1:
        stops = list(reversed(stops))
    return stops


def _get_leg_features(
    route_id: str, direction_id: int, cur: Any
) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT l.leg_idx, l.distance_m,
               COALESCE(l.offpeak_secs, 0)::float8 AS offpeak_secs,
               COALESCE(l.peak_secs, 0)::float8    AS peak_secs
        FROM gtfs_work.route_runtime_estimate_bindings b
        JOIN gtfs_work.runtime_route_leg_features l
          ON l.estimate_id = b.estimate_id
        WHERE b.route_id::text = %s AND b.direction_id = %s
        ORDER BY l.leg_idx
        """,
        (route_id, direction_id),
    )
    return [dict(r) for r in (cur.fetchall() or [])]


def _valhalla_leg_time(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> Optional[float]:
    """Query Valhalla for bus free-flow time between two points."""
    try:
        resp = requests.post(
            VALHALLA_URL,
            json={
                "locations": [
                    {"lat": lat1, "lon": lon1},
                    {"lat": lat2, "lon": lon2},
                ],
                "costing": "bus",
                "directions_options": {"units": "kilometers"},
            },
            timeout=VALHALLA_TIMEOUT,
        )
        if resp.status_code == 200:
            trip = resp.json().get("trip", {})
            return float(trip.get("summary", {}).get("time", 0))
    except (requests.RequestException, ValueError, KeyError):
        pass
    return None


def _interpret_ratio(ratio: Optional[float]) -> str:
    if ratio is None:
        return "NO_DATA"
    if ratio < 0.8:
        return "CATALOG_UNDERESTIMATES — catalog times shorter than Valhalla free-flow. Likely wrong."
    if ratio < 1.2:
        return "CATALOG_NEAR_FREEFLOW — barely above free-flow. May undercount stops/traffic."
    if ratio < 2.0:
        return "CATALOG_REASONABLE — 20-100% over free-flow for stops/traffic. Plausible."
    if ratio < 3.0:
        return "CATALOG_HIGH — 2-3x free-flow. Possible with heavy congestion."
    return "CATALOG_VERY_HIGH — >3x free-flow. Check for errors."


def cross_validate_against_valhalla(
    route_id: str,
    direction_id: int = 0,
    *,
    conn: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """
    Compare per-leg Runtime Lab estimate against Valhalla free-flow bus time.

    Returns per-leg comparison and overall calibration factor, or None if
    insufficient data.
    """
    own_conn = conn is None
    if own_conn:
        conn = db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            stops = _get_ordered_stops(route_id, direction_id, cur)
            legs = _get_leg_features(route_id, direction_id, cur)

        if len(stops) < 2 or not legs:
            return None

        comparisons: List[Dict[str, Any]] = []
        for i in range(min(len(stops) - 1, len(legs))):
            v_time = _valhalla_leg_time(
                stops[i]["lat"], stops[i]["lon"],
                stops[i + 1]["lat"], stops[i + 1]["lon"],
            )
            leg = legs[i]
            offpeak = float(leg.get("offpeak_secs", 0))
            peak = float(leg.get("peak_secs", 0))

            comparisons.append({
                "leg_index": i,
                "distance_m": round(float(leg.get("distance_m", 0)), 1),
                "valhalla_secs": round(v_time, 1) if v_time else None,
                "catalog_offpeak_secs": round(offpeak, 1),
                "catalog_peak_secs": round(peak, 1),
                "ratio_offpeak": round(offpeak / v_time, 2) if v_time and v_time > 0 else None,
                "ratio_peak": round(peak / v_time, 2) if v_time and v_time > 0 else None,
            })

        valid = [c for c in comparisons if c["ratio_offpeak"] is not None]
        avg_offpeak = (
            sum(c["ratio_offpeak"] for c in valid) / len(valid) if valid else None
        )
        avg_peak = (
            sum(c["ratio_peak"] for c in valid) / len(valid) if valid else None
        )

        return {
            "route_id": route_id,
            "direction_id": direction_id,
            "legs_compared": len(valid),
            "legs_total": len(comparisons),
            "avg_catalog_to_valhalla_offpeak": round(avg_offpeak, 2) if avg_offpeak else None,
            "avg_catalog_to_valhalla_peak": round(avg_peak, 2) if avg_peak else None,
            "interpretation": _interpret_ratio(avg_offpeak),
            "legs": comparisons,
        }
    finally:
        if own_conn:
            conn.close()
