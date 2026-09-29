"""Compute vehicle requirements from cycle-time data and sync to schedule profiles."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import psycopg2.extras

from phase5_gtfs.common.config import db_conn


def compute_n_vehicles(cycle_time_min: float, headway_min: float) -> int:
    """
    Compute the minimum number of vehicles needed for a route.

    Formula: n_vehicles = ceil(cycle_time / headway)
    where cycle_time = runtime_ida + layover_dest + runtime_vuelta + layover_origin

    Parameters
    ----------
    cycle_time_min : float
        Full round-trip cycle time in minutes (includes layovers).
    headway_min : float
        Service headway in minutes.

    Returns
    -------
    int  Minimum vehicles required (at least 1).
    """
    if headway_min <= 0 or cycle_time_min <= 0:
        return 1
    return max(1, math.ceil(cycle_time_min / headway_min))


def sync_vehicle_blocks(
    *,
    conn: Optional[Any] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Sync n_blocks in route_schedule_profiles from catalog.route_schedule_profile
    cycle-time data.

    For each active schedule profile, find the matching catalog entry with the
    smallest headway (peak demand), compute n_vehicles, and update n_blocks.

    Returns summary of updates.
    """
    own_conn = conn is None
    if own_conn:
        with db_conn() as c:
            return _do_sync(c, dry_run=dry_run)
    else:
        return _do_sync(conn, dry_run=dry_run)


def _do_sync(conn: Any, *, dry_run: bool = False) -> Dict[str, Any]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Get peak vehicle requirement per route-direction from catalog
        # (use the smallest headway window for max vehicle need)
        cur.execute(
            """
            SELECT DISTINCT ON (route_id, direction_id)
                   route_id::text AS route_id,
                   direction_id,
                   cycle_time_min,
                   headway_min,
                   estimated_vehicles
            FROM catalog.route_schedule_profile
            WHERE cycle_time_min IS NOT NULL
              AND cycle_time_min > 0
              AND headway_min IS NOT NULL
              AND headway_min > 0
            ORDER BY route_id, direction_id, headway_min ASC
            """
        )
        catalog_rows = [dict(r) for r in (cur.fetchall() or [])]

        if not catalog_rows:
            return {"updated": 0, "skipped": 0, "details": []}

        # Build lookup
        catalog_map: Dict[str, Dict[str, Any]] = {}
        for cr in catalog_rows:
            key = f"{cr['route_id']}_{cr['direction_id']}"
            catalog_map[key] = cr

        # Get active schedule profiles
        cur.execute(
            """
            SELECT profile_id::text AS profile_id,
                   route_id::text AS route_id,
                   direction_id,
                   COALESCE(n_blocks, 1) AS current_n_blocks
            FROM gtfs_work.route_schedule_profiles
            WHERE is_active = true
            """
        )
        profiles = [dict(r) for r in (cur.fetchall() or [])]

        updated = 0
        skipped = 0
        details: List[Dict[str, Any]] = []

        for p in profiles:
            key = f"{p['route_id']}_{p['direction_id']}"
            cat = catalog_map.get(key)
            if not cat:
                skipped += 1
                continue

            cycle_time = float(cat["cycle_time_min"])
            headway = float(cat["headway_min"])

            # Use catalog estimated_vehicles if available, else compute
            if cat.get("estimated_vehicles") and int(cat["estimated_vehicles"]) > 0:
                new_n_blocks = int(cat["estimated_vehicles"])
            else:
                new_n_blocks = compute_n_vehicles(cycle_time, headway)

            current = int(p["current_n_blocks"])
            if new_n_blocks == current:
                skipped += 1
                continue

            detail = {
                "profile_id": p["profile_id"],
                "route_id": p["route_id"],
                "direction_id": p["direction_id"],
                "old_n_blocks": current,
                "new_n_blocks": new_n_blocks,
                "cycle_time_min": cycle_time,
                "headway_min": headway,
                "source": "estimated_vehicles" if cat.get("estimated_vehicles") else "computed",
            }
            details.append(detail)

            if not dry_run:
                cur.execute(
                    """
                    UPDATE gtfs_work.route_schedule_profiles
                    SET n_blocks = %s, updated_at = NOW()
                    WHERE profile_id = %s::uuid
                    """,
                    (new_n_blocks, p["profile_id"]),
                )
            updated += 1

    return {
        "updated": updated,
        "skipped": skipped,
        "dry_run": dry_run,
        "details": details,
    }
