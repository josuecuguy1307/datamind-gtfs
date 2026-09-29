"""
path_inference — Valhalla path caching + polyline geometry helpers.

Responsibilities (from hades-path-aware-synthesis §5 + §6):

- Compute or cache the route's inferred path polyline
  (``route_prod.routes.inferred_path_polyline``).
- Project arbitrary coords to a polyline (for stage 3a / 3d).
- Find a named-road sub-segment of a polyline (stage 3b).
- Find the intersection of two named roads along a polyline (stage 3c).

All HTTP calls have a 30-second timeout. Infrastructure failures raise
``PathInferenceError``; "no data" cases return ``None`` with a logged
warning — stages handle that themselves.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import polyline as _polyline_codec
import requests
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

from datamind_console.persistence import patch_route_prod_fields

log = logging.getLogger(__name__)

_SOURCE_TYPE = "path_inference"
_PIPELINE_VERSION_CACHE = "synthesis.path_inference.cache_path"
_PIPELINE_VERSION_INVALIDATE = "synthesis.path_inference.invalidate_path"

# ---------------------------------------------------------------------------
# constants + errors
# ---------------------------------------------------------------------------

DEFAULT_VALHALLA_URL = "http://localhost:8003"
DEFAULT_TIMEOUT_S = 30
CACHE_FRESHNESS_DAYS = 7
POLYLINE_PRECISION = 6

PATH_SOURCE_OSM = "osm_relation_geometry"
PATH_SOURCE_VALHALLA_ANCHORS = "valhalla_with_anchors"
PATH_SOURCE_VALHALLA_TERMINUS = "valhalla_terminus_only"

VALID_PATH_SOURCES = frozenset({
    PATH_SOURCE_OSM,
    PATH_SOURCE_VALHALLA_ANCHORS,
    PATH_SOURCE_VALHALLA_TERMINUS,
})


class PathInferenceError(RuntimeError):
    """Raised on infrastructure failures (Valhalla unreachable, HTTP 5xx,
    malformed response). Missing data (e.g. no named-road match) returns
    ``None`` instead — it is not an error."""


@dataclass(frozen=True)
class RoutePath:
    polyline: str
    source: str
    computed_at: datetime


@dataclass(frozen=True)
class PolylineProjection:
    lat: float
    lon: float
    distance_m: float
    index_along: int  # index of nearest vertex in the decoded sequence


# ---------------------------------------------------------------------------
# polyline codec (Google encoded polyline, precision 6)
# ---------------------------------------------------------------------------


def encode_polyline(coords: Sequence[tuple[float, float]]) -> str:
    """``coords`` are (lat, lon) pairs."""
    return _polyline_codec.encode(list(coords), precision=POLYLINE_PRECISION)


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    if not encoded:
        return []
    return _polyline_codec.decode(encoded, precision=POLYLINE_PRECISION)


# ---------------------------------------------------------------------------
# haversine — used for distance-to-polyline
# ---------------------------------------------------------------------------


_EARTH_RADIUS_M = 6_371_008.8


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = (math.radians(a[0]), math.radians(a[1]))
    lat2, lon2 = (math.radians(b[0]), math.radians(b[1]))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    s = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(s))


# ---------------------------------------------------------------------------
# Valhalla call
# ---------------------------------------------------------------------------


def _valhalla_base_url() -> str:
    return os.environ.get("VALHALLA_CONSTRUCTION_URL", DEFAULT_VALHALLA_URL)


def _call_valhalla_route(
    waypoints: Sequence[tuple[float, float]],
    costing: str = "bus",
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> str:
    """POST to Valhalla /route and return the encoded shape (precision 6).

    Raises :class:`PathInferenceError` on infrastructure failure.
    """
    url = _valhalla_base_url().rstrip("/") + "/route"
    locations = [{"lat": lat, "lon": lon} for (lat, lon) in waypoints]
    payload = {
        "locations": locations,
        "costing": costing,
        "directions_options": {"units": "kilometers"},
        "shape_format": "polyline6",
    }
    try:
        resp = requests.post(url, json=payload, timeout=timeout_s)
    except requests.Timeout as e:
        raise PathInferenceError(f"Valhalla timeout after {timeout_s}s: {e}") from e
    except requests.RequestException as e:
        raise PathInferenceError(f"Valhalla request failed: {e}") from e

    if resp.status_code != 200:
        raise PathInferenceError(
            f"Valhalla returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
    try:
        data = resp.json()
    except ValueError as e:
        raise PathInferenceError(f"Valhalla returned non-JSON: {e}") from e

    try:
        shape = data["trip"]["legs"][0]["shape"]
    except (KeyError, IndexError, TypeError) as e:
        raise PathInferenceError(
            f"Valhalla response missing trip.legs[0].shape: {data}"
        ) from e
    if not shape:
        raise PathInferenceError("Valhalla returned empty shape")
    return shape


# ---------------------------------------------------------------------------
# OSM relation geometry loader (pluggable for tests)
# ---------------------------------------------------------------------------


def _fetch_osm_relation_polyline(
    osm_relation_id: int, conn=None
) -> Optional[str]:
    """Return the encoded polyline for an OSM relation, or None.

    Reads from ``route_raw.osm_relations.encoded_polyline`` if present.
    Tests inject the value via ``conn`` rather than hitting the network.
    """
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT encoded_polyline FROM route_raw.osm_relations "
            "WHERE osm_relation_id = %s",
            (osm_relation_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return row[0] if isinstance(row, (tuple, list)) else row.get("encoded_polyline")


# ---------------------------------------------------------------------------
# DB helpers (all optional — stages pass a conn to use them)
# ---------------------------------------------------------------------------


def get_cached_path(route_id: str, *, conn, fresh_days: int = CACHE_FRESHNESS_DAYS) -> Optional[RoutePath]:
    sql = (
        "SELECT inferred_path_polyline, inferred_path_source, inferred_path_computed_at "
        "FROM route_prod.routes WHERE route_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (route_id,))
        row = cur.fetchone()
    if not row:
        return None
    polyline, source, computed_at = (
        (row[0], row[1], row[2]) if isinstance(row, (tuple, list))
        else (row.get("inferred_path_polyline"),
              row.get("inferred_path_source"),
              row.get("inferred_path_computed_at"))
    )
    if not polyline or not computed_at:
        return None
    # freshness check
    now = datetime.now(timezone.utc)
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=timezone.utc)
    if now - computed_at > timedelta(days=fresh_days):
        return None
    return RoutePath(polyline=polyline, source=source, computed_at=computed_at)


def cache_path(
    route_id: str, polyline: str, source: str, *, conn, now: Optional[datetime] = None
) -> None:
    if source not in VALID_PATH_SOURCES:
        raise ValueError(f"invalid inferred_path_source: {source!r}")
    now = now or datetime.now(timezone.utc)
    patch_route_prod_fields(
        conn=conn,
        route_id=str(route_id),
        fields={
            "inferred_path_polyline": polyline,
            "inferred_path_source": source,
            "inferred_path_computed_at": now,
        },
        source_type=_SOURCE_TYPE,
        pipeline_version=_PIPELINE_VERSION_CACHE,
    )


def invalidate_path(route_id: str, *, conn) -> None:
    patch_route_prod_fields(
        conn=conn,
        route_id=str(route_id),
        fields={
            "inferred_path_polyline": None,
            "inferred_path_source": None,
            "inferred_path_computed_at": None,
        },
        source_type=_SOURCE_TYPE,
        pipeline_version=_PIPELINE_VERSION_INVALIDATE,
    )


# ---------------------------------------------------------------------------
# compute_route_path — the main entry point
# ---------------------------------------------------------------------------


def compute_route_path(
    route_id: str,
    termini: Sequence[tuple[float, float]],
    grounded_stops: Sequence[tuple[float, float]] = (),
    must_pass_through: Sequence[tuple[float, float]] = (),
    osm_relation_id: Optional[int] = None,
    *,
    conn=None,
    force_recompute: bool = False,
) -> RoutePath:
    """Return a :class:`RoutePath`; hit cache first unless ``force_recompute``.

    If ``osm_relation_id`` is provided, the OSM relation's polyline is used
    verbatim (source = ``osm_relation_geometry``).  Otherwise Valhalla is
    invoked with termini + grounded_stops + must_pass_through as waypoints.
    """
    if not termini or len(termini) < 2:
        raise ValueError("termini must contain at least 2 waypoints")

    if not force_recompute and conn is not None:
        cached = get_cached_path(route_id, conn=conn)
        if cached is not None:
            log.debug("path cache HIT route=%s source=%s", route_id, cached.source)
            return cached

    if osm_relation_id is not None:
        shape = _fetch_osm_relation_polyline(osm_relation_id, conn=conn)
        if not shape:
            raise PathInferenceError(
                f"OSM relation {osm_relation_id} has no cached encoded_polyline"
            )
        source = PATH_SOURCE_OSM
    else:
        waypoints = list(termini)
        # grounded stops + must-pass-through injected between the termini
        inserts = list(grounded_stops) + list(must_pass_through)
        if inserts:
            waypoints = [termini[0]] + inserts + [termini[-1]]
            source = PATH_SOURCE_VALHALLA_ANCHORS
        else:
            source = PATH_SOURCE_VALHALLA_TERMINUS
        log.info(
            "computing valhalla path route=%s waypoints=%d source=%s",
            route_id, len(waypoints), source,
        )
        shape = _call_valhalla_route(waypoints)

    now = datetime.now(timezone.utc)
    if conn is not None:
        cache_path(route_id, shape, source, conn=conn, now=now)
    return RoutePath(polyline=shape, source=source, computed_at=now)


# ---------------------------------------------------------------------------
# projection + named-road helpers
# ---------------------------------------------------------------------------


def _to_linestring(encoded: str) -> LineString:
    pts = decode_polyline(encoded)
    if len(pts) < 2:
        raise PathInferenceError("polyline has fewer than 2 vertices")
    # shapely is x=lon, y=lat
    return LineString([(lon, lat) for (lat, lon) in pts])


def project_point_to_polyline(
    lat: float, lon: float, polyline: str
) -> PolylineProjection:
    """Project a point to the polyline; returns coords, distance, index."""
    line = _to_linestring(polyline)
    pt = Point(lon, lat)
    _, nearest_on_line = nearest_points(pt, line)
    proj_lat, proj_lon = nearest_on_line.y, nearest_on_line.x
    distance_m = haversine_m((lat, lon), (proj_lat, proj_lon))

    # index_along = nearest vertex index
    pts = decode_polyline(polyline)
    best_idx = 0
    best_d = float("inf")
    for i, (plat, plon) in enumerate(pts):
        d = haversine_m((proj_lat, proj_lon), (plat, plon))
        if d < best_d:
            best_d = d
            best_idx = i
    return PolylineProjection(
        lat=proj_lat, lon=proj_lon, distance_m=distance_m, index_along=best_idx,
    )


def _fetch_named_ways_from_overpass(
    road_name: str, bbox: tuple[float, float, float, float], timeout_s: int = DEFAULT_TIMEOUT_S
) -> list[list[tuple[float, float]]]:
    """Return lists of (lat, lon) vertices for ways whose name matches.

    Uses the OVERPASS_URL env var. Tests stub this out.
    """
    overpass = os.environ.get("OVERPASS_URL", "http://localhost:12346/api/interpreter")
    # case-insensitive regex
    south, west, north, east = bbox
    ql = (
        f"[out:json][timeout:25];"
        f'way["highway"]["name"~"{road_name}",i]({south},{west},{north},{east});'
        f"out geom;"
    )
    try:
        resp = requests.post(overpass, data={"data": ql}, timeout=timeout_s)
    except requests.Timeout as e:
        raise PathInferenceError(f"Overpass timeout: {e}") from e
    except requests.RequestException as e:
        raise PathInferenceError(f"Overpass request failed: {e}") from e
    if resp.status_code != 200:
        raise PathInferenceError(f"Overpass HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    ways = []
    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        pts = [(g["lat"], g["lon"]) for g in geom if "lat" in g and "lon" in g]
        if len(pts) >= 2:
            ways.append(pts)
    return ways


def _polyline_bbox(polyline: str, pad_deg: float = 0.005) -> tuple[float, float, float, float]:
    pts = decode_polyline(polyline)
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    return (
        min(lats) - pad_deg, min(lons) - pad_deg,
        max(lats) + pad_deg, max(lons) + pad_deg,
    )


def polyline_segment_by_road_name(
    polyline: str, road_name: str, route_id: str, *, overpass_fetcher=None,
) -> Optional[list[tuple[float, float]]]:
    """Return the sub-polyline where a named road is traversed, or None.

    ``overpass_fetcher`` is an optional injected callable for testing; if
    None, the live Overpass endpoint is queried via
    :func:`_fetch_named_ways_from_overpass`.
    """
    fetcher = overpass_fetcher or _fetch_named_ways_from_overpass
    bbox = _polyline_bbox(polyline)
    ways = fetcher(road_name, bbox)
    if not ways:
        return None

    line = _to_linestring(polyline)
    decoded = decode_polyline(polyline)

    hit_indices: list[int] = []
    for way in ways:
        way_ls = LineString([(lon, lat) for (lat, lon) in way])
        for i, (plat, plon) in enumerate(decoded):
            d_deg = way_ls.distance(Point(plon, plat))
            # rough 20m — at Quito latitude 1deg ≈ 111km → 20m ≈ 0.00018
            if d_deg < 0.00018:
                hit_indices.append(i)
    if not hit_indices:
        return None
    hit_indices = sorted(set(hit_indices))
    start, end = hit_indices[0], hit_indices[-1]
    if end - start < 1:
        return None
    return decoded[start : end + 1]


def find_intersection_along_polyline(
    polyline: str,
    road_name_a: str,
    road_name_b: str,
    route_id: str,
    *,
    overpass_fetcher=None,
) -> Optional[tuple[float, float]]:
    fetcher = overpass_fetcher or _fetch_named_ways_from_overpass
    bbox = _polyline_bbox(polyline)
    ways_a = fetcher(road_name_a, bbox)
    ways_b = fetcher(road_name_b, bbox)
    if not ways_a or not ways_b:
        return None
    line = _to_linestring(polyline)

    best: Optional[tuple[float, float, float]] = None  # (lat, lon, path_dist_m)
    for wa in ways_a:
        la = LineString([(lon, lat) for (lat, lon) in wa])
        for wb in ways_b:
            lb = LineString([(lon, lat) for (lat, lon) in wb])
            inter = la.intersection(lb)
            if inter.is_empty:
                continue
            candidates: list[Point] = []
            if inter.geom_type == "Point":
                candidates = [inter]
            elif inter.geom_type == "MultiPoint":
                candidates = list(inter.geoms)
            else:
                continue
            for pt in candidates:
                lat, lon = pt.y, pt.x
                proj = project_point_to_polyline(lat, lon, polyline)
                if best is None or proj.distance_m < best[2]:
                    best = (lat, lon, proj.distance_m)
    if best is None:
        return None
    return (best[0], best[1])
