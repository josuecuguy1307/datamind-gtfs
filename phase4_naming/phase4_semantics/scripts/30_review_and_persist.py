from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phase4_semantics.review.persist import persist_review


def _parse_scores(items: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"Invalid --score '{item}'. Use candidate_id=score")
        cid, value = item.split("=", 1)
        out[cid.strip()] = int(value.strip())
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 4 Step 30: capture review and persist semantics")
    ap.add_argument("--route-id", required=True)
    ap.add_argument("--winner-candidate-id", required=True)
    ap.add_argument("--reviewer", default="console")
    ap.add_argument("--score", action="append", default=[])
    args = ap.parse_args()

    scores = _parse_scores(args.score or [])
    out = persist_review(
        args.route_id,
        args.winner_candidate_id,
        scores,
        reviewer=args.reviewer,
    )
    print("P4_JSON:", json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
