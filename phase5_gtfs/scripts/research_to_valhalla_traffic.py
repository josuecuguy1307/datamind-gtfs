#!/usr/bin/env python3
"""
Deep Research 06b → Valhalla predicted traffic tiles.

Takes a 06b research JSON file, decomposes route-level runtimes into
per-edge speeds, and generates Valhalla predicted-traffic CSVs that
can be loaded into the runtime Valhalla instance (port 8004).

Usage::

    python -m phase5_gtfs.scripts.research_to_valhalla_traffic \\
        --research /path/to/06b_research.json \\
        --output /tmp/valhalla_traffic \\
        [--load]   # also load into Valhalla and restart
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
os.environ.setdefault(
    "DB_DSN", "postgresql://localhost:5432/datamind_ml"
)

from phase5_gtfs.common.config import db_conn
from phase5_gtfs.runtime_lab.valhalla_integration import (
    map_route_to_edges,
    decompose_runtime_to_edge_speeds,
    generate_valhalla_traffic_csv,
    load_traffic_into_valhalla,
)


def _get_route_geometry(route_code: str, conn: Any) -> List[List[float]]:
    """Fetch route geometry as [[lon, lat], ...] from route_prod."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ST_AsGeoJSON(r.geom)::json->'coordinates' AS coords
            FROM route_prod.routes r
            LEFT JOIN route_prod.route_semantics rs ON rs.route_id = r.route_id
            WHERE COALESCE(rs.route_ref, '') = %s
              AND r.geom IS NOT NULL
            LIMIT 1
            """,
            (route_code,),
        )
        row = cur.fetchone()
        if row and row.get("coords"):
            return row["coords"]
    return []


def process_research_file(
    research_path: str,
    output_dir: str,
    do_load: bool = False,
) -> Dict[str, Any]:
    """
    Main pipeline: research JSON → per-edge speeds → Valhalla traffic CSV.
    """
    with open(research_path) as f:
        research = json.load(f)

    routes = research.get("routes", [])
    print(f"Loaded {len(routes)} routes from {research_path}")

    all_edge_speeds: Dict[int, Dict[str, Any]] = {}
    processed = 0
    skipped = 0

    with db_conn() as conn:
        for route in routes:
            code = route.get("route_code") or route.get("route_ref", "")
            rt = route.get("runtime") or {}
            runtime_data = rt.get("runtime_ida") or {}

            if not code or not runtime_data:
                skipped += 1
                continue

            # Get route geometry
            coords = _get_route_geometry(code, conn)
            if not coords or len(coords) < 2:
                print(f"  SKIP {code}: no geometry")
                skipped += 1
                continue

            # Map to Valhalla edges
            try:
                edges = map_route_to_edges(coords)
            except Exception as e:
                print(f"  SKIP {code}: trace_attributes failed: {e}")
                skipped += 1
                continue

            if not edges:
                print(f"  SKIP {code}: no edges from trace_attributes")
                skipped += 1
                continue

            # Decompose runtime into per-edge speeds
            hotspots = rt.get("congestion_hotspots", [])
            edge_speeds = decompose_runtime_to_edge_speeds(
                edges, runtime_data, hotspots
            )

            # Merge into global edge speed map
            for eid, data in edge_speeds.items():
                if eid in all_edge_speeds:
                    # Average speeds across routes sharing the same edge
                    existing = all_edge_speeds[eid]["speeds"]
                    for period, speed in data["speeds"].items():
                        if period in existing:
                            existing[period] = round(
                                (existing[period] + speed) / 2, 1
                            )
                        else:
                            existing[period] = speed
                else:
                    all_edge_speeds[eid] = data

            processed += 1
            print(f"  OK {code}: {len(edges)} edges, {len(edge_speeds)} with speeds")

    print(f"\nProcessed: {processed}, Skipped: {skipped}")
    print(f"Total unique edges with traffic data: {len(all_edge_speeds)}")

    if not all_edge_speeds:
        print("No edge speeds generated — nothing to write.")
        return {"processed": processed, "skipped": skipped, "edges": 0}

    # Generate CSV
    csv_path = generate_valhalla_traffic_csv(all_edge_speeds, output_dir)

    # Optionally load into Valhalla
    if do_load:
        print("\nLoading traffic into Valhalla runtime...")
        load_traffic_into_valhalla(output_dir)

    return {
        "processed": processed,
        "skipped": skipped,
        "edges": len(all_edge_speeds),
        "csv_path": csv_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Deep Research 06b runtimes to Valhalla predicted traffic"
    )
    parser.add_argument(
        "--research", required=True, help="Path to 06b research JSON file"
    )
    parser.add_argument(
        "--output",
        default="/tmp/valhalla_traffic",
        help="Output directory for traffic CSV",
    )
    parser.add_argument(
        "--load",
        action="store_true",
        help="Also load traffic into Valhalla runtime and restart",
    )
    args = parser.parse_args()

    result = process_research_file(args.research, args.output, args.load)
    print(f"\n=== Result: {json.dumps(result, indent=2)} ===")


if __name__ == "__main__":
    main()
