"""Canonical geometry + direction thresholds for Phase 4.5 and all consumers.

Single source of truth per workspace/skills/direction_construction.md.
DO NOT redefine these constants or functions elsewhere — import from here.

Convention: all 4-float APIs are lat-first. Helpers that take a single
``point`` use ``(lat, lon)`` tuples unless otherwise documented.
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple

# === Direction thresholds (degrees) ===
OPPOSITION_DEG_THRESHOLD = 120.0
CORRIDOR_AGREE_DEG = 30.0
SYNTHESIS_PLAUSIBILITY_DEG = 60.0

# === Pair scoring (0–1) ===
RELIABLE_PAIR_SCORE = 0.60
PLAUSIBLE_PAIR_SCORE = 0.45

# === Stop pair distance bands (meters) ===
HARD_MERGE_M = 5.0
TIGHT_MERGE_BAND_M = (5.0, 8.0)
OVER_KEEP_BAND_M = (8.0, 15.0)

# === Synthesis geometry (meters) ===
LATERAL_OFFSET_M = 4.0

# === Earth constants ===
EARTH_RADIUS_M = 6_371_000.0
_M_PER_DEG_LAT = 111_132.0
_M_PER_DEG_LON_EQUATOR = 111_320.0


def _m_per_deg_lon(lat_ref_deg: float) -> float:
    return _M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat_ref_deg))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in metres."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(max(0.0, min(1.0, h))))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from p1 to p2 as a compass heading in [0, 360).

    0° = north, increasing clockwise.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = (
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    )
    angle = math.degrees(math.atan2(y, x))
    return (angle + 360.0) % 360.0


# Legacy alias — pre-canonical name used inside hades/enforcers/.
bearing_deg = bearing


def angle_diff(b1: float, b2: float) -> float:
    """Smallest absolute angle between two compass bearings, in [0, 180]."""
    return abs((b1 - b2 + 180.0) % 360.0 - 180.0)


# Legacy alias.
angular_delta_deg = angle_diff


def polyline_bearing_at(
    polyline: Sequence[Tuple[float, float]],
    idx: int,
) -> float:
    """Bearing at polyline index using a window of 3 points centred on ``idx``.

    Falls back to a 2-point window at endpoints. Polyline points are
    ``(lat, lon)`` tuples. Raises ``ValueError`` for empty / single-point
    polylines or out-of-range indices.
    """
    n = len(polyline)
    if n < 2:
        raise ValueError("polyline must have at least 2 points")
    if idx < 0 or idx >= n:
        raise ValueError(f"idx {idx} out of range for polyline of length {n}")
    if idx == 0:
        a = polyline[0]
        b = polyline[1]
        return bearing(a[0], a[1], b[0], b[1])
    if idx == n - 1:
        a = polyline[n - 2]
        b = polyline[n - 1]
        return bearing(a[0], a[1], b[0], b[1])
    a = polyline[idx - 1]
    b = polyline[idx + 1]
    return bearing(a[0], a[1], b[0], b[1])


def lateral_offset_point(
    lat: float,
    lon: float,
    bearing_deg_value: float,
    side: str,
    distance_m: float = LATERAL_OFFSET_M,
) -> Tuple[float, float]:
    """Project a point laterally perpendicular to ``bearing_deg_value``.

    ``side`` ∈ {"left", "right"}. For right-hand-drive countries (Ecuador
    included), the opposite-direction stop sits on the right of travel.

    Uses an equirectangular projection at the input latitude — accurate
    to <0.1% for sub-kilometre offsets, which is all we use this for.
    """
    if side not in ("left", "right"):
        raise ValueError(f"side must be 'left' or 'right', got {side!r}")
    perpendicular = (bearing_deg_value + (90.0 if side == "right" else -90.0)) % 360.0
    theta = math.radians(perpendicular)
    dx_m = distance_m * math.sin(theta)
    dy_m = distance_m * math.cos(theta)
    dlat = dy_m / _M_PER_DEG_LAT
    dlon = dx_m / _m_per_deg_lon(lat)
    return (lat + dlat, lon + dlon)
