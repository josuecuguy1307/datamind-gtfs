from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phase4_semantics.common.db import fetchall
from phase4_semantics.seed.extract import extract_seed_for_route
from phase4_semantics.naming.candidate_builder import build_top_name_candidates
from phase4_semantics.review.persist import persist_review


def _load_routes(limit: Optional[int]) -> List[str]:
    sql = """
    SELECT route_id
    FROM route_raw.route_jobs
    ORDER BY created_at DESC NULLS LAST
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = fetchall(sql)
    return [str(r["route_id"]) for r in rows if r.get("route_id")]


def _auto_scores(candidates: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for idx, c in enumerate(candidates):
        cid = str(c.get("candidate_id"))
        out[cid] = max(1, 5 - idx)
    return out


def run_phase4(
    *,
    route_id: Optional[str] = None,
    limit: Optional[int] = None,
    start_step: int = 10,
    stop_after_step: Optional[int] = None,
    top_k: int = 5,
    auto_review: bool = False,
    reviewer: str = "console",
) -> Dict[str, Any]:
    route_ids = [route_id] if route_id else _load_routes(limit)
    stop = stop_after_step if stop_after_step is not None else 30

    report: Dict[str, Any] = {"ok": True, "routes": []}

    for rid in route_ids:
        rid = str(rid)
        row: Dict[str, Any] = {"route_id": rid, "steps": []}

        candidates: List[Dict[str, Any]] = []

        if start_step <= 10 <= stop:
            seed = extract_seed_for_route(rid, persist=True)
            row["steps"].append({"step": 10, "seed_source": seed.get("seed_source")})

        if start_step <= 20 <= stop:
            candidates = build_top_name_candidates(rid, top_k=top_k)
            row["steps"].append({"step": 20, "n_candidates": len(candidates)})

        if start_step <= 30 <= stop and auto_review and candidates:
            winner = str(candidates[0].get("candidate_id"))
            scores = _auto_scores(candidates)
            review = persist_review(rid, winner, scores, reviewer=reviewer)
            row["steps"].append(
                {
                    "step": 30,
                    "winner_candidate_id": review.get("winner_candidate_id"),
                    "saved_feedback": review.get("saved_feedback"),
                }
            )

        report["routes"].append(row)

    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 4 naming orchestrator (steps 10/20/30)")
    ap.add_argument("--route-id", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--start-step", type=int, default=10)
    ap.add_argument("--stop-after-step", type=int, default=None)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--auto-review", action="store_true")
    ap.add_argument("--reviewer", default="console")
    args = ap.parse_args()

    out = run_phase4(
        route_id=args.route_id,
        limit=args.limit,
        start_step=int(args.start_step),
        stop_after_step=args.stop_after_step,
        top_k=max(1, int(args.top_k)),
        auto_review=bool(args.auto_review),
        reviewer=str(args.reviewer),
    )
    print("P4_JSON:", json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
