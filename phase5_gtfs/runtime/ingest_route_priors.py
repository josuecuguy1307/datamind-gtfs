"""Ingest route-level runtimes from Deep Research into the Runtime Lab."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import psycopg2.extras

from phase5_gtfs.common.config import db_conn
from phase5_gtfs.runtime.scale_estimate import scale_estimate_to_known_total

CONFIDENCE_MAP = {
    "high": 0.85,
    "medium": 0.60,
    "low": 0.35,
    "estimated": 0.25,
}


def _resolve_route_id(entry: Dict[str, Any], cur: Any) -> Optional[str]:
    """Try route_id directly, then match by route_name/ref."""
    rid = str(entry.get("route_id") or "").strip()
    if rid:
        cur.execute(
            "SELECT route_id::text FROM route_prod.routes WHERE route_id::text = %s LIMIT 1",
            (rid,),
        )
        row = cur.fetchone()
        if row:
            return str(row["route_id"])

    # Fallback: match by route_name
    name = str(entry.get("route_name") or "").strip()
    if name:
        cur.execute(
            "SELECT route_id::text FROM route_prod.routes WHERE route_name ILIKE %s LIMIT 1",
            (f"%{name}%",),
        )
        row = cur.fetchone()
        if row:
            return str(row["route_id"])

    return None


def _get_active_binding(route_id: str, direction_id: int, cur: Any) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT b.estimate_id::text AS estimate_id
        FROM gtfs_work.route_runtime_estimate_bindings b
        WHERE b.route_id::text = %s AND b.direction_id = %s
        LIMIT 1
        """,
        (route_id, direction_id),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _compute_commercial_speed(entry: Dict[str, Any], route_id: str, cur: Any) -> Optional[float]:
    cur.execute(
        "SELECT ST_Length(geom::geography) AS length_m FROM route_prod.routes WHERE route_id::text = %s",
        (route_id,),
    )
    row = cur.fetchone()
    if not row or not row.get("length_m"):
        return None
    length_km = float(row["length_m"]) / 1000.0
    runtime_hours = float(entry.get("runtime_ida_min", 60)) / 60.0
    if runtime_hours <= 0:
        return None
    return round(length_km / runtime_hours, 2)


def _add_route_prior(
    route_id: str,
    route_name: str,
    commercial_kmh: Optional[float],
    source: str,
    cur: Any,
) -> None:
    if not commercial_kmh:
        return
    code = f"deep_{route_id[:8]}"
    payload = {
        "aliases": [route_name] if route_name else [],
        "route_id": route_id,
        "commercial_kmh": commercial_kmh,
        "derived_from": source,
    }
    cur.execute(
        """
        INSERT INTO gtfs_work.runtime_catalog_items
            (catalog_key, item_code, item_name, payload, is_active, source)
        VALUES ('route_prior_catalog', %s, %s, %s::jsonb, true, %s)
        ON CONFLICT (catalog_key, item_code) DO UPDATE SET
            payload = EXCLUDED.payload, updated_at = NOW()
        """,
        (code, route_name or code, json.dumps(payload), source),
    )


def _update_schedule_profile(
    route_id: str,
    entry: Dict[str, Any],
    cur: Any,
) -> None:
    headway = entry.get("headway_min")
    vehicles = entry.get("n_vehicles")
    runtime_min = entry.get("runtime_ida_min")
    source = entry.get("source", "deep_research")
    confidence = CONFIDENCE_MAP.get(str(entry.get("confidence", "medium")), 0.6)

    sets: List[str] = []
    params: List[Any] = []
    if headway:
        sets.append("headway_min = %s")
        params.append(int(headway))
    if vehicles:
        sets.append("estimated_vehicles = %s")
        params.append(int(vehicles))
    if runtime_min:
        sets.append("runtime_override_min = %s")
        params.append(float(runtime_min))
        sets.append("runtime_override_reason = %s")
        params.append(f"deep_research: {source}")
    if sets:
        sets.append("source = %s")
        params.append(source)
        sets.append("confidence = GREATEST(confidence, %s)")
        params.append(confidence)
        params.append(route_id)
        cur.execute(
            f"UPDATE catalog.route_schedule_profile SET {', '.join(sets)} WHERE route_id::text = %s",
            params,
        )


def ingest_deep_research_runtimes(
    data: List[Dict[str, Any]],
    *,
    conn: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """
    Ingest route-level runtimes from Deep Research.

    Input format per entry::

        {
            "route_id": "uuid-or-partial",
            "route_name": "Sangolquí - Quito (Marín)",
            "runtime_ida_min": 75,
            "runtime_vuelta_min": 70,
            "time_period": "peak_am",   # peak_am | peak_pm | offpeak
            "source": "AMT permit 2024-0847",
            "confidence": "high",       # high | medium | low | estimated
            "headway_min": 8,
            "n_vehicles": 19,
            "direction_id": 0,
            "notes": "..."
        }

    Actions per entry:
      1. Resolve route_id (UUID or name match).
      2. If route has existing Runtime Lab estimate → apply proportional scaling.
      3. If no estimate → add as route_prior to route_prior_catalog.
      4. If headway/n_vehicles provided → update catalog.route_schedule_profile.
    """
    own_conn = conn is None
    if own_conn:
        _ctx = db_conn()
        conn = _ctx.__enter__()
    else:
        _ctx = None
    try:
        results: List[Dict[str, Any]] = []
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for entry in data:
                route_id = _resolve_route_id(entry, cur)
                if not route_id:
                    results.append({
                        "status": "SKIP",
                        "reason": f"Cannot resolve route: {entry.get('route_name', entry.get('route_id', '?'))}",
                    })
                    continue

                direction_id = int(entry.get("direction_id", 0))
                binding = _get_active_binding(route_id, direction_id, cur)

                if binding:
                    tp_raw = str(entry.get("time_period", "offpeak"))
                    time_period = "peak" if "peak" in tp_raw else "offpeak"
                    known_secs = float(entry.get("runtime_ida_min", 60)) * 60.0

                    result = scale_estimate_to_known_total(
                        estimate_id=binding["estimate_id"],
                        known_total_secs=known_secs,
                        time_period=time_period,
                        source=str(entry.get("source", "deep_research")),
                        conn=conn,
                    )
                    result["action"] = "SCALED"
                    result["route_id"] = route_id
                    results.append(result)
                else:
                    kmh = _compute_commercial_speed(entry, route_id, cur)
                    _add_route_prior(
                        route_id=route_id,
                        route_name=str(entry.get("route_name", "")),
                        commercial_kmh=kmh,
                        source=str(entry.get("source", "deep_research")),
                        cur=cur,
                    )
                    results.append({
                        "action": "PRIOR_ADDED",
                        "route_id": route_id,
                        "commercial_kmh": kmh,
                    })

                # Update schedule profile if headway/vehicles provided
                if entry.get("headway_min") or entry.get("n_vehicles"):
                    _update_schedule_profile(route_id, entry, cur)

        if own_conn:
            conn.commit()
        return results
    finally:
        if own_conn and _ctx is not None:
            _ctx.__exit__(None, None, None)
