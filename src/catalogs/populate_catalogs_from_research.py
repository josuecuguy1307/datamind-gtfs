#!/usr/bin/env python3
"""
populate_catalogs_from_research.py
==================================
Reads three research JSON files and populates ALL five catalog tables
with real operational data for the 39 Valle de los Chillos routes.

Tables populated:
  1. catalog.route_semantics
  2. catalog.route_service_days
  3. catalog.route_schedule_profile   (per direction × per pattern)
  4. catalog.route_layover_policy
  5. catalog.route_service_exceptions (holidays)

All inserts are idempotent (ON CONFLICT ... DO UPDATE).

Usage:
    python populate_catalogs_from_research.py --dry-run   # preview only
    python populate_catalogs_from_research.py              # live write
"""

import argparse
import json
import os
import sys
import uuid
from datetime import date, time
from difflib import SequenceMatcher
from pathlib import Path

import psycopg2
import psycopg2.extras

# ── Config ──────────────────────────────────────────────────────────

DB_DSN = os.environ.get(
    "DB_DSN",
    "postgresql://localhost:5432/datamind_ml",
)

# Default JSON paths (override via CLI)
DEFAULT_SEMANTICS_JSON = os.path.expanduser(
    "~/Downloads/01_route_semantics (1).json"
)
DEFAULT_SERVICE_DAYS_JSON = os.path.expanduser(
    "~/Downloads/02_route_service_days (1).json"
)
DEFAULT_RUNTIME_JSON = os.path.expanduser(
    "~/Downloads/03_runtime_and_reference_data (1).json"
)

VALID_FROM = date(2026, 1, 1)

# ── Manual mapping ──────────────────────────────────────────────────
# Prod route names use "Operator – Description" format with em-dashes.
# Research route names use "Origin - Destination" format with hyphens.
# This manual map bridges the two naming conventions for known prod routes.
# key = research route_name, value = prod route_name (exact match)

MANUAL_MAP = {
    "Dean Bajo - Camal Conocoto - Armenia - Giron":
        "Libertadores del Valle – Dean Bajo – Armenia",
    "Marin - La Salle - Cuarteles - Amaguana":
        "Libertadores del Valle – La Salle – Amaguaña",
    "Integrado Ontaneda - Centro Conocoto":
        "Libertadores del Valle – Ontaneda Interna",
    "La Merced - El Tingo - Alangasi - Metro El Arbolito":
        "Termas Turis – La Merced vía El Tingo",
    "El Nacional - La Toglla - Guangopolo - Marin":
        "Termas Turis – Club El Nacional – La Merced",
    "Las Palmeras - San Carlos - Marin":
        "Termas Turis – La Merced por Puengasí",
    "La Merced - Supermaxi San Gabriel":
        "Termas Turis – Salesiana – La Merced",
    "Sangolqui - ESPE - Autopista - Isabel la Catolica":
        "Vingala – Selva Alegre",
    "Marin - E35 - Cashapamba - Pintag":
        "ExpreAntisana – San Alfonso",
    "Pintag - E35 - Autopista - Metro El Arbolito":
        "ExpreAntisana – San Alfonso",
}

# ── Step 0: Route ID resolution ────────────────────────────────────

def load_route_prod_routes(cur):
    """Load all routes from route_prod.routes for fuzzy matching."""
    cur.execute("""
        SELECT route_id, route_name, service_route_id, direction_id,
               route_aliases
        FROM route_prod.routes
        ORDER BY route_name
    """)
    return cur.fetchall()


def normalize_for_match(s):
    """Normalize a string for fuzzy matching: lowercase, strip accents, trim."""
    import unicodedata
    s = s.lower().strip()
    # Replace em-dash with hyphen
    s = s.replace("–", "-").replace("—", "-")
    # Strip accents
    nfkd = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Normalize whitespace
    s = " ".join(s.split())
    return s


def fuzzy_match_score(a, b):
    return SequenceMatcher(None, normalize_for_match(a), normalize_for_match(b)).ratio()


def resolve_route_ids(research_routes, prod_routes):
    """
    For each research route_name, find the best-matching route_prod.routes entry.
    Uses manual mapping first, then fuzzy matching against route_name + aliases.
    Returns dict: research_route_name -> {service_route_id, directions, score, prod_name}.
    """
    resolved = {}
    unresolved = []

    # Build lookup by service_route_id for grouping directions
    by_service_route = {}
    for r in prod_routes:
        sr_id = str(r["service_route_id"])
        if sr_id not in by_service_route:
            by_service_route[sr_id] = []
        by_service_route[sr_id].append(r)

    # Build prod_name -> service_route_id lookup
    name_to_sr = {}
    for sr_id, directions in by_service_route.items():
        for d in directions:
            pname = d["route_name"]
            if pname:
                name_to_sr[pname] = sr_id
            for alias in (d.get("route_aliases") or []):
                if alias:
                    name_to_sr[alias] = sr_id

    for rr in research_routes:
        rname = rr["route_name"]
        if rr.get("_action", "").startswith("SKIP"):
            continue

        # 1) Check manual map first
        manual_prod_name = MANUAL_MAP.get(rname)
        if manual_prod_name and manual_prod_name in name_to_sr:
            sr_id = name_to_sr[manual_prod_name]
            directions = by_service_route[sr_id]
            resolved[rname] = {
                "service_route_id": sr_id,
                "directions": [
                    {
                        "route_id": str(d["route_id"]),
                        "direction_id": d["direction_id"],
                    }
                    for d in sorted(directions, key=lambda x: x["direction_id"])
                ],
                "score": 1.0,
                "prod_name": manual_prod_name,
                "match_method": "manual_map",
            }
            continue

        # 2) Fuzzy match against all prod names + aliases
        best_score = 0
        best_sr_id = None
        best_prod_name = None

        for sr_id, directions in by_service_route.items():
            candidates = []
            for d in directions:
                if d["route_name"]:
                    candidates.append(d["route_name"])
                for alias in (d.get("route_aliases") or []):
                    if alias:
                        candidates.append(alias)

            for cand in set(candidates):
                score = fuzzy_match_score(rname, cand)
                if score > best_score:
                    best_score = score
                    best_sr_id = sr_id
                    best_prod_name = cand

        if best_score >= 0.70 and best_sr_id:
            directions = by_service_route[best_sr_id]
            resolved[rname] = {
                "service_route_id": best_sr_id,
                "directions": [
                    {
                        "route_id": str(d["route_id"]),
                        "direction_id": d["direction_id"],
                    }
                    for d in sorted(directions, key=lambda x: x["direction_id"])
                ],
                "score": round(best_score, 3),
                "prod_name": best_prod_name,
                "match_method": "fuzzy",
            }
        else:
            unresolved.append((rname, best_score, best_prod_name))

    return resolved, unresolved


# ── Step 1: route_semantics ─────────────────────────────────────────

SQL_UPSERT_SEMANTICS = """
INSERT INTO catalog.route_semantics (
    route_id, operator, route_short_name, route_long_name,
    route_type, public_origin, public_destination,
    jurisdiction, evidence_source, confidence, approved, notes
) VALUES (
    %(route_id)s, %(operator)s, %(route_short_name)s, %(route_long_name)s,
    %(route_type)s, %(public_origin)s, %(public_destination)s,
    %(jurisdiction)s, %(evidence_source)s, %(confidence)s, %(approved)s, %(notes)s
)
ON CONFLICT (route_id) DO UPDATE SET
    operator = EXCLUDED.operator,
    route_short_name = EXCLUDED.route_short_name,
    route_long_name = EXCLUDED.route_long_name,
    route_type = EXCLUDED.route_type,
    public_origin = EXCLUDED.public_origin,
    public_destination = EXCLUDED.public_destination,
    jurisdiction = EXCLUDED.jurisdiction,
    evidence_source = EXCLUDED.evidence_source,
    confidence = EXCLUDED.confidence,
    approved = EXCLUDED.approved,
    notes = EXCLUDED.notes
"""


def write_semantics(cur, research_routes, resolved, dry_run=False):
    """Upsert catalog.route_semantics for every resolved route direction."""
    count = 0
    skipped = 0
    for rr in research_routes:
        rname = rr["route_name"]
        if rr.get("_action", "").startswith("SKIP"):
            skipped += 1
            continue
        match = resolved.get(rname)
        if not match:
            continue

        for d in match["directions"]:
            params = {
                "route_id": d["route_id"],
                "operator": rr["operator"],
                "route_short_name": rr["route_short_name"],
                "route_long_name": rr["route_long_name"],
                "route_type": rr.get("route_type", 3),
                "public_origin": rr["public_origin"],
                "public_destination": rr["public_destination"],
                "jurisdiction": rr["jurisdiction"],
                "evidence_source": rr["evidence_source"],
                "confidence": rr.get("confidence"),
                "approved": rr.get("approved", False),
                "notes": rr.get("notes"),
            }
            if not dry_run:
                cur.execute(SQL_UPSERT_SEMANTICS, params)
            count += 1

    return count, skipped


# ── Step 2: route_service_days ──────────────────────────────────────

SQL_UPSERT_SERVICE_DAYS = """
INSERT INTO catalog.route_service_days (
    route_id, service_pattern_id,
    monday, tuesday, wednesday, thursday, friday, saturday, sunday,
    first_departure, last_departure, headway_min,
    valid_from, source, confidence
) VALUES (
    %(route_id)s, %(service_pattern_id)s,
    %(monday)s, %(tuesday)s, %(wednesday)s, %(thursday)s, %(friday)s,
    %(saturday)s, %(sunday)s,
    %(first_departure)s, %(last_departure)s, %(headway_min)s,
    %(valid_from)s, %(source)s, %(confidence)s
)
ON CONFLICT (route_id, service_pattern_id) DO UPDATE SET
    monday = EXCLUDED.monday,
    tuesday = EXCLUDED.tuesday,
    wednesday = EXCLUDED.wednesday,
    thursday = EXCLUDED.thursday,
    friday = EXCLUDED.friday,
    saturday = EXCLUDED.saturday,
    sunday = EXCLUDED.sunday,
    first_departure = EXCLUDED.first_departure,
    last_departure = EXCLUDED.last_departure,
    headway_min = EXCLUDED.headway_min,
    valid_from = EXCLUDED.valid_from,
    source = EXCLUDED.source,
    confidence = EXCLUDED.confidence
"""


def parse_time(s):
    """Parse 'HH:MM' string to time object."""
    parts = s.split(":")
    return time(int(parts[0]), int(parts[1]))


def write_service_days(cur, service_days_data, resolved, dry_run=False):
    """Upsert catalog.route_service_days."""
    count = 0
    for entry in service_days_data:
        if "_comment" in entry and "route_name" not in entry:
            continue
        if "_pattern_template" in entry and "route_name" not in entry:
            continue
        rname = entry.get("route_name")
        if not rname:
            continue
        match = resolved.get(rname)
        if not match:
            continue

        patterns = entry.get("patterns", [])
        for pat in patterns:
            days = pat["days"]
            for d in match["directions"]:
                params = {
                    "route_id": d["route_id"],
                    "service_pattern_id": pat["service_pattern_id"],
                    "monday": days.get("mon", False),
                    "tuesday": days.get("tue", False),
                    "wednesday": days.get("wed", False),
                    "thursday": days.get("thu", False),
                    "friday": days.get("fri", False),
                    "saturday": days.get("sat", False),
                    "sunday": days.get("sun", False),
                    "first_departure": parse_time(pat["first_departure"]),
                    "last_departure": parse_time(pat["last_departure"]),
                    "headway_min": pat.get("headway_min"),
                    "valid_from": VALID_FROM,
                    "source": pat.get("source", "research"),
                    "confidence": pat.get("confidence"),
                }
                if not dry_run:
                    cur.execute(SQL_UPSERT_SERVICE_DAYS, params)
                count += 1

    return count


# ── Step 3: route_schedule_profile ──────────────────────────────────

SQL_UPSERT_SCHEDULE_PROFILE = """
INSERT INTO catalog.route_schedule_profile (
    route_id, direction_id, service_pattern_id,
    window_start, window_end, headway_min,
    runtime_override_min, runtime_override_reason,
    estimated_vehicles, cycle_time_min,
    source, confidence, notes
) VALUES (
    %(route_id)s, %(direction_id)s, %(service_pattern_id)s,
    %(window_start)s, %(window_end)s, %(headway_min)s,
    %(runtime_override_min)s, %(runtime_override_reason)s,
    %(estimated_vehicles)s, %(cycle_time_min)s,
    %(source)s, %(confidence)s, %(notes)s
)
ON CONFLICT (route_id, direction_id, service_pattern_id, window_start) DO UPDATE SET
    window_end = EXCLUDED.window_end,
    headway_min = EXCLUDED.headway_min,
    runtime_override_min = EXCLUDED.runtime_override_min,
    runtime_override_reason = EXCLUDED.runtime_override_reason,
    estimated_vehicles = EXCLUDED.estimated_vehicles,
    cycle_time_min = EXCLUDED.cycle_time_min,
    source = EXCLUDED.source,
    confidence = EXCLUDED.confidence,
    notes = EXCLUDED.notes
"""


def compute_cycle_and_vehicles(runtime_ida, runtime_vuelta, layover_dest, layover_origin, headway_min):
    """
    cycle = runtime_dir0 + layover_dest + runtime_dir1 + layover_origin
    n_vehicles = ceil(cycle / headway)
    """
    if runtime_ida is None or runtime_vuelta is None:
        return None, None
    cycle = runtime_ida + layover_dest + runtime_vuelta + layover_origin
    if headway_min and headway_min > 0:
        import math
        n_vehicles = math.ceil(cycle / headway_min)
    else:
        n_vehicles = None
    return round(cycle, 1), n_vehicles


def write_schedule_profiles(cur, service_days_data, runtime_data, resolved, dry_run=False):
    """Upsert catalog.route_schedule_profile — one row per direction × pattern."""
    # Build runtime lookup by route_name
    runtime_by_name = {}
    for rt in runtime_data.get("runtime_references", []):
        runtime_by_name[rt["route_name"]] = rt

    # Build layover policy
    layover_cfg = runtime_data.get("layover_defaults", {})
    long_threshold = layover_cfg.get("long_route_threshold_min", 60)
    standard = layover_cfg.get("standard", {})
    long_rt = layover_cfg.get("long_route", {})

    # Fleet data
    fleet = runtime_data.get("fleet_data", {})

    count = 0
    for entry in service_days_data:
        if "route_name" not in entry:
            continue
        rname = entry["route_name"]
        match = resolved.get(rname)
        if not match:
            continue

        rt = runtime_by_name.get(rname, {})
        runtime_ida = rt.get("runtime_ida")
        runtime_vuelta = rt.get("runtime_vuelta")
        rt_source = rt.get("source", "")

        # Determine layover
        max_rt = max(runtime_ida or 0, runtime_vuelta or 0)
        if max_rt >= long_threshold:
            lay_dest = long_rt.get("layover_at_destination_min", 8.0)
            lay_orig = long_rt.get("layover_at_origin_min", 8.0)
        else:
            lay_dest = standard.get("layover_at_destination_min", 5.0)
            lay_orig = standard.get("layover_at_origin_min", 5.0)

        fleet_size = fleet.get(rname)

        patterns = entry.get("patterns", [])
        for pat in patterns:
            headway = pat.get("headway_min")
            cycle, n_veh = compute_cycle_and_vehicles(
                runtime_ida, runtime_vuelta, lay_dest, lay_orig, headway
            )
            # Use fleet data if available (override computed)
            est_vehicles = fleet_size if fleet_size else n_veh

            notes_parts = []
            if rt_source:
                notes_parts.append(f"runtime_source={rt_source}")
            if fleet_size:
                notes_parts.append(f"fleet_data={fleet_size}")

            for d in match["directions"]:
                dir_id = d["direction_id"]
                # Pick runtime for this direction
                if dir_id == 0:
                    rt_override = runtime_ida
                else:
                    rt_override = runtime_vuelta

                params = {
                    "route_id": d["route_id"],
                    "direction_id": dir_id,
                    "service_pattern_id": pat["service_pattern_id"],
                    "window_start": parse_time(pat["first_departure"]),
                    "window_end": parse_time(pat["last_departure"]),
                    "headway_min": headway,
                    "runtime_override_min": rt_override,
                    "runtime_override_reason": rt_source if rt_override else None,
                    "estimated_vehicles": est_vehicles,
                    "cycle_time_min": cycle,
                    "source": pat.get("source", "research"),
                    "confidence": pat.get("confidence"),
                    "notes": "; ".join(notes_parts) if notes_parts else None,
                }
                if not dry_run:
                    cur.execute(SQL_UPSERT_SCHEDULE_PROFILE, params)
                count += 1

    return count


# ── Step 4: route_layover_policy ────────────────────────────────────

SQL_UPSERT_LAYOVER = """
INSERT INTO catalog.route_layover_policy (
    route_id, layover_at_destination_min, layover_at_origin_min,
    min_layover_min, max_layover_min,
    applies_to_pattern, source, confidence, notes
) VALUES (
    %(route_id)s, %(layover_dest)s, %(layover_orig)s,
    %(min_lay)s, %(max_lay)s,
    %(applies_to)s, %(source)s, %(confidence)s, %(notes)s
)
ON CONFLICT (route_id, applies_to_pattern) DO UPDATE SET
    layover_at_destination_min = EXCLUDED.layover_at_destination_min,
    layover_at_origin_min = EXCLUDED.layover_at_origin_min,
    min_layover_min = EXCLUDED.min_layover_min,
    max_layover_min = EXCLUDED.max_layover_min,
    source = EXCLUDED.source,
    confidence = EXCLUDED.confidence,
    notes = EXCLUDED.notes
"""


def write_layover_policies(cur, research_routes, runtime_data, resolved, dry_run=False):
    """Upsert catalog.route_layover_policy — one row per route (all directions share)."""
    runtime_by_name = {}
    for rt in runtime_data.get("runtime_references", []):
        runtime_by_name[rt["route_name"]] = rt

    layover_cfg = runtime_data.get("layover_defaults", {})
    long_threshold = layover_cfg.get("long_route_threshold_min", 60)
    standard = layover_cfg.get("standard", {})
    long_rt = layover_cfg.get("long_route", {})

    count = 0
    seen_route_ids = set()

    for rr in research_routes:
        rname = rr["route_name"]
        if rr.get("_action", "").startswith("SKIP"):
            continue
        match = resolved.get(rname)
        if not match:
            continue

        rt = runtime_by_name.get(rname, {})
        max_rt = max(rt.get("runtime_ida") or 0, rt.get("runtime_vuelta") or 0)

        if max_rt >= long_threshold:
            cfg = long_rt
            policy_note = f"long_route (max_runtime={max_rt}min >= {long_threshold}min threshold)"
        else:
            cfg = standard
            policy_note = f"standard (max_runtime={max_rt}min < {long_threshold}min threshold)"

        for d in match["directions"]:
            rid = d["route_id"]
            if rid in seen_route_ids:
                continue
            seen_route_ids.add(rid)

            params = {
                "route_id": rid,
                "layover_dest": cfg.get("layover_at_destination_min", 5.0),
                "layover_orig": cfg.get("layover_at_origin_min", 5.0),
                "min_lay": cfg.get("min", 3.0),
                "max_lay": cfg.get("max", 15.0),
                "applies_to": "all",
                "source": "research_defaults",
                "confidence": 0.60,
                "notes": policy_note,
            }
            if not dry_run:
                cur.execute(SQL_UPSERT_LAYOVER, params)
            count += 1

    return count


# ── Step 5: route_service_exceptions (holidays) ────────────────────

SQL_UPSERT_EXCEPTION = """
INSERT INTO catalog.route_service_exceptions (
    route_id, exception_date, exception_type, reason
) VALUES (
    %(route_id)s, %(exception_date)s, %(exception_type)s, %(reason)s
)
ON CONFLICT (route_id, exception_date) DO UPDATE SET
    exception_type = EXCLUDED.exception_type,
    reason = EXCLUDED.reason
"""


def write_service_exceptions(cur, runtime_data, resolved, dry_run=False):
    """
    Insert exception_type=2 (service removed) for every Ecuador 2026 holiday
    for every resolved route_id.
    """
    holidays = runtime_data.get("holidays", [])
    if not holidays:
        return 0

    count = 0
    all_route_ids = set()
    for match in resolved.values():
        for d in match["directions"]:
            all_route_ids.add(d["route_id"])

    for rid in sorted(all_route_ids):
        for h in holidays:
            params = {
                "route_id": rid,
                "exception_date": h["date"],
                "exception_type": 2,  # service removed
                "reason": f"Holiday: {h['name']}",
            }
            if not dry_run:
                cur.execute(SQL_UPSERT_EXCEPTION, params)
            count += 1

    return count


# ── Step 6: Summary report ─────────────────────────────────────────

def print_resolution_report(resolved, unresolved, research_routes):
    """Print route resolution summary."""
    print("\n" + "=" * 70)
    print("STEP 0: ROUTE ID RESOLUTION")
    print("=" * 70)

    non_skip = [r for r in research_routes if not r.get("_action", "").startswith("SKIP")]
    print(f"  Research routes:     {len(non_skip)} (+ {len(research_routes) - len(non_skip)} skipped)")
    print(f"  Resolved:            {len(resolved)}")
    print(f"  Unresolved:          {len(unresolved)}")

    if resolved:
        print(f"\n  {'Research Name':<55} {'Score':>5}  {'Method':<8} {'Prod Name'}")
        print(f"  {'-'*55} {'-----':>5}  {'-'*8} {'-'*40}")
        for rname, info in sorted(resolved.items()):
            dirs = len(info["directions"])
            method = info.get("match_method", "?")
            print(f"  {rname:<55} {info['score']:>5.3f}  {method:<8} {info['prod_name']} ({dirs}d)")

    if unresolved:
        print(f"\n  UNRESOLVED:")
        for rname, score, pname in unresolved:
            print(f"    {rname:<55} best={score:.3f}  ({pname})")


def print_summary(counts):
    """Print final population summary."""
    print("\n" + "=" * 70)
    print("POPULATION SUMMARY")
    print("=" * 70)
    for label, n in counts.items():
        print(f"  {label:<40} {n:>5} rows")
    print("=" * 70)


# ── Main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Populate catalog tables from research JSON")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--semantics-json", default=DEFAULT_SEMANTICS_JSON)
    parser.add_argument("--service-days-json", default=DEFAULT_SERVICE_DAYS_JSON)
    parser.add_argument("--runtime-json", default=DEFAULT_RUNTIME_JSON)
    args = parser.parse_args()

    # Load JSON data
    print("Loading research JSON files...")
    with open(args.semantics_json) as f:
        research_routes = json.load(f)
    with open(args.service_days_json) as f:
        service_days_data = json.load(f)
    with open(args.runtime_json) as f:
        runtime_data = json.load(f)

    print(f"  01_route_semantics:  {len(research_routes)} entries")
    print(f"  02_service_days:     {len(service_days_data)} entries")
    print(f"  03_runtime_data:     {len(runtime_data.get('runtime_references', []))} runtime refs, "
          f"{len(runtime_data.get('holidays', []))} holidays")

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"\nMode: {mode}")

    conn = psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn.cursor() as cur:
            # Step 0: Resolve route IDs
            print("\nResolving route IDs from route_prod.routes...")
            prod_routes = load_route_prod_routes(cur)
            print(f"  Found {len(prod_routes)} rows in route_prod.routes")

            resolved, unresolved = resolve_route_ids(research_routes, prod_routes)
            print_resolution_report(resolved, unresolved, research_routes)

            if not resolved:
                print("\nERROR: No routes resolved. Cannot populate catalogs.")
                sys.exit(1)

            # Step 1: route_semantics
            print(f"\n--- Step 1: catalog.route_semantics ---")
            sem_count, sem_skipped = write_semantics(cur, research_routes, resolved, args.dry_run)
            print(f"  Upserted: {sem_count} rows  (skipped: {sem_skipped})")

            # Step 2: route_service_days
            print(f"\n--- Step 2: catalog.route_service_days ---")
            sd_count = write_service_days(cur, service_days_data, resolved, args.dry_run)
            print(f"  Upserted: {sd_count} rows")

            # Step 3: route_schedule_profile
            print(f"\n--- Step 3: catalog.route_schedule_profile ---")
            sp_count = write_schedule_profiles(cur, service_days_data, runtime_data, resolved, args.dry_run)
            print(f"  Upserted: {sp_count} rows")

            # Step 4: route_layover_policy
            print(f"\n--- Step 4: catalog.route_layover_policy ---")
            lp_count = write_layover_policies(cur, research_routes, runtime_data, resolved, args.dry_run)
            print(f"  Upserted: {lp_count} rows")

            # Step 5: route_service_exceptions
            print(f"\n--- Step 5: catalog.route_service_exceptions ---")
            ex_count = write_service_exceptions(cur, runtime_data, resolved, args.dry_run)
            print(f"  Upserted: {ex_count} rows")

            if not args.dry_run:
                conn.commit()
                print("\n** COMMITTED **")
            else:
                conn.rollback()
                print("\n** DRY-RUN — rolled back **")

        # Summary
        print_summary({
            "catalog.route_semantics": sem_count,
            "catalog.route_service_days": sd_count,
            "catalog.route_schedule_profile": sp_count,
            "catalog.route_layover_policy": lp_count,
            "catalog.route_service_exceptions": ex_count,
            "TOTAL": sem_count + sd_count + sp_count + lp_count + ex_count,
        })

    finally:
        conn.close()


if __name__ == "__main__":
    main()
