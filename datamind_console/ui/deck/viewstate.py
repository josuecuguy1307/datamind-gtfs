from __future__ import annotations
import pydeck as pdk
from typing import Sequence, Mapping, Optional

def viewstate_from_points(
    points: Sequence[Mapping],
    *,
    default_lat: float = -0.1807,
    default_lon: float = -78.4678,
    default_zoom: float = 11.5,
) -> pdk.ViewState:
    if not points:
        return pdk.ViewState(latitude=default_lat, longitude=default_lon, zoom=default_zoom)

    lats = [p["lat"] for p in points if "lat" in p]
    lons = [p["lon"] for p in points if "lon" in p]
    if not lats or not lons:
        return pdk.ViewState(latitude=default_lat, longitude=default_lon, zoom=default_zoom)

    lat = sum(lats) / len(lats)
    lon = sum(lons) / len(lons)
    return pdk.ViewState(latitude=lat, longitude=lon, zoom=default_zoom)
