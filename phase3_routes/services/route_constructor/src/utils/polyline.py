from __future__ import annotations

import math


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in meters between two (lat, lon) points."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def decode_polyline6(polyline: str) -> list[tuple[float, float]]:
    index, lat, lng = 0, 0, 0
    coordinates = []
    length = len(polyline)

    def _decode_value():
        nonlocal index
        result, shift = 0, 0
        while True:
            if index >= length:
                break
            b = ord(polyline[index]) - 63
            index += 1
            result |= (b & 0x1f) << shift
            shift += 5
            if b < 0x20:
                break
        return ~(result >> 1) if (result & 1) else (result >> 1)

    while index < length:
        lat += _decode_value()
        lng += _decode_value()
        coordinates.append((lat / 1e6, lng / 1e6))
    return coordinates

def to_linestring_wkt(latlon: list[tuple[float, float]]) -> str:
    pts = ", ".join([f"{lon} {lat}" for (lat, lon) in latlon])
    return f"LINESTRING({pts})"
