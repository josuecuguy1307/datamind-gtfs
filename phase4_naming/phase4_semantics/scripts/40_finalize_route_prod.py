from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phase4_semantics.review.finalize import finalize_route_prod


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 4 Step 40: finalize winner semantics into route_prod.routes")
    ap.add_argument("--route-id", required=True)
    args = ap.parse_args()

    out = finalize_route_prod(args.route_id)
    print("P4_JSON:", json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
