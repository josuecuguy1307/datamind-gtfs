from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union


BBox = Union[str, Dict[str, float]]


@dataclass(frozen=True)
class Point:
    lat: float
    lon: float
    label: str = ""
    value: Optional[float] = None


def bbox_to_str(b: BBox) -> str:
    """
    Accepts:
      - "south,west,north,east"
      - {"south":..,"west":..,"north":..,"east":..}
    Returns a standard string.
    """
    if isinstance(b, str):
        return b.strip()
    return f"{b['south']},{b['west']},{b['north']},{b['east']}"


def parse_bbox(s: str) -> Tuple[float, float, float, float]:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must be 'south,west,north,east'")
    south, west, north, east = map(float, parts)
    if south > north:
        raise ValueError("bbox invalid: south > north")
    if west > east:
        # allow dateline crossing? keep strict for now.
        raise ValueError("bbox invalid: west > east")
    return south, west, north, east


def bbox_center(b: BBox) -> Tuple[float, float]:
    south, west, north, east = parse_bbox(bbox_to_str(b))
    return (south + north) / 2.0, (west + east) / 2.0


from hades.geometry.canonical import haversine_m  # noqa: E402,F401


def points_bounds(points: Sequence[Point]) -> Optional[Tuple[float, float, float, float]]:
    if not points:
        return None
    south = min(p.lat for p in points)
    north = max(p.lat for p in points)
    west = min(p.lon for p in points)
    east = max(p.lon for p in points)
    return (south, west, north, east)


def linestring_wkt(coords_latlon: Sequence[Tuple[float, float]]) -> str:
    """
    Convert [(lat,lon),...] -> WKT LINESTRING(lon lat,...)
    """
    if len(coords_latlon) < 2:
        raise ValueError("Need at least 2 points for LINESTRING")
    parts = [f"{lon:.7f} {lat:.7f}" for (lat, lon) in coords_latlon]
    return f"LINESTRING({', '.join(parts)})"


def fmt_latlon(lat: float, lon: float, nd: int = 6) -> str:
    return f"{lat:.{nd}f}, {lon:.{nd}f}"
