"""
Runtime Lab v2 — Valhalla-powered with stochastic variance.

Replaces the old factor-based model with:
1. Valhalla :8004 for road travel time (time-of-day aware)
2. Custom dwell model for bus stop times
3. Variance model for p05/p50/p95 distributions
4. Seasonal + directional adjustments

Usage::

    from phase5_gtfs.runtime_lab.runtime_lab_v2 import RuntimeLabV2
    from phase5_gtfs.common.config import db_conn

    with db_conn() as conn:
        lab = RuntimeLabV2(conn)
        lab.load_research_data("/path/to/06b_research.json")
        result = lab.estimate_route("route-uuid", 0, "peak_am")
        # result['total_p50_secs'], result['legs'], ...
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
import psycopg2.extras

from .dwell_model import DwellModel
from .variance_model import VarianceModel
from .valhalla_integration import (
    VALHALLA_RUNTIME,
    PERIOD_DATETIME,
    valhalla_route_time,
)

VALHALLA_RUNTIME_URL = VALHALLA_RUNTIME
VALHALLA_TIMEOUT = 30


class RuntimeLabV2:
    """
    Main entry point: orchestrate Valhalla queries + dwell + variance
    to produce per-leg runtime distributions.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.dwell = DwellModel(conn)
        self.variance = VarianceModel()
        self._valhalla_available = self._check_valhalla()
        self._research_data: Optional[dict] = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _check_valhalla(self) -> bool:
        try:
            resp = requests.get(f"{VALHALLA_RUNTIME_URL}/status", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    def load_research_data(self, research_json_path: str) -> None:
        """Load Deep Research 06b data into all sub-models."""
        with open(research_json_path) as f:
            data = json.load(f)
        self.dwell.load_research_overrides(data)
        self.variance.load_research_data(data)
        self._research_data = data

    def load_research_dict(self, data: dict) -> None:
        """Load research data from an already-parsed dict."""
        self.dwell.load_research_overrides(data)
        self.variance.load_research_data(data)
        self._research_data = data

    # ------------------------------------------------------------------
    # Main estimation
    # ------------------------------------------------------------------

    def estimate_route(
        self,
        route_id: str,
        direction_id: int,
        time_period: str = "off_peak",
        day_type: str = "weekday",
    ) -> Optional[Dict[str, Any]]:
        """
        Full runtime estimate for a route-direction.

        Returns
        -------
        dict with:
          - ``total_p05_secs``, ``total_p50_secs``, ``total_p95_secs``
          - ``total_cv``, ``confidence``
          - ``legs``: list of per-leg dicts
          - ``source``, ``method``, ``n_legs``
        """
        route = self._get_route_data(route_id, direction_id)
        if not route or len(route["stops"]) < 2:
            return None

        research_runtime = self._find_research_runtime(
            route["route_code"], direction_id
        )

        if self._valhalla_available:
            legs = self._estimate_via_valhalla(
                route, time_period, research_runtime, day_type
            )
            method = "valhalla_v2"
        else:
            legs = self._estimate_via_fallback(
                route, time_period, research_runtime, day_type
            )
            method = "research_fallback" if research_runtime else "default_fallback"

        if not legs:
            return None

        # Totals
        total_travel_p50 = sum(l["travel_p50"] for l in legs)
        total_dwell = sum(l["dwell_mean"] for l in legs)
        total_p50 = total_travel_p50 + total_dwell

        total_var = sum(
            l.get("travel_std", l["travel_p50"] * 0.20) ** 2 + l["dwell_std"] ** 2
            for l in legs
        )
        total_std = math.sqrt(total_var)

        # Cumulative p50 for stop_times.txt
        cumulative = 0.0
        for leg in legs:
            cumulative += leg["travel_p50"] + leg["dwell_mean"]
            leg["cumulative_p50"] = round(cumulative)

        confidence = self._compute_confidence(method, research_runtime)

        return {
            "total_p05_secs": round(max(0, total_p50 - 1.645 * total_std)),
            "total_p50_secs": round(total_p50),
            "total_p95_secs": round(total_p50 + 1.645 * total_std),
            "total_cv": round(total_std / total_p50, 3) if total_p50 > 0 else 0,
            "confidence": confidence,
            "legs": legs,
            "source": "valhalla_runtime" if self._valhalla_available else "fallback",
            "method": method,
            "n_legs": len(legs),
            "route_id": route_id,
            "direction_id": direction_id,
            "time_period": time_period,
        }

    # ------------------------------------------------------------------
    # Valhalla-based estimation
    # ------------------------------------------------------------------

    def _estimate_via_valhalla(
        self,
        route: dict,
        time_period: str,
        research_runtime: Optional[dict],
        day_type: str,
    ) -> List[Dict[str, Any]]:
        """Query Valhalla for per-leg timing, add dwell, apply variance."""
        stops = route["stops"]

        # Query Valhalla through all stops
        valhalla_resp = valhalla_route_time(
            [{"lat": s["lat"], "lon": s["lon"]} for s in stops],
            time_period=time_period,
        )

        if not valhalla_resp:
            return self._estimate_via_fallback(
                route, time_period, research_runtime, day_type
            )

        # Extract per-leg times from Valhalla response
        trip = valhalla_resp.get("trip", {})
        trip_legs = trip.get("legs", [])

        # Valhalla returns one "leg" per break-type location pair.
        # Since only first/last are "break", we get one big leg.
        # Extract maneuver-level timing and map to stop pairs.
        maneuvers = trip_legs[0].get("maneuvers", []) if trip_legs else []

        # Build stop-to-stop leg times
        # Strategy: use Valhalla's total time, distributed proportionally
        # to straight-line distance between consecutive stops if maneuver
        # mapping is impractical.
        valhalla_total_secs = trip.get("summary", {}).get("time", 0)

        leg_distances = []
        for i in range(1, len(stops)):
            d = _haversine_m(
                stops[i - 1]["lat"], stops[i - 1]["lon"],
                stops[i]["lat"], stops[i]["lon"],
            )
            leg_distances.append(max(1.0, d))

        total_dist = sum(leg_distances)

        legs: List[Dict[str, Any]] = []
        leg_times: List[float] = []
        leg_road_classes: List[str] = []

        for i in range(1, len(stops)):
            stop = stops[i]
            prev_stop = stops[i - 1]

            # Proportional time allocation
            frac = leg_distances[i - 1] / total_dist if total_dist > 0 else 1 / max(1, len(stops) - 1)
            valhalla_time = valhalla_total_secs * frac

            # Dwell
            is_terminus = i == len(stops) - 1
            dwell = self.dwell.estimate_dwell(
                stop_node_id=stop["node_id"],
                stop_lat=stop["lat"],
                stop_lon=stop["lon"],
                area_type=stop.get("area_type", "suburban"),
                is_terminus=is_terminus,
                route_code=route["route_code"],
                time_period=time_period,
                day_type=day_type,
            )

            leg_times.append(valhalla_time)
            leg_road_classes.append("secondary")  # default; refined if trace_attributes used

            legs.append({
                "stop_id": stop["node_id"],
                "stop_name": stop["name"],
                "travel_p50": round(valhalla_time, 1),
                "travel_std": round(valhalla_time * 0.20, 1),
                "dwell_mean": dwell["mean_secs"],
                "dwell_std": dwell["std_secs"],
                "dwell_source": dwell["source"],
            })

        # Apply variance model with research data
        if research_runtime:
            period_rt = research_runtime.get(time_period) or research_runtime.get("off_peak")
            if period_rt:
                route_dist = self.variance.fit_route_distribution(period_rt)
                variance_legs = self.variance.distribute_variance_to_legs(
                    route_dist, leg_times, leg_road_classes,
                    time_period, route["route_code"],
                )
                for j, vl in enumerate(variance_legs):
                    legs[j]["travel_p05"] = vl["p05_secs"]
                    legs[j]["travel_p50"] = vl["p50_secs"]
                    legs[j]["travel_p95"] = vl["p95_secs"]
                    legs[j]["travel_std"] = vl["std_secs"]
                    legs[j]["travel_cv"] = vl["cv"]

                # Recalibrate: scale Valhalla times so total matches research p50
                research_total = route_dist["p50_secs"]
                valhalla_travel_total = sum(l["travel_p50"] for l in legs)
                if valhalla_travel_total > 0 and research_total > 0:
                    scale = research_total / valhalla_travel_total
                    # Only recalibrate if divergence is significant (>15%)
                    if abs(scale - 1.0) > 0.15:
                        for l in legs:
                            l["travel_p05"] = round(l.get("travel_p05", l["travel_p50"] * 0.67) * scale, 1)
                            l["travel_p50"] = round(l["travel_p50"] * scale, 1)
                            l["travel_p95"] = round(l.get("travel_p95", l["travel_p50"] * 1.33) * scale, 1)
                            l["travel_std"] = round(l.get("travel_std", l["travel_p50"] * 0.2) * scale, 1)

        if not research_runtime:
            # No research data — Valhalla times with default 20% CV
            for leg in legs:
                cv = 0.20
                leg["travel_p05"] = round(leg["travel_p50"] * (1 - 1.645 * cv), 1)
                leg["travel_p95"] = round(leg["travel_p50"] * (1 + 1.645 * cv), 1)
                leg["travel_cv"] = cv

        return legs

    # ------------------------------------------------------------------
    # Fallback estimation (no Valhalla)
    # ------------------------------------------------------------------

    def _estimate_via_fallback(
        self,
        route: dict,
        time_period: str,
        research_runtime: Optional[dict],
        day_type: str,
    ) -> List[Dict[str, Any]]:
        """
        Fallback when Valhalla is unavailable.

        Uses research runtimes distributed by straight-line distance,
        or a default 20 km/h commercial speed.
        """
        stops = route["stops"]

        # Compute per-leg distances
        leg_distances: List[float] = []
        for i in range(1, len(stops)):
            d = _haversine_m(
                stops[i - 1]["lat"], stops[i - 1]["lon"],
                stops[i]["lat"], stops[i]["lon"],
            )
            leg_distances.append(max(1.0, d))

        total_dist = sum(leg_distances)

        # Get total travel time
        if research_runtime:
            period_rt = research_runtime.get(time_period) or research_runtime.get("off_peak")
            if period_rt:
                total_travel_secs = period_rt.get("typical_min", 45) * 60
            else:
                total_travel_secs = (total_dist / 1000) / 20 * 3600  # 20 km/h default
        else:
            total_travel_secs = (total_dist / 1000) / 20 * 3600

        legs: List[Dict[str, Any]] = []
        leg_times: List[float] = []
        leg_road_classes: List[str] = []

        for i in range(1, len(stops)):
            stop = stops[i]
            frac = leg_distances[i - 1] / total_dist if total_dist > 0 else 1 / max(1, len(stops) - 1)
            travel_secs = total_travel_secs * frac

            is_terminus = i == len(stops) - 1
            dwell = self.dwell.estimate_dwell(
                stop_node_id=stop["node_id"],
                stop_lat=stop["lat"],
                stop_lon=stop["lon"],
                area_type=stop.get("area_type", "suburban"),
                is_terminus=is_terminus,
                route_code=route["route_code"],
                time_period=time_period,
                day_type=day_type,
            )

            leg_times.append(travel_secs)
            leg_road_classes.append("secondary")

            legs.append({
                "stop_id": stop["node_id"],
                "stop_name": stop["name"],
                "travel_p50": round(travel_secs, 1),
                "travel_std": round(travel_secs * 0.25, 1),
                "dwell_mean": dwell["mean_secs"],
                "dwell_std": dwell["std_secs"],
                "dwell_source": dwell["source"],
            })

        # Apply variance model
        if research_runtime:
            period_rt = research_runtime.get(time_period) or research_runtime.get("off_peak")
            if period_rt:
                route_dist = self.variance.fit_route_distribution(period_rt)
                variance_legs = self.variance.distribute_variance_to_legs(
                    route_dist, leg_times, leg_road_classes,
                    time_period, route["route_code"],
                )
                for j, vl in enumerate(variance_legs):
                    legs[j]["travel_p05"] = vl["p05_secs"]
                    legs[j]["travel_p50"] = vl["p50_secs"]
                    legs[j]["travel_p95"] = vl["p95_secs"]
                    legs[j]["travel_std"] = vl["std_secs"]
                    legs[j]["travel_cv"] = vl["cv"]
        else:
            for leg in legs:
                cv = 0.25
                leg["travel_p05"] = round(leg["travel_p50"] * (1 - 1.645 * cv), 1)
                leg["travel_p95"] = round(leg["travel_p50"] * (1 + 1.645 * cv), 1)
                leg["travel_cv"] = cv

        return legs

    # ------------------------------------------------------------------
    # Persist estimate into existing schema
    # ------------------------------------------------------------------

    def persist_estimate(self, result: dict) -> str:
        """
        Write a v2 estimate into ``gtfs_work.runtime_route_estimates`` and
        ``runtime_route_leg_features`` so that the existing GTFS compiler
        (``build_stop_times``) can consume it unchanged.

        Returns the new ``estimate_id``.
        """
        estimate_id = str(uuid.uuid4())
        route_id = result["route_id"]
        direction_id = result["direction_id"]

        with self.conn.cursor() as cur:
            # Route-level estimate
            metrics = {
                "runtime_offpeak_secs": result["total_p50_secs"],
                "runtime_peak_secs": result["total_p50_secs"],  # refined below
                "n_legs": result["n_legs"],
                "method": result["method"],
                "confidence": result["confidence"],
                "total_p05_secs": result["total_p05_secs"],
                "total_p95_secs": result["total_p95_secs"],
                "total_cv": result["total_cv"],
            }

            cur.execute(
                """
                INSERT INTO gtfs_work.runtime_route_estimates
                    (estimate_id, route_id, direction_id, metrics, estimated_at)
                VALUES (%s::uuid, %s::uuid, %s, %s::jsonb, NOW())
                """,
                (
                    estimate_id,
                    route_id,
                    direction_id,
                    json.dumps(metrics),
                ),
            )

            # Per-leg features (compatible with build_stop_times)
            for idx, leg in enumerate(result["legs"]):
                travel_p50 = leg["travel_p50"]
                dwell = leg["dwell_mean"]
                total = round(travel_p50 + dwell)

                attrs = {
                    "dwell_offpeak_secs": round(dwell, 1),
                    "dwell_peak_secs": round(dwell, 1),
                    "travel_offpeak_secs": round(travel_p50, 1),
                    "travel_peak_secs": round(travel_p50, 1),
                    "travel_p05_secs": leg.get("travel_p05", travel_p50 * 0.67),
                    "travel_p95_secs": leg.get("travel_p95", travel_p50 * 1.33),
                    "travel_cv": leg.get("travel_cv", 0.20),
                    "dwell_source": leg.get("dwell_source", "default"),
                    "method": result["method"],
                }

                # Compute distance_m if coordinates available
                distance_m = 0
                if idx > 0 and "stop_lat" in leg:
                    pass  # computed at query time if needed

                cur.execute(
                    """
                    INSERT INTO gtfs_work.runtime_route_leg_features
                        (estimate_id, leg_idx, distance_m, offpeak_secs, peak_secs, attrs)
                    VALUES (%s::uuid, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        estimate_id,
                        idx,
                        distance_m,
                        total,   # offpeak_secs = travel + dwell
                        total,   # peak_secs = same for now; refined with multi-period
                        json.dumps(attrs),
                    ),
                )

        return estimate_id

    def estimate_and_bind(
        self,
        route_id: str,
        direction_id: int,
        time_period: str = "off_peak",
        day_type: str = "weekday",
    ) -> Optional[Dict[str, Any]]:
        """
        Estimate, persist, and bind in one call.

        Returns the estimation result dict with ``estimate_id`` added.
        """
        result = self.estimate_route(route_id, direction_id, time_period, day_type)
        if not result:
            return None

        estimate_id = self.persist_estimate(result)
        result["estimate_id"] = estimate_id

        # Bind
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gtfs_work.route_runtime_estimate_bindings
                    (route_id, direction_id, estimate_id, updated_at)
                VALUES (%s::uuid, %s, %s::uuid, NOW())
                ON CONFLICT (route_id, direction_id) DO UPDATE SET
                    estimate_id = EXCLUDED.estimate_id,
                    updated_at = NOW()
                """,
                (route_id, direction_id, estimate_id),
            )

        return result

    # ------------------------------------------------------------------
    # Data loading helpers
    # ------------------------------------------------------------------

    def _get_route_data(self, route_id: str, direction_id: int) -> Optional[dict]:
        """Get route geometry, stops, metadata from the database."""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT r.route_id::text AS route_id,
                       r.route_name,
                       r.stop_node_ids,
                       COALESCE(rs.route_ref, '') AS route_code
                FROM route_prod.routes r
                LEFT JOIN route_prod.route_semantics rs ON rs.route_id = r.route_id
                WHERE r.route_id::text = %s
                  AND COALESCE(r.canonical_sequence_ready, FALSE) = TRUE
                LIMIT 1
                """,
                (route_id,),
            )
            row = cur.fetchone()
            if not row:
                return None

            # Get ordered stops
            stop_node_ids = row.get("stop_node_ids") or []
            if not stop_node_ids:
                return None

            geo_context_available = True
            try:
                cur.execute(
                    """
                    SELECT n.node_id::text AS node_id,
                           n.name,
                           ST_Y(n.geom) AS lat,
                           ST_X(n.geom) AS lon,
                           ngc.transit_density_300m
                    FROM unnest(%s::uuid[]) WITH ORDINALITY AS sid(node_id, seq)
                    JOIN node_prod.nodes n ON n.node_id = sid.node_id
                    LEFT JOIN geo_work.node_geo_context ngc ON ngc.node_id = n.node_id
                    ORDER BY sid.seq
                    """,
                    (stop_node_ids,),
                )
                stop_rows = cur.fetchall() or []
            except Exception:
                geo_context_available = False
                conn.rollback()
                cur.execute(
                    """
                    SELECT n.node_id::text AS node_id,
                           n.name,
                           ST_Y(n.geom) AS lat,
                           ST_X(n.geom) AS lon
                    FROM unnest(%s::uuid[]) WITH ORDINALITY AS sid(node_id, seq)
                    JOIN node_prod.nodes n ON n.node_id = sid.node_id
                    ORDER BY sid.seq
                    """,
                    (stop_node_ids,),
                )
                stop_rows = cur.fetchall() or []

        stops = []
        for s in stop_rows:
            if geo_context_available:
                density = s.get("transit_density_300m") or 0
                area = "urban_core" if density > 10 else ("suburban" if density > 3 else "periurban")
            else:
                area = "suburban"
            stops.append({
                "node_id": str(s["node_id"]),
                "name": s["name"] or "Unknown",
                "lat": float(s["lat"]),
                "lon": float(s["lon"]),
                "area_type": area,
            })

        # Reverse for direction 1
        if direction_id == 1:
            stops = list(reversed(stops))

        return {
            "route_id": str(row["route_id"]),
            "route_name": row["route_name"] or "",
            "route_code": row["route_code"] or "",
            "stops": stops,
        }

    def _find_research_runtime(
        self, route_code: str, direction_id: int
    ) -> Optional[dict]:
        """Find runtime ranges from loaded research data for this route."""
        if not self._research_data:
            return None

        for route in self._research_data.get("routes", []):
            code = route.get("route_code") or route.get("route_ref", "")
            if code and code == route_code:
                rt = route.get("runtime") or {}
                key = "runtime_ida" if direction_id == 0 else "runtime_vuelta"
                return rt.get(key) or rt.get("runtime_ida")

        return None

    def _compute_confidence(
        self, method: str, research_runtime: Optional[dict]
    ) -> float:
        base = 0.40 if "valhalla" in method else 0.30

        if research_runtime:
            base += 0.20
            periods = set()
            if isinstance(research_runtime, dict):
                periods = set(research_runtime.keys())
            if len(periods) >= 2:
                base += 0.10

        if self._valhalla_available:
            base += 0.10

        return min(0.95, round(base, 2))


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in meters."""
    R = 6_371_000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
