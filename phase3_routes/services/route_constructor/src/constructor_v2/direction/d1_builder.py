"""
Bidirectional (d1) Sequence Builder
====================================

Builds direction_id=1 (return) sequences for Valle de los Chillos routes.

Three tiers:
  Tier 1: Import confirmed d1 sequences from GTFS reference (ground truth)
  Tier 2: Extract d1 corridor constraints from confirmed data
  Tier 3: Build d1 sequences for remaining routes (swap termini + V2 pipeline)

Key principle: d1 is NOT reversed d0. Each direction gets independent
stop grounding and ordering, because:
  - Autopista has separate carriageways with different stops per side
  - Quito uses one-way street pairs (outbound vs return use different roads)
  - d0 and d1 typically share only 20-40% of stops
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from src.constructor_v2.common import haversine_m as _haversine_m

log = logging.getLogger(__name__)

VALHALLA_URL = os.getenv("VALHALLA_URL", "http://127.0.0.1:8003")


@dataclass
class D1Sequence:
    route_name: str
    route_short: str
    direction_id: int  # always 1
    tier: str  # "confirmed", "corridor_constrained", "constructed"
    stops: list[dict]  # [{stop_id, name, lat, lon, sequence}, ...]
    terminus_start: dict  # {name, lat, lon}
    terminus_end: dict  # {name, lat, lon}
    source: str  # "confirmed_gtfs", "valhalla_constructed"
    confidence: float  # 0-100
    notes: str = ""


def load_confirmed_d1_sequences(
    reference_path: str = "constructor_artifacts/confirmed_gtfs_reference.json",
) -> list[D1Sequence]:
    """
    Tier 1: Load confirmed d1 sequences from the GTFS reference file.
    These are ground truth — no construction needed.
    """
    with open(reference_path) as f:
        data = json.load(f)

    confirmed = data.get("confirmed_routes", {})
    results: list[D1Sequence] = []

    for key, route in confirmed.items():
        if not key.endswith("_d1"):
            continue

        stops = route.get("stops", [])
        if not stops:
            continue

        short = key.replace("_d1", "")
        results.append(
            D1Sequence(
                route_name=route["route_name"],
                route_short=short,
                direction_id=1,
                tier="confirmed",
                stops=stops,
                terminus_start={
                    "name": stops[0].get("name", ""),
                    "lat": stops[0]["lat"],
                    "lon": stops[0]["lon"],
                },
                terminus_end={
                    "name": stops[-1].get("name", ""),
                    "lat": stops[-1]["lat"],
                    "lon": stops[-1]["lon"],
                },
                source="confirmed_gtfs",
                confidence=100.0,
                notes=f"Ground truth from confirmed GTFS ({len(stops)} stops)",
            )
        )

    log.info("Loaded %d confirmed d1 sequences", len(results))
    return results


def extract_d1_corridor_constraints(
    reference_path: str = "constructor_artifacts/confirmed_gtfs_reference.json",
) -> dict[str, list[dict]]:
    """
    Tier 2: Extract d1-specific corridor orderings from confirmed data.

    Returns: {corridor_name: [{stop_id, name, lat, lon, sequence}, ...]}
    """
    with open(reference_path) as f:
        data = json.load(f)

    corridors = data.get("canonical_corridors", {})
    d1_corridors: dict[str, list[dict]] = {}

    # For each corridor, check if we have d1 data from confirmed routes
    confirmed = data.get("confirmed_routes", {})

    # Build corridor → d1 stops mapping from confirmed routes
    corridor_routes = {
        "autopista_marin_triangulo": ["TTU-02_d1", "EXP-01_d1", "LIB-01_d1"],
        "e35_triangulo_pintag": ["EXP-01_d1"],
        "puengasi_marin_merced": ["TTU-03_d1"],
        "conocoto_through": ["LIB-01_d1", "LIB-04_d1"],
    }

    for corridor_name, route_keys in corridor_routes.items():
        corridor_stops = corridors.get(corridor_name, {}).get("stops", [])
        if not corridor_stops:
            continue

        # Get stop IDs from all d1 routes that use this corridor
        d1_stop_ids: set[str] = set()
        d1_stop_order: list[dict] = []

        for rk in route_keys:
            route_data = confirmed.get(rk, {})
            for s in route_data.get("stops", []):
                sid = s.get("stop_id", "")
                if sid and sid not in d1_stop_ids:
                    d1_stop_ids.add(sid)
                    d1_stop_order.append(s)

        if d1_stop_order:
            d1_corridors[corridor_name] = d1_stop_order
            log.info(
                "Extracted d1 corridor constraint: %s (%d stops)",
                corridor_name,
                len(d1_stop_order),
            )

    return d1_corridors


def _valhalla_route(
    waypoints: list[tuple[float, float]],
    *,
    costing: str = "bus",
    url: str = VALHALLA_URL,
) -> Optional[dict]:
    """Call Valhalla /route with waypoints, return decoded geometry."""
    locations = [{"lat": lat, "lon": lon} for lat, lon in waypoints]

    payload = {
        "locations": locations,
        "costing": costing,
        "directions_options": {"units": "km"},
        "shape_match": "map_snap",
    }

    try:
        resp = requests.post(f"{url}/route", json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("Valhalla route failed: %s", e)
        return None


def _ground_stops_along_corridor(
    conn,
    corridor_coords: list[tuple[float, float]],
    *,
    scan_radius_m: float = 60.0,
    min_spacing_m: float = 200.0,
) -> list[dict]:
    """
    Find canonical stops near a Valhalla-routed corridor.
    Returns stops in corridor-order (by projection distance),
    filtered to keep only the closest stop per min_spacing_m interval.
    """
    if not corridor_coords or len(corridor_coords) < 2:
        return []

    # Build a LineString from corridor coords
    coords_wkt = ", ".join(f"{lon} {lat}" for lat, lon in corridor_coords)
    linestring_wkt = f"LINESTRING({coords_wkt})"

    with conn.cursor() as cur:
        cur.execute(
            """
            WITH corridor AS (
                SELECT ST_GeomFromText(%s, 4326)::geography AS geog,
                       ST_GeomFromText(%s, 4326) AS geom
            )
            SELECT
                n.node_id AS stop_id,
                n.name AS stop_name,
                ST_Y(n.geom) AS lat,
                ST_X(n.geom) AS lon,
                ST_Distance(n.geom::geography, c.geog) AS dist_m,
                ST_LineLocatePoint(c.geom, n.geom) AS fraction
            FROM node_prod.nodes n, corridor c
            WHERE n.node_type = 'STOP'
              AND ST_DWithin(n.geom::geography, c.geog, %s)
            ORDER BY fraction ASC, dist_m ASC
            """,
            (linestring_wkt, linestring_wkt, scan_radius_m),
        )
        rows = cur.fetchall()

    # Parse raw rows
    raw_stops = []
    for row in rows:
        raw_stops.append({
            "stop_id": str(row[0] if not hasattr(row, "get") else row.get("stop_id", row[0])),
            "name": str(row[1] if not hasattr(row, "get") else row.get("stop_name", row[1])),
            "lat": float(row[2] if not hasattr(row, "get") else row.get("lat", row[2])),
            "lon": float(row[3] if not hasattr(row, "get") else row.get("lon", row[3])),
            "dist_to_corridor_m": float(row[4] if not hasattr(row, "get") else row.get("dist_m", row[4])),
            "fraction": float(row[5] if not hasattr(row, "get") else row.get("fraction", row[5])),
        })

    if not raw_stops:
        return []

    # Filter: keep the closest stop per min_spacing_m interval.
    # Always keep the first and last stop.
    filtered = [raw_stops[0]]
    for s in raw_stops[1:]:
        dist_to_last = _haversine_m(
            filtered[-1]["lat"], filtered[-1]["lon"],
            s["lat"], s["lon"],
        )
        if dist_to_last >= min_spacing_m:
            filtered.append(s)
        elif s["dist_to_corridor_m"] < filtered[-1]["dist_to_corridor_m"]:
            # Closer to corridor — replace last
            filtered[-1] = s

    # Renumber
    for i, s in enumerate(filtered):
        s["sequence"] = i + 1

    return filtered


def build_d1_for_route(
    conn,
    *,
    route_name: str,
    d0_ordered_stops: list[dict],
    d1_corridor_constraints: Optional[dict[str, list[dict]]] = None,
    scan_radius_m: float = 60.0,
) -> Optional[D1Sequence]:
    """
    Tier 3: Build a d1 sequence for a route that doesn't have confirmed d1 data.

    Process:
    1. Swap termini (d0 start becomes d1 end, d0 end becomes d1 start)
    2. Route via Valhalla with swapped termini (gets return-direction roads)
    3. Ground canonical stops along the return corridor
    4. Apply d1 corridor constraints if available
    5. Return the d1 sequence

    Args:
        conn: DB connection
        route_name: route name for logging
        d0_ordered_stops: the d0 ordered stops from V2 artifact
        d1_corridor_constraints: optional d1 corridor orderings
        scan_radius_m: radius for stop grounding
    """
    if len(d0_ordered_stops) < 2:
        log.warning("Route %s has < 2 d0 stops, cannot build d1", route_name)
        return None

    # Swap termini
    d0_start = d0_ordered_stops[0]
    d0_end = d0_ordered_stops[-1]

    d1_start_lat, d1_start_lon = d0_end["lat"], d0_end["lon"]
    d1_end_lat, d1_end_lon = d0_start["lat"], d0_start["lon"]

    # Build Valhalla waypoints: d1_start -> d1_end
    # Include intermediate anchors from d0 in reverse as hints
    anchors = [s for s in d0_ordered_stops if s.get("is_known_anchor")]
    reverse_anchors = list(reversed(anchors))

    waypoints = [(d1_start_lat, d1_start_lon)]
    for a in reverse_anchors:
        # Skip if same as start or end
        if (abs(a["lat"] - d1_start_lat) < 0.001 and abs(a["lon"] - d1_start_lon) < 0.001):
            continue
        if (abs(a["lat"] - d1_end_lat) < 0.001 and abs(a["lon"] - d1_end_lon) < 0.001):
            continue
        waypoints.append((a["lat"], a["lon"]))
    waypoints.append((d1_end_lat, d1_end_lon))

    # Route via Valhalla
    valhalla_result = _valhalla_route(waypoints)
    if not valhalla_result:
        log.warning("Valhalla routing failed for d1 of %s", route_name)
        return None

    # Extract corridor coordinates from Valhalla response
    try:
        legs = valhalla_result.get("trip", {}).get("legs", [])
        corridor_coords = []
        for leg in legs:
            shape = leg.get("shape", "")
            if shape:
                # Decode polyline6
                decoded = _decode_polyline6(shape)
                corridor_coords.extend(decoded)
    except Exception as e:
        log.warning("Could not decode Valhalla shape for d1 of %s: %s", route_name, e)
        return None

    if len(corridor_coords) < 2:
        log.warning("Valhalla returned empty corridor for d1 of %s", route_name)
        return None

    # Ground stops along return corridor
    d1_stops = _ground_stops_along_corridor(
        conn, corridor_coords, scan_radius_m=scan_radius_m
    )

    if len(d1_stops) < 3:
        log.warning(
            "Only %d stops grounded for d1 of %s (need >= 3)",
            len(d1_stops),
            route_name,
        )
        return None

    # Renumber sequences
    for i, s in enumerate(d1_stops):
        s["sequence"] = i + 1

    return D1Sequence(
        route_name=route_name,
        route_short="",
        direction_id=1,
        tier="constructed",
        stops=d1_stops,
        terminus_start={
            "name": d0_end.get("stop_name", d0_end.get("name", "")),
            "lat": d1_start_lat,
            "lon": d1_start_lon,
        },
        terminus_end={
            "name": d0_start.get("stop_name", d0_start.get("name", "")),
            "lat": d1_end_lat,
            "lon": d1_end_lon,
        },
        source="valhalla_constructed",
        confidence=60.0,  # conservative default for constructed d1
        notes=f"Built from d0 with swapped termini, {len(d1_stops)} stops grounded",
    )


def _decode_polyline6(encoded: str) -> list[tuple[float, float]]:
    """Decode a Google-style polyline with precision 6."""
    coords = []
    index = 0
    lat = 0
    lon = 0

    while index < len(encoded):
        # Latitude
        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat

        # Longitude
        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlon = ~(result >> 1) if (result & 1) else (result >> 1)
        lon += dlon

        coords.append((lat / 1e6, lon / 1e6))

    return coords


def build_bidirectional_artifact(
    conn,
    d0_artifact_path: str,
    reference_path: str = "constructor_artifacts/confirmed_gtfs_reference.json",
    output_path: str = "constructor_artifacts/valle_v2_BIDIRECTIONAL_39.json",
    *,
    scan_radius_m: float = 60.0,
) -> dict:
    """
    Build a complete bidirectional artifact combining:
    - Tier 1: Confirmed d1 sequences (9 routes)
    - Tier 3: Constructed d1 sequences (remaining routes)

    Args:
        conn: DB connection
        d0_artifact_path: path to V2 d0 artifact
        reference_path: path to confirmed GTFS reference
        output_path: where to write the bidirectional artifact
        scan_radius_m: radius for d1 stop grounding
    """
    with open(d0_artifact_path) as f:
        d0_artifact = json.load(f)

    # Tier 1: Load confirmed d1
    confirmed_d1 = load_confirmed_d1_sequences(reference_path)
    confirmed_names = {d.route_name for d in confirmed_d1}

    # Tier 2: Load d1 corridor constraints
    d1_corridors = extract_d1_corridor_constraints(reference_path)

    # Build d1 for each d0 route
    d1_results: list[dict] = []
    stats = {"confirmed": 0, "constructed": 0, "failed": 0}

    # Map confirmed routes by name for lookup
    with open(reference_path) as f:
        ref_data = json.load(f)
    benchmark = ref_data.get("benchmark_mapping", {}).get("direct_matches", {})

    for d0_route in d0_artifact.get("routes", []):
        route_name = d0_route["route"]

        # Check if this route has a confirmed d1
        matched_short = benchmark.get(route_name)
        confirmed_match = None
        if matched_short:
            for cd1 in confirmed_d1:
                if cd1.route_short == matched_short:
                    confirmed_match = cd1
                    break

        if confirmed_match:
            # Tier 1: Use confirmed d1 (ground truth)
            d1_entry = {
                "route": route_name,
                "cooperative": d0_route.get("cooperative", ""),
                "direction_id": 1,
                "tier": "confirmed",
                "classification": "strong",
                "confidence": {"label": "strong", "score": 100.0, "auto_accept": True, "reasons": []},
                "auto_accept": True,
                "stops_constructed": len(confirmed_match.stops),
                "ordered_stops": [
                    {
                        "seq": s["sequence"],
                        "stop_id": s["stop_id"],
                        "stop_name": s.get("name", ""),
                        "lat": s["lat"],
                        "lon": s["lon"],
                        "is_fixed_start": s["sequence"] == 1,
                        "is_fixed_end": s["sequence"] == len(confirmed_match.stops),
                        "is_known_anchor": False,
                        "weak_candidate": False,
                        "representative_score": 0.8,
                    }
                    for s in confirmed_match.stops
                ],
                "source": "confirmed_gtfs",
                "notes": confirmed_match.notes,
            }
            d1_results.append(d1_entry)
            stats["confirmed"] += 1
            log.info("Tier 1 (confirmed): %s -> %d stops", route_name, len(confirmed_match.stops))
        else:
            # Tier 3: Build d1 from d0 with swapped termini
            d1_seq = build_d1_for_route(
                conn,
                route_name=route_name,
                d0_ordered_stops=d0_route.get("ordered_stops", []),
                d1_corridor_constraints=d1_corridors,
                scan_radius_m=scan_radius_m,
            )

            if d1_seq:
                d1_entry = {
                    "route": route_name,
                    "cooperative": d0_route.get("cooperative", ""),
                    "direction_id": 1,
                    "tier": "constructed",
                    "classification": "acceptable" if d1_seq.confidence >= 60 else "ambiguous",
                    "confidence": {
                        "label": "acceptable" if d1_seq.confidence >= 60 else "ambiguous",
                        "score": d1_seq.confidence,
                        "auto_accept": d1_seq.confidence >= 70,
                        "reasons": [],
                    },
                    "auto_accept": d1_seq.confidence >= 70,
                    "stops_constructed": len(d1_seq.stops),
                    "ordered_stops": [
                        {
                            "seq": s["sequence"],
                            "stop_id": s["stop_id"],
                            "stop_name": s.get("name", ""),
                            "lat": s["lat"],
                            "lon": s["lon"],
                            "is_fixed_start": s["sequence"] == 1,
                            "is_fixed_end": s["sequence"] == len(d1_seq.stops),
                            "is_known_anchor": False,
                            "weak_candidate": False,
                            "representative_score": 0.5,
                        }
                        for s in d1_seq.stops
                    ],
                    "source": "valhalla_constructed",
                    "notes": d1_seq.notes,
                }
                d1_results.append(d1_entry)
                stats["constructed"] += 1
                log.info(
                    "Tier 3 (constructed): %s -> %d stops, confidence=%.1f",
                    route_name,
                    len(d1_seq.stops),
                    d1_seq.confidence,
                )
            else:
                stats["failed"] += 1
                log.warning("Failed to build d1 for %s", route_name)

    # Build output artifact
    artifact = {
        "version": "v2_bidirectional",
        "direction_id": 1,
        "generated": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "summary": {
            "total_routes": len(d1_results),
            "confirmed_d1": stats["confirmed"],
            "constructed_d1": stats["constructed"],
            "failed": stats["failed"],
            "auto_accepted": sum(1 for r in d1_results if r.get("auto_accept")),
            "total_stops": sum(r["stops_constructed"] for r in d1_results),
        },
        "routes": d1_results,
    }

    with open(output_path, "w") as f:
        json.dump(artifact, f, indent=2, ensure_ascii=False)

    log.info(
        "Bidirectional artifact: %d routes (%d confirmed, %d constructed, %d failed) -> %s",
        len(d1_results),
        stats["confirmed"],
        stats["constructed"],
        stats["failed"],
        output_path,
    )

    return artifact
