from __future__ import annotations
import json
from typing import Any, Dict, List, Tuple, Optional

Coord = Tuple[float, float]  # (lon, lat)

def _coords_from_any(geom: Any, *, order: str = "lonlat") -> List[Coord]:
    if geom is None:
        raise ValueError("geom is None")

    if hasattr(geom, "coords"):
        pts = [(float(x), float(y)) for (x, y) in list(geom.coords)]
    else:
        coords = list(geom)
        if not coords:
            raise ValueError("Empty geometry coords")
        pts = [(float(x), float(y)) for (x, y) in coords]

    if order == "latlon":
        # swap into lonlat
        pts = [(lon, lat) for (lat, lon) in pts]

    return pts


def linestring_feature(
    geom: Any,
    properties: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    coords = _coords_from_any(geom)
    if len(coords) < 2:
        raise ValueError("LineString needs at least 2 points")

    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": properties or {},
    }

def feature_collection(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}

def write_geojson(path: str, obj: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
