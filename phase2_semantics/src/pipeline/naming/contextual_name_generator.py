"""
Block 3.3 — Contextual name generator.

Generates contextual replacement names using a cascade:
  1. Intersection (two nearest named roads)
  2. Landmark (nearest named POI/TERMINAL/STATION)
  3. Sector (geographic area)
  4. Sequential (route-based numbering)
  5. Fallback (UUID fragment)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from psycopg2.extensions import connection as PGConnection

logger = logging.getLogger("phase2.contextual_names")


@dataclass
class IntersectionResult:
    road_primary: str
    road_secondary: str
    primary_distance_m: float
    secondary_distance_m: float


@dataclass
class LandmarkResult:
    place_id: str
    canonical_name: str
    place_type: str
    distance_m: float


@dataclass
class ContextualName:
    place_id: str
    original_name: str
    new_name: str
    category: str  # GARBAGE, OVER_APPLIED, DIRECTIONAL_PAIR
    cascade_level: str  # intersection, landmark, sector, sequential, fallback
    confidence: float


# ─── SQL-based finders ────────────────────────────────────────────

_SQL_NEAREST_INTERSECTION = r"""
WITH nearest_roads AS (
    SELECT
        oe.osm_id,
        oe.tags->>'name' AS road_name,
        oe.geom,
        ST_Distance(
            ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
            oe.geom::geography
        ) AS dist_m
    FROM node_raw.overpass_elements oe
    WHERE oe.osm_type = 'way'
      AND oe.tags->>'highway' IS NOT NULL
      AND oe.tags->>'name' IS NOT NULL
      AND oe.tags->>'name' != ''
      AND ST_DWithin(
          ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
          oe.geom::geography,
          %(max_distance_m)s
      )
    ORDER BY ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) <-> oe.geom
    LIMIT 10
)
SELECT
    r1.road_name AS road_primary,
    r2.road_name AS road_secondary,
    r1.dist_m AS primary_distance,
    r2.dist_m AS secondary_distance
FROM nearest_roads r1
CROSS JOIN nearest_roads r2
WHERE r1.osm_id != r2.osm_id
  AND r1.road_name != r2.road_name
  AND r1.dist_m <= r2.dist_m
ORDER BY r1.dist_m + r2.dist_m
LIMIT 1
"""

_SQL_NEAREST_LANDMARK = r"""
SELECT
    p.place_id::text,
    p.canonical_name,
    p.place_type,
    ST_Distance(
        ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
        p.geom::geography
    ) AS dist_m
FROM geo_prod.v_active_places p
WHERE p.place_type IN ('POI', 'TERMINAL', 'STATION')
  AND p.canonical_name IS NOT NULL
  AND TRIM(p.canonical_name) != ''
  AND p.canonical_name NOT IN ('(sin nombre)', 'SN', 'Parada', 'Parada Sin Nombre', 'sin nombre')
  AND p.canonical_name !~ '\([0-9a-f]{6,8}\)'
  AND p.canonical_name !~* '^Parada\s*\('
  AND p.canonical_name !~* '^Parada Sector'
  AND p.canonical_name !~* '^(Frente a|Junto a|Cerca de) '
  AND ST_DWithin(
      ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
      p.geom::geography,
      %(max_distance_m)s
  )
ORDER BY ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) <-> p.geom
LIMIT 1
"""

_SQL_NEAREST_SECTOR = r"""
SELECT
    p.canonical_name AS sector_name,
    p.place_type,
    ST_Distance(
        ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
        p.geom::geography
    ) AS dist_m
FROM geo_prod.v_active_places p
WHERE p.place_type IN ('OTHER', 'POI')
  AND p.canonical_name IS NOT NULL
  AND TRIM(p.canonical_name) != ''
  AND p.canonical_name NOT IN ('(sin nombre)', 'SN', 'Parada', 'sin nombre')
  AND length(p.canonical_name) > 3
  AND p.canonical_name !~ '\([0-9a-f]{6,8}\)'
  AND p.canonical_name !~* '^Parada\s*\('
  AND p.canonical_name !~* '^Parada Sector'
  AND p.canonical_name !~* '^(Frente a|Junto a|Cerca de) '
  AND ST_DWithin(
      ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
      p.geom::geography,
      %(max_distance_m)s
  )
ORDER BY ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) <-> p.geom
LIMIT 1
"""


def find_nearest_intersection(
    conn: PGConnection, lat: float, lon: float, max_distance_m: float = 50,
) -> Optional[IntersectionResult]:
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_NEAREST_INTERSECTION, {"lat": lat, "lon": lon, "max_distance_m": max_distance_m})
            row = cur.fetchone()
            if not row:
                return None
            return IntersectionResult(
                road_primary=row["road_primary"],
                road_secondary=row["road_secondary"],
                primary_distance_m=float(row["primary_distance"]),
                secondary_distance_m=float(row["secondary_distance"]),
            )
    except Exception as e:
        logger.debug("intersection finder failed: %s", e)
        return None


def find_nearest_landmark(
    conn: PGConnection, lat: float, lon: float, max_distance_m: float = 75,
) -> Optional[LandmarkResult]:
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_NEAREST_LANDMARK, {"lat": lat, "lon": lon, "max_distance_m": max_distance_m})
            row = cur.fetchone()
            if not row:
                return None
            return LandmarkResult(
                place_id=row["place_id"],
                canonical_name=row["canonical_name"],
                place_type=row["place_type"],
                distance_m=float(row["dist_m"]),
            )
    except Exception as e:
        logger.debug("landmark finder failed: %s", e)
        return None


def find_nearest_sector(
    conn: PGConnection, lat: float, lon: float, max_distance_m: float = 300,
) -> Optional[str]:
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_NEAREST_SECTOR, {"lat": lat, "lon": lon, "max_distance_m": max_distance_m})
            row = cur.fetchone()
            if not row:
                return None
            return row["sector_name"]
    except Exception as e:
        logger.debug("sector finder failed: %s", e)
        return None


# ─── Proximity label ──────────────────────────────────────────────

def _proximity_prefix(distance_m: float) -> str:
    if distance_m < 20:
        return "Frente a"
    elif distance_m < 50:
        return "Junto a"
    else:
        return "Cerca de"


# ─── Direction label (simple compass from bearing) ────────────────

def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing from point 1 to point 2 in degrees (0=N, 90=E)."""
    import math
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(rlat2)
    y = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def get_direction_label(lat: float, lon: float, centroid_lat: float, centroid_lon: float) -> str:
    """Direction label relative to group centroid."""
    b = _bearing(centroid_lat, centroid_lon, lat, lon)
    if b < 22.5 or b >= 337.5:
        return "Norte"
    elif b < 67.5:
        return "Noreste"
    elif b < 112.5:
        return "Este"
    elif b < 157.5:
        return "Sureste"
    elif b < 202.5:
        return "Sur"
    elif b < 247.5:
        return "Suroeste"
    elif b < 292.5:
        return "Oeste"
    else:
        return "Noroeste"


# ─── Main cascade ─────────────────────────────────────────────────

def generate_contextual_name(
    conn: PGConnection,
    *,
    place_id: str,
    original_name: str,
    lat: float,
    lon: float,
    category: str,
    group_centroid: Optional[tuple] = None,
) -> ContextualName:
    """
    Run the naming cascade for a single place.

    For GARBAGE: full replacement via cascade
    For OVER_APPLIED: original_name + qualifier
    For DIRECTIONAL_PAIR: original_name + direction
    """

    if category == "DIRECTIONAL_PAIR" and group_centroid:
        direction = get_direction_label(lat, lon, group_centroid[0], group_centroid[1])
        return ContextualName(
            place_id=place_id,
            original_name=original_name,
            new_name=f"{original_name} ({direction})",
            category=category,
            cascade_level="direction",
            confidence=0.85,
        )

    # Cascade: intersection → landmark → sector → fallback
    # For GARBAGE: use result as full name
    # For OVER_APPLIED: use result as qualifier appended to original

    # 1. Intersection
    intersection = find_nearest_intersection(conn, lat, lon, max_distance_m=60)
    if intersection:
        int_name = f"{intersection.road_primary} y {intersection.road_secondary}"
        if category == "GARBAGE":
            new_name = int_name
        else:
            new_name = f"{original_name} - {int_name}"
        return ContextualName(
            place_id=place_id,
            original_name=original_name,
            new_name=new_name,
            category=category,
            cascade_level="intersection",
            confidence=0.90,
        )

    # 2. Landmark
    landmark = find_nearest_landmark(conn, lat, lon, max_distance_m=75)
    if landmark:
        prefix = _proximity_prefix(landmark.distance_m)
        lm_ref = f"{prefix} {landmark.canonical_name}"
        if category == "GARBAGE":
            new_name = lm_ref
        else:
            new_name = f"{original_name} - {lm_ref}"
        return ContextualName(
            place_id=place_id,
            original_name=original_name,
            new_name=new_name,
            category=category,
            cascade_level="landmark",
            confidence=0.80,
        )

    # 3. Sector
    sector = find_nearest_sector(conn, lat, lon, max_distance_m=300)
    if sector:
        if category == "GARBAGE":
            new_name = f"Parada en {sector}"
        else:
            new_name = f"{original_name} - en {sector}"
        return ContextualName(
            place_id=place_id,
            original_name=original_name,
            new_name=new_name,
            category=category,
            cascade_level="sector",
            confidence=0.60,
        )

    # 4. Extended sector (last-resort context, up to 2 km)
    far_sector = find_nearest_sector(conn, lat, lon, max_distance_m=2000)
    if far_sector:
        if category == "GARBAGE":
            new_name = f"Parada Cerca de {far_sector}"
        else:
            new_name = f"{original_name} - Cerca de {far_sector}"
        return ContextualName(
            place_id=place_id,
            original_name=original_name,
            new_name=new_name,
            category=category,
            cascade_level="extended_sector",
            confidence=0.50,
        )

    # 5. True orphan — DB-only cascade exhausted within 2 km.
    # We deliberately do NOT emit a synthetic name here. For GARBAGE the
    # caller (stop_treater) escalates to the rural extended cascade
    # (datamind_console.common.extended_stop_naming). For OVER_APPLIED we
    # preserve the original name unchanged. Returning level="orphan" with
    # an empty new_name signals the caller to escalate; we never produce
    # "Parada Aislada" here.
    if category == "GARBAGE":
        new_name = ""
    else:
        new_name = original_name

    return ContextualName(
        place_id=place_id,
        original_name=original_name,
        new_name=new_name,
        category=category,
        cascade_level="orphan",
        confidence=0.0 if category == "GARBAGE" else 0.20,
    )
