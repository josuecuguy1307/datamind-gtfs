from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from src.db.repo import db_conn, exec_sql
from src.settings import DEFAULT_BBOX, T_RESOLVED
from src.pipeline.build_node_set_job import run_build_node_set
from src.pipeline.normalize_job import run_normalize
from src.pipeline.features_job import run_features
from src.pipeline.cluster_job import run_cluster
from src.pipeline.resolve_job import run_resolve
from src.pipeline.rank_sets_job import run_rank_set
from src.pipeline.promote_job import run_promote


def parse_bbox(s: str) -> str:
    """
    CLI bbox format:
      "south,west,north,east"

    Returns Overpass-ready bbox string:
      "south,west,north,east"
    """
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be 'south,west,north,east'")
    south, west, north, east = map(float, parts)
    return f"{south},{west},{north},{east}"


def bbox_to_overpass(b) -> str:
    """
    DEFAULT_BBOX is a dict, but templates need a string.
    If bbox already is a string, pass through.
    """
    if isinstance(b, str):
        return b
    return f"{b['south']},{b['west']},{b['north']},{b['east']}"


def load_actions_json(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or "actions" not in data:
        raise ValueError("actions.json must contain key 'actions'")

    return data["actions"]


def main():
    root = Path(__file__).resolve().parents[1]
    actions_path = root / "actions.json"

    parser = argparse.ArgumentParser()
    parser.add_argument("--list-actions", action="store_true")
    parser.add_argument("--action-id", type=str, default=None, help="single action id (optional)")
    parser.add_argument("--bbox", type=parse_bbox, default=None)
    parser.add_argument("--k", type=int, default=1, help="how many node_sets to generate")
    parser.add_argument("--eps-m", type=float, default=35.0)
    parser.add_argument("--min-pts", type=int, default=3)

    parser.add_argument("--promote", action="store_true", help="promote approved nodes into node_prod.nodes")
    parser.add_argument("--auto-approve", action="store_true", help="DEV: set nodes_resolved.status='approved' for this node_set")
    args = parser.parse_args()

    actions = load_actions_json(actions_path)

    if args.list_actions:
        for a in actions:
            print("-", a["id"])
        return

    # ✅ IMPORTANT: bbox must be a string for Overpass templates
    bbox = args.bbox or DEFAULT_BBOX
    base_params = {"bbox": bbox_to_overpass(bbox)}

    if args.action_id:
        candidate_actions = [args.action_id]
    else:
        candidate_actions = sorted(a["id"] for a in actions)

    decision_id = uuid4()

    for i in range(args.k):
        node_set = run_build_node_set(
            actions_path=str(actions_path),
            candidate_actions=candidate_actions,
            base_params=base_params,
            n_runs=1,
        )
        node_set_id = node_set["node_set_id"]

        print(
            f"\n[node_set {i+1}/{args.k}] node_set_id={node_set_id} "
            f"actions={node_set['action_ids']} runs={node_set['run_ids']}"
        )

        print("[normalize]", run_normalize(node_set_id))
        print("[features]", run_features(node_set_id))
        print("[cluster ]", run_cluster(node_set_id, eps_m=args.eps_m, min_pts=args.min_pts))
        print("[resolve ]", run_resolve(node_set_id))
        print("[rank   ]", run_rank_set(node_set_id))

        if args.auto_approve:
            with db_conn() as conn:
                exec_sql(
                    conn,
                    f"""
                    UPDATE {T_RESOLVED}
                    SET status = 'approved'
                    WHERE node_set_id = %s
                    """,
                    (node_set_id,),
                )
            print("[approve]", {"node_set_id": node_set_id, "status": "approved"})

        if args.promote:
            print("[promote]", run_promote(node_set_id))

    print("\nDONE ✅ decision_id =", decision_id)


if __name__ == "__main__":
    main()
