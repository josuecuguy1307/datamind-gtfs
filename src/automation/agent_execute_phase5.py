"""
Phase 4-5 Automation — Agent 2b: Phase 5 Schedule Bridge

Reads approved catalog schedule data and writes it into the
gtfs_work authoring tables (route_schedule_profiles, service_windows,
calendar_exceptions) that the existing GTFS compilers read from.

Computes layover-aware cycle time and vehicle count (n_blocks)
using runtime estimate bindings + catalog layover policy.

Preconditions:
  - Agent 2a (Phase 4 semantics writer) has run for target routes.
  - Runtime estimates are bound (soft gate — writes defaults if missing).

Usage:
    python src/automation/agent_execute_phase5.py --all
    python src/automation/agent_execute_phase5.py --route <route_id>
    python src/automation/agent_execute_phase5.py --batch <id1>,<id2>
    python src/automation/agent_execute_phase5.py --all --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

DB_DSN = (
    os.environ.get("DB_DSN")
    or os.environ.get("DATABASE_URL")
    or "postgresql://localhost:5432/datamind_ml"
)

DEFAULT_LAYOVER_MIN = 5.0
DEFAULT_RUNTIME_SECS = 3600  # 1 hour fallback if no runtime binding
DEFAULT_DWELL_SECS = 20


# ------------------------------------------------------------------
# Gate check
# ------------------------------------------------------------------

def check_phase4_done(cur, route_id: str) -> tuple[bool, str]:
    """Verify Phase 4 semantics writer has run for this route.
    Returns (ok, reason)."""
    cur.execute("""
        SELECT status FROM automation.diagnostics
        WHERE route_id = %s::uuid AND phase = 'phase4_execute'
        ORDER BY created_at DESC
        LIMIT 1
    """, (route_id,))
    row = cur.fetchone()
    if not row:
        return False, "No phase4_execute diagnostic — run agent_execute_phase4.py first"
    if row["status"] == "BLOCKED":
        return False, "Phase 4 execution is BLOCKED"
    return True, row["status"]


# ------------------------------------------------------------------
# Data loading
# ------------------------------------------------------------------

def load_approved_routes(cur, route_ids: list[str] | None = None) -> list[str]:
    """Get approved route_ids from catalog.route_semantics."""
    if route_ids:
        cur.execute("""
            SELECT route_id::text
            FROM catalog.route_semantics
            WHERE approved = TRUE AND route_id = ANY(%s::uuid[])
        """, (route_ids,))
    else:
        cur.execute("""
            SELECT route_id::text
            FROM catalog.route_semantics
            WHERE approved = TRUE
        """)
    return [row["route_id"] for row in cur.fetchall()]


def load_schedule_profiles(cur, route_ids: list[str]) -> list[dict]:
    """Load catalog.route_schedule_profile for given routes."""
    cur.execute("""
        SELECT sp.route_id::text, sp.direction_id,
               sp.service_pattern_id,
               sp.window_start::text, sp.window_end::text,
               sp.headway_min, sp.exact_departures,
               sp.runtime_override_min, sp.peak_type,
               sp.estimated_vehicles, sp.cycle_time_min,
               sp.confidence
        FROM catalog.route_schedule_profile sp
        WHERE sp.route_id = ANY(%s::uuid[])
        ORDER BY sp.route_id, sp.direction_id, sp.service_pattern_id
    """, (route_ids,))
    return [dict(row) for row in cur.fetchall()]


def load_service_days(cur, route_ids: list[str]) -> list[dict]:
    """Load catalog.route_service_days for given routes."""
    cur.execute("""
        SELECT sd.route_id::text, sd.service_pattern_id,
               sd.monday, sd.tuesday, sd.wednesday, sd.thursday, sd.friday,
               sd.saturday, sd.sunday,
               sd.first_departure::text, sd.last_departure::text,
               sd.headway_min AS days_headway_min
        FROM catalog.route_service_days sd
        WHERE sd.route_id = ANY(%s::uuid[])
        ORDER BY sd.route_id, sd.service_pattern_id
    """, (route_ids,))
    return [dict(row) for row in cur.fetchall()]


def load_layover_policies(cur, route_ids: list[str]) -> dict[str, dict]:
    """Load catalog.route_layover_policy keyed by route_id."""
    cur.execute("""
        SELECT lp.route_id::text,
               lp.layover_at_destination_min,
               lp.layover_at_origin_min,
               lp.min_layover_min, lp.max_layover_min,
               lp.applies_to_pattern
        FROM catalog.route_layover_policy lp
        WHERE lp.route_id = ANY(%s::uuid[])
    """, (route_ids,))
    result = {}
    for row in cur.fetchall():
        result[row["route_id"]] = dict(row)
    return result


def load_runtime_bindings(cur, route_ids: list[str]) -> dict[tuple[str, int], dict]:
    """Load runtime bindings keyed by (route_id, direction_id)."""
    cur.execute("""
        SELECT b.route_id::text, b.direction_id,
               b.estimate_id::text,
               (e.metrics->>'runtime_offpeak_secs')::int AS runtime_offpeak_secs,
               (e.metrics->>'runtime_peak_secs')::int AS runtime_peak_secs,
               (e.metrics->>'n_legs')::int AS n_legs
        FROM gtfs_work.route_runtime_estimate_bindings b
        JOIN gtfs_work.runtime_route_estimates e ON e.estimate_id = b.estimate_id
        WHERE b.route_id = ANY(%s::uuid[])
    """, (route_ids,))
    result = {}
    for row in cur.fetchall():
        key = (row["route_id"], int(row["direction_id"]))
        result[key] = dict(row)
    return result


def load_route_info(cur, route_ids: list[str]) -> dict[str, dict]:
    """Load route_prod.routes info keyed by route_id."""
    cur.execute("""
        SELECT r.route_id::text, r.direction_id, r.service_route_id::text,
               array_length(r.stop_node_ids, 1) AS n_stops
        FROM route_prod.routes r
        WHERE r.route_id = ANY(%s::uuid[])
    """, (route_ids,))
    result = {}
    for row in cur.fetchall():
        result[row["route_id"]] = dict(row)
    return result


def find_partner_route_id(route_id: str, route_info: dict[str, dict]) -> str | None:
    """Find the opposite-direction route_id via service_route_id."""
    info = route_info.get(route_id)
    if not info or not info.get("service_route_id"):
        return None
    srv_id = info["service_route_id"]
    for rid, rinfo in route_info.items():
        if rid != route_id and rinfo.get("service_route_id") == srv_id:
            return rid
    return None


# ------------------------------------------------------------------
# Cycle time computation
# ------------------------------------------------------------------

def compute_cycle_time(
    route_id: str,
    route_info: dict[str, dict],
    runtime_bindings: dict[tuple[str, int], dict],
    layover_policies: dict[str, dict],
) -> dict:
    """Compute layover-aware cycle time and vehicle count.
    Returns dict with cycle_time_min, n_vehicles, runtime details."""
    info = route_info.get(route_id, {})
    my_dir = int(info.get("direction_id", 0))
    partner_id = find_partner_route_id(route_id, route_info)

    # Get runtime for this direction
    my_binding = runtime_bindings.get((route_id, my_dir))
    my_runtime_secs = my_binding["runtime_offpeak_secs"] if my_binding else DEFAULT_RUNTIME_SECS

    # Get runtime for partner direction
    partner_runtime_secs = DEFAULT_RUNTIME_SECS
    if partner_id:
        partner_info = route_info.get(partner_id, {})
        partner_dir = int(partner_info.get("direction_id", 1 - my_dir))
        partner_binding = runtime_bindings.get((partner_id, partner_dir))
        if partner_binding:
            partner_runtime_secs = partner_binding["runtime_offpeak_secs"]

    # Layover
    lp = layover_policies.get(route_id, {})
    layover_dest = float(lp.get("layover_at_destination_min", DEFAULT_LAYOVER_MIN))
    layover_orig = float(lp.get("layover_at_origin_min", DEFAULT_LAYOVER_MIN))

    # Cycle time
    runtime_dir0_min = my_runtime_secs / 60.0
    runtime_dir1_min = partner_runtime_secs / 60.0
    cycle_time_min = runtime_dir0_min + layover_dest + runtime_dir1_min + layover_orig

    return {
        "cycle_time_min": round(cycle_time_min, 1),
        "runtime_this_dir_min": round(runtime_dir0_min, 1),
        "runtime_partner_dir_min": round(runtime_dir1_min, 1),
        "layover_dest_min": layover_dest,
        "layover_orig_min": layover_orig,
        "has_runtime_binding": my_binding is not None,
        "has_partner_binding": partner_id is not None,
        "partner_route_id": partner_id,
    }


# ------------------------------------------------------------------
# Core: write to gtfs_work authoring tables
# ------------------------------------------------------------------

def write_schedule_profile(
    cur, route_id: str, direction_id: int,
    service_pattern_id: str, runtime_secs: int, n_blocks: int,
    dry_run: bool,
) -> str | None:
    """Upsert a gtfs_work.route_schedule_profiles row.
    Returns profile_id (UUID string) or None on dry_run."""
    service_name = service_pattern_id

    if dry_run:
        return None

    # Upsert using unique(route_id, direction_id, service_name)
    cur.execute("""
        INSERT INTO gtfs_work.route_schedule_profiles (
            route_id, direction_id, service_name,
            runtime_secs, dwell_secs, n_blocks, is_active
        ) VALUES (
            %(route_id)s::uuid, %(dir)s, %(svc_name)s,
            %(runtime)s, %(dwell)s, %(n_blocks)s, TRUE
        )
        ON CONFLICT (route_id, direction_id, service_name) DO UPDATE SET
            runtime_secs = EXCLUDED.runtime_secs,
            n_blocks = EXCLUDED.n_blocks,
            is_active = TRUE,
            updated_at = now()
        RETURNING profile_id::text
    """, {
        "route_id": route_id,
        "dir": direction_id,
        "svc_name": service_name,
        "runtime": runtime_secs,
        "dwell": DEFAULT_DWELL_SECS,
        "n_blocks": n_blocks,
    })
    row = cur.fetchone()
    return row["profile_id"] if row else None


def write_service_window(
    cur, profile_id: str,
    start_time: str, end_time: str,
    headway_secs: int | None,
    exact_departures: list[str] | None,
    day_flags: dict[str, bool],
    is_peak: bool,
    dry_run: bool,
) -> None:
    """Insert a service_window for a profile.
    We delete+reinsert because service_windows has no natural unique key
    beyond profile_id + start_time."""
    if dry_run:
        return

    # Delete existing windows for this profile (clean slate per bridge run)
    # This is safe because we're regenerating all windows from catalog
    cur.execute("""
        DELETE FROM gtfs_work.service_windows
        WHERE profile_id = %s::uuid AND start_time = %s
    """, (profile_id, start_time))

    cur.execute("""
        INSERT INTO gtfs_work.service_windows (
            profile_id, start_time, end_time,
            headway_secs, exact_departures,
            monday, tuesday, wednesday, thursday, friday, saturday, sunday,
            is_peak, is_active
        ) VALUES (
            %(profile_id)s::uuid, %(start)s, %(end)s,
            %(headway)s, %(exact_deps)s,
            %(mon)s, %(tue)s, %(wed)s, %(thu)s, %(fri)s, %(sat)s, %(sun)s,
            %(is_peak)s, TRUE
        )
    """, {
        "profile_id": profile_id,
        "start": start_time,
        "end": end_time,
        "headway": headway_secs,
        "exact_deps": exact_departures or [],
        "mon": day_flags.get("monday", False),
        "tue": day_flags.get("tuesday", False),
        "wed": day_flags.get("wednesday", False),
        "thu": day_flags.get("thursday", False),
        "fri": day_flags.get("friday", False),
        "sat": day_flags.get("saturday", False),
        "sun": day_flags.get("sunday", False),
        "is_peak": is_peak,
    })


def write_calendar_exceptions(cur, route_ids: list[str], dry_run: bool) -> int:
    """Copy catalog.route_service_exceptions → gtfs_work.calendar_exceptions.
    Returns count inserted."""
    cur.execute("""
        SELECT se.route_id::text, se.exception_date, se.exception_type
        FROM catalog.route_service_exceptions se
        WHERE se.route_id = ANY(%s::uuid[])
        ORDER BY se.route_id, se.exception_date
    """, (route_ids,))
    exceptions = cur.fetchall()

    if not exceptions or dry_run:
        return len(exceptions) if dry_run else 0

    inserted = 0
    for ex in exceptions:
        # Find the profile_id(s) for this route in gtfs_work
        cur.execute("""
            SELECT DISTINCT profile_id::text
            FROM gtfs_work.route_schedule_profiles
            WHERE route_id = %s::uuid AND is_active = TRUE
        """, (ex["route_id"],))
        profiles = cur.fetchall()

        for p in profiles:
            cur.execute("""
                INSERT INTO gtfs_work.calendar_exceptions (
                    profile_id, service_date, exception_type
                ) VALUES (
                    %s::uuid, %s, %s
                )
                ON CONFLICT (profile_id, service_date) DO UPDATE SET
                    exception_type = EXCLUDED.exception_type
            """, (p["profile_id"], ex["exception_date"], ex["exception_type"]))
            inserted += 1

    return inserted


# ------------------------------------------------------------------
# Main processing logic
# ------------------------------------------------------------------

def process_route(
    cur,
    route_id: str,
    schedule_profiles: list[dict],
    service_days: list[dict],
    layover_policies: dict[str, dict],
    runtime_bindings: dict[tuple[str, int], dict],
    route_info: dict[str, dict],
    dry_run: bool,
) -> dict:
    """Process one route_id: write profiles + windows from catalog data.
    Returns result dict."""
    info = route_info.get(route_id, {})
    my_dir = int(info.get("direction_id", 0))

    # Filter to this route's catalog data
    my_profiles = [sp for sp in schedule_profiles if sp["route_id"] == route_id]
    my_days = {sd["service_pattern_id"]: sd for sd in service_days if sd["route_id"] == route_id}

    result = {
        "route_id": route_id,
        "direction_id": my_dir,
        "action": None,
        "profiles_written": 0,
        "windows_written": 0,
        "warnings": [],
    }

    if not my_profiles:
        result["action"] = "SKIP_NO_PROFILES"
        result["warnings"].append("No catalog.route_schedule_profile entries")
        return result

    # Compute cycle time for vehicle count
    cycle = compute_cycle_time(route_id, route_info, runtime_bindings, layover_policies)
    result["cycle_time"] = cycle

    # Get runtime for this direction
    binding = runtime_bindings.get((route_id, my_dir))
    runtime_secs = binding["runtime_offpeak_secs"] if binding else DEFAULT_RUNTIME_SECS
    if not binding:
        result["warnings"].append("No runtime binding — using default 3600s")

    for sp in my_profiles:
        pattern_id = sp["service_pattern_id"]
        days = my_days.get(pattern_id)

        if not days:
            result["warnings"].append(f"No service_days for pattern '{pattern_id}'")
            continue

        # Headway: from profile, or from service_days, or default 15 min
        headway_min = sp.get("headway_min") or days.get("days_headway_min") or 15
        headway_secs = headway_min * 60

        # Runtime override from catalog
        if sp.get("runtime_override_min"):
            runtime_secs = int(sp["runtime_override_min"] * 60)

        # Vehicle count from cycle time
        n_vehicles = max(1, math.ceil(cycle["cycle_time_min"] / headway_min))

        # Override if catalog specifies it
        if sp.get("estimated_vehicles") and sp["estimated_vehicles"] > 0:
            n_vehicles = sp["estimated_vehicles"]

        # Window times: from profile first, then service_days
        window_start = sp["window_start"] or days["first_departure"]
        window_end = sp["window_end"] or days["last_departure"]

        is_peak = sp.get("peak_type") == "peak"

        # Day-of-week flags from service_days
        day_flags = {
            "monday": bool(days["monday"]),
            "tuesday": bool(days["tuesday"]),
            "wednesday": bool(days["wednesday"]),
            "thursday": bool(days["thursday"]),
            "friday": bool(days["friday"]),
            "saturday": bool(days["saturday"]),
            "sunday": bool(days["sunday"]),
        }

        if dry_run:
            result["profiles_written"] += 1
            result["windows_written"] += 1
            result["action"] = "DRY_RUN"
            continue

        # Write profile
        profile_id = write_schedule_profile(
            cur, route_id, my_dir,
            pattern_id, runtime_secs, n_vehicles,
            dry_run=False,
        )

        if not profile_id:
            result["warnings"].append(f"Failed to write profile for {pattern_id}")
            continue

        result["profiles_written"] += 1

        # Write service window
        write_service_window(
            cur, profile_id,
            window_start, window_end,
            headway_secs=headway_secs,
            exact_departures=None,
            day_flags=day_flags,
            is_peak=is_peak,
            dry_run=False,
        )
        result["windows_written"] += 1

    if not dry_run and result["profiles_written"] > 0:
        result["action"] = "UPSERTED"
    elif dry_run:
        result["action"] = "DRY_RUN"
    else:
        result["action"] = "NO_OP"

    return result


# ------------------------------------------------------------------
# Diagnostic persistence
# ------------------------------------------------------------------

def persist_phase5_diagnostic(cur, run_id: str, results: list[dict]) -> None:
    """Save Phase 5 execution diagnostic."""
    for r in results:
        route_id = r["route_id"]
        has_warnings = bool(r.get("warnings"))
        status = "WARNINGS" if has_warnings else "CLEAN"

        cur.execute("""
            INSERT INTO automation.diagnostics (run_id, route_id, phase, status, report)
            VALUES (%s, %s::uuid, 'phase5_bridge', %s, %s::jsonb)
        """, (run_id, route_id, status, json.dumps(r, default=str)))


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Agent 2b — Phase 5 schedule bridge"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Process all approved routes")
    group.add_argument("--route", type=str, help="Single route_id UUID")
    group.add_argument("--batch", type=str, help="Comma-separated route_id UUIDs")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--skip-gate-check", action="store_true",
                        help="Skip Phase 4 diagnostic check")
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "_phase5"

    conn = psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor)
    conn.autocommit = False

    try:
        cur = conn.cursor()

        # Resolve route_ids
        target_ids = None
        if args.route:
            target_ids = [args.route]
        elif args.batch:
            target_ids = [r.strip() for r in args.batch.split(",")]

        # Get approved routes
        approved_ids = load_approved_routes(cur, target_ids)

        print(f"Agent 2b — Phase 5 Schedule Bridge")
        print(f"Run ID: {run_id}")
        print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
        print(f"Approved routes found: {len(approved_ids)}")
        print("=" * 70)

        if not approved_ids:
            print("\nNo approved routes in catalog. Nothing to do.")
            conn.rollback()
            return

        # Load all catalog data
        schedule_profiles = load_schedule_profiles(cur, approved_ids)
        service_days = load_service_days(cur, approved_ids)
        layover_policies = load_layover_policies(cur, approved_ids)
        runtime_bindings = load_runtime_bindings(cur, approved_ids)
        route_info = load_route_info(cur, approved_ids)

        print(f"Catalog data: {len(schedule_profiles)} profiles, "
              f"{len(service_days)} service_days, "
              f"{len(layover_policies)} layover policies")
        print(f"Runtime bindings: {len(runtime_bindings)}")
        print("-" * 70)

        results = []
        skipped = 0

        for route_id in approved_ids:
            # Gate check
            if not args.skip_gate_check and not args.dry_run:
                ok, reason = check_phase4_done(cur, route_id)
                if not ok:
                    print(f"  SKIP  {route_id[:12]}.. : {reason}")
                    skipped += 1
                    continue

            r = process_route(
                cur, route_id,
                schedule_profiles, service_days,
                layover_policies, runtime_bindings, route_info,
                dry_run=args.dry_run,
            )
            results.append(r)

            ct = r.get("cycle_time", {})
            cycle_str = f"cycle={ct.get('cycle_time_min', '?')}min" if ct else "cycle=?"
            warns = f" [{len(r.get('warnings', []))} warns]" if r.get("warnings") else ""
            print(f"  {r['action']:18s} d{r['direction_id']} | "
                  f"prof={r['profiles_written']} win={r['windows_written']} | "
                  f"{cycle_str}{warns}")

        # Calendar exceptions
        n_exceptions = write_calendar_exceptions(cur, approved_ids, args.dry_run)

        # Persist diagnostics
        if results and not args.dry_run:
            persist_phase5_diagnostic(cur, run_id, results)

        # Summary
        print()
        print("=" * 70)
        upserted = sum(1 for r in results if r["action"] == "UPSERTED")
        dry = sum(1 for r in results if r["action"] == "DRY_RUN")
        no_op = sum(1 for r in results if r["action"] in ("NO_OP", "SKIP_NO_PROFILES"))
        total_profiles = sum(r["profiles_written"] for r in results)
        total_windows = sum(r["windows_written"] for r in results)
        total_warns = sum(len(r.get("warnings", [])) for r in results)
        print(f"  Routes processed: {len(results)}")
        print(f"  Upserted:  {upserted}")
        print(f"  Skipped:   {skipped}")
        print(f"  No-op:     {no_op}")
        print(f"  Dry-run:   {dry}")
        print(f"  Profiles written:  {total_profiles}")
        print(f"  Windows written:   {total_windows}")
        print(f"  Calendar exceptions: {n_exceptions}")
        print(f"  Warnings:  {total_warns}")
        print("=" * 70)

        if args.dry_run:
            print("\nDRY RUN — no changes committed.")
            conn.rollback()
        else:
            conn.commit()
            print("\nAll changes committed.")

    except Exception as e:
        conn.rollback()
        print(f"\nERROR: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
