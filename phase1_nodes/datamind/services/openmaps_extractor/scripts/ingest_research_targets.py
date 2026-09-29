"""
Ingest extraction targets from Deep Research output.
Called by the automation pipeline when processing a new canton.

Usage:
    python -m phase1_nodes.datamind.services.openmaps_extractor.scripts.ingest_research_targets \
        --input /path/to/research_output.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from datamind_core.province_config import DEFAULT_PROVINCE

CATALOG_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent.parent / "catalogs" / "extraction_bboxes.json"
)

# Selected at CLI entry time; defaults to the configured default
# province so the module is fully retro-compatible when imported as a
# library without arguments.
ACTIVE_PROVINCE: str = DEFAULT_PROVINCE


def _load_full_catalog() -> dict:
    if CATALOG_PATH.exists():
        with open(CATALOG_PATH) as f:
            return json.load(f)
    return {"version": "2.0", "provinces": {}}


def _ensure_v2(full: dict) -> dict:
    """Upgrade a legacy flat catalog to v2 shape in-memory."""
    if isinstance(full, dict) and "provinces" in full and isinstance(full["provinces"], dict):
        return full
    # Legacy flat catalog → assume everything belongs to the default province.
    return {"version": "2.0", "provinces": {DEFAULT_PROVINCE: full or {}}}


def load_existing(province: str | None = None) -> dict:
    """Return the flat bbox dict for a given province (empty if unknown)."""
    full = _ensure_v2(_load_full_catalog())
    key = (province or ACTIVE_PROVINCE or DEFAULT_PROVINCE).strip().lower()
    return dict(full["provinces"].get(key) or {})


def _save_province_slice(province: str, slice_dict: dict) -> None:
    full = _ensure_v2(_load_full_catalog())
    full["provinces"][province] = slice_dict
    with open(CATALOG_PATH, "w") as f:
        json.dump(full, f, indent=2, ensure_ascii=False)


def validate_bbox(bbox: dict) -> bool:
    """Validate bbox is within Ecuador mainland operational range."""
    required = {"south", "west", "north", "east"}
    if not isinstance(bbox, dict) or not required.issubset(bbox.keys()):
        return False
    s, w, n, e = bbox["south"], bbox["west"], bbox["north"], bbox["east"]
    # Longitude range for Ecuador mainland
    if not (-81.5 <= w <= -75.0 and -81.5 <= e <= -75.0):
        return False
    # Latitude range for Ecuador mainland (covers Sample Region B -2.x through Carchi +1.x)
    if not (-5.5 <= s <= 2.0 and -5.5 <= n <= 2.0):
        return False
    # Sanity: south < north, west < east
    if s >= n or w >= e:
        return False
    return True


def ingest(input_path: str) -> int:
    with open(input_path) as f:
        research = json.load(f)

    # Support two formats:
    # 1. {"extraction_targets": [{"place": ..., "bbox": {...}, "group": ...}, ...]}
    # 2. {"place_name": {"south": ..., "west": ..., "north": ..., "east": ...}, ...}
    targets = research.get("extraction_targets")
    if targets and isinstance(targets, list):
        return _ingest_list_format(targets)
    elif isinstance(research, dict) and not targets:
        return _ingest_dict_format(research)
    else:
        print("Unrecognized format. Expected 'extraction_targets' list or place->bbox dict.")
        return 0


def _ingest_dict_format(research: dict) -> int:
    """Ingest dict format: {"place_name": {"south":..., "west":..., "north":..., "east":...}}"""
    existing = load_existing(ACTIVE_PROVINCE)
    added = 0
    for place, bbox in research.items():
        if place.startswith("_"):
            continue
        if place in existing:
            print(f"  SKIP (duplicate): {place}")
            continue
        if not validate_bbox(bbox):
            print(f"  REJECT (invalid bbox): {place} -> {bbox}")
            continue
        existing[place] = bbox
        added += 1
        print(f"  ADDED: {place}")

    _save_province_slice(ACTIVE_PROVINCE, existing)

    print(f"\nTotal: {added} new targets added ({len(existing)} total in province '{ACTIVE_PROVINCE}')")
    return added


def _ingest_list_format(targets: list) -> int:
    """Ingest list format: [{"place": ..., "bbox": {...}, "group": ...}, ...]"""
    existing = load_existing(ACTIVE_PROVINCE)
    added = 0
    for target in targets:
        place = target.get("place", "")
        bbox = target.get("bbox", {})
        if not place:
            continue
        if place in existing:
            print(f"  SKIP (duplicate): {place}")
            continue
        if not validate_bbox(bbox):
            print(f"  REJECT (invalid bbox): {place} -> {bbox}")
            continue
        existing[place] = bbox
        added += 1
        print(f"  ADDED: {place} (group: {target.get('group', 'unassigned')})")

    _save_province_slice(ACTIVE_PROVINCE, existing)

    print(f"\nTotal: {added} new targets added ({len(existing)} total in province '{ACTIVE_PROVINCE}')")
    return added


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest Deep Research targets into extraction catalog")
    parser.add_argument("--input", required=True, help="Path to Deep Research JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing")
    parser.add_argument(
        "--province",
        type=str,
        default=DEFAULT_PROVINCE,
        help="Province slice to ingest into (default: sample_region)",
    )
    args = parser.parse_args()

    ACTIVE_PROVINCE = (args.province or DEFAULT_PROVINCE).strip().lower() or DEFAULT_PROVINCE

    if not Path(args.input).exists():
        print(f"File not found: {args.input}")
        sys.exit(1)

    count = ingest(args.input)
    if count == 0:
        print("No new targets ingested.")
    sys.exit(0)
