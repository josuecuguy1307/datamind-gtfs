"""
poi_matcher — Overpass-backed POI candidate queries + name scoring.

Responsibilities (from hades-path-aware-synthesis §5.3a and §6):

- Query the self-hosted Overpass endpoint for POIs matching a name within
  a bbox, with stage-specific tag filters (stage 3a vs osm_route_fill).
- Score anchor-name ↔ POI-name similarity via rapidfuzz token_set_ratio
  with Spanish-accent normalisation.
- Determine the POI's access point on the path polyline (entrance node,
  road projection, or centroid fallback).
- Cache Overpass responses in-process for 1 hour keyed on
  ``(name_slug, rounded_bbox, filter_id)`` so the stages/pipelines don't
  hammer the endpoint.

HTTP infrastructure failures raise :class:`POIMatcherError`. Empty result
sets return ``[]`` — callers decide whether that is terminal.
"""
from __future__ import annotations

import logging
import math
import os
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Sequence

import requests
from rapidfuzz import fuzz

from datamind_console.phases.phase3_routes.synthesis import path_inference as pi

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# constants + errors
# ---------------------------------------------------------------------------

DEFAULT_OVERPASS_URL = "http://localhost:12346/api/interpreter"
DEFAULT_TIMEOUT_S = 30
CACHE_TTL_S = 3600  # 1 hour

NAME_SIMILARITY_MIN = 0.70  # §5.3a success criterion
POI_PATH_DISTANCE_MAX_M = 80.0  # §5.3a hard ceiling

ENTRANCE_NEAR_PATH_MAX_M = 40.0
ROAD_NEAR_POI_MAX_M = 50.0


# Per §5.3a tag filter for stage 3a — semantic POIs
STAGE_3A_TAG_FILTERS: list[tuple[str, list[str]]] = [
    ("amenity", [
        "marketplace", "place_of_worship", "school", "college", "university",
        "hospital", "clinic", "police", "fire_station", "townhall",
        "courthouse", "bus_station",
    ]),
    ("shop", ["supermarket", "mall", "department_store"]),
    ("public_transport", ["station", "stop_position"]),
    ("building", ["train_station", "hospital"]),
]

# Per §6 — osm_route_fill tag filter (smaller; rural-friendly)
OSM_FILL_TAG_FILTERS: list[tuple[str, list[str]]] = [
    ("amenity", ["marketplace", "place_of_worship", "school", "hospital", "bus_station"]),
    ("shop", ["supermarket"]),
    ("place", ["village", "hamlet", "neighbourhood"]),
    ("junction", ["yes"]),  # named junctions
]

# class priority per §6 ranking: marketplace > bus_station > school > place_of_worship > hospital > shop > place > junction
POI_CLASS_PRIORITY: dict[str, int] = {
    "marketplace": 100,
    "bus_station": 95,
    "station": 95,
    "train_station": 95,
    "school": 85,
    "college": 85,
    "university": 85,
    "place_of_worship": 75,
    "hospital": 65,
    "clinic": 65,
    "townhall": 55,
    "courthouse": 55,
    "police": 55,
    "fire_station": 55,
    "mall": 50,
    "department_store": 50,
    "supermarket": 50,
    "stop_position": 40,
    "village": 30,
    "hamlet": 30,
    "neighbourhood": 30,
    "junction": 20,
}


class POIMatcherError(RuntimeError):
    """Overpass unreachable, malformed response, etc."""


@dataclass(frozen=True)
class POIMatch:
    osm_id: int
    osm_type: str  # 'node' | 'way' | 'relation'
    name: str
    centroid: tuple[float, float]  # (lat, lon)
    tags: dict
    class_name: str  # e.g. 'marketplace'
    class_priority: int
    name_similarity: Optional[float] = None  # 0..1
    poi_to_path_distance_m: Optional[float] = None
    entrance_nodes: tuple[tuple[float, float], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class POIAccessPoint:
    lat: float
    lon: float
    access_type: str  # 'entrance_node' | 'road_projection' | 'centroid_fallback'
    entrance_node_coords: Optional[tuple[float, float]] = None


# ---------------------------------------------------------------------------
# Spanish-accent normalisation + name scoring
# ---------------------------------------------------------------------------


def _strip_accents(s: str) -> str:
    if not s:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _normalise_name(s: str) -> str:
    return _strip_accents((s or "").strip()).casefold()


def score_poi_match(anchor_name: str, poi_name: str) -> float:
    """Return token-set-ratio similarity in [0, 1], Spanish-accent-insensitive."""
    a = _normalise_name(anchor_name)
    b = _normalise_name(poi_name)
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(a, b) / 100.0


# ---------------------------------------------------------------------------
# Overpass cache
# ---------------------------------------------------------------------------


_OVERPASS_CACHE: dict[tuple, tuple[float, list[POIMatch]]] = {}


def _cache_key(name: str, bbox: tuple[float, float, float, float], filter_id: str) -> tuple:
    # round to 4 decimals (~11 m) so tiny bbox changes still hit cache
    south, west, north, east = bbox
    return (
        _normalise_name(name),
        round(south, 4), round(west, 4), round(north, 4), round(east, 4),
        filter_id,
    )


def _cache_get(key: tuple, *, now: Optional[float] = None) -> Optional[list[POIMatch]]:
    now = now if now is not None else time.time()
    entry = _OVERPASS_CACHE.get(key)
    if entry is None:
        return None
    expires_at, payload = entry
    if now >= expires_at:
        _OVERPASS_CACHE.pop(key, None)
        return None
    return list(payload)


def _cache_put(key: tuple, payload: list[POIMatch], *, now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    _OVERPASS_CACHE[key] = (now + CACHE_TTL_S, list(payload))


def clear_overpass_cache() -> None:
    """Test helper — wipe the in-process cache."""
    _OVERPASS_CACHE.clear()


# ---------------------------------------------------------------------------
# Overpass query
# ---------------------------------------------------------------------------


def _overpass_url() -> str:
    return os.environ.get("OVERPASS_URL", DEFAULT_OVERPASS_URL)


def _compile_tag_clause(
    name: str, bbox: tuple[float, float, float, float],
    tag_filters: list[tuple[str, list[str]]],
) -> str:
    south, west, north, east = bbox
    # regex-escape name for Overpass QL
    safe_name = name.replace('"', '\\"')
    bbox_expr = f"({south},{west},{north},{east})"
    clauses = []
    for elem in ("node", "way", "relation"):
        for (tag, values) in tag_filters:
            vals_re = "|".join(values)
            clauses.append(
                f'{elem}["{tag}"~"^({vals_re})$"]["name"~"{safe_name}",i]{bbox_expr};'
            )
    return "".join(clauses)


def _extract_class_name(tags: dict) -> Optional[str]:
    """Return the POI's class (the value of amenity|shop|public_transport|building|place|junction)."""
    for k in ("amenity", "shop", "public_transport", "building", "place", "junction"):
        v = tags.get(k)
        if v:
            return v
    return None


def _polygon_centroid(nodes_latlon: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Planar centroid of a closed polygon using the shoelace formula.

    For Quito-scale polygons, ignoring Earth curvature introduces <1 m of
    error, which is deep inside the 80 m ceiling.
    """
    if not nodes_latlon:
        raise ValueError("cannot take centroid of empty geometry")
    if len(nodes_latlon) == 1:
        return nodes_latlon[0]
    pts = list(nodes_latlon)
    # close the ring if needed
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    cx = cy = a = 0.0
    for i in range(len(pts) - 1):
        lat1, lon1 = pts[i]
        lat2, lon2 = pts[i + 1]
        cross = lon1 * lat2 - lon2 * lat1
        a += cross
        cx += (lon1 + lon2) * cross
        cy += (lat1 + lat2) * cross
    a /= 2.0
    if a == 0:
        # degenerate (collinear) — fall back to mean
        lats = [p[0] for p in nodes_latlon]
        lons = [p[1] for p in nodes_latlon]
        return (sum(lats) / len(lats), sum(lons) / len(lons))
    cx /= 6 * a
    cy /= 6 * a
    return (cy, cx)


def _element_centroid(el: dict) -> Optional[tuple[float, float]]:
    """Extract (lat, lon) from an Overpass element regardless of type."""
    if el.get("type") == "node":
        if "lat" in el and "lon" in el:
            return (el["lat"], el["lon"])
        return None
    # way/relation — Overpass with "out center" returns center; "out geom" returns geometry
    center = el.get("center")
    if center and "lat" in center and "lon" in center:
        return (center["lat"], center["lon"])
    geom = el.get("geometry") or []
    pts = [(g["lat"], g["lon"]) for g in geom if "lat" in g and "lon" in g]
    if pts:
        return _polygon_centroid(pts)
    return None


def _parse_overpass_response(data: dict) -> list[POIMatch]:
    """Convert raw Overpass JSON into a list of :class:`POIMatch`."""
    out: list[POIMatch] = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue
        class_name = _extract_class_name(tags)
        if class_name is None:
            continue
        centroid = _element_centroid(el)
        if centroid is None:
            continue
        priority = POI_CLASS_PRIORITY.get(class_name, 10)
        out.append(POIMatch(
            osm_id=el.get("id"),
            osm_type=el.get("type"),
            name=name,
            centroid=centroid,
            tags=dict(tags),
            class_name=class_name,
            class_priority=priority,
        ))
    return out


def query_pois_by_name(
    name: str,
    bbox: tuple[float, float, float, float],
    *,
    tag_filters: Optional[list[tuple[str, list[str]]]] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    http_post=None,
    now: Optional[float] = None,
) -> list[POIMatch]:
    """Query Overpass for POIs whose name matches ``name`` (case-insensitive
    regex) within ``bbox`` and whose tags match one of ``tag_filters``.

    ``http_post`` is an injectable ``requests.post``-shaped callable for
    tests — default is the real endpoint.
    """
    filters = tag_filters if tag_filters is not None else STAGE_3A_TAG_FILTERS
    filter_id = "|".join(f"{t}={','.join(vs)}" for (t, vs) in filters)
    key = _cache_key(name, bbox, filter_id)
    cached = _cache_get(key, now=now)
    if cached is not None:
        log.debug("overpass cache HIT name=%s bbox=%s", name, bbox)
        return cached

    post = http_post or requests.post
    clause = _compile_tag_clause(name, bbox, filters)
    ql = f"[out:json][timeout:25];({clause});out tags center geom;"
    url = _overpass_url()
    try:
        resp = post(url, data={"data": ql}, timeout=timeout_s)
    except requests.Timeout as e:
        raise POIMatcherError(f"Overpass timeout after {timeout_s}s: {e}") from e
    except requests.RequestException as e:
        raise POIMatcherError(f"Overpass request failed: {e}") from e

    if resp.status_code != 200:
        raise POIMatcherError(f"Overpass HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise POIMatcherError(f"Overpass returned non-JSON: {e}") from e

    matches = _parse_overpass_response(data)
    _cache_put(key, matches, now=now)
    return matches


# ---------------------------------------------------------------------------
# Candidate filtering + scoring against an anchor name and path
# ---------------------------------------------------------------------------


def filter_and_score_candidates(
    anchor_name: str,
    candidates: Sequence[POIMatch],
    polyline: str,
    *,
    min_similarity: float = NAME_SIMILARITY_MIN,
    max_path_distance_m: float = POI_PATH_DISTANCE_MAX_M,
) -> list[POIMatch]:
    """Return candidates that pass the name-similarity + path-distance tests.

    Returned list is sorted by (path_distance ASC, class_priority DESC,
    similarity DESC). Each candidate has ``name_similarity`` and
    ``poi_to_path_distance_m`` populated.
    """
    scored: list[POIMatch] = []
    for m in candidates:
        sim = score_poi_match(anchor_name, m.name)
        if sim < min_similarity:
            continue
        proj = pi.project_point_to_polyline(m.centroid[0], m.centroid[1], polyline)
        if proj.distance_m > max_path_distance_m:
            continue
        scored.append(POIMatch(
            osm_id=m.osm_id,
            osm_type=m.osm_type,
            name=m.name,
            centroid=m.centroid,
            tags=m.tags,
            class_name=m.class_name,
            class_priority=m.class_priority,
            name_similarity=sim,
            poi_to_path_distance_m=proj.distance_m,
            entrance_nodes=m.entrance_nodes,
        ))
    scored.sort(
        key=lambda r: (
            r.poi_to_path_distance_m,
            -r.class_priority,
            -(r.name_similarity or 0.0),
        )
    )
    return scored


def rank_for_gap_fill(candidates: Sequence[POIMatch]) -> list[POIMatch]:
    """Rank osm_route_fill candidates by (class_priority DESC, distance ASC).

    Input candidates are assumed already filtered through
    :func:`filter_and_score_candidates` with a name="" / min_similarity=0.
    """
    return sorted(
        candidates,
        key=lambda r: (-r.class_priority, (r.poi_to_path_distance_m or math.inf)),
    )


# ---------------------------------------------------------------------------
# POI → access point (entrance / road projection / centroid fallback)
# ---------------------------------------------------------------------------


def poi_to_access_point(
    poi: POIMatch,
    polyline: str,
    *,
    road_projection_coords: Optional[tuple[float, float]] = None,
) -> POIAccessPoint:
    """Decide where the stop actually lands on the street.

    Priority per §5.3a:
      1. entrance_node within 40 m of path
      2. road_projection (POI centroid → nearest road edge → path)
      3. centroid_fallback (point POI, no road within 50 m)

    ``road_projection_coords`` (lat, lon), if given, represents the nearest
    road edge to the POI centroid — when absent or further than 50 m from
    the POI centroid, we return ``centroid_fallback``.
    """
    # (1) entrance_node — closest entrance to the path, if within 40 m
    best_entrance: Optional[tuple[float, float, float]] = None
    for (elat, elon) in poi.entrance_nodes:
        proj = pi.project_point_to_polyline(elat, elon, polyline)
        if proj.distance_m <= ENTRANCE_NEAR_PATH_MAX_M:
            if best_entrance is None or proj.distance_m < best_entrance[2]:
                best_entrance = (elat, elon, proj.distance_m)
    if best_entrance is not None:
        elat, elon, _ = best_entrance
        proj = pi.project_point_to_polyline(elat, elon, polyline)
        return POIAccessPoint(
            lat=proj.lat,
            lon=proj.lon,
            access_type="entrance_node",
            entrance_node_coords=(elat, elon),
        )

    # (2) road_projection — caller supplies road edge; project it to path
    if road_projection_coords is not None:
        rlat, rlon = road_projection_coords
        d = pi.haversine_m(poi.centroid, (rlat, rlon))
        if d <= ROAD_NEAR_POI_MAX_M:
            proj = pi.project_point_to_polyline(rlat, rlon, polyline)
            return POIAccessPoint(
                lat=proj.lat, lon=proj.lon, access_type="road_projection",
            )

    # (3) centroid_fallback — project POI centroid straight to path
    proj = pi.project_point_to_polyline(poi.centroid[0], poi.centroid[1], polyline)
    return POIAccessPoint(lat=proj.lat, lon=proj.lon, access_type="centroid_fallback")
