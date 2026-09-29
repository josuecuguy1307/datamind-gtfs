"""
Phase 4-5 Automation — Orchestrator

Chains the three automation agents in sequence:
  Agent 1  (agent_prepare.py)      → Pre-execution diagnostic
  Agent 2a (agent_execute_phase4.py) → Phase 4 semantics writer
  Agent 2b (agent_execute_phase5.py) → Phase 5 schedule bridge

Gate logic:
  - Agent 1 must produce CLEAN or WARNINGS for a route to proceed.
  - BLOCKED routes are logged and skipped.
  - Each agent's success is verified before advancing.

Can be triggered:
  - Manually: python src/automation/agent_orchestrator.py --all
  - Via pg_notify: listens on 'catalog_route_approved' channel
  - From HADES: called as a subprocess by the autopilot worker

Usage:
    python src/automation/agent_orchestrator.py --all
    python src/automation/agent_orchestrator.py --route <route_id>
    python src/automation/agent_orchestrator.py --batch <id1>,<id2>
    python src/automation/agent_orchestrator.py --all --dry-run
    python src/automation/agent_orchestrator.py --listen   # pg_notify listener mode
"""

from __future__ import annotations

import argparse
import json
import os
import select
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

NOTIFY_CHANNEL = "catalog_route_approved"

# ------------------------------------------------------------------
# Import agent modules
# ------------------------------------------------------------------

# Resolve src/automation path for imports
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import agent_prepare
import agent_execute_phase4
import agent_execute_phase5


# ------------------------------------------------------------------
# Orchestrator core
# ------------------------------------------------------------------

def run_pipeline(
    route_ids: list[str] | None,
    dry_run: bool = False,
    skip_gate_check: bool = False,
) -> dict[str, Any]:
    """Run the full Agent 1 → 2a → 2b pipeline for given routes.
    Returns summary dict."""

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "_orch"

    conn = psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor)
    conn.autocommit = False

    summary = {
        "run_id": run_id,
        "dry_run": dry_run,
        "agent1_results": [],
        "agent2a_results": [],
        "agent2b_results": [],
        "blocked": [],
        "errors": [],
    }

    try:
        cur = conn.cursor()

        # ── Step 0: Resolve approved route_ids ──────────────────────
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
        approved = [row["route_id"] for row in cur.fetchall()]

        print(f"{'='*70}")
        print(f"Orchestrator — Phase 4→5 Pipeline")
        print(f"Run ID: {run_id}")
        print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
        print(f"Approved routes: {len(approved)}")
        print(f"{'='*70}")

        if not approved:
            print("\nNo approved routes found. Nothing to do.")
            conn.rollback()
            return summary

        # ── Step 1: Agent 1 — Pre-execution diagnostic ─────────────
        print(f"\n{'─'*70}")
        print("STEP 1: Agent 1 — Pre-execution diagnostic")
        print(f"{'─'*70}")

        diag_results = agent_prepare.run_diagnostic(
            cur, approved, run_id + "_prepare", persist=not dry_run,
        )
        summary["agent1_results"] = diag_results

        # Partition by status
        passable = []
        blocked = []
        for d in diag_results:
            la = d.get("layover_analysis", {})
            cycle = la.get("cycle_time_min", "?")
            vehicles = la.get("n_vehicles_needed", "?")
            if d["status"] == "BLOCKED":
                blocked.append(d["route_id"])
                print(f"  BLOCKED  {d['route_id'][:12]}.. — {d.get('hard_fails', '?')} hard fails")
            else:
                passable.append(d["route_id"])
                print(f"  {d['status']:9s} {d['route_id'][:12]}.. — "
                      f"cycle={cycle}min, vehicles={vehicles}")

        summary["blocked"] = blocked

        if not passable:
            print("\nAll routes BLOCKED. Pipeline halted.")
            if not dry_run:
                conn.commit()  # persist diagnostics
            else:
                conn.rollback()
            return summary

        # ── Step 2: Agent 2a — Phase 4 semantics writer ────────────
        print(f"\n{'─'*70}")
        print(f"STEP 2: Agent 2a — Phase 4 semantics writer ({len(passable)} routes)")
        print(f"{'─'*70}")

        a2a_results = []
        for rid in passable:
            # Load catalog entry
            entries = agent_execute_phase4.load_approved_catalog(cur, [rid])
            for entry in entries:
                sem_result = agent_execute_phase4.write_route_semantics(cur, entry, dry_run)
                agency_result = agent_execute_phase4.write_agency_link(cur, entry, dry_run)
                if agency_result:
                    sem_result["agency"] = agency_result
                a2a_results.append(sem_result)

                action = sem_result["action"]
                agency_id = agency_result["agency_id"] if agency_result else "?"
                print(f"  {action:10s} {entry.get('route_short_name', '?'):8s} | agency={agency_id}")

        summary["agent2a_results"] = a2a_results

        # Persist Phase 4 diagnostics
        if a2a_results and not dry_run:
            agent_execute_phase4.persist_phase4_diagnostic(
                cur, run_id + "_phase4", a2a_results,
            )

        # ── Step 3: Agent 2b — Phase 5 schedule bridge ─────────────
        print(f"\n{'─'*70}")
        print(f"STEP 3: Agent 2b — Phase 5 schedule bridge ({len(passable)} routes)")
        print(f"{'─'*70}")

        # Load all catalog data for passable routes
        schedule_profiles = agent_execute_phase5.load_schedule_profiles(cur, passable)
        service_days = agent_execute_phase5.load_service_days(cur, passable)
        layover_policies = agent_execute_phase5.load_layover_policies(cur, passable)
        runtime_bindings = agent_execute_phase5.load_runtime_bindings(cur, passable)
        route_info = agent_execute_phase5.load_route_info(cur, passable)

        a2b_results = []
        for rid in passable:
            r = agent_execute_phase5.process_route(
                cur, rid,
                schedule_profiles, service_days,
                layover_policies, runtime_bindings, route_info,
                dry_run=dry_run,
            )
            a2b_results.append(r)

            ct = r.get("cycle_time", {})
            cycle_str = f"cycle={ct.get('cycle_time_min', '?')}min" if ct else "cycle=?"
            print(f"  {r['action']:18s} d{r['direction_id']} | "
                  f"prof={r['profiles_written']} win={r['windows_written']} | "
                  f"{cycle_str}")

        summary["agent2b_results"] = a2b_results

        # Persist Phase 5 diagnostics
        if a2b_results and not dry_run:
            agent_execute_phase5.persist_phase5_diagnostic(
                cur, run_id + "_phase5", a2b_results,
            )

        # Calendar exceptions
        n_exceptions = agent_execute_phase5.write_calendar_exceptions(cur, passable, dry_run)

        # ── Summary ────────────────────────────────────────────────
        print(f"\n{'='*70}")
        print("PIPELINE SUMMARY")
        print(f"  Agent 1 diagnosed:   {len(diag_results)}")
        print(f"  Blocked (skipped):   {len(blocked)}")
        print(f"  Agent 2a wrote:      {sum(1 for r in a2a_results if r['action'] == 'UPSERTED')}")
        print(f"  Agent 2b wrote:      {sum(1 for r in a2b_results if r['action'] == 'UPSERTED')}")
        print(f"  Calendar exceptions: {n_exceptions}")
        print(f"{'='*70}")

        if dry_run:
            print("\nDRY RUN — no changes committed.")
            conn.rollback()
        else:
            conn.commit()
            print("\nAll changes committed.")

        # Persist orchestrator-level diagnostic
        if not dry_run:
            conn2 = psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor)
            try:
                with conn2.cursor() as c2:
                    for rid in passable:
                        c2.execute("""
                            INSERT INTO automation.diagnostics
                                (run_id, route_id, phase, status, report)
                            VALUES (%s, %s::uuid, 'orchestrator', 'CLEAN', %s::jsonb)
                        """, (run_id, rid, json.dumps({
                            "pipeline": "phase4_phase5",
                            "agents": ["prepare", "phase4", "phase5"],
                            "dry_run": False,
                        })))
                    conn2.commit()
            finally:
                conn2.close()

    except Exception as e:
        conn.rollback()
        summary["errors"].append(str(e))
        print(f"\nERROR: {e}")
        raise
    finally:
        conn.close()

    return summary


# ------------------------------------------------------------------
# pg_notify listener
# ------------------------------------------------------------------

def listen_for_approvals():
    """Listen on pg_notify channel for catalog approval events.
    Blocks forever, running the pipeline for each approved route."""
    print(f"Orchestrator listener starting on channel '{NOTIFY_CHANNEL}'...")
    print("Waiting for catalog.route_semantics approval events...")
    print("(Press Ctrl+C to stop)\n")

    conn = psycopg2.connect(DB_DSN)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)

    cur = conn.cursor()
    cur.execute(f"LISTEN {NOTIFY_CHANNEL};")

    try:
        while True:
            if select.select([conn], [], [], 5.0) == ([], [], []):
                continue  # timeout, check again

            conn.poll()
            while conn.notifies:
                notify = conn.notifies.pop(0)
                payload = notify.payload or ""
                print(f"\n[{datetime.now(timezone.utc).isoformat()}] "
                      f"Received notification: {payload}")

                # Parse route_ids from payload (comma-separated UUIDs)
                route_ids = [r.strip() for r in payload.split(",") if r.strip()]

                if route_ids:
                    try:
                        run_pipeline(route_ids, dry_run=False)
                    except Exception as e:
                        print(f"Pipeline error: {e}")
                else:
                    print("  (empty payload, skipping)")

    except KeyboardInterrupt:
        print("\nListener stopped.")
    finally:
        conn.close()


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Orchestrator — Phase 4→5 automation pipeline"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Process all approved routes")
    group.add_argument("--route", type=str, help="Single route_id UUID")
    group.add_argument("--batch", type=str, help="Comma-separated route_id UUIDs")
    group.add_argument("--listen", action="store_true",
                       help="Listen for pg_notify approval events (daemon mode)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    if args.listen:
        listen_for_approvals()
        return

    route_ids = None
    if args.route:
        route_ids = [args.route]
    elif args.batch:
        route_ids = [r.strip() for r in args.batch.split(",")]

    run_pipeline(route_ids, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
