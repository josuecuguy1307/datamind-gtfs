"""
Stage B — DB Stop Grounding

Resolves text hints (anchor names, intermediate stop names) against the real
DB stop universe using trigram similarity, Levenshtein, and composite scoring.

Uses geo_prod.node_place_map + node_prod.nodes + geo_prod.places as canonical
stop source (same as geo_prod_repo.py / list_manual_builder_approved_stops).

Schema:
  - node_prod.nodes: node_id, geom, name, ref, operator, node_type
  - geo_prod.places: place_id, canonical_name, place_type, region, status
  - geo_prod.node_place_map: node_id, place_id, confidence, mapping_source
"""
from __future__ import annotations

import logging
import math
import os
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import requests

from datamind_console.db.db import db_conn, fetch_all
from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    RouteSeed,
    StopGroundingResult,
    StopMatch,
)
from datamind_console.phases.phase3_routes.stop_grounding.geography_guardrails import (
    PLACE_BBOXES,
    derive_expected_geographic_envelope,
    derive_hint_geography_context,
    distance_to_bbox_m,
    infer_locality_keys,
    grounding_geography_score,
    grounding_text_alignment,
)

_LOG = logging.getLogger(__name__)
_STOP_USAGE_CACHE: Dict[str, int] = {}

_TERMINAL_KEYWORDS = ("terminal", "estacion", "estación", "parada final", "cabecera", "base")

KNOWN_LANDMARK_COORDS: Dict[str, Tuple[float, float]] = {
    "parque turismo": (-0.3126, -78.4503),
    "el choclo": (-0.3350, -78.4415),
    "redondel de cumanda": (-0.2314, -78.5221),
    "estacion san francisco": (-0.2203, -78.5122),
    "estación san francisco": (-0.2203, -78.5122),
    "viaducto 24 de mayo": (-0.2255, -78.5195),
}

# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------

def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_hint_text(text: str) -> str:
    return _strip_accents(text).strip().lower()


# ---------------------------------------------------------------------------
# Grounding query — fuzzy text → DB stops
# ---------------------------------------------------------------------------

_GROUNDING_SQL = """
WITH hint AS (
    SELECT lower(unaccent(%(hint_text)s)) AS norm_text
)
SELECT
    n.node_id::text  AS stop_id,
    COALESCE(
        NULLIF(BTRIM(p.canonical_name), ''),
        NULLIF(BTRIM(n.name), ''),
        NULLIF(BTRIM(n.ref), ''),
        'stop_' || LEFT(n.node_id::text, 8)
    ) AS stop_name,
    n.operator,
    p.region,
    p.place_id::text AS place_id,
    n.ref,
    ST_Y(n.geom)     AS lat,
    ST_X(n.geom)     AS lon,
    GREATEST(
        similarity(lower(unaccent(COALESCE(p.canonical_name, ''))), h.norm_text),
        similarity(lower(unaccent(COALESCE(n.name, ''))), h.norm_text)
    ) AS name_sim
FROM geo_prod.node_place_map m
JOIN node_prod.nodes n ON n.node_id = m.node_id
JOIN geo_prod.places p ON p.place_id = m.place_id
CROSS JOIN hint h
WHERE n.node_type = 'STOP'
  AND p.status = 'active'
  AND (
      %(bbox_south)s IS NULL
      OR (
          ST_Y(n.geom) BETWEEN %(bbox_south)s AND %(bbox_north)s
          AND ST_X(n.geom) BETWEEN %(bbox_west)s AND %(bbox_east)s
      )
  )
  AND (
      similarity(lower(unaccent(COALESCE(p.canonical_name, ''))), h.norm_text) > 0.25
      OR similarity(lower(unaccent(COALESCE(n.name, ''))), h.norm_text) > 0.25
      OR lower(unaccent(COALESCE(p.canonical_name, ''))) LIKE '%%' || h.norm_text || '%%'
      OR lower(unaccent(COALESCE(n.name, ''))) LIKE '%%' || h.norm_text || '%%'
  )
ORDER BY name_sim DESC
LIMIT %(max_results)s
"""

# Simpler fallback when pg_trgm / unaccent are not installed
_GROUNDING_SQL_FALLBACK = """
SELECT
    n.node_id::text  AS stop_id,
    COALESCE(
        NULLIF(BTRIM(p.canonical_name), ''),
        NULLIF(BTRIM(n.name), ''),
        NULLIF(BTRIM(n.ref), ''),
        'stop_' || LEFT(n.node_id::text, 8)
    ) AS stop_name,
    n.operator,
    p.region,
    p.place_id::text AS place_id,
    n.ref,
    ST_Y(n.geom)     AS lat,
    ST_X(n.geom)     AS lon,
    0.5              AS name_sim
FROM geo_prod.node_place_map m
JOIN node_prod.nodes n ON n.node_id = m.node_id
JOIN geo_prod.places p ON p.place_id = m.place_id
WHERE n.node_type = 'STOP'
  AND p.status = 'active'
  AND (
      %(bbox_south)s IS NULL
      OR (
          ST_Y(n.geom) BETWEEN %(bbox_south)s AND %(bbox_north)s
          AND ST_X(n.geom) BETWEEN %(bbox_west)s AND %(bbox_east)s
      )
  )
  AND (
      lower(COALESCE(p.canonical_name, '')) LIKE '%%' || lower(%(hint_text)s) || '%%'
      OR lower(COALESCE(n.name, '')) LIKE '%%' || lower(%(hint_text)s) || '%%'
  )
LIMIT %(max_results)s
"""


def _compute_composite_score(
    name_similarity: float,
    locality_match: bool,
    operator_match: bool,
) -> float:
    """
    Composite scoring based on available schema fields.
    Weights adjusted since aliases aren't available in this schema.
    """
    return (
        0.50 * name_similarity
        + 0.25 * (1.0 if locality_match else 0.0)
        + 0.25 * (1.0 if operator_match else 0.0)
    )


def _query_rows(connection, *, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Run a grounding query against the stop universe.

    Camino D PIEZA 3 (2026-04-09): refuses to execute when all four bbox
    parameters are None. An unfiltered pg_trgm query against
    ``node_prod.nodes`` was the root cause of the ALAUSI-01 leak — with
    only Sample Region data loaded in the prod nodes table, a fuzzy hint from
    any non-Sample Region province ("Guayaquil", "Cuenca", "Manta", ...) would
    pg_trgm-match an accidentally similar Sample Region name and inject a
    Quito-area stop into the corridor.

    The safe behavior is to return an empty list with a WARNING: if the
    caller has no spatial constraint to narrow the search, it must not be
    allowed to blunder across the entire country's worth of stops. The
    downstream anchor_proxy injection in typed_token_dispatch (PIEZA 4)
    will take over as the coordinate source when the fuzzy matcher yields
    nothing.
    """
    bbox_south = params.get("bbox_south")
    bbox_west = params.get("bbox_west")
    bbox_north = params.get("bbox_north")
    bbox_east = params.get("bbox_east")
    if bbox_south is None and bbox_west is None and bbox_north is None and bbox_east is None:
        _LOG.warning(
            "[GROUNDING-BBOX-GUARD] Refusing unfiltered grounding query for hint_text=%r — "
            "all bbox params are None. This is a SAFE-FALLBACK guard added in Camino D "
            "PIEZA 3 (2026-04-09) to prevent cross-province pg_trgm leakage (e.g. "
            "ALAUSI-01 picking up Imbabura stops). Returning 0 rows; anchor_proxy "
            "injection downstream must supply coordinates.",
            params.get("hint_text"),
        )
        return []
    try:
        return fetch_all(connection, _GROUNDING_SQL, params)
    except Exception as exc:
        _LOG.warning("pg_trgm query failed (%s), using fallback", exc)
        return fetch_all(connection, _GROUNDING_SQL_FALLBACK, params)


def _proxy_match_for_hint(
    hint_text: str,
    *,
    operator_name: Optional[str],
    locality_values: List[str],
    expected_envelope: Optional[Dict[str, Any]],
    hint_geo: Dict[str, Any],
    province: Optional[str] = None,  # Camino D PIEZA 7
) -> Optional[StopMatch]:
    proxy_lon = hint_geo.get("proxy_lon")
    proxy_lat = hint_geo.get("proxy_lat")
    proxy_key = str(hint_geo.get("proxy_key") or "").strip()
    if proxy_lon is None or proxy_lat is None or not proxy_key:
        return None

    locality_match = any(
        value and proxy_key and (value in proxy_key or proxy_key in value)
        for value in locality_values
    )
    text_meta = grounding_text_alignment(
        hint_text=hint_text,
        stop_name=str(hint_geo.get("proxy_name") or hint_text),
        locality=proxy_key,
        province=province,
    )
    geo = grounding_geography_score(
        lon=float(proxy_lon),
        lat=float(proxy_lat),
        locality=proxy_key,
        stop_name=str(hint_geo.get("proxy_name") or hint_text),
        hint_text=hint_text,
        envelope=expected_envelope,
        province=province,
    )

    return StopMatch(
        stop_id=f"proxy:{proxy_key}",
        stop_name=str(hint_geo.get("proxy_name") or hint_text),
        aliases=[],
        locality=proxy_key,
        operator_id=None,
        lat=float(proxy_lat),
        lon=float(proxy_lon),
        name_similarity=0.10,
        alias_match=False,
        locality_match=locality_match,
        operator_match=bool(operator_name),
        composite_score=0.40,
        place_id=None,
        geography_score=float(geo.get("geography_score") or 0.0),
        in_expected_geography=bool(geo.get("in_expected_geography", True)),
        distance_to_expected_bbox_m=float(geo.get("distance_to_expected_bbox_m") or 0.0),
        locality_consistency_score=float(geo.get("locality_consistency_score") or 0.0),
        match_source="proxy_waypoint",
        text_alignment_score=float(text_meta.get("text_alignment_score") or 1.0),
        matched_locality_keys=list(text_meta.get("hint_locality_keys") or [proxy_key]),
        metadata={"operator": operator_name or "", "proxy_key": proxy_key},
    )


def ground_hint(
    hint_text: str,
    *,
    locality_hint: Optional[str] = None,
    locality_hints: Optional[List[str]] = None,
    operator_id: Optional[int] = None,
    operator_name: Optional[str] = None,
    max_results: int = 10,
    expected_envelope: Optional[Dict[str, Any]] = None,
    conn=None,
    province: Optional[str] = None,  # Camino D PIEZA 7: province routing token (None == sample_region legacy)
) -> List[StopMatch]:
    """Resolve a single text hint against the DB stop universe."""
    if not hint_text or not hint_text.strip():
        return []

    def _do_query(connection) -> List[StopMatch]:
        route_bbox = dict((expected_envelope or {}).get("bbox") or {})
        hint_geo = derive_hint_geography_context(hint_text, envelope=expected_envelope, province=province)
        hint_bbox = dict(hint_geo.get("hint_bbox") or {})
        candidate_pool_size = max(25, max_results * 6)

        rows: List[Dict[str, Any]] = []
        bboxes_to_try: List[Optional[Dict[str, Any]]] = []
        if hint_bbox:
            bboxes_to_try.append(hint_bbox)
        if route_bbox and route_bbox != hint_bbox:
            bboxes_to_try.append(route_bbox)
        if not bboxes_to_try:
            bboxes_to_try.append(None)

        for bbox in bboxes_to_try:
            params = {
                "hint_text": hint_text.strip(),
                "max_results": candidate_pool_size,
                "bbox_south": bbox.get("south") if bbox else None,
                "bbox_west": bbox.get("west") if bbox else None,
                "bbox_north": bbox.get("north") if bbox else None,
                "bbox_east": bbox.get("east") if bbox else None,
            }
            rows = _query_rows(connection, params=params)
            if rows:
                break

        if not rows and not hint_geo.get("hint_locality_keys") and route_bbox:
            rows = _query_rows(
                connection,
                params={
                    "hint_text": hint_text.strip(),
                    "max_results": candidate_pool_size,
                    "bbox_south": None,
                    "bbox_west": None,
                    "bbox_north": None,
                    "bbox_east": None,
                },
            )

        matches = []
        op_hint = normalize_hint_text(operator_name) if operator_name else ""
        locality_values = list(locality_hints or [])
        if locality_hint:
            locality_values.append(locality_hint)
        locality_values.extend(list((expected_envelope or {}).get("expected_localities") or []))
        locality_values = [normalize_hint_text(value) for value in locality_values if str(value or "").strip()]

        for row in rows:
            region = str(row.get("region") or "").strip().lower()
            operator = str(row.get("operator") or "").strip().lower()

            locality_match = any(
                value and region and (value in region or region in value)
                for value in locality_values
            )
            operator_match = bool(
                op_hint and operator and (op_hint in operator or operator in op_hint)
            )

            name_sim = float(row.get("name_sim") or 0.0)
            base_score = _compute_composite_score(
                name_similarity=name_sim,
                locality_match=locality_match,
                operator_match=operator_match,
            )
            geo = grounding_geography_score(
                lon=float(row.get("lon") or 0.0),
                lat=float(row.get("lat") or 0.0),
                locality=str(row.get("region") or ""),
                stop_name=str(row.get("stop_name") or ""),
                hint_text=hint_text,
                envelope=expected_envelope,
                province=province,
            )
            text_meta = grounding_text_alignment(
                hint_text=hint_text,
                stop_name=str(row.get("stop_name") or ""),
                locality=str(row.get("region") or ""),
                ref=str(row.get("ref") or ""),
                province=province,
            )
            composite = (
                (base_score * 0.38)
                + (float(geo.get("geography_score") or 0.0) * 0.22)
                + (float(text_meta.get("text_alignment_score") or 0.0) * 0.40)
            )

            if text_meta.get("hard_token_mismatch"):
                composite *= 0.18

            if hint_bbox:
                hint_distance_m = distance_to_bbox_m(
                    float(row.get("lon") or 0.0),
                    float(row.get("lat") or 0.0),
                    hint_bbox,
                )
                if hint_distance_m > 1200.0:
                    composite *= 0.25
            else:
                hint_distance_m = 0.0

            matches.append(StopMatch(
                stop_id=str(row["stop_id"]),
                stop_name=str(row.get("stop_name") or ""),
                aliases=[],
                locality=str(row.get("region") or ""),
                operator_id=None,
                lat=float(row.get("lat") or 0),
                lon=float(row.get("lon") or 0),
                name_similarity=name_sim,
                alias_match=False,
                locality_match=locality_match,
                operator_match=operator_match,
                composite_score=composite,
                place_id=row.get("place_id"),
                geography_score=float(geo.get("geography_score") or 0.0),
                in_expected_geography=bool(geo.get("in_expected_geography", True)),
                distance_to_expected_bbox_m=float(geo.get("distance_to_expected_bbox_m") or 0.0),
                locality_consistency_score=float(geo.get("locality_consistency_score") or 0.0),
                match_source="db_stop",
                text_alignment_score=float(text_meta.get("text_alignment_score") or 0.0),
                matched_locality_keys=list(text_meta.get("candidate_locality_keys") or []),
                metadata={
                    "operator": str(row.get("operator") or ""),
                    "place_id": row.get("place_id"),
                    "ref": str(row.get("ref") or ""),
                },
            ))

        proxy = _proxy_match_for_hint(
            hint_text,
            operator_name=operator_name,
            locality_values=locality_values,
            expected_envelope=expected_envelope,
            hint_geo=hint_geo,
            province=province,
        )
        if proxy is not None:
            best_text_score = max((match.text_alignment_score for match in matches), default=0.0)
            best_composite = max((match.composite_score for match in matches), default=0.0)
            if not matches or best_text_score < 0.60 or best_composite < 0.58:
                matches.append(proxy)

        matches.sort(key=lambda m: m.composite_score, reverse=True)
        return matches[:max_results]

    if conn is not None:
        return _do_query(conn)

    with db_conn(readonly=True) as connection:
        return _do_query(connection)


# ---------------------------------------------------------------------------
# OSM fallback grounding (Overpass)
# ---------------------------------------------------------------------------

try:
    from datamind_core.settings import OVERPASS_URL as _OVERPASS_URL
except ImportError:
    _OVERPASS_URL = os.getenv("OVERPASS_URL", "http://127.0.0.1:12346/api/interpreter")

_OVERPASS_STOP_QUERY = """
[out:json][timeout:30];
(
  node["highway"="bus_stop"]({{bbox}});
  node["public_transport"="platform"]({{bbox}});
  node["public_transport"="stop_position"]({{bbox}});
);
out body;
"""

_AREA_STOP_SQL = """
SELECT
    n.node_id::text AS stop_id,
    COALESCE(
        NULLIF(BTRIM(p.canonical_name), ''),
        NULLIF(BTRIM(n.name), ''),
        NULLIF(BTRIM(n.ref), ''),
        'stop_' || LEFT(n.node_id::text, 8)
    ) AS stop_name,
    n.operator,
    p.region,
    p.place_id::text AS place_id,
    n.ref,
    ST_Y(n.geom) AS lat,
    ST_X(n.geom) AS lon,
    COALESCE(route_stats.route_count, 0) AS route_count
FROM geo_prod.node_place_map m
JOIN node_prod.nodes n ON n.node_id = m.node_id
JOIN geo_prod.places p ON p.place_id = m.place_id
LEFT JOIN LATERAL (
    SELECT count(*) AS route_count
    FROM route_prod.routes r
    WHERE r.stop_node_ids IS NOT NULL
      AND n.node_id = ANY(r.stop_node_ids)
) route_stats ON TRUE
WHERE n.node_type = 'STOP'
  AND p.status = 'active'
  AND ST_Y(n.geom) BETWEEN %(bbox_south)s AND %(bbox_north)s
  AND ST_X(n.geom) BETWEEN %(bbox_west)s AND %(bbox_east)s
ORDER BY route_count DESC, stop_name
LIMIT %(max_results)s
"""

_LANDMARK_PLACE_SQL = """
WITH hint AS (
    SELECT lower(unaccent(%(hint_text)s)) AS norm_text
)
SELECT
    p.place_id::text AS place_id,
    p.canonical_name,
    p.region,
    ST_Y(p.geom) AS lat,
    ST_X(p.geom) AS lon,
    GREATEST(
        similarity(lower(unaccent(COALESCE(p.canonical_name, ''))), h.norm_text),
        COALESCE(MAX(similarity(COALESCE(pa.normalized_alias, ''), h.norm_text)), 0)
    ) AS name_sim
FROM geo_prod.places p
LEFT JOIN geo_prod.place_aliases pa ON pa.place_id = p.place_id
CROSS JOIN hint h
WHERE p.status = 'active'
GROUP BY p.place_id, p.canonical_name, p.region, p.geom, h.norm_text
HAVING (
    GREATEST(
        similarity(lower(unaccent(COALESCE(p.canonical_name, ''))), h.norm_text),
        COALESCE(MAX(similarity(COALESCE(pa.normalized_alias, ''), h.norm_text)), 0)
    ) > 0.22
    OR lower(unaccent(COALESCE(p.canonical_name, ''))) LIKE '%%' || h.norm_text || '%%'
)
ORDER BY name_sim DESC
LIMIT %(max_results)s
"""

_LANDMARK_NEARBY_STOP_SQL = """
SELECT
    n.node_id::text AS stop_id,
    COALESCE(
        NULLIF(BTRIM(p.canonical_name), ''),
        NULLIF(BTRIM(n.name), ''),
        NULLIF(BTRIM(n.ref), ''),
        'stop_' || LEFT(n.node_id::text, 8)
    ) AS stop_name,
    n.operator,
    p.region,
    p.place_id::text AS place_id,
    n.ref,
    ST_Y(n.geom) AS lat,
    ST_X(n.geom) AS lon,
    ST_Distance(
        n.geom::geography,
        ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography
    ) AS dist_m
FROM geo_prod.node_place_map m
JOIN node_prod.nodes n ON n.node_id = m.node_id
JOIN geo_prod.places p ON p.place_id = m.place_id
WHERE n.node_type = 'STOP'
  AND p.status = 'active'
  AND ST_DWithin(
      n.geom::geography,
      ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
      %(radius_m)s
  )
ORDER BY dist_m
LIMIT %(max_results)s
"""


def _norm(text: str) -> str:
    return normalize_hint_text(text or "")


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * radius_m * math.asin(math.sqrt(a))


def _approx_bbox_for_center(lat: float, lon: float, radius_m: float = 2000.0) -> Dict[str, float]:
    lat_delta = radius_m / 111000.0
    lon_delta = radius_m / max(111000.0 * math.cos(math.radians(lat)), 20000.0)
    return {
        "south": lat - lat_delta,
        "west": lon - lon_delta,
        "north": lat + lat_delta,
        "east": lon + lon_delta,
    }


def _bbox_for_area_label(
    label: str,
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    province: Optional[str] = None,  # Camino D PIEZA 7
) -> Tuple[Dict[str, float], Tuple[float, float]]:
    hint_geo = derive_hint_geography_context(label, envelope=expected_envelope, province=province)
    bbox = dict(hint_geo.get("hint_bbox") or {})
    if bbox:
        return bbox, (
            (float(bbox["south"]) + float(bbox["north"])) / 2.0,
            (float(bbox["west"]) + float(bbox["east"])) / 2.0,
        )

    # Camino D PIEZA 7: sample_region fallback ONLY for legacy callers (province None).
    # For non-sample_region provinces, hit the loader via PLACE_BBOXES for that
    # province. PLACE_BBOXES is the sample_region module-level shim; for other
    # provinces we route via the loader.
    hint_keys = infer_locality_keys([label], province=province)
    if province is None or str(province).strip().lower() in ("", "sample_region"):
        province_bboxes = PLACE_BBOXES
    else:
        from datamind_console.phases.phase3_routes.stop_grounding.place_geography_loader import (
            get_place_bboxes,
        )
        province_bboxes = get_place_bboxes(province)
    for key in hint_keys:
        if key in province_bboxes:
            south, west, north, east = province_bboxes[key]
            return (
                {
                    "south": south,
                    "west": west,
                    "north": north,
                    "east": east,
                },
                ((south + north) / 2.0, (west + east) / 2.0),
            )

    route_bbox = dict((expected_envelope or {}).get("bbox") or {})
    if route_bbox:
        return route_bbox, (
            (float(route_bbox["south"]) + float(route_bbox["north"])) / 2.0,
            (float(route_bbox["west"]) + float(route_bbox["east"])) / 2.0,
        )
    return _approx_bbox_for_center(-0.3130, -78.4500), (-0.3130, -78.4500)


def _route_usage_frequency_for_stop(stop_id: str, *, conn) -> int:
    if ":" in stop_id:
        return 0
    cached = _STOP_USAGE_CACHE.get(stop_id)
    if cached is not None:
        return cached
    try:
        rows = fetch_all(
            conn,
            """
            SELECT count(*) AS route_count
            FROM route_prod.routes r
            WHERE r.stop_node_ids IS NOT NULL
              AND %(stop_id)s::uuid = ANY(r.stop_node_ids)
            """,
            {"stop_id": stop_id},
        )
        route_count = int(rows[0]["route_count"]) if rows else 0
    except Exception:
        route_count = 0
    _STOP_USAGE_CACHE[stop_id] = route_count
    return route_count


def _operator_matches(operator_name: Optional[str], value: str) -> bool:
    lhs = _norm(operator_name or "")
    rhs = _norm(value or "")
    return bool(lhs and rhs and (lhs in rhs or rhs in lhs))


def _locality_matches(locality_hints: Optional[List[str]], value: str) -> bool:
    candidates = [_norm(item) for item in (locality_hints or []) if str(item or "").strip()]
    rhs = _norm(value or "")
    return any(lhs and rhs and (lhs in rhs or rhs in lhs) for lhs in candidates)


def _enrich_terminal_candidate_scores(
    candidates: List[StopMatch],
    *,
    operator_name: Optional[str],
    conn,
) -> List[StopMatch]:
    out: List[StopMatch] = []
    for candidate in candidates:
        route_count = _route_usage_frequency_for_stop(candidate.stop_id, conn=conn)
        terminal_bonus = 0.15 if any(keyword in _norm(candidate.stop_name) for keyword in _TERMINAL_KEYWORDS) else 0.0
        operator_bonus = 0.08 if _operator_matches(operator_name, candidate.metadata.get("operator", "")) else 0.0
        usage_bonus = min(0.18, route_count / 20.0)
        boosted = StopMatch(**candidate.__dict__)
        boosted.route_count = route_count
        boosted.composite_score = min(1.0, candidate.composite_score + terminal_bonus + usage_bonus + operator_bonus)
        boosted.metadata = dict(candidate.metadata or {})
        boosted.metadata["operator"] = candidate.metadata.get("operator") or ""
        out.append(boosted)
    out.sort(key=lambda item: item.composite_score, reverse=True)
    return out


def _geocode_landmark(
    hint_text: str,
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    conn=None,
    province: Optional[str] = None,  # Camino D PIEZA 7
) -> Optional[Dict[str, Any]]:
    norm_hint = _norm(hint_text)
    if norm_hint in KNOWN_LANDMARK_COORDS:
        lat, lon = KNOWN_LANDMARK_COORDS[norm_hint]
        return {"lat": lat, "lon": lon, "name": hint_text, "source": "known_landmark"}

    hint_geo = derive_hint_geography_context(hint_text, envelope=expected_envelope, province=province)
    proxy_lat = hint_geo.get("proxy_lat")
    proxy_lon = hint_geo.get("proxy_lon")
    if proxy_lat is not None and proxy_lon is not None:
        return {
            "lat": float(proxy_lat),
            "lon": float(proxy_lon),
            "name": str(hint_geo.get("proxy_name") or hint_text),
            "source": "place_bbox_proxy",
        }

    if conn is None:
        with db_conn(readonly=True) as connection:
            return _geocode_landmark(hint_text, expected_envelope=expected_envelope, conn=connection, province=province)

    rows = fetch_all(
        conn,
        _LANDMARK_PLACE_SQL,
        {"hint_text": hint_text, "max_results": 5},
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "lat": float(row.get("lat") or 0.0),
        "lon": float(row.get("lon") or 0.0),
        "name": str(row.get("canonical_name") or hint_text),
        "source": "geo_prod_place",
        "place_id": row.get("place_id"),
    }


def _osm_fallback_grounding(
    hint_text: str,
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    operator_name: Optional[str] = None,
    max_results: int = 5,
    province: Optional[str] = None,  # Camino D PIEZA 7
) -> List[StopMatch]:
    """
    Query Overpass for bus stops within the expected bbox and fuzzy-match
    against hint_text. Used as fallback when DB grounding yields no good match.
    """
    bbox = dict((expected_envelope or {}).get("bbox") or {})
    if not bbox or not all(k in bbox for k in ("south", "west", "north", "east")):
        _LOG.debug("OSM fallback: no bbox available, skipping")
        return []

    bbox_str = f"{bbox['south']},{bbox['west']},{bbox['north']},{bbox['east']}"
    query = _OVERPASS_STOP_QUERY.replace("{{bbox}}", bbox_str)

    try:
        resp = requests.post(_OVERPASS_URL, data={"data": query}, timeout=25)
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
    except Exception as exc:
        _LOG.warning("OSM fallback query failed: %s", exc)
        return []

    if not elements:
        return []

    hint_norm = _strip_accents(hint_text).strip().lower()
    matches: List[StopMatch] = []

    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name") or tags.get("ref") or ""
        if not name:
            continue

        name_norm = _strip_accents(name).strip().lower()

        # Simple token-overlap similarity
        hint_tokens = set(hint_norm.split())
        name_tokens = set(name_norm.split())
        if not hint_tokens or not name_tokens:
            continue

        overlap = len(hint_tokens & name_tokens)
        sim = overlap / max(len(hint_tokens), len(name_tokens))

        # Also check substring containment
        if hint_norm in name_norm or name_norm in hint_norm:
            sim = max(sim, 0.65)

        if sim < 0.25:
            continue

        lat = float(el.get("lat", 0))
        lon = float(el.get("lon", 0))
        osm_id = str(el.get("id", ""))

        geo = grounding_geography_score(
            lon=lon, lat=lat,
            locality="", stop_name=name,
            hint_text=hint_text,
            envelope=expected_envelope,
            province=province,
        )

        composite = sim * 0.55 + float(geo.get("geography_score") or 0.0) * 0.45

        matches.append(StopMatch(
            stop_id=f"osm:{osm_id}",
            stop_name=name,
            aliases=[],
            locality=tags.get("addr:city", ""),
            operator_id=None,
            lat=lat,
            lon=lon,
            name_similarity=sim,
            alias_match=False,
            locality_match=False,
            operator_match=False,
            composite_score=composite,
            place_id=None,
            geography_score=float(geo.get("geography_score") or 0.0),
            in_expected_geography=bool(geo.get("in_expected_geography", True)),
            distance_to_expected_bbox_m=float(geo.get("distance_to_expected_bbox_m") or 0.0),
            locality_consistency_score=0.0,
            match_source="osm_fallback",
            text_alignment_score=sim,
            matched_locality_keys=[],
            metadata={"operator": tags.get("operator", ""), "osm_id": osm_id},
        ))

    matches.sort(key=lambda m: m.composite_score, reverse=True)
    _LOG.info("OSM fallback for '%s': %d candidates found", hint_text, len(matches))
    return matches[:max_results]


def _ground_as_stop(
    hint_text: str,
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    operator_name: Optional[str] = None,
    locality_hints: Optional[List[str]] = None,
    max_results: int = 5,
    conn=None,
    province: Optional[str] = None,  # Camino D PIEZA 7
) -> List[StopMatch]:
    matches = ground_hint(
        hint_text,
        locality_hints=locality_hints,
        operator_name=operator_name,
        max_results=max_results,
        expected_envelope=expected_envelope,
        conn=conn,
        province=province,
    )
    if matches and matches[0].composite_score >= 0.50:
        return matches
    fallback = _osm_fallback_grounding(
        hint_text,
        expected_envelope=expected_envelope,
        operator_name=operator_name,
        max_results=max_results,
        province=province,
    )
    return _merge_top_matches(matches, fallback, max_results=max_results)


def _ground_as_terminal(
    hint_text: str,
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    operator_name: Optional[str] = None,
    locality_hints: Optional[List[str]] = None,
    max_results: int = 5,
    conn=None,
    province: Optional[str] = None,  # Camino D PIEZA 7
) -> List[StopMatch]:
    if conn is None:
        with db_conn(readonly=True) as connection:
            return _ground_as_terminal(
                hint_text,
                expected_envelope=expected_envelope,
                operator_name=operator_name,
                locality_hints=locality_hints,
                max_results=max_results,
                conn=connection,
                province=province,
            )

    matches = ground_hint(
        hint_text,
        locality_hints=locality_hints,
        operator_name=operator_name,
        max_results=max(max_results, 8),
        expected_envelope=expected_envelope,
        conn=conn,
        province=province,
    )
    enriched: List[StopMatch] = []
    for match in matches:
        operator = str(match.metadata.get("operator") or "")
        route_count = _route_usage_frequency_for_stop(match.stop_id, conn=conn)
        bonus = 0.15 if any(keyword in _norm(match.stop_name) for keyword in _TERMINAL_KEYWORDS) else 0.0
        bonus += min(0.12, route_count / 25.0)
        if _operator_matches(operator_name, operator):
            bonus += 0.10
        updated = StopMatch(**match.__dict__)
        updated.route_count = route_count
        updated.composite_score = min(1.0, match.composite_score + bonus)
        enriched.append(updated)
    enriched.sort(key=lambda item: item.composite_score, reverse=True)

    if enriched and enriched[0].composite_score >= 0.50:
        return enriched[:max_results]

    fallback = _osm_fallback_grounding(
        hint_text,
        expected_envelope=expected_envelope,
        operator_name=operator_name,
        max_results=max_results,
        province=province,
    )
    boosted_fallback = []
    for match in fallback:
        updated = StopMatch(**match.__dict__)
        if any(keyword in _norm(updated.stop_name) for keyword in _TERMINAL_KEYWORDS):
            updated.composite_score = min(1.0, updated.composite_score + 0.15)
        boosted_fallback.append(updated)
    merged = _merge_top_matches(enriched, boosted_fallback, max_results=max_results)
    return merged


def _search_sector_for_best_stop(
    hint_text: str,
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    operator_name: Optional[str] = None,
    locality_hints: Optional[List[str]] = None,
    max_results: int = 5,
    prefer_terminals: bool = True,
    conn=None,
    sector_bbox_override: Optional[Dict[str, float]] = None,
    province: Optional[str] = None,  # Camino D PIEZA 7
) -> List[StopMatch]:
    if sector_bbox_override:
        bbox = dict(sector_bbox_override)
        center = (
            (bbox["south"] + bbox["north"]) / 2.0,
            (bbox["west"] + bbox["east"]) / 2.0,
        )
    else:
        bbox, center = _bbox_for_area_label(hint_text, expected_envelope=expected_envelope, province=province)

    def _do_query(connection) -> List[StopMatch]:
        rows = fetch_all(
            connection,
            _AREA_STOP_SQL,
            {
                "bbox_south": bbox["south"],
                "bbox_west": bbox["west"],
                "bbox_north": bbox["north"],
                "bbox_east": bbox["east"],
                "max_results": max(max_results * 3, 15),
            },
        )
        candidates: List[StopMatch] = []
        center_lat, center_lon = center
        for row in rows:
            route_count = int(row.get("route_count") or 0)
            stop_name = str(row.get("stop_name") or "")
            locality = str(row.get("region") or "")
            distance_m = _haversine_m(center_lon, center_lat, float(row.get("lon") or 0.0), float(row.get("lat") or 0.0))
            route_score = min(1.0, route_count / 6.0)
            proximity_score = max(0.0, 1.0 - (distance_m / 2200.0))
            locality_score = 1.0 if _locality_matches(locality_hints, locality) else 0.0
            operator_score = 1.0 if _operator_matches(operator_name, str(row.get("operator") or "")) else 0.0
            terminal_bonus = 0.20 if (prefer_terminals and any(keyword in _norm(stop_name) for keyword in _TERMINAL_KEYWORDS)) else 0.0
            text_meta = grounding_text_alignment(
                hint_text=hint_text,
                stop_name=stop_name,
                locality=locality,
                ref=str(row.get("ref") or ""),
                province=province,
            )
            composite = (
                0.42 * route_score
                + 0.22 * proximity_score
                + 0.12 * locality_score
                + 0.10 * operator_score
                + 0.14 * float(text_meta.get("text_alignment_score") or 0.0)
                + terminal_bonus
            )
            geo = grounding_geography_score(
                lon=float(row.get("lon") or 0.0),
                lat=float(row.get("lat") or 0.0),
                locality=locality,
                stop_name=stop_name,
                hint_text=hint_text,
                envelope=expected_envelope,
                province=province,
            )
            candidates.append(
                StopMatch(
                    stop_id=str(row.get("stop_id") or ""),
                    stop_name=stop_name,
                    locality=locality,
                    lat=float(row.get("lat") or 0.0),
                    lon=float(row.get("lon") or 0.0),
                    composite_score=min(1.0, composite),
                    geography_score=float(geo.get("geography_score") or 0.0),
                    in_expected_geography=bool(geo.get("in_expected_geography", True)),
                    distance_to_expected_bbox_m=float(geo.get("distance_to_expected_bbox_m") or 0.0),
                    locality_consistency_score=float(geo.get("locality_consistency_score") or 0.0),
                    match_source="sector_search",
                    text_alignment_score=float(text_meta.get("text_alignment_score") or 0.0),
                    route_count=route_count,
                    distance_m=distance_m,
                    metadata={
                        "operator": str(row.get("operator") or ""),
                        "place_id": row.get("place_id"),
                        "resolved_from_sector": True,
                    },
                )
            )
        candidates.sort(key=lambda item: item.composite_score, reverse=True)
        return candidates[:max_results]

    if conn is not None:
        return _do_query(conn)
    with db_conn(readonly=True) as connection:
        return _do_query(connection)


def _ground_as_landmark(
    hint_text: str,
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    operator_name: Optional[str] = None,
    locality_hints: Optional[List[str]] = None,
    max_results: int = 5,
    conn=None,
    province: Optional[str] = None,  # Camino D PIEZA 7
) -> List[StopMatch]:
    def _do_query(connection) -> List[StopMatch]:
        geocoded = _geocode_landmark(
            hint_text,
            expected_envelope=expected_envelope,
            conn=connection,
            province=province,
        )
        if not geocoded:
            return []
        rows = fetch_all(
            connection,
            _LANDMARK_NEARBY_STOP_SQL,
            {
                "lon": geocoded["lon"],
                "lat": geocoded["lat"],
                "radius_m": 300,
                "max_results": max_results,
            },
        )
        candidates: List[StopMatch] = []
        for row in rows:
            geo = grounding_geography_score(
                lon=float(row.get("lon") or 0.0),
                lat=float(row.get("lat") or 0.0),
                locality=str(row.get("region") or ""),
                stop_name=str(row.get("stop_name") or ""),
                hint_text=hint_text,
                envelope=expected_envelope,
                province=province,
            )
            dist_m = float(row.get("dist_m") or 0.0)
            proximity_score = max(0.0, 1.0 - (dist_m / 300.0))
            operator_score = 0.08 if _operator_matches(operator_name, str(row.get("operator") or "")) else 0.0
            locality_score = 0.08 if _locality_matches(locality_hints, str(row.get("region") or "")) else 0.0
            candidates.append(
                StopMatch(
                    stop_id=str(row.get("stop_id") or ""),
                    stop_name=str(row.get("stop_name") or ""),
                    locality=str(row.get("region") or ""),
                    lat=float(row.get("lat") or 0.0),
                    lon=float(row.get("lon") or 0.0),
                    composite_score=min(1.0, 0.70 * proximity_score + operator_score + locality_score),
                    geography_score=float(geo.get("geography_score") or 0.0),
                    in_expected_geography=bool(geo.get("in_expected_geography", True)),
                    distance_to_expected_bbox_m=float(geo.get("distance_to_expected_bbox_m") or 0.0),
                    locality_consistency_score=float(geo.get("locality_consistency_score") or 0.0),
                    match_source="landmark_search",
                    route_count=_route_usage_frequency_for_stop(str(row.get("stop_id") or ""), conn=connection),
                    distance_m=dist_m,
                    metadata={
                        "landmark_name": geocoded["name"],
                        "geocode_source": geocoded["source"],
                        "resolved_from_landmark": True,
                    },
                )
            )
        candidates.sort(key=lambda item: item.composite_score, reverse=True)
        return candidates[:max_results]

    if conn is not None:
        return _do_query(conn)
    with db_conn(readonly=True) as connection:
        return _do_query(connection)


def _merge_top_matches(
    primary: Optional[List[StopMatch]],
    secondary: Optional[List[StopMatch]],
    *,
    max_results: int,
) -> List[StopMatch]:
    merged: Dict[str, StopMatch] = {}
    for group in (primary or [], secondary or []):
        if group is None:
            continue
        if isinstance(group, list):
            for candidate in group:
                existing = merged.get(candidate.stop_id)
                if existing is None or candidate.composite_score > existing.composite_score:
                    merged[candidate.stop_id] = candidate
        else:
            existing = merged.get(group.stop_id)
            if existing is None or group.composite_score > existing.composite_score:
                merged[group.stop_id] = group
    out = list(merged.values())
    out.sort(key=lambda item: item.composite_score, reverse=True)
    return out[:max_results]


# ---------------------------------------------------------------------------
# Full grounding for a RouteSeed
# ---------------------------------------------------------------------------

def ground_route_seed(
    seed: RouteSeed,
    *,
    max_candidates_per_hint: int = 5,
    conn=None,
) -> StopGroundingResult:
    """
    Ground all hints in a RouteSeed against the DB.
    Returns StopGroundingResult with ranked candidates per hint.
    """
    # Camino D PIEZA 7: read province from seed (stamped by discovery_pipeline).
    # None == sample_region (legacy retrocompat).
    _province = getattr(seed, "province", None)
    expected_envelope = seed.expected_geographic_envelope or derive_expected_geographic_envelope(
        route_name=seed.route_name,
        operator_name=seed.operator_name,
        corridor_description=seed.corridor_description,
        anchor_a_hint=seed.anchor_a_hint,
        anchor_b_hint=seed.anchor_b_hint,
        intermediate_hints=seed.intermediate_hints,
        locality_hints=seed.locality_hints,
        sequence_seed_fragments=seed.sequence_seed_fragments,
        source_notes=seed.source_notes,
        sector_key=seed.sector_key,
        province=_province,
    )
    locality_hint = (seed.locality_hints[0] if seed.locality_hints else None)

    # Ground anchor A
    anchor_a = ground_hint(
        seed.anchor_a_hint,
        locality_hint=locality_hint,
        locality_hints=seed.locality_hints,
        operator_name=seed.operator_name,
        max_results=max_candidates_per_hint,
        expected_envelope=expected_envelope,
        conn=conn,
        province=_province,
    )

    # OSM fallback for anchor A if DB grounding is weak
    if seed.anchor_a_hint and (not anchor_a or anchor_a[0].composite_score < 0.50):
        osm_a = _osm_fallback_grounding(
            seed.anchor_a_hint,
            expected_envelope=expected_envelope,
            operator_name=seed.operator_name,
            max_results=max_candidates_per_hint,
            province=_province,
        )
        if osm_a:
            anchor_a = (anchor_a or []) + osm_a
            anchor_a.sort(key=lambda m: m.composite_score, reverse=True)
            anchor_a = anchor_a[:max_candidates_per_hint]

    # Ground anchor B
    anchor_b = ground_hint(
        seed.anchor_b_hint,
        locality_hint=locality_hint,
        locality_hints=seed.locality_hints,
        operator_name=seed.operator_name,
        max_results=max_candidates_per_hint,
        expected_envelope=expected_envelope,
        conn=conn,
        province=_province,
    )

    # OSM fallback for anchor B if DB grounding is weak
    if seed.anchor_b_hint and (not anchor_b or anchor_b[0].composite_score < 0.50):
        osm_b = _osm_fallback_grounding(
            seed.anchor_b_hint,
            expected_envelope=expected_envelope,
            operator_name=seed.operator_name,
            max_results=max_candidates_per_hint,
            province=_province,
        )
        if osm_b:
            anchor_b = (anchor_b or []) + osm_b
            anchor_b.sort(key=lambda m: m.composite_score, reverse=True)
            anchor_b = anchor_b[:max_candidates_per_hint]

    # Ground intermediates
    intermediate_candidates: Dict[str, List[StopMatch]] = {}
    unmatched: List[str] = []
    for hint in seed.intermediate_hints:
        matches = ground_hint(
            hint,
            locality_hint=locality_hint,
            locality_hints=seed.locality_hints,
            operator_name=seed.operator_name,
            max_results=max_candidates_per_hint,
            expected_envelope=expected_envelope,
            conn=conn,
            province=_province,
        )
        if matches:
            intermediate_candidates[hint] = matches
        else:
            unmatched.append(hint)

    # Compute overall confidence
    total_hints = 2 + len(seed.intermediate_hints)
    matched_count = (1 if anchor_a else 0) + (1 if anchor_b else 0) + len(intermediate_candidates)
    match_rate = matched_count / total_hints if total_hints > 0 else 0.0

    best_scores = []
    if anchor_a:
        best_scores.append(anchor_a[0].composite_score)
    if anchor_b:
        best_scores.append(anchor_b[0].composite_score)
    for matches in intermediate_candidates.values():
        if matches:
            best_scores.append(matches[0].composite_score)

    avg_best = sum(best_scores) / len(best_scores) if best_scores else 0.0
    overall_confidence = 0.5 * match_rate + 0.5 * avg_best

    notes_parts = []
    if not anchor_a:
        notes_parts.append(f"Anchor A '{seed.anchor_a_hint}' unmatched")
    if not anchor_b:
        notes_parts.append(f"Anchor B '{seed.anchor_b_hint}' unmatched")
    if unmatched:
        notes_parts.append(f"{len(unmatched)} intermediate hint(s) unmatched: {unmatched}")

    proxy_count = 0
    for matches in [anchor_a, anchor_b, *intermediate_candidates.values()]:
        if matches and str(matches[0].match_source).startswith("proxy"):
            proxy_count += 1
    if proxy_count:
        notes_parts.append(f"proxy_waypoints_used={proxy_count}")

    notes_parts.append(
        f"expected_localities={list(expected_envelope.get('expected_localities') or [])}"
    )

    return StopGroundingResult(
        matched_anchor_a_candidates=anchor_a,
        matched_anchor_b_candidates=anchor_b,
        matched_intermediate_candidates=intermediate_candidates,
        unmatched_hints=unmatched,
        overall_grounding_confidence=overall_confidence,
        grounding_notes="; ".join(notes_parts) if notes_parts else "All hints grounded",
    )
