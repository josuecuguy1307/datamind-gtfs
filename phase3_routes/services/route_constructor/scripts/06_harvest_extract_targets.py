from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


REPO_ROOT = _repo_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from datamind_console.phases.phase3_routes.client import Phase3Client  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets-path", required=True, help="Path to phase3_extract_targets.json")
    ap.add_argument("--minimum-goal", type=int, default=50)
    ap.add_argument("--max-candidates", type=int, default=12)
    ap.add_argument("--timeout-s", type=int, default=120)
    ap.add_argument("--max-attempts", type=int, default=None)
    ap.add_argument("--skip-fetch", action="store_true", help="Only run Step 05 discover without Step 10 fetch")
    ap.add_argument(
        "--disable-ai-assist",
        action="store_true",
        help="Disable geography advisory usage even if configured. Deterministic/geocoder fallback still runs.",
    )
    args = ap.parse_args()

    client = Phase3Client()
    out = client.run_phase3_extractor_harvest(
        targets_path=args.targets_path,
        minimum_goal=int(args.minimum_goal),
        max_candidates=int(args.max_candidates),
        timeout_s=int(args.timeout_s),
        fetch_selected_relation=(not bool(args.skip_fetch)),
        allow_ai_assist=(not bool(args.disable_ai_assist)),
        max_attempts=args.max_attempts,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
