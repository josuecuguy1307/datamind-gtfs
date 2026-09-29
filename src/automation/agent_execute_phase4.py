"""
Phase 4-5 Automation — Agent 2a: Phase 4 Semantics Writer

Reads approved catalog.route_semantics entries and writes them into
route_prod.route_semantics + gtfs_work.route_agency_links, which are
the data sources the existing GTFS compilers read from.

Precondition: Agent 1 diagnostic status != BLOCKED for target routes.

Usage:
    python src/automation/agent_execute_phase4.py --all
    python src/automation/agent_execute_phase4.py --route <route_id>
    python src/automation/agent_execute_phase4.py --batch <id1>,<id2>
    python src/automation/agent_execute_phase4.py --all --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from datamind_console.persistence import patch_route_prod_fields

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

DB_DSN = (
    os.environ.get("DB_DSN")
    or os.environ.get("DATABASE_URL")
    or "postgresql://localhost:5432/datamind_ml"
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def normalize_agency_id(operator_name: str) -> str:
    """Derive agency_id from operator name, matching the compiler's convention:
    'op_' + lowered + non-alnum replaced with '_'."""
    return "op_" + re.sub(r"[^a-z0-9]+", "_", operator_name.lower()).strip("_")


def lookup_agency_id(cur, operator_name: str) -> str | None:
    """Check if operator has a named agency in gtfs_work.agency_catalog."""
    cur.execute("""
        SELECT agency_id FROM gtfs_work.agency_catalog
        WHERE lower(agency_name) = lower(%s)
           OR lower(agency_name_norm) = lower(%s)
        LIMIT 1
    """, (operator_name, re.sub(r"[^a-z0-9]+", "", operator_name.lower())))
    row = cur.fetchone()
    return row["agency_id"] if row else None


# ------------------------------------------------------------------
# Gate check
# ------------------------------------------------------------------

def check_precondition(cur, route_id: str) -> tuple[bool, str]:
    """Verify Agent 1 diagnostic is not BLOCKED for this route.
    Returns (ok, reason)."""
    cur.execute("""
        SELECT status FROM automation.diagnostics
        WHERE route_id = %s::uuid AND phase = 'pre_execution'
        ORDER BY created_at DESC
        LIMIT 1
    """, (route_id,))
    row = cur.fetchone()
    if not row:
        return False, "No pre_execution diagnostic found — run agent_prepare.py first"
    if row["status"] == "BLOCKED":
        return False, f"Route is BLOCKED in latest diagnostic"
    return True, row["status"]


# ------------------------------------------------------------------
# Core logic
# ------------------------------------------------------------------

def load_approved_catalog(cur, route_ids: list[str] | None = None) -> list[dict]:
    """Load approved catalog.route_semantics entries."""
    if route_ids:
        cur.execute("""
            SELECT cs.route_id::text, cs.operator, cs.route_short_name,
                   cs.route_long_name, cs.route_type,
                   cs.public_origin, cs.public_destination,
                   cs.aliases, cs.jurisdiction, cs.confidence,
                   r.service_route_id::text, r.direction_id
            FROM catalog.route_semantics cs
            JOIN route_prod.routes r ON r.route_id = cs.route_id
            WHERE cs.approved = TRUE
              AND cs.route_id = ANY(%s::uuid[])
            ORDER BY cs.operator, cs.route_short_name
        """, (route_ids,))
    else:
        cur.execute("""
            SELECT cs.route_id::text, cs.operator, cs.route_short_name,
                   cs.route_long_name, cs.route_type,
                   cs.public_origin, cs.public_destination,
                   cs.aliases, cs.jurisdiction, cs.confidence,
                   r.service_route_id::text, r.direction_id
            FROM catalog.route_semantics cs
            JOIN route_prod.routes r ON r.route_id = cs.route_id
            WHERE cs.approved = TRUE
            ORDER BY cs.operator, cs.route_short_name
        """)
    return [dict(row) for row in cur.fetchall()]


def write_route_semantics(cur, entry: dict, dry_run: bool) -> dict:
    """Upsert one route into route_prod.route_semantics.
    Returns a result dict with what was done."""
    route_id = entry["route_id"]
    ref = entry["route_short_name"]
    name = entry["route_long_name"]
    operator = entry["operator"]
    aliases = entry.get("aliases") or []
    confidence = float(entry.get("confidence") or 0.0)
    service_route_id = entry.get("service_route_id")
    direction_id = entry.get("direction_id")

    result = {
        "route_id": route_id,
        "route_short_name": ref,
        "operator": operator,
        "action": None,
        "validations": [],
    }

    # Validate
    if not ref or not ref.strip():
        result["validations"].append({"check": "route_short_name_not_empty", "passed": False})
    else:
        result["validations"].append({"check": "route_short_name_not_empty", "passed": True})

    if not name or not name.strip():
        result["validations"].append({"check": "route_long_name_not_empty", "passed": False})
    else:
        result["validations"].append({"check": "route_long_name_not_empty", "passed": True})

    origin = entry.get("public_origin", "")
    dest = entry.get("public_destination", "")
    has_od = (origin and origin in name) or (dest and dest in name)
    result["validations"].append({
        "check": "route_long_name_contains_origin_or_dest",
        "passed": has_od,
        "detail": f"origin='{origin}', dest='{dest}' in '{name}'",
        "severity": "SOFT",
    })

    if dry_run:
        result["action"] = "DRY_RUN"
        return result

    # Upsert route_prod.route_semantics
    cur.execute("""
        INSERT INTO route_prod.route_semantics (
            route_id, route_name, route_ref, operator_name,
            route_aliases, naming_confidence, human_verified,
            semantics_updated_at, service_route_id, direction_id
        ) VALUES (
            %(route_id)s::uuid, %(name)s, %(ref)s, %(operator)s,
            %(aliases)s, %(confidence)s, TRUE,
            now(), %(srv_id)s::uuid, %(dir)s
        )
        ON CONFLICT (route_id) DO UPDATE SET
            route_name = EXCLUDED.route_name,
            route_ref = EXCLUDED.route_ref,
            operator_name = EXCLUDED.operator_name,
            route_aliases = EXCLUDED.route_aliases,
            naming_confidence = EXCLUDED.naming_confidence,
            human_verified = TRUE,
            semantics_updated_at = now(),
            service_route_id = EXCLUDED.service_route_id,
            direction_id = EXCLUDED.direction_id
    """, {
        "route_id": route_id,
        "name": name,
        "ref": ref,
        "operator": operator,
        "aliases": aliases,
        "confidence": confidence,
        "srv_id": service_route_id,
        "dir": direction_id,
    })

    # Also update route_prod.routes.route_name to keep it in sync
    patch_route_prod_fields(
        conn=cur.connection,
        route_id=str(route_id),
        fields={
            "route_name": name,
            "route_aliases": aliases,
            "naming_confidence": confidence,
            "human_verified": True,
            "semantics_updated_at": datetime.now(timezone.utc),
        },
        source_type="agent_execute_phase4.sync_route_prod_routes",
        pipeline_version="src/automation/agent_execute_phase4",
    )

    result["action"] = "UPSERTED"
    return result


def write_agency_link(cur, entry: dict, dry_run: bool) -> dict | None:
    """Ensure the operator has an agency link for this route.
    Returns result dict or None if no action needed."""
    route_id = entry["route_id"]
    operator = entry["operator"]

    if not operator or operator == "Unknown":
        return None

    # Check for existing agency in catalog
    catalog_agency_id = lookup_agency_id(cur, operator)
    agency_id = catalog_agency_id or normalize_agency_id(operator)

    if dry_run:
        return {"route_id": route_id, "agency_id": agency_id, "action": "DRY_RUN"}

    cur.execute("""
        INSERT INTO gtfs_work.route_agency_links (
            route_id, agency_id, match_score, match_method,
            source_operator_name, status, updated_by, updated_at
        ) VALUES (
            %(route_id)s::uuid, %(agency_id)s, 1.0,
            'catalog_phase4_automation', %(operator)s,
            'linked', 'agent_execute_phase4', now()
        )
        ON CONFLICT (route_id) DO UPDATE SET
            agency_id = EXCLUDED.agency_id,
            match_score = EXCLUDED.match_score,
            match_method = EXCLUDED.match_method,
            source_operator_name = EXCLUDED.source_operator_name,
            status = EXCLUDED.status,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
    """, {
        "route_id": route_id,
        "agency_id": agency_id,
        "operator": operator,
    })

    return {"route_id": route_id, "agency_id": agency_id, "action": "UPSERTED"}


# ------------------------------------------------------------------
# Diagnostic persistence
# ------------------------------------------------------------------

def persist_phase4_diagnostic(cur, run_id: str, results: list[dict]) -> None:
    """Save Phase 4 execution diagnostic."""
    for r in results:
        route_id = r["route_id"]
        failed_validations = [
            v for v in r.get("validations", [])
            if not v["passed"] and v.get("severity") != "SOFT"
        ]
        status = "BLOCKED" if failed_validations else "CLEAN"

        cur.execute("""
            INSERT INTO automation.diagnostics (run_id, route_id, phase, status, report)
            VALUES (%s, %s::uuid, 'phase4_execute', %s, %s::jsonb)
        """, (run_id, route_id, status, json.dumps(r, default=str)))


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Agent 2a — Phase 4 semantics writer"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Process all approved routes")
    group.add_argument("--route", type=str, help="Single route_id UUID")
    group.add_argument("--batch", type=str, help="Comma-separated route_id UUIDs")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--skip-gate-check", action="store_true",
                        help="Skip Agent 1 diagnostic check (for initial bootstrap)")
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "_phase4"

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

        # Load approved catalog entries
        entries = load_approved_catalog(cur, route_ids)

        print(f"Agent 2a — Phase 4 Semantics Writer")
        print(f"Run ID: {run_id}")
        print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
        print(f"Approved routes found: {len(entries)}")
        print("=" * 60)

        if not entries:
            print("\nNo approved routes in catalog. Nothing to do.")
            print("Hint: Set approved=TRUE in catalog.route_semantics for target routes.")
            conn.rollback()
            return

        results = []
        skipped = 0

        for entry in entries:
            rid = entry["route_id"]
            ref = entry["route_short_name"]

            # Gate check
            if not args.skip_gate_check and not args.dry_run:
                ok, reason = check_precondition(cur, rid)
                if not ok:
                    print(f"  SKIP {ref:8s} ({rid[:12]}..): {reason}")
                    skipped += 1
                    continue

            # Write semantics
            sem_result = write_route_semantics(cur, entry, args.dry_run)
            results.append(sem_result)

            # Write agency link
            agency_result = write_agency_link(cur, entry, args.dry_run)
            if agency_result:
                sem_result["agency"] = agency_result

            action = sem_result["action"]
            agency_id = agency_result["agency_id"] if agency_result else "?"
            print(f"  {action:10s} {ref:8s} | {entry['operator']:25s} | agency={agency_id}")

        # Persist diagnostic
        if results and not args.dry_run:
            persist_phase4_diagnostic(cur, run_id, results)

        # Summary
        print()
        print("=" * 60)
        upserted = sum(1 for r in results if r["action"] == "UPSERTED")
        dry = sum(1 for r in results if r["action"] == "DRY_RUN")
        soft_warns = sum(
            1 for r in results
            for v in r.get("validations", [])
            if not v["passed"]
        )
        print(f"  Processed: {len(results)}")
        print(f"  Upserted:  {upserted}")
        print(f"  Skipped:   {skipped}")
        print(f"  Dry-run:   {dry}")
        print(f"  Soft warnings: {soft_warns}")
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
