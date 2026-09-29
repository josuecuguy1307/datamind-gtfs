"""
Phase 1 Batch Pipeline Runner
==============================
Runs the full Phase 1 pipeline (normalize → cluster → features → resolve → rank)
for all node_candidate_sets that have been extracted but not yet processed.

Usage:
    cd <project_root>
    python -m phase1_nodes.datamind.services.openmaps_extractor.scripts.batch_pipeline \
        [--all]             # process ALL node sets (including previously processed)
        [--new-only]        # only process node sets missing candidates (default)
        [--node-set-id X]   # process a single node set
        [--skip-rank]       # skip ranking step
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s %(message)s",
)


def _setup_env():
    if load_dotenv:
        load_dotenv()


def get_node_set_ids(mode: str = "new_only", single_id: Optional[str] = None) -> List[Dict[str, Any]]:
    from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import db_conn, fetchall

    with db_conn() as conn:
        if single_id:
            rows = fetchall(
                conn,
                """
                SELECT node_set_id, params_used, source_run_ids
                FROM node_work.node_candidate_sets
                WHERE node_set_id = %s::uuid
                """,
                (single_id,),
            )
        elif mode == "all":
            rows = fetchall(
                conn,
                """
                SELECT ns.node_set_id, ns.params_used, ns.source_run_ids
                FROM node_work.node_candidate_sets ns
                ORDER BY ns.created_at
                """,
            )
        else:
            # new_only: node sets that don't have candidates yet
            rows = fetchall(
                conn,
                """
                SELECT ns.node_set_id, ns.params_used, ns.source_run_ids
                FROM node_work.node_candidate_sets ns
                WHERE NOT EXISTS (
                    SELECT 1 FROM node_work.node_candidates nc
                    WHERE nc.node_set_id = ns.node_set_id
                )
                ORDER BY ns.created_at
                """,
            )
    return [dict(r) for r in rows]


def get_node_set_state(node_set_id: str) -> Dict[str, Any]:
    from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import db_conn, fetchone

    with db_conn() as conn:
        row = fetchone(
            conn,
            """
            WITH c AS (
              SELECT COUNT(*)::int AS candidate_count
              FROM node_work.node_candidates
              WHERE node_set_id = %s::uuid
            ),
            f AS (
              SELECT COUNT(*)::int AS feature_count
              FROM node_work.node_candidates c
              JOIN node_work.node_features f
                ON f.node_candidate_id = c.node_candidate_id
              WHERE c.node_set_id = %s::uuid
            ),
            cl AS (
              SELECT
                COUNT(*)::int AS cluster_assignment_count,
                COUNT(DISTINCT cluster_id)::int AS cluster_count
              FROM node_work.node_clusters
              WHERE node_set_id = %s::uuid
            ),
            r AS (
              SELECT
                COUNT(*)::int AS resolved_count,
                COUNT(*) FILTER (WHERE status = 'approved')::int AS approved_count,
                COUNT(*) FILTER (WHERE status = 'work')::int AS work_count,
                COUNT(*) FILTER (WHERE status = 'rejected')::int AS rejected_count
              FROM node_work.nodes_resolved
              WHERE node_set_id = %s::uuid
            ),
            p AS (
              SELECT COUNT(*)::int AS prod_count
              FROM node_prod.nodes
              WHERE source_node_set_id = %s::uuid
            )
            SELECT
              %s::uuid AS node_set_id,
              COALESCE((SELECT candidate_count FROM c), 0) AS candidate_count,
              COALESCE((SELECT feature_count FROM f), 0) AS feature_count,
              COALESCE((SELECT cluster_assignment_count FROM cl), 0) AS cluster_assignment_count,
              COALESCE((SELECT cluster_count FROM cl), 0) AS cluster_count,
              COALESCE((SELECT resolved_count FROM r), 0) AS resolved_count,
              COALESCE((SELECT approved_count FROM r), 0) AS approved_count,
              COALESCE((SELECT work_count FROM r), 0) AS work_count,
              COALESCE((SELECT rejected_count FROM r), 0) AS rejected_count,
              COALESCE((SELECT prod_count FROM p), 0) AS prod_count,
              (
                SELECT rank_score
                FROM node_work.node_candidate_sets
                WHERE node_set_id = %s::uuid
              ) AS rank_score
            """,
            (node_set_id, node_set_id, node_set_id, node_set_id, node_set_id, node_set_id, node_set_id),
        )
    return dict(row or {})


def _phase2_eligible(state: Dict[str, Any]) -> bool:
    return int(state.get("prod_count") or 0) > 0


def run_pipeline_for_node_set(
    node_set_id: str,
    eps_m: float = 35.0,
    min_pts: int = 3,
    skip_rank: bool = False,
    promote_approved: bool = False,
) -> Dict[str, Any]:
    from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.normalize_job import run_normalize
    from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.cluster_job import run_cluster
    from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.features_job import run_features
    from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.resolve_job import run_resolve
    from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.rank_sets_job import run_rank_set
    from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.promote_job import run_promote

    result: Dict[str, Any] = {
        "node_set_id": node_set_id,
        "already_present_beforehand": True,
        "status": "ok",
        "steps": {},
        "executed_steps": [],
        "skipped_steps": [
            {
                "step": "discovery",
                "reason": "node_set already existed; initial Phase 1 discovery intentionally skipped",
            }
        ],
        "errors": [],
    }

    try:
        state_before = get_node_set_state(node_set_id)
        result["state_before"] = state_before
        state = dict(state_before)
        reranked = False

        # 1) Normalize only when candidates are still missing.
        if int(state.get("candidate_count") or 0) == 0:
            norm = run_normalize(node_set_id)
            result["steps"]["normalize"] = norm
            result["executed_steps"].append("normalize")
            state = get_node_set_state(node_set_id)
        else:
            result["skipped_steps"].append(
                {
                    "step": "normalize",
                    "reason": f"already materialized with {int(state.get('candidate_count') or 0)} candidates",
                }
            )

        candidates = int(state.get("candidate_count") or 0)
        if candidates == 0:
            result["status"] = "empty"
            result["candidates"] = 0
            result["state_after"] = state
            result["phase2_can_proceed"] = _phase2_eligible(state)
            return result

        # 2) Features are safe to upsert whenever missing.
        if int(state.get("feature_count") or 0) < candidates:
            feat = run_features(node_set_id)
            result["steps"]["features"] = feat
            result["executed_steps"].append("features")
            state = get_node_set_state(node_set_id)
        else:
            result["skipped_steps"].append(
                {
                    "step": "features",
                    "reason": f"already materialized with {int(state.get('feature_count') or 0)} feature rows",
                }
            )

        # 3) Preserve reviewed workspace if resolutions already exist.
        if int(state.get("resolved_count") or 0) > 0:
            result["skipped_steps"].append(
                {
                    "step": "cluster",
                    "reason": (
                        f"preserved existing resolved workspace ({int(state.get('resolved_count') or 0)} rows); "
                        "cluster rerun would replace downstream state"
                    ),
                }
            )
            result["skipped_steps"].append(
                {
                    "step": "resolve",
                    "reason": (
                        f"preserved existing resolved workspace ({int(state.get('resolved_count') or 0)} rows); "
                        "resolve rerun would reset reviewed statuses"
                    ),
                }
            )
        else:
            if int(state.get("cluster_assignment_count") or 0) < candidates:
                clu = run_cluster(node_set_id, eps_m=eps_m, min_pts=min_pts)
                result["steps"]["cluster"] = clu
                result["executed_steps"].append("cluster")
                state = get_node_set_state(node_set_id)
            else:
                result["skipped_steps"].append(
                    {
                        "step": "cluster",
                        "reason": (
                            f"already materialized with {int(state.get('cluster_assignment_count') or 0)} "
                            "cluster assignments"
                        ),
                    }
                )

            if int(state.get("cluster_assignment_count") or 0) > 0:
                res = run_resolve(node_set_id)
                result["steps"]["resolve"] = res
                result["executed_steps"].append("resolve")
                state = get_node_set_state(node_set_id)
            else:
                result["skipped_steps"].append(
                    {
                        "step": "resolve",
                        "reason": "no cluster assignments available after clustering",
                    }
                )

        # 4) Rank is safe; only run when missing or after upstream mutation.
        if not skip_rank:
            should_rank = bool(result["executed_steps"]) or state.get("rank_score") is None
            if should_rank:
                rank = run_rank_set(node_set_id)
                result["steps"]["rank"] = rank
                result["executed_steps"].append("rank")
                reranked = True
                state = get_node_set_state(node_set_id)
            else:
                result["skipped_steps"].append(
                    {
                        "step": "rank",
                        "reason": f"rank already present ({state.get('rank_score')})",
                    }
                )
        else:
            result["skipped_steps"].append(
                {
                    "step": "rank",
                    "reason": "rank explicitly skipped by CLI flag",
                }
            )

        # 5) Promote only when approved rows already exist.
        if promote_approved:
            if int(state.get("prod_count") or 0) > 0:
                result["skipped_steps"].append(
                    {
                        "step": "promote",
                        "reason": f"already promoted with {int(state.get('prod_count') or 0)} node_prod rows",
                    }
                )
            elif int(state.get("approved_count") or 0) > 0:
                prom = run_promote(node_set_id)
                result["steps"]["promote"] = prom
                result["executed_steps"].append("promote")
                state = get_node_set_state(node_set_id)
            elif int(state.get("resolved_count") or 0) > 0:
                result["skipped_steps"].append(
                    {
                        "step": "promote",
                        "reason": (
                            f"resolved workspace exists but approved_count={int(state.get('approved_count') or 0)}; "
                            "promotion gate not satisfied"
                        ),
                    }
                )
            else:
                result["skipped_steps"].append(
                    {
                        "step": "promote",
                        "reason": "no resolved workspace available for promotion",
                    }
                )
        else:
            result["skipped_steps"].append(
                {
                    "step": "promote",
                    "reason": "promotion disabled for this batch run",
                }
            )

        if result["errors"]:
            result["status"] = "partial"

        result["candidates"] = int(state.get("candidate_count") or 0)
        result["clusters"] = int(state.get("cluster_assignment_count") or 0)
        result["cluster_count"] = int(state.get("cluster_count") or 0)
        result["features"] = int(state.get("feature_count") or 0)
        result["resolved"] = int(state.get("resolved_count") or 0)
        result["approved"] = int(state.get("approved_count") or 0)
        result["work"] = int(state.get("work_count") or 0)
        result["rejected"] = int(state.get("rejected_count") or 0)
        result["promoted"] = int(state.get("prod_count") or 0)
        result["rank_score"] = state.get("rank_score")
        result["state_after"] = state
        result["phase2_can_proceed"] = _phase2_eligible(state)
        if reranked and "rank" not in result["executed_steps"]:
            result["executed_steps"].append("rank")

    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()
        result["errors"].append(str(exc))
        try:
            result["state_after"] = get_node_set_state(node_set_id)
            result["phase2_can_proceed"] = _phase2_eligible(result["state_after"])
        except Exception:
            result["state_after"] = None
            result["phase2_can_proceed"] = False

    return result


def run_batch_pipeline(
    mode: str = "new_only",
    single_id: Optional[str] = None,
    eps_m: float = 35.0,
    min_pts: int = 3,
    skip_rank: bool = False,
    promote_approved: bool = False,
) -> Dict[str, Any]:
    node_sets = get_node_set_ids(mode=mode, single_id=single_id)

    print(f"\n{'='*70}")
    print(f"Phase 1 Batch Pipeline")
    print(f"{'='*70}")
    print(f"Node sets to process: {len(node_sets)}")
    print(f"Mode:                 {mode}")
    print(f"DBSCAN:               eps_m={eps_m}, min_pts={min_pts}")
    print(f"Skip rank:            {skip_rank}")
    print(f"Promote approved:     {promote_approved}")
    print(f"{'='*70}\n")

    if not node_sets:
        print("No node sets to process.")
        return {"total": 0, "results": []}

    all_results: List[Dict[str, Any]] = []
    total_ok = 0
    total_partial = 0
    total_empty = 0
    total_error = 0
    total_candidates = 0
    total_resolved = 0
    total_phase2_eligible = 0

    for i, ns in enumerate(node_sets, 1):
        ns_id = str(ns["node_set_id"])
        params = ns.get("params_used") or {}
        place = params.get("place_name", "")
        action = params.get("action_id", "")
        label = f"{place} / {action}" if place else ns_id[:12]

        sys.stdout.write(f"[{i}/{len(node_sets)}] {label} ... ")
        sys.stdout.flush()

        t0 = time.time()
        result = run_pipeline_for_node_set(
            ns_id,
            eps_m=eps_m,
            min_pts=min_pts,
            skip_rank=skip_rank,
            promote_approved=promote_approved,
        )
        elapsed = time.time() - t0

        result["place_name"] = place
        result["action_id"] = action
        result["elapsed_s"] = round(elapsed, 2)

        status = result["status"]
        if status == "ok":
            total_ok += 1
            cand = result.get("candidates", 0)
            resolved = result.get("resolved", 0)
            rank = result.get("rank_score")
            total_candidates += cand
            total_resolved += resolved
            total_phase2_eligible += int(bool(result.get("phase2_can_proceed")))
            rank_str = f" rank={rank:.3f}" if rank is not None else ""
            print(
                f"OK  cand={cand} resolved={resolved} promoted={int(result.get('promoted') or 0)}"
                f"{rank_str}  ({elapsed:.1f}s)"
            )
        elif status == "partial":
            total_partial += 1
            cand = result.get("candidates", 0)
            resolved = result.get("resolved", 0)
            total_candidates += cand
            total_resolved += resolved
            total_phase2_eligible += int(bool(result.get("phase2_can_proceed")))
            err = "; ".join(result.get("errors") or [])[:120]
            print(
                f"PARTIAL cand={cand} resolved={resolved} promoted={int(result.get('promoted') or 0)} "
                f"err={err}  ({elapsed:.1f}s)"
            )
        elif status == "empty":
            total_empty += 1
            print(f"EMPTY (no candidates)  ({elapsed:.1f}s)")
        else:
            total_error += 1
            err = result.get("error", "")[:80]
            print(f"ERROR: {err}  ({elapsed:.1f}s)")

        all_results.append(result)

    print(f"\n{'='*70}")
    print(f"BATCH PIPELINE COMPLETE")
    print(f"{'='*70}")
    print(f"Total processed:  {len(node_sets)}")
    print(f"  OK:             {total_ok}")
    print(f"  Partial:        {total_partial}")
    print(f"  Empty:          {total_empty}")
    print(f"  Error:          {total_error}")
    print(f"Total candidates: {total_candidates}")
    print(f"Total resolved:   {total_resolved}")
    print(f"Phase2 eligible:  {total_phase2_eligible}")
    print(f"{'='*70}")

    return {
        "total": len(node_sets),
        "ok": total_ok,
        "partial": total_partial,
        "empty": total_empty,
        "error": total_error,
        "total_candidates": total_candidates,
        "total_resolved": total_resolved,
        "phase2_eligible": total_phase2_eligible,
        "results": all_results,
    }


if __name__ == "__main__":
    _setup_env()

    parser = argparse.ArgumentParser(description="Phase 1 Batch Pipeline")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="Process ALL node sets")
    group.add_argument("--new-only", action="store_true", default=True, help="Process only unprocessed node sets (default)")
    group.add_argument("--node-set-id", type=str, default=None, help="Process a single node set")
    parser.add_argument("--eps-m", type=float, default=35.0, help="DBSCAN eps in meters (default: 35)")
    parser.add_argument("--min-pts", type=int, default=3, help="DBSCAN min points (default: 3)")
    parser.add_argument("--skip-rank", action="store_true", help="Skip the ranking step")
    parser.add_argument(
        "--promote-approved",
        action="store_true",
        help="Promote node sets that already have approved resolved rows but are not yet in node_prod",
    )
    args = parser.parse_args()

    if args.node_set_id:
        mode = "single"
    elif args.all:
        mode = "all"
    else:
        mode = "new_only"

    summary = run_batch_pipeline(
        mode=mode,
        single_id=args.node_set_id,
        eps_m=args.eps_m,
        min_pts=args.min_pts,
        skip_rank=args.skip_rank,
        promote_approved=args.promote_approved,
    )
