from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phase4_semantics.seed.extract import extract_seed_for_route


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 4 Step 10: extract deterministic naming seed")
    ap.add_argument("--route-id", required=True)
    ap.add_argument("--no-persist", action="store_true")
    args = ap.parse_args()

    out = extract_seed_for_route(args.route_id, persist=not bool(args.no_persist))
    print("P4_JSON:", json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
