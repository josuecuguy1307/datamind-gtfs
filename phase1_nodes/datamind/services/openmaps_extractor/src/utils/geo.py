from __future__ import annotations
import math
from typing import Dict, Tuple, Optional

# ---------- BBOX ----------

def validate_bbox(b: Dict[str, float]) -> None:
    for k in ("south", "west", "north", "east"):
        if k not in b:
            raise ValueError(f"bbox missing '{k}'")
    if not (-90 <= b["south"] <= 90 and -90 <= b["north"] <= 90):
        raise ValueError("bbox latitude out of range")
    if not (-180 <= b["west"] <= 180 and -180 <= b["east"] <= 180):
        raise ValueError("bbox longitude out of range")
    if b["south"] >= b["north"]:
        raise ValueError("bbox invalid: south >= north")
    # west/east crossing dateline not needed for Quito; keep simple:
    if b["west"] >= b["east"]:
        raise ValueError("bbox invalid: west >= east")

def bbox_to_overpass(b: Dict[str, float]) -> str:
    validate_bbox(b)
    # Overpass expects: south,west,north,east
    return f'{b["south"]},{b["west"]},{b["north"]},{b["east"]}'

def expand_bbox_m(b: Dict[str, float], buffer_m: float) -> Dict[str, float]:
    """
    Expands bbox by ~buffer_m in all directions (roughly).
    Good enough for small areas like Quito.
    """
    validate_bbox(b)
    mid_lat = (b["south"] + b["north"]) / 2.0
    # meters per degree
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))

    dlat = buffer_m / m_per_deg_lat
    dlon = buffer_m / m_per_deg_lon if m_per_deg_lon != 0 else 0

    return {
        "south": b["south"] - dlat,
        "west":  b["west"]  - dlon,
        "north": b["north"] + dlat,
        "east":  b["east"]  + dlon,
    }

# ---------- DISTANCE ----------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2 * R * math.asin(math.sqrt(a))

# ---------- WKT / POINT ----------

def point_wkt(lon: float, lat: float, srid: int = 4326) -> str:
    return f"SRID={srid};POINT({lon} {lat})"

def pick_point(
    lat: Optional[float], lon: Optional[float],
    center_lat: Optional[float], center_lon: Optional[float],
) -> Optional[Tuple[float, float]]:
    """
    Return (lat, lon) from node coords else center coords else None.
    """
    if lat is not None and lon is not None:
        return (lat, lon)
    if center_lat is not None and center_lon is not None:
        return (center_lat, center_lon)
    return None
