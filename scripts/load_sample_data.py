#!/usr/bin/env python3
"""Load a SYNTHETIC sample into the database created with `python scripts/init_db.py`.

100% made-up data: a fictional region ("sample_region") with 3 bus lines, 26 stops and
6 places of interest drawn on a grid. The coordinates only exist so the map renders;
they are NOT real stops.

    python scripts/load_sample_data.py            # load (repeatable: replaces the sample)
    python scripts/load_sample_data.py --remove   # remove ONLY the sample rows
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from datamind_core.dsn import MissingConfigError, need_dsn  # noqa: E402
from init_db import load_dotenv  # noqa: E402

PROVINCE = "sample_region"
NS = uuid.UUID("6f1d3a52-0a55-4c6b-9d3e-5a4d5a4d5a4d")  # fixed namespace of the sample


def sid(*parts: str) -> str:
    """Deterministic UUID: same name -> same id, so the load is repeatable."""
    return str(uuid.uuid5(NS, "/".join(parts)))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


# (code, name, [(lon, lat), ...] vertices interpolated into N stops)
LINES = [
    ("L1", "Line 1 · North Terminal – Downtown", [(-99.140, 19.455), (-99.140, 19.412)], 9),
    ("L2", "Line 2 · West – East", [(-99.175, 19.430), (-99.105, 19.430)], 9),
    ("L3", "Line 3 · Downtown Loop", [(-99.150, 19.440), (-99.125, 19.440), (-99.125, 19.418), (-99.150, 19.418), (-99.150, 19.440)], 8),
]

PLACES = [
    ("North Terminal (sample)", "TERMINAL", -99.140, 19.455),
    ("Central Square (sample)", "POI", -99.135, 19.428),
    ("City Market (sample)", "POI", -99.150, 19.440),
    ("General Hospital (sample)", "POI", -99.125, 19.418),
    ("University (sample)", "POI", -99.165, 19.430),
    ("Linear Park (sample)", "POI", -99.110, 19.430),
]


def polyline_points(vertices, n):
    """n evenly spaced points along the polyline `vertices`."""
    seg = []
    total = 0.0
    for (x0, y0), (x1, y1) in zip(vertices, vertices[1:]):
        d = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        seg.append((total, d, (x0, y0), (x1, y1)))
        total += d
    pts = []
    for i in range(n):
        target = total * i / (n - 1)
        for start, d, (x0, y0), (x1, y1) in seg:
            if target <= start + d + 1e-12:
                t = 0 if d == 0 else (target - start) / d
                pts.append((round(lerp(x0, x1, t), 6), round(lerp(y0, y1, t), 6)))
                break
    return pts


def remove_sample(cur) -> None:
    ids_routes = [sid("route", code) for code, *_ in LINES]
    cur.execute("DELETE FROM route_prod.routes_audit WHERE route_id = ANY(%s::uuid[])", (ids_routes,))
    cur.execute("DELETE FROM route_prod.routes WHERE route_id = ANY(%s::uuid[])", (ids_routes,))
    cur.execute("DELETE FROM route_raw.route_jobs WHERE route_id = ANY(%s::uuid[])", (ids_routes,))
    node_ids = [sid("node", code, str(i)) for code, _, _, n in LINES for i in range(n)]
    node_ids += [sid("poi", name) for name, *_ in PLACES]
    cur.execute("DELETE FROM node_prod.nodes WHERE node_id = ANY(%s::uuid[])", (node_ids,))
    cur.execute("DELETE FROM geo_prod.places WHERE place_id = ANY(%s::uuid[])", ([sid("place", p[0]) for p in PLACES],))


def load_sample(cur) -> dict:
    remove_sample(cur)
    stats = {"paradas": 0, "lugares": 0, "rutas": 0}

    for name, ptype, lon, lat in PLACES:
        cur.execute(
            """INSERT INTO geo_prod.places (place_id, canonical_name, place_type, region, province, geom)
               VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))""",
            (sid("place", name), name, ptype, "Sample City", PROVINCE, lon, lat),
        )
        cur.execute(
            """INSERT INTO node_prod.nodes (node_id, geom, node_type, name, tag_kind, source, province, source_type)
               VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), 'POI', %s, 'poi', 'sample', %s, 'osm')""",
            (sid("poi", name), lon, lat, name, PROVINCE),
        )
        stats["lugares"] += 1

    for code, route_name, vertices, n in LINES:
        pts = polyline_points(vertices, n)
        node_ids = []
        for i, (lon, lat) in enumerate(pts):
            nid = sid("node", code, str(i))
            node_ids.append(nid)
            cur.execute(
                """INSERT INTO node_prod.nodes (node_id, geom, node_type, name, ref, operator, tag_kind, source,
                                                province, source_type, confidence)
                   VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), 'STOP', %s, %s, %s, 'bus_stop', 'sample',
                           %s, 'osm', 0.9)""",
                (nid, lon, lat, f"Stop {code}-{i + 1:02d}", f"{code}-{i + 1:02d}", "Sample Operator", PROVINCE),
            )
            stats["paradas"] += 1

        rid = sid("route", code)
        cur.execute(
            "INSERT INTO route_raw.route_jobs (route_id, created_by, status, area_key, known_ref, direction_id, province, notes)"
            " VALUES (%s, 'sample', 'promoted', 'sample_region', %s, 0, %s, 'Sample (synthetic) route')",
            (rid, code, PROVINCE),
        )
        line_wkt = "LINESTRING(" + ",".join(f"{x} {y}" for x, y in pts) + ")"
        cur.execute(
            """INSERT INTO route_prod.routes
                 (route_id, geom, stop_node_ids, source, route_name, route_aliases, landmark_tags,
                  naming_confidence, human_verified, direction_id, province, deploy_status, version,
                  pipeline_version, valhalla_request, geometry_enforcer_report, stop_coverage_report,
                  quality_gate_passed_at, source_type, canonical_sequence_ready, cleanliness_status)
               VALUES (%s, ST_GeomFromText(%s, 4326), %s::uuid[], 'sample', %s, %s, %s, 0.95, true, 0, %s,
                       'active', 1, 'sample-1.0', %s, %s, %s, now(), 'manual_constructor', true, 'clean')""",
            (rid, line_wkt, node_ids, route_name, [code, route_name.split("·")[0].strip()],
             ["Central Square (sample)"], PROVINCE,
             Json({"sample": True}), Json({"passed": True, "sample": True}), Json({"coverage": 1.0, "sample": True})),
        )
        stats["rutas"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn")
    ap.add_argument("--remove", action="store_true", help="remove only the sample rows")
    args = ap.parse_args()
    load_dotenv()
    try:
        dsn = need_dsn(args.dsn)
    except MissingConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('route_prod.routes')")
            if cur.fetchone()[0] is None:
                print("ERROR: the database is not initialized. Run first: python scripts/init_db.py", file=sys.stderr)
                return 3
            if args.remove:
                remove_sample(cur)
                print("✓ Sample removed.")
            else:
                s = load_sample(cur)
                print(f"✓ Sample loaded: {s['rutas']} routes, {s['paradas']} stops, {s['lugares']} places.")
    except psycopg2.Error as e:
        print(f"DATABASE ERROR: {e.pgerror or e}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
