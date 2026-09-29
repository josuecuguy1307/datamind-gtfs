from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


DEFAULT_BBOX = {"south": -0.220, "west": -78.515, "north": -0.210, "east": -78.505}


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


def choose_action_interactive(available: list[str]) -> str:
    print("Available action_id:")
    for i, a in enumerate(available, 1):
        print(f"{i}. {a}")

    raw = input("Choose number (or q to quit): ").strip()

    # ESC is '\x1b'
    if raw in {"", "q", "quit", "exit", "\x1b"}:
        print("Cancelled.")
        raise SystemExit(0)

    try:
        choice = int(raw)
    except ValueError:
        raise SystemExit(f"Invalid choice: {raw!r} (type a number)")

    if not (1 <= choice <= len(available)):
        raise SystemExit(f"Choice out of range: {choice}")

    return available[choice - 1]


def _bbox_to_str(bbox: dict) -> str:
    return f"{bbox['south']},{bbox['west']},{bbox['north']},{bbox['east']}"


def run_full_pipeline(actions_path: Path, action_id: str, bbox: dict, eps_m: float, min_pts: int) -> dict:
    """
    Runs the full Phase 1 pipeline.

    IMPORTANT FIX:
    Overpass queries typically expect bbox as:
      - {{bbox}} or {{bbox_str}} formatted string "south,west,north,east"
      OR
      - {{south}} {{west}} {{north}} {{east}}

    So we pass both formats:
      params["bbox"]     -> string
      params["bbox_str"] -> string
      params["south"/"west"/"north"/"east"] -> floats
    """

    # Lazy imports so "--list-actions" never fails due to ML deps
    from src.pipeline.extract_job import run_extract
    from src.pipeline.normalize_job import run_normalize
    from src.pipeline.cluster_job import run_cluster
    from src.pipeline.features_job import run_features
    from src.pipeline.resolve_job import run_resolve
    from src.pipeline.promote_job import run_promote

    bbox_str = _bbox_to_str(bbox)

    # 1) extract
    out = run_extract(
        action_id=action_id,
        params={
            "bbox": bbox_str,      # for {{bbox}}
            "bbox_str": bbox_str,  # for {{bbox_str}}
            **bbox,                # for {{south}} {{west}} {{north}} {{east}}
        },
        actions_path=str(actions_path),
    )
    run_id = out["run_id"]

    print("\n[extract] OK ✅")
    print("action_id   =", action_id)
    print("run_id      =", run_id)
    print("elements    =", out.get("elements"))
    print("http_status =", out.get("http_status"))
    print("runtime_ms  =", out.get("runtime_ms"))
    if "template" in out:
        print("template    =", out["template"])

    # 2) normalize
    norm = run_normalize(run_id)
    print("\n[normalize]", json.dumps(norm, indent=2))

    # 3) cluster
    clu = run_cluster(run_id, eps_m=eps_m, min_pts=min_pts)
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
    return {
        "action_id": action_id,
        "run_id": run_id,
        "extract": out,
        "normalize": norm,
        "cluster": clu,
        "features": feat,
        "resolve": res,
        "promote": prom,
    }


if __name__ == "__main__":
    SERVICE_ROOT = Path(__file__).resolve().parents[1]
    actions_path = SERVICE_ROOT / "actions.json"

    if load_dotenv:
        load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--list-actions", action="store_true", help="Print available action ids")
    parser.add_argument("--action-id", type=str, default=None, help="Action id from actions.json (optional)")
    parser.add_argument("--bbox", type=parse_bbox, default=None, help="south,west,north,east (optional)")
    parser.add_argument("--eps-m", type=float, default=35.0, help="DBSCAN eps in meters (default: 35)")
    parser.add_argument("--min-pts", type=int, default=3, help="DBSCAN min points (default: 3)")
    args = parser.parse_args()

    available = list_action_ids(actions_path)

    if args.list_actions:
        print("Available action_id:")
        for a in available:
            print(" -", a)
        raise SystemExit(0)

    bbox = args.bbox or DEFAULT_BBOX
    action_id = args.action_id or choose_action_interactive(available)

    if action_id not in available:
        raise SystemExit(f"Unknown action_id '{action_id}'. Available: {available[:50]} ...")

    run_full_pipeline(actions_path, action_id, bbox, eps_m=args.eps_m, min_pts=args.min_pts)
