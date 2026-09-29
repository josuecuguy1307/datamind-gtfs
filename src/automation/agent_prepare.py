"""
Phase 4-5 Automation — Agent 1: PREPARE (Pre-execution diagnostic engine)

Deterministic Python script (NO LLM calls). Validates catalog data,
runtime model availability, and schedule feasibility for each route
before execution.

Usage:
    python src/automation/agent_prepare.py --all
    python src/automation/agent_prepare.py --route <route_id>
    python src/automation/agent_prepare.py --batch <id1>,<id2>,<id3>
    python src/automation/agent_prepare.py --all --persist
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import uuid
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

# Gate thresholds
HEADWAY_MIN_BOUND = 5
HEADWAY_MAX_BOUND = 60
CONFIDENCE_FLOOR = 0.60
CYCLE_TIME_MIN_BOUND = 30.0
CYCLE_TIME_MAX_BOUND = 360.0
VEHICLE_MAX_BOUND = 25
DEFAULT_LAYOVER_MIN = 5.0


# ------------------------------------------------------------------
# Data loaders
# ------------------------------------------------------------------

def load_route_semantics(cur, route_id: str | None = None) -> dict[str, dict]:
    """Load catalog.route_semantics rows, keyed by route_id string."""
    where = "WHERE route_id = %s" if route_id else ""
    params = (route_id,) if route_id else ()
    cur.execute(f"""
        SELECT route_id::text, operator, route_short_name, route_long_name,
               route_type, public_origin, public_destination, jurisdiction,
               evidence_source, confidence, approved, approved_by, approved_at, notes
        FROM catalog.route_semantics
        {where}
    """, params)
    return {row["route_id"]: dict(row) for row in cur.fetchall()}


def load_service_days(cur, route_ids: list[str]) -> dict[str, list[dict]]:
    """Load catalog.route_service_days, grouped by route_id."""
    if not route_ids:
        return {}
    cur.execute("""
        SELECT route_id::text, service_pattern_id,
               monday, tuesday, wednesday, thursday, friday, saturday, sunday,
               first_departure, last_departure, headway_min,
               valid_from, valid_to, source, confidence
        FROM catalog.route_service_days
        WHERE route_id = ANY(%s::uuid[])
        ORDER BY route_id, service_pattern_id
    """, (route_ids,))
    result: dict[str, list[dict]] = {}
    for row in cur.fetchall():
        rid = row["route_id"]
        result.setdefault(rid, []).append(dict(row))
    return result


def load_schedule_profiles(cur, route_ids: list[str]) -> dict[str, list[dict]]:
    """Load catalog.route_schedule_profile, grouped by route_id."""
    if not route_ids:
        return {}
    cur.execute("""
        SELECT route_id::text, direction_id, service_pattern_id,
               window_start, window_end, headway_min,
               exact_departures, runtime_override_min, peak_type,
               estimated_vehicles, cycle_time_min, source, confidence
        FROM catalog.route_schedule_profile
        WHERE route_id = ANY(%s::uuid[])
        ORDER BY route_id, direction_id, window_start
    """, (route_ids,))
    result: dict[str, list[dict]] = {}
    for row in cur.fetchall():
        rid = row["route_id"]
        result.setdefault(rid, []).append(dict(row))
    return result


def load_layover_policies(cur, route_ids: list[str]) -> dict[str, dict]:
    """Load catalog.route_layover_policy, keyed by route_id.
    Returns the 'all' pattern row (or first available)."""
    if not route_ids:
        return {}
    cur.execute("""
        SELECT route_id::text, layover_at_destination_min, layover_at_origin_min,
               min_layover_min, max_layover_min, applies_to_pattern,
               source, confidence
        FROM catalog.route_layover_policy
        WHERE route_id = ANY(%s::uuid[])
        ORDER BY route_id, applies_to_pattern
    """, (route_ids,))
    result: dict[str, dict] = {}
    for row in cur.fetchall():
        rid = row["route_id"]
        # Prefer 'all' pattern, keep first seen otherwise
        if rid not in result or row["applies_to_pattern"] == "all":
            result[rid] = dict(row)
    return result


def load_runtime_bindings(cur, route_ids: list[str]) -> dict[str, dict]:
    """Load runtime estimate bindings from gtfs_work, keyed by 'route_id:dir'."""
    if not route_ids:
        return {}
    cur.execute("""
        SELECT b.route_id::text, b.direction_id,
               COALESCE((e.metrics->>'runtime_offpeak_secs')::numeric, 0) AS runtime_offpeak_secs,
               COALESCE((e.metrics->>'runtime_peak_secs')::numeric, 0) AS runtime_peak_secs,
               COALESCE((e.metrics->>'route_len_m')::numeric, 0) AS route_len_m,
               COALESCE((e.metrics->>'n_legs')::int, 0) AS n_legs,
               COALESCE((e.metrics->>'confidence')::numeric, 0) AS model_confidence
        FROM gtfs_work.route_runtime_estimate_bindings b
        JOIN gtfs_work.runtime_route_estimates e ON e.estimate_id = b.estimate_id
        WHERE b.route_id = ANY(%s::uuid[])
        ORDER BY b.route_id, b.direction_id
    """, (route_ids,))
    result: dict[str, dict] = {}
    for row in cur.fetchall():
        key = f"{row['route_id']}:{row['direction_id']}"
        result[key] = dict(row)
    return result


def load_direction_pairs(cur, route_ids: list[str]) -> dict[str, dict]:
    """Load route_prod direction info for given route_ids AND their partner
    routes (same service_route_id, opposite direction)."""
    if not route_ids:
        return {}
    cur.execute("""
        WITH target AS (
            SELECT service_route_id FROM route_prod.routes
            WHERE route_id = ANY(%s::uuid[]) AND service_route_id IS NOT NULL
        )
        SELECT r.route_id::text, r.service_route_id::text, r.direction_id,
               array_length(r.stop_node_ids, 1) AS n_stops
        FROM route_prod.routes r
        WHERE r.service_route_id IN (SELECT service_route_id FROM target)
        ORDER BY r.service_route_id, r.direction_id
    """, (route_ids,))
    result: dict[str, dict] = {}
    for row in cur.fetchall():
        result[row["route_id"]] = dict(row)
    return result


def find_partner_route_id(route_id: str, all_route_info: dict[str, dict]) -> str | None:
    """Find the route_id of the partner direction (same service_route, opposite dir)."""
    info = all_route_info.get(route_id)
    if not info or not info.get("service_route_id"):
        return None
    srv = info["service_route_id"]
    my_dir = info["direction_id"]
    for rid, ri in all_route_info.items():
        if rid != route_id and ri.get("service_route_id") == srv and ri.get("direction_id") != my_dir:
            return rid
    return None


# ------------------------------------------------------------------
# Gate evaluation
# ------------------------------------------------------------------

def gate(rule: str, gate_type: str, passed: bool, detail: str) -> dict:
    return {
        "rule": rule,
        "gate": gate_type,
        "passed": passed,
        "detail": detail,
    }


def evaluate_gates(
    route_id: str,
    sem: dict | None,
    days: list[dict],
    profiles: list[dict],
    layover: dict | None,
    runtime_bindings: dict[str, dict],
    route_info: dict | None,
    partner_route_id: str | None = None,
    partner_profiles: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    """Run all pre-execution gates. Returns (gates, layover_analysis).

    'profiles' contains this route's catalog profiles.
    'partner_profiles' contains the opposite-direction route's profiles.
    Together they should cover direction 0 and 1.
    """
    gates: list[dict] = []
    layover_analysis: dict[str, Any] = {}

    # ============================================================
    # HARD GATES
    # ============================================================

    # H1: Route exists in semantics catalog
    gates.append(gate(
        "route_exists_in_semantics", "HARD",
        sem is not None,
        f"route_id={route_id[:12]}... {'found' if sem else 'NOT FOUND in catalog.route_semantics'}"
    ))
    if sem is None:
        return gates, layover_analysis

    # H2: Route has approved semantics
    gates.append(gate(
        "route_approved", "HARD",
        sem.get("approved", False),
        f"approved={sem.get('approved', False)}"
    ))

    # H3: At least 1 service pattern
    gates.append(gate(
        "has_service_days", "HARD",
        len(days) >= 1,
        f"{len(days)} service_days row(s)"
    ))

    # H4: Schedule profile exists for both directions
    # Combine this route's profiles with partner's to check coverage
    all_dir_profiles = list(profiles) + (partner_profiles or [])
    dir_ids_in_profiles = set(p["direction_id"] for p in all_dir_profiles)
    has_dir0 = 0 in dir_ids_in_profiles
    has_dir1 = 1 in dir_ids_in_profiles
    detail_h4 = f"direction_ids across pair: {sorted(dir_ids_in_profiles)}"
    if not partner_route_id:
        detail_h4 += " (no partner route found)"
    gates.append(gate(
        "schedule_profile_both_directions", "HARD",
        has_dir0 and has_dir1,
        detail_h4,
    ))

    # H5: window_start < window_end for all profiles
    bad_windows = [p for p in profiles if p["window_start"] >= p["window_end"]]
    gates.append(gate(
        "window_start_before_end", "HARD",
        len(bad_windows) == 0,
        f"{len(bad_windows)} profile(s) with window_start >= window_end" if bad_windows
        else "All windows valid"
    ))

    # H6: headway or exact_departures defined
    missing_schedule = [
        p for p in profiles
        if p.get("headway_min") is None and not p.get("exact_departures")
    ]
    gates.append(gate(
        "headway_or_departures_defined", "HARD",
        len(missing_schedule) == 0,
        f"{len(missing_schedule)} profile(s) missing both headway_min and exact_departures"
        if missing_schedule else "All profiles have headway or departures"
    ))

    # H7: At least one day active in service_days
    any_day_active = any(
        any(d.get(day) for day in ["monday", "tuesday", "wednesday", "thursday",
                                    "friday", "saturday", "sunday"])
        for d in days
    )
    gates.append(gate(
        "at_least_one_day_active", "HARD",
        any_day_active,
        "At least one day boolean is TRUE" if any_day_active
        else "No day boolean is TRUE in any service_days row"
    ))

    # H8: Layover within bounds (if policy exists)
    if layover:
        dest_ok = float(layover["min_layover_min"]) <= float(layover["layover_at_destination_min"]) <= float(layover["max_layover_min"])
        orig_ok = float(layover["min_layover_min"]) <= float(layover["layover_at_origin_min"]) <= float(layover["max_layover_min"])
        gates.append(gate(
            "layover_within_bounds", "HARD",
            dest_ok and orig_ok,
            f"dest={layover['layover_at_destination_min']}min, orig={layover['layover_at_origin_min']}min, "
            f"bounds=[{layover['min_layover_min']}, {layover['max_layover_min']}]"
        ))

    # ============================================================
    # COMPUTE: cycle_time, n_vehicles (needed for gates H9 + soft)
    # ============================================================

    # Runtime bindings are keyed as "route_id:direction_id" where route_id
    # is the actual route_prod.routes.route_id. Each route_id has ONE direction.
    # To get both directions we need this route AND its partner.
    my_dir = route_info["direction_id"] if route_info else 0
    rt_this = runtime_bindings.get(f"{route_id}:{my_dir}")
    rt_partner = None
    if partner_route_id:
        partner_dir = 1 - my_dir
        rt_partner = runtime_bindings.get(f"{partner_route_id}:{partner_dir}")

    # Assign to dir0/dir1 for cycle computation
    if my_dir == 0:
        rt_dir0, rt_dir1 = rt_this, rt_partner
    else:
        rt_dir0, rt_dir1 = rt_partner, rt_this

    runtime_dir0_min = float(rt_dir0["runtime_offpeak_secs"]) / 60.0 if rt_dir0 else None
    runtime_dir1_min = float(rt_dir1["runtime_offpeak_secs"]) / 60.0 if rt_dir1 else None

    lay_dest = float(layover["layover_at_destination_min"]) if layover else DEFAULT_LAYOVER_MIN
    lay_orig = float(layover["layover_at_origin_min"]) if layover else DEFAULT_LAYOVER_MIN

    cycle_time = None
    n_vehicles = None

    if runtime_dir0_min is not None and runtime_dir1_min is not None:
        cycle_time = runtime_dir0_min + lay_dest + runtime_dir1_min + lay_orig

        # Use first available headway for vehicle calculation
        headway = None
        for p in profiles:
            if p.get("headway_min"):
                headway = p["headway_min"]
                break

        if headway and headway > 0:
            n_vehicles = math.ceil(cycle_time / headway)

    layover_analysis = {
        "layover_at_destination_min": lay_dest,
        "layover_at_origin_min": lay_orig,
        "runtime_dir0_min": round(runtime_dir0_min, 1) if runtime_dir0_min else None,
        "runtime_dir1_min": round(runtime_dir1_min, 1) if runtime_dir1_min else None,
        "cycle_time_min": round(cycle_time, 1) if cycle_time else None,
        "n_vehicles_needed": n_vehicles,
        "headway_used_min": headway if n_vehicles else None,
    }

    # H9: Vehicle count >= 1
    if n_vehicles is not None:
        gates.append(gate(
            "vehicle_count_gte_1", "HARD",
            n_vehicles >= 1,
            f"n_vehicles={n_vehicles}"
        ))

    # ============================================================
    # SOFT GATES
    # ============================================================

    # S1: Headway between 5-60 min
    for p in profiles:
        h = p.get("headway_min")
        if h is not None:
            gates.append(gate(
                "headway_sane", "SOFT",
                HEADWAY_MIN_BOUND <= h <= HEADWAY_MAX_BOUND,
                f"headway_min={h} (dir={p['direction_id']}, window={p['window_start']})"
            ))

    # S2: All confidence >= 0.60
    low_conf_items = []
    if sem and sem.get("confidence") is not None and float(sem["confidence"]) < CONFIDENCE_FLOOR:
        low_conf_items.append(f"semantics={sem['confidence']}")
    for d in days:
        if d.get("confidence") is not None and float(d["confidence"]) < CONFIDENCE_FLOOR:
            low_conf_items.append(f"service_days/{d['service_pattern_id']}={d['confidence']}")
    for p in profiles:
        if p.get("confidence") is not None and float(p["confidence"]) < CONFIDENCE_FLOOR:
            low_conf_items.append(f"schedule/d{p['direction_id']}={p['confidence']}")
    if layover and layover.get("confidence") is not None and float(layover["confidence"]) < CONFIDENCE_FLOOR:
        low_conf_items.append(f"layover={layover['confidence']}")

    gates.append(gate(
        "all_confidence_gte_0.60", "SOFT",
        len(low_conf_items) == 0,
        f"{len(low_conf_items)} low-confidence items: {', '.join(low_conf_items[:5])}"
        if low_conf_items else "All confidence >= 0.60"
    ))

    # S3: Layover policy exists
    gates.append(gate(
        "layover_policy_exists", "SOFT",
        layover is not None,
        "Layover policy found" if layover else f"No layover policy — using default {DEFAULT_LAYOVER_MIN}min"
    ))

    # S4: Runtime model available both directions
    gates.append(gate(
        "runtime_model_both_directions", "SOFT",
        rt_dir0 is not None and rt_dir1 is not None,
        f"dir0={'bound' if rt_dir0 else 'MISSING'}, dir1={'bound' if rt_dir1 else 'MISSING'}"
        + (f" (partner={partner_route_id[:12]}..)" if partner_route_id else " (no partner)")
    ))

    # S5: Cycle time 30-360 min
    if cycle_time is not None:
        gates.append(gate(
            "cycle_time_sane", "SOFT",
            CYCLE_TIME_MIN_BOUND <= cycle_time <= CYCLE_TIME_MAX_BOUND,
            f"cycle_time={round(cycle_time, 1)}min"
        ))

    # S6: Vehicle count <= 25
    if n_vehicles is not None:
        gates.append(gate(
            "vehicle_count_lte_25", "SOFT",
            n_vehicles <= VEHICLE_MAX_BOUND,
            f"n_vehicles={n_vehicles}"
        ))

    # S7: Schedule covers full service window
    for d in days:
        first_dep = d.get("first_departure")
        last_dep = d.get("last_departure")
        pattern = d.get("service_pattern_id")
        matching_profiles = [
            p for p in profiles
            if p.get("service_pattern_id") == pattern
        ]
        if first_dep and matching_profiles:
            earliest_window = min(p["window_start"] for p in matching_profiles)
            latest_window = max(p["window_end"] for p in matching_profiles)
            covers = earliest_window <= first_dep and latest_window >= last_dep
            gates.append(gate(
                "schedule_covers_service_window", "SOFT",
                covers,
                f"pattern={pattern}: service={first_dep}-{last_dep}, "
                f"windows={earliest_window}-{latest_window}"
            ))

    return gates, layover_analysis


# ------------------------------------------------------------------
# Suggestion generator
# ------------------------------------------------------------------

def generate_suggestions(
    route_id: str,
    sem: dict | None,
    days: list[dict],
    profiles: list[dict],
    layover: dict | None,
    runtime_bindings: dict[str, dict],
) -> list[dict]:
    """Generate fill suggestions for missing/low-confidence data."""
    suggestions = []

    if sem is None:
        suggestions.append({
            "type": "catalog_fill",
            "target": "catalog.route_semantics",
            "detail": f"Route {route_id[:12]} not in catalog — needs manual entry",
            "requires_human_approval": True,
        })
        return suggestions

    if not sem.get("approved"):
        suggestions.append({
            "type": "approval_needed",
            "target": "catalog.route_semantics",
            "detail": f"Set approved=TRUE for {sem.get('route_short_name', route_id[:12])}",
            "requires_human_approval": True,
        })

    if len(days) == 0:
        suggestions.append({
            "type": "catalog_fill",
            "target": "catalog.route_service_days",
            "detail": "No service_days rows — run bootstrap or add manually",
            "requires_human_approval": True,
        })

    dir_ids = set(p["direction_id"] for p in profiles)
    for missing_dir in [0, 1]:
        if missing_dir not in dir_ids:
            suggestions.append({
                "type": "catalog_fill",
                "target": "catalog.route_schedule_profile",
                "detail": f"Missing profile for direction_id={missing_dir}",
                "proposed_values": {
                    "route_id": route_id,
                    "direction_id": missing_dir,
                    "service_pattern_id": "weekday_normal",
                    "window_start": "05:30",
                    "window_end": "20:00",
                    "headway_min": 15,
                    "source": "agent_suggestion",
                    "confidence": 0.20,
                },
                "requires_human_approval": True,
            })

    rt0 = runtime_bindings.get(f"{route_id}:0")
    rt1 = runtime_bindings.get(f"{route_id}:1")
    if rt0 is None or rt1 is None:
        missing = []
        if rt0 is None:
            missing.append("dir0")
        if rt1 is None:
            missing.append("dir1")
        suggestions.append({
            "type": "runtime_estimate_needed",
            "target": "gtfs_work.route_runtime_estimate_bindings",
            "detail": f"Missing runtime binding for {', '.join(missing)}. "
                      "Run estimate_runtime_from_catalog() via Runtime Lab.",
            "requires_human_approval": False,
        })

    return suggestions


# ------------------------------------------------------------------
# Report builder
# ------------------------------------------------------------------

def build_report(
    route_id: str,
    sem: dict | None,
    gates: list[dict],
    layover_analysis: dict,
    suggestions: list[dict],
    run_id: str,
) -> dict:
    """Build the structured diagnostic report."""
    hard_fails = [g for g in gates if g["gate"] == "HARD" and not g["passed"]]
    soft_warns = [g for g in gates if g["gate"] == "SOFT" and not g["passed"]]

    if hard_fails:
        status = "BLOCKED"
    elif soft_warns:
        status = "WARNINGS"
    else:
        status = "CLEAN"

    return {
        "run_id": run_id,
        "phase": "pre_execution",
        "route_id": route_id,
        "route_short_name": sem.get("route_short_name", "?") if sem else "?",
        "operator": sem.get("operator", "?") if sem else "?",
        "status": status,
        "hard_fails": len(hard_fails),
        "soft_warns": len(soft_warns),
        "gates": gates,
        "layover_analysis": layover_analysis,
        "suggestions": suggestions,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------

def persist_diagnostic(cur, report: dict) -> None:
    """Save diagnostic report to automation.diagnostics."""
    cur.execute("""
        INSERT INTO automation.diagnostics (run_id, route_id, phase, status, report)
        VALUES (%s, %s::uuid, %s, %s, %s::jsonb)
    """, (
        report["run_id"],
        report["route_id"],
        report["phase"],
        report["status"],
        json.dumps(report, default=str),
    ))


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def run_diagnostic(
    cur,
    route_ids: list[str],
    run_id: str,
    persist: bool = False,
) -> list[dict]:
    """Run diagnostic for a list of route_ids. Returns list of reports."""

    # Load all data in bulk
    all_sem = {}
    for rid in route_ids:
        sem = load_route_semantics(cur, rid)
        all_sem.update(sem)

    all_days = load_service_days(cur, route_ids)
    all_profiles = load_schedule_profiles(cur, route_ids)
    all_layover = load_layover_policies(cur, route_ids)
    all_route_info = load_direction_pairs(cur, route_ids)

    # Collect all route_ids including partners for runtime binding lookup
    all_related_ids = set(route_ids)
    for rid in route_ids:
        partner = find_partner_route_id(rid, all_route_info)
        if partner:
            all_related_ids.add(partner)
    all_runtime = load_runtime_bindings(cur, list(all_related_ids))

    # Also load partner profiles (for partners not already in target list)
    partner_ids = all_related_ids - set(route_ids)
    partner_profiles_extra = load_schedule_profiles(cur, list(partner_ids)) if partner_ids else {}

    reports = []
    for rid in route_ids:
        sem = all_sem.get(rid)
        days = all_days.get(rid, [])
        profiles = all_profiles.get(rid, [])
        layover = all_layover.get(rid)
        partner_rid = find_partner_route_id(rid, all_route_info)
        # Partner profiles: check already-loaded (if partner is in target list) or extras
        p_profiles = (
            all_profiles.get(partner_rid, [])
            or partner_profiles_extra.get(partner_rid, [])
        ) if partner_rid else []

        gates, layover_analysis = evaluate_gates(
            rid, sem, days, profiles, layover, all_runtime,
            all_route_info.get(rid),
            partner_route_id=partner_rid,
            partner_profiles=p_profiles,
        )

        suggestions = generate_suggestions(
            rid, sem, days, profiles, layover, all_runtime,
        )

        report = build_report(rid, sem, gates, layover_analysis, suggestions, run_id)
        reports.append(report)

        if persist:
            persist_diagnostic(cur, report)

    return reports


def resolve_route_ids(cur, args) -> list[str]:
    """Resolve CLI args to a list of route_id strings."""
    if args.all:
        cur.execute("SELECT route_id::text FROM catalog.route_semantics ORDER BY route_id")
        return [row["route_id"] for row in cur.fetchall()]
    elif args.route:
        return [args.route]
    elif args.batch:
        return [r.strip() for r in args.batch.split(",")]
    else:
        print("ERROR: Specify --all, --route, or --batch")
        sys.exit(1)


def print_summary(reports: list[dict]) -> None:
    """Print summary table to stdout."""
    print()
    header = f"{'route_id':>14s} | {'ref':>8s} | {'status':>8s} | {'hard':>4s} | {'soft':>4s} | {'vehicles':>8s} | {'cycle_min':>9s}"
    print(header)
    print("-" * len(header))

    for r in reports:
        rid = r["route_id"][:12] + ".."
        ref = r.get("route_short_name", "?")[:8]
        status = r["status"]
        hard = str(r["hard_fails"])
        soft = str(r["soft_warns"])
        la = r.get("layover_analysis", {})
        vehicles = str(la.get("n_vehicles_needed", "-"))
        cycle = str(la.get("cycle_time_min", "-"))
        print(f"{rid:>14s} | {ref:>8s} | {status:>8s} | {hard:>4s} | {soft:>4s} | {vehicles:>8s} | {cycle:>9s}")

    print()
    total = len(reports)
    clean = sum(1 for r in reports if r["status"] == "CLEAN")
    warnings = sum(1 for r in reports if r["status"] == "WARNINGS")
    blocked = sum(1 for r in reports if r["status"] == "BLOCKED")
    print(f"Total: {total}  |  CLEAN: {clean}  |  WARNINGS: {warnings}  |  BLOCKED: {blocked}")


def main():
    parser = argparse.ArgumentParser(
        description="Agent 1 — Pre-execution diagnostic for Phase 4-5 automation"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Diagnose all routes in catalog")
    group.add_argument("--route", type=str, help="Single route_id UUID")
    group.add_argument("--batch", type=str, help="Comma-separated route_id UUIDs")
    parser.add_argument("--persist", action="store_true",
                        help="Save diagnostics to automation.diagnostics table")
    parser.add_argument("--json", action="store_true",
                        help="Output full JSON reports to stdout")
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "_prepare"

    conn = psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor)
    conn.autocommit = False

    try:
        cur = conn.cursor()
        route_ids = resolve_route_ids(cur, args)

        print(f"Agent 1 — PREPARE diagnostic")
        print(f"Run ID: {run_id}")
        print(f"Routes: {len(route_ids)}")
        print("=" * 60)

        reports = run_diagnostic(cur, route_ids, run_id, persist=args.persist)

        if args.persist:
            conn.commit()
            print(f"\nDiagnostics persisted to automation.diagnostics ({len(reports)} rows)")
        else:
            conn.rollback()

        print_summary(reports)

        if args.json:
            print("\n--- FULL JSON REPORTS ---")
            print(json.dumps(reports, indent=2, default=str))

    except Exception as e:
        conn.rollback()
        print(f"\nERROR: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
