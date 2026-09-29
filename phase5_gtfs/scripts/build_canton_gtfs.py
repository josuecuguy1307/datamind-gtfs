#!/usr/bin/env python3
"""
Run the full Phase 5 pipeline for routes from a specific canton/sector:
1. Estimate runtimes (Runtime Lab, mixed_sections mode)
2. Bind estimates
3. Compute vehicle blocks
4. Compile GTFS (steps 01-05)
5. Validate
6. Package (pending approval)

Usage:
    python -m phase5_gtfs.scripts.build_canton_gtfs --canton cayambe
    python -m phase5_gtfs.scripts.build_canton_gtfs --canton cayambe --dry-run
    python -m phase5_gtfs.scripts.build_canton_gtfs --canton cayambe --skip-estimate
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DB_DSN", "postgresql://localhost:5432/datamind_ml")

from phase5_gtfs.common.config import db_conn, GTFS_OUT_DIR
from phase5_gtfs.runtime.batch_estimate import get_unbound_route_directions
from phase5_gtfs.runtime.compute_vehicles import sync_vehicle_blocks
from phase5_gtfs.compiler.build_calendar import build_calendar
from phase5_gtfs.compiler.build_routes import build_routes_and_stops
from phase5_gtfs.compiler.build_shapes import build_shapes
from phase5_gtfs.compiler.build_trips import build_trips_and_frequencies
from phase5_gtfs.compiler.build_stop_times import build_stop_times
from phase5_gtfs.validator.gtfs_validate import validate_export_run
from phase5_gtfs.publisher.package_gtfs import package_gtfs


def get_canton_routes(canton: str, province: str = "") -> List[Dict[str, Any]]:
    """Query route_prod for routes matching a canton/sector filter.

    If province is given, also includes all routes for that province
    (catches OSM/Metrovía routes whose source doesn't contain the canton name).
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT r.route_id::text AS route_id,
                       r.route_name,
                       r.source,
                       COALESCE(r.canonical_sequence_ready, FALSE) AS seq_ready
                FROM route_prod.routes r
                WHERE LOWER(r.source) LIKE %s
                   OR (%s != '' AND LOWER(r.province) = LOWER(%s))
                ORDER BY r.route_name
                """,
                (f"%{canton.lower()}%", province, province),
            )
            return [dict(row) for row in (cur.fetchall() or [])]


def estimate_and_bind_canton(
    canton_routes: List[Dict[str, Any]],
    *,
    dry_run: bool = False,
    use_v2: bool = False,
    research_file: str = "",
) -> Dict[str, Any]:
    """Estimate runtimes for unbound route-directions and bind them."""
    from datamind_console.phases.phase5_gtfs.client import Phase5Client

    client = Phase5Client()
    route_ids = {r["route_id"] for r in canton_routes}

    # Find unbound directions for canton routes
    all_unbound = get_unbound_route_directions(client)
    canton_unbound = [u for u in all_unbound if str(u["route_id"]) in route_ids]

    if dry_run:
        return {
            "dry_run": True,
            "canton_routes": len(canton_routes),
            "unbound_directions": len(canton_unbound),
            "routes": [
                {"route_id": u["route_id"], "route_name": u["route_name"], "direction_id": u["direction_id"]}
                for u in canton_unbound
            ],
        }

    # ── V2 path: RuntimeLabV2 with Valhalla + variance ──
    if use_v2:
        from phase5_gtfs.runtime_lab.runtime_lab_v2 import RuntimeLabV2

        with db_conn() as conn:
            lab = RuntimeLabV2(conn)
            if research_file:
                lab.load_research_data(research_file)
                print(f"  Loaded research data: {research_file}")

            ok = 0
            failed = 0
            results: List[Dict[str, Any]] = []

            for i, u in enumerate(canton_unbound):
                rid = str(u["route_id"])
                did = int(u["direction_id"])
                name = str(u.get("route_name") or rid[:8])
                print(f"  [{i + 1}/{len(canton_unbound)}] {name} d{did} [v2] ... ", end="", flush=True)

                try:
                    est = lab.estimate_and_bind(rid, did, time_period="off_peak")
                    if not est:
                        print("NO_RESULT")
                        failed += 1
                        continue

                    p50 = est["total_p50_secs"]
                    p05 = est["total_p05_secs"]
                    p95 = est["total_p95_secs"]
                    cv = est["total_cv"]
                    conf = est["confidence"]
                    print(
                        f"OK p50={p50 // 60}min "
                        f"(range: {p05 // 60}-{p95 // 60}min) "
                        f"CV={cv:.2f} conf={conf}"
                    )
                    results.append({
                        "route_id": rid,
                        "direction_id": did,
                        "status": "OK",
                        "estimate_id": est.get("estimate_id", ""),
                        "p50_secs": p50,
                        "p05_secs": p05,
                        "p95_secs": p95,
                        "cv": cv,
                        "confidence": conf,
                        "method": est["method"],
                    })
                    ok += 1

                except Exception as e:
                    print(f"FAIL: {str(e)[:100]}")
                    results.append({
                        "route_id": rid,
                        "direction_id": did,
                        "status": "FAIL",
                        "error": str(e)[:200],
                    })
                    failed += 1

            conn.commit()

        return {"total": len(canton_unbound), "ok": ok, "failed": failed, "results": results}

    # ── V1 path: existing catalog-based estimation ──
    ok = 0
    failed = 0
    results: List[Dict[str, Any]] = []

    for i, u in enumerate(canton_unbound):
        rid = str(u["route_id"])
        did = int(u["direction_id"])
        name = str(u.get("route_name") or rid[:8])
        print(f"  [{i + 1}/{len(canton_unbound)}] {name} d{did} ... ", end="", flush=True)

        try:
            est = client.estimate_runtime_from_catalog(
                route_id=rid,
                direction_id=did,
                area_profile_code="auto",
                mode_strategy="mixed_sections",
                persist_snapshot=True,
                use_external_elevation=True,
                use_external_signals=False,
            )
            estimate_id = str(est.get("estimate_id") or "")
            if not estimate_id:
                print("NO_ESTIMATE_ID")
                failed += 1
                continue

            client.bind_runtime_estimate_to_route_direction(
                route_id=rid,
                direction_id=did,
                estimate_id=estimate_id,
            )
            metrics = est.get("metrics") or {}
            print(f"OK (off={metrics.get('runtime_offpeak_secs', 0)}s, peak={metrics.get('runtime_peak_secs', 0)}s)")
            results.append({"route_id": rid, "direction_id": did, "status": "OK", "estimate_id": estimate_id})
            ok += 1

        except Exception as e:
            print(f"FAIL: {str(e)[:100]}")
            results.append({"route_id": rid, "direction_id": did, "status": "FAIL", "error": str(e)[:200]})
            failed += 1

    return {"total": len(canton_unbound), "ok": ok, "failed": failed, "results": results}


def compile_gtfs(export_run_id: str) -> Dict[str, Any]:
    """Run all compiler steps sequentially."""
    start_date = date.today()
    end_date = start_date + timedelta(days=120)

    print("\n── Step 1: Calendar")
    cal = build_calendar(export_run_id, start_date=start_date, end_date=end_date)
    print(f"   {cal}")

    print("── Step 2: Routes & Stops")
    rs = build_routes_and_stops(export_run_id)
    print(f"   {rs}")

    print("── Step 3: Shapes")
    sh = build_shapes(export_run_id)
    print(f"   {sh}")

    print("── Step 4: Trips & Frequencies")
    tr = build_trips_and_frequencies(export_run_id)
    print(f"   {tr}")

    print("── Step 5: Stop Times")
    st = build_stop_times(export_run_id)
    print(f"   {st}")

    return {"calendar": cal, "routes_stops": rs, "shapes": sh, "trips": tr, "stop_times": st}


def main():
    parser = argparse.ArgumentParser(description="Build GTFS for a canton's routes")
    parser.add_argument("--canton", required=True, help="Canton/sector name to filter routes")
    parser.add_argument("--province", default="", help="Also include all routes for this province")
    parser.add_argument("--dry-run", action="store_true", help="Only show what would be done")
    parser.add_argument("--skip-estimate", action="store_true", help="Skip runtime estimation (use existing bindings)")
    parser.add_argument("--skip-compile", action="store_true", help="Only estimate and bind, don't compile GTFS")
    parser.add_argument("--use-v2", action="store_true", help="Use RuntimeLabV2 (Valhalla + variance) instead of catalog model")
    parser.add_argument("--research", default="", help="Path to 06b research JSON (used with --use-v2)")
    args = parser.parse_args()

    print(f"=== Phase 5 GTFS Builder: canton={args.canton} province={args.province} ===\n")

    # 1. Find canton routes
    routes = get_canton_routes(args.canton, province=args.province)
    print(f"Found {len(routes)} routes for canton '{args.canton}'")
    if not routes:
        print("No routes found. Check sector_key values in route_prod.routes.")
        return

    for r in routes[:10]:
        print(f"  - {r['route_name']} (seq_ready={r['seq_ready']})")
    if len(routes) > 10:
        print(f"  ... +{len(routes) - 10} more")

    # 2. Estimate & bind
    if not args.skip_estimate:
        print("\n── Runtime Estimation & Binding ──")
        est_result = estimate_and_bind_canton(
            routes,
            dry_run=args.dry_run,
            use_v2=args.use_v2,
            research_file=args.research,
        )
        print(f"\nEstimation: {json.dumps({k: v for k, v in est_result.items() if k != 'results'}, indent=2)}")

        if args.dry_run:
            return

    # 3. Vehicle blocks
    if not args.dry_run:
        print("\n── Vehicle Blocks ──")
        veh = sync_vehicle_blocks(dry_run=False)
        print(f"Vehicle blocks: {veh['updated']} updated, {veh['skipped']} skipped")

    if args.skip_compile:
        print("\nSkipping GTFS compilation (--skip-compile).")
        return

    # 3b. Pre-export enforcer — hard gate before any GTFS tables are built
    from datamind_console.phases.phase5_gtfs.pre_export_enforcer import PreExportEnforcer
    print("\n── Pre-Export Enforcer ──")
    with db_conn() as enforcer_conn:
        enforcer = PreExportEnforcer()
        enforcer_report = enforcer.enforce(
            canton=args.canton, province=args.province or "sample_region",
            db_conn=enforcer_conn,
        )
    for check in enforcer_report.checks:
        icon = "PASS" if check.passed else "FAIL"
        line = f"  [{icon}] {check.name}: {check.count} found"
        if check.auto_fixed:
            line += f", {check.auto_fixed} auto-fixed"
        if check.remaining:
            line += f", {check.remaining} remaining"
        if check.details:
            line += f" ({check.details})"
        print(line)
    if enforcer_report.excluded_routes:
        print(f"  Excluded {len(enforcer_report.excluded_routes)} routes from GTFS:")
        for ex in enforcer_report.excluded_routes[:10]:
            print(f"    {ex['route_name']}: {ex['reason']}")
        if len(enforcer_report.excluded_routes) > 10:
            print(f"    ... +{len(enforcer_report.excluded_routes) - 10} more")
    if not enforcer_report.passed:
        print("\nPRE-EXPORT ENFORCER BLOCKED GTFS COMPILATION")
        for reason in enforcer_report.blocking_reasons:
            print(f"   BLOCKED: {reason}")
        print("Fix these issues before retrying.")
        sys.exit(1)
    if enforcer_report.total_auto_fixed > 0:
        print(f"\n  Enforcer auto-fixed {enforcer_report.total_auto_fixed} issues")
    print("  Pre-export enforcer: ALL CHECKS PASS")

    # 4. Create export_run and compile
    from datamind_console.phases.phase5_gtfs.client import Phase5Client
    client = Phase5Client()

    build_name = f"canton_{args.canton}_{date.today().isoformat()}"
    build = client.create_gtfs_build(build_name=build_name)
    gtfs_id = str(build.get("gtfs_id") or build.get("build_id", ""))
    run = client.ensure_export_run_for_gtfs_id(gtfs_id)
    if isinstance(run, dict):
        export_run_id = str(run.get("export_run_id") or run.get("id", ""))
    else:
        export_run_id = str(run)
    print(f"\nCreated build: {gtfs_id}, export_run: {export_run_id}")

    # 5. Compile
    print("\n── Compiling GTFS ──")
    compile_gtfs(export_run_id)

    # 6. Validate
    print("\n── Validation ──")
    report = validate_export_run(export_run_id)
    is_valid = bool(report.get("ok"))
    print(f"Validation: {'PASS' if is_valid else 'FAIL'}")
    if not is_valid:
        print(f"Errors: {report.get('errors', [])}")

    # 7. Package
    if is_valid:
        print("\n── Packaging ──")
        pkg = package_gtfs(export_run_id, require_valid=True)
        print(f"ZIP: {pkg['zip_path']}")
        print(f"Row counts: {pkg['row_counts']}")

    # 8. Summary
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(DISTINCT route_id) AS n FROM gtfs_work.gtfs_routes WHERE export_run_id = %s",
                (export_run_id,),
            )
            row = cur.fetchone()
            n_routes = row["n"] if row else 0

    print(f"\n=== Done: {n_routes} routes in feed, validation={'PASS' if is_valid else 'FAIL'} ===")


if __name__ == "__main__":
    main()
