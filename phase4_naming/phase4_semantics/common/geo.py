from __future__ import annotations

import math
from typing import Iterable, List, Tuple, Optional

# ============================================================
# Core distance math (SEMANTIC ONLY)
# ============================================================

EARTH_RADIUS_M = 6371000.0  # meters

LatLon = Tuple[float, float]
BBox = Tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)


def haversine_m(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """
    Great-circle distance between two WGS84 points in meters.

    Semantic use only:
    - proximity scoring
    - evidence confidence
    - NEVER routing / snapping
    """
    φ1 = math.radians(lat1)
    φ2 = math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)

    a = (
        math.sin(Δφ / 2.0) ** 2
        + math.cos(φ1) * math.cos(φ2) * math.sin(Δλ / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_M * c


# ============================================================
# Point-based semantic helpers
# ============================================================

def min_distance_point_to_points_m(
    pt: LatLon,
    others: Iterable[LatLon],
) -> Optional[float]:
    """
    Minimum haversine distance (meters) from pt to a set of points.

    Used for:
    - landmark → route proximity
    - evidence soft matching
    """
    lat, lon = pt
    dmin: Optional[float] = None

    for lat2, lon2 in others:
        d = haversine_m(lat, lon, lat2, lon2)
        if dmin is None or d < dmin:
            dmin = d

    return dmin


def count_points_within_radius(
    center: LatLon,
    points: Iterable[LatLon],
    radius_m: float,
) -> int:
    """
    Counts how many points fall within radius_m of center.

    Semantic usage:
    - coverage hints
    - confidence shaping
    """
    lat, lon = center
    count = 0

    for lat2, lon2 in points:
        if haversine_m(lat, lon, lat2, lon2) <= radius_m:
            count += 1

    return count


def normalize_distance_score(
    distance_m: Optional[float],
    max_radius_m: float,
) -> float:
    """
    Converts distance → [0,1] score.

    - 1.0 = perfect (distance = 0)
    - 0.0 = outside max_radius_m
    """
    if distance_m is None or distance_m >= max_radius_m:
        return 0.0

    return 1.0 - (distance_m / max_radius_m)


# ============================================================
# Bounding-box semantics (CRITICAL FOR PHASE 4)
# ============================================================

def bbox_from_points(points: Iterable[LatLon]) -> Optional[BBox]:
    """
    Computes bounding box from (lat, lon) points.

    Returns:
      (min_lon, min_lat, max_lon, max_lat)

    Semantic usage:
    - corridor similarity
    - candidate pruning
    - overlap scoring
    """
    lats: List[float] = []
    lons: List[float] = []

    for lat, lon in points:
        lats.append(lat)
        lons.append(lon)

    if not lats or not lons:
        return None

    return (
        min(lons),
        min(lats),
        max(lons),
        max(lats),
    )


def bbox_area(b: BBox) -> float:
    min_lon, min_lat, max_lon, max_lat = b
    if max_lon <= min_lon or max_lat <= min_lat:
        return 0.0
    return (max_lon - min_lon) * (max_lat - min_lat)


def bbox_intersection(a: BBox, b: BBox) -> Optional[BBox]:
    min_lon = max(a[0], b[0])
    min_lat = max(a[1], b[1])
    max_lon = min(a[2], b[2])
    max_lat = min(a[3], b[3])

    if min_lon >= max_lon or min_lat >= max_lat:
        return None

    return (min_lon, min_lat, max_lon, max_lat)


def bbox_overlap_ratio(
    a: Optional[BBox],
    b: Optional[BBox],
) -> float:
    """
    Intersection-over-union (IoU) of two bounding boxes.

    Returns value in [0,1].

    Semantic usage:
    - route similarity
    - corridor overlap
    - candidate pruning

    NOT for geometry correctness.
    """
    if a is None or b is None:
        return 0.0

    inter = bbox_intersection(a, b)
    if inter is None:
        return 0.0

    inter_area = bbox_area(inter)
    if inter_area <= 0:
        return 0.0

    union_area = bbox_area(a) + bbox_area(b) - inter_area
    if union_area <= 0:
        return 0.0

    return inter_area / union_area
