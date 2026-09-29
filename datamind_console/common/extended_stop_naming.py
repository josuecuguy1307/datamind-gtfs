"""Extended stop-naming cascade — 4 tiers, never returns "Parada Aislada".

Composed from existing canonical primitives:

  TIER 1 — Module B (Overpass-rich, 200m → 500m progressive)
      Source: BackfillExecutor._query_context_radius + _build_context_name
      Outputs: "Colegio X" / "Av. A y Av. B" / "Road - Landmark" /
               "Entrada Place (Road)" / "Av. Road"

  TIER 2 — Module A (DB-only, fast)
      Source: phase2_semantics contextual_name_generator.generate_contextual_name
      Cascade: intersection 60m / landmark 75m / sector 300m
      The orphan branch (>=2km) is deliberately skipped here — escalates to TIER 3.

  TIER 3 — Rural extended (Overpass progressive radius + admin is_in)
      Always finds something in a populated province.
      9.  nearest named POI at 1km / 5km / 10km   → "Cerca de {POI}"
      10. nearest named road at 1km / 5km / 10km  → "Parada en {Road}"
      11. parroquia (admin_level=8) via is_in     → "Parada en {Parroquia}"
      12. canton    (admin_level=6) via is_in     → "Parada en {Canton}"

  TIER 4 — Last resort
      13. "Parada {route_short}" — route_name from cohort lookup
           Only used if every tier above failed AND a cohort route refs node_id.

If TIER 4 also fails, raise ExtendedNamingError. Silent fallback to a
forbidden string is forbidden by design.

Public API:
    compute_extended_stop_name(conn, *, lat, lon, original_name, node_id=None,
                               executor=None) -> ExtendedNameResult

The caller passes a psycopg2 connection (Module A and TIER 4 use SQL).
Overpass calls go through a shared BackfillExecutor (created on demand if
no executor is passed); pass an executor to share its disk cache across
many calls.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from datamind_console.common.naming_patterns import is_stop_name_forbidden
from datamind_console.common.name_normalizer import normalize_name

log = logging.getLogger(__name__)


class ExtendedNamingError(RuntimeError):
    """Raised when every tier (1..4) failed. Should never happen in
    populated provinces; existence indicates an Overpass outage or a stop
    placed in genuinely empty space (Antarctica, ocean)."""


@dataclass
class ExtendedNameResult:
    new_name: str
    tier: str            # one of TIER_LABELS keys
    confidence: float    # 0..1
    distance_m: Optional[float] = None


# ─── Module B (BackfillExecutor) loader ──────────────────────────────────────


_DEFAULT_EXECUTOR = None


def _get_default_executor():
    global _DEFAULT_EXECUTOR
    if _DEFAULT_EXECUTOR is None:
        from datamind_console.orchestrator.executors.p1_3b_backfill_executor import (
            BackfillExecutor,
        )
        _DEFAULT_EXECUTOR = BackfillExecutor(dry_run=True)
    return _DEFAULT_EXECUTOR


# ─── Module A (DB cascade) loader ────────────────────────────────────────────


import importlib.util as _ilu
import sys as _sys
from pathlib import Path as _Path

_GEN_PATH = (
    _Path(__file__).resolve().parents[2]
    / "phase2_semantics/src/pipeline/naming/contextual_name_generator.py"
)
_spec = _ilu.spec_from_file_location("_cng_for_extended", _GEN_PATH)
_cng = _ilu.module_from_spec(_spec)
_sys.modules["_cng_for_extended"] = _cng
_spec.loader.exec_module(_cng)
_find_intersection = _cng.find_nearest_intersection
_find_landmark = _cng.find_nearest_landmark
_find_sector = _cng.find_nearest_sector
_proximity_prefix = _cng._proximity_prefix


# ─── TIER 1 — Module B (Overpass 200m → 500m) ────────────────────────────────


def _is_bare_parada_fallback(name: str) -> bool:
    """Module B's terminal `Parada {route_short}` shape — caller should
    skip it during TIER 1 so we escalate to TIER 2/3 and try harder."""
    return bool(re.match(r"^Parada\s+\S", name)) and len(name) < 30 and " " in name and name.split(" ", 1)[1].strip() != ""


def _try_tier1(executor, lat: float, lon: float) -> Optional[ExtendedNameResult]:
    """Module B at 200m, then 500m. Skip bare `Parada {x}` fallback."""
    for radius in (200, 500):
        try:
            ctx = executor._query_context_radius(lat, lon, radius_m=radius)
        except Exception as exc:
            log.warning("tier1 query @ %dm failed: %r", radius, exc)
            continue
        if sum(len(v) for v in ctx.values()) == 0:
            continue
        try:
            name, bonus = executor._build_context_name(ctx, candidate=None, route_name="")
        except Exception as exc:
            log.warning("tier1 build_context_name @ %dm failed: %r", radius, exc)
            continue
        if not name:
            continue
        # Module B's last branch returns "Parada " (empty when no route_name) —
        # skip ANY "Parada something" fallback here so we escalate.
        if name.strip().lower().startswith("parada"):
            continue
        if is_stop_name_forbidden(name):
            continue
        normalized = normalize_name(name) or name
        if is_stop_name_forbidden(normalized):
            continue
        # Detect which sub-tier was selected by checking the context payload.
        sub = _classify_tier1_subtier(ctx, normalized, radius)
        return ExtendedNameResult(
            new_name=normalized,
            tier=sub,
            confidence=min(0.85, 0.5 + bonus),
        )
    return None


def _classify_tier1_subtier(ctx: dict, name: str, radius: int) -> str:
    """Heuristic: figure out which Module B branch produced `name` for telemetry."""
    if " y " in name:
        return f"tier1_b_intersection_{radius}m"
    if name.startswith(("Colegio ", "Hospital ", "Centro de Salud ", "Universidad ",
                         "Mercado ", "Iglesia ", "Banco ", "Farmacia ", "UPC ", "Bomberos ")):
        return f"tier1_b_transit_poi_{radius}m"
    if " - " in name:
        return f"tier1_b_road_landmark_{radius}m"
    if name.startswith("Entrada "):
        return f"tier1_b_road_place_{radius}m"
    return f"tier1_b_road_{radius}m"


# ─── TIER 2 — Module A (DB-only) ─────────────────────────────────────────────


def _try_tier2(conn, lat: float, lon: float) -> Optional[ExtendedNameResult]:
    """Module A's 60m intersection / 75m landmark / 300m sector. Skip orphan."""
    if conn is None:
        return None

    # 1. intersection (60m)
    inter = _find_intersection(conn, lat, lon, max_distance_m=60)
    if inter:
        candidate = f"{inter.road_primary} y {inter.road_secondary}"
        normalized = normalize_name(candidate) or candidate
        if not is_stop_name_forbidden(normalized):
            return ExtendedNameResult(
                new_name=normalized, tier="tier2_a_intersection_60m",
                confidence=0.78,
                distance_m=inter.primary_distance_m,
            )

    # 2. landmark (75m)
    lm = _find_landmark(conn, lat, lon, max_distance_m=75)
    if lm:
        prefix = _proximity_prefix(lm.distance_m)
        candidate = f"{prefix} {lm.canonical_name}"
        normalized = normalize_name(candidate) or candidate
        if not is_stop_name_forbidden(normalized):
            return ExtendedNameResult(
                new_name=normalized, tier="tier2_a_landmark_75m",
                confidence=0.70, distance_m=lm.distance_m,
            )

    # 3. sector (300m)
    sec = _find_sector(conn, lat, lon, max_distance_m=300)
    if sec:
        candidate = f"Parada en {sec}"
        normalized = normalize_name(candidate) or candidate
        if not is_stop_name_forbidden(normalized):
            return ExtendedNameResult(
                new_name=normalized, tier="tier2_a_sector_300m",
                confidence=0.55,
            )

    return None


# ─── TIER 3 — Rural extended (Overpass progressive + admin is_in) ────────────


_POI_QUERY_TMPL = """
[out:json][timeout:25];
(
  node["amenity"~"^(school|hospital|clinic|university|marketplace|place_of_worship|police|fire_station|townhall|community_centre|bus_station|fuel)$"]["name"](around:{r},{lat},{lon});
  way["amenity"~"^(school|hospital|university|marketplace|place_of_worship)$"]["name"](around:{r},{lat},{lon});
  node["place"~"^(village|hamlet|neighbourhood|suburb|town|city|locality)$"]["name"](around:{r},{lat},{lon});
  node["leisure"~"^(park|stadium)$"]["name"](around:{r},{lat},{lon});
  node["tourism"="attraction"]["name"](around:{r},{lat},{lon});
);
out center 30;"""

_ROAD_QUERY_TMPL = """
[out:json][timeout:25];
way["highway"~"^(primary|secondary|tertiary|trunk|motorway|residential|unclassified)$"]["name"](around:{r},{lat},{lon});
out center 30;"""

_ADMIN_QUERY_TMPL = """
[out:json][timeout:25];
is_in({lat},{lon});
relation._["boundary"="administrative"]["admin_level"="{lvl}"]["name"];
out tags 5;"""


def _haversine_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    import math
    r = 6371000.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _nearest_named_from(elements: list, lat: float, lon: float) -> Optional[tuple[str, float]]:
    """Return (name, distance_m) of the nearest element with a tag 'name'."""
    best = None
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name") or ""
        if not name.strip():
            continue
        el_lat = el.get("lat") or (el.get("center") or {}).get("lat")
        el_lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if el_lat is None or el_lon is None:
            continue
        d = _haversine_m(lat, lon, el_lat, el_lon)
        if best is None or d < best[1]:
            best = (name.strip(), d)
    return best


def _try_tier3_poi(executor, lat: float, lon: float) -> Optional[ExtendedNameResult]:
    """Expanding-radius POI search: 1km → 5km → 10km."""
    for radius in (1000, 5000, 10000):
        q = _POI_QUERY_TMPL.format(r=radius, lat=lat, lon=lon)
        try:
            els = executor._run_overpass(q)
        except Exception as exc:
            log.warning("tier3 POI %dm failed: %r", radius, exc)
            continue
        nearest = _nearest_named_from(els, lat, lon)
        if not nearest:
            continue
        poi_name, dist = nearest
        candidate = f"Cerca de {poi_name}"
        normalized = normalize_name(candidate) or candidate
        if is_stop_name_forbidden(normalized):
            continue
        return ExtendedNameResult(
            new_name=normalized,
            tier=f"tier3_poi_{radius}m",
            confidence=max(0.20, 0.45 - (dist / 20000.0)),
            distance_m=dist,
        )
    return None


def _try_tier3_road(executor, lat: float, lon: float) -> Optional[ExtendedNameResult]:
    """Expanding-radius road search: 1km → 5km → 10km."""
    for radius in (1000, 5000, 10000):
        q = _ROAD_QUERY_TMPL.format(r=radius, lat=lat, lon=lon)
        try:
            els = executor._run_overpass(q)
        except Exception as exc:
            log.warning("tier3 road %dm failed: %r", radius, exc)
            continue
        nearest = _nearest_named_from(els, lat, lon)
        if not nearest:
            continue
        road_name, dist = nearest
        candidate = f"Parada en {road_name}"
        normalized = normalize_name(candidate) or candidate
        if is_stop_name_forbidden(normalized):
            continue
        return ExtendedNameResult(
            new_name=normalized,
            tier=f"tier3_road_{radius}m",
            confidence=max(0.18, 0.40 - (dist / 20000.0)),
            distance_m=dist,
        )
    return None


def _try_tier3_admin(executor, lat: float, lon: float, level: int, label: str) -> Optional[ExtendedNameResult]:
    """admin_level=8 (parroquia) or 6 (canton) via Overpass `is_in`."""
    q = _ADMIN_QUERY_TMPL.format(lat=lat, lon=lon, lvl=level)
    try:
        els = executor._run_overpass(q)
    except Exception as exc:
        log.warning("tier3 admin lvl=%d failed: %r", level, exc)
        return None
    # is_in returns relations directly; use first with a name.
    for el in els:
        tags = el.get("tags") or {}
        nm = tags.get("name") or ""
        if not nm.strip():
            continue
        candidate = f"Parada en {nm.strip()}"
        normalized = normalize_name(candidate) or candidate
        if is_stop_name_forbidden(normalized):
            continue
        return ExtendedNameResult(
            new_name=normalized,
            tier=f"tier3_{label}",
            confidence=0.30 if level == 8 else 0.25,
        )
    return None


# ─── TIER 4 — route_short_name from cohort lookup ────────────────────────────


def _route_for_node(conn, node_id) -> Optional[str]:
    """Return one cohort-active route_name that references this node_id.
    Used only as last-resort name suffix; first-match is fine."""
    if conn is None or node_id is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT route_name
                  FROM route_prod.routes
                 WHERE %s::uuid = ANY(stop_node_ids)
                   AND deploy_status = 'active'
                   AND route_name IS NOT NULL
                   AND LENGTH(TRIM(route_name)) > 0
                 ORDER BY LENGTH(route_name) ASC
                 LIMIT 1
                """,
                (str(node_id),),
            )
            row = cur.fetchone()
            if not row:
                return None
            # Tolerate dict-cursor and tuple-cursor.
            return row[0] if not isinstance(row, dict) else row.get("route_name")
    except Exception as exc:
        log.debug("route lookup failed for %s: %r", node_id, exc)
        return None


def _try_tier4(conn, node_id) -> Optional[ExtendedNameResult]:
    rn = _route_for_node(conn, node_id)
    if not rn:
        return None
    rn = rn.strip()[:25].strip()
    candidate = f"Parada {rn}"
    normalized = normalize_name(candidate) or candidate
    if is_stop_name_forbidden(normalized):
        return None
    return ExtendedNameResult(
        new_name=normalized, tier="tier4_route", confidence=0.20,
    )


# ─── Public API ──────────────────────────────────────────────────────────────


def compute_extended_stop_name(
    conn,
    *,
    lat: float,
    lon: float,
    original_name: Optional[str] = None,
    node_id=None,
    executor=None,
) -> ExtendedNameResult:
    """Run the full 13-step cascade. Never returns `Parada Aislada`.

    Raises ExtendedNamingError if every tier failed (Overpass outage or
    genuinely empty geography).
    """
    if lat is None or lon is None:
        raise ExtendedNamingError("compute_extended_stop_name requires lat & lon")

    ex = executor or _get_default_executor()

    # TIER 1 — Module B Overpass
    r = _try_tier1(ex, lat, lon)
    if r:
        return r

    # TIER 2 — Module A DB
    r = _try_tier2(conn, lat, lon)
    if r:
        return r

    # TIER 3 — rural extended
    r = _try_tier3_poi(ex, lat, lon)
    if r:
        return r
    r = _try_tier3_road(ex, lat, lon)
    if r:
        return r
    r = _try_tier3_admin(ex, lat, lon, level=8, label="parroquia")
    if r:
        return r
    r = _try_tier3_admin(ex, lat, lon, level=6, label="canton")
    if r:
        return r

    # TIER 4 — route_short
    r = _try_tier4(conn, node_id)
    if r:
        return r

    raise ExtendedNamingError(
        f"all tiers exhausted at ({lat:.5f}, {lon:.5f}); "
        "Overpass likely down or stop placed outside any populated admin polygon"
    )
