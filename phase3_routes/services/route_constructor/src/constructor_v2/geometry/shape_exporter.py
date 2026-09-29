from __future__ import annotations

from typing import Sequence


def export_linestring_geojson(coords: Sequence[tuple[float, float]]) -> dict:
    return {
        "type": "LineString",
        "coordinates": [[lon, lat] for lon, lat in coords],
    }
