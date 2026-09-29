#!/usr/bin/env python3
"""
Batch-construct routes for a canton from seed + geography catalogs.

Finds all seed catalogs for the canton that don't yet exist in route_prod,
runs the Constructor V2 pipeline for each, and reports results.

Usage:
    python scripts/construct_canton_routes.py --canton cayambe
    python scripts/construct_canton_routes.py --canton cayambe --dry-run
    python scripts/construct_canton_routes.py --canton cayambe --max 5
    python scripts/construct_canton_routes.py --canton cayambe --catalog-dir /path/to/catalogs
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
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

_ROOT = Path(__file__).resolve().parents[1]


def find_seed_catalogs(catalog_dir: str) -> List[Dict[str, Any]]:
    """Find all seed catalog JSON files in the directory."""
    seed_dir = os.path.join(catalog_dir, "seed")
    if not os.path.isdir(seed_dir):
        print(f"  No seed catalog directory: {seed_dir}")
        return []

    catalogs = []
    for path in sorted(glob.glob(os.path.join(seed_dir, "*.json"))):
        try:
            with open(path) as f:
                data = json.load(f)
            data["_path"] = path
            catalogs.append(data)
        except Exception as exc:
            print(f"  WARNING: Could not read {path}: {exc}")

    return catalogs


def check_route_exists_by_ref(route_code: str, conn: Any) -> bool:
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


def check_route_exists_by_terminus(
    seed: Dict[str, Any], conn: Any, threshold_m: float = 500
) -> bool:
    """Check if a route with matching termini already exists."""
    origin = seed.get("terminus_origin", {})
    dest = seed.get("terminus_destination", {})

    if not origin.get("lat") or not dest.get("lat"):
        return False

    with conn.cursor() as cur:
        cur.execute(
            """
            WITH route_termini AS (
                SELECT r.route_id,
                       n_first.geom AS origin_geom,
                       n_last.geom AS dest_geom
                FROM route_prod.routes r
                JOIN node_prod.nodes n_first ON n_first.node_id = r.stop_node_ids[1]
                JOIN node_prod.nodes n_last ON n_last.node_id = r.stop_node_ids[array_length(r.stop_node_ids, 1)]
                WHERE array_length(r.stop_node_ids, 1) >= 2
            )
            SELECT 1 FROM route_termini
            WHERE ST_DWithin(
                origin_geom::geography,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                %s
            )
            AND ST_DWithin(
                dest_geom::geography,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                %s
            )
            LIMIT 1
            """,
            (
                origin["lon"], origin["lat"], threshold_m,
                dest["lon"], dest["lat"], threshold_m,
            ),
        )
        return cur.fetchone() is not None


def check_dedup(seed: Dict[str, Any], conn: Any) -> bool:
    """Check both ref and terminus match. Returns True if already exists."""
    code = seed.get("route_code", "")
    if code and check_route_exists_by_ref(code, conn):
        return True
    return check_route_exists_by_terminus(seed, conn)


def build_route_from_seed(
    seed_path: str, geo_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Build a single route via the Constructor V2 run_all.py pipeline.
    Returns {success, route_id, error}.
    """
    scripts_dir = _ROOT / "scripts"
    run_all = scripts_dir / "run_all.py"

    if not run_all.exists():
        return {
            "success": False,
            "route_id": None,
            "error": f"run_all.py not found at {run_all}",
        }

    cmd = [sys.executable, str(run_all), "from_seed", "--seed", seed_path]
    if geo_path and os.path.exists(geo_path):
        cmd.extend(["--geography", geo_path])

    env = os.environ.copy()
    env["PYTHONPATH"] = str(_ROOT)

    try:
        result = subprocess.run(
            cmd,
            cwd=str(_ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "route_id": None, "error": "timeout (300s)"}

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "unknown error")[-200:]
        return {"success": False, "route_id": None, "error": error.strip()}

    # Extract route_id from output
    import re

    route_id = None
    m = re.search(r"route_id:\s*([0-9a-fA-F-]{36})", result.stdout or "")
    if m:
        route_id = m.group(1)

    return {"success": True, "route_id": route_id, "error": None}


def construct_canton_routes(
    canton: str,
    catalog_dir: str = "catalogs",
    dry_run: bool = False,
    max_routes: Optional[int] = None,
) -> Dict[str, int]:
    """Main entry: batch-construct all routes for a canton."""
    catalogs = find_seed_catalogs(catalog_dir)
    if not catalogs:
        print(f"  No seed catalogs found in {catalog_dir}/seed/")
        return {"constructed": 0, "existed": 0, "failed": 0}

    print(f"  Found {len(catalogs)} seed catalogs")

    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True

    stats = {"constructed": 0, "existed": 0, "failed": 0}
    processed = 0

    for seed in catalogs:
        if max_routes and processed >= max_routes:
            print(f"  Reached --max {max_routes}, stopping.")
            break

        code = seed.get("route_code", "???")
        seed_path = seed.get("_path", "")

        # Dedup check
        if check_dedup(seed, conn):
            print(f"  SKIP: {code} (already exists)")
            stats["existed"] += 1
            continue

        if dry_run:
            print(f"  DRY: {code} would be constructed")
            stats["constructed"] += 1
            processed += 1
            continue

        # Find matching geography catalog
        geo_path = seed_path.replace("/seed/", "/geography/")
        if not os.path.exists(geo_path):
            geo_path = None

        print(f"  BUILD: {code}...", end=" ", flush=True)
        result = build_route_from_seed(seed_path, geo_path)

        if result["success"]:
            print(f"OK (route_id: {result['route_id']})")
            stats["constructed"] += 1
        else:
            print(f"FAILED: {result['error']}")
            stats["failed"] += 1

        processed += 1

    conn.close()
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Batch-construct routes for a canton from seed catalogs"
    )
    parser.add_argument(
        "--canton", required=True, help="Canton name (e.g. cayambe)"
    )
    parser.add_argument(
        "--catalog-dir",
        default="catalogs",
        help="Directory containing seed/ and geography/ catalogs (default: catalogs)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without building"
    )
    parser.add_argument(
        "--max", type=int, help="Maximum routes to construct (for testing)"
    )
    args = parser.parse_args()

    print(f"Constructing routes for canton: {args.canton}")
    if args.dry_run:
        print("  DRY RUN mode")

    stats = construct_canton_routes(
        args.canton, args.catalog_dir, args.dry_run, args.max
    )

    print(f"\nResults:")
    print(f"  Constructed: {stats['constructed']}")
    print(f"  Already existed: {stats['existed']}")
    print(f"  Failed: {stats['failed']}")


if __name__ == "__main__":
    main()
