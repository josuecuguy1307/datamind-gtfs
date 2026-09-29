"""
Phase 4-5 Automation — Agent 3: Valhalla Runtime Cross-Check

Compares the catalog-based runtime estimates against Valhalla's bus
routing duration for the same stop sequence. Flags routes where the
divergence exceeds a threshold.

Precondition: Valhalla is running on localhost (port 8003).

Usage:
    python src/automation/agent_valhalla_crosscheck.py --all
    python src/automation/agent_valhalla_crosscheck.py --route <route_id>
    python src/automation/agent_valhalla_crosscheck.py --batch <id1>,<id2>
    python src/automation/agent_valhalla_crosscheck.py --all --persist
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

DB_DSN = (
    os.environ.get("DB_DSN")
    or os.environ.get("DATABASE_URL")
    or "postgresql://localhost:5432/datamind_ml"
)

VALHALLA_URL = os.environ.get("VALHALLA_URL", "http://127.0.0.1:8003")
VALHALLA_COSTING = os.environ.get("VALHALLA_COSTING", "bus")
VALHALLA_TIMEOUT = int(os.environ.get("VALHALLA_TIMEOUT", "60"))
VALHALLA_MAX_LOCATIONS = int(os.environ.get("VALHALLA_MAX_LOCATIONS_PER_CALL", "20"))

# Divergence thresholds
WARN_DIVERGENCE_PCT = 30.0   # soft warning if >30% off
BLOCK_DIVERGENCE_PCT = 60.0  # hard flag if >60% off


# ------------------------------------------------------------------
# Valhalla client (lightweight, returns duration + distance)
# ------------------------------------------------------------------

def valhalla_route_with_summary(
    locations: list[tuple[float, float]],
) -> dict[str, Any]:
    """Route through Valhalla and return summary with duration/distance.
    locations: list of (lon, lat) tuples.
    Returns: {duration_s, distance_km, n_legs, legs: [{time, length}]}
    """
    url = f"{VALHALLA_URL.rstrip('/')}/route"

    # Build payload
    payload = {
        "locations": [
            {"lat": lat, "lon": lon, "type": "break"}
            for lon, lat in locations
        ],
        "costing": VALHALLA_COSTING,
        "shape_format": "polyline6",
        "costing_options": {},
    }

    # Chunk if too many locations
    if len(locations) <= VALHALLA_MAX_LOCATIONS:
        return _route_once_summary(url, payload)

    # Chunk and accumulate
    total_duration = 0.0
    total_distance = 0.0
    total_legs = 0
    legs = []

    start = 0
    n = len(locations)
    while start < n - 1:
        end = min(start + VALHALLA_MAX_LOCATIONS, n)
        chunk_locs = locations[start:end]
        chunk_payload = {
            "locations": [
                {"lat": lat, "lon": lon, "type": "break"}
                for lon, lat in chunk_locs
            ],
            "costing": VALHALLA_COSTING,
            "shape_format": "polyline6",
            "costing_options": {},
        }
        result = _route_once_summary(url, chunk_payload)
        total_duration += result["duration_s"]
        total_distance += result["distance_km"]
        total_legs += result["n_legs"]
        legs.extend(result.get("legs", []))

        if end == n:
            break
        start = end - 1  # overlap by 1

    return {
        "duration_s": total_duration,
        "distance_km": round(total_distance, 2),
        "n_legs": total_legs,
        "legs": legs,
    }


def _route_once_summary(url: str, payload: dict) -> dict[str, Any]:
    """Single Valhalla /route call, returns summary."""
    resp = requests.post(url, json=payload, timeout=VALHALLA_TIMEOUT)

    if resp.status_code >= 400:
        # Try GET fallback
        resp = requests.get(
            url,
            params={"json": json.dumps(payload)},
            timeout=VALHALLA_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Valhalla /route failed ({resp.status_code}): "
                f"{(resp.text or '')[:300]}"
            )

    data = resp.json()
    trip = data.get("trip", {})
    summary = trip.get("summary", {})
    legs_raw = trip.get("legs", [])

    legs = []
    for leg in legs_raw:
        leg_summary = leg.get("summary", {})
        legs.append({
            "time_s": leg_summary.get("time", 0),
            "length_km": leg_summary.get("length", 0),
        })

    return {
        "duration_s": summary.get("time", 0),
        "distance_km": round(summary.get("length", 0), 2),
        "n_legs": len(legs_raw),
        "legs": legs,
    }


# ------------------------------------------------------------------
# Data loading
# ------------------------------------------------------------------

def load_routes_for_crosscheck(
    cur, route_ids: list[str] | None = None,
) -> list[dict]:
    """Load routes with stop coordinates and runtime bindings."""
    where = "cs.approved = TRUE"
    params: list[Any] = []
    if route_ids:
        where += " AND cs.route_id = ANY(%s::uuid[])"
        params.append(route_ids)

    cur.execute(f"""
        SELECT
            r.route_id::text,
            r.direction_id,
            r.service_route_id::text,
            cs.route_short_name,
            cs.operator,
            b.estimate_id::text,
            (e.metrics->>'runtime_offpeak_secs')::int AS catalog_runtime_offpeak_secs,
            (e.metrics->>'runtime_peak_secs')::int AS catalog_runtime_peak_secs,
            (e.metrics->>'route_len_m')::float AS catalog_route_len_m,
            (e.metrics->>'n_legs')::int AS catalog_n_legs
        FROM catalog.route_semantics cs
        JOIN route_prod.routes r ON r.route_id = cs.route_id
        LEFT JOIN gtfs_work.route_runtime_estimate_bindings b
            ON b.route_id = r.route_id AND b.direction_id = r.direction_id
        LEFT JOIN gtfs_work.runtime_route_estimates e
            ON e.estimate_id = b.estimate_id
        WHERE {where}
        ORDER BY cs.operator, cs.route_short_name, r.direction_id
    """, params)
    return [dict(row) for row in cur.fetchall()]


def load_stop_coords(cur, route_id: str, direction_id: int) -> list[tuple[float, float]]:
    """Load stop coordinates as (lon, lat) for a route.
    Reverses order for direction_id=1."""
    cur.execute("""
        SELECT ST_X(n.geom) AS lon, ST_Y(n.geom) AS lat
        FROM route_prod.routes r
        JOIN LATERAL unnest(r.stop_node_ids) WITH ORDINALITY AS sid(node_id, seq) ON true
        JOIN node_prod.nodes n ON n.node_id = sid.node_id
        WHERE r.route_id = %s::uuid
          AND COALESCE(r.canonical_sequence_ready, FALSE) = TRUE
          AND r.chosen_stop_sequence_candidate_id IS NOT NULL
        ORDER BY sid.seq
    """, (route_id,))
    stops = [(row["lon"], row["lat"]) for row in cur.fetchall()]

    if direction_id == 1:
        stops = list(reversed(stops))

    return stops


# ------------------------------------------------------------------
# Cross-check logic
# ------------------------------------------------------------------

def crosscheck_route(
    cur, route: dict,
) -> dict[str, Any]:
    """Compare catalog runtime vs Valhalla for one route.
    Returns result dict."""
    route_id = route["route_id"]
    direction_id = int(route.get("direction_id", 0))
    ref = route.get("route_short_name", "?")

    result = {
        "route_id": route_id,
        "direction_id": direction_id,
        "route_short_name": ref,
        "operator": route.get("operator", "?"),
        "status": None,
        "catalog_runtime_secs": route.get("catalog_runtime_offpeak_secs"),
        "catalog_route_len_m": route.get("catalog_route_len_m"),
        "valhalla_duration_s": None,
        "valhalla_distance_km": None,
        "divergence_pct": None,
        "distance_divergence_pct": None,
        "warnings": [],
        "error": None,
    }

    # Check if we have a runtime binding
    if not route.get("estimate_id"):
        result["status"] = "SKIP_NO_BINDING"
        result["warnings"].append("No runtime estimate binding")
        return result

    catalog_secs = route["catalog_runtime_offpeak_secs"]
    if not catalog_secs or catalog_secs <= 0:
        result["status"] = "SKIP_NO_RUNTIME"
        result["warnings"].append("Catalog runtime is 0 or missing")
        return result

    # Load stop coords
    stops = load_stop_coords(cur, route_id, direction_id)
    if len(stops) < 2:
        result["status"] = "SKIP_NO_STOPS"
        result["warnings"].append(f"Only {len(stops)} stops found")
        return result

    # Call Valhalla
    try:
        valhalla = valhalla_route_with_summary(stops)
    except Exception as e:
        result["status"] = "VALHALLA_ERROR"
        result["error"] = str(e)[:200]
        return result

    valhalla_secs = valhalla["duration_s"]
    valhalla_km = valhalla["distance_km"]

    result["valhalla_duration_s"] = valhalla_secs
    result["valhalla_distance_km"] = valhalla_km
    result["valhalla_n_legs"] = valhalla["n_legs"]

    # Runtime divergence (catalog vs Valhalla)
    if valhalla_secs > 0:
        # Valhalla gives pure road travel time (no dwell), so compare
        # against catalog total which includes dwell. Expect catalog > valhalla.
        divergence = ((catalog_secs - valhalla_secs) / valhalla_secs) * 100.0
        result["divergence_pct"] = round(divergence, 1)

        abs_div = abs(divergence)
        if abs_div > BLOCK_DIVERGENCE_PCT:
            result["status"] = "FLAGGED"
            result["warnings"].append(
                f"Runtime divergence {divergence:+.1f}% exceeds {BLOCK_DIVERGENCE_PCT}% threshold"
            )
        elif abs_div > WARN_DIVERGENCE_PCT:
            result["status"] = "WARNING"
            result["warnings"].append(
                f"Runtime divergence {divergence:+.1f}% exceeds {WARN_DIVERGENCE_PCT}% warning threshold"
            )
        else:
            result["status"] = "OK"
    else:
        result["status"] = "VALHALLA_ZERO"
        result["warnings"].append("Valhalla returned 0 duration")

    # Distance divergence
    catalog_len_m = route.get("catalog_route_len_m")
    if catalog_len_m and catalog_len_m > 0 and valhalla_km > 0:
        catalog_km = catalog_len_m / 1000.0
        dist_div = ((catalog_km - valhalla_km) / valhalla_km) * 100.0
        result["distance_divergence_pct"] = round(dist_div, 1)

        if abs(dist_div) > WARN_DIVERGENCE_PCT:
            result["warnings"].append(
                f"Distance divergence {dist_div:+.1f}%: "
                f"catalog={catalog_km:.1f}km vs valhalla={valhalla_km:.1f}km"
            )

    return result


# ------------------------------------------------------------------
# Diagnostic persistence
# ------------------------------------------------------------------

def persist_crosscheck_diagnostic(cur, run_id: str, results: list[dict]) -> None:
    """Save cross-check results to automation.diagnostics."""
    for r in results:
        status_map = {
            "OK": "CLEAN",
            "WARNING": "WARNINGS",
            "FLAGGED": "WARNINGS",
            "SKIP_NO_BINDING": "WARNINGS",
            "SKIP_NO_RUNTIME": "WARNINGS",
            "SKIP_NO_STOPS": "WARNINGS",
            "VALHALLA_ERROR": "WARNINGS",
            "VALHALLA_ZERO": "WARNINGS",
        }
        db_status = status_map.get(r["status"], "WARNINGS")

        cur.execute("""
            INSERT INTO automation.diagnostics (run_id, route_id, phase, status, report)
            VALUES (%s, %s::uuid, 'valhalla_crosscheck', %s, %s::jsonb)
        """, (run_id, r["route_id"], db_status, json.dumps(r, default=str)))


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Agent 3 — Valhalla runtime cross-check"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Check all approved routes")
    group.add_argument("--route", type=str, help="Single route_id UUID")
    group.add_argument("--batch", type=str, help="Comma-separated route_id UUIDs")
    parser.add_argument("--persist", action="store_true",
                        help="Save results to automation.diagnostics")
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "_valhalla"

    # Quick Valhalla health check
    try:
        requests.post(
            f"{VALHALLA_URL}/route",
            json={"locations": [
                {"lat": -0.22, "lon": -78.50, "type": "break"},
                {"lat": -0.23, "lon": -78.51, "type": "break"},
            ], "costing": VALHALLA_COSTING, "shape_format": "polyline6"},
            timeout=10,
        )
        valhalla_ok = True
    except Exception as e:
        valhalla_ok = False
        print(f"WARNING: Valhalla not reachable at {VALHALLA_URL}: {e}")
        print("Cross-check will mark all routes as VALHALLA_ERROR.\n")

    conn = psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor)
    conn.autocommit = False

    try:
        cur = conn.cursor()

        # Resolve route_ids
        route_ids = None
        if args.route:
            route_ids = [args.route]
        elif args.batch:
            route_ids = [r.strip() for r in args.batch.split(",")]

        routes = load_routes_for_crosscheck(cur, route_ids)

        print(f"Agent 3 — Valhalla Runtime Cross-Check")
        print(f"Run ID: {run_id}")
        print(f"Valhalla: {VALHALLA_URL} ({'OK' if valhalla_ok else 'UNREACHABLE'})")
        print(f"Routes to check: {len(routes)}")
        print(f"Thresholds: WARN>{WARN_DIVERGENCE_PCT}%, FLAG>{BLOCK_DIVERGENCE_PCT}%")
        print("=" * 80)

        if not routes:
            print("\nNo approved routes with runtime bindings. Nothing to check.")
            conn.rollback()
            return

        results = []
        for route in routes:
            r = crosscheck_route(cur, route)
            results.append(r)

            ref = r["route_short_name"]
            status = r["status"]
            div = r.get("divergence_pct")
            cat_min = (r["catalog_runtime_secs"] or 0) / 60.0
            val_min = (r["valhalla_duration_s"] or 0) / 60.0
            dist_div = r.get("distance_divergence_pct")

            if div is not None:
                print(f"  {status:16s} d{r['direction_id']} {ref:8s} | "
                      f"catalog={cat_min:.0f}min valhalla={val_min:.0f}min | "
                      f"div={div:+.1f}%"
                      f"{f' dist_div={dist_div:+.1f}%' if dist_div is not None else ''}")
            else:
                print(f"  {status:16s} d{r['direction_id']} {ref:8s} | "
                      f"{r.get('error') or '; '.join(r.get('warnings', []))}")

        # Persist
        if args.persist and results:
            persist_crosscheck_diagnostic(cur, run_id, results)
            conn.commit()
            print("\nDiagnostics persisted.")
        else:
            conn.rollback()

        # Summary
        print()
        print("=" * 80)
        ok = sum(1 for r in results if r["status"] == "OK")
        warn = sum(1 for r in results if r["status"] == "WARNING")
        flagged = sum(1 for r in results if r["status"] == "FLAGGED")
        skipped = sum(1 for r in results if r["status"] and r["status"].startswith("SKIP"))
        errors = sum(1 for r in results if r["status"] in ("VALHALLA_ERROR", "VALHALLA_ZERO"))

        avg_div = None
        divs = [r["divergence_pct"] for r in results if r.get("divergence_pct") is not None]
        if divs:
            avg_div = sum(divs) / len(divs)

        print(f"  Total:    {len(results)}")
        print(f"  OK:       {ok}")
        print(f"  Warning:  {warn}")
        print(f"  Flagged:  {flagged}")
        print(f"  Skipped:  {skipped}")
        print(f"  Errors:   {errors}")
        if avg_div is not None:
            print(f"  Avg divergence: {avg_div:+.1f}%")
        print("=" * 80)

    except Exception as e:
        conn.rollback()
        print(f"\nERROR: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
