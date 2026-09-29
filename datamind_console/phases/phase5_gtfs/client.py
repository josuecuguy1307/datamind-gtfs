from __future__ import annotations

import os
import json
import csv
import io
import zipfile
import math
import uuid
import re
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections.abc import Mapping
from urllib.parse import quote_plus
from urllib import request as urlrequest
from urllib import parse as urlparse
from urllib import error as urlerror
from difflib import SequenceMatcher

import psycopg2
from psycopg2.extras import RealDictCursor
import psycopg2.extras
import streamlit as st

from phase5_gtfs.common.ids import new_uuid
from phase5_gtfs.schedules.validate import validate_window
from phase5_gtfs.schedules.store import run_all_migrations, ensure_default_profiles_for_verified_routes, ensure_default_windows
from phase5_gtfs.compiler.build_calendar import build_calendar
from phase5_gtfs.compiler.build_routes import build_routes_and_stops
from phase5_gtfs.compiler.build_shapes import build_shapes
from phase5_gtfs.compiler.build_trips import build_trips_and_frequencies
from phase5_gtfs.compiler.build_stop_times import build_stop_times
from phase5_gtfs.validator.gtfs_validate import validate_export_run
from phase5_gtfs.validator.stop_coverage import (
    get_stop_coverage_metrics,
    list_orphan_stops,
    list_served_stops,
)
from phase5_gtfs.publisher.package_gtfs import package_gtfs
from phase3_routes.services.route_constructor.src.geometry.valhalla_client import valhalla_route
from datamind_console.services.gtfs_exports_service import create_pending_artifact


TABLE_PKS: Dict[str, List[str]] = {
    "gtfs_agency": ["agency_id"],
    "gtfs_stops": ["stop_id"],
    "gtfs_routes": ["route_id"],
    "gtfs_shapes": ["shape_id", "shape_pt_sequence"],
    "gtfs_calendar": ["service_id"],
    "gtfs_calendar_dates": ["service_id", "date"],
    "gtfs_trips": ["trip_id"],
    "gtfs_stop_times": ["trip_id", "stop_sequence"],
    "gtfs_frequencies": ["trip_id", "start_time"],
}


def _normalize_text(v: Any) -> str:
    s = str(v or "").strip().lower()
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _slugify(v: Any) -> str:
    s = _normalize_text(v).replace(" ", "_")
    s = re.sub(r"[^a-z0-9_]+", "", s).strip("_")
    return s or "agency"


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return float(SequenceMatcher(a=a, b=b).ratio())


def _token_jaccard(a: str, b: str) -> float:
    aa = {x for x in str(a or "").split(" ") if x}
    bb = {x for x in str(b or "").split(" ") if x}
    if not aa or not bb:
        return 0.0
    inter = len(aa.intersection(bb))
    union = len(aa.union(bb))
    return float(inter / union) if union > 0 else 0.0


def _time_to_secs(v: Any) -> Optional[int]:
    s = str(v or "").strip()
    if not s or ":" not in s:
        return None
    try:
        parts = [int(x) for x in s.split(":")]
    except Exception:
        return None
    if len(parts) == 2:
        h, m = parts
        sec = 0
    elif len(parts) >= 3:
        h, m, sec = parts[0], parts[1], parts[2]
    else:
        return None
    return int(h * 3600 + m * 60 + sec)


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.asin(math.sqrt(a))


def _polyline_length_m(path: List[List[float]]) -> float:
    if len(path) < 2:
        return 0.0
    acc = 0.0
    for i in range(1, len(path)):
        a = path[i - 1]
        b = path[i]
        acc += _haversine_m(float(a[0]), float(a[1]), float(b[0]), float(b[1]))
    return acc


def _geojson_to_lonlat_path(geojson: Any) -> List[List[float]]:
    if not isinstance(geojson, dict):
        return []
    gtype = str(geojson.get("type") or "")
    coords = geojson.get("coordinates") or []
    out: List[List[float]] = []
    if gtype == "LineString" and isinstance(coords, list):
        for pt in coords:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                out.append([float(pt[0]), float(pt[1])])
        return out
    if gtype == "MultiLineString" and isinstance(coords, list):
        for seg in coords:
            if isinstance(seg, list):
                for pt in seg:
                    if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                        out.append([float(pt[0]), float(pt[1])])
        return out
    return out


def _path_cumulative_m(path: List[List[float]]) -> List[float]:
    if not path:
        return []
    cum: List[float] = [0.0]
    for i in range(1, len(path)):
        a = path[i - 1]
        b = path[i]
        cum.append(cum[-1] + _haversine_m(float(a[0]), float(a[1]), float(b[0]), float(b[1])))
    return cum


def _nearest_path_vertex_idx(path: List[List[float]], lon: float, lat: float) -> int:
    if not path:
        return 0
    best_i = 0
    best_d = float("inf")
    for i, p in enumerate(path):
        d = _haversine_m(float(lon), float(lat), float(p[0]), float(p[1]))
        if d < best_d:
            best_d = d
            best_i = i
    return int(best_i)


def _lonlat_to_xy_m(lon: float, lat: float, lat0: float) -> tuple[float, float]:
    r = 6371000.0
    x = math.radians(lon) * r * math.cos(math.radians(lat0))
    y = math.radians(lat) * r
    return (x, y)


def _project_point_to_path_dist_m(
    path: List[List[float]],
    path_cum: List[float],
    *,
    lon: float,
    lat: float,
) -> Optional[float]:
    if len(path) < 2 or len(path_cum) < 2:
        return None
    lat0 = float(lat)
    px, py = _lonlat_to_xy_m(float(lon), float(lat), lat0)
    best = None
    for i in range(len(path) - 1):
        a = path[i]
        b = path[i + 1]
        ax, ay = _lonlat_to_xy_m(float(a[0]), float(a[1]), lat0)
        bx, by = _lonlat_to_xy_m(float(b[0]), float(b[1]), lat0)
        dx = bx - ax
        dy = by - ay
        seg_len2 = (dx * dx) + (dy * dy)
        if seg_len2 <= 1e-9:
            continue
        t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        projx = ax + (t * dx)
        projy = ay + (t * dy)
        dist2 = (px - projx) ** 2 + (py - projy) ** 2
        if best is None or dist2 < best[0]:
            best = (dist2, i, t)
    if best is None:
        return None
    _, i, t = best
    if i + 1 >= len(path_cum):
        return None
    seg_m = float(path_cum[i + 1] - path_cum[i])
    if seg_m < 0:
        seg_m = 0.0
    return float(path_cum[i] + (float(t) * seg_m))


def _safe_num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _parse_ele_m(v: Any) -> Optional[float]:
    s = str(v or "").strip().lower()
    if not s:
        return None
    s = s.replace("metros", "").replace("metro", "").replace("m", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def _http_json_get(url: str, timeout: int = 20) -> Dict[str, Any]:
    req = urlrequest.Request(url=url, method="GET")
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw or "{}")


def _http_text_post(url: str, body: str, timeout: int = 25, content_type: str = "application/x-www-form-urlencoded") -> str:
    data = body.encode("utf-8")
    req = urlrequest.Request(url=url, data=data, method="POST")
    req.add_header("Content-Type", content_type)
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _open_elevation_lookup(points: List[Dict[str, float]], *, endpoint: str, timeout: int = 25) -> Dict[int, Optional[float]]:
    """
    points: [{lat, lon, idx}]
    Returns map idx -> elevation_m (or None).
    """
    out: Dict[int, Optional[float]] = {int(p.get("idx", i)): None for i, p in enumerate(points)}
    if not points:
        return out
    batch_size = 90
    for i in range(0, len(points), batch_size):
        chunk = points[i : i + batch_size]
        loc = "|".join(f"{float(p['lat'])},{float(p['lon'])}" for p in chunk)
        url = endpoint.rstrip("/") + "/api/v1/lookup?locations=" + urlparse.quote(loc, safe="|,.-")
        try:
            res = _http_json_get(url, timeout=timeout)
            vals = res.get("results") or []
            for j, p in enumerate(chunk):
                idx = int(p.get("idx", i + j))
                if j < len(vals):
                    try:
                        out[idx] = float(vals[j].get("elevation"))
                    except Exception:
                        out[idx] = None
        except Exception:
            continue
    return out


def _overpass_signals_bbox(
    *,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    endpoint: str,
    timeout: int = 30,
) -> List[Dict[str, float]]:
    query = f"""
    [out:json][timeout:25];
    (
      node["highway"="traffic_signals"]({min_lat},{min_lon},{max_lat},{max_lon});
    );
    out body;
    """
    try:
        raw = _http_text_post(endpoint, query, timeout=timeout, content_type="text/plain; charset=utf-8")
        data = json.loads(raw or "{}")
    except Exception:
        return []
    out: List[Dict[str, float]] = []
    for el in (data.get("elements") or []):
        try:
            out.append({"lat": float(el.get("lat")), "lon": float(el.get("lon"))})
        except Exception:
            continue
    return out


def _range_score(v: float, mn: float, mx: float) -> float:
    lo = float(min(mn, mx))
    hi = float(max(mn, mx))
    if hi <= lo:
        return 0.0
    if lo <= v <= hi:
        mid = (lo + hi) / 2.0
        half = (hi - lo) / 2.0
        if half <= 0:
            return 1.0
        return max(0.55, 1.0 - (abs(v - mid) / half) * 0.45)
    span = hi - lo
    dist = lo - v if v < lo else v - hi
    return max(0.0, 0.55 - (dist / max(1.0, span)) * 0.55)


def _density_score(value: float, expected_min: float, expected_max: float) -> float:
    lo = float(min(expected_min, expected_max))
    hi = float(max(expected_min, expected_max))
    if hi <= lo:
        return 0.0
    if lo <= value <= hi:
        return 1.0
    span = hi - lo
    dist = lo - value if value < lo else value - hi
    return max(0.0, 1.0 - dist / max(0.001, span))


def _dwell_route_family(route_name: str, route_ref: str) -> str:
    t = _normalize_text(f"{route_ref} {route_name}")
    if any(k in t for k in ["libertadores", "valle"]):
        return "valle_libertadores"
    if any(k in t for k in ["termas turis", "termas", "merced"]):
        return "valle_termas"
    if any(k in t for k in ["expreantisana", "antisana", "pintag", "san alfonso"]):
        return "antisana_rural"
    if "vingala" in t:
        return "vingala"
    return "generic"


def _infer_stop_role(*, stop: Dict[str, Any], stop_idx: int, n_stops: int) -> str:
    if stop_idx <= 0 or stop_idx >= max(0, n_stops - 1):
        return "terminal"
    t = _normalize_text(stop.get("canonical_name") or stop.get("name"))
    if any(k in t for k in ["terminal", "estacion", "marin", "giron", "conocoto", "cuarteles", "tingo", "salesiana", "espe"]):
        return "transfer"
    if stop_idx % 8 == 0:
        return "timepoint"
    return "regular"


def _infer_land_use_proxy(*, stop: Dict[str, Any]) -> str:
    t = _normalize_text(stop.get("canonical_name") or stop.get("name"))
    if any(k in t for k in ["mercado", "camal", "feria"]):
        return "market"
    if any(k in t for k in ["salesiana", "la salle", "espe", "universidad", "colegio", "escuela", "instituto"]):
        return "school_university"
    if any(k in t for k in ["hospital", "iess", "clinica", "salud"]):
        return "hospital"
    if any(k in t for k in ["centro", "comercial", "mall", "plaza"]):
        return "commercial"
    if any(k in t for k in ["pintag", "san alfonso", "rural", "hacienda"]) or str(stop.get("place_type") or "").lower() in {"hamlet", "village"}:
        return "rural"
    return "residential"


def _infer_lane_context(*, stop: Dict[str, Any]) -> str:
    t = _normalize_text(stop.get("canonical_name") or stop.get("name"))
    if any(k in t for k in ["autopista", "panamericana", "perimetral", "triangulo", "puente"]):
        return "highway_shoulder"
    if any(k in t for k in ["av ", "avenida", "via ", "via", "bypass"]):
        return "arterial"
    return "local_street"


def _infer_intersection_context(*, stop: Dict[str, Any], signal_count_leg: int) -> str:
    t = _normalize_text(stop.get("canonical_name") or stop.get("name"))
    if any(k in t for k in ["redondel", "rotonda"]):
        return "near_roundabout"
    if int(signal_count_leg) >= 2 or str(stop.get("highway_tag") or "") == "traffic_signals":
        return "near_signal"
    return "midblock"


def _infer_direction_bias(
    *,
    route_name: str,
    route_ref: str,
    direction_id: int,
    avg_grade_pct: float,
) -> str:
    fam = _dwell_route_family(route_name, route_ref)
    if fam in {"valle_libertadores", "valle_termas", "antisana_rural", "vingala"}:
        if abs(float(avg_grade_pct)) >= 3.0:
            return "dir0_heavier" if int(direction_id) == 0 else "dir1_heavier"
    return "symmetric"


def _compute_stop_dwell_factorized(
    *,
    stop: Dict[str, Any],
    route_name: str,
    route_ref: str,
    direction_id: int,
    stop_idx: int,
    n_stops: int,
    base_off_secs: float,
    base_peak_secs: float,
    signal_count_leg: int,
    grade_pct: float,
) -> Dict[str, Any]:
    role = _infer_stop_role(stop=stop, stop_idx=stop_idx, n_stops=n_stops)
    land_use = _infer_land_use_proxy(stop=stop)
    payment_mode = "onboard_cash"
    lane_context = _infer_lane_context(stop=stop)
    intersection_context = _infer_intersection_context(stop=stop, signal_count_leg=int(signal_count_leg))
    direction_bias = _infer_direction_bias(
        route_name=route_name,
        route_ref=route_ref,
        direction_id=int(direction_id),
        avg_grade_pct=float(grade_pct),
    )
    peak_window_profile = "AM_PM"

    role_mult = {
        "terminal": (2.25, 2.45),
        "transfer": (1.45, 1.70),
        "timepoint": (1.15, 1.25),
        "regular": (1.00, 1.00),
    }.get(role, (1.0, 1.0))
    land_mult = {
        "market": (1.55, 1.85),
        "school_university": (1.25, 1.55),
        "hospital": (1.20, 1.35),
        "commercial": (1.15, 1.30),
        "residential": (1.00, 1.12),
        "rural": (0.78, 0.92),
    }.get(land_use, (1.0, 1.0))
    payment_mult = {"onboard_cash": (1.20, 1.28), "offboard": (0.75, 0.80), "card": (1.0, 1.05)}.get(payment_mode, (1.0, 1.0))
    lane_mult = {"local_street": (1.0, 1.0), "arterial": (1.06, 1.12), "highway_shoulder": (1.10, 1.18)}.get(lane_context, (1.0, 1.0))
    inter_mult = {"midblock": (1.0, 1.0), "near_signal": (1.10, 1.18), "near_roundabout": (1.05, 1.10)}.get(intersection_context, (1.0, 1.0))

    ag = abs(float(grade_pct))
    terrain_off = 1.0 + (0.03 if ag >= 2.0 else 0.0) + (0.04 if ag >= 5.0 else 0.0)
    terrain_peak = 1.0 + (0.04 if ag >= 2.0 else 0.0) + (0.06 if ag >= 5.0 else 0.0)
    if float(grade_pct) > 0:
        terrain_off += 0.02
        terrain_peak += 0.03

    dir_off = 1.0
    dir_peak = 1.0
    if direction_bias == "dir0_heavier" and int(direction_id) == 0:
        dir_peak = 1.08
    elif direction_bias == "dir1_heavier" and int(direction_id) == 1:
        dir_peak = 1.08

    off = float(base_off_secs) * role_mult[0] * land_mult[0] * payment_mult[0] * lane_mult[0] * inter_mult[0] * terrain_off * dir_off
    peak = float(base_peak_secs) * role_mult[1] * land_mult[1] * payment_mult[1] * lane_mult[1] * inter_mult[1] * terrain_peak * dir_peak
    # keep values bounded for stability in generated schedules
    off = max(6.0, min(off, 240.0 if role == "terminal" else 120.0))
    peak = max(8.0, min(peak, 300.0 if role == "terminal" else 160.0))

    return {
        "stop_role": role,
        "land_use_proxy": land_use,
        "payment_mode": payment_mode,
        "lane_context": lane_context,
        "intersection_context": intersection_context,
        "direction_bias": direction_bias,
        "peak_window_profile": peak_window_profile,
        "offpeak_secs": float(round(off, 2)),
        "peak_secs": float(round(peak, 2)),
        "factors": {
            "role": {"offpeak": float(round(role_mult[0], 4)), "peak": float(round(role_mult[1], 4))},
            "land_use": {"offpeak": float(round(land_mult[0], 4)), "peak": float(round(land_mult[1], 4))},
            "payment_mode": {"offpeak": float(round(payment_mult[0], 4)), "peak": float(round(payment_mult[1], 4))},
            "lane_context": {"offpeak": float(round(lane_mult[0], 4)), "peak": float(round(lane_mult[1], 4))},
            "intersection_context": {"offpeak": float(round(inter_mult[0], 4)), "peak": float(round(inter_mult[1], 4))},
            "terrain": {"offpeak": float(round(terrain_off, 4)), "peak": float(round(terrain_peak, 4))},
            "direction_bias": {"offpeak": float(round(dir_off, 4)), "peak": float(round(dir_peak, 4))},
        },
    }


def _runtime_catalog_defaults_v1() -> List[Dict[str, Any]]:
    """
    Quito/Valle priors (catalog v1). Values are editable from Runtime Lab UI.
    """
    rows: List[Dict[str, Any]] = []

    def add(catalog_key: str, item_code: str, item_name: str, payload: Dict[str, Any], source: str = "seed_sample_v1") -> None:
        rows.append(
            {
                "catalog_key": str(catalog_key),
                "item_code": str(item_code),
                "item_name": str(item_name),
                "payload": dict(payload),
                "is_active": True,
                "source": str(source),
            }
        )

    # 1) speed_catalog (km/h, moving speed between stops)
    add("speed_catalog", "urban_core_local", "Urban core local", {"offpeak_kmh": 21.0, "peak_kmh": 15.0, "offpeak_range_kmh": [18, 24], "peak_range_kmh": [12, 18]})
    add("speed_catalog", "urban_core_arterial", "Urban core arterial", {"offpeak_kmh": 28.0, "peak_kmh": 19.0, "offpeak_range_kmh": [24, 32], "peak_range_kmh": [16, 22]})
    add("speed_catalog", "urban_exclusive_lane", "Urban exclusive lane", {"offpeak_kmh": 32.0, "peak_kmh": 25.0, "offpeak_range_kmh": [28, 36], "peak_range_kmh": [22, 28]})
    add("speed_catalog", "suburban_valle_local", "Suburban valle local", {"offpeak_kmh": 28.0, "peak_kmh": 19.0, "offpeak_range_kmh": [24, 32], "peak_range_kmh": [16, 22]})
    add("speed_catalog", "suburban_valle_arterial", "Suburban valle arterial", {"offpeak_kmh": 35.0, "peak_kmh": 24.0, "offpeak_range_kmh": [30, 40], "peak_range_kmh": [20, 28]})
    add("speed_catalog", "periurban_arterial", "Periurban arterial", {"offpeak_kmh": 44.0, "peak_kmh": 31.0, "offpeak_range_kmh": [38, 50], "peak_range_kmh": [26, 36]})
    add("speed_catalog", "perimetral_highway", "Perimetral/highway", {"offpeak_kmh": 52.0, "peak_kmh": 39.0, "offpeak_range_kmh": [45, 60], "peak_range_kmh": [32, 45]})

    # 2) dwell_catalog (seconds per stop)
    add("dwell_catalog", "low", "Low demand stop", {"offpeak_secs": 14.0, "peak_secs": 20.0, "offpeak_range_secs": [10, 18], "peak_range_secs": [15, 25]})
    add("dwell_catalog", "medium", "Medium demand stop", {"offpeak_secs": 24.0, "peak_secs": 35.0, "offpeak_range_secs": [18, 30], "peak_range_secs": [25, 45]})
    add("dwell_catalog", "high", "High demand stop", {"offpeak_secs": 42.0, "peak_secs": 62.0, "offpeak_range_secs": [30, 55], "peak_range_secs": [45, 80]})
    add("dwell_catalog", "terminal", "Terminal stop", {"offpeak_secs": 100.0, "peak_secs": 150.0, "offpeak_range_secs": [60, 150], "peak_range_secs": [90, 210]})

    # 3) intersection_delay_catalog (seconds)
    add(
        "intersection_delay_catalog",
        "urban_signalized",
        "Urban signalized",
        {
            "signals_per_km": 4.0,
            "roundabouts_per_km": 0.2,
            "offpeak_signal_delay_secs": 22.0,
            "peak_signal_delay_secs": 45.0,
            "offpeak_roundabout_delay_secs": 8.0,
            "peak_roundabout_delay_secs": 15.0,
            "offpeak_turn_extra_secs": 7.0,
            "peak_turn_extra_secs": 14.0,
        },
    )
    add(
        "intersection_delay_catalog",
        "suburban_mixed",
        "Suburban mixed",
        {
            "signals_per_km": 2.5,
            "roundabouts_per_km": 0.3,
            "offpeak_signal_delay_secs": 18.0,
            "peak_signal_delay_secs": 35.0,
            "offpeak_roundabout_delay_secs": 7.0,
            "peak_roundabout_delay_secs": 12.0,
            "offpeak_turn_extra_secs": 5.0,
            "peak_turn_extra_secs": 10.0,
        },
    )
    add(
        "intersection_delay_catalog",
        "periurban_light",
        "Periurban light control",
        {
            "signals_per_km": 1.0,
            "roundabouts_per_km": 0.1,
            "offpeak_signal_delay_secs": 14.0,
            "peak_signal_delay_secs": 24.0,
            "offpeak_roundabout_delay_secs": 6.0,
            "peak_roundabout_delay_secs": 10.0,
            "offpeak_turn_extra_secs": 3.0,
            "peak_turn_extra_secs": 7.0,
        },
    )

    # 4) slope_penalty_catalog (multipliers)
    add("slope_penalty_catalog", "grade_0_2", "0-2%", {"uphill_mult": 1.00, "downhill_mult": 1.00, "grade_min_pct": 0.0, "grade_max_pct": 2.0})
    add("slope_penalty_catalog", "grade_2_5", "2-5%", {"uphill_mult": 1.06, "downhill_mult": 0.98, "grade_min_pct": 2.0, "grade_max_pct": 5.0})
    add("slope_penalty_catalog", "grade_5_8", "5-8%", {"uphill_mult": 1.15, "downhill_mult": 0.95, "grade_min_pct": 5.0, "grade_max_pct": 8.0})
    add("slope_penalty_catalog", "grade_8_12", "8-12%", {"uphill_mult": 1.30, "downhill_mult": 0.92, "grade_min_pct": 8.0, "grade_max_pct": 12.0})
    add("slope_penalty_catalog", "grade_gt_12", ">12%", {"uphill_mult": 1.50, "downhill_mult": 0.90, "grade_min_pct": 12.0, "grade_max_pct": 99.0})

    # 5) peak_penalty_catalog
    add("peak_penalty_catalog", "urban_core", "Urban core peak profile", {"am_peak_mult": 1.55, "pm_peak_mult": 1.55, "shoulder_mult": 1.22})
    add("peak_penalty_catalog", "suburban_valle", "Suburban valle peak profile", {"am_peak_mult": 1.35, "pm_peak_mult": 1.35, "shoulder_mult": 1.15})
    add("peak_penalty_catalog", "periurban", "Periurban peak profile", {"am_peak_mult": 1.22, "pm_peak_mult": 1.22, "shoulder_mult": 1.10})
    add("peak_penalty_catalog", "highway", "Highway/perimetral peak profile", {"am_peak_mult": 1.15, "pm_peak_mult": 1.15, "shoulder_mult": 1.05})

    # 6) area_profile_catalog
    add(
        "area_profile_catalog",
        "urban_core",
        "Urban core",
        {
            "stop_spacing_min_m": 250.0,
            "stop_spacing_max_m": 400.0,
            "default_speed_profile": "urban_core_local",
            "default_dwell_profile": "high",
            "default_intersection_profile": "urban_signalized",
            "default_peak_profile": "urban_core",
        },
    )
    add(
        "area_profile_catalog",
        "suburban_valle",
        "Suburban valle",
        {
            "stop_spacing_min_m": 400.0,
            "stop_spacing_max_m": 700.0,
            "default_speed_profile": "suburban_valle_local",
            "default_dwell_profile": "medium",
            "default_intersection_profile": "suburban_mixed",
            "default_peak_profile": "suburban_valle",
        },
    )
    add(
        "area_profile_catalog",
        "periurban_valle",
        "Periurban valle",
        {
            "stop_spacing_min_m": 700.0,
            "stop_spacing_max_m": 1200.0,
            "default_speed_profile": "periurban_arterial",
            "default_dwell_profile": "low",
            "default_intersection_profile": "periurban_light",
            "default_peak_profile": "periurban",
        },
    )
    add(
        "area_profile_catalog",
        "express_intervalle",
        "Express/inter-valle",
        {
            "stop_spacing_min_m": 1200.0,
            "stop_spacing_max_m": 2500.0,
            "default_speed_profile": "perimetral_highway",
            "default_dwell_profile": "low",
            "default_intersection_profile": "periurban_light",
            "default_peak_profile": "highway",
        },
    )

    # 7) confidence_rules_catalog
    add(
        "confidence_rules_catalog",
        "default_v1",
        "Confidence rules v1",
        {
            "base": 0.50,
            "plus_gtfs_observed": 0.25,
            "plus_stop_sequence": 0.10,
            "plus_osm_intersections": 0.10,
            "plus_slope_data": 0.05,
            "minus_missing_stops": -0.20,
            "minus_missing_road_class": -0.10,
            "minus_missing_time_window": -0.10,
            "min": 0.05,
            "max": 0.99,
        },
    )

    # Optional route-level priors from public corridor values (commercial speeds)
    add("route_prior_catalog", "EXP_01", "ExpreAntisana - San Alfonso", {"aliases": ["EXP-01", "EXP 01", "EXPREANTISANA", "SAN ALFONSO"], "commercial_kmh": 27.4})
    add("route_prior_catalog", "LIB_01", "San Pedro de Taboada", {"aliases": ["LIB-01", "SAN PEDRO DE TABOADA"], "commercial_kmh": 21.4})
    add("route_prior_catalog", "LIB_02", "La Salle - Amaguaña", {"aliases": ["LIB-02", "LA SALLE", "AMAGUA"], "commercial_kmh": 21.45})
    add("route_prior_catalog", "VIN_01", "Vingala - Selva Alegre", {"aliases": ["VIN-01", "VINGALA", "SELVA ALEGRE"], "commercial_kmh": 20.85})
    add("route_prior_catalog", "LIB_04", "Dean Bajo - Armenia", {"aliases": ["LIB-04", "DEAN BAJO", "ARMENIA"], "commercial_kmh": 21.43})
    add("route_prior_catalog", "TTU_01", "La Merced via El Tingo", {"aliases": ["TTU-01", "LA MERCED", "EL TINGO"], "commercial_kmh": 22.08})
    add("route_prior_catalog", "TTU_02", "Club El Nacional - La Merced", {"aliases": ["TTU-02", "CLUB EL NACIONAL", "LA MERCED"], "commercial_kmh": 29.86})
    add("route_prior_catalog", "ONTANEDA_INTERNA", "Ontaneda interna", {"aliases": ["LIB-05", "ONTANEDA"], "commercial_kmh": 15.39})

    return rows


def _point_to_polyline_vertex_dist_m(lon: float, lat: float, path: List[List[float]]) -> float:
    if not path:
        return 1e9
    best = 1e18
    for p in path:
        d = _haversine_m(lon, lat, float(p[0]), float(p[1]))
        if d < best:
            best = d
    return float(best)


def _nearest_vertex_index(path: List[List[float]], lon: float, lat: float) -> int:
    if not path:
        return 0
    best_i = 0
    best_d = 1e18
    for i, p in enumerate(path):
        d = _haversine_m(lon, lat, float(p[0]), float(p[1]))
        if d < best_d:
            best_d = d
            best_i = i
    return int(best_i)


def _clip_to_terminal_stops(path: List[List[float]], start_stop: List[float], end_stop: List[float]) -> List[List[float]]:
    """
    Keep only the path segment between nearest start/end stop vertices and
    force first/last point to be exact stop coordinates.
    """
    if len(path) < 2:
        return path
    s_lon, s_lat = float(start_stop[0]), float(start_stop[1])
    e_lon, e_lat = float(end_stop[0]), float(end_stop[1])
    i0 = _nearest_vertex_index(path, s_lon, s_lat)
    i1 = _nearest_vertex_index(path, e_lon, e_lat)

    if i0 <= i1:
        seg = [list(p) for p in path[i0 : i1 + 1]]
    else:
        # Valhalla sometimes returns opposite orientation; normalize to start->end.
        seg = [list(p) for p in reversed(path[i1 : i0 + 1])]

    if len(seg) < 2:
        seg = [list(start_stop), list(end_stop)]
    else:
        seg[0] = [s_lon, s_lat]
        seg[-1] = [e_lon, e_lat]
    return seg


@st.cache_resource
def _get_phase5_client(_version: str = "v2"):
    return Phase5Client()


class Phase5Client:
    def __init__(self) -> None:
        self._schema_ready = False

    def _dsn(self) -> str:
        local_only = os.getenv("DATAMIND_LOCAL_ONLY_MODE", "").strip().lower() in {
            "1", "true", "t", "yes", "y", "on"
        }
        if local_only:
            dsn = os.getenv("LOCAL_DB_DSN") or os.getenv("DATAMIND_LOCAL_DB_DSN") or os.getenv("DB_DSN_LOCAL")
            if not dsn:
                raise RuntimeError(
                    "Phase 5 data is unavailable: configure LOCAL_DB_DSN for the local-only session. "
                    "Remote database fallbacks are disabled."
                )
            return dsn
        dsn = os.getenv("DB_DSN") or os.getenv("DATABASE_URL") or os.getenv("DB_DSN_PHASE5")
        if not dsn and os.getenv("SUPABASE_DB_HOST"):
            host = os.getenv("SUPABASE_DB_HOST")
            port = os.getenv("SUPABASE_DB_PORT", "5432")
            name = os.getenv("SUPABASE_DB_NAME", "postgres")
            user = os.getenv("SUPABASE_DB_USER", "postgres")
            password = os.getenv("SUPABASE_DB_PASSWORD", "")
            dsn = (
                f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
                f"@{host}:{port}/{name}?sslmode=require"
            )
        if not dsn:
            raise RuntimeError(
                "DB_DSN is not set. Configure DB_DSN/DATABASE_URL/DB_DSN_PHASE5 "
                "or SUPABASE_DB_*."
            )
        return dsn

    @contextmanager
    def _conn(self):
        conn = psycopg2.connect(self._dsn(), cursor_factory=RealDictCursor)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _phase5_root(self) -> Path:
        return Path(__file__).resolve().parents[3] / "phase5_gtfs"

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            try:
                with self._conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT 1
                            FROM information_schema.tables
                            WHERE table_schema = 'gtfs_work'
                              AND table_name = 'route_runtime_estimate_bindings'
                            LIMIT 1
                            """
                        )
                        if cur.fetchone():
                            return
            except Exception:
                pass
        run_all_migrations(self._phase5_root() / "db" / "migrations")
        self._schema_ready = True

    def _column_exists(self, schema: str, table: str, column: str) -> bool:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = %s
                      AND table_name = %s
                      AND column_name = %s
                    LIMIT 1
                    """,
                    (schema, table, column),
                )
                return bool(cur.fetchone())

    def _routes_has_service_route_id(self) -> bool:
        try:
            return bool(self._column_exists("route_prod", "routes", "service_route_id"))
        except Exception:
            return False

    def _resolve_gtfs_route_id(self, route_id: Optional[str]) -> str:
        rid = str(route_id or "").strip()
        if not rid:
            return ""
        if not self._routes_has_service_route_id():
            return rid
        with self._conn() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """
                        SELECT COALESCE(service_route_id::text, route_id::text) AS gtfs_route_id
                        FROM route_prod.routes
                        WHERE route_id::text = %s
                        LIMIT 1
                        """,
                        (rid,),
                    )
                    row = dict(cur.fetchone() or {})
                    return str(row.get("gtfs_route_id") or rid)
                except Exception:
                    return rid

    # ----------------------------------------
    # Inputs / Authoring
    # ----------------------------------------
    def list_route_inputs(self, *, verified_only: bool = True, limit: int = 300) -> List[Dict[str, Any]]:
        where = "WHERE human_verified = true" if verified_only else ""
        sql = f"""
        SELECT *
        FROM gtfs_work.v_route_inputs
        {where}
        ORDER BY naming_confidence DESC, created_at DESC
        LIMIT %s
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (int(limit),))
                return list(cur.fetchall() or [])

    def list_service_direction_inputs(
        self,
        *,
        verified_only: bool = True,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """
        Phase 5 context rows keyed by service_route_id + direction_id.
        route_id is resolved from Phase 3/4 output in route_prod.routes.
        """
        has_service_route_id = self._routes_has_service_route_id()
        service_route_expr = "r.service_route_id::text" if has_service_route_id else "r.route_id::text"
        where = [
            "COALESCE(r.direction_id, 0) IN (0, 1)",
        ]
        if has_service_route_id:
            where.append("r.service_route_id IS NOT NULL")
        if verified_only:
            # Keep Phase 5 inputs aligned with Phase 3/4 approved outputs.
            where.append("COALESCE(r.human_verified, false) = true")
            where.append("COALESCE(rs.human_verified, true) = true")

        sql = f"""
        SELECT
          {service_route_expr} AS service_route_id,
          COALESCE(r.direction_id, 0)::int AS direction_id,
          r.route_id::text AS route_id,
          COALESCE(rs.route_name, v.route_name, r.route_name, 'route_' || left(r.route_id::text, 8))::text AS route_name,
          COALESCE(rs.route_ref, v.route_ref, '')::text AS route_ref,
          COALESCE(rs.operator_name, v.operator_name, '')::text AS operator_name,
          COALESCE(v.n_stops, cardinality(r.stop_node_ids), 0)::int AS n_stops,
          COALESCE(r.human_verified, false) AS geom_verified,
          COALESCE(rs.human_verified, v.human_verified, false) AS naming_verified,
          COALESCE(rs.naming_confidence, v.naming_confidence, r.naming_confidence, 0.0)::float8 AS naming_confidence,
          r.created_at,
          r.updated_at
        FROM route_prod.routes r
        LEFT JOIN route_prod.route_semantics rs
          ON rs.route_id = r.route_id
        LEFT JOIN gtfs_work.v_route_inputs v
          ON v.route_id = r.route_id
        WHERE {" AND ".join(where)}
        ORDER BY r.updated_at DESC NULLS LAST, r.created_at DESC NULLS LAST, r.route_id
        LIMIT %s
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(sql, (int(limit),))
                    return list(cur.fetchall() or [])
                except Exception as e:
                    # Some DBs may still miss route_prod.routes.service_route_id even if cached checks said yes.
                    # Retry with route_id as the logical key so UI stays usable.
                    if "service_route_id" not in str(e).lower():
                        raise
                    fallback_sql = sql.replace(service_route_expr, "r.route_id::text")
                    cur.execute(fallback_sql, (int(limit),))
                    return list(cur.fetchall() or [])

    # ----------------------------------------
    # Runtime lab catalogs + estimator
    # ----------------------------------------
    def list_runtime_catalog_items(
        self,
        *,
        catalog_key: Optional[str] = None,
        active_only: bool = False,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        self._ensure_schema()
        where: List[str] = []
        params: List[Any] = []
        if catalog_key:
            where.append("catalog_key = %s")
            params.append(str(catalog_key))
        if active_only:
            where.append("is_active = true")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        sql = f"""
        SELECT catalog_key, item_code, item_name, payload, is_active, source, created_at, updated_at
        FROM gtfs_work.runtime_catalog_items
        {where_sql}
        ORDER BY catalog_key, item_code
        LIMIT %s
        """
        params.append(int(limit))
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return [dict(r) for r in (cur.fetchall() or [])]

    def upsert_runtime_catalog_item(
        self,
        *,
        catalog_key: str,
        item_code: str,
        item_name: str,
        payload: Dict[str, Any],
        is_active: bool = True,
        source: str = "manual",
    ) -> Dict[str, Any]:
        self._ensure_schema()
        ckey = str(catalog_key or "").strip()
        icode = str(item_code or "").strip()
        iname = str(item_name or "").strip() or icode
        if not ckey or not icode:
            raise RuntimeError("catalog_key and item_code are required")
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO gtfs_work.runtime_catalog_items (
                      catalog_key, item_code, item_name, payload, is_active, source, updated_at
                    ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, now())
                    ON CONFLICT (catalog_key, item_code) DO UPDATE SET
                      item_name = EXCLUDED.item_name,
                      payload = EXCLUDED.payload,
                      is_active = EXCLUDED.is_active,
                      source = EXCLUDED.source,
                      updated_at = now()
                    RETURNING catalog_key, item_code, item_name, payload, is_active, source, created_at, updated_at
                    """,
                    (ckey, icode, iname, psycopg2.extras.Json(payload or {}), bool(is_active), str(source or "manual")),
                )
                return dict(cur.fetchone() or {})

    def delete_runtime_catalog_item(self, *, catalog_key: str, item_code: str) -> int:
        self._ensure_schema()
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM gtfs_work.runtime_catalog_items WHERE catalog_key = %s AND item_code = %s",
                    (str(catalog_key), str(item_code)),
                )
                return int(cur.rowcount or 0)

    def seed_runtime_catalog_defaults(self, *, replace_existing: bool = False) -> Dict[str, Any]:
        self._ensure_schema()
        rows = _runtime_catalog_defaults_v1()
        inserted = 0
        updated = 0
        with self._conn() as conn:
            with conn.cursor() as cur:
                for r in rows:
                    if replace_existing:
                        cur.execute(
                            """
                            INSERT INTO gtfs_work.runtime_catalog_items (
                              catalog_key, item_code, item_name, payload, is_active, source, updated_at
                            ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, now())
                            ON CONFLICT (catalog_key, item_code) DO UPDATE SET
                              item_name = EXCLUDED.item_name,
                              payload = EXCLUDED.payload,
                              is_active = EXCLUDED.is_active,
                              source = EXCLUDED.source,
                              updated_at = now()
                            """,
                            (
                                str(r.get("catalog_key")),
                                str(r.get("item_code")),
                                str(r.get("item_name")),
                                psycopg2.extras.Json(r.get("payload") or {}),
                                bool(r.get("is_active", True)),
                                str(r.get("source") or "seed_sample_v1"),
                            ),
                        )
                        updated += 1
                    else:
                        cur.execute(
                            """
                            INSERT INTO gtfs_work.runtime_catalog_items (
                              catalog_key, item_code, item_name, payload, is_active, source, updated_at
                            ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, now())
                            ON CONFLICT (catalog_key, item_code) DO NOTHING
                            """,
                            (
                                str(r.get("catalog_key")),
                                str(r.get("item_code")),
                                str(r.get("item_name")),
                                psycopg2.extras.Json(r.get("payload") or {}),
                                bool(r.get("is_active", True)),
                                str(r.get("source") or "seed_sample_v1"),
                            ),
                        )
                        inserted += int(cur.rowcount or 0)
        return {"ok": True, "inserted": int(inserted), "updated": int(updated), "total_seed_rows": int(len(rows))}

    def estimate_runtime_from_catalog(
        self,
        *,
        route_id: str,
        direction_id: int,
        area_profile_code: str = "auto",
        speed_profile_code: Optional[str] = None,
        dwell_profile_code: Optional[str] = None,
        intersection_profile_code: Optional[str] = None,
        peak_profile_code: Optional[str] = None,
        confidence_profile_code: str = "default_v1",
        use_external_elevation: bool = True,
        use_external_signals: bool = False,
        persist_snapshot: bool = True,
        mode_strategy: str = "single_mode",
        section_min_score: float = 0.58,
        allow_haversine_fallback: bool = False,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        rid = str(route_id or "").strip()
        did = int(direction_id or 0)
        if not rid:
            raise RuntimeError("route_id is required")

        payload = self.get_route_map_payload(rid)
        stops = [dict(x) for x in (payload.get("stops") or [])]
        geo = payload.get("route_geojson") or {}
        path = _geojson_to_lonlat_path(geo)

        # Distances / legs must come from shape projection only (no stop-to-stop haversine fallback).
        path_cum = _path_cumulative_m(path) if len(path) >= 2 else []
        if len(path_cum) < 2:
            raise RuntimeError("Shape points not available for this route_id (missing/invalid route geometry path).")
        stop_dists: List[Optional[float]] = []
        if len(path_cum) >= 2 and len(stops) >= 2:
            for s in stops:
                lon = float(s.get("lon") or 0.0)
                lat = float(s.get("lat") or 0.0)
                stop_dists.append(_project_point_to_path_dist_m(path, path_cum, lon=lon, lat=lat))
            # enforce non-decreasing along stop order to avoid inversions
            last_val: Optional[float] = None
            for i in range(len(stop_dists)):
                d = stop_dists[i]
                if d is None:
                    continue
                if last_val is not None and d < last_val:
                    d = last_val
                    stop_dists[i] = d
                last_val = d

        leg_preview: List[Dict[str, Any]] = []
        unavailable_legs: List[str] = []
        fallback_legs: List[str] = []
        clamped_legs: List[str] = []
        fallback_reason: List[str] = []
        if len(stops) >= 2:
            for i in range(1, len(stops)):
                a = stops[i - 1]
                b = stops[i]
                d_leg: Optional[float] = None
                source = "shape_unavailable"
                if len(stop_dists) == len(stops):
                    da = stop_dists[i - 1]
                    db = stop_dists[i]
                    if da is not None and db is not None:
                        d_shape = float(db - da)
                        if d_shape >= 1.0:
                            d_leg = d_shape
                            source = "shape_projected"
                        elif d_shape >= 0.0:
                            d_leg = 1.0
                            source = "shape_projected_clamped"
                            clamped_legs.append(f"{int(a.get('seq') or i)}->{int(b.get('seq') or (i + 1))}")
                if d_leg is None and allow_haversine_fallback:
                    try:
                        lon1, lat1 = float(a.get("lon") or 0.0), float(a.get("lat") or 0.0)
                        lon2, lat2 = float(b.get("lon") or 0.0), float(b.get("lat") or 0.0)
                        d_leg = max(1.0, _haversine_m(lon1, lat1, lon2, lat2))
                        source = "haversine_fallback"
                        fallback_legs.append(f"{int(a.get('seq') or i)}->{int(b.get('seq') or (i + 1))}")
                    except Exception:
                        d_leg = None
                if d_leg is None:
                    unavailable_legs.append(f"{int(a.get('seq') or i)}->{int(b.get('seq') or (i + 1))}")
                leg_preview.append(
                    {
                        "leg_idx": int(i),
                        "from_seq": int(a.get("seq") or i),
                        "to_seq": int(b.get("seq") or (i + 1)),
                        "distance_m": (float(round(d_leg, 2)) if d_leg is not None else None),
                        "distance_source": source,
                    }
                )
        if unavailable_legs:
            preview = ", ".join(unavailable_legs[:8])
            extra = "" if len(unavailable_legs) <= 8 else f" (+{len(unavailable_legs) - 8} more)"
            raise RuntimeError(
                "Shape points not available for one or more stop legs; cannot use haversine fallback. "
                f"Unavailable legs: {preview}{extra}"
            )
        if fallback_legs:
            fallback_reason.append("haversine_fallback_used")
        if clamped_legs:
            fallback_reason.append("shape_projection_clamped")

        n_stops = int(len(stops))
        n_legs = int(max(1, len(leg_preview) if leg_preview else n_stops - 1))
        route_len_m = float(sum(_safe_num(x.get("distance_m"), 0.0) for x in leg_preview)) if leg_preview else _polyline_length_m(path)
        stop_spacing_m = float(route_len_m / n_legs) if n_legs > 0 else 0.0
        route_km = float(route_len_m / 1000.0)
        route_curve_ratio = 1.0
        if len(path) >= 2:
            straight_m = _haversine_m(float(path[0][0]), float(path[0][1]), float(path[-1][0]), float(path[-1][1]))
            if straight_m > 1.0:
                route_curve_ratio = float(route_len_m / straight_m)
        mode_strategy = str(mode_strategy or "single_mode").strip().lower()
        if mode_strategy not in ("single_mode", "mixed_sections"):
            mode_strategy = "single_mode"
        section_min_score = max(0.0, min(1.0, float(section_min_score or 0.0)))

        rows = self.list_runtime_catalog_items(active_only=True, limit=5000)
        by_key: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for r in rows:
            ck = str(r.get("catalog_key") or "")
            code = str(r.get("item_code") or "")
            by_key.setdefault(ck, {})[code] = dict(r)

        area_items = by_key.get("area_profile_catalog", {})
        if not area_items:
            raise RuntimeError("Runtime Lab catalogs are empty. Seed defaults first.")

        chosen_area = str(area_profile_code or "auto").strip() or "auto"
        if chosen_area == "auto":
            for code, row in area_items.items():
                p = dict(row.get("payload") or {})
                mn = _safe_num(p.get("stop_spacing_min_m"), 0.0)
                mx = _safe_num(p.get("stop_spacing_max_m"), 99999.0)
                if stop_spacing_m >= mn and stop_spacing_m <= mx:
                    chosen_area = str(code)
                    break
            if chosen_area == "auto":
                chosen_area = "suburban_valle" if "suburban_valle" in area_items else next(iter(area_items.keys()))

        area_row = dict(area_items.get(chosen_area) or {})
        area_payload = dict(area_row.get("payload") or {})
        if not area_row:
            raise RuntimeError(f"Area profile not found: {chosen_area}")

        speed_code = str(speed_profile_code or area_payload.get("default_speed_profile") or "").strip()
        dwell_code = str(dwell_profile_code or area_payload.get("default_dwell_profile") or "").strip()
        inter_code = str(intersection_profile_code or area_payload.get("default_intersection_profile") or "").strip()
        peak_code = str(peak_profile_code or area_payload.get("default_peak_profile") or "").strip()
        conf_code = str(confidence_profile_code or "default_v1").strip()

        speed_row = dict(by_key.get("speed_catalog", {}).get(speed_code) or {})
        dwell_row = dict(by_key.get("dwell_catalog", {}).get(dwell_code) or {})
        inter_row = dict(by_key.get("intersection_delay_catalog", {}).get(inter_code) or {})
        peak_row = dict(by_key.get("peak_penalty_catalog", {}).get(peak_code) or {})
        conf_row = dict(by_key.get("confidence_rules_catalog", {}).get(conf_code) or {})
        if not speed_row or not dwell_row or not inter_row:
            raise RuntimeError("Missing speed/dwell/intersection profiles in Runtime Lab catalogs.")

        area_payload_by_code: Dict[str, Dict[str, Any]] = {
            str(r.get("item_code") or ""): dict(r.get("payload") or {})
            for r in area_items.values()
        }
        speed_payload_by_code: Dict[str, Dict[str, Any]] = {
            str(r.get("item_code") or ""): dict(r.get("payload") or {})
            for r in (by_key.get("speed_catalog", {}) or {}).values()
        }
        dwell_payload_by_code: Dict[str, Dict[str, Any]] = {
            str(r.get("item_code") or ""): dict(r.get("payload") or {})
            for r in (by_key.get("dwell_catalog", {}) or {}).values()
        }
        inter_payload_by_code: Dict[str, Dict[str, Any]] = {
            str(r.get("item_code") or ""): dict(r.get("payload") or {})
            for r in (by_key.get("intersection_delay_catalog", {}) or {}).values()
        }
        peak_payload_by_code: Dict[str, Dict[str, Any]] = {
            str(r.get("item_code") or ""): dict(r.get("payload") or {})
            for r in (by_key.get("peak_penalty_catalog", {}) or {}).values()
        }

        def _score_area_choice(area_code: str, *, spacing_m: float, signals_km: float, grade_abs: float, curve_ratio: float) -> float:
            ap = dict(area_payload_by_code.get(area_code) or {})
            mn = _safe_num(ap.get("stop_spacing_min_m"), 0.0)
            mx = _safe_num(ap.get("stop_spacing_max_m"), 999999.0)
            spacing_s = _range_score(spacing_m, mn, mx)
            ac = str(area_code or "").lower()
            if "urban" in ac:
                sig_s = _density_score(signals_km, 2.5, 6.5)
                slope_s = _density_score(grade_abs, 0.0, 3.0)
                curve_s = _density_score(curve_ratio, 1.0, 1.25)
            elif "suburban" in ac:
                sig_s = _density_score(signals_km, 1.5, 4.0)
                slope_s = _density_score(grade_abs, 1.0, 4.0)
                curve_s = _density_score(curve_ratio, 1.05, 1.45)
            elif "periurban" in ac:
                sig_s = _density_score(signals_km, 0.5, 2.2)
                slope_s = _density_score(grade_abs, 2.0, 6.5)
                curve_s = _density_score(curve_ratio, 1.15, 1.8)
            else:
                sig_s = _density_score(signals_km, 0.0, 1.6)
                slope_s = _density_score(grade_abs, 1.0, 5.5)
                curve_s = _density_score(curve_ratio, 1.0, 1.6)
            return float(0.58 * spacing_s + 0.16 * sig_s + 0.16 * slope_s + 0.10 * curve_s)

        def _area_defaults(area_code: str) -> Dict[str, str]:
            ap = dict(area_payload_by_code.get(area_code) or {})
            return {
                "speed_profile_code": str(ap.get("default_speed_profile") or speed_code),
                "dwell_profile_code": str(ap.get("default_dwell_profile") or dwell_code),
                "intersection_profile_code": str(ap.get("default_intersection_profile") or inter_code),
                "peak_profile_code": str(ap.get("default_peak_profile") or peak_code),
            }

        speed = dict(speed_row.get("payload") or {})
        dwell = dict(dwell_row.get("payload") or {})
        inter = dict(inter_row.get("payload") or {})
        peak = dict(peak_row.get("payload") or {})
        conf = dict(conf_row.get("payload") or {})

        # Route-name prior match (optional override)
        route_ref = str((payload.get("route_meta") or {}).get("route_ref") or "")
        route_name = str((payload.get("route_name") or "") or "")
        name_ctx = f"{route_ref} {route_name}".strip()
        prior_rows = by_key.get("route_prior_catalog", {})
        prior_match_code = ""
        prior_match_score = 0.0
        prior_kmh = 0.0
        for code, row in prior_rows.items():
            pp = dict(row.get("payload") or {})
            aliases = [str(x) for x in (pp.get("aliases") or []) if str(x).strip()]
            base_score = _similarity(_normalize_text(name_ctx), _normalize_text(str(row.get("item_name") or code)))
            alias_score = max([_similarity(_normalize_text(name_ctx), _normalize_text(a)) for a in aliases], default=0.0)
            score = max(base_score, alias_score)
            if score > prior_match_score:
                prior_match_score = float(score)
                prior_match_code = str(code)
                prior_kmh = _safe_num(pp.get("commercial_kmh"), 0.0)

        offpeak_kmh = _safe_num(speed.get("offpeak_kmh"), 20.0)
        peak_kmh = _safe_num(speed.get("peak_kmh"), 14.0)
        fallback_reason: List[str] = []
        if prior_match_score >= 0.72 and prior_kmh > 0.0:
            offpeak_kmh = float(prior_kmh)
            peak_kmh = max(8.0, float(prior_kmh * 0.80))
            fallback_reason.append(f"route_prior_match:{prior_match_code}:{prior_match_score:.3f}")

        dwell_off = _safe_num(dwell.get("offpeak_secs"), 20.0)
        dwell_peak = _safe_num(dwell.get("peak_secs"), 30.0)
        sig_per_km = _safe_num(inter.get("signals_per_km"), 2.0)
        rnd_per_km = _safe_num(inter.get("roundabouts_per_km"), 0.1)
        sig_off = _safe_num(inter.get("offpeak_signal_delay_secs"), 20.0)
        sig_peak = _safe_num(inter.get("peak_signal_delay_secs"), 35.0)
        rnd_off = _safe_num(inter.get("offpeak_roundabout_delay_secs"), 8.0)
        rnd_peak = _safe_num(inter.get("peak_roundabout_delay_secs"), 14.0)
        turn_off = _safe_num(inter.get("offpeak_turn_extra_secs"), 0.0)
        turn_peak = _safe_num(inter.get("peak_turn_extra_secs"), 0.0)
        peak_am_mult = _safe_num(peak.get("am_peak_mult"), 1.25)
        peak_pm_mult = _safe_num(peak.get("pm_peak_mult"), peak_am_mult)
        peak_mult = float((peak_am_mult + peak_pm_mult) / 2.0)

        # Elevation enrichment (stop-level) for grade/slope per leg.
        stop_elev_m: Dict[int, Optional[float]] = {}
        for i, s in enumerate(stops):
            stop_elev_m[i] = _parse_ele_m(s.get("ele_tag"))

        elevation_source = "node_tags"
        elevation_samples = sum(1 for v in stop_elev_m.values() if v is not None)
        if use_external_elevation and elevation_samples < max(2, len(stops) // 3):
            endpoint = str(os.getenv("OPEN_ELEVATION_URL") or "https://api.open-elevation.com")
            pts = []
            for i, s in enumerate(stops):
                pts.append({"idx": i, "lat": float(s.get("lat") or 0.0), "lon": float(s.get("lon") or 0.0)})
            elev_remote = _open_elevation_lookup(pts, endpoint=endpoint, timeout=28)
            for i in range(len(stops)):
                if stop_elev_m.get(i) is None and elev_remote.get(i) is not None:
                    stop_elev_m[i] = float(elev_remote[i])  # type: ignore[index]
            elevation_samples = sum(1 for v in stop_elev_m.values() if v is not None)
            if elevation_samples > 0:
                elevation_source = "open_elevation"
        if elevation_samples == 0:
            fallback_reason.append("missing_elevation")

        # Signal enrichment
        signal_payload = self.list_route_signal_points(
            route_id=rid,
            include_overpass=bool(use_external_signals),
            overpass_margin_m=120.0,
        )
        signal_points = [dict(x) for x in (signal_payload.get("points") or [])]
        signal_source = str(signal_payload.get("source_used") or "db")
        observed_signals_per_km = float(len(signal_points) / max(0.001, route_km)) if route_km > 0 else 0.0
        if observed_signals_per_km > 0.01:
            sig_per_km = max(sig_per_km, observed_signals_per_km)

        # slope bin lookup from catalog
        slope_bins: List[Dict[str, Any]] = []
        for code, row in (by_key.get("slope_penalty_catalog", {}) or {}).items():
            p = dict(row.get("payload") or {})
            slope_bins.append(
                {
                    "code": code,
                    "min": _safe_num(p.get("grade_min_pct"), 0.0),
                    "max": _safe_num(p.get("grade_max_pct"), 99.0),
                    "uphill_mult": _safe_num(p.get("uphill_mult"), 1.0),
                    "downhill_mult": _safe_num(p.get("downhill_mult"), 1.0),
                }
            )
        slope_bins.sort(key=lambda x: float(x.get("min") or 0.0))

        def _grade_mult(grade_pct: float) -> tuple[float, str]:
            ag = abs(float(grade_pct))
            chosen = None
            for b in slope_bins:
                if ag >= float(b["min"]) and ag <= float(b["max"]):
                    chosen = b
                    break
            if chosen is None and slope_bins:
                chosen = slope_bins[-1]
            if chosen is None:
                return (1.0, "none")
            if grade_pct >= 0.0:
                return (float(chosen["uphill_mult"]), str(chosen["code"]))
            return (float(chosen["downhill_mult"]), str(chosen["code"]))

        # Build signal projection along shape path for per-leg allocation.
        signal_dists: List[float] = []
        if len(path_cum) >= 2:
            for sp in signal_points:
                lon = float(sp.get("lon") or 0.0)
                lat = float(sp.get("lat") or 0.0)
                d = _project_point_to_path_dist_m(path, path_cum, lon=lon, lat=lat)
                if d is None:
                    try:
                        idx = _nearest_path_vertex_idx(path, lon=lon, lat=lat)
                        if 0 <= idx < len(path_cum):
                            d = float(path_cum[idx])
                    except Exception:
                        d = None
                if d is not None:
                    signal_dists.append(float(d))

        # Leg-by-leg timing model (shape-aware).
        leg_rows: List[Dict[str, Any]] = []
        runtime_off_f = 0.0
        runtime_peak_f = 0.0
        section_rows: List[Dict[str, Any]] = []
        stop_dwell_rows: List[Dict[str, Any]] = []

        def _params_from_area(area_code_use: str) -> Dict[str, float]:
            defaults = _area_defaults(area_code_use)
            spd = dict(speed_payload_by_code.get(defaults["speed_profile_code"]) or speed)
            dw = dict(dwell_payload_by_code.get(defaults["dwell_profile_code"]) or dwell)
            it = dict(inter_payload_by_code.get(defaults["intersection_profile_code"]) or inter)
            pk = dict(peak_payload_by_code.get(defaults["peak_profile_code"]) or peak)
            am = _safe_num(pk.get("am_peak_mult"), peak_am_mult)
            pm = _safe_num(pk.get("pm_peak_mult"), peak_pm_mult)
            return {
                "offpeak_kmh": _safe_num(spd.get("offpeak_kmh"), offpeak_kmh),
                "peak_kmh": _safe_num(spd.get("peak_kmh"), peak_kmh),
                "dwell_off": _safe_num(dw.get("offpeak_secs"), dwell_off),
                "dwell_peak": _safe_num(dw.get("peak_secs"), dwell_peak),
                "sig_per_km": _safe_num(it.get("signals_per_km"), sig_per_km),
                "rnd_per_km": _safe_num(it.get("roundabouts_per_km"), rnd_per_km),
                "sig_off": _safe_num(it.get("offpeak_signal_delay_secs"), sig_off),
                "sig_peak": _safe_num(it.get("peak_signal_delay_secs"), sig_peak),
                "rnd_off": _safe_num(it.get("offpeak_roundabout_delay_secs"), rnd_off),
                "rnd_peak": _safe_num(it.get("peak_roundabout_delay_secs"), rnd_peak),
                "turn_off": _safe_num(it.get("offpeak_turn_extra_secs"), turn_off),
                "turn_peak": _safe_num(it.get("peak_turn_extra_secs"), turn_peak),
                "peak_mult": float((am + pm) / 2.0),
            }

        for lg in leg_preview:
            leg_i = int(_safe_num(lg.get("leg_idx"), 1))
            from_i = max(0, leg_i - 1)
            to_i = min(len(stops) - 1, leg_i)
            d_leg_m = _safe_num(lg.get("distance_m"), 0.0)
            d_leg_km = d_leg_m / 1000.0
            e_from = stop_elev_m.get(from_i)
            e_to = stop_elev_m.get(to_i)
            grade_pct = 0.0
            if e_from is not None and e_to is not None and d_leg_m > 1.0:
                grade_pct = ((float(e_to) - float(e_from)) / float(d_leg_m)) * 100.0
            slope_mult_leg, slope_bin_code = _grade_mult(grade_pct)

            signal_count_leg = 0
            if len(stop_dists) == len(stops) and signal_dists:
                da = stop_dists[from_i]
                db = stop_dists[to_i]
                if da is not None and db is not None:
                    lo = min(float(da), float(db))
                    hi = max(float(da), float(db))
                    signal_count_leg = int(sum(1 for sd in signal_dists if sd >= lo and sd <= hi))
            if signal_count_leg == 0 and d_leg_km > 0:
                signal_count_leg = int(round(max(0.0, sig_per_km * d_leg_km)))

            area_code_leg = chosen_area
            area_score_leg = 1.0
            if mode_strategy == "mixed_sections" and area_payload_by_code:
                signals_leg_km = float(signal_count_leg / max(0.001, d_leg_km)) if d_leg_km > 0 else 0.0
                scored = []
                for ac in area_payload_by_code.keys():
                    sc = _score_area_choice(
                        ac,
                        spacing_m=d_leg_m,
                        signals_km=signals_leg_km,
                        grade_abs=abs(float(grade_pct)),
                        curve_ratio=route_curve_ratio,
                    )
                    scored.append((ac, sc))
                scored.sort(key=lambda x: float(x[1]), reverse=True)
                if scored:
                    area_code_leg = str(scored[0][0])
                    area_score_leg = float(scored[0][1])
                if area_score_leg < section_min_score:
                    area_code_leg = chosen_area
                    area_score_leg = max(area_score_leg, 0.01)

            pvals = _params_from_area(area_code_leg)
            moving_off_leg = d_leg_m / max(0.1, (pvals["offpeak_kmh"] * 1000.0 / 3600.0))
            moving_peak_leg = d_leg_m / max(0.1, (pvals["peak_kmh"] * 1000.0 / 3600.0))
            signal_delay_off = signal_count_leg * pvals["sig_off"]
            signal_delay_peak = signal_count_leg * pvals["sig_peak"]
            round_delay_off = d_leg_km * (pvals["rnd_per_km"] * pvals["rnd_off"])
            round_delay_peak = d_leg_km * (pvals["rnd_per_km"] * pvals["rnd_peak"])
            inter_off_leg = signal_delay_off + round_delay_off + (pvals["turn_off"] * 0.12)
            inter_peak_leg = signal_delay_peak + round_delay_peak + (pvals["turn_peak"] * 0.20)
            stop_to = dict(stops[to_i]) if 0 <= to_i < len(stops) else {}
            dwell_detail = _compute_stop_dwell_factorized(
                stop=stop_to,
                route_name=route_name,
                route_ref=route_ref,
                direction_id=int(did),
                stop_idx=int(to_i),
                n_stops=int(len(stops)),
                base_off_secs=float(pvals["dwell_off"]),
                base_peak_secs=float(pvals["dwell_peak"]),
                signal_count_leg=int(signal_count_leg),
                grade_pct=float(grade_pct),
            )
            dwell_off_leg = _safe_num(dwell_detail.get("offpeak_secs"), pvals["dwell_off"])
            dwell_peak_leg = _safe_num(dwell_detail.get("peak_secs"), pvals["dwell_peak"])
            travel_off_leg = (moving_off_leg + inter_off_leg) * slope_mult_leg
            travel_peak_leg = (moving_peak_leg + inter_peak_leg) * pvals["peak_mult"]
            leg_off = travel_off_leg + dwell_off_leg
            leg_peak = travel_peak_leg + dwell_peak_leg
            runtime_off_f += leg_off
            runtime_peak_f += leg_peak
            eff_off_kmh = (d_leg_m / max(1.0, leg_off)) * 3.6 if d_leg_m > 0 else 0.0
            eff_peak_kmh = (d_leg_m / max(1.0, leg_peak)) * 3.6 if d_leg_m > 0 else 0.0
            leg_rows.append(
                {
                    **lg,
                    "from_lat": float(stops[from_i].get("lat") or 0.0) if stops else None,
                    "from_lon": float(stops[from_i].get("lon") or 0.0) if stops else None,
                    "to_lat": float(stops[to_i].get("lat") or 0.0) if stops else None,
                    "to_lon": float(stops[to_i].get("lon") or 0.0) if stops else None,
                    "elev_from_m": (float(e_from) if e_from is not None else None),
                    "elev_to_m": (float(e_to) if e_to is not None else None),
                    "grade_pct": float(round(grade_pct, 4)),
                    "slope_bin": slope_bin_code,
                    "slope_mult": float(round(slope_mult_leg, 4)),
                    "signal_count": int(signal_count_leg),
                    "area_profile_code": area_code_leg,
                    "area_profile_score": float(round(area_score_leg, 4)),
                    "mode_strategy": mode_strategy,
                    "travel_offpeak_secs": float(round(travel_off_leg, 2)),
                    "travel_peak_secs": float(round(travel_peak_leg, 2)),
                    "dwell_offpeak_secs": float(round(dwell_off_leg, 2)),
                    "dwell_peak_secs": float(round(dwell_peak_leg, 2)),
                    "dwell_model": dwell_detail,
                    "offpeak_secs": float(round(leg_off, 2)),
                    "peak_secs": float(round(leg_peak, 2)),
                    "offpeak_kmh_effective": float(round(eff_off_kmh, 3)),
                    "peak_kmh_effective": float(round(eff_peak_kmh, 3)),
                }
            )
            stop_dwell_rows.append(
                {
                    "stop_seq": int(stop_to.get("seq") or (to_i + 1)),
                    "stop_id": str(stop_to.get("node_id") or ""),
                    "stop_name": str(stop_to.get("canonical_name") or stop_to.get("name") or ""),
                    "direction_id": int(did),
                    "grade_pct_inbound_leg": float(round(grade_pct, 4)),
                    "signal_count_inbound_leg": int(signal_count_leg),
                    **{k: v for k, v in dwell_detail.items() if k != "factors"},
                    "factors": dict(dwell_detail.get("factors") or {}),
                }
            )

        if leg_rows and mode_strategy == "mixed_sections" and len(leg_rows) >= 3:
            for i in range(1, len(leg_rows) - 1):
                left = str(leg_rows[i - 1].get("area_profile_code") or "")
                mid = str(leg_rows[i].get("area_profile_code") or "")
                right = str(leg_rows[i + 1].get("area_profile_code") or "")
                if left and left == right and mid != left:
                    if _safe_num(leg_rows[i].get("area_profile_score"), 0.0) < 0.86:
                        leg_rows[i]["area_profile_code"] = left
                        leg_rows[i]["area_profile_score"] = max(
                            _safe_num(leg_rows[i].get("area_profile_score"), 0.0),
                            min(
                                _safe_num(leg_rows[i - 1].get("area_profile_score"), 0.0),
                                _safe_num(leg_rows[i + 1].get("area_profile_score"), 0.0),
                            ),
                        )

        if leg_rows:
            cur = None
            for lg in leg_rows:
                ac = str(lg.get("area_profile_code") or chosen_area)
                if cur is None or str(cur.get("area_profile_code") or "") != ac:
                    if cur is not None:
                        section_rows.append(cur)
                    cur = {
                        "section_idx": int(len(section_rows) + 1),
                        "area_profile_code": ac,
                        "from_leg": int(_safe_num(lg.get("leg_idx"), 0)),
                        "to_leg": int(_safe_num(lg.get("leg_idx"), 0)),
                        "distance_m": float(_safe_num(lg.get("distance_m"), 0.0)),
                        "score_sum": float(_safe_num(lg.get("area_profile_score"), 0.0)),
                        "n_legs": 1,
                    }
                else:
                    cur["to_leg"] = int(_safe_num(lg.get("leg_idx"), 0))
                    cur["distance_m"] = float(cur["distance_m"] + _safe_num(lg.get("distance_m"), 0.0))
                    cur["score_sum"] = float(cur["score_sum"] + _safe_num(lg.get("area_profile_score"), 0.0))
                    cur["n_legs"] = int(cur["n_legs"] + 1)
            if cur is not None:
                section_rows.append(cur)
            for s in section_rows:
                s["avg_score"] = float(round(_safe_num(s.get("score_sum"), 0.0) / max(1, int(s.get("n_legs") or 1)), 4))
                s.pop("score_sum", None)
            if mode_strategy == "mixed_sections" and len(section_rows) >= 3:
                i = 1
                while i < len(section_rows) - 1:
                    s = section_rows[i]
                    if int(s.get("n_legs") or 0) <= 1:
                        p = section_rows[i - 1]
                        n = section_rows[i + 1]
                        merge_into_prev = _safe_num(p.get("avg_score"), 0.0) >= _safe_num(n.get("avg_score"), 0.0)
                        if merge_into_prev:
                            p["to_leg"] = int(s.get("to_leg") or p.get("to_leg") or 0)
                            p["n_legs"] = int(_safe_num(p.get("n_legs"), 0) + _safe_num(s.get("n_legs"), 0))
                            p["distance_m"] = float(_safe_num(p.get("distance_m"), 0.0) + _safe_num(s.get("distance_m"), 0.0))
                            p["avg_score"] = float(round((float(_safe_num(p.get("avg_score"), 0.0)) + float(_safe_num(s.get("avg_score"), 0.0))) / 2.0, 4))
                        else:
                            n["from_leg"] = int(s.get("from_leg") or n.get("from_leg") or 0)
                            n["n_legs"] = int(_safe_num(n.get("n_legs"), 0) + _safe_num(s.get("n_legs"), 0))
                            n["distance_m"] = float(_safe_num(n.get("distance_m"), 0.0) + _safe_num(s.get("distance_m"), 0.0))
                            n["avg_score"] = float(round((float(_safe_num(n.get("avg_score"), 0.0)) + float(_safe_num(s.get("avg_score"), 0.0))) / 2.0, 4))
                        section_rows.pop(i)
                        continue
                    i += 1
                merged: List[Dict[str, Any]] = []
                for s in section_rows:
                    if merged and str(merged[-1].get("area_profile_code") or "") == str(s.get("area_profile_code") or ""):
                        prev = merged[-1]
                        prev["to_leg"] = int(s.get("to_leg") or prev.get("to_leg") or 0)
                        prev["n_legs"] = int(_safe_num(prev.get("n_legs"), 0) + _safe_num(s.get("n_legs"), 0))
                        prev["distance_m"] = float(_safe_num(prev.get("distance_m"), 0.0) + _safe_num(s.get("distance_m"), 0.0))
                        prev["avg_score"] = float(round((float(_safe_num(prev.get("avg_score"), 0.0)) + float(_safe_num(s.get("avg_score"), 0.0))) / 2.0, 4))
                    else:
                        merged.append(dict(s))
                section_rows = merged
                for j, s in enumerate(section_rows, start=1):
                    s["section_idx"] = int(j)

        if not leg_rows:
            moving_off = route_len_m / max(0.1, (offpeak_kmh * 1000.0 / 3600.0))
            moving_peak = route_len_m / max(0.1, (peak_kmh * 1000.0 / 3600.0))
            inter_off = route_km * (sig_per_km * sig_off + rnd_per_km * rnd_off) + (n_legs * turn_off * 0.12)
            inter_peak = route_km * (sig_per_km * sig_peak + rnd_per_km * rnd_peak) + (n_legs * turn_peak * 0.20)
            dwell_total_off = n_stops * dwell_off
            dwell_total_peak = n_stops * dwell_peak
            runtime_off_f = (moving_off + inter_off + dwell_total_off) * 1.0
            runtime_peak_f = (moving_peak + inter_peak) * peak_mult + dwell_total_peak
            section_rows = [
                {
                    "section_idx": 1,
                    "area_profile_code": chosen_area,
                    "from_leg": 1,
                    "to_leg": int(max(1, n_legs)),
                    "distance_m": float(round(route_len_m, 2)),
                    "n_legs": int(max(1, n_legs)),
                    "avg_score": 1.0,
                }
            ]
        elif not section_rows:
            section_rows = [
                {
                    "section_idx": 1,
                    "area_profile_code": chosen_area,
                    "from_leg": 1,
                    "to_leg": int(max(1, n_legs)),
                    "distance_m": float(round(route_len_m, 2)),
                    "n_legs": int(max(1, n_legs)),
                    "avg_score": 1.0,
                }
            ]

        runtime_off = int(max(300, round(runtime_off_f)))
        runtime_peak = int(max(300, round(runtime_peak_f)))
        offpeak_min_per_leg = float(round(runtime_off / max(1, n_legs) / 60.0, 3))
        peak_min_per_leg = float(round(runtime_peak / max(1, n_legs) / 60.0, 3))

        # Derived headway suggestion (conservative defaults)
        offpeak_headway = int(max(180, min(1800, round((offpeak_min_per_leg * 4.0) * 60.0 / 30.0) * 30)))
        peak_headway = int(max(120, min(offpeak_headway, round((peak_min_per_leg * 3.5) * 60.0 / 30.0) * 30)))

        # Confidence scoring
        c_base = _safe_num(conf.get("base"), 0.50)
        c = c_base
        has_geo = len(path) >= 2
        has_stops = n_stops >= 2
        has_gtfs_prior = prior_match_score >= 0.72
        if has_gtfs_prior:
            c += _safe_num(conf.get("plus_gtfs_observed"), 0.25)
        if prior_match_score >= 0.50 and prior_kmh > 0.0:
            c += _safe_num(conf.get("plus_route_prior_match"), 0.15)
        if has_stops:
            c += _safe_num(conf.get("plus_stop_sequence"), 0.10)
        if has_geo:
            c += _safe_num(conf.get("plus_osm_intersections"), 0.10)
        if elevation_samples > 0:
            c += _safe_num(conf.get("plus_slope_data"), 0.05)
        if not has_stops:
            c += _safe_num(conf.get("minus_missing_stops"), -0.20)
            fallback_reason.append("missing_stop_sequence")
        if not has_geo:
            c += _safe_num(conf.get("minus_missing_road_class"), -0.10)
            fallback_reason.append("missing_geometry_path")
        c = max(_safe_num(conf.get("min"), 0.05), min(_safe_num(conf.get("max"), 0.99), c))

        terrain_density = float(sum(abs(_safe_num(x.get("grade_pct"), 0.0)) for x in leg_rows) / max(1, len(leg_rows))) if leg_rows else 0.0

        estimate_payload = {
            "route_id": rid,
            "direction_id": int(did),
            "service_route_id": str((payload.get("route_meta") or {}).get("service_route_id") or ""),
            "metrics": {
                "n_stops": int(n_stops),
                "n_legs": int(n_legs),
                "route_len_m": float(round(route_len_m, 2)),
                "stop_spacing_m": float(round(stop_spacing_m, 2)),
                "runtime_offpeak_secs": int(runtime_off),
                "runtime_peak_secs": int(runtime_peak),
                "offpeak_min_per_leg": float(offpeak_min_per_leg),
                "peak_min_per_leg": float(peak_min_per_leg),
                "offpeak_headway_secs": int(offpeak_headway),
                "peak_headway_secs": int(peak_headway),
                "terrain_density_score": float(round(terrain_density, 4)),
                "confidence": float(round(c, 4)),
            },
            "selected_catalogs": {
                "area_profile_code": chosen_area,
                "speed_profile_code": speed_code,
                "dwell_profile_code": dwell_code,
                "intersection_profile_code": inter_code,
                "peak_profile_code": peak_code,
                "confidence_profile_code": conf_code,
                "mode_strategy": mode_strategy,
                "section_min_score": float(round(section_min_score, 4)),
            },
            "model_inputs": {
                "offpeak_kmh": float(offpeak_kmh),
                "peak_kmh": float(peak_kmh),
                "dwell_offpeak_secs": float(dwell_off),
                "dwell_peak_secs": float(dwell_peak),
                "signals_per_km_catalog": float(_safe_num(inter.get("signals_per_km"), 0.0)),
                "signals_per_km_used": float(round(sig_per_km, 4)),
                "roundabouts_per_km": float(rnd_per_km),
                "peak_multiplier": float(round(peak_mult, 4)),
                "elevation_source": elevation_source,
                "elevation_samples": int(elevation_samples),
                "signal_source": signal_source,
                "signal_points": int(len(signal_points)),
                "route_curve_ratio": float(round(route_curve_ratio, 4)),
            },
            "route_prior_match": {
                "item_code": prior_match_code,
                "score": float(round(prior_match_score, 4)),
                "commercial_kmh": float(prior_kmh),
            },
            "fallback_reason": fallback_reason,
            "fallback_legs": fallback_legs[:80],
            "projection_clamped_legs": clamped_legs[:80],
            "signal_points": signal_points,
            "sections": section_rows[:120],
            "leg_preview": leg_rows[:300] if leg_rows else leg_preview[:300],
            "stop_dwell_preview": stop_dwell_rows[:300],
        }

        if persist_snapshot:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.runtime_route_estimates (
                          route_id, direction_id, area_profile_code, speed_profile_code, dwell_profile_code,
                          intersection_profile_code, peak_profile_code, confidence_profile_code,
                          metrics, model_inputs, fallback_reason, route_prior_match
                        ) VALUES (
                          %s::uuid,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb
                        )
                        RETURNING estimate_id::text
                        """,
                        (
                            rid,
                            int(did),
                            chosen_area,
                            speed_code,
                            dwell_code,
                            inter_code,
                            peak_code,
                            conf_code,
                            psycopg2.extras.Json(estimate_payload.get("metrics") or {}),
                            psycopg2.extras.Json(estimate_payload.get("model_inputs") or {}),
                            psycopg2.extras.Json(estimate_payload.get("fallback_reason") or []),
                            psycopg2.extras.Json(estimate_payload.get("route_prior_match") or {}),
                        ),
                    )
                    est_id = str((cur.fetchone() or {}).get("estimate_id") or "")
                    if est_id and leg_rows:
                        for lg in leg_rows:
                            cur.execute(
                                """
                                INSERT INTO gtfs_work.runtime_route_leg_features (
                                  estimate_id, leg_idx, from_seq, to_seq, distance_m,
                                  elev_from_m, elev_to_m, grade_pct, slope_mult, slope_bin,
                                  signal_count, offpeak_secs, peak_secs,
                                  offpeak_kmh_effective, peak_kmh_effective, attrs
                                ) VALUES (
                                  %s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                                )
                                """,
                                (
                                    est_id,
                                    int(_safe_num(lg.get("leg_idx"), 0)),
                                    int(_safe_num(lg.get("from_seq"), 0)),
                                    int(_safe_num(lg.get("to_seq"), 0)),
                                    float(_safe_num(lg.get("distance_m"), 0.0)),
                                    (float(lg.get("elev_from_m")) if lg.get("elev_from_m") is not None else None),
                                    (float(lg.get("elev_to_m")) if lg.get("elev_to_m") is not None else None),
                                    float(_safe_num(lg.get("grade_pct"), 0.0)),
                                    float(_safe_num(lg.get("slope_mult"), 1.0)),
                                    str(lg.get("slope_bin") or ""),
                                    int(_safe_num(lg.get("signal_count"), 0)),
                                    float(_safe_num(lg.get("offpeak_secs"), 0.0)),
                                    float(_safe_num(lg.get("peak_secs"), 0.0)),
                                    float(_safe_num(lg.get("offpeak_kmh_effective"), 0.0)),
                                    float(_safe_num(lg.get("peak_kmh_effective"), 0.0)),
                                    psycopg2.extras.Json(
                                        {
                                            "from_lat": lg.get("from_lat"),
                                            "from_lon": lg.get("from_lon"),
                                            "to_lat": lg.get("to_lat"),
                                            "to_lon": lg.get("to_lon"),
                                            "distance_source": lg.get("distance_source"),
                                            "travel_offpeak_secs": lg.get("travel_offpeak_secs"),
                                            "travel_peak_secs": lg.get("travel_peak_secs"),
                                            "dwell_offpeak_secs": lg.get("dwell_offpeak_secs"),
                                            "dwell_peak_secs": lg.get("dwell_peak_secs"),
                                            "dwell_model": lg.get("dwell_model"),
                                        }
                                    ),
                                ),
                            )
                    estimate_payload["estimate_id"] = est_id
        return estimate_payload

    def suggest_area_profile_for_route(
        self,
        *,
        route_id: str,
        direction_id: int,
        use_external_elevation: bool = True,
        use_external_signals: bool = False,
        allow_haversine_fallback: bool = False,
    ) -> Dict[str, Any]:
        """
        Analyze current route context and suggest best area_profile_code.
        Non-destructive helper for Runtime Lab autofill.
        """
        rid = str(route_id or "").strip()
        if not rid:
            raise RuntimeError("route_id is required")

        est = self.estimate_runtime_from_catalog(
            route_id=rid,
            direction_id=int(direction_id or 0),
            area_profile_code="auto",
            speed_profile_code=None,
            dwell_profile_code=None,
            intersection_profile_code=None,
            peak_profile_code=None,
            confidence_profile_code="default_v1",
            use_external_elevation=bool(use_external_elevation),
            use_external_signals=bool(use_external_signals),
            persist_snapshot=False,
            allow_haversine_fallback=bool(allow_haversine_fallback),
        )

        metrics = dict(est.get("metrics") or {})
        model = dict(est.get("model_inputs") or {})
        stop_spacing_m = _safe_num(metrics.get("stop_spacing_m"), 0.0)
        terrain_density = _safe_num(metrics.get("terrain_density_score"), 0.0)
        signals_per_km = _safe_num(model.get("signals_per_km_used"), 0.0)

        route_payload = self.get_route_map_payload(rid)
        path = _geojson_to_lonlat_path(route_payload.get("route_geojson") or {})
        curve_ratio = 1.0
        if len(path) >= 2:
            straight = _haversine_m(float(path[0][0]), float(path[0][1]), float(path[-1][0]), float(path[-1][1]))
            curve_ratio = float((_polyline_length_m(path) / max(1.0, straight))) if straight > 0 else 1.0

        rows = self.list_runtime_catalog_items(active_only=True, limit=5000)
        area_rows: List[Dict[str, Any]] = [dict(r) for r in rows if str(r.get("catalog_key") or "") == "area_profile_catalog"]
        if not area_rows:
            raise RuntimeError("No area_profile_catalog rows found. Seed catalogs first.")

        suggestions: List[Dict[str, Any]] = []
        for row in area_rows:
            code = str(row.get("item_code") or "")
            name = str(row.get("item_name") or code)
            payload = dict(row.get("payload") or {})
            mn = _safe_num(payload.get("stop_spacing_min_m"), 0.0)
            mx = _safe_num(payload.get("stop_spacing_max_m"), 999999.0)

            spacing_s = _range_score(stop_spacing_m, mn, mx)

            c = code.lower()
            if "urban" in c:
                sig_s = _density_score(signals_per_km, 2.5, 6.5)
                slope_s = _density_score(terrain_density, 0.0, 3.0)
                curve_s = _density_score(curve_ratio, 1.0, 1.25)
            elif "suburban" in c:
                sig_s = _density_score(signals_per_km, 1.5, 4.0)
                slope_s = _density_score(terrain_density, 1.0, 4.0)
                curve_s = _density_score(curve_ratio, 1.05, 1.45)
            elif "periurban" in c:
                sig_s = _density_score(signals_per_km, 0.5, 2.2)
                slope_s = _density_score(terrain_density, 2.0, 6.5)
                curve_s = _density_score(curve_ratio, 1.15, 1.8)
            else:  # express / fallback
                sig_s = _density_score(signals_per_km, 0.0, 1.6)
                slope_s = _density_score(terrain_density, 1.0, 5.5)
                curve_s = _density_score(curve_ratio, 1.0, 1.6)

            score = 0.58 * spacing_s + 0.16 * sig_s + 0.16 * slope_s + 0.10 * curve_s
            suggestions.append(
                {
                    "area_profile_code": code,
                    "area_profile_name": name,
                    "score": float(round(score, 4)),
                    "signals_score": float(round(sig_s, 4)),
                    "slope_score": float(round(slope_s, 4)),
                    "curve_score": float(round(curve_s, 4)),
                    "spacing_score": float(round(spacing_s, 4)),
                    "defaults": {
                        "speed_profile_code": str(payload.get("default_speed_profile") or ""),
                        "dwell_profile_code": str(payload.get("default_dwell_profile") or ""),
                        "intersection_profile_code": str(payload.get("default_intersection_profile") or ""),
                        "peak_profile_code": str(payload.get("default_peak_profile") or ""),
                    },
                    "range": {
                        "stop_spacing_min_m": float(round(mn, 2)),
                        "stop_spacing_max_m": float(round(mx, 2)),
                    },
                }
            )
        suggestions.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        chosen = suggestions[0] if suggestions else {}

        return {
            "route_id": rid,
            "direction_id": int(direction_id or 0),
            "features": {
                "stop_spacing_m": float(round(stop_spacing_m, 2)),
                "signals_per_km": float(round(signals_per_km, 3)),
                "terrain_density_score": float(round(terrain_density, 4)),
                "curve_ratio": float(round(curve_ratio, 4)),
            },
            "suggested_area_profile_code": str(chosen.get("area_profile_code") or ""),
            "suggestions": suggestions[:6],
            "estimate_preview": est,
        }

    def bind_runtime_estimate_to_route_direction(
        self,
        *,
        route_id: str,
        direction_id: int,
        estimate_id: str,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        rid = str(route_id or "").strip()
        did = int(direction_id or 0)
        est_id = str(estimate_id or "").strip()
        if not rid:
            raise RuntimeError("route_id is required")
        if did not in (0, 1):
            raise RuntimeError("direction_id must be 0 or 1")
        if not est_id:
            raise RuntimeError("estimate_id is required")

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT estimate_id::text AS estimate_id,
                           route_id::text AS route_id,
                           direction_id::int AS direction_id
                    FROM gtfs_work.runtime_route_estimates
                    WHERE estimate_id::text = %s
                    LIMIT 1
                    """,
                    (est_id,),
                )
                row = dict(cur.fetchone() or {})
                if not row:
                    raise RuntimeError(f"estimate_id not found: {est_id}")
                if str(row.get("route_id") or "") != rid or int(row.get("direction_id") or 0) != did:
                    raise RuntimeError("estimate_id does not match selected route_id + direction_id.")

                cur.execute(
                    """
                    INSERT INTO gtfs_work.route_runtime_estimate_bindings (
                      route_id, direction_id, estimate_id, updated_at
                    ) VALUES (%s::uuid, %s, %s::uuid, now())
                    ON CONFLICT (route_id, direction_id) DO UPDATE SET
                      estimate_id = EXCLUDED.estimate_id,
                      updated_at = now()
                    """,
                    (rid, did, est_id),
                )
                cur.execute(
                    """
                    SELECT route_id::text AS route_id,
                           direction_id::int AS direction_id,
                           estimate_id::text AS estimate_id,
                           updated_at
                    FROM gtfs_work.route_runtime_estimate_bindings
                    WHERE route_id::text = %s
                      AND direction_id = %s
                    LIMIT 1
                    """,
                    (rid, did),
                )
                out = dict(cur.fetchone() or {})
        return {"ok": True, "binding": out}

    def get_runtime_estimate_binding(
        self,
        *,
        route_id: str,
        direction_id: int,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        rid = str(route_id or "").strip()
        did = int(direction_id or 0)
        if not rid:
            return {}
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT b.route_id::text AS route_id,
                           b.direction_id::int AS direction_id,
                           b.estimate_id::text AS estimate_id,
                           b.updated_at,
                           e.estimated_at,
                           COALESCE((e.metrics->>'runtime_offpeak_secs')::int, 0) AS runtime_offpeak_secs,
                           COALESCE((e.metrics->>'runtime_peak_secs')::int, 0) AS runtime_peak_secs,
                           COALESCE((e.metrics->>'offpeak_headway_secs')::int, 0) AS offpeak_headway_secs,
                           COALESCE((e.metrics->>'peak_headway_secs')::int, 0) AS peak_headway_secs,
                           COALESCE((e.metrics->>'offpeak_min_per_leg')::float8, 0) AS offpeak_min_per_leg,
                           COALESCE((e.metrics->>'peak_min_per_leg')::float8, 0) AS peak_min_per_leg,
                           COALESCE((e.metrics->>'n_legs')::int, 0) AS n_legs,
                           COALESCE((e.metrics->>'route_len_m')::float8, 0) AS route_len_m
                    FROM gtfs_work.route_runtime_estimate_bindings b
                    LEFT JOIN gtfs_work.runtime_route_estimates e
                      ON e.estimate_id = b.estimate_id
                    WHERE b.route_id::text = %s
                      AND b.direction_id = %s
                    LIMIT 1
                    """,
                    (rid, did),
                )
                return dict(cur.fetchone() or {})

    def analyze_route_terrain_profile(
        self,
        *,
        route_id: str,
        direction_id: int,
        use_external_elevation: bool = True,
        use_external_signals: bool = False,
    ) -> Dict[str, Any]:
        """
        Terrain-only analysis over existing route shape + ordered stops.
        Does not generate new geometries.
        """
        suggestion = self.suggest_area_profile_for_route(
            route_id=str(route_id),
            direction_id=int(direction_id or 0),
            use_external_elevation=bool(use_external_elevation),
            use_external_signals=bool(use_external_signals),
        )
        est = dict(suggestion.get("estimate_preview") or {})
        legs = [dict(x) for x in (est.get("leg_preview") or [])]
        if not legs:
            return suggestion

        ascent_m = 0.0
        descent_m = 0.0
        grade_abs = []
        dist_total = 0.0
        bins = {
            "0_2": 0.0,
            "2_5": 0.0,
            "5_8": 0.0,
            "8_12": 0.0,
            "gt_12": 0.0,
        }
        for lg in legs:
            d = max(0.0, _safe_num(lg.get("distance_m"), 0.0))
            dist_total += d
            ef = lg.get("elev_from_m")
            et = lg.get("elev_to_m")
            if ef is not None and et is not None:
                de = float(et) - float(ef)
                if de > 0:
                    ascent_m += de
                elif de < 0:
                    descent_m += abs(de)
            gp = abs(_safe_num(lg.get("grade_pct"), 0.0))
            grade_abs.append(gp)
            if gp <= 2.0:
                bins["0_2"] += d
            elif gp <= 5.0:
                bins["2_5"] += d
            elif gp <= 8.0:
                bins["5_8"] += d
            elif gp <= 12.0:
                bins["8_12"] += d
            else:
                bins["gt_12"] += d

        p90_grade = 0.0
        if grade_abs:
            g = sorted(grade_abs)
            i = min(len(g) - 1, max(0, int(round(0.9 * (len(g) - 1)))))
            p90_grade = float(g[i])

        suggestion["terrain_stats"] = {
            "legs": int(len(legs)),
            "total_ascent_m": float(round(ascent_m, 2)),
            "total_descent_m": float(round(descent_m, 2)),
            "mean_abs_grade_pct": float(round(sum(grade_abs) / max(1, len(grade_abs)), 4)),
            "p90_grade_pct": float(round(p90_grade, 4)),
            "grade_bin_share": {
                "0_2_pct": float(round((bins["0_2"] / max(1.0, dist_total)) * 100.0, 2)),
                "2_5_pct": float(round((bins["2_5"] / max(1.0, dist_total)) * 100.0, 2)),
                "5_8_pct": float(round((bins["5_8"] / max(1.0, dist_total)) * 100.0, 2)),
                "8_12_pct": float(round((bins["8_12"] / max(1.0, dist_total)) * 100.0, 2)),
                "gt_12_pct": float(round((bins["gt_12"] / max(1.0, dist_total)) * 100.0, 2)),
            },
        }
        return suggestion

    def _actor(self) -> str:
        return str(os.getenv("USER") or os.getenv("USERNAME") or "console")

    def list_agency_catalog(self, *, active_only: bool = True, limit: int = 500) -> List[Dict[str, Any]]:
        self._ensure_schema()
        where = "WHERE is_active = true" if active_only else ""
        sql = f"""
        SELECT
          agency_id,
          agency_name,
          agency_name_norm,
          agency_url,
          agency_timezone,
          agency_lang,
          is_active,
          created_at,
          updated_at
        FROM gtfs_work.agency_catalog
        {where}
        ORDER BY agency_name, agency_id
        LIMIT %s
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (int(limit),))
                return list(cur.fetchall() or [])

    def preview_agencies_from_uploaded_gtfs(
        self,
        *,
        export_run_id: str,
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        self._ensure_schema()
        run_id = str(export_run_id or "").strip()
        if not run_id:
            raise RuntimeError("export_run_id is required")

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT agency_id::text AS source_agency_id,
                           agency_name::text AS source_agency_name,
                           agency_url::text AS source_agency_url,
                           agency_timezone::text AS source_agency_timezone,
                           agency_lang::text AS source_agency_lang
                    FROM gtfs_work.gtfs_agency
                    WHERE export_run_id = %s::uuid
                    ORDER BY agency_name, agency_id
                    LIMIT %s
                    """,
                    (run_id, int(limit)),
                )
                gtfs_rows = [dict(r) for r in (cur.fetchall() or [])]

                cur.execute(
                    """
                    SELECT agency_id::text AS agency_id,
                           agency_name::text AS agency_name,
                           agency_name_norm::text AS agency_name_norm
                    FROM gtfs_work.agency_catalog
                    ORDER BY agency_name, agency_id
                    """
                )
                cat_rows = [dict(r) for r in (cur.fetchall() or [])]

        by_id = {str(r.get("agency_id") or ""): r for r in cat_rows}
        by_norm = {str(r.get("agency_name_norm") or ""): r for r in cat_rows if str(r.get("agency_name_norm") or "")}

        out: List[Dict[str, Any]] = []
        seen_norm: Dict[str, int] = {}
        for r in gtfs_rows:
            src_id = str(r.get("source_agency_id") or "").strip()
            src_name = str(r.get("source_agency_name") or "").strip()
            src_norm = _normalize_text(src_name)
            seen_norm[src_norm] = seen_norm.get(src_norm, 0) + 1

            row_by_id = by_id.get(src_id) if src_id else None
            row_by_norm = by_norm.get(src_norm) if src_norm else None
            exists_id = bool(row_by_id)
            exists_name = bool(row_by_norm)

            status = "missing"
            if exists_id:
                status = "exists_id"
            elif exists_name:
                status = "exists_name"

            out.append(
                {
                    "source_agency_id": src_id,
                    "source_agency_name": src_name,
                    "source_agency_norm": src_norm,
                    "status": status,
                    "existing_agency_id": str((row_by_id or row_by_norm or {}).get("agency_id") or ""),
                    "existing_agency_name": str((row_by_id or row_by_norm or {}).get("agency_name") or ""),
                    "source_agency_url": str(r.get("source_agency_url") or "https://example.com"),
                    "source_agency_timezone": str(r.get("source_agency_timezone") or "America/Guayaquil"),
                    "source_agency_lang": str(r.get("source_agency_lang") or "es"),
                }
            )

        for row in out:
            n = int(seen_norm.get(str(row.get("source_agency_norm") or ""), 0))
            if n > 1:
                row["duplicate_in_uploaded_gtfs"] = True
                if str(row.get("status") or "") == "missing":
                    row["status"] = "duplicate_in_uploaded_gtfs"
            else:
                row["duplicate_in_uploaded_gtfs"] = False
        return out

    def resolve_uploaded_gtfs_source_export_run_id(
        self,
        *,
        gtfs_id: Optional[str],
        current_export_run_id: Optional[str] = None,
    ) -> str:
        """
        Resolve the export_run_id that contains the uploaded GTFS source rows (agency.txt, etc.)
        for a given gtfs_id.

        Phase 5 also creates an internal export context for generated rows; this helper ensures
        Step 00 (agencies) reads from the uploaded source context instead.
        """
        self._ensure_schema()
        gid = str(gtfs_id or "").strip()
        cur_run = str(current_export_run_id or "").strip()
        if not gid and not cur_run:
            return ""

        with self._conn() as conn:
            with conn.cursor() as cur:
                # If the current run already has uploaded agencies, keep using it.
                if cur_run:
                    cur.execute(
                        """
                        SELECT 1
                        FROM gtfs_work.gtfs_agency
                        WHERE export_run_id = %s::uuid
                        LIMIT 1
                        """,
                        (cur_run,),
                    )
                    if cur.fetchone():
                        return cur_run

                if not gid:
                    return ""

                # Prefer explicit upload contexts for this gtfs_id.
                cur.execute(
                    """
                    SELECT e.export_run_id::text AS export_run_id
                    FROM gtfs_work.export_runs e
                    WHERE e.gtfs_id = %s
                      AND COALESCE(e.params->>'source', '') = 'upload_zip'
                    ORDER BY e.created_at DESC
                    LIMIT 1
                    """,
                    (gid,),
                )
                row = dict(cur.fetchone() or {})
                src_run = str(row.get("export_run_id") or "").strip()
                if src_run:
                    return src_run

                # Fallback: any run for this gtfs_id that actually contains gtfs_agency rows.
                cur.execute(
                    """
                    SELECT e.export_run_id::text AS export_run_id
                    FROM gtfs_work.export_runs e
                    WHERE e.gtfs_id = %s
                      AND EXISTS (
                        SELECT 1
                        FROM gtfs_work.gtfs_agency a
                        WHERE a.export_run_id = e.export_run_id
                        LIMIT 1
                      )
                    ORDER BY e.created_at DESC
                    LIMIT 1
                    """,
                    (gid,),
                )
                row = dict(cur.fetchone() or {})
                return str(row.get("export_run_id") or "").strip()

    def insert_agency_from_uploaded_gtfs(
        self,
        *,
        source_agency_name: str,
        source_agency_id: Optional[str] = None,
        source_agency_url: Optional[str] = None,
        source_agency_timezone: Optional[str] = None,
        source_agency_lang: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        name = str(source_agency_name or "").strip()
        if not name:
            raise RuntimeError("source_agency_name is required")
        norm = _normalize_text(name)
        if not norm:
            raise RuntimeError("source_agency_name is invalid after normalization")

        preferred_id = str(source_agency_id or "").strip()
        if not preferred_id:
            preferred_id = _slugify(name)

        url = str(source_agency_url or "https://example.com").strip() or "https://example.com"
        tz = str(source_agency_timezone or "America/Guayaquil").strip() or "America/Guayaquil"
        lang = str(source_agency_lang or "es").strip() or "es"

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT agency_id::text AS agency_id, agency_name::text AS agency_name
                    FROM gtfs_work.agency_catalog
                    WHERE agency_name_norm = %s
                    LIMIT 1
                    """,
                    (norm,),
                )
                by_norm = dict(cur.fetchone() or {})
                if by_norm:
                    return {"status": "already_exists_name", **by_norm}

                cur.execute(
                    """
                    SELECT agency_id::text AS agency_id, agency_name::text AS agency_name
                    FROM gtfs_work.agency_catalog
                    WHERE agency_id = %s
                    LIMIT 1
                    """,
                    (preferred_id,),
                )
                by_id = dict(cur.fetchone() or {})
                if by_id:
                    return {"status": "already_exists_id", **by_id}

                final_id = preferred_id
                base = final_id
                n = 1
                while True:
                    cur.execute(
                        "SELECT 1 FROM gtfs_work.agency_catalog WHERE agency_id = %s LIMIT 1",
                        (final_id,),
                    )
                    if not cur.fetchone():
                        break
                    n += 1
                    final_id  = f"{base}_{n}"

                cur.execute(
                    """
                    INSERT INTO gtfs_work.agency_catalog (
                      agency_id, agency_name, agency_name_norm, agency_url, agency_timezone, agency_lang, is_active
                    ) VALUES (%s,%s,%s,%s,%s,%s,true)
                    RETURNING agency_id::text AS agency_id, agency_name::text AS agency_name
                    """,
                    (final_id, name, norm, url, tz, lang),
                )
                created = dict(cur.fetchone() or {})
        return {"status": "inserted", **created}

    def sync_agency_catalog_from_uploaded_gtfs(
        self,
        *,
        export_run_id: str,
        limit: int = 3000,
    ) -> Dict[str, Any]:
        """
        Bulk-import missing agencies from uploaded GTFS into agency_catalog.
        Existing rows by id or normalized name are reused (no duplicates).
        """
        run_id = str(export_run_id or "").strip()
        if not run_id:
            raise RuntimeError("export_run_id is required")

        preview_rows = self.preview_agencies_from_uploaded_gtfs(export_run_id=run_id, limit=int(limit))
        inserted = 0
        already_exists_id = 0
        already_exists_name = 0
        failed = 0
        errors: List[Dict[str, Any]] = []

        for row in preview_rows:
            status = str(row.get("status") or "")
            if status == "exists_id":
                already_exists_id += 1
                continue
            if status == "exists_name":
                already_exists_name += 1
                continue
            try:
                out = self.insert_agency_from_uploaded_gtfs(
                    source_agency_name=str(row.get("source_agency_name") or ""),
                    source_agency_id=(str(row.get("source_agency_id") or "").strip() or None),
                    source_agency_url=str(row.get("source_agency_url") or "https://example.com"),
                    source_agency_timezone=str(row.get("source_agency_timezone") or "America/Guayaquil"),
                    source_agency_lang=str(row.get("source_agency_lang") or "es"),
                )
                out_status = str(out.get("status") or "")
                if out_status == "inserted":
                    inserted += 1
                elif out_status == "already_exists_id":
                    already_exists_id += 1
                elif out_status == "already_exists_name":
                    already_exists_name += 1
                else:
                    failed += 1
                    errors.append(
                        {
                            "source_agency_id": str(row.get("source_agency_id") or ""),
                            "source_agency_name": str(row.get("source_agency_name") or ""),
                            "status": out_status,
                        }
                    )
            except Exception as e:
                failed += 1
                errors.append(
                    {
                        "source_agency_id": str(row.get("source_agency_id") or ""),
                        "source_agency_name": str(row.get("source_agency_name") or ""),
                        "error": str(e),
                    }
                )

        return {
            "ok": True,
            "export_run_id": run_id,
            "uploaded_total": int(len(preview_rows)),
            "inserted": int(inserted),
            "already_exists_id": int(already_exists_id),
            "already_exists_name": int(already_exists_name),
            "failed": int(failed),
            "errors": errors[:50],
        }

    def preview_gtfs_agency_insert_for_build(
        self,
        *,
        source_export_run_id: str,
        target_export_run_id: str,
        limit: int = 3000,
    ) -> List[Dict[str, Any]]:
        self._ensure_schema()
        src_run = self._require_export_run_exists(source_export_run_id)
        tgt_run = self._require_export_run_exists(target_export_run_id)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      s.agency_id::text AS agency_id,
                      s.agency_name::text AS agency_name,
                      s.agency_url::text AS agency_url,
                      s.agency_timezone::text AS agency_timezone,
                      s.agency_lang::text AS agency_lang
                    FROM gtfs_work.gtfs_agency s
                    WHERE s.export_run_id = %s::uuid
                    ORDER BY s.agency_name, s.agency_id
                    LIMIT %s
                    """,
                    (src_run, int(limit)),
                )
                src_rows = [dict(r) for r in (cur.fetchall() or [])]
                cur.execute(
                    """
                    SELECT
                      agency_id::text AS agency_id,
                      agency_name::text AS agency_name
                    FROM gtfs_work.gtfs_agency
                    WHERE export_run_id = %s::uuid
                    """,
                    (tgt_run,),
                )
                tgt_rows = [dict(r) for r in (cur.fetchall() or [])]

        by_id = {str(r.get("agency_id") or ""): dict(r) for r in tgt_rows}
        by_name_norm = {_normalize_text(r.get("agency_name")): dict(r) for r in tgt_rows if _normalize_text(r.get("agency_name"))}
        out: List[Dict[str, Any]] = []
        seen_src_name: dict[str, int] = {}
        for r in src_rows:
            aid = str(r.get("agency_id") or "").strip()
            anm = str(r.get("agency_name") or "").strip()
            anm_norm = _normalize_text(anm)
            seen_src_name[anm_norm] = seen_src_name.get(anm_norm, 0) + 1
            tgt_by_id = by_id.get(aid) if aid else None
            tgt_by_name = by_name_norm.get(anm_norm) if anm_norm else None
            status = "ready"
            if tgt_by_id:
                status = "already_in_gtfs_id_by_id"
            elif tgt_by_name:
                status = "already_in_gtfs_id_by_name"
            out.append(
                {
                    "agency_id": aid,
                    "agency_name": anm,
                    "agency_url": str(r.get("agency_url") or ""),
                    "agency_timezone": str(r.get("agency_timezone") or ""),
                    "agency_lang": str(r.get("agency_lang") or ""),
                    "status": status,
                    "existing_agency_id": str((tgt_by_id or tgt_by_name or {}).get("agency_id") or ""),
                    "existing_agency_name": str((tgt_by_id or tgt_by_name or {}).get("agency_name") or ""),
                }
            )
        for row in out:
            if seen_src_name.get(_normalize_text(row.get("agency_name")), 0) > 1:
                row["duplicate_in_source_upload"] = True
                if row["status"] == "ready":
                    row["status"] = "duplicate_in_source_upload"
            else:
                row["duplicate_in_source_upload"] = False
        return out

    def run_step_00_load_gtfs_agencies(
        self,
        *,
        source_export_run_id: str,
        target_export_run_id: str,
        limit: int = 3000,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        src_run = self._require_export_run_exists(source_export_run_id)
        tgt_run = self._require_export_run_exists(target_export_run_id)

        preview = self.preview_gtfs_agency_insert_for_build(
            source_export_run_id=src_run,
            target_export_run_id=tgt_run,
            limit=int(limit),
        )
        ready_rows = [r for r in preview if str(r.get("status") or "") == "ready"]
        inserted = 0
        with self._conn() as conn:
            with conn.cursor() as cur:
                for r in ready_rows:
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_agency (
                          export_run_id, agency_id, agency_name, agency_url, agency_timezone, agency_lang
                        ) VALUES (%s::uuid, %s, %s, %s, %s, %s)
                        ON CONFLICT (export_run_id, agency_id) DO UPDATE SET
                          agency_name = EXCLUDED.agency_name,
                          agency_url = EXCLUDED.agency_url,
                          agency_timezone = EXCLUDED.agency_timezone,
                          agency_lang = EXCLUDED.agency_lang
                        """,
                        (
                            tgt_run,
                            str(r.get("agency_id") or ""),
                            str(r.get("agency_name") or ""),
                            str(r.get("agency_url") or "https://example.com"),
                            str(r.get("agency_timezone") or "America/Guayaquil"),
                            str(r.get("agency_lang") or "es"),
                        ),
                    )
                    inserted += 1

        return {
            "ok": True,
            "source_export_run_id": src_run,
            "target_export_run_id": tgt_run,
            "preview_total": int(len(preview)),
            "inserted_gtfs_agency_rows": int(inserted),
            "skipped_existing_by_id": int(sum(1 for r in preview if str(r.get("status") or "") == "already_in_gtfs_id_by_id")),
            "skipped_existing_by_name": int(sum(1 for r in preview if str(r.get("status") or "") == "already_in_gtfs_id_by_name")),
            "skipped_duplicate_in_source": int(sum(1 for r in preview if str(r.get("status") or "") == "duplicate_in_source_upload")),
        }

    def upsert_agency_catalog(
        self,
        *,
        agency_name: str,
        agency_id: Optional[str] = None,
        agency_url: str = "https://example.com",
        agency_timezone: str = "America/Guayaquil",
        agency_lang: str = "es",
    ) -> Dict[str, Any]:
        self._ensure_schema()
        name = str(agency_name or "").strip()
        if not name:
            raise RuntimeError("agency_name is required")
        norm = _normalize_text(name)
        if not norm:
            raise RuntimeError("agency_name is invalid after normalization")

        aid = str(agency_id or "").strip() or _slugify(name)
        with self._conn() as conn:
            with conn.cursor() as cur:
                # If a very similar normalized name already exists, reuse it.
                cur.execute(
                    """
                    SELECT agency_id, agency_name
                    FROM gtfs_work.agency_catalog
                    WHERE agency_name_norm = %s
                    LIMIT 1
                    """,
                    (norm,),
                )
                existing = dict(cur.fetchone() or {})
                if existing:
                    return existing

                base = aid
                i = 1
                while True:
                    cur.execute("SELECT 1 FROM gtfs_work.agency_catalog WHERE agency_id = %s LIMIT 1", (aid,))
                    if not cur.fetchone():
                        break
                    i += 1
                    aid = f"{base}_{i}"

                cur.execute(
                    """
                    INSERT INTO gtfs_work.agency_catalog (
                      agency_id, agency_name, agency_name_norm, agency_url, agency_timezone, agency_lang, is_active
                    ) VALUES (%s,%s,%s,%s,%s,%s,true)
                    RETURNING agency_id, agency_name, agency_name_norm, agency_url, agency_timezone, agency_lang, is_active
                    """,
                    (aid, name, norm, str(agency_url), str(agency_timezone), str(agency_lang)),
                )
                return dict(cur.fetchone() or {})

    def preview_operator_agency_matching(
        self,
        *,
        route_id: Optional[str] = None,
        threshold: float = 0.88,
        verified_only: bool = True,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        self._ensure_schema()
        agencies = [dict(a) for a in self.list_agency_catalog(active_only=True, limit=2000)]
        by_agency = [
            {
                "agency_id": str(a.get("agency_id") or ""),
                "agency_name": str(a.get("agency_name") or ""),
                "agency_name_norm": str(a.get("agency_name_norm") or _normalize_text(a.get("agency_name"))),
            }
            for a in agencies
            if str(a.get("agency_id") or "").strip()
        ]

        where: List[str] = []
        params: List[Any] = []
        if route_id:
            where.append("v.route_id::text = %s")
            params.append(str(route_id))
        if verified_only:
            where.append("COALESCE(v.human_verified, false) = true")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        sql = f"""
        SELECT
          v.route_id::text AS route_id,
          COALESCE(v.route_name, '')::text AS route_name,
          COALESCE(v.route_ref, '')::text AS route_ref,
          COALESCE(v.operator_name, '')::text AS source_agency_name
        FROM gtfs_work.v_route_inputs v
        {where_sql}
        ORDER BY v.updated_at DESC NULLS LAST, v.created_at DESC NULLS LAST
        LIMIT %s
        """
        params.append(int(limit))

        out: List[Dict[str, Any]] = []
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                routes = [dict(r) for r in (cur.fetchall() or [])]
                route_ids = [str(r.get("route_id") or "") for r in routes if str(r.get("route_id") or "")]
                link_map: Dict[str, Dict[str, Any]] = {}
                if route_ids:
                    cur.execute(
                        """
                        SELECT route_id::text AS route_id, agency_id, status, match_score, match_method
                        FROM gtfs_work.route_agency_links
                        WHERE route_id = ANY(%s::uuid[])
                        """,
                        (route_ids,),
                    )
                    link_map = {str(x["route_id"]): dict(x) for x in (cur.fetchall() or [])}

        for r in routes:
            rid = str(r.get("route_id") or "")
            src_agency_name = str(r.get("source_agency_name") or "")
            src_agency_norm = _normalize_text(src_agency_name)
            best = None
            best_score = 0.0
            for a in by_agency:
                score = _similarity(src_agency_norm, str(a.get("agency_name_norm") or ""))
                if score > best_score:
                    best_score = score
                    best = a

            existing = link_map.get(rid) or {}
            current_agency_id = str(existing.get("agency_id") or "")
            decision = "needs_manual"
            if current_agency_id:
                decision = "already_linked"
            elif best and best_score >= float(threshold):
                decision = "auto_link"
            elif not src_agency_norm:
                decision = "needs_manual"

            out.append(
                {
                    "route_id": rid,
                    "route_name": str(r.get("route_name") or ""),
                    "route_ref": str(r.get("route_ref") or ""),
                    "source_agency_name": src_agency_name,
                    "source_agency_norm": src_agency_norm,
                    # backward compatibility keys
                    "operator_name": src_agency_name,
                    "operator_norm": src_agency_norm,
                    "best_agency_id": str((best or {}).get("agency_id") or ""),
                    "best_agency_name": str((best or {}).get("agency_name") or ""),
                    "best_score": round(float(best_score), 6),
                    "threshold": float(threshold),
                    "decision": decision,
                    "current_agency_id": current_agency_id,
                    "current_status": str(existing.get("status") or ""),
                    "current_match_score": existing.get("match_score"),
                    "current_match_method": str(existing.get("match_method") or ""),
                }
            )

        return out

    def apply_operator_agency_matching(
        self,
        *,
        route_id: Optional[str] = None,
        threshold: float = 0.88,
        verified_only: bool = True,
        limit: int = 500,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        rows = self.preview_operator_agency_matching(
            route_id=route_id,
            threshold=float(threshold),
            verified_only=bool(verified_only),
            limit=int(limit),
        )

        linked = 0
        queued = 0
        skipped = 0
        actor = self._actor()
        with self._conn() as conn:
            with conn.cursor() as cur:
                for r in rows:
                    rid = str(r.get("route_id") or "").strip()
                    if not rid:
                        skipped += 1
                        continue
                    decision = str(r.get("decision") or "")
                    if decision == "already_linked":
                        skipped += 1
                        continue
                    if decision == "auto_link":
                        aid = str(r.get("best_agency_id") or "").strip()
                        if not aid:
                            skipped += 1
                            continue
                        cur.execute(
                            """
                            INSERT INTO gtfs_work.route_agency_links (
                              route_id, agency_id, match_score, match_method, source_operator_name, source_agency_name, status, updated_by, updated_at
                            ) VALUES (%s::uuid,%s,%s,'operator_similarity_auto',%s,%s,'linked',%s,now())
                            ON CONFLICT (route_id) DO UPDATE SET
                              agency_id = EXCLUDED.agency_id,
                              match_score = EXCLUDED.match_score,
                              match_method = EXCLUDED.match_method,
                              source_operator_name = EXCLUDED.source_operator_name,
                              source_agency_name = EXCLUDED.source_agency_name,
                              status = 'linked',
                              updated_by = EXCLUDED.updated_by,
                              updated_at = now()
                            """,
                            (
                                rid,
                                aid,
                                float(r.get("best_score") or 0.0),
                                str(r.get("source_agency_name") or r.get("operator_name") or ""),
                                str(r.get("source_agency_name") or r.get("operator_name") or ""),
                                actor,
                            ),
                        )
                        cur.execute(
                            """
                            UPDATE gtfs_work.agency_match_requests
                            SET status = 'resolved',
                                resolved_agency_id = %s,
                                resolution_note = 'auto-linked by similarity threshold',
                                resolved_at = now()
                            WHERE route_id = %s::uuid
                              AND status = 'pending'
                            """,
                            (aid, rid),
                        )
                        linked += 1
                    else:
                        # Requests flow intentionally disabled: unmatched rows stay manual-only.
                        skipped += 1

        return {
            "ok": True,
            "threshold": float(threshold),
            "rows_total": int(len(rows)),
            "linked": int(linked),
            "queued_requests": int(queued),
            "skipped": int(skipped),
        }

    def list_pending_agency_requests(
        self,
        *,
        route_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        self._ensure_schema()
        where = ["q.status = 'pending'"]
        params: List[Any] = []
        if route_id:
            where.append("q.route_id::text = %s")
            params.append(str(route_id))
        where_sql = " AND ".join(where)
        sql = f"""
        SELECT
          q.request_id::text AS request_id,
          q.route_id::text AS route_id,
          COALESCE(q.source_agency_name, q.source_operator_name)::text AS source_agency_name,
          COALESCE(q.source_agency_norm, q.source_operator_norm)::text AS source_agency_norm,
          q.source_operator_name,
          q.source_operator_norm,
          q.suggested_agency_id,
          q.suggested_agency_name,
          q.score,
          q.status,
          q.created_at,
          v.route_name,
          v.route_ref
        FROM gtfs_work.agency_match_requests q
        LEFT JOIN gtfs_work.v_route_inputs v ON v.route_id = q.route_id
        WHERE {where_sql}
        ORDER BY q.created_at DESC
        LIMIT %s
        """
        params.append(int(limit))
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return list(cur.fetchall() or [])

    def resolve_agency_request(
        self,
        *,
        request_id: str,
        mode: str,
        agency_id: Optional[str] = None,
        agency_name: Optional[str] = None,
        resolution_note: str = "",
    ) -> Dict[str, Any]:
        self._ensure_schema()
        rid = str(request_id or "").strip()
        if not rid:
            raise RuntimeError("request_id is required")
        actor = self._actor()
        action = str(mode or "").strip().lower()

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT request_id::text,
                           route_id::text,
                           COALESCE(source_agency_name, source_operator_name)::text AS source_agency_name,
                           score,
                           status
                    FROM gtfs_work.agency_match_requests
                    WHERE request_id = %s::uuid
                    LIMIT 1
                    """,
                    (rid,),
                )
                req = dict(cur.fetchone() or {})
                if not req:
                    raise RuntimeError("agency request not found")
                if str(req.get("status") or "") != "pending":
                    raise RuntimeError("agency request is not pending")

                route_id_txt = str(req.get("route_id") or "").strip()
                if not route_id_txt:
                    raise RuntimeError("agency request has empty route_id")

                chosen_agency_id: Optional[str] = None
                if action == "reject":
                    cur.execute(
                        """
                        UPDATE gtfs_work.agency_match_requests
                        SET status = 'rejected',
                            resolution_note = %s,
                            resolved_at = now()
                        WHERE request_id = %s::uuid
                        """,
                        (str(resolution_note or "rejected by reviewer"), rid),
                    )
                    return {
                        "ok": True,
                        "request_id": rid,
                        "route_id": route_id_txt,
                        "status": "rejected",
                    }

                if action == "use_existing":
                    chosen_agency_id = str(agency_id or "").strip()
                    if not chosen_agency_id:
                        raise RuntimeError("agency_id is required for use_existing")
                    cur.execute(
                        """
                        SELECT agency_id
                        FROM gtfs_work.agency_catalog
                        WHERE agency_id = %s
                        LIMIT 1
                        """,
                        (chosen_agency_id,),
                    )
                    if not cur.fetchone():
                        raise RuntimeError(f"agency_id not found in catalog: {chosen_agency_id}")
                elif action == "create_new":
                    nm = str(agency_name or "").strip()
                    if not nm:
                        raise RuntimeError("agency_name is required for create_new")
                    norm = _normalize_text(nm)
                    cur.execute(
                        """
                        SELECT agency_id
                        FROM gtfs_work.agency_catalog
                        WHERE agency_name_norm = %s
                        LIMIT 1
                        """,
                        (norm,),
                    )
                    row_norm = dict(cur.fetchone() or {})
                    if row_norm:
                        chosen_agency_id = str(row_norm.get("agency_id") or "")
                    else:
                        chosen_agency_id = str(agency_id or "").strip() or _slugify(nm)
                        base = chosen_agency_id
                        n = 1
                        while True:
                            cur.execute(
                                "SELECT 1 FROM gtfs_work.agency_catalog WHERE agency_id = %s LIMIT 1",
                                (chosen_agency_id,),
                            )
                            if not cur.fetchone():
                                break
                            n += 1
                            chosen_agency_id = f"{base}_{n}"
                        cur.execute(
                            """
                            INSERT INTO gtfs_work.agency_catalog (
                              agency_id, agency_name, agency_name_norm, agency_url, agency_timezone, agency_lang, is_active
                            ) VALUES (%s,%s,%s,'https://example.com','America/Guayaquil','es',true)
                            """,
                            (chosen_agency_id, nm, norm),
                        )
                else:
                    raise RuntimeError("mode must be one of: use_existing, create_new, reject")

                cur.execute(
                    """
                    INSERT INTO gtfs_work.route_agency_links (
                      route_id, agency_id, match_score, match_method, source_operator_name, source_agency_name, status, updated_by, updated_at
                    ) VALUES (%s::uuid,%s,%s,'manual_review_resolve',%s,%s,'linked',%s,now())
                    ON CONFLICT (route_id) DO UPDATE SET
                      agency_id = EXCLUDED.agency_id,
                      match_score = EXCLUDED.match_score,
                      match_method = EXCLUDED.match_method,
                      source_operator_name = EXCLUDED.source_operator_name,
                      source_agency_name = EXCLUDED.source_agency_name,
                      status = 'linked',
                      updated_by = EXCLUDED.updated_by,
                      updated_at = now()
                    """,
                    (
                        route_id_txt,
                        chosen_agency_id,
                        float(req.get("score") or 0.0),
                        str(req.get("source_agency_name") or ""),
                        str(req.get("source_agency_name") or ""),
                        actor,
                    ),
                )

                cur.execute(
                    """
                    UPDATE gtfs_work.agency_match_requests
                    SET status = 'resolved',
                        resolved_agency_id = %s,
                        resolution_note = %s,
                        resolved_at = now()
                    WHERE request_id = %s::uuid
                    """,
                    (chosen_agency_id, str(resolution_note or ""), rid),
                )

                return {
                    "ok": True,
                    "request_id": rid,
                    "route_id": route_id_txt,
                    "status": "resolved",
                    "agency_id": chosen_agency_id,
                }

    def get_route_input(self, route_id: str) -> Dict[str, Any]:
        sql = """
        SELECT *
        FROM gtfs_work.v_route_inputs
        WHERE route_id::text = %s
        LIMIT 1
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (str(route_id),))
                return dict(cur.fetchone() or {})

    def get_route_map_payload(self, route_id: str) -> Dict[str, Any]:
        route_geo = {}
        stops: List[Dict[str, Any]] = []
        has_service_route_id = self._routes_has_service_route_id()
        service_route_expr = "COALESCE(r.service_route_id::text, '')::text" if has_service_route_id else "r.route_id::text"
        with self._conn() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """
                        SELECT r.route_id::text AS route_id,
                               """
                        + service_route_expr
                        + """ AS service_route_id,
                               COALESCE(r.direction_id, 0)::int AS direction_id,
                               COALESCE(s.route_name, 'route_' || left(r.route_id::text, 8)) AS route_name,
                               ST_AsGeoJSON(r.geom)::text AS route_geojson,
                               r.source,
                               r.human_verified AS geom_human_verified,
                               r.naming_confidence AS geom_naming_confidence,
                               r.chosen_geometry_candidate_id::text AS chosen_geometry_candidate_id,
                               s.route_ref,
                               s.operator_name,
                               s.route_aliases,
                               s.model_alias_score,
                               s.naming_confidence AS semantics_naming_confidence,
                               s.human_verified AS semantics_human_verified
                        FROM route_prod.routes r
                        LEFT JOIN route_prod.route_semantics s ON s.route_id = r.route_id
                        WHERE r.route_id::text = %s
                        LIMIT 1
                        """,
                        (str(route_id),),
                    )
                except Exception:
                    # Fallback for older schemas without route_semantics.model_alias_score
                    cur.execute(
                        """
                        SELECT r.route_id::text AS route_id,
                               """
                        + service_route_expr
                        + """ AS service_route_id,
                               COALESCE(r.direction_id, 0)::int AS direction_id,
                               COALESCE(s.route_name, 'route_' || left(r.route_id::text, 8)) AS route_name,
                               ST_AsGeoJSON(r.geom)::text AS route_geojson,
                               r.source,
                               r.human_verified AS geom_human_verified,
                               r.naming_confidence AS geom_naming_confidence,
                               r.chosen_geometry_candidate_id::text AS chosen_geometry_candidate_id,
                               s.route_ref,
                               s.operator_name,
                               s.route_aliases,
                               NULL::float8 AS model_alias_score,
                               s.naming_confidence AS semantics_naming_confidence,
                               s.human_verified AS semantics_human_verified
                        FROM route_prod.routes r
                        LEFT JOIN route_prod.route_semantics s ON s.route_id = r.route_id
                        WHERE r.route_id::text = %s
                        LIMIT 1
                        """,
                        (str(route_id),),
                    )
                route_geo = dict(cur.fetchone() or {})

                try:
                    cur.execute(
                        """
                        SELECT sid.seq::int AS seq,
                               n.node_id::text AS node_id,
                               COALESCE(vp.canonical_name, n.chosen_tags->>'name', '') AS name,
                               COALESCE(vp.lat, ST_Y(n.geom)::float8) AS lat,
                               COALESCE(vp.lon, ST_X(n.geom)::float8) AS lon,
                               COALESCE(n.chosen_tags->>'ele', '')::text AS ele_tag,
                               COALESCE(n.chosen_tags->>'highway', '')::text AS highway_tag,
                               vp.place_id::text AS place_id,
                               vp.canonical_name,
                               vp.place_type,
                               vp.mapping_source,
                               vp.confidence AS map_confidence
                        FROM route_prod.routes r
                        JOIN LATERAL unnest(r.stop_node_ids) WITH ORDINALITY AS sid(node_id, seq) ON true
                        JOIN node_prod.nodes n ON n.node_id = sid.node_id
                        LEFT JOIN geo_prod.v_place_points vp ON vp.node_id = n.node_id
                        WHERE r.route_id::text = %s
                        ORDER BY sid.seq
                        """,
                        (str(route_id),),
                    )
                    stops = list(cur.fetchall() or [])
                except Exception:
                    # Fallback for environments where geo_prod.v_place_points is not present yet.
                    cur.execute(
                        """
                        SELECT sid.seq::int AS seq,
                               n.node_id::text AS node_id,
                               COALESCE(n.chosen_tags->>'name', '') AS name,
                               ST_Y(n.geom)::float8 AS lat,
                               ST_X(n.geom)::float8 AS lon,
                               COALESCE(n.chosen_tags->>'ele', '')::text AS ele_tag,
                               COALESCE(n.chosen_tags->>'highway', '')::text AS highway_tag,
                               NULL::text AS place_id,
                               NULL::text AS canonical_name,
                               NULL::text AS place_type,
                               NULL::text AS mapping_source,
                               NULL::float8 AS map_confidence
                        FROM route_prod.routes r
                        JOIN LATERAL unnest(r.stop_node_ids) WITH ORDINALITY AS sid(node_id, seq) ON true
                        JOIN node_prod.nodes n ON n.node_id = sid.node_id
                        WHERE r.route_id::text = %s
                        ORDER BY sid.seq
                        """,
                        (str(route_id),),
                    )
                    stops = list(cur.fetchall() or [])

        geojson = {}
        try:
            geojson = json.loads(str(route_geo.get("route_geojson") or "{}"))
        except Exception:
            geojson = {}

        return {
            "route_id": str(route_geo.get("route_id") or route_id),
            "route_name": str(route_geo.get("route_name") or ""),
            "route_geojson": geojson,
            "route_meta": {
                "source": route_geo.get("source"),
                "service_route_id": route_geo.get("service_route_id"),
                "direction_id": route_geo.get("direction_id"),
                "geom_human_verified": route_geo.get("geom_human_verified"),
                "geom_naming_confidence": route_geo.get("geom_naming_confidence"),
                "chosen_geometry_candidate_id": route_geo.get("chosen_geometry_candidate_id"),
                "route_ref": route_geo.get("route_ref"),
                "operator_name": route_geo.get("operator_name"),
                "route_aliases": route_geo.get("route_aliases"),
                "model_alias_score": route_geo.get("model_alias_score"),
                "semantics_naming_confidence": route_geo.get("semantics_naming_confidence"),
                "semantics_human_verified": route_geo.get("semantics_human_verified"),
            },
            "stops": [dict(x) for x in stops],
        }

    def list_route_signal_points(
        self,
        *,
        route_id: str,
        include_overpass: bool = False,
        overpass_margin_m: float = 120.0,
    ) -> Dict[str, Any]:
        """
        Returns traffic signal points around route geometry.
        Sources:
          - db: node_prod.nodes with chosen_tags.highway=traffic_signals near route geom
          - overpass: optional external fallback by bbox
        """
        rid = str(route_id or "").strip()
        if not rid:
            raise RuntimeError("route_id is required")

        db_points: List[Dict[str, float]] = []
        route_bbox = None
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ST_XMin(geom)::float8 AS min_lon,
                           ST_YMin(geom)::float8 AS min_lat,
                           ST_XMax(geom)::float8 AS max_lon,
                           ST_YMax(geom)::float8 AS max_lat
                    FROM route_prod.routes
                    WHERE route_id::text = %s
                    LIMIT 1
                    """,
                    (rid,),
                )
                route_bbox = dict(cur.fetchone() or {})
                cur.execute(
                    """
                    SELECT DISTINCT
                      ST_Y(n.geom)::float8 AS lat,
                      ST_X(n.geom)::float8 AS lon
                    FROM node_prod.nodes n
                    JOIN route_prod.routes r ON r.route_id::text = %s
                    WHERE COALESCE(n.chosen_tags->>'highway','') = 'traffic_signals'
                      AND ST_DWithin(n.geom::geography, r.geom::geography, %s)
                    ORDER BY lat, lon
                    """,
                    (rid, float(overpass_margin_m)),
                )
                db_points = [dict(r) for r in (cur.fetchall() or [])]

        overpass_points: List[Dict[str, float]] = []
        if include_overpass and route_bbox:
            min_lon = float(route_bbox.get("min_lon") or 0.0)
            min_lat = float(route_bbox.get("min_lat") or 0.0)
            max_lon = float(route_bbox.get("max_lon") or 0.0)
            max_lat = float(route_bbox.get("max_lat") or 0.0)
            if max_lon > min_lon and max_lat > min_lat:
                # Expand bbox slightly for edge effects.
                pad = 0.0015
                endpoint = str(os.getenv("OVERPASS_URL") or "http://127.0.0.1:12346/api/interpreter")
                overpass_points = _overpass_signals_bbox(
                    min_lat=min_lat - pad,
                    min_lon=min_lon - pad,
                    max_lat=max_lat + pad,
                    max_lon=max_lon + pad,
                    endpoint=endpoint,
                    timeout=35,
                )

        merged = {(round(float(p.get("lat") or 0.0), 7), round(float(p.get("lon") or 0.0), 7)): {"lat": float(p.get("lat") or 0.0), "lon": float(p.get("lon") or 0.0)} for p in (db_points + overpass_points)}
        return {
            "route_id": rid,
            "db_points": db_points,
            "overpass_points": overpass_points,
            "points": list(merged.values()),
            "bbox": route_bbox or {},
            "source_used": "db+overpass" if (db_points and overpass_points) else ("overpass" if overpass_points else "db"),
        }

    def list_profiles(self, *, route_id: Optional[str] = None, limit: int = 300) -> List[Dict[str, Any]]:
        self._ensure_schema()
        where = ""
        params: List[Any] = []
        if route_id:
            where = "WHERE p.route_id = %s"
            params.append(route_id)
        sql = f"""
        SELECT p.*, v.route_name, v.route_ref, v.operator_name
        FROM gtfs_work.route_schedule_profiles p
        LEFT JOIN gtfs_work.v_route_inputs v ON v.route_id = p.route_id
        {where}
        ORDER BY p.updated_at DESC
        LIMIT %s
        """
        params.append(int(limit))
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return list(cur.fetchall() or [])

    def upsert_profile(
        self,
        *,
        route_id: str,
        direction_id: int,
        service_name: str,
        runtime_secs: int,
        dwell_secs: int = 0,
        n_blocks: int = 1,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        has_n_blocks = self._column_exists("gtfs_work", "route_schedule_profiles", "n_blocks")
        with self._conn() as conn:
            with conn.cursor() as cur:
                if has_n_blocks:
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.route_schedule_profiles (
                          route_id, direction_id, service_name, runtime_secs, dwell_secs, n_blocks, is_active
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (route_id, direction_id, service_name) DO UPDATE SET
                          runtime_secs = EXCLUDED.runtime_secs,
                          dwell_secs = EXCLUDED.dwell_secs,
                          n_blocks = EXCLUDED.n_blocks,
                          is_active = EXCLUDED.is_active,
                          updated_at = now()
                        RETURNING *
                        """,
                        (
                            route_id,
                            int(direction_id),
                            service_name,
                            int(runtime_secs),
                            int(dwell_secs),
                            max(1, int(n_blocks)),
                            bool(is_active),
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.route_schedule_profiles (
                          route_id, direction_id, service_name, runtime_secs, dwell_secs, is_active
                        ) VALUES (%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (route_id, direction_id, service_name) DO UPDATE SET
                          runtime_secs = EXCLUDED.runtime_secs,
                          dwell_secs = EXCLUDED.dwell_secs,
                          is_active = EXCLUDED.is_active,
                          updated_at = now()
                        RETURNING *
                        """,
                        (route_id, int(direction_id), service_name, int(runtime_secs), int(dwell_secs), bool(is_active)),
                    )
                return dict(cur.fetchone() or {})

    def list_windows(self, profile_id: str) -> List[Dict[str, Any]]:
        self._ensure_schema()
        sql = "SELECT * FROM gtfs_work.service_windows WHERE profile_id = %s ORDER BY start_time"
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (profile_id,))
                return list(cur.fetchall() or [])

    def upsert_window(
        self,
        *,
        profile_id: str,
        start_time: str,
        end_time: str,
        headway_secs: Optional[int],
        is_peak: bool = False,
        monday: bool,
        tuesday: bool,
        wednesday: bool,
        thursday: bool,
        friday: bool,
        saturday: bool,
        sunday: bool,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        validate_window(start_time, end_time, headway_secs)
        has_is_peak = self._column_exists("gtfs_work", "service_windows", "is_peak")
        with self._conn() as conn:
            with conn.cursor() as cur:
                if has_is_peak:
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.service_windows (
                          profile_id, start_time, end_time, headway_secs, is_peak,
                          monday, tuesday, wednesday, thursday, friday, saturday, sunday
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        RETURNING *
                        """,
                        (
                            profile_id,
                            start_time,
                            end_time,
                            int(headway_secs) if headway_secs else None,
                            bool(is_peak),
                            bool(monday), bool(tuesday), bool(wednesday), bool(thursday), bool(friday), bool(saturday), bool(sunday),
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.service_windows (
                          profile_id, start_time, end_time, headway_secs,
                          monday, tuesday, wednesday, thursday, friday, saturday, sunday
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        RETURNING *
                        """,
                        (
                            profile_id,
                            start_time,
                            end_time,
                            int(headway_secs) if headway_secs else None,
                            bool(monday), bool(tuesday), bool(wednesday), bool(thursday), bool(friday), bool(saturday), bool(sunday),
                        ),
                    )
                return dict(cur.fetchone() or {})

    def clear_windows(self, profile_id: str) -> int:
        self._ensure_schema()
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM gtfs_work.service_windows WHERE profile_id = %s", (profile_id,))
                return int(cur.rowcount or 0)

    def update_service_windows_days(
        self,
        *,
        profile_ids: List[str],
        monday: bool,
        tuesday: bool,
        wednesday: bool,
        thursday: bool,
        friday: bool,
        saturday: bool,
        sunday: bool,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        ids = [str(x) for x in (profile_ids or []) if str(x).strip()]
        if not ids:
            return {"ok": True, "updated": 0}
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE gtfs_work.service_windows
                    SET monday = %s,
                        tuesday = %s,
                        wednesday = %s,
                        thursday = %s,
                        friday = %s,
                        saturday = %s,
                        sunday = %s
                    WHERE profile_id = ANY(%s::uuid[])
                    """,
                    (
                        bool(monday),
                        bool(tuesday),
                        bool(wednesday),
                        bool(thursday),
                        bool(friday),
                        bool(saturday),
                        bool(sunday),
                        ids,
                    ),
                )
                return {"ok": True, "updated": int(cur.rowcount or 0)}

    def export_timing_bundle(
        self,
        *,
        route_id: Optional[str] = None,
        include_inactive: bool = False,
        limit: int = 500,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        rows = self.list_profiles(route_id=route_id, limit=int(limit))
        out_profiles: List[Dict[str, Any]] = []
        for row in rows:
            if (not include_inactive) and (not bool(row.get("is_active", True))):
                continue
            pid = str(row.get("profile_id") or "")
            if not pid:
                continue
            windows = self.list_windows(pid)
            if not include_inactive:
                windows = [w for w in windows if bool(w.get("is_active", True))]
            out_profiles.append(
                {
                    "route_id": str(row.get("route_id") or ""),
                    "direction_id": int(row.get("direction_id") or 0),
                    "service_name": str(row.get("service_name") or ""),
                    "runtime_secs": int(row.get("runtime_secs") or 3600),
                    "dwell_secs": int(row.get("dwell_secs") or 0),
                    "n_blocks": max(1, int(row.get("n_blocks") or 1)),
                    "is_active": bool(row.get("is_active", True)),
                    "windows": [
                        {
                            "start_time": str(w.get("start_time") or ""),
                            "end_time": str(w.get("end_time") or ""),
                            "headway_secs": (int(w.get("headway_secs")) if w.get("headway_secs") is not None else None),
                            "is_peak": bool(w.get("is_peak", False)),
                            "monday": bool(w.get("monday", True)),
                            "tuesday": bool(w.get("tuesday", True)),
                            "wednesday": bool(w.get("wednesday", True)),
                            "thursday": bool(w.get("thursday", True)),
                            "friday": bool(w.get("friday", True)),
                            "saturday": bool(w.get("saturday", False)),
                            "sunday": bool(w.get("sunday", False)),
                        }
                        for w in windows
                    ],
                }
            )

        return {
            "bundle_type": "phase5_timing",
            "bundle_version": "v1",
            "route_id": str(route_id or ""),
            "profiles": out_profiles,
        }

    def import_timing_bundle(
        self,
        payload: Mapping[str, Any],
        *,
        route_id_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        bundle = dict(payload or {})
        profiles = bundle.get("profiles") or []
        if not isinstance(profiles, list):
            raise RuntimeError("Invalid timing bundle: 'profiles' must be a list.")

        imported_profiles = 0
        imported_windows = 0
        route_ids: set[str] = set()

        for p in profiles:
            if not isinstance(p, Mapping):
                continue
            p_map = dict(p)
            route_id = str(route_id_override or p_map.get("route_id") or "").strip()
            if not route_id:
                continue

            prof = self.upsert_profile(
                route_id=route_id,
                direction_id=int(p_map.get("direction_id") or 0),
                service_name=str(p_map.get("service_name") or "weekday_base").strip() or "weekday_base",
                runtime_secs=max(60, int(p_map.get("runtime_secs") or 3600)),
                dwell_secs=max(0, int(p_map.get("dwell_secs") or 0)),
                n_blocks=max(1, int(p_map.get("n_blocks") or 1)),
                is_active=bool(p_map.get("is_active", True)),
            )
            profile_id = str(prof.get("profile_id") or "")
            if not profile_id:
                continue

            self.clear_windows(profile_id)
            imported_profiles += 1
            route_ids.add(route_id)

            windows = p_map.get("windows") or []
            if not isinstance(windows, list):
                windows = []

            for w in windows:
                if not isinstance(w, Mapping):
                    continue
                w_map = dict(w)
                self.upsert_window(
                    profile_id=profile_id,
                    start_time=str(w_map.get("start_time") or "").strip(),
                    end_time=str(w_map.get("end_time") or "").strip(),
                    headway_secs=(int(w_map.get("headway_secs")) if w_map.get("headway_secs") is not None else None),
                    is_peak=bool(w_map.get("is_peak", False)),
                    monday=bool(w_map.get("monday", True)),
                    tuesday=bool(w_map.get("tuesday", True)),
                    wednesday=bool(w_map.get("wednesday", True)),
                    thursday=bool(w_map.get("thursday", True)),
                    friday=bool(w_map.get("friday", True)),
                    saturday=bool(w_map.get("saturday", False)),
                    sunday=bool(w_map.get("sunday", False)),
                )
                imported_windows += 1

        return {
            "ok": True,
            "profiles_imported": imported_profiles,
            "windows_imported": imported_windows,
            "routes_touched": sorted(route_ids),
        }

    # ----------------------------------------
    # Exports / Steps
    # ----------------------------------------
    def create_export_run(self, *, gtfs_id: Optional[str] = None, params: Optional[Dict[str, Any]] = None) -> str:
        self._ensure_schema()
        gid = str(gtfs_id or "").strip()
        if not gid:
            raise RuntimeError("gtfs_id is required to create export_run_id.")
        run_id = new_uuid()
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO gtfs_work.export_runs (export_run_id, gtfs_id, status, params) VALUES (%s::uuid, %s, 'draft', %s::jsonb)",
                    (run_id, gid, psycopg2.extras.Json(params or {})),
                )
        return run_id

    def create_gtfs_build(
        self,
        *,
        gtfs_id: Optional[str] = None,
        export_run_id: Optional[str] = None,
        build_name: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        gid = str(gtfs_id or "").strip() or f"gtfs-{str(uuid.uuid4())[:8]}"
        run_id = str(export_run_id or "").strip()
        run_uuid = uuid.UUID(run_id) if run_id else None
        actor = self._actor()
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO gtfs_work.gtfs_builds
                      (gtfs_id, export_run_id, build_name, status, notes, created_by, updated_at)
                    VALUES
                      (%s, %s::uuid, %s, 'draft', %s, %s, now())
                    ON CONFLICT (gtfs_id) DO UPDATE SET
                      export_run_id = EXCLUDED.export_run_id,
                      build_name = COALESCE(EXCLUDED.build_name, gtfs_work.gtfs_builds.build_name),
                      notes = COALESCE(EXCLUDED.notes, gtfs_work.gtfs_builds.notes),
                      updated_at = now()
                    RETURNING
                      gtfs_id,
                      export_run_id::text AS export_run_id,
                      build_name,
                      status,
                      notes,
                      created_by,
                      created_at,
                      updated_at
                    """,
                    (gid, run_uuid, str(build_name or "").strip() or None, str(notes or "").strip() or None, actor),
                )
                return dict(cur.fetchone() or {})

    def list_gtfs_builds(self, *, limit: int = 300) -> List[Dict[str, Any]]:
        self._ensure_schema()
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      b.gtfs_id,
                      b.export_run_id::text AS export_run_id,
                      b.build_name,
                      b.status,
                      b.notes,
                      b.created_by,
                      b.created_at,
                      b.updated_at,
                      e.status AS export_status,
                      e.created_at AS export_created_at,
                      e.completed_at AS export_completed_at
                    FROM gtfs_work.gtfs_builds b
                    LEFT JOIN gtfs_work.export_runs e
                      ON e.export_run_id = b.export_run_id
                    ORDER BY b.updated_at DESC, b.created_at DESC, b.gtfs_id
                    LIMIT %s
                    """,
                    (int(limit),),
                )
                return list(cur.fetchall() or [])

    def link_gtfs_build_export_run(
        self,
        *,
        gtfs_id: str,
        export_run_id: str,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        gid = str(gtfs_id or "").strip()
        run_id = str(export_run_id or "").strip()
        if not gid:
            raise RuntimeError("gtfs_id is required")
        if not run_id:
            raise RuntimeError("export_run_id is required")
        run_uuid = str(uuid.UUID(run_id))
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE gtfs_work.gtfs_builds
                    SET export_run_id = %s::uuid,
                        updated_at = now()
                    WHERE gtfs_id = %s
                    RETURNING
                      gtfs_id,
                      export_run_id::text AS export_run_id,
                      build_name,
                      status,
                      notes,
                      created_by,
                      created_at,
                      updated_at
                    """,
                    (run_uuid, gid),
                )
                row = dict(cur.fetchone() or {})
                if not row:
                    raise RuntimeError(f"gtfs_id not found: {gid}")
                return row

    def unlink_gtfs_build_export_run(self, *, gtfs_id: str) -> Dict[str, Any]:
        self._ensure_schema()
        gid = str(gtfs_id or "").strip()
        if not gid:
            raise RuntimeError("gtfs_id is required")
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE gtfs_work.gtfs_builds
                    SET export_run_id = NULL,
                        updated_at = now()
                    WHERE gtfs_id = %s
                    RETURNING
                      gtfs_id,
                      export_run_id::text AS export_run_id,
                      build_name,
                      status,
                      notes,
                      created_by,
                      created_at,
                      updated_at
                    """,
                    (gid,),
                )
                row = dict(cur.fetchone() or {})
                if not row:
                    raise RuntimeError(f"gtfs_id not found: {gid}")
                return row

    def get_export_run_id_for_gtfs_id(self, gtfs_id: str) -> str:
        self._ensure_schema()
        gid = str(gtfs_id or "").strip()
        if not gid:
            return ""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT export_run_id::text AS export_run_id
                    FROM gtfs_work.gtfs_builds
                    WHERE gtfs_id = %s
                    LIMIT 1
                    """,
                    (gid,),
                )
                row = dict(cur.fetchone() or {})
                return str(row.get("export_run_id") or "")

    def ensure_export_run_for_gtfs_id(self, gtfs_id: str) -> str:
        self._ensure_schema()
        gid = str(gtfs_id or "").strip()
        if not gid:
            raise RuntimeError("gtfs_id is required")
        existing = self.get_export_run_id_for_gtfs_id(gid)
        if existing and self.export_run_exists(existing):
            return str(existing)
        if existing and not self.export_run_exists(existing):
            try:
                self.unlink_gtfs_build_export_run(gtfs_id=gid)
            except Exception:
                pass
        run_id = self.create_export_run(
            gtfs_id=gid,
            params={"source": "phase5_gtfs_build", "gtfs_id": gid},
        )
        self.link_gtfs_build_export_run(gtfs_id=gid, export_run_id=run_id)
        return str(run_id)

    def list_export_runs(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM gtfs_work.export_runs ORDER BY created_at DESC LIMIT %s", (int(limit),))
                return list(cur.fetchall() or [])

    def export_run_exists(self, export_run_id: str) -> bool:
        run_id = str(export_run_id or "").strip()
        if not run_id:
            return False
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM gtfs_work.export_runs WHERE export_run_id = %s::uuid LIMIT 1",
                    (run_id,),
                )
                return bool(cur.fetchone())

    def _require_export_run_exists(self, export_run_id: str) -> str:
        run_id = str(export_run_id or "").strip()
        if not run_id:
            raise RuntimeError("export_run_id is required")
        if not self.export_run_exists(run_id):
            raise RuntimeError(
                "Selected execution export_run_id does not exist (stale/deleted). "
                "Pick a valid export_run_id in Phase 5 context controls or create a new one."
            )
        return run_id

    def run_step_01_prepare(self, *, export_run_id: str, route_id: Optional[str] = None) -> Dict[str, Any]:
        self._ensure_schema()
        run_id = self._require_export_run_exists(export_run_id)
        p = ensure_default_profiles_for_verified_routes()
        w = ensure_default_windows()
        return {
            "ok": True,
            "export_run_id": run_id,
            "route_id": (str(route_id) if route_id else None),
            "profiles_inserted": int(p or 0),
            "windows_inserted": int(w or 0),
        }

    def _log_step_deletions(
        self,
        *,
        export_run_id: str,
        step_name: str,
        before_counts: Dict[str, int],
        after_counts: Dict[str, int],
        route_id: Optional[str] = None,
    ) -> Dict[str, int]:
        run_id = str(export_run_id or "").strip()
        if not run_id:
            return {}
        deleted: Dict[str, int] = {}
        for t, b in (before_counts or {}).items():
            a = int((after_counts or {}).get(t, 0))
            b_i = int(b or 0)
            if a < b_i:
                deleted[str(t)] = int(b_i - a)
        if not deleted:
            return {}

        actor = self._actor()
        with self._conn() as conn:
            with conn.cursor() as cur:
                for table_name, n_del in deleted.items():
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.revision_events
                          (export_run_id, event_type, table_name, row_key, before_row, after_row, edited_by)
                        VALUES
                          (%s::uuid, 'step_rebuild_delete', %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)
                        """,
                        (
                            run_id,
                            str(table_name),
                            json.dumps(
                                {
                                    "step": str(step_name),
                                    "route_id": (str(route_id) if route_id else None),
                                    "deleted_rows": int(n_del),
                                },
                                ensure_ascii=False,
                            ),
                            json.dumps({"count": int(before_counts.get(table_name, 0))}, ensure_ascii=False),
                            json.dumps({"count": int(after_counts.get(table_name, 0))}, ensure_ascii=False),
                            actor,
                        ),
                    )
        return deleted

    def list_revision_events(
        self,
        *,
        export_run_id: str,
        event_type: Optional[str] = None,
        table_name: Optional[str] = None,
        limit: int = 300,
    ) -> List[Dict[str, Any]]:
        self._ensure_schema()
        run_id = str(export_run_id or "").strip()
        if not run_id:
            return []
        where = ["export_run_id = %s::uuid"]
        params: List[Any] = [run_id]
        if event_type:
            where.append("event_type = %s")
            params.append(str(event_type))
        if table_name:
            where.append("table_name = %s")
            params.append(str(table_name))
        sql = f"""
        SELECT event_id::text AS event_id,
               export_run_id::text AS export_run_id,
               event_type,
               table_name,
               row_key,
               before_row,
               after_row,
               edited_by,
               edited_at
        FROM gtfs_work.revision_events
        WHERE {' AND '.join(where)}
        ORDER BY edited_at DESC
        LIMIT %s
        """
        params.append(int(limit))
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return list(cur.fetchall() or [])

    def run_step_02_calendar(self, export_run_id: str) -> Dict[str, Any]:
        self._ensure_schema()
        export_run_id = self._require_export_run_exists(export_run_id)
        from datetime import date, timedelta
        before = self.get_gtfs_table_counts(str(export_run_id))
        out = build_calendar(export_run_id, start_date=date.today(), end_date=date.today() + timedelta(days=120))
        after = self.get_gtfs_table_counts(str(export_run_id))
        deleted = self._log_step_deletions(
            export_run_id=str(export_run_id),
            step_name="step_02_calendar",
            before_counts=before,
            after_counts=after,
        )
        if isinstance(out, dict):
            out["deleted_rows_logged"] = deleted
        return out

    def run_step_03_routes_only(self, export_run_id: str, route_id: Optional[str] = None) -> Dict[str, Any]:
        self._ensure_schema()
        export_run_id = self._require_export_run_exists(export_run_id)
        before = self.get_gtfs_table_counts(str(export_run_id))
        out = build_routes_and_stops(export_run_id, route_id=route_id, include_stops=False)
        after = self.get_gtfs_table_counts(str(export_run_id))
        deleted = self._log_step_deletions(
            export_run_id=str(export_run_id),
            step_name="step_03_routes_only",
            before_counts=before,
            after_counts=after,
            route_id=route_id,
        )
        out["deleted_rows_logged"] = deleted
        return out

    def run_step_03b_shapes(
        self,
        export_run_id: str,
        *,
        route_id: Optional[str] = None,
        direction_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        export_run_id = self._require_export_run_exists(export_run_id)
        before = self.get_gtfs_table_counts(str(export_run_id))
        out = build_shapes(
            export_run_id,
            route_id=(str(route_id or "").strip() or None),
            direction_id=(None if direction_id is None else int(direction_id)),
        )
        after = self.get_gtfs_table_counts(str(export_run_id))
        deleted = self._log_step_deletions(
            export_run_id=str(export_run_id),
            step_name="step_03b_shapes",
            before_counts=before,
            after_counts=after,
            route_id=(str(route_id or "").strip() or None),
        )
        if isinstance(out, dict):
            out["deleted_rows_logged"] = deleted
        return out

    def run_step_03_routes_shapes(self, export_run_id: str, route_id: Optional[str] = None) -> Dict[str, Any]:
        # Backward-compatible combined path (routes first, then shapes for the same route scope).
        out_routes = self.run_step_03_routes_only(export_run_id, route_id=route_id)
        out_shapes = self.run_step_03b_shapes(export_run_id, route_id=route_id)
        return {
            **dict(out_routes or {}),
            **dict(out_shapes or {}),
            "compat_combined": True,
        }

    def preview_phase2_final_gtfs_stops(
        self,
        *,
        route_ids: Optional[List[str]] = None,
        place_set_id: Optional[str] = None,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        self._ensure_schema()
        scoped = [str(x).strip() for x in (route_ids or []) if str(x).strip()]
        psid = str(place_set_id or "").strip()
        with self._conn() as conn:
            with conn.cursor() as cur:
                if psid:
                    cur.execute(
                        """
                        SELECT DISTINCT
                          n.node_id::text AS stop_id,
                          vp.canonical_name::text AS stop_name,
                          COALESCE(vp.lat, ST_Y(n.geom)::float8) AS stop_lat,
                          COALESCE(vp.lon, ST_X(n.geom)::float8) AS stop_lon,
                          vp.place_id::text AS place_id,
                          vp.place_type::text AS place_type,
                          n.node_type::text AS node_type,
                          COUNT(*) OVER ()::int AS _total
                        FROM geo_work.v_place_set_points sp
                        JOIN node_prod.nodes n
                          ON n.node_id::text = sp.node_id::text
                         AND n.node_type = 'STOP'
                        JOIN geo_prod.v_place_points vp
                          ON vp.node_id = n.node_id
                         AND vp.node_type = 'STOP'
                        WHERE sp.place_set_id::text = %s
                          AND COALESCE(NULLIF(BTRIM(vp.canonical_name), ''), '') <> ''
                        ORDER BY stop_name, stop_id
                        LIMIT %s
                        """,
                        (psid, int(limit)),
                    )
                elif scoped:
                    cur.execute(
                        """
                        SELECT DISTINCT
                          n.node_id::text AS stop_id,
                          vp.canonical_name::text AS stop_name,
                          COALESCE(vp.lat, ST_Y(n.geom)::float8) AS stop_lat,
                          COALESCE(vp.lon, ST_X(n.geom)::float8) AS stop_lon,
                          vp.place_id::text AS place_id,
                          vp.place_type::text AS place_type,
                          n.node_type::text AS node_type,
                          COUNT(*) OVER ()::int AS _total
                        FROM route_prod.routes r
                        JOIN LATERAL unnest(r.stop_node_ids) AS sid(node_id) ON true
                        JOIN node_prod.nodes n
                          ON n.node_id = sid.node_id
                         AND n.node_type = 'STOP'
                        JOIN geo_prod.v_place_points vp
                          ON vp.node_id = n.node_id
                         AND vp.node_type = 'STOP'
                        WHERE r.route_id::text = ANY(%s::text[])
                          AND COALESCE(NULLIF(BTRIM(vp.canonical_name), ''), '') <> ''
                        ORDER BY stop_name, stop_id
                        LIMIT %s
                        """,
                        (scoped, int(limit)),
                    )
                else:
                    cur.execute(
                        """
                        SELECT DISTINCT
                          n.node_id::text AS stop_id,
                          vp.canonical_name::text AS stop_name,
                          COALESCE(vp.lat, ST_Y(n.geom)::float8) AS stop_lat,
                          COALESCE(vp.lon, ST_X(n.geom)::float8) AS stop_lon,
                          vp.place_id::text AS place_id,
                          vp.place_type::text AS place_type,
                          n.node_type::text AS node_type,
                          COUNT(*) OVER ()::int AS _total
                        FROM geo_prod.v_place_points vp
                        JOIN node_prod.nodes n
                          ON n.node_id = vp.node_id
                         AND n.node_type = 'STOP'
                        WHERE vp.node_type = 'STOP'
                          AND COALESCE(NULLIF(BTRIM(vp.canonical_name), ''), '') <> ''
                        ORDER BY stop_name, stop_id
                        LIMIT %s
                        """,
                        (int(limit),),
                    )
                return [dict(r) for r in (cur.fetchall() or [])]

    def run_step_015_load_stops_from_phase2_final(
        self,
        *,
        export_run_id: str,
        route_ids: Optional[List[str]] = None,
        place_set_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        run_id = self._require_export_run_exists(export_run_id)
        scoped = [str(x).strip() for x in (route_ids or []) if str(x).strip()]
        psid = str(place_set_id or "").strip()

        with self._conn() as conn:
            with conn.cursor() as cur:
                if psid:
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_stops (
                          export_run_id, stop_id, stop_name, stop_lat, stop_lon, location_type, parent_station
                        )
                        SELECT DISTINCT
                          %s::uuid AS export_run_id,
                          n.node_id::text AS stop_id,
                          vp.canonical_name::text AS stop_name,
                          COALESCE(vp.lat, ST_Y(n.geom)::float8) AS stop_lat,
                          COALESCE(vp.lon, ST_X(n.geom)::float8) AS stop_lon,
                          0 AS location_type,
                          NULL::text AS parent_station
                        FROM geo_work.v_place_set_points sp
                        JOIN node_prod.nodes n
                          ON n.node_id::text = sp.node_id::text
                         AND n.node_type = 'STOP'
                        JOIN geo_prod.v_place_points vp
                          ON vp.node_id = n.node_id
                         AND vp.node_type = 'STOP'
                        WHERE sp.place_set_id::text = %s
                          AND COALESCE(NULLIF(BTRIM(vp.canonical_name), ''), '') <> ''
                        ON CONFLICT (export_run_id, stop_id) DO UPDATE SET
                          stop_name = EXCLUDED.stop_name,
                          stop_lat = EXCLUDED.stop_lat,
                          stop_lon = EXCLUDED.stop_lon
                        """,
                        (run_id, psid),
                    )
                elif scoped:
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_stops (
                          export_run_id, stop_id, stop_name, stop_lat, stop_lon, location_type, parent_station
                        )
                        SELECT DISTINCT
                          %s::uuid AS export_run_id,
                          n.node_id::text AS stop_id,
                          vp.canonical_name::text AS stop_name,
                          COALESCE(vp.lat, ST_Y(n.geom)::float8) AS stop_lat,
                          COALESCE(vp.lon, ST_X(n.geom)::float8) AS stop_lon,
                          0 AS location_type,
                          NULL::text AS parent_station
                        FROM route_prod.routes r
                        JOIN LATERAL unnest(r.stop_node_ids) AS sid(node_id) ON true
                        JOIN node_prod.nodes n
                          ON n.node_id = sid.node_id
                         AND n.node_type = 'STOP'
                        JOIN geo_prod.v_place_points vp
                          ON vp.node_id = n.node_id
                         AND vp.node_type = 'STOP'
                        WHERE r.route_id::text = ANY(%s::text[])
                          AND COALESCE(NULLIF(BTRIM(vp.canonical_name), ''), '') <> ''
                        ON CONFLICT (export_run_id, stop_id) DO UPDATE SET
                          stop_name = EXCLUDED.stop_name,
                          stop_lat = EXCLUDED.stop_lat,
                          stop_lon = EXCLUDED.stop_lon
                        """,
                        (run_id, scoped),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_stops (
                          export_run_id, stop_id, stop_name, stop_lat, stop_lon, location_type, parent_station
                        )
                        SELECT DISTINCT
                          %s::uuid AS export_run_id,
                          n.node_id::text AS stop_id,
                          vp.canonical_name::text AS stop_name,
                          COALESCE(vp.lat, ST_Y(n.geom)::float8) AS stop_lat,
                          COALESCE(vp.lon, ST_X(n.geom)::float8) AS stop_lon,
                          0 AS location_type,
                          NULL::text AS parent_station
                        FROM geo_prod.v_place_points vp
                        JOIN node_prod.nodes n
                          ON n.node_id = vp.node_id
                         AND n.node_type = 'STOP'
                        WHERE vp.node_type = 'STOP'
                          AND COALESCE(NULLIF(BTRIM(vp.canonical_name), ''), '') <> ''
                        ON CONFLICT (export_run_id, stop_id) DO UPDATE SET
                          stop_name = EXCLUDED.stop_name,
                          stop_lat = EXCLUDED.stop_lat,
                          stop_lon = EXCLUDED.stop_lon
                        """,
                        (run_id,),
                    )
                n_upsert = int(cur.rowcount or 0)

                if psid:
                    cur.execute(
                        """
                        SELECT COUNT(*)::int AS n
                        FROM gtfs_work.gtfs_stops
                        WHERE export_run_id = %s::uuid
                          AND stop_id IN (
                            SELECT DISTINCT n.node_id::text
                            FROM geo_work.v_place_set_points sp
                            JOIN node_prod.nodes n
                              ON n.node_id::text = sp.node_id::text
                             AND n.node_type = 'STOP'
                            JOIN geo_prod.v_place_points vp
                              ON vp.node_id = n.node_id
                             AND vp.node_type = 'STOP'
                            WHERE sp.place_set_id::text = %s
                              AND COALESCE(NULLIF(BTRIM(vp.canonical_name), ''), '') <> ''
                          )
                        """,
                        (run_id, psid),
                    )
                elif scoped:
                    cur.execute(
                        """
                        SELECT COUNT(*)::int AS n
                        FROM gtfs_work.gtfs_stops
                        WHERE export_run_id = %s::uuid
                          AND stop_id IN (
                            SELECT DISTINCT n.node_id::text
                            FROM route_prod.routes r
                            JOIN LATERAL unnest(r.stop_node_ids) AS sid(node_id) ON true
                            JOIN node_prod.nodes n
                              ON n.node_id = sid.node_id
                             AND n.node_type = 'STOP'
                            JOIN geo_prod.v_place_points vp
                              ON vp.node_id = n.node_id
                             AND vp.node_type = 'STOP'
                            WHERE r.route_id::text = ANY(%s::text[])
                              AND COALESCE(NULLIF(BTRIM(vp.canonical_name), ''), '') <> ''
                          )
                        """,
                        (run_id, scoped),
                    )
                else:
                    cur.execute(
                        """
                        SELECT COUNT(*)::int AS n
                        FROM gtfs_work.gtfs_stops
                        WHERE export_run_id = %s::uuid
                        """,
                        (run_id,),
                    )
                row = dict(cur.fetchone() or {})

        return {
            "ok": True,
            "export_run_id": run_id,
            "route_ids": scoped,
            "place_set_id": psid or None,
            "scope": (
                "place_set_scoped"
                if psid
                else ("all_phase2_final_stops" if not scoped else "route_scoped")
            ),
            "stops_upserted": int(n_upsert),
            "scoped_gtfs_stops_now": int(row.get("n") or 0),
            "source": "phase2_final_geo_prod_v_place_points",
        }

    def list_phase2_place_sets_for_gtfs_stops(self, *, limit: int = 500) -> List[Dict[str, Any]]:
        self._ensure_schema()
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      sp.place_set_id::text AS place_set_id,
                      COUNT(DISTINCT sp.node_id)::int AS n_points,
                      COUNT(*) FILTER (WHERE n.node_type = 'STOP')::int AS n_stop_rows,
                      MAX(COALESCE(vp.canonical_name, '')) AS sample_name
                    FROM geo_work.v_place_set_points sp
                    LEFT JOIN node_prod.nodes n
                      ON n.node_id::text = sp.node_id::text
                    LEFT JOIN geo_prod.v_place_points vp
                      ON vp.node_id::text = sp.node_id::text
                     AND vp.node_type = 'STOP'
                    GROUP BY sp.place_set_id
                    ORDER BY n_stop_rows DESC, n_points DESC, sp.place_set_id::text
                    LIMIT %s
                    """,
                    (int(limit),),
                )
                return [dict(r) for r in (cur.fetchall() or [])]

    def run_step_04_trips(
        self,
        export_run_id: str,
        *,
        route_id: Optional[str] = None,
        direction_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        export_run_id = self._require_export_run_exists(export_run_id)
        before = self.get_gtfs_table_counts(str(export_run_id))
        out = build_trips_and_frequencies(
            export_run_id,
            route_id=str(route_id or "").strip() or None,
            direction_id=(None if direction_id is None else int(direction_id)),
        )
        after = self.get_gtfs_table_counts(str(export_run_id))
        deleted = self._log_step_deletions(
            export_run_id=str(export_run_id),
            step_name="step_04_trips",
            before_counts=before,
            after_counts=after,
            route_id=(str(route_id or "").strip() or None),
        )
        if isinstance(out, dict):
            out["deleted_rows_logged"] = deleted
        return out

    def run_step_05_stop_times(
        self,
        export_run_id: str,
        *,
        route_id: Optional[str] = None,
        direction_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        export_run_id = self._require_export_run_exists(export_run_id)
        before = self.get_gtfs_table_counts(str(export_run_id))
        out = build_stop_times(
            export_run_id,
            route_id=str(route_id or "").strip() or None,
            direction_id=(None if direction_id is None else int(direction_id)),
        )
        after = self.get_gtfs_table_counts(str(export_run_id))
        deleted = self._log_step_deletions(
            export_run_id=str(export_run_id),
            step_name="step_05_stop_times",
            before_counts=before,
            after_counts=after,
            route_id=(str(route_id or "").strip() or None),
        )
        if isinstance(out, dict):
            out["deleted_rows_logged"] = deleted
        return out

    def run_step_06_validate(self, export_run_id: str) -> Dict[str, Any]:
        export_run_id = self._require_export_run_exists(export_run_id)
        rep = validate_export_run(export_run_id)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE gtfs_work.export_runs SET validator_report = %s::jsonb WHERE export_run_id = %s::uuid",
                    (psycopg2.extras.Json(rep), export_run_id),
                )
        return rep

    def run_step_07_package(self, export_run_id: str) -> Dict[str, Any]:
        export_run_id = self._require_export_run_exists(export_run_id)
        out = package_gtfs(export_run_id)
        summary = {
            "export_run_id": str(export_run_id),
            "row_counts": dict(out.get("row_counts") or {}),
            "validator_ok": bool((out.get("validator_report") or {}).get("ok")),
            "validator_errors": list((out.get("validator_report") or {}).get("errors") or []),
        }
        artifact = create_pending_artifact(
            zip_path=str(out.get("zip_path") or ""),
            summary_json=summary,
            actor="system",
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE gtfs_work.export_runs SET status='packaged', output_zip_path=%s, completed_at=now() WHERE export_run_id=%s::uuid",
                    (out.get("zip_path"), export_run_id),
                )
        out["artifact"] = artifact
        return out

    # ----------------------------------------
    # Review / Edit generated GTFS rows
    # ----------------------------------------
    def list_gtfs_rows(
        self,
        table_name: str,
        export_run_id: str,
        *,
        limit: int = 500,
        route_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if table_name not in TABLE_PKS:
            raise RuntimeError(f"Unsupported table_name: {table_name}")
        route_id_txt = str(route_id or "").strip()
        gtfs_route_id_txt = self._resolve_gtfs_route_id(route_id_txt) if route_id_txt else ""
        if not route_id_txt:
            sql = f"SELECT * FROM gtfs_work.{table_name} WHERE export_run_id = %s LIMIT %s"
            params: tuple[Any, ...] = (export_run_id, int(limit))
        elif table_name == "gtfs_routes":
            sql = """
            SELECT *
            FROM gtfs_work.gtfs_routes
            WHERE export_run_id = %s
              AND route_id = %s
            LIMIT %s
            """
            params = (export_run_id, gtfs_route_id_txt, int(limit))
        elif table_name == "gtfs_trips":
            sql = """
            SELECT *
            FROM gtfs_work.gtfs_trips
            WHERE export_run_id = %s
              AND route_id = %s
            ORDER BY trip_id
            LIMIT %s
            """
            params = (export_run_id, gtfs_route_id_txt, int(limit))
        elif table_name == "gtfs_stop_times":
            sql = """
            SELECT st.*
            FROM gtfs_work.gtfs_stop_times st
            JOIN gtfs_work.gtfs_trips t
              ON t.export_run_id = st.export_run_id
             AND t.trip_id = st.trip_id
            WHERE st.export_run_id = %s
              AND t.route_id = %s
            ORDER BY st.trip_id, st.stop_sequence
            LIMIT %s
            """
            params = (export_run_id, gtfs_route_id_txt, int(limit))
        elif table_name == "gtfs_shapes":
            sql = """
            SELECT s.*
            FROM gtfs_work.gtfs_shapes s
            WHERE s.export_run_id = %s
              AND s.shape_id IN (
                SELECT DISTINCT t.shape_id
                FROM gtfs_work.gtfs_trips t
                WHERE t.export_run_id = %s
                  AND t.route_id = %s
                  AND t.shape_id IS NOT NULL
              )
            ORDER BY s.shape_id, s.shape_pt_sequence
            LIMIT %s
            """
            params = (export_run_id, export_run_id, gtfs_route_id_txt, int(limit))
        elif table_name == "gtfs_stops":
            sql = """
            SELECT s.*
            FROM gtfs_work.gtfs_stops s
            WHERE s.export_run_id = %s
              AND s.stop_id IN (
                SELECT DISTINCT st.stop_id
                FROM gtfs_work.gtfs_stop_times st
                JOIN gtfs_work.gtfs_trips t
                  ON t.export_run_id = st.export_run_id
                 AND t.trip_id = st.trip_id
                WHERE st.export_run_id = %s
                  AND t.route_id = %s
              )
            ORDER BY s.stop_name, s.stop_id
            LIMIT %s
            """
            params = (export_run_id, export_run_id, gtfs_route_id_txt, int(limit))
        elif table_name == "gtfs_frequencies":
            sql = """
            SELECT f.*
            FROM gtfs_work.gtfs_frequencies f
            WHERE f.export_run_id = %s
              AND f.trip_id IN (
                SELECT t.trip_id
                FROM gtfs_work.gtfs_trips t
                WHERE t.export_run_id = %s
                  AND t.route_id = %s
              )
            ORDER BY f.trip_id, f.start_time
            LIMIT %s
            """
            params = (export_run_id, export_run_id, gtfs_route_id_txt, int(limit))
        elif table_name == "gtfs_calendar":
            sql = """
            SELECT c.*
            FROM gtfs_work.gtfs_calendar c
            WHERE c.export_run_id = %s
              AND c.service_id IN (
                SELECT DISTINCT t.service_id
                FROM gtfs_work.gtfs_trips t
                WHERE t.export_run_id = %s
                  AND t.route_id = %s
              )
            ORDER BY c.service_id
            LIMIT %s
            """
            params = (export_run_id, export_run_id, gtfs_route_id_txt, int(limit))
        elif table_name == "gtfs_calendar_dates":
            sql = """
            SELECT cd.*
            FROM gtfs_work.gtfs_calendar_dates cd
            WHERE cd.export_run_id = %s
              AND cd.service_id IN (
                SELECT DISTINCT t.service_id
                FROM gtfs_work.gtfs_trips t
                WHERE t.export_run_id = %s
                  AND t.route_id = %s
              )
            ORDER BY cd.service_id, cd.date
            LIMIT %s
            """
            params = (export_run_id, export_run_id, gtfs_route_id_txt, int(limit))
        elif table_name == "gtfs_agency":
            sql = """
            SELECT a.*
            FROM gtfs_work.gtfs_agency a
            WHERE a.export_run_id = %s
              AND a.agency_id IN (
                SELECT DISTINCT r.agency_id
                FROM gtfs_work.gtfs_routes r
                WHERE r.export_run_id = %s
                  AND r.route_id = %s
                  AND r.agency_id IS NOT NULL
              )
            ORDER BY a.agency_id
            LIMIT %s
            """
            params = (export_run_id, export_run_id, gtfs_route_id_txt, int(limit))
        else:
            sql = f"SELECT * FROM gtfs_work.{table_name} WHERE export_run_id = %s LIMIT %s"
            params = (export_run_id, int(limit))
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall() or [])

    def get_gtfs_table_counts(self, export_run_id: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        with self._conn() as conn:
            with conn.cursor() as cur:
                for table_name in TABLE_PKS.keys():
                    cur.execute(
                        f"SELECT COUNT(*)::int AS n FROM gtfs_work.{table_name} WHERE export_run_id = %s",
                        (export_run_id,),
                    )
                    row = cur.fetchone() or {}
                    out[table_name] = int(row.get("n") or 0)
        return out

    def get_gtfs_shape_counts(self, export_run_id: str) -> Dict[str, int]:
        """
        Returns total shape-point rows and distinct shape_ids for the export run.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)::int AS shape_rows,
                           COUNT(DISTINCT shape_id)::int AS shape_ids
                    FROM gtfs_work.gtfs_shapes
                    WHERE export_run_id = %s
                    """,
                    (export_run_id,),
                )
                row = cur.fetchone() or {}
        return {"shape_rows": int(row.get("shape_rows") or 0), "shape_ids": int(row.get("shape_ids") or 0)}

    def update_gtfs_row(self, table_name: str, export_run_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
        if table_name not in TABLE_PKS:
            raise RuntimeError(f"Unsupported table_name: {table_name}")
        pks = TABLE_PKS[table_name]
        for k in pks:
            if k not in row:
                raise RuntimeError(f"Missing PK column: {k}")

        cols = [k for k in row.keys() if k != "export_run_id"]
        set_cols = [c for c in cols if c not in pks]
        set_sql = ", ".join([f"{c} = %s" for c in set_cols])
        where_sql = " AND ".join([f"{k} = %s" for k in pks])
        sql = f"UPDATE gtfs_work.{table_name} SET {set_sql} WHERE export_run_id = %s AND {where_sql} RETURNING *"
        params = [row[c] for c in set_cols] + [export_run_id] + [row[k] for k in pks]

        with self._conn() as conn:
            with conn.cursor() as cur:
                where_sql = " AND ".join([f"{k} = %s" for k in pks])
                cur.execute(
                    f"SELECT * FROM gtfs_work.{table_name} WHERE export_run_id = %s AND {where_sql}",
                    tuple([export_run_id] + [row[k] for k in pks]),
                )
                before = dict(cur.fetchone() or {})
                cur.execute(sql, tuple(params))
                out = cur.fetchone()
                if not out:
                    raise RuntimeError("Row not found for update")
                after = dict(out)
                try:
                    row_key = {k: row.get(k) for k in pks}
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.revision_events
                          (export_run_id, event_type, table_name, row_key, before_row, after_row, edited_by)
                        VALUES
                          (%s::uuid, 'update_row', %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)
                        """,
                        (
                            export_run_id,
                            table_name,
                            json.dumps(row_key, ensure_ascii=False),
                            json.dumps(before, ensure_ascii=False),
                            json.dumps(after, ensure_ascii=False),
                            os.getenv("USER") or os.getenv("USERNAME") or "console",
                        ),
                    )
                except Exception:
                    pass
                return after

    # ----------------------------------------
    # Revise Current GTFS (upload + focused editors)
    # ----------------------------------------
    def _to_int(self, v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            return int(default)

    def _to_float(self, v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except Exception:
            return float(default)

    def import_gtfs_zip(self, *, file_bytes: bytes, filename: str, gtfs_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Imports uploaded GTFS zip into a new gtfs_work.export_run_id.
        Stores parsed rows directly in gtfs_work.gtfs_* tables so existing review tools work.
        """
        self._ensure_schema()
        gid = str(gtfs_id or "").strip() or f"gtfs-upload-{str(uuid.uuid4())[:8]}"
        try:
            self.create_gtfs_build(gtfs_id=gid, build_name=filename)
        except Exception:
            pass
        export_run_id = self.create_export_run(
            gtfs_id=gid,
            params={"source": "upload_zip", "filename": filename, "gtfs_id": gid},
        )
        try:
            self.link_gtfs_build_export_run(gtfs_id=gid, export_run_id=export_run_id)
        except Exception:
            pass
        counts: Dict[str, int] = {k: 0 for k in TABLE_PKS.keys()}

        with self._conn() as conn:
            with conn.cursor() as cur:
                # stamp upload metadata
                try:
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.upload_runs (export_run_id, file_name, file_size_bytes, uploaded_by)
                        VALUES (%s::uuid, %s, %s, %s)
                        ON CONFLICT (export_run_id) DO UPDATE SET
                          file_name = EXCLUDED.file_name,
                          file_size_bytes = EXCLUDED.file_size_bytes,
                          uploaded_by = EXCLUDED.uploaded_by,
                          uploaded_at = now()
                        """,
                        (
                            export_run_id,
                            filename,
                            len(file_bytes),
                            os.getenv("USER") or os.getenv("USERNAME") or "console",
                        ),
                    )
                except Exception:
                    pass

                zf = zipfile.ZipFile(io.BytesIO(file_bytes))
                names = {n.lower().split("/")[-1]: n for n in zf.namelist()}

                def _read_rows(txt_name: str) -> List[Dict[str, Any]]:
                    full = names.get(txt_name.lower())
                    if not full:
                        return []
                    raw = zf.read(full)
                    text = raw.decode("utf-8-sig", errors="replace")
                    return list(csv.DictReader(io.StringIO(text)))

                # agency.txt
                for r in _read_rows("agency.txt"):
                    agency_id = (r.get("agency_id") or "agency_1").strip() or "agency_1"
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_agency
                          (export_run_id, agency_id, agency_name, agency_url, agency_timezone, agency_lang)
                        VALUES (%s::uuid,%s,%s,%s,%s,%s)
                        ON CONFLICT (export_run_id, agency_id) DO UPDATE SET
                          agency_name = EXCLUDED.agency_name,
                          agency_url = EXCLUDED.agency_url,
                          agency_timezone = EXCLUDED.agency_timezone,
                          agency_lang = EXCLUDED.agency_lang
                        """,
                        (
                            export_run_id,
                            agency_id,
                            r.get("agency_name") or "Agency",
                            r.get("agency_url") or "https://example.com",
                            r.get("agency_timezone") or "America/Guayaquil",
                            r.get("agency_lang"),
                        ),
                    )
                    counts["gtfs_agency"] += 1

                # stops.txt
                for r in _read_rows("stops.txt"):
                    sid = (r.get("stop_id") or "").strip()
                    if not sid:
                        continue
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_stops
                          (export_run_id, stop_id, stop_name, stop_lat, stop_lon, location_type, parent_station)
                        VALUES (%s::uuid,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (export_run_id, stop_id) DO UPDATE SET
                          stop_name = EXCLUDED.stop_name,
                          stop_lat = EXCLUDED.stop_lat,
                          stop_lon = EXCLUDED.stop_lon,
                          location_type = EXCLUDED.location_type,
                          parent_station = EXCLUDED.parent_station
                        """,
                        (
                            export_run_id,
                            sid,
                            r.get("stop_name") or sid,
                            self._to_float(r.get("stop_lat"), 0.0),
                            self._to_float(r.get("stop_lon"), 0.0),
                            self._to_int(r.get("location_type"), 0),
                            r.get("parent_station"),
                        ),
                    )
                    counts["gtfs_stops"] += 1

                # routes.txt
                for r in _read_rows("routes.txt"):
                    rid = (r.get("route_id") or "").strip()
                    if not rid:
                        continue
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_routes
                          (export_run_id, route_id, agency_id, route_short_name, route_long_name, route_type, route_color, route_text_color)
                        VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (export_run_id, route_id) DO UPDATE SET
                          agency_id = EXCLUDED.agency_id,
                          route_short_name = EXCLUDED.route_short_name,
                          route_long_name = EXCLUDED.route_long_name,
                          route_type = EXCLUDED.route_type,
                          route_color = EXCLUDED.route_color,
                          route_text_color = EXCLUDED.route_text_color
                        """,
                        (
                            export_run_id,
                            rid,
                            r.get("agency_id"),
                            r.get("route_short_name"),
                            r.get("route_long_name") or rid,
                            self._to_int(r.get("route_type"), 3),
                            r.get("route_color"),
                            r.get("route_text_color"),
                        ),
                    )
                    counts["gtfs_routes"] += 1

                # trips.txt
                for r in _read_rows("trips.txt"):
                    tid = (r.get("trip_id") or "").strip()
                    rid = (r.get("route_id") or "").strip()
                    sid = (r.get("service_id") or "").strip()
                    if not tid or not rid or not sid:
                        continue
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_trips
                          (export_run_id, route_id, service_id, trip_id, trip_headsign, direction_id, shape_id, block_id)
                        VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (export_run_id, trip_id) DO UPDATE SET
                          route_id = EXCLUDED.route_id,
                          service_id = EXCLUDED.service_id,
                          trip_headsign = EXCLUDED.trip_headsign,
                          direction_id = EXCLUDED.direction_id,
                          shape_id = EXCLUDED.shape_id,
                          block_id = EXCLUDED.block_id
                        """,
                        (
                            export_run_id,
                            rid,
                            sid,
                            tid,
                            r.get("trip_headsign"),
                            self._to_int(r.get("direction_id"), 0),
                            r.get("shape_id"),
                            r.get("block_id"),
                        ),
                    )
                    counts["gtfs_trips"] += 1

                # stop_times.txt
                for r in _read_rows("stop_times.txt"):
                    tid = (r.get("trip_id") or "").strip()
                    seq = self._to_int(r.get("stop_sequence"), -1)
                    stop_id = (r.get("stop_id") or "").strip()
                    if not tid or seq < 0 or not stop_id:
                        continue
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_stop_times
                          (export_run_id, trip_id, arrival_time, departure_time, stop_id, stop_sequence, timepoint)
                        VALUES (%s::uuid,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (export_run_id, trip_id, stop_sequence) DO UPDATE SET
                          arrival_time = EXCLUDED.arrival_time,
                          departure_time = EXCLUDED.departure_time,
                          stop_id = EXCLUDED.stop_id,
                          timepoint = EXCLUDED.timepoint
                        """,
                        (
                            export_run_id,
                            tid,
                            r.get("arrival_time") or "00:00:00",
                            r.get("departure_time") or r.get("arrival_time") or "00:00:00",
                            stop_id,
                            seq,
                            self._to_int(r.get("timepoint"), 1),
                        ),
                    )
                    counts["gtfs_stop_times"] += 1

                # shapes.txt
                for r in _read_rows("shapes.txt"):
                    sid = (r.get("shape_id") or "").strip()
                    seq = self._to_int(r.get("shape_pt_sequence"), -1)
                    if not sid or seq < 0:
                        continue
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_shapes
                          (export_run_id, shape_id, shape_pt_lat, shape_pt_lon, shape_pt_sequence, shape_dist_traveled)
                        VALUES (%s::uuid,%s,%s,%s,%s,%s)
                        ON CONFLICT (export_run_id, shape_id, shape_pt_sequence) DO UPDATE SET
                          shape_pt_lat = EXCLUDED.shape_pt_lat,
                          shape_pt_lon = EXCLUDED.shape_pt_lon,
                          shape_dist_traveled = EXCLUDED.shape_dist_traveled
                        """,
                        (
                            export_run_id,
                            sid,
                            self._to_float(r.get("shape_pt_lat"), 0.0),
                            self._to_float(r.get("shape_pt_lon"), 0.0),
                            seq,
                            self._to_float(r.get("shape_dist_traveled"), 0.0) if r.get("shape_dist_traveled") not in (None, "") else None,
                        ),
                    )
                    counts["gtfs_shapes"] += 1

                # optional calendar.txt
                for r in _read_rows("calendar.txt"):
                    service_id = (r.get("service_id") or "").strip()
                    if not service_id:
                        continue
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_calendar
                          (export_run_id, service_id, monday, tuesday, wednesday, thursday, friday, saturday, sunday, start_date, end_date)
                        VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (export_run_id, service_id) DO UPDATE SET
                          monday=EXCLUDED.monday, tuesday=EXCLUDED.tuesday, wednesday=EXCLUDED.wednesday,
                          thursday=EXCLUDED.thursday, friday=EXCLUDED.friday, saturday=EXCLUDED.saturday, sunday=EXCLUDED.sunday,
                          start_date=EXCLUDED.start_date, end_date=EXCLUDED.end_date
                        """,
                        (
                            export_run_id,
                            service_id,
                            self._to_int(r.get("monday"), 0),
                            self._to_int(r.get("tuesday"), 0),
                            self._to_int(r.get("wednesday"), 0),
                            self._to_int(r.get("thursday"), 0),
                            self._to_int(r.get("friday"), 0),
                            self._to_int(r.get("saturday"), 0),
                            self._to_int(r.get("sunday"), 0),
                            r.get("start_date") or "",
                            r.get("end_date") or "",
                        ),
                    )
                    counts["gtfs_calendar"] += 1

                # optional calendar_dates.txt
                for r in _read_rows("calendar_dates.txt"):
                    service_id = (r.get("service_id") or "").strip()
                    date_txt = (r.get("date") or "").strip()
                    if not service_id or not date_txt:
                        continue
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_calendar_dates
                          (export_run_id, service_id, date, exception_type)
                        VALUES (%s::uuid,%s,%s,%s)
                        ON CONFLICT (export_run_id, service_id, date) DO UPDATE SET
                          exception_type = EXCLUDED.exception_type
                        """,
                        (
                            export_run_id,
                            service_id,
                            date_txt,
                            self._to_int(r.get("exception_type"), 1),
                        ),
                    )
                    counts["gtfs_calendar_dates"] += 1

                # optional frequencies.txt
                for r in _read_rows("frequencies.txt"):
                    trip_id = (r.get("trip_id") or "").strip()
                    start_time = (r.get("start_time") or "").strip()
                    if not trip_id or not start_time:
                        continue
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_frequencies
                          (export_run_id, trip_id, start_time, end_time, headway_secs, exact_times)
                        VALUES (%s::uuid,%s,%s,%s,%s,%s)
                        ON CONFLICT (export_run_id, trip_id, start_time) DO UPDATE SET
                          end_time = EXCLUDED.end_time,
                          headway_secs = EXCLUDED.headway_secs,
                          exact_times = EXCLUDED.exact_times
                        """,
                        (
                            export_run_id,
                            trip_id,
                            start_time,
                            r.get("end_time") or start_time,
                            self._to_int(r.get("headway_secs"), 60),
                            self._to_int(r.get("exact_times"), 0),
                        ),
                    )
                    counts["gtfs_frequencies"] += 1

                cur.execute(
                    """
                    UPDATE gtfs_work.export_runs
                    SET status = 'uploaded',
                        summary = %s::jsonb
                    WHERE export_run_id = %s::uuid
                    """,
                    (json.dumps(counts, ensure_ascii=False), export_run_id),
                )

        return {"gtfs_id": gid, "export_run_id": export_run_id, "counts": counts, "filename": filename}

    def list_route_ids_for_export(self, export_run_id: str) -> List[str]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT route_id
                    FROM (
                      SELECT route_id FROM gtfs_work.gtfs_routes WHERE export_run_id = %s
                      UNION
                      SELECT route_id FROM gtfs_work.gtfs_trips WHERE export_run_id = %s
                    ) x
                    ORDER BY route_id
                    """,
                    (str(export_run_id), str(export_run_id)),
                )
                return [str(r["route_id"]) for r in (cur.fetchall() or []) if r.get("route_id")]

    def suggest_profile_from_uploaded_gtfs(
        self,
        *,
        export_run_id: str,
        route_id: str,
        direction_id: int,
        service_route_id: Optional[str] = None,
        strict_direction: bool = True,
    ) -> Dict[str, Any]:
        """
        Build non-destructive scheduling suggestions from uploaded GTFS rows:
        - route match (fuzzy by route_ref / route_name if IDs differ)
        - service day flags (calendar majority over selected direction trips)
        - headway + day window (frequencies or first departures fallback)
        - runtime_secs (median trip duration)
        - n_blocks (distinct block_id)
        """
        exp = str(export_run_id or "").strip()
        rid = str(route_id or "").strip()
        sid = str(service_route_id or "").strip()
        did = int(direction_id or 0)
        if not exp:
            raise RuntimeError("export_run_id is required")
        if not rid:
            raise RuntimeError("route_id is required")

        has_service_route_id = self._routes_has_service_route_id()
        service_route_expr = "COALESCE(r.service_route_id::text, '')::text" if has_service_route_id else "r.route_id::text"
        service_where_expr = "r.service_route_id::text" if has_service_route_id else "r.route_id::text"

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      r.route_id::text AS route_id,
                      """
                    + service_route_expr
                    + """ AS service_route_id,
                      COALESCE(rs.route_ref, v.route_ref, '')::text AS route_ref,
                      COALESCE(rs.route_name, v.route_name, '')::text AS route_name,
                      COALESCE(rs.operator_name, v.operator_name, '')::text AS operator_name
                    FROM route_prod.routes r
                    LEFT JOIN route_prod.route_semantics rs
                      ON rs.route_id = r.route_id
                    LEFT JOIN gtfs_work.v_route_inputs v
                      ON v.route_id = r.route_id
                    WHERE r.route_id::text = %s
                    LIMIT 1
                    """,
                    (rid,),
                )
                phase_row = dict(cur.fetchone() or {})

                # Optional broader context by service_route_id (both directions).
                # This improves fuzzy matching when route_id differs between imports.
                service_rows: List[Dict[str, Any]] = []
                service_route_norm = ""
                if sid:
                    cur.execute(
                        """
                        SELECT
                          r.route_id::text AS route_id,
                          """
                        + service_route_expr
                        + """ AS service_route_id,
                          COALESCE(rs.route_ref, '')::text AS route_ref,
                          COALESCE(rs.route_name, '')::text AS route_name,
                          COALESCE(rs.operator_name, '')::text AS operator_name,
                          COALESCE(r.direction_id, 0)::int AS direction_id
                        FROM route_prod.routes r
                        LEFT JOIN route_prod.route_semantics rs
                          ON rs.route_id = r.route_id
                        WHERE """
                        + service_where_expr
                        + """ = %s
                        ORDER BY COALESCE(r.direction_id, 0), r.updated_at DESC NULLS LAST, r.created_at DESC NULLS LAST
                        """,
                        (sid,),
                    )
                    service_rows = [dict(r) for r in (cur.fetchall() or [])]
                    service_route_norm = _normalize_text(sid)

                cur.execute(
                    """
                    SELECT
                      r.route_id::text AS route_id,
                      COALESCE(r.route_short_name, '')::text AS route_short_name,
                      COALESCE(r.route_long_name, '')::text AS route_long_name,
                      COALESCE(r.agency_id, '')::text AS agency_id,
                      COALESCE(a.agency_name, '')::text AS agency_name
                    FROM gtfs_work.gtfs_routes r
                    LEFT JOIN gtfs_work.gtfs_agency a
                      ON a.export_run_id = r.export_run_id
                     AND a.agency_id = r.agency_id
                    WHERE r.export_run_id = %s
                    ORDER BY route_id
                    """,
                    (exp,),
                )
                gtfs_routes = [dict(r) for r in (cur.fetchall() or [])]

                if not gtfs_routes:
                    raise RuntimeError("No gtfs_routes found for selected export_run_id.")

                ref_norm = _normalize_text(phase_row.get("route_ref"))
                name_norm = _normalize_text(phase_row.get("route_name"))
                rid_norm = _normalize_text(rid)
                sid_norm = _normalize_text(sid or phase_row.get("service_route_id"))
                operator_norm = _normalize_text(phase_row.get("operator_name"))

                service_route_ids = {str(x.get("route_id") or "") for x in service_rows if str(x.get("route_id") or "")}
                service_refs = [_normalize_text(x.get("route_ref")) for x in service_rows if _normalize_text(x.get("route_ref"))]
                service_names = [_normalize_text(x.get("route_name")) for x in service_rows if _normalize_text(x.get("route_name"))]

                scored: List[Dict[str, Any]] = []
                for gr in gtfs_routes:
                    gr_id = str(gr.get("route_id") or "")
                    gr_short_norm = _normalize_text(gr.get("route_short_name"))
                    gr_long_norm = _normalize_text(gr.get("route_long_name"))
                    gr_agency_norm = _normalize_text(gr.get("agency_name") or gr.get("agency_id"))

                    # Signals
                    exact_route_id = 1.0 if gr_id and _normalize_text(gr_id) == rid_norm else 0.0
                    service_route_id_hit = 1.0 if gr_id and gr_id in service_route_ids else 0.0
                    ref_sim = _similarity(gr_short_norm, ref_norm) if ref_norm else 0.0
                    name_sim = _similarity(gr_long_norm, name_norm) if name_norm else 0.0
                    sid_sim = _similarity(_normalize_text(gr_id), sid_norm) if sid_norm else 0.0
                    # Operator from Phase is mapped against GTFS agency identity/name.
                    op_sim = _similarity(operator_norm, gr_agency_norm) if (operator_norm and gr_agency_norm) else 0.0
                    service_ref_sim = max([_similarity(gr_short_norm, x) for x in service_refs], default=0.0) if service_refs else 0.0
                    service_name_sim = max([_similarity(gr_long_norm, x) for x in service_names], default=0.0) if service_names else 0.0
                    token_overlap = max(
                        _token_jaccard(gr_long_norm, name_norm),
                        _token_jaccard(gr_short_norm, ref_norm),
                        _token_jaccard(gr_long_norm, " ".join(service_names)),
                    )

                    score = (
                        1.20 * exact_route_id
                        + 0.95 * service_route_id_hit
                        + 0.75 * ref_sim
                        + 0.85 * name_sim
                        + 0.45 * service_ref_sim
                        + 0.55 * service_name_sim
                        + 0.20 * sid_sim
                        + 0.15 * op_sim
                        + 0.30 * token_overlap
                    )
                    scored.append(
                        {
                            "route_id": gr_id,
                            "route_short_name": str(gr.get("route_short_name") or ""),
                            "route_long_name": str(gr.get("route_long_name") or ""),
                            "agency_id": str(gr.get("agency_id") or ""),
                            "agency_name": str(gr.get("agency_name") or ""),
                            "score": float(score),
                            "components": {
                                "exact_route_id": float(exact_route_id),
                                "service_route_id_hit": float(service_route_id_hit),
                                "ref_sim": float(ref_sim),
                                "name_sim": float(name_sim),
                                "service_ref_sim": float(service_ref_sim),
                                "service_name_sim": float(service_name_sim),
                                "sid_sim": float(sid_sim),
                                "operator_sim": float(op_sim),
                                "token_overlap": float(token_overlap),
                            },
                        }
                    )

                scored.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
                best = dict(scored[0]) if scored else {}
                best_score = float(best.get("score") or 0.0)

                gtfs_route_id = str(best.get("route_id") or "")
                if not gtfs_route_id:
                    raise RuntimeError("Could not match a GTFS route for suggestions.")

                # Direction-aware trip scope
                cur.execute(
                    """
                    SELECT trip_id::text AS trip_id, service_id::text AS service_id, COALESCE(block_id, '')::text AS block_id
                    FROM gtfs_work.gtfs_trips
                    WHERE export_run_id = %s
                      AND route_id = %s
                      AND COALESCE(direction_id, 0) = %s
                    ORDER BY trip_id
                    """,
                    (exp, gtfs_route_id, did),
                )
                trips = [dict(r) for r in (cur.fetchall() or [])]
                used_direction = did
                direction_fallback = False
                if not trips and not bool(strict_direction):
                    # fallback: same route without direction filter
                    cur.execute(
                        """
                        SELECT trip_id::text AS trip_id, service_id::text AS service_id, COALESCE(block_id, '')::text AS block_id,
                               COALESCE(direction_id, 0)::int AS direction_id
                        FROM gtfs_work.gtfs_trips
                        WHERE export_run_id = %s
                          AND route_id = %s
                        ORDER BY trip_id
                        """,
                        (exp, gtfs_route_id),
                    )
                    trips = [dict(r) for r in (cur.fetchall() or [])]
                    if trips:
                        direction_fallback = True
                        # Fallback to the direction with most trips.
                        dir_counts: Dict[int, int] = {}
                        for t in trips:
                            dd = int(t.get("direction_id") or 0)
                            dir_counts[dd] = int(dir_counts.get(dd, 0) + 1)
                        used_direction = int(sorted(dir_counts.items(), key=lambda kv: kv[1], reverse=True)[0][0])
                        trips = [t for t in trips if int(t.get("direction_id") or 0) == used_direction]
                if not trips:
                    cur.execute(
                        """
                        SELECT DISTINCT COALESCE(direction_id, 0)::int AS direction_id, COUNT(*)::int AS n
                        FROM gtfs_work.gtfs_trips
                        WHERE export_run_id = %s
                          AND route_id = %s
                        GROUP BY COALESCE(direction_id, 0)
                        ORDER BY direction_id
                        """,
                        (exp, gtfs_route_id),
                    )
                    avail = [dict(r) for r in (cur.fetchall() or [])]
                    raise RuntimeError(
                        f"No trips in uploaded GTFS for matched route `{gtfs_route_id}` with direction_id={did}. "
                        f"Available directions: {avail or 'none'}"
                    )

                trip_ids = [str(t.get("trip_id") or "") for t in trips if str(t.get("trip_id") or "")]
                service_ids = sorted({str(t.get("service_id") or "") for t in trips if str(t.get("service_id") or "")})
                block_ids = sorted({str(t.get("block_id") or "") for t in trips if str(t.get("block_id") or "").strip()})
                n_blocks = max(1, len(block_ids)) if block_ids else 1

                # Service-day suggestion (majority over involved service_ids)
                day_flags: Dict[str, bool] = {
                    "monday": True,
                    "tuesday": True,
                    "wednesday": True,
                    "thursday": True,
                    "friday": True,
                    "saturday": False,
                    "sunday": False,
                }
                if service_ids:
                    cur.execute(
                        """
                        SELECT service_id,
                               monday::int AS monday,
                               tuesday::int AS tuesday,
                               wednesday::int AS wednesday,
                               thursday::int AS thursday,
                               friday::int AS friday,
                               saturday::int AS saturday,
                               sunday::int AS sunday
                        FROM gtfs_work.gtfs_calendar
                        WHERE export_run_id = %s
                          AND service_id = ANY(%s::text[])
                        """,
                        (exp, service_ids),
                    )
                    cal_rows = [dict(r) for r in (cur.fetchall() or [])]
                    if cal_rows:
                        for day in day_flags.keys():
                            vals = [int(r.get(day) or 0) for r in cal_rows]
                            day_flags[day] = (sum(vals) / max(1, len(vals))) >= 0.5

                # Runtime suggestion from stop_times trip duration median
                cur.execute(
                    """
                    WITH ordered AS (
                      SELECT
                        st.trip_id::text AS trip_id,
                        COALESCE(st.departure_time, st.arrival_time)::text AS dep_t,
                        COALESCE(st.arrival_time, st.departure_time)::text AS arr_t,
                        st.stop_sequence,
                        row_number() OVER (PARTITION BY st.trip_id ORDER BY st.stop_sequence ASC) AS rn_first,
                        row_number() OVER (PARTITION BY st.trip_id ORDER BY st.stop_sequence DESC) AS rn_last
                      FROM gtfs_work.gtfs_stop_times st
                      WHERE st.export_run_id = %s
                        AND st.trip_id = ANY(%s::text[])
                    )
                    SELECT
                      trip_id,
                      MAX(CASE WHEN rn_first = 1 THEN dep_t END) AS first_t,
                      MAX(CASE WHEN rn_last = 1 THEN arr_t END) AS last_t
                    FROM ordered
                    GROUP BY trip_id
                    """,
                    (exp, trip_ids),
                )
                dur_rows = [dict(r) for r in (cur.fetchall() or [])]
                durations: List[int] = []
                first_departures: List[int] = []
                for r in dur_rows:
                    t0 = _time_to_secs(r.get("first_t"))
                    t1 = _time_to_secs(r.get("last_t"))
                    if t0 is None or t1 is None:
                        continue
                    if t1 < t0:
                        t1 += 24 * 3600
                    d = int(t1 - t0)
                    if d > 0:
                        durations.append(d)
                    first_departures.append(int(t0))

                runtime_secs = int(sorted(durations)[len(durations) // 2]) if durations else 3600
                runtime_secs = max(300, runtime_secs)

                # Frequency/day window from gtfs_frequencies preferred
                cur.execute(
                    """
                    SELECT f.start_time::text AS start_time, f.end_time::text AS end_time, f.headway_secs::int AS headway_secs
                    FROM gtfs_work.gtfs_frequencies f
                    WHERE f.export_run_id = %s
                      AND f.trip_id = ANY(%s::text[])
                    """,
                    (exp, trip_ids),
                )
                freq_rows = [dict(r) for r in (cur.fetchall() or [])]

                start_secs: List[int] = []
                end_secs: List[int] = []
                headways: List[int] = []
                for r in freq_rows:
                    s0 = _time_to_secs(r.get("start_time"))
                    s1 = _time_to_secs(r.get("end_time"))
                    h = r.get("headway_secs")
                    if s0 is not None:
                        start_secs.append(int(s0))
                    if s1 is not None:
                        end_secs.append(int(s1))
                    if h is not None:
                        try:
                            hv = int(h)
                            if hv > 0:
                                headways.append(hv)
                        except Exception:
                            pass

                if not start_secs and first_departures:
                    start_secs = list(first_departures)
                if not end_secs and first_departures:
                    # infer service end by last departure + runtime
                    end_secs = [int(max(first_departures) + runtime_secs)]

                if not headways and len(first_departures) >= 2:
                    f_sorted = sorted(first_departures)
                    gaps = [f_sorted[i] - f_sorted[i - 1] for i in range(1, len(f_sorted)) if f_sorted[i] - f_sorted[i - 1] > 0]
                    if gaps:
                        gaps_sorted = sorted(gaps)
                        headways = [int(gaps_sorted[len(gaps_sorted) // 2])]

                offpeak_headway_secs = int(sorted(headways)[len(headways) // 2]) if headways else 600
                offpeak_headway_secs = max(60, offpeak_headway_secs)
                peak_headway_secs = max(60, int(min(headways) if headways else max(120, offpeak_headway_secs // 2)))
                use_peak = bool(headways and peak_headway_secs < offpeak_headway_secs)

                def _fmt(sec: int) -> str:
                    if sec < 0:
                        sec = 0
                    sec = sec % (48 * 3600)
                    h = sec // 3600
                    m = (sec % 3600) // 60
                    s = sec % 60
                    return f"{h:02d}:{m:02d}:{s:02d}"

                day_start_secs = int(min(start_secs) if start_secs else 6 * 3600)
                day_end_secs = int(max(end_secs) if end_secs else 22 * 3600)
                if day_end_secs <= day_start_secs:
                    day_end_secs = day_start_secs + max(runtime_secs, 3600)

                suggestion = {
                    "export_run_id": exp,
                    "phase_route_id": rid,
                    "phase_service_route_id": sid or str(phase_row.get("service_route_id") or ""),
                    "matched_gtfs_route_id": gtfs_route_id,
                    "match_score": float(round(best_score, 6)),
                    "match_rank_top5": [
                        {
                            "route_id": str(x.get("route_id") or ""),
                            "route_short_name": str(x.get("route_short_name") or ""),
                            "route_long_name": str(x.get("route_long_name") or ""),
                            "agency_id": str(x.get("agency_id") or ""),
                            "agency_name": str(x.get("agency_name") or ""),
                            "score": float(round(float(x.get("score") or 0.0), 6)),
                        }
                        for x in scored[:5]
                    ],
                    "direction_id_requested": int(did),
                    "direction_id_used": int(used_direction),
                    "direction_fallback": bool(direction_fallback),
                    "strict_direction": bool(strict_direction),
                    "trip_count": int(len(trips)),
                    "service_ids": service_ids,
                    "block_ids": block_ids,
                    "service_name": "weekday_base",
                    "day_flags": day_flags,
                    "day_start_time": _fmt(day_start_secs),
                    "day_end_time": _fmt(day_end_secs),
                    "offpeak_headway_secs": int(offpeak_headway_secs),
                    "use_peak": bool(use_peak),
                    "peak_headway_secs": int(peak_headway_secs),
                    "peak_am_start": "07:00:00",
                    "peak_am_end": "09:00:00",
                    "peak_pm_start": "16:30:00",
                    "peak_pm_end": "19:30:00",
                    "runtime_secs": int(runtime_secs),
                    "n_blocks": int(max(1, n_blocks)),
                    "source": {
                        "matching_context": {
                            "service_route_id_norm": str(service_route_norm),
                            "route_id_norm": str(rid_norm),
                            "route_ref_norm": str(ref_norm),
                            "route_name_norm": str(name_norm),
                            "operator_norm": str(operator_norm),
                        },
                        "route_ref": str(phase_row.get("route_ref") or ""),
                        "route_name": str(phase_row.get("route_name") or ""),
                        "operator_name": str(phase_row.get("operator_name") or ""),
                        "frequency_rows": int(len(freq_rows)),
                        "duration_samples": int(len(durations)),
                        "top_components": dict(best.get("components") or {}),
                    },
                }
                return suggestion

    def list_trip_ids_for_export(
        self,
        export_run_id: str,
        *,
        route_id: Optional[str] = None,
        direction_id: Optional[int] = None,
    ) -> List[str]:
        rid_resolved = self._resolve_gtfs_route_id(route_id) if route_id else ""
        with self._conn() as conn:
            with conn.cursor() as cur:
                if route_id and direction_id is not None:
                    cur.execute(
                        """
                        SELECT DISTINCT trip_id
                        FROM gtfs_work.gtfs_trips
                        WHERE export_run_id = %s
                          AND route_id = %s
                          AND COALESCE(direction_id, 0) = %s
                        ORDER BY trip_id
                        """,
                        (str(export_run_id), str(rid_resolved), int(direction_id)),
                    )
                elif route_id:
                    cur.execute(
                        """
                        SELECT DISTINCT trip_id
                        FROM gtfs_work.gtfs_trips
                        WHERE export_run_id = %s AND route_id = %s
                        ORDER BY trip_id
                        """,
                        (str(export_run_id), str(rid_resolved)),
                    )
                else:
                    cur.execute(
                        """
                        SELECT DISTINCT trip_id
                        FROM gtfs_work.gtfs_trips
                        WHERE export_run_id = %s
                        ORDER BY trip_id
                        """,
                        (str(export_run_id),),
                    )
                return [str(r["trip_id"]) for r in (cur.fetchall() or []) if r.get("trip_id")]

    def list_shape_ids_for_export(
        self,
        export_run_id: str,
        *,
        route_id: Optional[str] = None,
        direction_id: Optional[int] = None,
    ) -> List[str]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                if route_id and direction_id is not None:
                    cur.execute(
                        """
                        SELECT DISTINCT shape_id
                        FROM gtfs_work.gtfs_trips
                        WHERE export_run_id = %s
                          AND route_id = %s
                          AND COALESCE(direction_id, 0) = %s
                          AND shape_id IS NOT NULL
                        ORDER BY shape_id
                        """,
                        (str(export_run_id), str(route_id), int(direction_id)),
                    )
                elif route_id:
                    cur.execute(
                        """
                        SELECT DISTINCT shape_id
                        FROM gtfs_work.gtfs_trips
                        WHERE export_run_id = %s AND route_id = %s AND shape_id IS NOT NULL
                        ORDER BY shape_id
                        """,
                        (str(export_run_id), str(route_id)),
                    )
                else:
                    cur.execute(
                        """
                        SELECT DISTINCT shape_id
                        FROM gtfs_work.gtfs_shapes
                        WHERE export_run_id = %s
                        ORDER BY shape_id
                        """,
                        (str(export_run_id),),
                )
                return [str(r["shape_id"]) for r in (cur.fetchall() or []) if r.get("shape_id")]

    def list_generated_trips_with_departures(
        self,
        *,
        export_run_id: str,
        route_id: str,
        direction_id: Optional[int] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        run_id = str(export_run_id or "").strip()
        rid = str(route_id or "").strip()
        if not run_id or not rid:
            return []
        rid_gtfs = self._resolve_gtfs_route_id(rid)
        with self._conn() as conn:
            with conn.cursor() as cur:
                if direction_id is None:
                    cur.execute(
                        """
                        SELECT
                          t.route_id::text AS route_id,
                          COALESCE(t.direction_id, 0)::int AS direction_id,
                          t.trip_id::text AS trip_id,
                          t.service_id::text AS service_id,
                          COALESCE(t.shape_id, '')::text AS shape_id,
                          COALESCE(t.block_id, '')::text AS block_id,
                          COALESCE(t.trip_headsign, '')::text AS trip_headsign,
                          COALESCE(td.departure_time, '')::text AS departure_time
                        FROM gtfs_work.gtfs_trips t
                        LEFT JOIN gtfs_work.trip_departures td
                          ON td.export_run_id = t.export_run_id
                         AND td.trip_id = t.trip_id
                        WHERE t.export_run_id = %s
                          AND t.route_id = %s
                        ORDER BY COALESCE(t.direction_id, 0), COALESCE(td.departure_time, ''), t.trip_id
                        LIMIT %s
                        """,
                        (run_id, rid_gtfs, int(limit)),
                    )
                else:
                    cur.execute(
                        """
                        SELECT
                          t.route_id::text AS route_id,
                          COALESCE(t.direction_id, 0)::int AS direction_id,
                          t.trip_id::text AS trip_id,
                          t.service_id::text AS service_id,
                          COALESCE(t.shape_id, '')::text AS shape_id,
                          COALESCE(t.block_id, '')::text AS block_id,
                          COALESCE(t.trip_headsign, '')::text AS trip_headsign,
                          COALESCE(td.departure_time, '')::text AS departure_time
                        FROM gtfs_work.gtfs_trips t
                        LEFT JOIN gtfs_work.trip_departures td
                          ON td.export_run_id = t.export_run_id
                         AND td.trip_id = t.trip_id
                        WHERE t.export_run_id = %s
                          AND t.route_id = %s
                          AND COALESCE(t.direction_id, 0) = %s
                        ORDER BY COALESCE(td.departure_time, ''), t.trip_id
                        LIMIT %s
                        """,
                        (run_id, rid_gtfs, int(direction_id), int(limit)),
                    )
                return [dict(r) for r in (cur.fetchall() or [])]

    def list_generated_frequencies(
        self,
        *,
        export_run_id: str,
        route_id: str,
        direction_id: Optional[int] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        run_id = str(export_run_id or "").strip()
        rid = str(route_id or "").strip()
        if not run_id or not rid:
            return []
        rid_gtfs = self._resolve_gtfs_route_id(rid)
        with self._conn() as conn:
            with conn.cursor() as cur:
                if direction_id is None:
                    cur.execute(
                        """
                        SELECT
                          t.route_id::text AS route_id,
                          COALESCE(t.direction_id, 0)::int AS direction_id,
                          f.trip_id::text AS trip_id,
                          f.start_time::text AS start_time,
                          f.end_time::text AS end_time,
                          f.headway_secs::int AS headway_secs,
                          f.exact_times::int AS exact_times
                        FROM gtfs_work.gtfs_frequencies f
                        JOIN gtfs_work.gtfs_trips t
                          ON t.export_run_id = f.export_run_id
                         AND t.trip_id = f.trip_id
                        WHERE f.export_run_id = %s
                          AND t.route_id = %s
                        ORDER BY COALESCE(t.direction_id, 0), f.start_time, f.trip_id
                        LIMIT %s
                        """,
                        (run_id, rid_gtfs, int(limit)),
                    )
                else:
                    cur.execute(
                        """
                        SELECT
                          t.route_id::text AS route_id,
                          COALESCE(t.direction_id, 0)::int AS direction_id,
                          f.trip_id::text AS trip_id,
                          f.start_time::text AS start_time,
                          f.end_time::text AS end_time,
                          f.headway_secs::int AS headway_secs,
                          f.exact_times::int AS exact_times
                        FROM gtfs_work.gtfs_frequencies f
                        JOIN gtfs_work.gtfs_trips t
                          ON t.export_run_id = f.export_run_id
                         AND t.trip_id = f.trip_id
                        WHERE f.export_run_id = %s
                          AND t.route_id = %s
                          AND COALESCE(t.direction_id, 0) = %s
                        ORDER BY f.start_time, f.trip_id
                        LIMIT %s
                        """,
                        (run_id, rid_gtfs, int(direction_id), int(limit)),
                    )
                return [dict(r) for r in (cur.fetchall() or [])]

    def list_generated_stop_times(
        self,
        *,
        export_run_id: str,
        route_id: str,
        direction_id: Optional[int] = None,
        limit: int = 3000,
    ) -> List[Dict[str, Any]]:
        run_id = str(export_run_id or "").strip()
        rid = str(route_id or "").strip()
        if not run_id or not rid:
            return []
        rid_gtfs = self._resolve_gtfs_route_id(rid)
        with self._conn() as conn:
            with conn.cursor() as cur:
                if direction_id is None:
                    cur.execute(
                        """
                        SELECT
                          t.route_id::text AS route_id,
                          COALESCE(t.direction_id, 0)::int AS direction_id,
                          st.trip_id::text AS trip_id,
                          st.stop_sequence::int AS stop_sequence,
                          st.stop_id::text AS stop_id,
                          COALESCE(st.arrival_time::text, '')::text AS arrival_time,
                          COALESCE(st.departure_time::text, '')::text AS departure_time
                        FROM gtfs_work.gtfs_stop_times st
                        JOIN gtfs_work.gtfs_trips t
                          ON t.export_run_id = st.export_run_id
                         AND t.trip_id = st.trip_id
                        WHERE st.export_run_id = %s
                          AND t.route_id = %s
                        ORDER BY COALESCE(t.direction_id, 0), st.trip_id, st.stop_sequence
                        LIMIT %s
                        """,
                        (run_id, rid_gtfs, int(limit)),
                    )
                else:
                    cur.execute(
                        """
                        SELECT
                          t.route_id::text AS route_id,
                          COALESCE(t.direction_id, 0)::int AS direction_id,
                          st.trip_id::text AS trip_id,
                          st.stop_sequence::int AS stop_sequence,
                          st.stop_id::text AS stop_id,
                          COALESCE(st.arrival_time::text, '')::text AS arrival_time,
                          COALESCE(st.departure_time::text, '')::text AS departure_time
                        FROM gtfs_work.gtfs_stop_times st
                        JOIN gtfs_work.gtfs_trips t
                          ON t.export_run_id = st.export_run_id
                         AND t.trip_id = st.trip_id
                        WHERE st.export_run_id = %s
                          AND t.route_id = %s
                          AND COALESCE(t.direction_id, 0) = %s
                        ORDER BY st.trip_id, st.stop_sequence
                        LIMIT %s
                        """,
                        (run_id, rid_gtfs, int(direction_id), int(limit)),
                    )
                return [dict(r) for r in (cur.fetchall() or [])]

    def delete_phase5_route_direction_context(
        self,
        *,
        export_run_id: str,
        route_id: str,
        direction_id: int,
        delete_revision_logs: bool = False,
    ) -> Dict[str, Any]:
        """
        Deletes Phase 5 generated/edit context for one route_id + direction_id in current export.
        Keeps gtfs_routes row intact (no route delete).
        """
        self._ensure_schema()
        run_id = str(export_run_id or "").strip()
        run_uuid = uuid.UUID(run_id) if run_id else None
        rid = str(route_id or "").strip()
        did = int(direction_id)
        if not run_id:
            raise RuntimeError("export_run_id is required")
        if not rid:
            raise RuntimeError("route_id is required")

        deleted: Dict[str, int] = {
            "service_windows": 0,
            "route_schedule_profiles": 0,
            "gtfs_stop_times": 0,
            "gtfs_frequencies": 0,
            "trip_departures": 0,
            "gtfs_trips": 0,
            "revision_events": 0,
        }

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT profile_id::text AS profile_id
                    FROM gtfs_work.route_schedule_profiles
                    WHERE route_id = %s
                      AND COALESCE(direction_id, 0) = %s
                    """,
                    (rid, did),
                )
                pids = [str(x.get("profile_id") or "") for x in (cur.fetchall() or []) if str(x.get("profile_id") or "")]
                if pids:
                    cur.execute(
                        "DELETE FROM gtfs_work.service_windows WHERE profile_id = ANY(%s::uuid[])",
                        (pids,),
                    )
                    deleted["service_windows"] = int(cur.rowcount or 0)
                cur.execute(
                    """
                    DELETE FROM gtfs_work.route_schedule_profiles
                    WHERE route_id = %s
                      AND COALESCE(direction_id, 0) = %s
                    """,
                    (rid, did),
                )
                deleted["route_schedule_profiles"] = int(cur.rowcount or 0)

                cur.execute(
                    """
                    SELECT trip_id::text AS trip_id
                    FROM gtfs_work.gtfs_trips
                    WHERE export_run_id = %s::uuid
                      AND route_id = %s
                      AND COALESCE(direction_id, 0) = %s
                    """,
                    (run_uuid, rid, did),
                )
                trip_ids = [str(x.get("trip_id") or "") for x in (cur.fetchall() or []) if str(x.get("trip_id") or "")]

                if trip_ids:
                    cur.execute(
                        """
                        DELETE FROM gtfs_work.gtfs_stop_times
                        WHERE export_run_id = %s::uuid
                          AND trip_id = ANY(%s::text[])
                        """,
                        (run_uuid, trip_ids),
                    )
                    deleted["gtfs_stop_times"] = int(cur.rowcount or 0)

                    cur.execute(
                        """
                        DELETE FROM gtfs_work.gtfs_frequencies
                        WHERE export_run_id = %s::uuid
                          AND trip_id = ANY(%s::text[])
                        """,
                        (run_uuid, trip_ids),
                    )
                    deleted["gtfs_frequencies"] = int(cur.rowcount or 0)

                    try:
                        cur.execute(
                            """
                            DELETE FROM gtfs_work.trip_departures
                            WHERE export_run_id = %s::uuid
                              AND trip_id = ANY(%s::text[])
                            """,
                            (run_uuid, trip_ids),
                        )
                        deleted["trip_departures"] = int(cur.rowcount or 0)
                    except Exception:
                        deleted["trip_departures"] = 0

                cur.execute(
                    """
                    DELETE FROM gtfs_work.gtfs_trips
                    WHERE export_run_id = %s::uuid
                      AND route_id = %s
                      AND COALESCE(direction_id, 0) = %s
                    """,
                    (run_uuid, rid, did),
                )
                deleted["gtfs_trips"] = int(cur.rowcount or 0)

                if bool(delete_revision_logs):
                    cur.execute(
                        """
                        DELETE FROM gtfs_work.revision_events
                        WHERE export_run_id = %s::uuid
                          AND (
                            row_key::text ILIKE %s
                            OR before_row::text ILIKE %s
                            OR after_row::text ILIKE %s
                          )
                        """,
                        (run_uuid, f"%{rid}%", f"%{rid}%", f"%{rid}%"),
                    )
                    deleted["revision_events"] = int(cur.rowcount or 0)

                cur.execute(
                    """
                    INSERT INTO gtfs_work.revision_events
                      (export_run_id, event_type, table_name, row_key, before_row, after_row, edited_by)
                    VALUES
                      (%s::uuid, 'route_direction_cleanup_delete', 'phase5_route_direction_context', %s::jsonb, %s::jsonb, %s::jsonb, %s)
                    """,
                    (
                        run_uuid,
                        json.dumps({"route_id": rid, "direction_id": did}, ensure_ascii=False),
                        json.dumps({"scope": "phase5_generated_context_only"}, ensure_ascii=False),
                        json.dumps({"deleted": deleted}, ensure_ascii=False),
                        self._actor(),
                    ),
                )

        return {
            "ok": True,
            "export_run_id": run_id,
            "route_id": rid,
            "direction_id": did,
            "deleted": deleted,
            "note": "Route row was not deleted.",
        }

    def delete_phase5_all_route_context(
        self,
        *,
        export_run_id: str,
        delete_revision_logs: bool = False,
        verified_only: bool = False,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        """
        Bulk cleanup for Phase 5 route context:
        - iterates all route_id + direction_id inputs
        - applies the same cleanup as delete_phase5_route_direction_context
        - does NOT delete route_prod route rows
        """
        self._ensure_schema()
        run_id = str(export_run_id or "").strip()
        if not run_id:
            raise RuntimeError("export_run_id is required")

        rows = self.list_service_direction_inputs(
            verified_only=bool(verified_only),
            limit=int(limit),
        )
        pairs: List[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        for r in (rows or []):
            rid = str(r.get("route_id") or "").strip()
            did = int(r.get("direction_id") or 0)
            if not rid:
                continue
            k = (rid, did)
            if k in seen:
                continue
            seen.add(k)
            pairs.append(k)

        summary_deleted: Dict[str, int] = {
            "service_windows": 0,
            "route_schedule_profiles": 0,
            "gtfs_stop_times": 0,
            "gtfs_frequencies": 0,
            "trip_departures": 0,
            "gtfs_trips": 0,
            "revision_events": 0,
        }
        details: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []

        for rid, did in pairs:
            try:
                out = self.delete_phase5_route_direction_context(
                    export_run_id=run_id,
                    route_id=rid,
                    direction_id=int(did),
                    delete_revision_logs=bool(delete_revision_logs),
                )
                details.append(out)
                d = dict(out.get("deleted") or {})
                for k in summary_deleted.keys():
                    summary_deleted[k] += int(d.get(k) or 0)
            except Exception as e:
                failures.append(
                    {
                        "route_id": rid,
                        "direction_id": int(did),
                        "error": str(e),
                    }
                )

        return {
            "ok": len(failures) == 0,
            "export_run_id": run_id,
            "pairs_total": len(pairs),
            "pairs_cleaned": len(details),
            "pairs_failed": len(failures),
            "deleted_total": summary_deleted,
            "details": details,
            "failures": failures,
            "note": "Route rows were not deleted.",
        }

    def list_stops_for_export(
        self,
        export_run_id: str,
        *,
        limit: int = 5000,
        served_only: bool = True,
    ) -> List[Dict[str, Any]]:
        if served_only:
            return list_served_stops(str(export_run_id), limit=int(limit))
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM gtfs_work.gtfs_stops
                    WHERE export_run_id = %s
                    ORDER BY stop_name, stop_id
                    LIMIT %s
                    """,
                    (str(export_run_id), int(limit)),
                )
                return list(cur.fetchall() or [])

    def list_orphan_stops_for_export(
        self,
        export_run_id: str,
        *,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        return list_orphan_stops(str(export_run_id), limit=int(limit))

    def get_stop_coverage_summary(
        self,
        export_run_id: str,
        *,
        include_duplicate_metrics: bool = True,
    ) -> Dict[str, Any]:
        return get_stop_coverage_metrics(
            str(export_run_id),
            include_duplicate_metrics=bool(include_duplicate_metrics),
        )

    def list_stop_times_by_trip(self, export_run_id: str, trip_id: str, *, limit: int = 3000) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM gtfs_work.gtfs_stop_times
                    WHERE export_run_id = %s AND trip_id = %s
                    ORDER BY stop_sequence
                    LIMIT %s
                    """,
                    (str(export_run_id), str(trip_id), int(limit)),
                )
                return list(cur.fetchall() or [])

    def get_representative_trip_id(self, export_run_id: str, route_id: str, direction_id: int) -> Optional[str]:
        rep = self._get_representative_trip_for_route_direction(
            str(export_run_id),
            str(route_id),
            int(direction_id),
        )
        tid = str(rep.get("trip_id") or "").strip()
        return tid or None

    def list_trip_stops_with_sequence(self, export_run_id: str, trip_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      st.trip_id::text AS trip_id,
                      st.stop_sequence::int AS stop_sequence,
                      st.stop_id::text AS stop_id,
                      st.arrival_time::text AS arrival_time,
                      st.departure_time::text AS departure_time,
                      s.stop_name::text AS stop_name,
                      s.stop_lat::float8 AS stop_lat,
                      s.stop_lon::float8 AS stop_lon
                    FROM gtfs_work.gtfs_stop_times st
                    JOIN gtfs_work.gtfs_stops s
                      ON s.export_run_id = st.export_run_id
                     AND s.stop_id = st.stop_id
                    WHERE st.export_run_id = %s
                      AND st.trip_id = %s
                    ORDER BY st.stop_sequence
                    """,
                    (str(export_run_id), str(trip_id)),
                )
                return [dict(r) for r in (cur.fetchall() or [])]

    def reorder_trip_stop_sequence(
        self,
        *,
        export_run_id: str,
        trip_id: str,
        current_sequence: int,
        new_sequence: int,
        mode: str = "shift",
    ) -> Dict[str, Any]:
        """
        Reorders one stop_sequence in a trip.
        mode:
          - shift: move selected row to new position and shift range.
          - swap: swap selected row with row at new_sequence.
        """
        rows = self.list_stop_times_by_trip(str(export_run_id), str(trip_id), limit=20000)
        if not rows:
            raise RuntimeError("Trip has no stop_times.")

        rows = sorted(rows, key=lambda r: int(r.get("stop_sequence") or 0))
        n = len(rows)
        cur_seq = int(current_sequence)
        new_seq = max(1, min(int(new_sequence), n))

        cur_idx = next((i for i, r in enumerate(rows) if int(r.get("stop_sequence") or 0) == cur_seq), None)
        if cur_idx is None:
            raise RuntimeError("Current stop_sequence was not found in trip.")

        changed = 0
        if mode == "swap":
            tgt_idx = next((i for i, r in enumerate(rows) if int(r.get("stop_sequence") or 0) == new_seq), None)
            if tgt_idx is None:
                raise RuntimeError("Target stop_sequence was not found in trip.")
            if tgt_idx != cur_idx:
                rows[cur_idx], rows[tgt_idx] = rows[tgt_idx], rows[cur_idx]
                changed = 1
        else:
            item = rows.pop(cur_idx)
            rows.insert(new_seq - 1, item)
            changed = 1

        if changed == 0:
            return {
                "export_run_id": str(export_run_id),
                "trip_id": str(trip_id),
                "updated": 0,
                "message": "No reorder needed.",
            }

        old_to_new: Dict[int, int] = {}
        for i, r in enumerate(rows, start=1):
            old_seq = int(r.get("stop_sequence") or 0)
            old_to_new[old_seq] = i

        offset = 1000000
        with self._conn() as conn:
            with conn.cursor() as cur:
                # move to temporary sequence range first to avoid PK collisions
                cur.execute(
                    """
                    UPDATE gtfs_work.gtfs_stop_times
                    SET stop_sequence = stop_sequence + %s
                    WHERE export_run_id = %s
                      AND trip_id = %s
                    """,
                    (int(offset), str(export_run_id), str(trip_id)),
                )
                for old_seq, new_seq_val in old_to_new.items():
                    cur.execute(
                        """
                        UPDATE gtfs_work.gtfs_stop_times
                        SET stop_sequence = %s
                        WHERE export_run_id = %s
                          AND trip_id = %s
                          AND stop_sequence = %s
                        """,
                        (int(new_seq_val), str(export_run_id), str(trip_id), int(old_seq + offset)),
                    )

        return {
            "export_run_id": str(export_run_id),
            "trip_id": str(trip_id),
            "updated": int(len(old_to_new)),
            "mode": str(mode),
            "from_sequence": int(cur_seq),
            "to_sequence": int(new_seq),
        }

    def delete_trip_stop_point(
        self,
        *,
        export_run_id: str,
        trip_id: str,
        stop_sequence: int,
    ) -> Dict[str, Any]:
        rows = self.list_stop_times_by_trip(str(export_run_id), str(trip_id), limit=20000)
        if not rows:
            raise RuntimeError("Trip has no stop_times.")
        target = int(stop_sequence)
        before_count = int(len(rows))
        kept = [r for r in rows if int(r.get("stop_sequence") or 0) != target]
        if len(kept) == len(rows):
            raise RuntimeError("stop_sequence not found in trip.")
        if not kept:
            raise RuntimeError("Cannot delete last stop of trip.")

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM gtfs_work.gtfs_stop_times
                    WHERE export_run_id = %s
                      AND trip_id = %s
                    """,
                    (str(export_run_id), str(trip_id)),
                )
                for i, r in enumerate(sorted(kept, key=lambda x: int(x.get("stop_sequence") or 0)), start=1):
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_stop_times
                          (export_run_id, trip_id, arrival_time, departure_time, stop_id, stop_sequence)
                        VALUES (%s::uuid, %s, %s, %s, %s, %s)
                        """,
                        (
                            str(export_run_id),
                            str(trip_id),
                            r.get("arrival_time"),
                            r.get("departure_time"),
                            r.get("stop_id"),
                            int(i),
                        ),
                    )
                cur.execute(
                    """
                    SELECT COUNT(*)::int AS n
                    FROM gtfs_work.gtfs_stop_times
                    WHERE export_run_id = %s
                      AND trip_id = %s
                    """,
                    (str(export_run_id), str(trip_id)),
                )
                after_count = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute(
                    """
                    SELECT COUNT(*)::int AS n
                    FROM gtfs_work.gtfs_stop_times
                    WHERE export_run_id = %s
                      AND trip_id = %s
                      AND stop_sequence = %s
                    """,
                    (str(export_run_id), str(trip_id), int(target)),
                )
                still_has_target = int((cur.fetchone() or {}).get("n") or 0) > 0
                if still_has_target:
                    raise RuntimeError("Delete verification failed: target stop_sequence still exists in DB.")
        return {
            "export_run_id": str(export_run_id),
            "trip_id": str(trip_id),
            "deleted_stop_sequence": int(target),
            "remaining_stops": int(len(kept)),
            "before_count": int(before_count),
            "after_count": int(after_count),
        }

    def delete_shape_point(
        self,
        *,
        export_run_id: str,
        shape_id: str,
        shape_pt_sequence: Any,
    ) -> Dict[str, Any]:
        sid = str(shape_id or "").strip()
        if not sid:
            raise RuntimeError("shape_id is required.")
        rows = self.list_shape_points(str(export_run_id), sid, limit=20000)
        if not rows:
            raise RuntimeError("Shape has no points.")

        pick = str(shape_pt_sequence)
        before_count = int(len(rows))
        kept = [r for r in rows if str(r.get("shape_pt_sequence")) != pick]
        if len(kept) == len(rows):
            raise RuntimeError("shape_pt_sequence not found.")
        if len(kept) < 2:
            raise RuntimeError("Cannot keep a shape with less than 2 points.")

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM gtfs_work.gtfs_shapes WHERE export_run_id = %s AND shape_id = %s",
                    (str(export_run_id), sid),
                )
                cum = 0.0
                prev: Optional[List[float]] = None
                for i, r in enumerate(kept, start=1):
                    lon = float(r.get("shape_pt_lon") or 0.0)
                    lat = float(r.get("shape_pt_lat") or 0.0)
                    if prev is not None:
                        cum += _haversine_m(float(prev[0]), float(prev[1]), lon, lat)
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_shapes
                          (export_run_id, shape_id, shape_pt_lat, shape_pt_lon, shape_pt_sequence, shape_dist_traveled)
                        VALUES (%s::uuid, %s, %s, %s, %s, %s)
                        """,
                        (str(export_run_id), sid, lat, lon, int(i), float(cum)),
                    )
                    prev = [lon, lat]
                cur.execute(
                    """
                    SELECT COUNT(*)::int AS n
                    FROM gtfs_work.gtfs_shapes
                    WHERE export_run_id = %s
                      AND shape_id = %s
                    """,
                    (str(export_run_id), sid),
                )
                after_count = int((cur.fetchone() or {}).get("n") or 0)
                if after_count != len(kept):
                    raise RuntimeError("Delete verification failed: shape point count mismatch in DB.")

        return {
            "export_run_id": str(export_run_id),
            "shape_id": sid,
            "deleted_shape_pt_sequence": pick,
            "remaining_points": int(len(kept)),
            "before_count": int(before_count),
            "after_count": int(after_count),
        }

    def list_shape_points(self, export_run_id: str, shape_id: str, *, limit: int = 10000) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM gtfs_work.gtfs_shapes
                    WHERE export_run_id = %s AND shape_id = %s
                    ORDER BY
                      CASE
                        WHEN (shape_pt_sequence::text ~ '^-?[0-9]+(\\.[0-9]+)?$')
                          THEN (shape_pt_sequence::text)::double precision
                        ELSE NULL
                      END,
                      CASE
                        WHEN shape_dist_traveled IS NULL THEN NULL
                        ELSE shape_dist_traveled::double precision
                      END,
                      shape_pt_sequence::text
                    LIMIT %s
                    """,
                    (str(export_run_id), str(shape_id), int(limit)),
                )
                return list(cur.fetchall() or [])

    def update_stop_and_optionally_sync_node(
        self,
        *,
        export_run_id: str,
        stop_id: str,
        stop_name: str,
        stop_lat: float,
        stop_lon: float,
        sync_node_prod: bool = False,
    ) -> Dict[str, Any]:
        out = self.update_gtfs_row(
            "gtfs_stops",
            export_run_id,
            {
                "stop_id": str(stop_id),
                "stop_name": str(stop_name),
                "stop_lat": float(stop_lat),
                "stop_lon": float(stop_lon),
            },
        )
        synced = False
        if sync_node_prod:
            from phase3_routes.services.stop_quality import (
                StopTreatmentInput,
                treat_stop,
            )
            try:
                with self._conn() as conn:
                    res = treat_stop(
                        StopTreatmentInput(
                            operation="snap_align",
                            caller="phase5_gtfs.export_sync",
                            node_id=str(stop_id),
                            proposed_name=str(stop_name),
                            proposed_lat=float(stop_lat),
                            proposed_lon=float(stop_lon),
                        ),
                        conn,
                    )
                    synced = bool(res.success)
                    if synced:
                        conn.commit()
                    else:
                        conn.rollback()
            except Exception:
                synced = False
        return {"row": out, "synced_node_prod": synced}

    def get_export_map_payload(
        self,
        export_run_id: str,
        *,
        route_id: Optional[str] = None,
        direction_id: Optional[int] = None,
        include_all_shapes: bool = False,
        served_stops_only: bool = True,
    ) -> Dict[str, Any]:
        """
        Returns map-ready shapes + stops from uploaded/generated GTFS rows.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                representative_trip_id: Optional[str] = None
                representative_shape_id: Optional[str] = None
                representative_stop_path: List[List[float]] = []

                if route_id and direction_id is not None:
                    cur.execute(
                        """
                        SELECT trip_id::text AS trip_id, shape_id::text AS shape_id
                        FROM gtfs_work.gtfs_trips
                        WHERE export_run_id = %s
                          AND route_id = %s
                          AND COALESCE(direction_id, 0) = %s
                        ORDER BY trip_id
                        LIMIT 1
                        """,
                        (str(export_run_id), str(route_id), int(direction_id)),
                    )
                    rep = dict(cur.fetchone() or {})
                    representative_trip_id = str(rep.get("trip_id") or "") or None
                    representative_shape_id = str(rep.get("shape_id") or "") or None
                    if representative_trip_id:
                        cur.execute(
                            """
                            SELECT
                              s.stop_lon::float8 AS lon,
                              s.stop_lat::float8 AS lat,
                              st.stop_sequence::int AS stop_sequence
                            FROM gtfs_work.gtfs_stop_times st
                            JOIN gtfs_work.gtfs_stops s
                              ON s.export_run_id = st.export_run_id
                             AND s.stop_id = st.stop_id
                            WHERE st.export_run_id = %s
                              AND st.trip_id = %s
                            ORDER BY st.stop_sequence
                            """,
                            (str(export_run_id), str(representative_trip_id)),
                        )
                        rep_pts = cur.fetchall() or []
                        representative_stop_path = [
                            [float(r["lon"]), float(r["lat"])]
                            for r in rep_pts
                            if r.get("lon") is not None and r.get("lat") is not None
                        ]

                if route_id and direction_id is not None:
                    if include_all_shapes:
                        cur.execute(
                            """
                            SELECT DISTINCT t.shape_id
                            FROM gtfs_work.gtfs_trips t
                            WHERE t.export_run_id = %s
                              AND t.route_id = %s
                              AND COALESCE(t.direction_id, 0) = %s
                              AND t.shape_id IS NOT NULL
                            ORDER BY t.shape_id
                            """,
                            (str(export_run_id), str(route_id), int(direction_id)),
                        )
                        shape_ids = [str(r["shape_id"]) for r in (cur.fetchall() or []) if r.get("shape_id")]
                    else:
                        shape_ids = [representative_shape_id] if representative_shape_id else []
                elif route_id:
                    if include_all_shapes:
                        cur.execute(
                            """
                            SELECT DISTINCT t.shape_id
                            FROM gtfs_work.gtfs_trips t
                            WHERE t.export_run_id = %s
                              AND t.route_id = %s
                              AND t.shape_id IS NOT NULL
                            ORDER BY t.shape_id
                            """,
                            (str(export_run_id), str(route_id)),
                        )
                        shape_ids = [str(r["shape_id"]) for r in (cur.fetchall() or []) if r.get("shape_id")]
                    else:
                        cur.execute(
                            """
                            SELECT shape_id::text
                            FROM gtfs_work.gtfs_trips
                            WHERE export_run_id = %s
                              AND route_id = %s
                              AND shape_id IS NOT NULL
                            GROUP BY shape_id
                            ORDER BY COUNT(*) DESC, shape_id
                            LIMIT 1
                            """,
                            (str(export_run_id), str(route_id)),
                        )
                        r = dict(cur.fetchone() or {})
                        shape_ids = [str(r.get("shape_id") or "")] if r.get("shape_id") else []
                else:
                    cur.execute(
                        """
                        SELECT DISTINCT shape_id
                        FROM gtfs_work.gtfs_shapes
                        WHERE export_run_id = %s
                        ORDER BY shape_id
                        """,
                        (str(export_run_id),),
                    )
                    shape_ids = [str(r["shape_id"]) for r in (cur.fetchall() or []) if r.get("shape_id")]

                shapes: List[Dict[str, Any]] = []
                for sid in shape_ids:
                    cur.execute(
                        """
                        SELECT shape_pt_lon, shape_pt_lat, shape_pt_sequence
                        FROM gtfs_work.gtfs_shapes
                        WHERE export_run_id = %s AND shape_id = %s
                        """,
                        (str(export_run_id), sid),
                    )
                    pts = cur.fetchall() or []

                    def _seq_key(row: Dict[str, Any]) -> int:
                        raw = row.get("shape_pt_sequence")
                        try:
                            return int(raw)
                        except Exception:
                            try:
                                return int(float(raw))
                            except Exception:
                                return 0

                    pts_sorted = sorted((dict(p) for p in pts), key=_seq_key)
                    path = [[float(p["shape_pt_lon"]), float(p["shape_pt_lat"])] for p in pts_sorted]
                    if len(path) >= 2:
                        shapes.append({"shape_id": sid, "path": path})

                # For focused route+direction view, default to OTP-like trip path:
                # the exact stop_times sequence of the representative trip.
                if (
                    route_id
                    and direction_id is not None
                    and not include_all_shapes
                    and len(representative_stop_path) >= 2
                ):
                    shapes = [
                        {
                            "shape_id": f"trip_path:{representative_trip_id or 'unknown'}",
                            "path": representative_stop_path,
                        }
                    ]

                stops: List[Dict[str, Any]] = []
                if route_id and direction_id is not None:
                    if representative_trip_id:
                        cur.execute(
                            """
                            SELECT s.*
                            FROM gtfs_work.gtfs_stop_times st
                            JOIN gtfs_work.gtfs_stops s
                              ON s.export_run_id = st.export_run_id
                             AND s.stop_id = st.stop_id
                            WHERE st.export_run_id = %s
                              AND st.trip_id = %s
                            ORDER BY st.stop_sequence
                            """,
                            (str(export_run_id), str(representative_trip_id)),
                        )
                        stops = [dict(x) for x in (cur.fetchall() or [])]
                    else:
                        cur.execute(
                            """
                            SELECT DISTINCT s.*
                            FROM gtfs_work.gtfs_stops s
                            JOIN gtfs_work.gtfs_stop_times st
                              ON st.export_run_id = s.export_run_id
                             AND st.stop_id = s.stop_id
                            JOIN gtfs_work.gtfs_trips t
                              ON t.export_run_id = st.export_run_id
                             AND t.trip_id = st.trip_id
                            WHERE s.export_run_id = %s
                              AND t.route_id = %s
                              AND COALESCE(t.direction_id, 0) = %s
                            ORDER BY s.stop_name, s.stop_id
                            """,
                            (str(export_run_id), str(route_id), int(direction_id)),
                        )
                        stops = [dict(x) for x in (cur.fetchall() or [])]
                elif route_id:
                    cur.execute(
                        """
                        SELECT DISTINCT s.*
                        FROM gtfs_work.gtfs_stops s
                        JOIN gtfs_work.gtfs_stop_times st
                          ON st.export_run_id = s.export_run_id
                         AND st.stop_id = s.stop_id
                        JOIN gtfs_work.gtfs_trips t
                          ON t.export_run_id = st.export_run_id
                         AND t.trip_id = st.trip_id
                        WHERE s.export_run_id = %s
                          AND t.route_id = %s
                        ORDER BY s.stop_name, s.stop_id
                        """,
                        (str(export_run_id), str(route_id)),
                    )
                    stops = [dict(x) for x in (cur.fetchall() or [])]
                else:
                    if served_stops_only:
                        stops = list_served_stops(str(export_run_id), limit=7000)
                    else:
                        cur.execute(
                            """
                            SELECT *
                            FROM gtfs_work.gtfs_stops
                            WHERE export_run_id = %s
                            ORDER BY stop_name, stop_id
                            LIMIT 7000
                            """,
                            (str(export_run_id),),
                        )
                        stops = [dict(x) for x in (cur.fetchall() or [])]

        return {
            "export_run_id": str(export_run_id),
            "route_id": (str(route_id) if route_id else None),
            "direction_id": (int(direction_id) if direction_id is not None else None),
            "representative_trip_id": representative_trip_id,
            "representative_shape_id": representative_shape_id,
            "include_all_shapes": bool(include_all_shapes),
            "served_stops_only": bool(served_stops_only),
            "shapes": shapes,
            "stops": stops,
        }

    def get_packaged_zip_info(self, export_run_id: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "export_run_id": str(export_run_id),
            "zip_path": None,
            "exists": False,
            "size_bytes": 0,
            "members": [],
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT output_zip_path
                    FROM gtfs_work.export_runs
                    WHERE export_run_id = %s
                    LIMIT 1
                    """,
                    (str(export_run_id),),
                )
                row = dict(cur.fetchone() or {})
        zip_path = str(row.get("output_zip_path") or "").strip()
        if not zip_path:
            return out
        p = Path(zip_path)
        out["zip_path"] = str(p)
        out["exists"] = p.exists()
        if not p.exists():
            return out
        out["size_bytes"] = int(p.stat().st_size)
        try:
            with zipfile.ZipFile(p, "r") as zf:
                out["members"] = sorted(zf.namelist())
        except Exception:
            out["members"] = []
        return out

    # ----------------------------------------
    # GTFS-native Valhalla (no Phase 3 listing dependency)
    # ----------------------------------------
    def list_gtfs_valhalla_presets(self) -> List[Dict[str, Any]]:
        return [
            {"name": "bus_low_highways", "costing_options": {"bus": {"use_highways": 0.05, "use_tolls": 0.0}}},
            {"name": "bus_balanced", "costing_options": {"bus": {"use_highways": 0.20, "use_tolls": 0.0}}},
            {"name": "bus_more_highways", "costing_options": {"bus": {"use_highways": 0.35, "use_tolls": 0.0}}},
        ]

    def _get_representative_trip_for_route_direction(
        self,
        export_run_id: str,
        route_id: str,
        direction_id: int,
    ) -> Dict[str, Any]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT trip_id::text AS trip_id, shape_id::text AS shape_id
                    FROM gtfs_work.gtfs_trips
                    WHERE export_run_id = %s
                      AND route_id = %s
                      AND COALESCE(direction_id, 0) = %s
                    ORDER BY trip_id
                    LIMIT 1
                    """,
                    (str(export_run_id), str(route_id), int(direction_id)),
                )
                return dict(cur.fetchone() or {})

    def _get_trip_stop_points(self, export_run_id: str, trip_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      st.stop_sequence::int AS stop_sequence,
                      st.stop_id::text AS stop_id,
                      s.stop_name::text AS stop_name,
                      s.stop_lon::float8 AS lon,
                      s.stop_lat::float8 AS lat
                    FROM gtfs_work.gtfs_stop_times st
                    JOIN gtfs_work.gtfs_stops s
                      ON s.export_run_id = st.export_run_id
                     AND s.stop_id = st.stop_id
                    WHERE st.export_run_id = %s
                      AND st.trip_id = %s
                    ORDER BY st.stop_sequence
                    """,
                    (str(export_run_id), str(trip_id)),
                )
                return [dict(r) for r in (cur.fetchall() or [])]

    def build_gtfs_valhalla_candidates(
        self,
        *,
        export_run_id: str,
        route_id: str,
        direction_id: int,
        preset_names: Optional[List[str]] = None,
        timeout_s: int = 60,
    ) -> Dict[str, Any]:
        presets = self.list_gtfs_valhalla_presets()
        preset_map = {str(p["name"]): p for p in presets}
        names = [n for n in (preset_names or ["bus_balanced"]) if n in preset_map]
        if not names:
            names = ["bus_balanced"]

        rep = self._get_representative_trip_for_route_direction(export_run_id, route_id, int(direction_id))
        trip_id = str(rep.get("trip_id") or "")
        shape_id = str(rep.get("shape_id") or "")
        if not trip_id:
            raise RuntimeError("No trip found for selected route_id + direction_id.")

        stop_rows = self._get_trip_stop_points(export_run_id, trip_id)
        if len(stop_rows) < 2:
            raise RuntimeError("Representative trip has <2 stops; cannot run Valhalla.")
        stop_path = [[float(r["lon"]), float(r["lat"])] for r in stop_rows if r.get("lon") is not None and r.get("lat") is not None]
        if len(stop_path) < 2:
            raise RuntimeError("Representative trip stop coordinates are invalid.")

        candidates: List[Dict[str, Any]] = []
        for nm in names:
            preset = preset_map[nm]
            pts = valhalla_route(stop_path, costing_options=dict(preset.get("costing_options") or {}), timeout_s=int(timeout_s))
            raw_path = [[float(lon), float(lat)] for lon, lat in pts]
            path = _clip_to_terminal_stops(raw_path, stop_path[0], stop_path[-1])
            if len(path) < 2:
                continue

            dists = [_point_to_polyline_vertex_dist_m(float(s["lon"]), float(s["lat"]), path) for s in stop_rows]
            avg_d = float(sum(dists) / len(dists)) if dists else 1e9
            max_d = float(max(dists)) if dists else 1e9
            length_m = _polyline_length_m(path)
            score = -((avg_d * 5.0) + (max_d * 1.0) + (length_m * 0.0005))

            candidates.append(
                {
                    "candidate_id": str(uuid.uuid4()),
                    "preset": nm,
                    "score": float(score),
                    "avg_stop_dist_m": float(avg_d),
                    "max_stop_dist_m": float(max_d),
                    "length_m": float(length_m),
                    "n_points": int(len(path)),
                    "shape_points": path,
                }
            )

        candidates.sort(key=lambda x: float(x.get("score") or -1e18), reverse=True)
        return {
            "export_run_id": str(export_run_id),
            "route_id": str(route_id),
            "direction_id": int(direction_id),
            "trip_id": trip_id,
            "shape_id": shape_id or None,
            "stop_points": stop_path,
            "candidates": candidates,
        }

    def apply_gtfs_valhalla_candidate(
        self,
        *,
        export_run_id: str,
        shape_id: str,
        shape_points: List[List[float]],
    ) -> Dict[str, Any]:
        sid = str(shape_id or "").strip()
        if not sid:
            raise RuntimeError("shape_id is required to apply candidate.")
        pts = shape_points or []
        if len(pts) < 2:
            raise RuntimeError("shape_points must have at least 2 points.")

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM gtfs_work.gtfs_shapes WHERE export_run_id = %s AND shape_id = %s",
                    (str(export_run_id), sid),
                )
                cum = 0.0
                prev: Optional[List[float]] = None
                inserted = 0
                for i, p in enumerate(pts, start=1):
                    lon = float(p[0])
                    lat = float(p[1])
                    if prev is not None:
                        cum += _haversine_m(float(prev[0]), float(prev[1]), lon, lat)
                    cur.execute(
                        """
                        INSERT INTO gtfs_work.gtfs_shapes
                          (export_run_id, shape_id, shape_pt_lat, shape_pt_lon, shape_pt_sequence, shape_dist_traveled)
                        VALUES (%s::uuid, %s, %s, %s, %s, %s)
                        """,
                        (str(export_run_id), sid, lat, lon, int(i), float(cum)),
                    )
                    prev = [lon, lat]
                    inserted += 1
        return {
            "export_run_id": str(export_run_id),
            "shape_id": sid,
            "inserted_points": int(inserted),
        }

    def get_route_direction_summary(self, export_run_id: str, route_id: str) -> Dict[str, Any]:
        """
        Detects available directions for a route in this export run and their counterpart.
        """
        out: Dict[str, Any] = {
            "route_id": str(route_id),
            "directions": [],
            "counterpart_of_0": None,
            "counterpart_of_1": None,
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      COALESCE(direction_id, 0)::int AS direction_id,
                      COUNT(DISTINCT trip_id)::int AS n_trips
                    FROM gtfs_work.gtfs_trips
                    WHERE export_run_id = %s
                      AND route_id = %s
                    GROUP BY COALESCE(direction_id, 0)
                    ORDER BY direction_id
                    """,
                    (str(export_run_id), str(route_id)),
                )
                dir_rows = list(cur.fetchall() or [])
                directions = [int(r["direction_id"]) for r in dir_rows]
                out["directions"] = [{"direction_id": int(r["direction_id"]), "n_trips": int(r["n_trips"])} for r in dir_rows]

                if 0 in directions and 1 in directions:
                    out["counterpart_of_0"] = 1
                    out["counterpart_of_1"] = 0
                elif 0 in directions:
                    out["counterpart_of_0"] = None
                elif 1 in directions:
                    out["counterpart_of_1"] = None

                # shape ids by direction
                cur.execute(
                    """
                    SELECT
                      COALESCE(direction_id, 0)::int AS direction_id,
                      ARRAY_AGG(DISTINCT shape_id) FILTER (WHERE shape_id IS NOT NULL) AS shape_ids
                    FROM gtfs_work.gtfs_trips
                    WHERE export_run_id = %s
                      AND route_id = %s
                    GROUP BY COALESCE(direction_id, 0)
                    ORDER BY direction_id
                    """,
                    (str(export_run_id), str(route_id)),
                )
                out["shape_ids_by_direction"] = [
                    {
                        "direction_id": int(r["direction_id"]),
                        "shape_ids": [str(x) for x in (r.get("shape_ids") or []) if x],
                    }
                    for r in (cur.fetchall() or [])
                ]
        return out
