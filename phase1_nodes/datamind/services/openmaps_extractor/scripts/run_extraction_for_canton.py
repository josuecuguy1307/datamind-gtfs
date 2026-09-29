"""
Run Phase 1 extraction for all bboxes belonging to a specific canton/group.

Generates a temporary targets file filtered from extraction_bboxes.json,
then delegates to batch_extract.py.

Usage:
    cd <project_root>
    python -m phase1_nodes.datamind.services.openmaps_extractor.scripts.run_extraction_for_canton \
        --canton cayambe [--dry-run] [--actions stops_broad_bbox,platforms_bbox,terminals_and_stations_bbox]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

from datamind_core.province_config import (
    DEFAULT_PROVINCE,
    load_province_namespaced_catalog,
)

CATALOG_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent.parent / "catalogs" / "extraction_bboxes.json"
)

DEFAULT_ACTIONS = [
    "stops_broad_bbox",
    "platforms_bbox",
    "terminals_and_stations_bbox",
]


def _normalize(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", raw.lower().strip())


def build_targets_for_canton(canton: str, province: str = DEFAULT_PROVINCE) -> list[dict]:
    """Filter extraction_bboxes.json entries that match the canton name.

    The catalog is province-namespaced (v2 format); only entries
    belonging to ``province`` are considered. Unknown provinces yield
    an empty list without crashing.
    """
    if not CATALOG_PATH.exists():
        print(f"Catalog not found: {CATALOG_PATH}")
        return []

    all_bboxes = load_province_namespaced_catalog(CATALOG_PATH, province) or {}

    canton_norm = _normalize(canton)
    targets = []

    for place_name, bbox in all_bboxes.items():
        place_norm = _normalize(place_name)
        # Match if canton appears in the place name or vice versa
        if canton_norm in place_norm or place_norm in canton_norm:
            targets.append({
                "name": place_name,
                "group": canton,
                "priority": "medium",
                "bbox_hint": bbox,
            })

    return targets


def main():
    parser = argparse.ArgumentParser(description="Run Phase 1 extraction for a canton")
    parser.add_argument("--canton", required=True, help="Canton name to filter (e.g. 'cayambe')")
    parser.add_argument("--actions", type=str, default=",".join(DEFAULT_ACTIONS),
                        help="Comma-separated Overpass action_ids")
    parser.add_argument("--dry-run", action="store_true", help="Resolve bboxes but don't extract")
    parser.add_argument("--skip-existing", action="store_true", help="Skip already-extracted places")
    parser.add_argument("--list-only", action="store_true", help="Just list matching places, don't run")
    parser.add_argument(
        "--province",
        type=str,
        default=DEFAULT_PROVINCE,
        help="Province catalog slice to read from (default: sample_region)",
    )
    args = parser.parse_args()

    targets = build_targets_for_canton(args.canton, province=args.province)
    if not targets:
        print(f"No extraction targets found for canton '{args.canton}'")
        print(f"Available places in catalog: {CATALOG_PATH}")
        sys.exit(1)

    print(f"Found {len(targets)} targets for canton '{args.canton}':")
    for t in targets:
        print(f"  - {t['name']}")

    if args.list_only:
        sys.exit(0)

    # Write temporary targets file
    targets_doc = {
        "document_name": f"canton_{args.canton}",
        "target_places": targets,
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix=f"canton_{args.canton}_",
        delete=False
    ) as tmp:
        json.dump(targets_doc, tmp, indent=2)
        tmp_path = tmp.name

    print(f"\nTargets file: {tmp_path}")

    # Import and run batch_extract
    from phase1_nodes.datamind.services.openmaps_extractor.scripts.batch_extract import run_batch

    actions_path = str(Path(__file__).resolve().parents[1] / "actions.json")
    action_ids = [a.strip() for a in args.actions.split(",") if a.strip()]

    results = run_batch(
        targets_path=tmp_path,
        actions_path=actions_path,
        action_ids=action_ids,
        dry_run=args.dry_run,
        skip_existing=args.skip_existing,
    )

    # Summary
    print(f"\n{'=' * 70}")
    print(f"Canton Extraction Complete: {args.canton}")
    print(f"{'=' * 70}")
    if results:
        print(f"Results: {json.dumps(results, indent=2, default=str)}")


if __name__ == "__main__":
    main()
