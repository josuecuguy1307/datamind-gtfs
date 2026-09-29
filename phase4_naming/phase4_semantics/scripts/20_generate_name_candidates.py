from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phase4_semantics.naming.candidate_builder import build_top_name_candidates


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 4 Step 20: build top route name candidates")
    ap.add_argument("--route-id", required=True)
    ap.add_argument("--operator", default="")
    ap.add_argument("--route-ref", default="")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--custom-name", action="append", default=[])
    args = ap.parse_args()

    out = build_top_name_candidates(
        args.route_id,
        catalog_inputs={"operator_name": args.operator},
        user_inputs={
            "route_ref": args.route_ref,
            "custom_names": args.custom_name or [],
            "operator_name": args.operator,
        },
        top_k=max(1, int(args.top_k)),
    )
    print("P4_JSON:", json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
