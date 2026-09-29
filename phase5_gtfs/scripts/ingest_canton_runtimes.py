#!/usr/bin/env python3
"""
Ingest runtime data from a Deep Research file for Phase 5.
Adds route priors and scales existing estimates.

Usage:
    python -m phase5_gtfs.scripts.ingest_canton_runtimes --input ~/research/cayambe.json
    python -m phase5_gtfs.scripts.ingest_canton_runtimes --input ~/research/cayambe.json --dry-run

Expected JSON format:
{
    "runtime_priors": [
        {
            "route_id": "uuid-or-partial",
            "route_name": "Sangolquí - Quito (Marín)",
            "runtime_ida_min": 75,
            "runtime_vuelta_min": 70,
            "time_period": "peak_am",
            "source": "AMT permit 2024-0847",
            "confidence": "high",
            "headway_min": 8,
            "n_vehicles": 19,
            "notes": "..."
        }
    ]
}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DB_DSN", "postgresql://localhost:5432/datamind_ml")

from phase5_gtfs.runtime.ingest_route_priors import ingest_deep_research_runtimes


def load_research_file(path: str) -> List[Dict[str, Any]]:
    """Load and normalize a Deep Research JSON file into ingestion format."""
    with open(path) as f:
        data = json.load(f)

    entries: List[Dict[str, Any]] = []

    # Support runtime_priors[] array
    for item in data.get("runtime_priors", []):
        entries.append(item)

    # Also support per-route runtime sections
    for route in data.get("routes", []):
        rt = route.get("runtime", {})
        if not rt:
            continue
        entry: Dict[str, Any] = {
            "route_id": route.get("route_id", ""),
            "route_name": route.get("route_name", ""),
            "source": rt.get("source", data.get("source", "deep_research")),
            "confidence": rt.get("confidence", "medium"),
        }
        if rt.get("ida_min"):
            entry["runtime_ida_min"] = rt["ida_min"]
        if rt.get("vuelta_min"):
            entry["runtime_vuelta_min"] = rt["vuelta_min"]
        if rt.get("time_period"):
            entry["time_period"] = rt["time_period"]
        if rt.get("headway_min"):
            entry["headway_min"] = rt["headway_min"]
        if rt.get("n_vehicles"):
            entry["n_vehicles"] = rt["n_vehicles"]
        entries.append(entry)

    return entries


def expand_directions(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Expand entries with runtime_vuelta_min into two direction entries.
    direction_id=0 uses runtime_ida_min, direction_id=1 uses runtime_vuelta_min.
    """
    expanded: List[Dict[str, Any]] = []
    for e in entries:
        # Direction 0 (ida)
        d0 = dict(e)
        d0["direction_id"] = 0
        if "runtime_vuelta_min" in d0:
            del d0["runtime_vuelta_min"]
        expanded.append(d0)

        # Direction 1 (vuelta) if runtime provided
        vuelta = e.get("runtime_vuelta_min")
        if vuelta:
            d1 = dict(e)
            d1["direction_id"] = 1
            d1["runtime_ida_min"] = vuelta  # ingest function expects runtime_ida_min
            if "runtime_vuelta_min" in d1:
                del d1["runtime_vuelta_min"]
            expanded.append(d1)

    return expanded


def main():
    parser = argparse.ArgumentParser(description="Ingest Deep Research runtimes")
    parser.add_argument("--input", required=True, help="Path to Deep Research JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Only show what would be ingested")
    args = parser.parse_args()

    input_path = os.path.expanduser(args.input)
    if not os.path.exists(input_path):
        print(f"ERROR: File not found: {input_path}")
        sys.exit(1)

    print(f"=== Ingest Deep Research Runtimes ===")
    print(f"Input: {input_path}\n")

    entries = load_research_file(input_path)
    print(f"Loaded {len(entries)} route entries from research file")

    entries = expand_directions(entries)
    print(f"Expanded to {len(entries)} direction entries (d0 + d1)\n")

    if args.dry_run:
        for e in entries[:20]:
            name = e.get("route_name", e.get("route_id", "?"))[:40]
            rt = e.get("runtime_ida_min", "?")
            d = e.get("direction_id", "?")
            conf = e.get("confidence", "?")
            print(f"  d{d} {name:40s} {rt:>5} min  conf={conf}")
        if len(entries) > 20:
            print(f"  ... +{len(entries) - 20} more")
        print(f"\nDry run — {len(entries)} entries would be ingested.")
        return

    results = ingest_deep_research_runtimes(entries)

    # Summarize
    actions = {}
    for r in results:
        action = r.get("action") or r.get("status", "UNKNOWN")
        actions[action] = actions.get(action, 0) + 1

    print(f"\nResults:")
    for action, count in sorted(actions.items()):
        print(f"  {action}: {count}")

    # Show details for first few
    for r in results[:10]:
        action = r.get("action") or r.get("status")
        rid = r.get("route_id", "")[:8]
        reason = r.get("reason", "")
        print(f"  [{action}] {rid} {reason}")

    print(f"\nDone: {len(results)} entries processed.")


if __name__ == "__main__":
    main()
