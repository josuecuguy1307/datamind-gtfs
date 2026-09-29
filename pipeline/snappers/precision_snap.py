"""Precision stop-to-polyline snapper (post-GREEK pass).

Re-aligns stop coordinates in node_prod.nodes to the exact polyline foot
of their parent route. Does NOT re-run GREEK — operates directly on the
already-stored Valhalla-traced geometry.

Usage:
    python -m pipeline.snappers.precision_snap --dry-run
    python -m pipeline.snappers.precision_snap --apply --province sample_region
    python -m pipeline.snappers.precision_snap --rollback <run_id>
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import uuid
from typing import Optional, Sequence

import psycopg2
import psycopg2.extras

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(it, **_kw):  # type: ignore[misc]
        return it

# ---------------------------------------------------------------------------
# Geo helpers — re-export canonical primitives under legacy names.
# ---------------------------------------------------------------------------

from hades.geometry.canonical import (  # noqa: E402
    EARTH_RADIUS_M as _EARTH_R,
    _M_PER_DEG_LAT,
    _m_per_deg_lon,
    haversine_m as _haversine_m,
)


def _cumulative_m(coords: Sequence[tuple[float, float]]) -> list[float]:
    """Arc-length array for a (lon, lat) polyline."""
    cum = [0.0]
    for i in range(1, len(coords)):
        lon_a, lat_a = coords[i - 1]
        lon_b, lat_b = coords[i]
        cum.append(cum[-1] + _haversine_m(lat_a, lon_a, lat_b, lon_b))
    return cum


def _project_point(
    lat: float,
    lon: float,
    coords: Sequence[tuple[float, float]],
    cum: Sequence[float],
) -> tuple[float, float]:
    """Project (lat, lon) onto a (lon, lat) polyline.

    Returns (arc_length_m, perpendicular_distance_m).
    """
    n = len(coords)
    if n == 0:
        return (0.0, float("inf"))
    if n == 1:
        lon0, lat0 = coords[0]
        return (0.0, _haversine_m(lat, lon, lat0, lon0))

    lat_ref = sum(p[1] for p in coords) / n
    mlon = _m_per_deg_lon(lat_ref)

    px = lon * mlon
    py = lat * _M_PER_DEG_LAT

    best_cum = 0.0
    best_dist = float("inf")
    for i in range(1, n):
        lon_a, lat_a = coords[i - 1]
        lon_b, lat_b = coords[i]
        ax = lon_a * mlon
        ay = lat_a * _M_PER_DEG_LAT
        bx = lon_b * mlon
        by = lat_b * _M_PER_DEG_LAT
        abx = bx - ax
        aby = by - ay
        seg_sq = abx * abx + aby * aby
        if seg_sq < 1e-9:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / seg_sq))
        fx = ax + t * abx
        fy = ay + t * aby
        d = math.hypot(px - fx, py - fy)
        if d < best_dist:
            best_dist = d
            seg_m = cum[i] - cum[i - 1]
            best_cum = cum[i - 1] + t * seg_m
    return (best_cum, best_dist)


def _project_point_windowed(
    lat: float,
    lon: float,
    coords: Sequence[tuple[float, float]],
    cum: Sequence[float],
    s_min: float,
    s_max: float,
) -> tuple[float, float]:
    """Like _project_point but restricted to arc-length window [s_min, s_max]."""
    n = len(coords)
    if n < 2:
        return _project_point(lat, lon, coords, cum)

    lat_ref = sum(p[1] for p in coords) / n
    mlon = _m_per_deg_lon(lat_ref)
    px = lon * mlon
    py = lat * _M_PER_DEG_LAT

    best_cum = -1.0
    best_dist = float("inf")
    for i in range(1, n):
        seg_start = cum[i - 1]
        seg_end = cum[i]
        if seg_end < s_min or seg_start > s_max:
            continue

        lon_a, lat_a = coords[i - 1]
        lon_b, lat_b = coords[i]
        ax = lon_a * mlon
        ay = lat_a * _M_PER_DEG_LAT
        bx = lon_b * mlon
        by = lat_b * _M_PER_DEG_LAT
        abx = bx - ax
        aby = by - ay
        seg_sq = abx * abx + aby * aby
        if seg_sq < 1e-9:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / seg_sq))

        cand_cum = cum[i - 1] + t * (cum[i] - cum[i - 1])
        cand_cum = max(s_min, min(s_max, cand_cum))
        # re-derive t from clamped cum
        seg_m = cum[i] - cum[i - 1]
        if seg_m > 1e-9:
            t = (cand_cum - cum[i - 1]) / seg_m
        else:
            t = 0.0

        fx = ax + t * abx
        fy = ay + t * aby
        d = math.hypot(px - fx, py - fy)
        if d < best_dist:
            best_dist = d
            best_cum = cand_cum

    if best_cum < 0:
        return _project_point(lat, lon, coords, cum)
    return (best_cum, best_dist)


def _interpolate(
    target_s: float,
    coords: Sequence[tuple[float, float]],
    cum: Sequence[float],
) -> tuple[float, float]:
    """Return (lat, lon) at arc-length target_s on a (lon, lat) polyline."""
    n = len(coords)
    if n == 0:
        return (0.0, 0.0)
    if target_s <= 0.0:
        lon0, lat0 = coords[0]
        return (lat0, lon0)
    if target_s >= cum[-1]:
        lon0, lat0 = coords[-1]
        return (lat0, lon0)
    for i in range(1, n):
        if cum[i] >= target_s:
            seg_m = cum[i] - cum[i - 1]
            if seg_m < 1e-9:
                lon0, lat0 = coords[i]
                return (lat0, lon0)
            t = (target_s - cum[i - 1]) / seg_m
            lon_a, lat_a = coords[i - 1]
            lon_b, lat_b = coords[i]
            return (lat_a + t * (lat_b - lat_a), lon_a + t * (lon_b - lon_a))
    lon0, lat0 = coords[-1]
    return (lat0, lon0)


# ---------------------------------------------------------------------------
# v2 / v3 hook stubs
# ---------------------------------------------------------------------------

def apply_curb_offset(
    foot_lat: float,
    foot_lon: float,
    tangent_bearing_deg: float,
) -> tuple[float, float]:
    """v2 hook: lateral curb-side offset. Returns identity for now."""
    return (foot_lat, foot_lon)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _dsn() -> str:
    dsn = os.getenv("DB_DSN") or ""
    if "amazonaws" in dsn or "rds" in dsn:
        print("FATAL: DB_DSN points to AWS. This script runs on local DB only.", file=sys.stderr)
        sys.exit(1)
    return dsn


def _connect():
    return psycopg2.connect(_dsn())


def _ensure_audit_table(conn) -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS node_prod.precision_snap_audit (
      id         BIGSERIAL       PRIMARY KEY,
      run_id     UUID            NOT NULL,
      node_id    UUID            NOT NULL,
      route_id   UUID            NOT NULL,
      direction  SMALLINT        NOT NULL,
      stop_sequence INT,
      orig_lat   DOUBLE PRECISION,
      orig_lng   DOUBLE PRECISION,
      new_lat    DOUBLE PRECISION,
      new_lng    DOUBLE PRECISION,
      shift_m    DOUBLE PRECISION,
      arc_length_s DOUBLE PRECISION,
      source_type TEXT,
      confidence REAL,
      action     TEXT NOT NULL,
      reason     TEXT,
      created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_psa_run   ON node_prod.precision_snap_audit(run_id);
    CREATE INDEX IF NOT EXISTS idx_psa_route ON node_prod.precision_snap_audit(route_id);
    CREATE INDEX IF NOT EXISTS idx_psa_node  ON node_prod.precision_snap_audit(node_id);
    """
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


# ---------------------------------------------------------------------------
# Core: load routes + stops, snap, audit
# ---------------------------------------------------------------------------

def _load_routes(conn, province: str, route_ids: Optional[list[str]]) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        where = ["province = %s"]
        params: list = [province]
        if route_ids:
            where.append("route_id = ANY(%s::uuid[])")
            params.append(route_ids)
        cur.execute(f"""
            SELECT route_id::text, direction_id,
                   ST_AsGeoJSON(geom) AS geom_json,
                   stop_node_ids::text[] AS stop_node_ids
            FROM route_prod.routes
            WHERE {' AND '.join(where)}
              AND geom IS NOT NULL
              AND stop_node_ids IS NOT NULL
              AND array_length(stop_node_ids, 1) >= 2
            ORDER BY route_id, direction_id
        """, params)
        return [dict(r) for r in cur.fetchall()]


def _parse_polyline(geom_json_str: str) -> list[tuple[float, float]]:
    """Parse ST_AsGeoJSON → [(lon, lat), ...]."""
    g = json.loads(geom_json_str)
    if g["type"] == "LineString":
        return [(float(c[0]), float(c[1])) for c in g["coordinates"]]
    if g["type"] == "MultiLineString":
        out = []
        for line in g["coordinates"]:
            for c in line:
                out.append((float(c[0]), float(c[1])))
        return out
    return []


def _load_stops(conn, node_ids: list[str]) -> dict[str, dict]:
    """Load node details keyed by node_id string."""
    if not node_ids:
        return {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT node_id::text,
                   ST_Y(geom) AS lat, ST_X(geom) AS lon,
                   confidence, source_type, osm_id
            FROM node_prod.nodes
            WHERE node_id = ANY(%s::uuid[])
        """, (node_ids,))
        return {str(r["node_id"]): dict(r) for r in cur.fetchall()}


def snap_route(
    route: dict,
    stop_map: dict[str, dict],
    *,
    min_confidence: float,
    max_shift_m: float,
) -> list[dict]:
    """Snap all stops of one route to its polyline. Returns audit rows."""
    polyline = _parse_polyline(route["geom_json"])
    if len(polyline) < 2:
        return []

    cum = _cumulative_m(polyline)
    node_ids = route["stop_node_ids"] or []
    route_id = route["route_id"]
    direction = int(route["direction_id"] or 0)

    # Compute median stop spacing for adaptive window
    stop_cum_positions: list[float] = []
    for nid in node_ids:
        s = stop_map.get(nid)
        if s and s.get("lat") is not None:
            sc, _ = _project_point(float(s["lat"]), float(s["lon"]), polyline, cum)
            stop_cum_positions.append(sc)

    spacings = [
        stop_cum_positions[i] - stop_cum_positions[i - 1]
        for i in range(1, len(stop_cum_positions))
        if stop_cum_positions[i] > stop_cum_positions[i - 1]
    ]
    median_spacing = statistics.median(spacings) if spacings else 300.0

    audit_rows: list[dict] = []
    prev_s = -1.0

    for seq, nid in enumerate(node_ids):
        stop = stop_map.get(nid)
        if not stop or stop.get("lat") is None:
            continue

        orig_lat = float(stop["lat"])
        orig_lon = float(stop["lon"])
        conf = float(stop.get("confidence") or 0.0)
        src_type = stop.get("source_type") or ""
        is_osm = stop.get("osm_id") is not None

        base = {
            "node_id": nid,
            "route_id": route_id,
            "direction": direction,
            "stop_sequence": seq,
            "orig_lat": orig_lat,
            "orig_lng": orig_lon,
            "source_type": src_type,
            "confidence": conf,
        }

        # Gate: OSM anchor with small offset
        if is_osm:
            _, cur_dist = _project_point(orig_lat, orig_lon, polyline, cum)
            if cur_dist < 25.0:
                audit_rows.append({
                    **base,
                    "new_lat": orig_lat, "new_lng": orig_lon,
                    "shift_m": 0.0, "arc_length_s": None,
                    "action": "skipped_osm",
                    "reason": f"osm_anchor_offset_{cur_dist:.1f}m<25m",
                })
                # still advance prev_s
                s_val, _ = _project_point(orig_lat, orig_lon, polyline, cum)
                prev_s = max(prev_s, s_val)
                continue

        # Gate: high confidence
        if conf >= min_confidence:
            _, cur_dist = _project_point(orig_lat, orig_lon, polyline, cum)
            audit_rows.append({
                **base,
                "new_lat": orig_lat, "new_lng": orig_lon,
                "shift_m": 0.0, "arc_length_s": None,
                "action": "skipped_high_conf",
                "reason": f"confidence_{conf:.2f}>={min_confidence}",
            })
            s_val, _ = _project_point(orig_lat, orig_lon, polyline, cum)
            prev_s = max(prev_s, s_val)
            continue

        # Windowed projection (monotonic arc-length)
        window_min = prev_s + 15.0 if prev_s >= 0 else 0.0
        window_max = prev_s + max(500.0, 3.0 * median_spacing) if prev_s >= 0 else cum[-1]
        window_max = min(window_max, cum[-1])

        fallback = False
        if window_min >= cum[-1] or window_min >= window_max:
            cand_s, cand_dist = _project_point(orig_lat, orig_lon, polyline, cum)
            fallback = True
        else:
            cand_s, cand_dist = _project_point_windowed(
                orig_lat, orig_lon, polyline, cum, window_min, window_max,
            )
            # if windowed projection returned inf (no segments in window), fall back
            if cand_dist == float("inf"):
                cand_s, cand_dist = _project_point(orig_lat, orig_lon, polyline, cum)
                fallback = True

        foot_lat, foot_lon = _interpolate(cand_s, polyline, cum)
        foot_lat, foot_lon = apply_curb_offset(foot_lat, foot_lon, 0.0)

        shift_m = _haversine_m(orig_lat, orig_lon, foot_lat, foot_lon)

        if shift_m > max_shift_m:
            audit_rows.append({
                **base,
                "new_lat": orig_lat, "new_lng": orig_lon,
                "shift_m": shift_m, "arc_length_s": cand_s,
                "action": "flagged_excess_shift",
                "reason": f"shift_{shift_m:.1f}m>max_{max_shift_m}m",
            })
            prev_s = max(prev_s, cand_s)
            continue

        action = "fallback_global" if fallback else "snapped"
        reason = None
        if fallback:
            reason = "window_empty_or_terminal"

        # Idempotency: if shift is < 0.5m, don't bother
        if shift_m < 0.5:
            audit_rows.append({
                **base,
                "new_lat": orig_lat, "new_lng": orig_lon,
                "shift_m": shift_m, "arc_length_s": cand_s,
                "action": "skipped_already_aligned",
                "reason": f"shift_{shift_m:.2f}m<0.5m",
            })
            prev_s = max(prev_s, cand_s)
            continue

        audit_rows.append({
            **base,
            "new_lat": foot_lat, "new_lng": foot_lon,
            "shift_m": shift_m, "arc_length_s": cand_s,
            "action": action,
            "reason": reason,
        })
        prev_s = max(prev_s, cand_s)

    return audit_rows


# ---------------------------------------------------------------------------
# Write paths
# ---------------------------------------------------------------------------

def _write_audit(conn, run_id: uuid.UUID, rows: list[dict]) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO node_prod.precision_snap_audit
               (run_id, node_id, route_id, direction, stop_sequence,
                orig_lat, orig_lng, new_lat, new_lng, shift_m, arc_length_s,
                source_type, confidence, action, reason)
               VALUES %s""",
            [
                (
                    str(run_id), r["node_id"], r["route_id"], r["direction"],
                    r["stop_sequence"], r["orig_lat"], r["orig_lng"],
                    r["new_lat"], r["new_lng"], r["shift_m"], r["arc_length_s"],
                    r["source_type"], r["confidence"], r["action"], r["reason"],
                )
                for r in rows
            ],
            page_size=500,
        )


def _apply_snaps(conn, rows: list[dict]) -> int:
    """UPDATE node_prod.nodes for rows with action in (snapped, fallback_global)."""
    updates = [r for r in rows if r["action"] in ("snapped", "fallback_global")]
    if not updates:
        return 0
    with conn.cursor() as cur:
        for r in updates:
            cur.execute("""
                UPDATE node_prod.nodes
                SET geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                    updated_at = now()
                WHERE node_id = %s::uuid
            """, (r["new_lng"], r["new_lat"], r["node_id"]))
    return len(updates)


def _invalidate_runtime(conn, route_ids: set[str]) -> None:
    if not route_ids:
        return
    ids = list(route_ids)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE gtfs_work.runtime_route_estimates
            SET status = 'superseded'
            WHERE route_id = ANY(%s::uuid[]) AND status = 'active'
        """, (ids,))
        cur.execute("""
            DELETE FROM gtfs_work.route_runtime_estimate_bindings
            WHERE route_id = ANY(%s::uuid[])
        """, (ids,))


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def _rollback(conn, rollback_run_id: str) -> None:
    new_run = uuid.uuid4()
    print(f"Rollback run: {new_run}")
    print(f"Restoring from audit run: {rollback_run_id}")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT node_id::text, route_id::text, direction, stop_sequence,
                   orig_lat, orig_lng, new_lat, new_lng, shift_m, arc_length_s,
                   source_type, confidence, action
            FROM node_prod.precision_snap_audit
            WHERE run_id = %s::uuid
              AND action IN ('snapped', 'fallback_global')
        """, (rollback_run_id,))
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        print("No snapped rows found for that run_id. Nothing to rollback.")
        return

    print(f"Found {len(rows)} nodes to restore.")

    rollback_audit: list[dict] = []
    route_ids: set[str] = set()
    with conn.cursor() as cur:
        for r in tqdm(rows, desc="Rolling back"):
            cur.execute("""
                UPDATE node_prod.nodes
                SET geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                    updated_at = now()
                WHERE node_id = %s::uuid
            """, (r["orig_lng"], r["orig_lat"], r["node_id"]))
            route_ids.add(r["route_id"])
            rollback_audit.append({
                "node_id": r["node_id"],
                "route_id": r["route_id"],
                "direction": r["direction"],
                "stop_sequence": r["stop_sequence"],
                "orig_lat": r["new_lat"],
                "orig_lng": r["new_lng"],
                "new_lat": r["orig_lat"],
                "new_lng": r["orig_lng"],
                "shift_m": r["shift_m"],
                "arc_length_s": r["arc_length_s"],
                "source_type": r["source_type"],
                "confidence": r["confidence"],
                "action": "rolled_back",
                "reason": f"rollback_of_{rollback_run_id}",
            })

    _write_audit(conn, new_run, rollback_audit)
    _invalidate_runtime(conn, route_ids)
    conn.commit()
    print(f"Rolled back {len(rows)} nodes across {len(route_ids)} routes.")
    print(f"Runtime bindings invalidated for {len(route_ids)} routes.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Precision stop-to-polyline snapper (post-GREEK).",
    )
    parser.add_argument("--province", default="sample_region")
    parser.add_argument("--route-ids", default=None, help="Comma-separated route UUIDs")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Compute + audit but do not write to node_prod.nodes (default)")
    parser.add_argument("--apply", action="store_true", default=False,
                        help="Write snapped coordinates to node_prod.nodes")
    parser.add_argument("--min-confidence", type=float, default=0.85)
    parser.add_argument("--max-shift-m", type=float, default=50.0)
    parser.add_argument("--rollback", default=None, metavar="RUN_ID",
                        help="Restore orig coords from a previous run")
    args = parser.parse_args()

    if args.apply:
        args.dry_run = False

    conn = _connect()
    _ensure_audit_table(conn)

    if args.rollback:
        _rollback(conn, args.rollback)
        conn.close()
        return

    run_id = uuid.uuid4()
    mode = "DRY-RUN" if args.dry_run else "APPLY"
    print(f"=== Precision Snap ===")
    print(f"Run ID:         {run_id}")
    print(f"Mode:           {mode}")
    print(f"Province:       {args.province}")
    print(f"Min confidence: {args.min_confidence}")
    print(f"Max shift:      {args.max_shift_m}m")
    print()

    route_filter = None
    if args.route_ids:
        route_filter = [s.strip() for s in args.route_ids.split(",") if s.strip()]

    routes = _load_routes(conn, args.province, route_filter)
    print(f"Loaded {len(routes)} route-directions.")

    # Collect all unique node_ids across routes
    all_node_ids: set[str] = set()
    for r in routes:
        all_node_ids.update(r["stop_node_ids"] or [])
    print(f"Loading {len(all_node_ids)} unique stop nodes...")
    stop_map = _load_stops(conn, list(all_node_ids))
    print(f"Loaded {len(stop_map)} nodes with coordinates.")
    print()

    # Counters
    counts: dict[str, int] = {}
    all_audit: list[dict] = []
    affected_routes: set[str] = set()

    for route in tqdm(routes, desc="Snapping"):
        rows = snap_route(
            route, stop_map,
            min_confidence=args.min_confidence,
            max_shift_m=args.max_shift_m,
        )
        for r in rows:
            counts[r["action"]] = counts.get(r["action"], 0) + 1
            if r["action"] in ("snapped", "fallback_global"):
                affected_routes.add(r["route_id"])
        all_audit.extend(rows)

    # Write audit (always, even dry-run)
    _write_audit(conn, run_id, all_audit)

    # Apply if not dry-run
    n_applied = 0
    if not args.dry_run:
        n_applied = _apply_snaps(conn, all_audit)
        _invalidate_runtime(conn, affected_routes)

    conn.commit()
    conn.close()

    # Summary
    print()
    print(f"{'='*50}")
    print(f"Run ID: {run_id}")
    print(f"Mode:   {mode}")
    print(f"{'='*50}")
    print(f"{'Action':<30} {'Count':>8}")
    print(f"{'-'*30} {'-'*8}")
    for action in sorted(counts.keys()):
        print(f"{action:<30} {counts[action]:>8}")
    print(f"{'-'*30} {'-'*8}")
    print(f"{'TOTAL':<30} {sum(counts.values()):>8}")
    print()
    if not args.dry_run:
        print(f"Nodes updated:              {n_applied}")
        print(f"Routes needing re-estimation: {len(affected_routes)}")
    else:
        print(f"Nodes WOULD update:         {sum(counts.get(a, 0) for a in ('snapped', 'fallback_global'))}")
        print(f"Routes WOULD need re-est:   {len(affected_routes)}")
    print()

    if affected_routes and args.dry_run:
        print("To apply: re-run with --apply")


if __name__ == "__main__":
    main()
