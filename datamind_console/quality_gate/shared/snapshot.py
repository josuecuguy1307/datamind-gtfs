"""Snapshot and restore canton state for reproducible benchmarks.

In production mode, uses pg_dump/pg_restore scoped to canton bbox.
In offline/benchmark mode, works with in-memory QualityGateInput objects.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import (
    Fare,
    QualityGateInput,
    Route,
    RouteSemantics,
    ScheduleProfile,
    Shape,
    ShapePoint,
    Stop,
)


@dataclass
class CantonSnapshot:
    """A serializable snapshot of a canton's quality-relevant data."""
    canton: str
    province: str
    input: QualityGateInput
    dump_path: Optional[str] = None  # path to pg_dump file if DB-backed


def snapshot(province: str, canton: str, db_dsn: Optional[str] = None) -> QualityGateInput:
    """Create a snapshot of canton data.

    If db_dsn is provided, reads from PostgreSQL. Otherwise returns an empty
    QualityGateInput suitable for synthetic benchmark use.
    """
    if db_dsn:
        return _snapshot_from_db(province, canton, db_dsn)
    return _empty_snapshot(province, canton)


def snapshot_to_db(canton_snapshot: CantonSnapshot, db_dsn: str) -> str:
    """Dump canton-scoped tables to a temp file via pg_dump.

    Returns path to the dump file for later restore.
    """
    schemas = "route_prod,node_prod,geo_prod,catalog"
    dump_file = tempfile.mktemp(suffix=f"_{canton_snapshot.canton}_snapshot.dump")

    cmd = [
        "pg_dump", db_dsn,
        "--format=custom",
        "--no-owner", "--no-acl",
    ]
    for schema in schemas.split(","):
        cmd.extend(["--schema", schema.strip()])

    with open(dump_file, "wb") as f:
        subprocess.run(cmd, stdout=f, check=True, timeout=120)

    canton_snapshot.dump_path = dump_file
    return dump_file


def restore_from_db(dump_path: str, db_dsn: str) -> None:
    """Restore a canton snapshot from a pg_dump file."""
    cmd = [
        "pg_restore", "--dbname", db_dsn,
        "--clean", "--if-exists",
        "--no-owner", "--no-acl",
        dump_path,
    ]
    subprocess.run(cmd, check=True, timeout=120)


def restore_in_memory(original: QualityGateInput) -> QualityGateInput:
    """Restore an in-memory snapshot by deep-copying the original."""
    return copy.deepcopy(original)


def _empty_snapshot(province: str, canton: str) -> QualityGateInput:
    """Return an empty QualityGateInput for synthetic benchmarks."""
    return QualityGateInput(
        canton=canton,
        province=province,
    )


def _snapshot_from_db(province: str, canton: str, db_dsn: str) -> QualityGateInput:
    """Read canton-scoped data from PostgreSQL into a QualityGateInput.

    All queries are READ-ONLY and filter by canton bbox using ST_Intersects.
    The canton bbox is resolved via Nominatim lookup cached in geo_admin.canton_bbox,
    or falls back to a generous Sample Region-wide envelope if no row exists.
    """
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(db_dsn)
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET TRANSACTION READ ONLY;")

            bbox_wkt = _resolve_canton_bbox(cur, canton, province)

            stops = _load_stops(cur, bbox_wkt)
            routes = _load_routes(cur, bbox_wkt)
            route_ids = [r.route_id for r in routes]
            shapes = _load_shapes(cur, route_ids)
            semantics = _load_semantics(cur, route_ids)
            schedules = _load_schedules(cur, route_ids)
            fares = _load_fares(cur, route_ids)

        return QualityGateInput(
            canton=canton,
            province=province,
            db_dsn=db_dsn,
            stops=stops,
            routes=routes,
            shapes=shapes,
            semantics=semantics,
            schedules=schedules,
            fares=fares,
        )
    finally:
        conn.close()


def _resolve_canton_bbox(cur, canton: str, province: str) -> str:
    """Resolve canton bbox as WKT POLYGON for ST_Intersects filtering.

    Tries geo_admin.canton_bbox first, then falls back to a generous
    Sample Region-wide envelope so the benchmark can still run without the
    admin table.
    """
    try:
        cur.execute("SAVEPOINT bbox_lookup;")
        cur.execute("""
            SELECT ST_AsText(ST_Envelope(geom)) AS bbox_wkt
            FROM geo_admin.canton_bbox
            WHERE lower(canton_name) = lower(%s)
              AND lower(province_name) = lower(%s)
            LIMIT 1
        """, (canton, province))
        row = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT bbox_lookup;")
        if row and row["bbox_wkt"]:
            return row["bbox_wkt"]
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT bbox_lookup;")

    # Fallback: generous Sample Region envelope
    return "POLYGON((-78.9 -0.6, -78.9 0.2, -78.0 0.2, -78.0 -0.6, -78.9 -0.6))"


def _load_stops(cur, bbox_wkt: str) -> List[Stop]:
    cur.execute("""
        SELECT node_id::text, name, ref, ST_Y(geom) as lat, ST_X(geom) as lon,
               operator, confidence, node_type, source
        FROM node_prod.nodes
        WHERE confidence > 0
          AND ST_Intersects(geom, ST_GeomFromText(%s, 4326))
    """, (bbox_wkt,))
    return [
        Stop(
            stop_id=r["node_id"], name=r["name"], ref=r["ref"],
            lat=r["lat"], lon=r["lon"],
            operator=r.get("operator"), confidence=r.get("confidence", 0),
            node_type=r.get("node_type", "STOP"), source=r.get("source"),
        )
        for r in cur.fetchall()
    ]


def _load_routes(cur, bbox_wkt: str) -> List[Route]:
    cur.execute("""
        SELECT route_id::text, route_name, service_route_id::text,
               direction_id, stop_node_ids, operator_name,
               ST_AsText(geom) as geom_wkt
        FROM route_prod.routes
        WHERE ST_Intersects(geom, ST_GeomFromText(%s, 4326))
    """, (bbox_wkt,))
    rows = cur.fetchall()
    return [
        Route(
            route_id=r["route_id"], route_name=r["route_name"],
            service_route_id=r.get("service_route_id"),
            direction_id=r.get("direction_id", 0),
            stop_node_ids=[str(s) for s in (r.get("stop_node_ids") or [])],
            operator_name=r.get("operator_name"),
            geom_wkt=r.get("geom_wkt"),
        )
        for r in rows
    ]


def _load_shapes(cur, route_ids: List[str]) -> List[Shape]:
    if not route_ids:
        return []
    cur.execute("""
        SELECT s.shape_id, s.shape_pt_lat, s.shape_pt_lon,
               s.shape_pt_sequence, COALESCE(s.shape_dist_traveled, 0) as dist
        FROM gtfs_work.gtfs_shapes s
        WHERE s.shape_id = ANY(%s)
    """, ([f"shape_{rid[:8]}_d0" for rid in route_ids],))
    rows = cur.fetchall()
    shapes_map: Dict[str, Shape] = {}
    for r in rows:
        sid = r["shape_id"]
        if sid not in shapes_map:
            shapes_map[sid] = Shape(shape_id=sid)
        shapes_map[sid].points.append(ShapePoint(
            lat=r["shape_pt_lat"], lon=r["shape_pt_lon"],
            sequence=r["shape_pt_sequence"], dist_traveled=r["dist"],
        ))
    return list(shapes_map.values())


def _load_semantics(cur, route_ids: List[str]) -> List[RouteSemantics]:
    if not route_ids:
        return []
    cur.execute("""
        SELECT route_id::text, operator, route_short_name, route_long_name,
               route_type, public_origin, public_destination,
               jurisdiction, confidence, approved
        FROM catalog.route_semantics
        WHERE route_id::text = ANY(%s)
    """, (route_ids,))
    return [
        RouteSemantics(
            route_id=r["route_id"], operator=r.get("operator"),
            route_short_name=r.get("route_short_name"),
            route_long_name=r.get("route_long_name"),
            route_type=r.get("route_type", 3),
            public_origin=r.get("public_origin"),
            public_destination=r.get("public_destination"),
            jurisdiction=r.get("jurisdiction"),
            confidence=r.get("confidence", 0),
            approved=r.get("approved", False),
        )
        for r in cur.fetchall()
    ]


def _load_schedules(cur, route_ids: List[str]) -> List[ScheduleProfile]:
    if not route_ids:
        return []
    cur.execute("""
        SELECT route_id::text, peak_runtime_min, offpeak_runtime_min
        FROM catalog.route_schedule_profile
        WHERE route_id::text = ANY(%s)
    """, (route_ids,))
    return [
        ScheduleProfile(
            route_id=r["route_id"],
            peak_runtime_min=r.get("peak_runtime_min"),
            offpeak_runtime_min=r.get("offpeak_runtime_min"),
        )
        for r in cur.fetchall()
    ]


def _load_fares(cur, route_ids: List[str]) -> List[Fare]:
    if not route_ids:
        return []
    cur.execute("""
        SELECT fare_id, route_id::text, agency_id, price, currency_type
        FROM catalog.route_fare
        WHERE route_id::text = ANY(%s)
    """, (route_ids,))
    return [
        Fare(
            fare_id=r["fare_id"], route_id=r.get("route_id"),
            agency_id=r.get("agency_id"),
            price=r.get("price", 0), currency_type=r.get("currency_type", "USD"),
        )
        for r in cur.fetchall()
    ]


def generate_synthetic_canton(
    canton: str = "test_canton",
    province: str = "test_province",
    n_stops: int = 200,
    n_routes: int = 20,
    n_shapes: int = 20,
    seed: int = 42,
) -> QualityGateInput:
    """Generate a synthetic canton with realistic data for benchmarking.

    All coordinates are within the Quito metropolitan area.
    """
    import math
    import random as _random
    import uuid

    # Incorporate canton+province into seed so each canton gets unique data
    canton_hash = hash((seed, canton.lower(), province.lower()))
    rng = _random.Random(canton_hash)

    # Vary entity counts per canton (±20% from base)
    count_jitter = 1.0 + rng.uniform(-0.20, 0.20)
    n_stops = max(10, int(n_stops * count_jitter))
    n_routes = max(5, int(n_routes * count_jitter))
    n_shapes = max(5, int(n_shapes * count_jitter))

    # Quito area center — shift slightly per canton
    base_lat = -0.22 + rng.uniform(-0.05, 0.05)
    base_lon = -78.51 + rng.uniform(-0.05, 0.05)

    stops = []
    for i in range(n_stops):
        lat = base_lat + rng.uniform(-0.15, 0.15)
        lon = base_lon + rng.uniform(-0.15, 0.15)
        stops.append(Stop(
            stop_id=uuid.UUID(int=rng.getrandbits(128)).hex,
            name=f"Parada {rng.choice(['Norte', 'Sur', 'Este', 'Oeste', 'Central'])} {i+1}",
            ref=str(rng.randint(1000, 9999)),
            lat=lat, lon=lon,
            operator=rng.choice(["Coop TransValle", "MetroQ", "Coop Libertad", "TroleBus"]),
            confidence=rng.uniform(0.3, 1.0),
        ))

    routes = []
    for i in range(n_routes):
        n_stops_route = rng.randint(5, 15)
        route_stops = rng.sample([s.stop_id for s in stops], min(n_stops_route, len(stops)))
        rid = uuid.UUID(int=rng.getrandbits(128)).hex
        routes.append(Route(
            route_id=rid,
            route_name=f"Ruta {rng.choice(['Ecovia', 'Trole', 'Metrobus', 'Alimentador'])} {i+1}",
            service_route_id=uuid.UUID(int=rng.getrandbits(128)).hex,
            direction_id=rng.choice([0, 1]),
            stop_node_ids=route_stops,
            operator_name=rng.choice(["Coop TransValle", "MetroQ", "Coop Libertad", "TroleBus"]),
        ))

    shapes = []
    for i, route in enumerate(routes[:n_shapes]):
        pts = []
        n_pts = rng.randint(20, 80)
        # Generate a realistic path: random walk along a direction with small steps
        # Each step ~200-800m (0.002-0.007° at equator), ensuring <5km between points
        start_lat = base_lat + rng.uniform(-0.10, 0.10)
        start_lon = base_lon + rng.uniform(-0.10, 0.10)
        heading = rng.uniform(0, 2 * math.pi)  # overall direction
        cur_lat, cur_lon = start_lat, start_lon
        cumulative_dist = 0.0
        for j in range(n_pts):
            pts.append(ShapePoint(
                lat=cur_lat, lon=cur_lon,
                sequence=j,
                dist_traveled=cumulative_dist,
            ))
            # Step: 200-800m in roughly consistent direction with some wobble
            step_deg = rng.uniform(0.002, 0.007)  # ~220-780m
            wobble = rng.gauss(0, 0.3)  # slight direction change
            heading += wobble
            cur_lat += step_deg * math.sin(heading)
            cur_lon += step_deg * math.cos(heading)
            cumulative_dist += step_deg * 111_320  # approx meters
        shapes.append(Shape(shape_id=f"shape_{route.route_id[:8]}_d0", points=pts))

    semantics = []
    for route in routes:
        semantics.append(RouteSemantics(
            route_id=route.route_id,
            operator=route.operator_name,
            route_short_name=f"R{rng.randint(1, 99)}",
            route_long_name=route.route_name,
            public_origin=rng.choice(["Terminal Norte", "La Marin", "Quitumbe", "Carcelen"]),
            public_destination=rng.choice(["El Recreo", "La Y", "Solanda", "Carapungo"]),
        ))

    schedules = []
    for route in routes:
        schedules.append(ScheduleProfile(
            route_id=route.route_id,
            peak_runtime_min=rng.uniform(30, 120),
            offpeak_runtime_min=rng.uniform(35, 140),
            service_days={
                "monday": True, "tuesday": True, "wednesday": True,
                "thursday": True, "friday": True,
                "saturday": rng.choice([True, False]),
                "sunday": rng.choice([True, False]),
            },
        ))

    fares = []
    for route in routes:
        fares.append(Fare(
            fare_id=f"fare_{route.route_id[:8]}",
            route_id=route.route_id,
            agency_id=f"op_{route.operator_name.lower().replace(' ', '_')[:20]}",
            price=rng.choice([0.25, 0.30, 0.35, 0.40, 0.50]),
        ))

    return QualityGateInput(
        canton=canton, province=province,
        stops=stops, routes=routes, shapes=shapes,
        semantics=semantics, schedules=schedules, fares=fares,
    )
