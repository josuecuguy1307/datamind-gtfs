#!/usr/bin/env python3
"""
Work Queue Dashboard — shows pipeline status, incomplete bundles, and next actions.

Usage:
    python -m phase5_gtfs.scripts.work_queue_dashboard
    python -m phase5_gtfs.scripts.work_queue_dashboard --canton valle
    python -m phase5_gtfs.scripts.work_queue_dashboard --next-action
    python -m phase5_gtfs.scripts.work_queue_dashboard --gaps
"""
from __future__ import annotations

import argparse
import json
import os
import sys

DB_DSN = os.environ.get(
    "DB_DSN",
    "postgresql://localhost:5432/datamind_ml",
)

# Prompt file names
PROMPT_FILES = {
    0: "PROMPT_06_ZERO.md",
    1: "PROMPT_06a_INVENTORY.md",
    "06b-S": "PROMPT_06b_S_SCHEDULES.md",
    "06b-R": "PROMPT_06b_R_RUNTIMES.md",
    3: "PROMPT_06c_FARES.md",
}

# Batch sizes per prompt type
BATCH_06B_S = 18   # 15-20 routes per schedule prompt
BATCH_06B_R = 9    # 8-10 routes per runtime prompt


def get_conn():
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


def _safe_query(cur, sql: str) -> int:
    try:
        cur.execute(sql)
        return cur.fetchone()[0]
    except Exception:
        return 0


# ── Cycle detection ────────────────────────────────────────

def detect_cycle(values: dict) -> dict:
    """
    Detect which pipeline cycle the canton is in.

    Returns dict with: cycle (int), label (str), prompt_file (str), description (str).
    """
    total = values.get("Routes", 0)
    sem = values.get("Routes with semantics", 0)
    sched = values.get("Routes with schedules", 0)
    svc = values.get("Routes with service days", 0)

    if total == 0:
        return {
            "cycle": 0,
            "label": "Cycle 0 — Context + OSM",
            "prompt_file": PROMPT_FILES[0],
            "description": "No routes exist. Run 06-zero to establish canton context and extract OSM routes.",
        }

    # If routes exist but zero catalogs at all → Cycle 1
    if sem == 0 and sched == 0:
        return {
            "cycle": 1,
            "label": "Cycle 1 — Route Inventory (06a)",
            "prompt_file": PROMPT_FILES[1],
            "description": "Routes exist but no catalogs. Run 06a to inventory all routes and enrich metadata.",
        }

    # If some/all routes have catalogs but not all have schedules → Cycle 2
    if sched < total:
        return {
            "cycle": 2,
            "label": "Cycle 2 — Multi-batch Cataloging (06b-S + 06b-R)",
            "prompt_file": f"{PROMPT_FILES['06b-S']} + {PROMPT_FILES['06b-R']}",
            "description": f"{sched}/{total} routes cataloged. Fill schedule + runtime gaps with 06b-S and 06b-R prompts.",
        }

    # All routes have schedules → Cycle 3
    return {
        "cycle": 3,
        "label": "Cycle 3 — Fares + Finalize",
        "prompt_file": PROMPT_FILES[3],
        "description": "All routes cataloged. Run 06c for fares or finalize GTFS compilation.",
    }


# ── Section 1: Global health ────────────────────────────────

def global_health(conn) -> dict:
    cur = conn.cursor()

    print("=" * 60)
    print("  PIPELINE HEALTH")
    print("=" * 60)

    queries = [
        ("Nodes (promoted)", "SELECT COUNT(*) FROM node_prod.nodes"),
        ("Active places", "SELECT COUNT(*) FROM geo_prod.places WHERE status='active'"),
        (
            "Garbage names",
            "SELECT COUNT(*) FROM geo_prod.places WHERE status='active' "
            "AND canonical_name IN ('(sin nombre)','SN','Parada Sin Nombre',"
            "'Parada','La y','S/N','Sin Nombre','N/A')",
        ),
        (
            "Places with embeddings",
            "SELECT COUNT(*) FROM geo_prod.place_embeddings pe "
            "JOIN geo_prod.places p ON p.place_id=pe.place_id WHERE p.status='active'",
        ),
        ("Routes", "SELECT COUNT(*) FROM route_prod.routes"),
        (
            "Routes with semantics",
            "SELECT COUNT(DISTINCT route_id) FROM catalog.route_semantics",
        ),
        (
            "Routes with schedules",
            "SELECT COUNT(DISTINCT route_id) FROM catalog.route_schedule_profile",
        ),
        (
            "Routes with service days",
            "SELECT COUNT(DISTINCT route_id) FROM catalog.route_service_days",
        ),
        (
            "Routes with estimates",
            "SELECT COUNT(DISTINCT route_id) FROM gtfs_work.runtime_route_estimates",
        ),
        ("GTFS feeds", "SELECT COUNT(*) FROM gtfs_prod.feed_versions"),
        (
            "Routes with research runtimes",
            "SELECT COUNT(DISTINCT route_id) FROM catalog.route_schedule_profile "
            "WHERE runtime_override_min IS NOT NULL",
        ),
    ]

    values = {}
    for label, sql in queries:
        val = _safe_query(cur, sql)
        values[label] = val
        print(f"  {label}: {val:,}")

    return values


# ── Section 2: Cycle status ─────────────────────────────────

def cycle_status(values: dict) -> dict:
    print("\n" + "=" * 60)
    print("  CYCLE STATUS")
    print("=" * 60)

    info = detect_cycle(values)
    print(f"  Current: {info['label']}")
    print(f"  Status:  {info['description']}")
    print(f"  Prompt:  {info['prompt_file']}")

    return info


# ── Section 3: Incomplete bundles ────────────────────────────

def incomplete_bundles(values: dict) -> list:
    print("\n" + "=" * 60)
    print("  INCOMPLETE BUNDLES")
    print("=" * 60)

    total_routes = values.get("Routes", 0)
    bundles: list = []

    if values.get("Garbage names", 0) > 0:
        bundles.append(
            ("HIGH", f"{values['Garbage names']} garbage names remain — Phase 2 naming incomplete")
        )

    places = values.get("Active places", 0)
    embeddings = values.get("Places with embeddings", 0)
    if places > 0 and embeddings < places:
        gap = places - embeddings
        bundles.append(("HIGH", f"{gap} places missing embeddings — Phase 2 incomplete"))

    sem = values.get("Routes with semantics", 0)
    sched = values.get("Routes with schedules", 0)
    svc = values.get("Routes with service days", 0)
    est = values.get("Routes with estimates", 0)

    if total_routes > 0:
        if sem < total_routes:
            bundles.append(
                ("MEDIUM", f"{total_routes - sem} routes missing semantics — run OSM bridge or 06b")
            )
        if sched < total_routes:
            bundles.append(
                ("HIGH", f"{total_routes - sched} routes missing schedules — need 06b-S")
            )
        if svc < total_routes:
            bundles.append(
                ("MEDIUM", f"{total_routes - svc} routes missing service days — included in 06b-S")
            )
        research_rt = values.get("Routes with research runtimes", 0)
        if sched > research_rt:
            bundles.append(
                ("HIGH", f"{sched - research_rt} routes have schedules but no research runtimes — need 06b-R")
            )

    if not bundles:
        print("  No incomplete bundles — all phases up to date")
    else:
        for priority, msg in bundles:
            print(f"  [{priority}] {msg}")

    return bundles


# ── Section 4: GTFS readiness ───────────────────────────────

def coverage_and_threshold(conn, values: dict):
    print("\n" + "=" * 60)
    print("  GTFS READINESS")
    print("=" * 60)

    total = values.get("Routes", 0)
    sched = values.get("Routes with schedules", 0)
    est = values.get("Routes with estimates", 0)

    if total == 0:
        print("  No routes — nothing to compile")
        return

    coverage = sched / total
    est_coverage = est / total

    print(f"  Total routes: {total}")
    print(f"  With complete catalogs: {sched} ({coverage * 100:.0f}%)")
    print(f"  With runtime estimates: {est} ({est_coverage * 100:.0f}%)")
    print(f"  Threshold: 80%")

    if coverage >= 1.0:
        print(f"  -> READY: 100% coverage — compile full GTFS")
    elif coverage >= 0.80:
        print(
            f"  -> READY: {coverage * 100:.0f}% coverage — compile GTFS with {sched} routes"
        )
        print(
            f"    ({total - sched} routes excluded, will be added in next recompile)"
        )
    elif coverage >= 0.50:
        needed = int(total * 0.80) - sched
        print(f"  -> APPROACHING: need {needed} more routes cataloged to reach 80%")
        batches_s = (needed + BATCH_06B_S - 1) // BATCH_06B_S
        print(f"    ~ {batches_s} more 06b-S prompts of ~{BATCH_06B_S} routes each")
    else:
        needed = int(total * 0.80) - sched
        print(f"  -> NOT READY: need {needed} more routes cataloged")


# ── Section 5: Route gap report (split 06b-S / 06b-R) ──────

def route_gap_report(conn):
    print("\n" + "=" * 60)
    print("  ROUTES NEEDING DEEP RESEARCH 06b")
    print("=" * 60)

    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT r.route_name,
                COALESCE(rs.route_ref, '?') AS route_ref,
                CASE WHEN cs.route_id IS NOT NULL THEN 'Y' ELSE 'N' END AS has_semantics,
                CASE WHEN csp.route_id IS NOT NULL THEN 'Y' ELSE 'N' END AS has_schedule,
                CASE WHEN csd.route_id IS NOT NULL THEN 'Y' ELSE 'N' END AS has_service_days,
                CASE WHEN rro.route_id IS NOT NULL THEN 'Y' ELSE 'N' END AS has_research_runtime
            FROM route_prod.routes r
            LEFT JOIN route_prod.route_semantics rs ON rs.route_id = r.route_id
            LEFT JOIN catalog.route_semantics cs ON cs.route_id = r.route_id
            LEFT JOIN (SELECT DISTINCT route_id FROM catalog.route_schedule_profile) csp
                ON csp.route_id = r.route_id
            LEFT JOIN (SELECT DISTINCT route_id FROM catalog.route_service_days) csd
                ON csd.route_id = r.route_id
            LEFT JOIN (
                SELECT DISTINCT route_id FROM catalog.route_schedule_profile
                WHERE runtime_override_min IS NOT NULL
            ) rro ON rro.route_id = r.route_id
            ORDER BY r.route_name
            """
        )
        all_rows = cur.fetchall()
    except Exception as e:
        print(f"  ERROR: {e}")
        return

    # Split into 06b-S (missing schedule) and 06b-R (has schedule but no research runtime)
    gaps_s = [r for r in all_rows if r[3] == "N"]   # has_schedule == N
    gaps_r = [r for r in all_rows if r[3] == "Y" and r[5] == "N"]  # has schedule, no research runtime

    if not gaps_s and not gaps_r:
        print("  All routes have schedule data and estimates — no 06b needed")
        return

    # ── 06b-S: Schedule gaps ──
    if gaps_s:
        n_batches_s = (len(gaps_s) + BATCH_06B_S - 1) // BATCH_06B_S
        print(f"\n  --- 06b-S SCHEDULE GAPS ({len(gaps_s)} routes -> {n_batches_s} prompts) ---")
        print(f"  Prompt file: {PROMPT_FILES['06b-S']}")
        print(f"  Batch size: ~{BATCH_06B_S} routes per prompt\n")

        for i in range(n_batches_s):
            batch = gaps_s[i * BATCH_06B_S: (i + 1) * BATCH_06B_S]
            print(f"  === 06b-S Prompt {i + 1} ({len(batch)} routes) ===")
            for r in batch:
                ref = r[1] or "?"
                name = (r[0] or "unnamed")[:45]
                sem = r[2]
                print(f"    {ref:>10}  {name}  (semantics:{sem})")
            print()
    else:
        print("\n  06b-S: All routes have schedule profiles")

    # ── 06b-R: Runtime gaps ──
    if gaps_r:
        n_batches_r = (len(gaps_r) + BATCH_06B_R - 1) // BATCH_06B_R
        print(f"\n  --- 06b-R RUNTIME GAPS ({len(gaps_r)} routes -> {n_batches_r} prompts) ---")
        print(f"  Prompt file: {PROMPT_FILES['06b-R']}")
        print(f"  Batch size: ~{BATCH_06B_R} routes per prompt\n")

        for i in range(n_batches_r):
            batch = gaps_r[i * BATCH_06B_R: (i + 1) * BATCH_06B_R]
            print(f"  === 06b-R Prompt {i + 1} ({len(batch)} routes) ===")
            for r in batch:
                ref = r[1] or "?"
                name = (r[0] or "unnamed")[:45]
                print(f"    {ref:>10}  {name}")
            print()
    else:
        print("\n  06b-R: All cataloged routes have runtime estimates")

    # Summary
    total_s = len(gaps_s)
    total_r = len(gaps_r)
    n_s = (total_s + BATCH_06B_S - 1) // BATCH_06B_S if total_s else 0
    n_r = (total_r + BATCH_06B_R - 1) // BATCH_06B_R if total_r else 0
    print(f"  TOTAL: {total_s} schedule gaps ({n_s} 06b-S prompts) + {total_r} runtime gaps ({n_r} 06b-R prompts)")


# ── Section 6: Next actions ──────────────────────────────────

def next_actions(values: dict, bundles: list, cycle_info: dict):
    print("\n" + "=" * 60)
    print("  NEXT ACTIONS")
    print("=" * 60)

    total = values.get("Routes", 0)
    sched = values.get("Routes with schedules", 0)
    research_rt = values.get("Routes with research runtimes", 0)
    cycle = cycle_info["cycle"]

    actions: list = []

    if values.get("Garbage names", 0) > 0:
        actions.append(
            (
                "HIGH",
                "Fix garbage names",
                "python -m phase2_semantics.scripts.26b_fast_contextual_names --apply",
            )
        )

    if cycle == 0:
        actions.append(
            ("HIGH", "Run 06-zero for canton context", f"Open {PROMPT_FILES[0]}")
        )
    elif cycle == 1:
        actions.append(
            ("HIGH", "Run 06a route inventory", f"Open {PROMPT_FILES[1]}")
        )
    elif cycle == 2:
        gap_s = total - sched
        gap_r = sched - research_rt if sched > research_rt else 0
        if gap_s > 0:
            n_s = (gap_s + BATCH_06B_S - 1) // BATCH_06B_S
            actions.append(
                ("HIGH", f"Fill {gap_s}-route schedule gap", f"{n_s} x {PROMPT_FILES['06b-S']} (~{BATCH_06B_S} routes each)")
            )
        if gap_r > 0:
            n_r = (gap_r + BATCH_06B_R - 1) // BATCH_06B_R
            actions.append(
                ("HIGH", f"Fill {gap_r}-route runtime gap", f"{n_r} x {PROMPT_FILES['06b-R']} (~{BATCH_06B_R} routes each)")
            )
    elif cycle == 3:
        actions.append(
            ("MEDIUM", "Run 06c for fares", f"Open {PROMPT_FILES[3]}")
        )

    if total > 0 and sched / total >= 0.80:
        actions.append(
            (
                "MEDIUM",
                "Compile GTFS",
                f"python build_canton_gtfs.py --use-v2 ({sched} routes ready)",
            )
        )

    actions.append(
        (
            "LOW",
            "Start next canton",
            f"Open {PROMPT_FILES[0]} for next canton in queue",
        )
    )

    for priority, action, how in actions:
        print(f"\n  [{priority}] {action}")
        print(f"       -> {how}")


# ── Section 7: Canton terminal states ────────────────────────

def canton_states():
    print("\n" + "=" * 60)
    print("  CANTON TERMINAL STATES")
    print("=" * 60)

    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "workspace", "cantons"),
        os.path.join(os.getcwd(), "workspace", "cantons"),
    ]

    cantons_dir = None
    for c in candidates:
        if os.path.isdir(c):
            cantons_dir = c
            break

    if not cantons_dir:
        print("  No canton directories found")
        print("  (Cantons will appear after the first 06-zero is processed)")
        return

    found = False
    for canton in sorted(os.listdir(cantons_dir)):
        state_file = os.path.join(cantons_dir, canton, "state.json")
        if not os.path.exists(state_file):
            continue
        found = True

        with open(state_file) as f:
            state = json.load(f)

        cycle = state.get("current_cycle", "?")
        routes = state.get("routes_built", 0)
        cataloged = state.get("routes_with_catalog", 0)
        coverage = cataloged / routes if routes > 0 else 0
        gtfs = state.get("gtfs_compiled", False)
        next_act = state.get("next_action", "?")

        print(f"\n  {canton.upper()}")
        print(f"  |-- Cycle: {cycle}")
        print(f"  |-- Routes: {routes} built, {cataloged} cataloged ({coverage * 100:.0f}%)")
        print(f"  |-- GTFS compiled: {gtfs}")
        print(f"  `-- Next: {next_act[:70]}")

    if not found:
        print("  No canton state files found yet")


# ── Main ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="HADES Work Queue Dashboard")
    parser.add_argument("--canton", help="Show status for one canton only")
    parser.add_argument(
        "--next-action", action="store_true", help="Show only the next action"
    )
    parser.add_argument(
        "--gaps",
        action="store_true",
        help="Show routes needing 06b with prompt batches",
    )
    args = parser.parse_args()

    conn = get_conn()

    if args.gaps:
        route_gap_report(conn)
        conn.close()
        return

    values = global_health(conn)
    c_info = cycle_status(values)
    bundles = incomplete_bundles(values)

    if args.next_action:
        next_actions(values, bundles, c_info)
        conn.close()
        return

    coverage_and_threshold(conn, values)
    route_gap_report(conn)
    next_actions(values, bundles, c_info)
    canton_states()

    conn.close()

    print("\n" + "=" * 60)
    print("  Run anytime: python -m phase5_gtfs.scripts.work_queue_dashboard")
    print("=" * 60)


if __name__ == "__main__":
    main()
