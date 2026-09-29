"""
Valhalla integration for Runtime Lab v2.

Handles:
  - Mapping routes to Valhalla edges via trace_attributes
  - Decomposing Deep Research route runtimes into per-edge speeds
  - Generating Valhalla predicted-traffic CSVs
  - Loading traffic tiles into the runtime Valhalla instance
"""
from __future__ import annotations

import json
import math
import os
import subprocess
from typing import Any, Dict, List, Optional

import requests

# Resolved from env/settings so a single Valhalla host can serve any
# province. The default (``http://127.0.0.1:8003``) matches the pre-
# generalization hardcode exactly.
try:
    from datamind_core.settings import VALHALLA_URL as _VALHALLA_URL
except Exception:  # pragma: no cover - defensive fallback
    _VALHALLA_URL = os.environ.get("VALHALLA_URL", "http://127.0.0.1:8003")

VALHALLA_CONSTRUCTION = _VALHALLA_URL
VALHALLA_RUNTIME = os.environ.get("VALHALLA_RUNTIME_URL", "http://localhost:8004")
VALHALLA_TIMEOUT = 30

BUS_COSTING_CONFIG = os.path.join(os.path.dirname(__file__), "bus_costing_config.json")


def _load_bus_costing() -> Dict[str, Any]:
    """Load bus costing_options from config file if it exists."""
    if os.path.exists(BUS_COSTING_CONFIG):
        with open(BUS_COSTING_CONFIG) as f:
            config = json.load(f)
        return config.get("costing_options", {})
    return {}

# Road-class weights: slower classes get proportionally more time per meter
ROAD_CLASS_WEIGHTS: Dict[str, float] = {
    "motorway": 0.6,
    "trunk": 0.7,
    "primary": 0.85,
    "secondary": 1.0,
    "tertiary": 1.15,
    "residential": 1.3,
    "service": 1.5,
    "unclassified": 1.2,
}


# ------------------------------------------------------------------
# Edge mapping
# ------------------------------------------------------------------

def map_route_to_edges(route_geometry_coords: list) -> List[Dict[str, Any]]:
    """
    Use Valhalla trace_attributes to decompose a route polyline
    into a sequence of Valhalla edge IDs with their properties.

    Parameters
    ----------
    route_geometry_coords : list
        List of ``[lon, lat]`` from ``route_prod.routes.geom``.

    Returns
    -------
    list of edge dicts with ``edge_id``, ``osm_way_id``, ``length_m``,
    ``speed_limit_kmh``, ``road_class``, ``begin_shape_index``,
    ``end_shape_index``, ``grade``.
    """
    # Subsample if too many points (trace_attributes has a limit)
    coords = route_geometry_coords
    if len(coords) > 500:
        step = max(1, len(coords) // 400)
        coords = coords[::step]
        if coords[-1] != route_geometry_coords[-1]:
            coords.append(route_geometry_coords[-1])

    shape = [{"lon": c[0], "lat": c[1]} for c in coords]

    resp = requests.post(
        f"{VALHALLA_CONSTRUCTION}/trace_attributes",
        json={
            "shape": shape,
            "costing": "bus",
            "shape_match": "map_snap",
            "filters": {
                "attributes": [
                    "edge.id",
                    "edge.way_id",
                    "edge.length",
                    "edge.speed",
                    "edge.road_class",
                    "edge.begin_shape_index",
                    "edge.end_shape_index",
                    "edge.weighted_grade",
                ],
                "action": "include",
            },
        },
        timeout=VALHALLA_TIMEOUT,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"trace_attributes failed ({resp.status_code}): {resp.text[:300]}")

    result = resp.json()
    edges: List[Dict[str, Any]] = []
    for edge in result.get("edges", []):
        edges.append({
            "edge_id": edge.get("id"),
            "osm_way_id": edge.get("way_id"),
            "length_m": (edge.get("length") or 0) * 1000,
            "speed_limit_kmh": edge.get("speed", 30),
            "road_class": edge.get("road_class", "tertiary"),
            "begin_shape_index": edge.get("begin_shape_index", 0),
            "end_shape_index": edge.get("end_shape_index", 0),
            "grade": edge.get("weighted_grade", 0),
        })

    return edges


# ------------------------------------------------------------------
# Decompose runtime → per-edge speeds
# ------------------------------------------------------------------

def decompose_runtime_to_edge_speeds(
    edges: List[Dict[str, Any]],
    runtime_data: dict,
    hotspots: Optional[List[dict]] = None,
) -> Dict[int, Dict[str, Any]]:
    """
    Given a route's edges and Deep Research runtime ranges,
    compute speed per edge per time period.

    Parameters
    ----------
    edges : list
        From ``map_route_to_edges()``.
    runtime_data : dict
        Per-period runtime ranges, e.g.::

            {
                "peak_am": {"min_min": 55, "typical_min": 65, "max_min": 80},
                "off_peak": {"min_min": 40, "typical_min": 48, "max_min": 55},
                ...
            }
    hotspots : list, optional
        Congestion hotspot dicts with ``lat``, ``lon``, ``<period>_delay_min``.

    Returns
    -------
    dict of ``{edge_id: {osm_way_id, length_m, speeds: {period: speed_kmh}}}``.
    """
    total_length_m = sum(e["length_m"] for e in edges)
    if total_length_m <= 0:
        return {}

    edge_speeds: Dict[int, Dict[str, Any]] = {}

    for period in ("peak_am", "off_peak", "peak_pm", "night"):
        rt = runtime_data.get(period)
        if not rt:
            continue
        typical_secs = rt.get("typical_min", 45) * 60

        # Weight edges by road class + grade
        weighted_lengths: List[float] = []
        for e in edges:
            w = ROAD_CLASS_WEIGHTS.get(e["road_class"], 1.0)

            grade_pct = abs(e.get("grade") or 0)
            if grade_pct > 2:
                w *= 1.0 + (grade_pct - 2) * 0.05

            if hotspots:
                for hs in hotspots:
                    if _is_edge_near_hotspot(e, hs):
                        delay_key = f"{period}_delay_min"
                        if delay_key in hs:
                            hotspot_extra_secs = hs[delay_key] * 60
                            equiv_extra_m = hotspot_extra_secs * (e["speed_limit_kmh"] / 3.6)
                            w *= 1.0 + (equiv_extra_m / max(e["length_m"], 1))

            weighted_lengths.append(e["length_m"] * w)

        total_weighted = sum(weighted_lengths)
        if total_weighted <= 0:
            continue

        for i, e in enumerate(edges):
            edge_fraction = weighted_lengths[i] / total_weighted
            edge_time_secs = typical_secs * edge_fraction
            edge_speed_kmh = (e["length_m"] / edge_time_secs) * 3.6 if edge_time_secs > 0 else 30
            edge_speed_kmh = max(3, min(80, edge_speed_kmh))

            eid = e["edge_id"]
            if eid not in edge_speeds:
                edge_speeds[eid] = {
                    "osm_way_id": e["osm_way_id"],
                    "length_m": e["length_m"],
                    "speeds": {},
                }
            edge_speeds[eid]["speeds"][period] = round(edge_speed_kmh, 1)

    return edge_speeds


def _is_edge_near_hotspot(
    edge: Dict[str, Any], hotspot: dict, threshold_m: float = 500
) -> bool:
    """Check if an edge is near a congestion hotspot coordinate (placeholder)."""
    # Full implementation requires edge geometry from trace_attributes.
    # For now, always False — hotspot logic will be activated when edge
    # geometry is available.
    return False


# ------------------------------------------------------------------
# Generate Valhalla predicted-traffic CSV
# ------------------------------------------------------------------

def generate_valhalla_traffic_csv(
    all_edge_speeds: Dict[int, Dict[str, Any]],
    output_dir: str = "/tmp/valhalla_traffic",
) -> str:
    """
    Generate Valhalla-format predicted traffic CSV.

    Writes a flat file with ``edge_id, freeflow_speed, constrained_speed``
    plus 2016 weekly speed buckets (5-min intervals x 7 days).

    Returns path to the generated CSV.
    """
    os.makedirs(output_dir, exist_ok=True)

    rows: List[Dict[str, Any]] = []
    for edge_id, data in all_edge_speeds.items():
        speeds = data["speeds"]

        freeflow = speeds.get("night", speeds.get("off_peak", 30))
        peak_speeds = [
            s for s in [speeds.get("peak_am"), speeds.get("peak_pm")] if s is not None
        ]
        constrained = sum(peak_speeds) / len(peak_speeds) if peak_speeds else freeflow

        # 2016 weekly speed buckets (5-min intervals × 7 days, Sun=0)
        weekly_speeds: List[float] = []
        for day in range(7):
            is_weekday = day in (1, 2, 3, 4, 5)
            for bucket in range(288):
                hour = bucket // 12
                if not is_weekday:
                    speed = (
                        speeds.get("off_peak", freeflow) * 0.95
                        if 10 <= hour <= 14
                        else speeds.get("night", freeflow)
                    )
                elif 6 <= hour <= 9:
                    speed = speeds.get("peak_am", constrained)
                elif 16 <= hour <= 19:
                    speed = speeds.get("peak_pm", constrained)
                elif 9 < hour < 16:
                    speed = speeds.get("off_peak", (freeflow + constrained) / 2)
                else:
                    speed = speeds.get("night", freeflow)

                weekly_speeds.append(max(5, round(speed, 1)))

        rows.append({
            "edge_id": edge_id,
            "freeflow_speed": round(freeflow, 1),
            "constrained_speed": round(constrained, 1),
            "weekly_speeds": weekly_speeds,
        })

    # Write simple format (freeflow + constrained)
    csv_path = os.path.join(output_dir, "predicted_traffic.csv")
    with open(csv_path, "w") as f:
        for row in rows:
            f.write(f"{row['edge_id']},{row['freeflow_speed']},{row['constrained_speed']}\n")

    print(f"Generated traffic CSV: {len(rows)} edges → {csv_path}")
    return csv_path


# ------------------------------------------------------------------
# Load traffic into Valhalla runtime instance
# ------------------------------------------------------------------

def load_traffic_into_valhalla(traffic_dir: str = "/tmp/valhalla_traffic") -> None:
    """
    Run ``valhalla_add_predicted_traffic`` inside the runtime container
    and restart the service to pick up updated tiles.
    """
    # Copy traffic CSV into the container
    subprocess.run(
        ["docker", "cp", traffic_dir, "valhalla-runtime:/data/traffic/"],
        check=True,
    )

    result = subprocess.run(
        [
            "docker", "exec", "valhalla-runtime",
            "valhalla_add_predicted_traffic",
            "-t", "/data/traffic/",
            "--config", "/data/valhalla.json",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to load traffic: {result.stderr[:500]}")

    print("Traffic loaded into Valhalla runtime instance")

    subprocess.run(["docker", "restart", "valhalla-runtime"], check=True)
    print("Valhalla runtime restarted with updated traffic")


# ------------------------------------------------------------------
# Valhalla route query (time-of-day aware)
# ------------------------------------------------------------------

# Representative date-times for each period (known non-holiday, 2026)
PERIOD_DATETIME: Dict[str, str] = {
    "peak_am": "2026-04-06T07:30",
    "off_peak": "2026-04-06T14:00",
    "peak_pm": "2026-04-06T17:30",
    "night": "2026-04-06T21:00",
}


def valhalla_route_time(
    stops: List[Dict[str, float]],
    time_period: str = "off_peak",
    *,
    url: str = VALHALLA_RUNTIME,
) -> Optional[Dict[str, Any]]:
    """
    Query Valhalla for a bus route through ordered stops.

    Parameters
    ----------
    stops : list
        ``[{"lat": ..., "lon": ...}, ...]``
    time_period : str
        One of ``peak_am``, ``off_peak``, ``peak_pm``, ``night``.

    Returns
    -------
    Valhalla trip response dict, or ``None`` on failure.
    """
    locations = [{"lat": s["lat"], "lon": s["lon"], "type": "through"} for s in stops]
    locations[0]["type"] = "break"
    locations[-1]["type"] = "break"

    dt = PERIOD_DATETIME.get(time_period, "2026-04-06T14:00")

    try:
        payload: Dict[str, Any] = {
            "locations": locations,
            "costing": "bus",
            "date_time": {"type": 1, "value": dt},
            "directions_options": {"units": "kilometers"},
        }
        costing_opts = _load_bus_costing()
        if costing_opts:
            payload["costing_options"] = costing_opts

        resp = requests.post(
            f"{url}/route",
            json=payload,
            timeout=VALHALLA_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None
