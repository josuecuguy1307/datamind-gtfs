#!/usr/bin/env python3
"""
Ingest Deep Research 06a route discovery JSON into seed + geography catalog files
that Constructor V2 can consume.

Usage:
    python scripts/ingest_research_routes.py --input /path/to/06a_routes.json
    python scripts/ingest_research_routes.py --input file.json --dry-run
    python scripts/ingest_research_routes.py --input file.json --output-dir catalogs

Expected input format (06a):
{
  "routes": [
    {
      "route_code": "CAY-01",
      "display_name": "Cayambe - Tabacundo",
      "cooperative": "Flor del Valle",
      "seed_catalog": {
        "terminus_origin": {"name": "Cayambe", "lat": 0.04, "lon": -78.14},
        "terminus_destination": {"name": "Tabacundo", "lat": 0.07, "lon": -78.23},
        "intermediate_stops": [{"name": "Ayora", "lat": 0.05, "lon": -78.18}],
        "primary_corridor": "E35",
        "corridor_roads": ["E35", "Av. Natalia Jarrín"]
      },
      "geography_catalog": {
        "zone_classification": "periurban",
        "envelope_bbox": [-78.25, 0.03, -78.12, 0.08],
        "required_corridors": ["E35"]
      }
    }
  ]
}

Output: JSON files in catalogs/seed/ and catalogs/geography/ per route.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DB_DSN = os.environ.get(
    "DB_DSN",
    "postgresql://localhost:5432/datamind_ml",
)

# Ecuador coordinate bounds for validation
ECUADOR_LON_RANGE = (-81.5, -75.0)
ECUADOR_LAT_RANGE = (-5.0, 2.0)


def validate_coord(lat: float, lon: float) -> bool:
    """Check coordinates are within Ecuador."""
    return (
        ECUADOR_LAT_RANGE[0] <= lat <= ECUADOR_LAT_RANGE[1]
        and ECUADOR_LON_RANGE[0] <= lon <= ECUADOR_LON_RANGE[1]
    )


def check_route_exists(route_code: str, conn: Any) -> bool:
    """Check if a route with this ref already exists in route_prod."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM route_prod.route_semantics
            WHERE route_ref = %s
            LIMIT 1
            """,
            (route_code,),
        )
        return cur.fetchone() is not None


def validate_route(route: Dict[str, Any]) -> Optional[str]:
    """Validate a single route entry. Returns error string or None."""
    code = route.get("route_code")
    if not code:
        return "missing route_code"

    seed = route.get("seed_catalog", {})
    origin = seed.get("terminus_origin", {})
    dest = seed.get("terminus_destination", {})

    if not origin.get("lat") or not origin.get("lon"):
        return f"{code}: missing origin coordinates"
    if not dest.get("lat") or not dest.get("lon"):
        return f"{code}: missing destination coordinates"

    if not validate_coord(origin["lat"], origin["lon"]):
        return f"{code}: origin coords outside Ecuador ({origin['lat']}, {origin['lon']})"
    if not validate_coord(dest["lat"], dest["lon"]):
        return f"{code}: dest coords outside Ecuador ({dest['lat']}, {dest['lon']})"

    for i, stop in enumerate(seed.get("intermediate_stops", [])):
        if stop.get("lat") and stop.get("lon"):
            if not validate_coord(stop["lat"], stop["lon"]):
                return f"{code}: intermediate stop {i} outside Ecuador"

    return None


def write_seed_catalog(
    route: Dict[str, Any], output_dir: str, dry_run: bool
) -> str:
    """Write seed catalog JSON for one route. Returns output path."""
    code = route["route_code"]
    seed = route.get("seed_catalog", {})

    catalog = {
        "route_code": code,
        "display_name": route.get("display_name", code),
        "cooperative": route.get("cooperative", ""),
        "terminus_origin": seed.get("terminus_origin", {}),
        "terminus_destination": seed.get("terminus_destination", {}),
        "intermediate_stops": seed.get("intermediate_stops", []),
        "primary_corridor": seed.get("primary_corridor", ""),
        "corridor_roads": seed.get("corridor_roads", []),
    }

    path = os.path.join(output_dir, "seed", f"{code}.json")
    if not dry_run:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(catalog, f, indent=2, ensure_ascii=False)
    return path


def write_geography_catalog(
    route: Dict[str, Any], output_dir: str, dry_run: bool
) -> str:
    """Write geography catalog JSON for one route. Returns output path."""
    code = route["route_code"]
    geo = route.get("geography_catalog", {})

    catalog = {
        "route_code": code,
        "zone_classification": geo.get("zone_classification", "periurban"),
        "envelope_bbox": geo.get("envelope_bbox", []),
        "required_corridors": geo.get("required_corridors", []),
        "must_pass_through": geo.get("must_pass_through", []),
        "must_not_enter": geo.get("must_not_enter", []),
    }

    path = os.path.join(output_dir, "geography", f"{code}.json")
    if not dry_run:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(catalog, f, indent=2, ensure_ascii=False)
    return path


def ingest_research_routes(
    input_file: str,
    output_dir: str = "catalogs",
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    Read unified 06a JSON, write seed + geography catalogs.

    Handles the ``in_osm`` flag from the unified 06a format:
    - ``in_osm: true`` routes are skipped (already have geometry)
    - ``in_osm: false`` routes with ``seed_catalog`` get catalogs written
    - ``in_osm: false`` routes WITHOUT ``seed_catalog`` are flagged
    """
    with open(input_file) as f:
        data = json.load(f)

    all_routes = data.get("routes", [])
    if not all_routes:
        print("  No routes found in input JSON.")
        return {
            "ingested": 0, "skipped_osm": 0, "skipped_exists": 0,
            "skipped_no_seed": 0, "invalid": 0,
        }

    # Split by type
    osm_routes = [r for r in all_routes if r.get("in_osm", False)]
    construction_routes = [
        r for r in all_routes
        if not r.get("in_osm", False) and r.get("seed_catalog")
    ]
    no_seed_routes = [
        r for r in all_routes
        if not r.get("in_osm", False) and not r.get("seed_catalog")
    ]

    print(f"  Total routes: {len(all_routes)}")
    print(f"    OSM (metadata only, skip catalogs): {len(osm_routes)}")
    print(f"    Need construction (have seed): {len(construction_routes)}")
    print(f"    Non-OSM but no seed data: {len(no_seed_routes)}")

    if no_seed_routes:
        print("  WARNING: these routes are not in OSM but have no seed_catalog:")
        for r in no_seed_routes:
            print(f"    - {r.get('route_code', '?')}: {r.get('display_name', '?')}")

    # Connect to check for existing routes
    conn = None
    try:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = True
    except Exception as exc:
        print(f"  WARNING: DB connection failed ({exc}), skipping dedup check")

    stats = {
        "ingested": 0,
        "skipped_osm": len(osm_routes),
        "skipped_exists": 0,
        "skipped_no_seed": len(no_seed_routes),
        "invalid": 0,
    }

    # Only process construction routes (non-OSM with seed_catalog)
    for route in construction_routes:
        code = route.get("route_code", "???")

        # Validate
        err = validate_route(route)
        if err:
            print(f"  INVALID: {err}")
            stats["invalid"] += 1
            continue

        # Check if already exists
        if conn:
            try:
                if check_route_exists(code, conn):
                    print(f"  SKIP: {code} (already in route_prod)")
                    stats["skipped_exists"] += 1
                    continue
            except Exception:
                pass

        # Write catalogs
        write_seed_catalog(route, output_dir, dry_run)
        write_geography_catalog(route, output_dir, dry_run)

        action = "DRY" if dry_run else "OK"
        print(f"  {action}: {code} -> seed + geography")
        stats["ingested"] += 1

    if conn:
        conn.close()

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Ingest Deep Research 06a routes into seed + geography catalogs"
    )
    parser.add_argument(
        "--input", required=True, help="Path to 06a research JSON"
    )
    parser.add_argument(
        "--output-dir",
        default="catalogs",
        help="Output directory for catalog files (default: catalogs)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without writing files"
    )
    args = parser.parse_args()

    print(f"Ingesting research routes from {args.input}")
    if args.dry_run:
        print("  DRY RUN mode")

    stats = ingest_research_routes(
        args.input, args.output_dir, args.dry_run
    )

    print(f"\nResults:")
    print(f"  Catalogs created: {stats['ingested']}")
    print(f"  Skipped (in OSM, metadata only): {stats.get('skipped_osm', 0)}")
    print(f"  Skipped (already in route_prod): {stats.get('skipped_exists', 0)}")
    print(f"  Skipped (no seed_catalog): {stats.get('skipped_no_seed', 0)}")
    print(f"  Invalid: {stats['invalid']}")


if __name__ == "__main__":
    main()
