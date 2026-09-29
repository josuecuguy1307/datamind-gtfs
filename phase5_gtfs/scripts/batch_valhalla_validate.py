#!/usr/bin/env python3
"""
Run Valhalla cross-validation for all routes with runtime estimate bindings.
Flags routes where catalog estimate diverges >50% from Valhalla free-flow.

Usage:
    python -m phase5_gtfs.scripts.batch_valhalla_validate
    python -m phase5_gtfs.scripts.batch_valhalla_validate --limit 20
    python -m phase5_gtfs.scripts.batch_valhalla_validate --threshold 2.0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DB_DSN", "postgresql://localhost:5432/datamind_ml")

from phase5_gtfs.common.config import db_conn
from phase5_gtfs.runtime.valhalla_cross_validate import cross_validate_against_valhalla


def get_bound_route_directions(limit: int | None = None) -> List[Dict[str, Any]]:
    """Get all route-directions with active runtime bindings."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT b.route_id::text AS route_id,
                       b.direction_id,
                       r.route_name,
                       (e.metrics->>'runtime_offpeak_secs')::numeric AS offpeak_secs,
                       (e.metrics->>'runtime_peak_secs')::numeric AS peak_secs
                FROM gtfs_work.route_runtime_estimate_bindings b
                JOIN route_prod.routes r ON r.route_id::text = b.route_id::text
                JOIN gtfs_work.runtime_route_estimates e ON e.estimate_id = b.estimate_id
                ORDER BY r.route_name, b.direction_id
            """
            if limit:
                sql += f" LIMIT {int(limit)}"
            cur.execute(sql)
            return [dict(row) for row in (cur.fetchall() or [])]


def main():
    parser = argparse.ArgumentParser(description="Batch Valhalla cross-validation")
    parser.add_argument("--limit", type=int, default=None, help="Max route-directions to validate")
    parser.add_argument("--threshold", type=float, default=1.5, help="Flag routes with ratio above this (default 1.5)")
    parser.add_argument("--output", type=str, default=None, help="Write results JSON to file")
    args = parser.parse_args()

    print(f"=== Batch Valhalla Cross-Validation ===")
    print(f"Threshold: {args.threshold}x\n")

    bindings = get_bound_route_directions(limit=args.limit)
    print(f"Found {len(bindings)} bound route-directions\n")

    results: List[Dict[str, Any]] = []
    flagged: List[Dict[str, Any]] = []
    errors = 0
    no_data = 0

    for i, b in enumerate(bindings):
        rid = str(b["route_id"])
        did = int(b["direction_id"])
        name = str(b.get("route_name") or rid[:8])
        print(f"  [{i + 1}/{len(bindings)}] {name} d{did} ... ", end="", flush=True)

        try:
            result = cross_validate_against_valhalla(rid, did)
            if result is None:
                print("NO_DATA")
                no_data += 1
                continue

            ratio = result.get("avg_catalog_to_valhalla_offpeak")
            interp = result.get("interpretation", "")
            legs = result.get("legs_compared", 0)
            print(f"ratio={ratio} ({legs} legs) — {interp[:40]}")

            entry = {
                "route_id": rid,
                "direction_id": did,
                "route_name": name,
                "ratio_offpeak": ratio,
                "ratio_peak": result.get("avg_catalog_to_valhalla_peak"),
                "legs_compared": legs,
                "interpretation": interp,
                "catalog_offpeak_secs": float(b.get("offpeak_secs") or 0),
                "catalog_peak_secs": float(b.get("peak_secs") or 0),
            }
            results.append(entry)

            if ratio is not None and (ratio < 0.8 or ratio > args.threshold):
                entry["flag"] = "DIVERGENT"
                flagged.append(entry)

        except Exception as e:
            print(f"ERROR: {str(e)[:80]}")
            errors += 1

    # Summary
    print(f"\n{'='*60}")
    print(f"Validated: {len(results)}")
    print(f"No data:   {no_data}")
    print(f"Errors:    {errors}")
    print(f"Flagged:   {len(flagged)} (ratio <0.8 or >{args.threshold})")

    if flagged:
        print(f"\n── Flagged Routes ──")
        for f in sorted(flagged, key=lambda x: abs((x.get("ratio_offpeak") or 1.0) - 1.0), reverse=True):
            r = f.get("ratio_offpeak", "?")
            print(f"  {f['route_name'][:40]:40s} d{f['direction_id']} ratio={r} — {f.get('interpretation', '')[:30]}")

    if args.output:
        output_path = os.path.expanduser(args.output)
        with open(output_path, "w") as fp:
            json.dump(
                {"results": results, "flagged": flagged, "summary": {"total": len(results), "flagged": len(flagged), "errors": errors}},
                fp, indent=2, default=str,
            )
        print(f"\nResults written to {output_path}")


if __name__ == "__main__":
    main()
