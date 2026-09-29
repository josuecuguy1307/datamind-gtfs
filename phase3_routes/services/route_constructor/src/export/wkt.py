from __future__ import annotations
from typing import Any, Iterable, Tuple, List, Optional

Coord = Tuple[float, float]  # (lon, lat)

def _as_lonlat_pair(p: Any, *, order: str) -> Coord:
    """
    order:
      - "lonlat": input is (lon,lat)
      - "latlon": input is (lat,lon) -> will swap
      - "auto": try to infer from ranges; fallback to lonlat
    """
    if not isinstance(p, (list, tuple)) or len(p) < 2:
        raise ValueError(f"Invalid coordinate pair: {p!r}")

    a = float(p[0])
    b = float(p[1])

    if order == "lonlat":
        lon, lat = a, b
    elif order == "latlon":
        lat, lon = a, b
    elif order == "auto":
        # If first looks like latitude and second looks like longitude, swap.
        # lat must be [-90,90], lon must be [-180,180]
        a_is_lat = -90 <= a <= 90
        b_is_lon = -180 <= b <= 180
        a_is_lon = -180 <= a <= 180
        b_is_lat = -90 <= b <= 90

        if a_is_lat and b_is_lon and not (a_is_lon and b_is_lat):
            lat, lon = a, b
        else:
            lon, lat = a, b
    else:
        raise ValueError("order must be one of: lonlat | latlon | auto")

    # Final sanity
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        # Don't hard-fail if you want; but failing early catches bugs fast.
        raise ValueError(f"Out-of-range lon/lat: lon={lon}, lat={lat}")

    return (lon, lat)

def _coords_from_any(
    geom: Any,
    *,
    order: str = "lonlat",
    dedup_consecutive: bool = True,
    precision: Optional[int] = 6,
) -> List[Coord]:
    """
    Accepts:
      - list/iterable of coordinate pairs
      - Shapely LineString-like (has .coords)
    Returns: list[(lon,lat)]
    """
    if geom is None:
        raise ValueError("geom is None")

    # Shapely-like
    if hasattr(geom, "coords"):
        raw = list(geom.coords)
    else:
        raw = list(geom)

    if not raw:
        raise ValueError("Empty geometry coords")

    coords: List[Coord] = []
    last: Optional[Coord] = None

    for p in raw:
        lon, lat = _as_lonlat_pair(p, order=order)

        if precision is not None:
            lon = round(lon, precision)
            lat = round(lat, precision)

        cur = (lon, lat)

        if dedup_consecutive and last is not None and cur == last:
            continue

        coords.append(cur)
        last = cur

    # Must have at least 2 DISTINCT points
    if len(coords) < 2 or len(set(coords)) < 2:
        raise ValueError("LineString needs at least 2 distinct points")

    return coords

def linestring_wkt(
    geom: Any,
    *,
    order: str = "lonlat",
    precision: Optional[int] = 6,
    dedup_consecutive: bool = True,
) -> str:
    coords = _coords_from_any(
        geom,
        order=order,
        precision=precision,
        dedup_consecutive=dedup_consecutive,
    )
    coord_str = ", ".join(f"{lon} {lat}" for (lon, lat) in coords)
    return f"LINESTRING({coord_str})"
