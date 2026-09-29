from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

from src.pipeline.extract_job import run_extract
from src.pipeline.normalize_job import run_normalize
from src.pipeline.cluster_job import run_cluster
from src.pipeline.features_job import run_features
from src.pipeline.resolve_job import run_resolve
from src.pipeline.promote_job import run_promote


DEFAULT_BBOX = {"south": -0.38, "west": -78.60, "north": -0.02, "east": -78.35}


def load_actions(actions_path: Path) -> list[dict]:
    data = json.loads(actions_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "actions" in data:
        return data["actions"]
    if isinstance(data, list):
        return data
    raise ValueError("actions.json must be a list of actions or an object with key 'actions'.")


def list_action_ids(actions_path: Path) -> list[str]:
    actions = load_actions(actions_path)
    ids = [a.get("id") for a in actions if isinstance(a, dict) and a.get("id")]
    return sorted(ids)


def parse_bbox(s: str) -> dict:
    # "south,west,north,east"
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be 'south,west,north,east'")
    south, west, north, east = map(float, parts)
    return {"south": south, "west": west, "north": north, "east": east}


def bbox_to_str(bbox: dict) -> str:
    return f"{bbox['south']},{bbox['west']},{bbox['north']},{bbox['east']}"


if __name__ == "__main__":
    SERVICE_ROOT = Path(__file__).resolve().parents[1]
    actions_path = SERVICE_ROOT / "actions.json"

    if load_dotenv:
        load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--list-actions", action="store_true", help="Print available action ids")
    parser.add_argument("--action-id", type=str, default=None, help="Action id from actions.json")
    parser.add_argument("--bbox", type=parse_bbox, default=None, help="south,west,north,east")
    parser.add_argument("--eps-m", type=float, default=35.0, help="DBSCAN eps in meters (default: 35)")
    parser.add_argument("--min-pts", type=int, default=3, help="DBSCAN min points (default: 3)")
    args = parser.parse_args()

    available = list_action_ids(actions_path)

    if args.list_actions:
        print("Available action_id:")
        for a in available:
            print(" -", a)
        raise SystemExit(0)

    action_id = args.action_id
    if not action_id:
        print("Available action_id:")
        for i, a in enumerate(available, 1):
            print(f"{i}. {a}")
        choice = int(input("Choose number: ").strip())
        action_id = available[choice - 1]

    if action_id not in available:
        raise SystemExit(f"Unknown action_id '{action_id}'. Available: {available[:50]} ...")

    # ✅ Always pass bbox (templates need it)
    bbox = args.bbox or DEFAULT_BBOX
    bbox_str = bbox_to_str(bbox)

    params = {
        "bbox": bbox_str,      # for {{bbox}}
        "bbox_str": bbox_str,  # for {{bbox_str}}
        **bbox,                # for {{south}} {{west}} {{north}} {{east}}
    }

    # 1) extract
    out = run_extract(
        action_id=action_id,
        params=params,
        actions_path=str(actions_path),
    )
    run_id = out["run_id"]
    print("\n[extract] OK ✅")
    print("action_id   =", action_id)
    print("run_id      =", run_id)
    print("elements    =", out["elements"])
    print("http_status =", out["http_status"])
    print("runtime_ms  =", out["runtime_ms"])

    # 2) normalize
    norm = run_normalize(run_id)
    print("\n[normalize]", json.dumps(norm, indent=2))

    # 3) cluster
    clu = run_cluster(run_id, eps_m=args.eps_m, min_pts=args.min_pts)
    print("\n[cluster]", json.dumps(clu, indent=2))

    # 4) features
    feat = run_features(run_id)
    print("\n[features]", json.dumps(feat, indent=2))

    # 5) resolve
    res = run_resolve(run_id)
    print("\n[resolve]", json.dumps(res, indent=2))

    # 6) promote
    prom = run_promote(run_id)
    print("\n[promote]", json.dumps(prom, indent=2))

    print("\nDONE ✅ run_id =", run_id)
