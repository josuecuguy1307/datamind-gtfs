"""
Phase 4-5 Automation — Bootstrap Catalog Tables

Populates catalog.route_semantics, catalog.route_service_days,
catalog.route_schedule_profile, and catalog.route_layover_policy
from existing data in route_prod + route_prod.route_semantics +
gtfs_work.agency_catalog.

Safe to re-run (INSERT ON CONFLICT DO NOTHING / skip higher-confidence rows).

Usage:
    python src/automation/bootstrap_catalogs.py
    python src/automation/bootstrap_catalogs.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

DB_DSN = os.environ.get("DB_DSN") or os.environ.get("DATABASE_URL") or \
    "postgresql://localhost:5432/datamind_ml"

TODAY = date.today().isoformat()

# Default schedule parameters (placeholders — will be refined later)
DEFAULT_WEEKDAY_FIRST_DEP = "05:30:00"
DEFAULT_WEEKDAY_LAST_DEP = "20:00:00"
DEFAULT_SATURDAY_FIRST_DEP = "06:00:00"
DEFAULT_SATURDAY_LAST_DEP = "18:00:00"
DEFAULT_HEADWAY_MIN = 15
DEFAULT_LAYOVER_MIN = 5.0
DEFAULT_MIN_LAYOVER = 3.0
DEFAULT_MAX_LAYOVER = 15.0
BOOTSTRAP_CONFIDENCE = 0.30
SEMANTICS_CONFIDENCE = 0.60

# Jurisdiction mapping: operator_name -> jurisdiction
# Códigos de jurisdicción: ejemplos; define los de tu región
JURISDICTION_MAP = {
    "ExpreAntisana": "DMQ",
    "Libertadores del Valle": "DMQ",
    "Termas Turis": "DMQ",
    "Vingala": "ANT",
    "CondorVall": "ANT",
    "Los Chillos": "DMQ",
    "San Pedro de Amaguaña": "ANT",
    "Azblan": "DMQ",
    "CALSIG Express": "ANT",
    "Marco Polo": "DMQ",
}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def parse_origin_destination(route_name: str, operator_name: str | None = None) -> tuple[str, str]:
    """Extract origin and destination from route names like 'Operator – A – B'.

    Heuristic: split on ' – ' (en-dash with spaces). If the first token
    matches the operator name, skip it and use remaining parts.
    """
    if not route_name:
        return ("Unknown", "Unknown")

    # Split on en-dash (–) or em-dash (—) or plain hyphen surrounded by spaces
    parts = []
    for sep in [" – ", " — ", " - "]:
        if sep in route_name:
            parts = [p.strip() for p in route_name.split(sep)]
            break

    if not parts:
        return (route_name, route_name)

    # If first part matches operator, strip it
    if operator_name and parts[0].lower() == operator_name.lower():
        parts = parts[1:]

    if len(parts) >= 2:
        return (parts[0], parts[-1])
    elif len(parts) == 1:
        return (parts[0], parts[0])
    else:
        return (route_name, route_name)


def lookup_jurisdiction(operator_name: Optional[str]) -> str:
    """Map operator to jurisdiction. Default to DMQ if unknown."""
    if not operator_name:
        return "DMQ"
    return JURISDICTION_MAP.get(operator_name, "DMQ")


# ------------------------------------------------------------------
# Data loading
# ------------------------------------------------------------------

def load_prod_routes(cur) -> list[dict]:
    """Load all route_prod.routes with their semantics and agency info."""
    cur.execute("""
        SELECT
            r.route_id,
            r.service_route_id,
            r.direction_id,
            r.route_name,
            r.human_verified,
            array_length(r.stop_node_ids, 1) AS n_stops,
            -- route_prod.route_semantics
            rs.route_ref,
            rs.route_name AS sem_route_name,
            rs.operator_name,
            rs.naming_confidence,
            rs.human_verified AS sem_human_verified
        FROM route_prod.routes r
        LEFT JOIN route_prod.route_semantics rs ON rs.route_id = r.route_id
        ORDER BY rs.operator_name NULLS LAST, r.route_name NULLS LAST
    """)
    return cur.fetchall()


# ------------------------------------------------------------------
# Bootstrap functions
# ------------------------------------------------------------------

def bootstrap_route_semantics(cur, routes: list[dict], dry_run: bool) -> tuple[int, set]:
    """Populate catalog.route_semantics from route_prod data.
    Returns (insert_count, set_of_route_ids_in_catalog).
    """
    inserted = 0
    skipped = 0
    bootstrapped_ids: set = set()

    for r in routes:
        route_id = r["route_id"]
        route_name = r["sem_route_name"] or r["route_name"] or ""
        operator = r["operator_name"] or "Unknown"
        route_ref = r["route_ref"] or f"R-{str(route_id)[:8]}"

        if not route_name:
            route_name = f"Route {route_ref}"

        origin, destination = parse_origin_destination(route_name, r["operator_name"])
        jurisdiction = lookup_jurisdiction(r["operator_name"])

        confidence = SEMANTICS_CONFIDENCE
        if r["naming_confidence"] and r["naming_confidence"] > 0:
            confidence = min(r["naming_confidence"], 1.0)

        evidence = "route_prod_semantics" if r["sem_route_name"] else "route_prod_name_only"

        if dry_run:
            print(f"  [DRY] route_semantics: {route_ref:8s} {operator:25s} | {origin} -> {destination}")
            inserted += 1
            bootstrapped_ids.add(route_id)
            continue

        cur.execute("""
            INSERT INTO catalog.route_semantics (
                route_id, operator, route_short_name, route_long_name,
                route_type, public_origin, public_destination,
                jurisdiction, evidence_source, confidence, approved
            ) VALUES (
                %(route_id)s, %(operator)s, %(short_name)s, %(long_name)s,
                3, %(origin)s, %(destination)s,
                %(jurisdiction)s, %(evidence)s, %(confidence)s, FALSE
            )
            ON CONFLICT (route_id) DO NOTHING
        """, {
            "route_id": route_id,
            "operator": operator,
            "short_name": route_ref,
            "long_name": route_name,
            "origin": origin,
            "destination": destination,
            "jurisdiction": jurisdiction,
            "evidence": evidence,
            "confidence": confidence,
        })

        if cur.rowcount > 0:
            inserted += 1
            bootstrapped_ids.add(route_id)
            print(f"  + route_semantics: {route_ref:8s} {operator:25s} | {origin} -> {destination}")
        else:
            skipped += 1
            bootstrapped_ids.add(route_id)  # already exists = still valid
            print(f"  = route_semantics: {route_ref:8s} (already exists, skipped)")

    return inserted, bootstrapped_ids


def bootstrap_service_days(cur, routes: list[dict], catalog_ids: set, dry_run: bool) -> int:
    """Populate catalog.route_service_days with default patterns."""
    inserted = 0

    patterns = [
        {
            "service_pattern_id": "weekday_normal",
            "monday": True, "tuesday": True, "wednesday": True,
            "thursday": True, "friday": True, "saturday": False, "sunday": False,
            "first_departure": DEFAULT_WEEKDAY_FIRST_DEP,
            "last_departure": DEFAULT_WEEKDAY_LAST_DEP,
        },
        {
            "service_pattern_id": "saturday_reduced",
            "monday": False, "tuesday": False, "wednesday": False,
            "thursday": False, "friday": False, "saturday": True, "sunday": False,
            "first_departure": DEFAULT_SATURDAY_FIRST_DEP,
            "last_departure": DEFAULT_SATURDAY_LAST_DEP,
        },
    ]

    for r in routes:
        route_id = r["route_id"]
        if route_id not in catalog_ids:
            continue

        for pat in patterns:
            if dry_run:
                print(f"  [DRY] service_days: {str(route_id)[:8]} / {pat['service_pattern_id']}")
                inserted += 1
                continue

            cur.execute("""
                INSERT INTO catalog.route_service_days (
                    route_id, service_pattern_id,
                    monday, tuesday, wednesday, thursday, friday, saturday, sunday,
                    first_departure, last_departure,
                    valid_from, source, confidence
                ) VALUES (
                    %(route_id)s, %(pattern)s,
                    %(mon)s, %(tue)s, %(wed)s, %(thu)s, %(fri)s, %(sat)s, %(sun)s,
                    %(first_dep)s, %(last_dep)s,
                    %(valid_from)s, 'default_placeholder', %(confidence)s
                )
                ON CONFLICT (route_id, service_pattern_id) DO NOTHING
            """, {
                "route_id": route_id,
                "pattern": pat["service_pattern_id"],
                "mon": pat["monday"], "tue": pat["tuesday"], "wed": pat["wednesday"],
                "thu": pat["thursday"], "fri": pat["friday"],
                "sat": pat["saturday"], "sun": pat["sunday"],
                "first_dep": pat["first_departure"],
                "last_dep": pat["last_departure"],
                "valid_from": TODAY,
                "confidence": BOOTSTRAP_CONFIDENCE,
            })

            if cur.rowcount > 0:
                inserted += 1

    return inserted


def bootstrap_schedule_profiles(cur, routes: list[dict], catalog_ids: set, dry_run: bool) -> int:
    """Populate catalog.route_schedule_profile — one per route × direction."""
    inserted = 0

    for r in routes:
        route_id = r["route_id"]
        direction_id = r["direction_id"]
        if route_id not in catalog_ids:
            continue
        if direction_id is None:
            direction_id = 0

        if dry_run:
            print(f"  [DRY] schedule_profile: {str(route_id)[:8]} d{direction_id}")
            inserted += 1
            continue

        cur.execute("""
            INSERT INTO catalog.route_schedule_profile (
                route_id, direction_id, service_pattern_id,
                window_start, window_end,
                headway_min, peak_type,
                source, confidence
            ) VALUES (
                %(route_id)s, %(direction_id)s, 'weekday_normal',
                %(window_start)s, %(window_end)s,
                %(headway)s, 'offpeak',
                'default_placeholder', %(confidence)s
            )
            ON CONFLICT (route_id, direction_id, service_pattern_id, window_start) DO NOTHING
        """, {
            "route_id": route_id,
            "direction_id": direction_id,
            "window_start": DEFAULT_WEEKDAY_FIRST_DEP,
            "window_end": DEFAULT_WEEKDAY_LAST_DEP,
            "headway": DEFAULT_HEADWAY_MIN,
            "confidence": 0.20,
        })

        if cur.rowcount > 0:
            inserted += 1

    return inserted


def bootstrap_layover_policy(cur, routes: list[dict], catalog_ids: set, dry_run: bool) -> int:
    """Populate catalog.route_layover_policy — one per route."""
    inserted = 0

    # Only insert one per route_id (not per direction)
    seen = set()
    for r in routes:
        route_id = r["route_id"]
        if route_id not in catalog_ids or route_id in seen:
            continue
        seen.add(route_id)

        if dry_run:
            print(f"  [DRY] layover_policy: {str(route_id)[:8]}")
            inserted += 1
            continue

        cur.execute("""
            INSERT INTO catalog.route_layover_policy (
                route_id,
                layover_at_destination_min, layover_at_origin_min,
                min_layover_min, max_layover_min,
                applies_to_pattern, source, confidence
            ) VALUES (
                %(route_id)s,
                %(dest)s, %(orig)s,
                %(min_l)s, %(max_l)s,
                'all', 'default_assumption', %(confidence)s
            )
            ON CONFLICT (route_id, applies_to_pattern) DO NOTHING
        """, {
            "route_id": route_id,
            "dest": DEFAULT_LAYOVER_MIN,
            "orig": DEFAULT_LAYOVER_MIN,
            "min_l": DEFAULT_MIN_LAYOVER,
            "max_l": DEFAULT_MAX_LAYOVER,
            "confidence": BOOTSTRAP_CONFIDENCE,
        })

        if cur.rowcount > 0:
            inserted += 1

    return inserted


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bootstrap catalog tables from existing route data")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be inserted without writing to DB")
    args = parser.parse_args()

    print("=" * 60)
    print("Phase 4-5 Automation — Bootstrap Catalog Tables")
    print(f"Date: {TODAY}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("=" * 60)

    conn = psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor)
    conn.autocommit = False

    try:
        cur = conn.cursor()

        # Load data
        print("\n--- Loading route_prod data ---")
        routes = load_prod_routes(cur)
        print(f"Found {len(routes)} routes in route_prod.routes")

        # 1. route_semantics
        print(f"\n--- Bootstrapping catalog.route_semantics ({len(routes)} candidates) ---")
        n_sem, catalog_ids = bootstrap_route_semantics(cur, routes, args.dry_run)

        # 2. service_days
        print(f"\n--- Bootstrapping catalog.route_service_days ---")
        n_days = bootstrap_service_days(cur, routes, catalog_ids, args.dry_run)

        # 3. schedule_profiles
        print(f"\n--- Bootstrapping catalog.route_schedule_profile ---")
        n_prof = bootstrap_schedule_profiles(cur, routes, catalog_ids, args.dry_run)

        # 4. layover_policy
        print(f"\n--- Bootstrapping catalog.route_layover_policy ---")
        n_lay = bootstrap_layover_policy(cur, routes, catalog_ids, args.dry_run)

        # Summary
        print("\n" + "=" * 60)
        print("BOOTSTRAP SUMMARY")
        print(f"  route_semantics:      {n_sem} inserted")
        print(f"  route_service_days:   {n_days} inserted")
        print(f"  route_schedule_profile: {n_prof} inserted")
        print(f"  route_layover_policy: {n_lay} inserted")
        print(f"  route_service_exceptions: 0 (left empty)")
        print("=" * 60)

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
