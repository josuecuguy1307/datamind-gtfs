"""Batch-run Runtime Lab estimation for all unbound route-directions."""
from __future__ import annotations

import sys
import traceback
from typing import Any, Dict, List, Optional

sys.path.insert(0, ".")

from datamind_console.phases.phase5_gtfs.client import Phase5Client


def get_unbound_route_directions(client: Phase5Client) -> List[Dict[str, Any]]:
    """Return route-directions that have no runtime estimate binding."""
    with client._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH active_routes AS (
                    SELECT r.route_id, r.route_name
                    FROM route_prod.routes r
                    WHERE COALESCE(r.canonical_sequence_ready, FALSE) = TRUE
                      AND r.chosen_stop_sequence_candidate_id IS NOT NULL
                ),
                directions AS (
                    SELECT ar.route_id::text AS route_id, ar.route_name, d.direction_id
                    FROM active_routes ar
                    CROSS JOIN (VALUES (0), (1)) AS d(direction_id)
                )
                SELECT dd.route_id, dd.route_name, dd.direction_id
                FROM directions dd
                LEFT JOIN gtfs_work.route_runtime_estimate_bindings b
                    ON b.route_id::text = dd.route_id AND b.direction_id = dd.direction_id
                WHERE b.estimate_id IS NULL
                ORDER BY dd.route_name, dd.direction_id
                """
            )
            return [dict(r) for r in (cur.fetchall() or [])]


def batch_estimate_and_bind(
    *,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Estimate runtime from catalog and bind for all unbound route-directions.

    Parameters
    ----------
    limit : int, optional
        Max route-directions to process (for testing).
    dry_run : bool
        If True, only list what would be processed.
    """
    client = Phase5Client()
    unbound = get_unbound_route_directions(client)

    if limit:
        unbound = unbound[:limit]

    if dry_run:
        return {
            "dry_run": True,
            "unbound_count": len(unbound),
            "routes": [
                {"route_id": u["route_id"], "route_name": u["route_name"], "direction_id": u["direction_id"]}
                for u in unbound
            ],
        }

    results: List[Dict[str, Any]] = []
    ok = 0
    failed = 0

    for i, u in enumerate(unbound):
        rid = str(u["route_id"])
        did = int(u["direction_id"])
        name = str(u.get("route_name") or rid[:8])
        print(f"  [{i + 1}/{len(unbound)}] {name} d{did} ... ", end="", flush=True)

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
                results.append({"route_id": rid, "direction_id": did, "status": "NO_ID"})
                failed += 1
                continue

            # Bind the estimate
            bind_result = client.bind_runtime_estimate_to_route_direction(
                route_id=rid,
                direction_id=did,
                estimate_id=estimate_id,
            )
            metrics = est.get("metrics") or {}
            off_secs = metrics.get("runtime_offpeak_secs", 0)
            peak_secs = metrics.get("runtime_peak_secs", 0)
            n_legs = metrics.get("n_legs", 0)
            print(f"OK (off={off_secs}s, peak={peak_secs}s, legs={n_legs})")
            results.append({
                "route_id": rid,
                "direction_id": did,
                "status": "OK",
                "estimate_id": estimate_id,
                "offpeak_secs": off_secs,
                "peak_secs": peak_secs,
                "n_legs": n_legs,
            })
            ok += 1

        except Exception as e:
            err_msg = str(e)[:120]
            print(f"FAIL: {err_msg}")
            results.append({
                "route_id": rid,
                "direction_id": did,
                "status": "FAIL",
                "error": err_msg,
            })
            failed += 1

    return {
        "total": len(unbound),
        "ok": ok,
        "failed": failed,
        "results": results,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Batch Runtime Lab estimation")
    parser.add_argument("--limit", type=int, default=None, help="Max routes to process")
    parser.add_argument("--dry-run", action="store_true", help="Only list unbound routes")
    args = parser.parse_args()

    result = batch_estimate_and_bind(limit=args.limit, dry_run=args.dry_run)

    if args.dry_run:
        print(f"\nUnbound route-directions: {result['unbound_count']}")
        for r in result["routes"][:20]:
            print(f"  {r['route_name']} d{r['direction_id']}")
        if result["unbound_count"] > 20:
            print(f"  ... +{result['unbound_count'] - 20} more")
    else:
        print(f"\nDone: {result['ok']} OK, {result['failed']} failed out of {result['total']}")
