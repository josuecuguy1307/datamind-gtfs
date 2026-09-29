#!/usr/bin/env python3
"""
Run Pipeline A on all unnamed routes from a specific canton/area.

Usage:
    cd phase4_naming
    PYTHONPATH=. python scripts/name_canton_routes.py --canton cayambe
    PYTHONPATH=. python scripts/name_canton_routes.py --canton cayambe --dry-run
    PYTHONPATH=. python scripts/name_canton_routes.py --all-unnamed
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phase4_semantics.common.db import fetchall, get_conn
from phase4_semantics.seed.extract import extract_seed_for_route
from phase4_semantics.naming.candidate_builder import build_top_name_candidates
from phase4_semantics.naming.scoring import score_candidate
from phase4_semantics.review.finalize import finalize_route_prod


def _find_unnamed_routes(canton: str | None = None) -> List[Dict[str, Any]]:
    """Find routes with no name or route_name = ''."""
    base = """
        SELECT r.route_id::text AS route_id,
               r.route_name,
               r.province AS sector_key
        FROM route_prod.routes r
        WHERE (r.route_name IS NULL OR r.route_name = '')
    """
    params: list = []
    if canton:
        base += " AND LOWER(COALESCE(r.province, '')) LIKE %s"
        params.append(f"%{canton.lower()}%")
    base += " ORDER BY r.route_id"
    return fetchall(base, params)


def _find_routes_missing_catalog(canton: str | None = None) -> List[Dict[str, Any]]:
    """Find routes missing catalog.route_semantics entries."""
    base = """
        SELECT r.route_id::text AS route_id,
               r.route_name,
               v.sector_key
        FROM route_prod.routes r
        LEFT JOIN route_review.route_interpretation_v1 v
            ON v.route_id = r.route_id
        WHERE NOT EXISTS (
            SELECT 1 FROM catalog.route_semantics cs
            WHERE cs.route_id = r.route_id
        )
    """
    params: list = []
    if canton:
        base += " AND LOWER(COALESCE(v.sector_key, '')) LIKE %s"
        params.append(f"%{canton.lower()}%")
    base += " ORDER BY r.route_name"
    return fetchall(base, params)


def name_route(route_id: str, auto_accept: bool = True) -> Dict[str, Any]:
    """Run full Pipeline A for one route: seed -> candidates -> score -> finalize."""
    result: Dict[str, Any] = {"route_id": route_id, "status": "ok"}

    # Step 1: Extract seed
    try:
        seed = extract_seed_for_route(route_id, persist=True)
    except Exception as e:
        result["status"] = "seed_failed"
        result["error"] = str(e)
        return result

    if not seed or seed.get("error"):
        result["status"] = "seed_empty"
        result["error"] = seed.get("error", "empty seed")
        return result

    # Step 2: Build candidates
    try:
        candidates = build_top_name_candidates(route_id)
    except Exception as e:
        result["status"] = "candidates_failed"
        result["error"] = str(e)
        return result

    if not candidates:
        result["status"] = "no_candidates"
        return result

    # Step 3: Score and pick best
    scored = []
    for c in candidates:
        score, breakdown = score_candidate(c)
        scored.append({**c, "_score": score, "_breakdown": breakdown})
    scored.sort(key=lambda x: x["_score"], reverse=True)
    best = scored[0]
    result["best_name"] = best.get("route_long_name", best.get("candidate_name", "?"))
    result["score"] = best["_score"]

    # Step 4: Finalize (write to route_prod)
    if auto_accept:
        try:
            fin = finalize_route_prod(route_id)
            result["finalized"] = True
            result["finalize_result"] = fin
        except Exception as e:
            result["status"] = "finalize_failed"
            result["error"] = str(e)
            return result

    return result


def main():
    parser = argparse.ArgumentParser(description="Name unnamed routes via Pipeline A")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--canton", help="Filter by sector_key (e.g., cayambe)")
    group.add_argument("--all-unnamed", action="store_true", help="Process all unnamed routes")
    group.add_argument("--missing-catalog", action="store_true",
                       help="Process routes missing catalog.route_semantics")
    parser.add_argument("--dry-run", action="store_true", help="Show routes but don't process")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of routes to process")
    args = parser.parse_args()

    canton = args.canton if args.canton else None

    if args.missing_catalog:
        routes = _find_routes_missing_catalog(canton)
        print(f"Found {len(routes)} routes missing catalog data")
    else:
        routes = _find_unnamed_routes(canton)
        print(f"Found {len(routes)} unnamed routes" +
              (f" matching '{canton}'" if canton else ""))

    if args.limit > 0:
        routes = routes[:args.limit]
        print(f"  (limited to {args.limit})")

    if not routes:
        print("Nothing to do.")
        return

    if args.dry_run:
        print("\n[DRY RUN] Would process:")
        for r in routes:
            name = r.get('route_name') or '(unnamed)'
            print(f"  {name:50s}  sector={r.get('sector_key', '?')}")
        return

    ok = 0
    fail = 0
    for i, r in enumerate(routes, 1):
        rid = r["route_id"]
        name = r.get("route_name") or "(unnamed)"
        print(f"\n[{i}/{len(routes)}] {name} ({rid})")

        result = name_route(rid)
        if result["status"] == "ok":
            print(f"  -> {result.get('best_name', '?')} (score={result.get('score', 0):.2f})")
            ok += 1
        else:
            print(f"  FAILED: {result['status']} - {result.get('error', '')}")
            fail += 1

    print(f"\n--- Summary ---")
    print(f"  OK: {ok}  |  FAILED: {fail}  |  TOTAL: {ok + fail}")


if __name__ == "__main__":
    main()
