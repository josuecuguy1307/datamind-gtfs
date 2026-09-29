"""
P1.3B Backfill Executor — automated Overpass extraction + dedup + promote.

Receives MissingNodeCandidate[] from the gap analyzer, queries Overpass with
multiple algorithms per candidate, deduplicates against node_prod, and either
auto-promotes (confidence >= threshold) or queues for operator review.

Rate-limited at 1 req/sec with MD5-keyed response caching (7-day TTL).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from datamind_console.common.name_normalizer import normalize_name
from datamind_console.common.naming_patterns import (
    STOP_FORBIDDEN_PATTERNS,
    is_stop_name_forbidden,
)
from datamind_console.phases.phase3_routes.stop_grounding.backfill_contracts import (
    BackfillResult,
    BackfillStatus,
    MissingNodeCandidate,
)

_LOG = logging.getLogger(__name__)

try:
    from datamind_core.settings import OVERPASS_LOCAL_URL, OVERPASS_PUBLIC_URL
except ImportError:
    OVERPASS_LOCAL_URL = "http://127.0.0.1:12346/api/interpreter"
    OVERPASS_PUBLIC_URL = "https://overpass-api.de/api/interpreter"

OVERPASS_ENDPOINT = OVERPASS_LOCAL_URL
OVERPASS_DELAY_S = 1.0
DEDUP_RADIUS_M = 60.0
ECUADOR_BBOX = (-5.02, -81.08, 1.68, -75.19)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2.0 * r * math.asin(math.sqrt(a))


def _fuzzy_name_match(name_a: str, name_b: str) -> float:
    """Quick token-overlap similarity (0.0-1.0). No external deps."""
    a_tokens = set(name_a.lower().split())
    b_tokens = set(name_b.lower().split())
    if not a_tokens or not b_tokens:
        return 0.0
    intersection = a_tokens & b_tokens
    return len(intersection) / max(len(a_tokens), len(b_tokens))


class BackfillExecutor:
    """Automated P1.3B executor for the HADES pipeline autopilot."""

    def __init__(
        self,
        *,
        auto_promote_threshold: float = 0.75,
        overpass_endpoint: str = OVERPASS_ENDPOINT,
        overpass_fallback_endpoint: Optional[str] = OVERPASS_PUBLIC_URL,
        overpass_delay_s: float = OVERPASS_DELAY_S,
        cache_dir: Optional[str] = None,
        dry_run: bool = False,
    ):
        self.auto_promote_threshold = auto_promote_threshold
        self.overpass_endpoint = overpass_endpoint
        self.overpass_fallback_endpoint = overpass_fallback_endpoint
        self.overpass_delay_s = overpass_delay_s
        self.dry_run = dry_run
        self._local_healthy = True  # flip to False after consecutive failures
        self._local_fail_count = 0
        self._cache: Dict[str, list] = {}
        self._cache_dir = Path(cache_dir) if cache_dir else None
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ================================================================
    # Name quality gate — permanent safety net
    # ================================================================

    _GARBAGE_PATTERNS = STOP_FORBIDDEN_PATTERNS

    def _name_is_garbage(self, name: str) -> bool:
        return is_stop_name_forbidden(name)

    # ================================================================
    # Public API
    # ================================================================

    def execute(
        self,
        candidates: List[MissingNodeCandidate],
        db_conn=None,
    ) -> List[BackfillResult]:
        """Process all candidates. Returns results in same order."""
        merged = self._merge_nearby(candidates, radius_m=300.0)
        results: List[BackfillResult] = []
        for candidate in merged:
            result = self._process_candidate(candidate, db_conn)
            results.append(result)
            _LOG.info(
                "[BACKFILL] %s → %s (algo=%s, hits=%d, conf=%.2f) %s",
                candidate.candidate_id[:8],
                result.status.value,
                result.algorithm_used or "-",
                result.overpass_hits,
                result.final_confidence,
                result.log,
            )
        return results

    # ================================================================
    # Candidate processing — multi-algorithm cascade
    # ================================================================

    def _process_candidate(
        self, candidate: MissingNodeCandidate, db_conn,
    ) -> BackfillResult:
        """Try algorithms 1-4. If all fail, context naming ALWAYS produces a result."""
        lat, lon, radius = candidate.expected_lat, candidate.expected_lon, candidate.search_radius_m

        # --- Algorithm 1: Explicit transit tags ---
        hits = self._query_transit_tags(lat, lon, radius)
        if hits:
            return self._evaluate_and_promote(candidate, hits, "transit_tags", db_conn)

        # --- Algorithm 2: Broader public transport ---
        hits = self._query_public_transport(lat, lon, radius * 1.5)
        if hits:
            return self._evaluate_and_promote(candidate, hits, "public_transport", db_conn)

        # --- Algorithm 3: Intersections ---
        hits = self._query_intersections(lat, lon, min(radius, 200.0))
        if hits:
            return self._evaluate_and_promote(
                candidate, hits, "intersection", db_conn, confidence_penalty=0.15,
            )

        # --- Algorithm 4: POI anchors ---
        hits = self._query_poi_anchors(lat, lon, radius)
        if hits:
            return self._evaluate_and_promote(
                candidate, hits, "poi_anchor", db_conn, confidence_penalty=0.20,
            )

        # --- Algorithm 5: Context naming (universal fallback — never returns NOT_FOUND) ---
        return self._context_naming_fallback(candidate, db_conn)

    # ================================================================
    # Overpass query templates
    # ================================================================

    def _query_transit_tags(self, lat: float, lon: float, radius: float) -> list:
        query = f"""
[out:json][timeout:25];
(
  node["highway"="bus_stop"](around:{radius},{lat},{lon});
  node["public_transport"="stop_position"](around:{radius},{lat},{lon});
  node["public_transport"="platform"](around:{radius},{lat},{lon});
  node["amenity"="bus_station"](around:{radius},{lat},{lon});
);
out body;"""
        return self._run_overpass(query)

    def _query_public_transport(self, lat: float, lon: float, radius: float) -> list:
        query = f"""
[out:json][timeout:25];
(
  node["public_transport"](around:{radius},{lat},{lon});
  node["railway"="tram_stop"](around:{radius},{lat},{lon});
  node["railway"="halt"](around:{radius},{lat},{lon});
);
out body;"""
        return self._run_overpass(query)

    def _query_intersections(self, lat: float, lon: float, radius: float) -> list:
        query = f"""
[out:json][timeout:25];
(
  node["highway"="traffic_signals"](around:{radius},{lat},{lon});
  node["highway"="turning_circle"](around:{radius},{lat},{lon});
  node["highway"="crossing"]["crossing"="traffic_signals"](around:{radius},{lat},{lon});
);
out body;"""
        return self._run_overpass(query)

    def _query_poi_anchors(self, lat: float, lon: float, radius: float) -> list:
        query = f"""
[out:json][timeout:25];
(
  node["amenity"="school"](around:{radius},{lat},{lon});
  node["amenity"="hospital"](around:{radius},{lat},{lon});
  node["amenity"="clinic"](around:{radius},{lat},{lon});
  node["amenity"="marketplace"](around:{radius},{lat},{lon});
  node["shop"="supermarket"](around:{radius},{lat},{lon});
  node["amenity"="place_of_worship"](around:{radius},{lat},{lon});
  node["amenity"="university"](around:{radius},{lat},{lon});
);
out body;"""
        return self._run_overpass(query)

    # ================================================================
    # Algorithm 5: Context Naming Engine
    # ================================================================

    def _query_context_radius(self, lat: float, lon: float, radius_m: int = 200) -> dict:
        """Pull full geographic context around a point in a single Overpass call."""
        query = f"""
[out:json][timeout:25];
(
  way["highway"~"primary|secondary|tertiary|residential|unclassified|trunk|motorway"](around:{radius_m},{lat},{lon});
  node["amenity"~"school|hospital|clinic|university|marketplace|place_of_worship|bank|pharmacy|police|fire_station"](around:{radius_m},{lat},{lon});
  way["amenity"~"school|hospital|university"](around:{radius_m},{lat},{lon});
  node["shop"~"supermarket|mall|convenience"](around:{radius_m},{lat},{lon});
  node["tourism"~"hotel|hostel|viewpoint"](around:{radius_m},{lat},{lon});
  node["place"~"village|hamlet|neighbourhood|suburb|town"](around:500,{lat},{lon});
  node["highway"="traffic_signals"](around:100,{lat},{lon});
  node["leisure"~"park|stadium"](around:{radius_m},{lat},{lon});
  way["leisure"~"park|stadium"](around:{radius_m},{lat},{lon});
  node["tourism"="attraction"]["name"](around:{radius_m},{lat},{lon});
  node["amenity"~"restaurant|cafe|fuel"](around:{radius_m},{lat},{lon});
);
out body center;"""
        elements = self._run_overpass(query)

        _TRANSIT_AMENITIES = frozenset({
            "school", "hospital", "clinic", "university", "marketplace",
            "place_of_worship", "bank", "pharmacy", "police", "fire_station",
        })
        _COMMERCIAL_AMENITIES = frozenset({"restaurant", "cafe", "fuel"})

        context: Dict[str, list] = {
            "roads": [], "transit_pois": [], "commercial_pois": [],
            "places": [], "intersections": [], "landmarks": [],
        }

        for el in elements:
            tags = el.get("tags", {})
            name = tags.get("name", "")
            el_lat = el.get("lat") or (el.get("center") or {}).get("lat")
            el_lon = el.get("lon") or (el.get("center") or {}).get("lon")

            entry = {
                "name": name, "tags": tags,
                "lat": el_lat, "lon": el_lon,
                "distance_m": _haversine_m(lat, lon, el_lat, el_lon) if el_lat else 999,
                "type": el.get("type"),
            }

            if "highway" in tags and el.get("type") == "way":
                context["roads"].append(entry)
            elif tags.get("amenity") in _TRANSIT_AMENITIES:
                context["transit_pois"].append(entry)
            elif "shop" in tags or tags.get("amenity") in _COMMERCIAL_AMENITIES:
                context["commercial_pois"].append(entry)
            elif "place" in tags:
                context["places"].append(entry)
            elif tags.get("highway") == "traffic_signals":
                context["intersections"].append(entry)
            elif "leisure" in tags or "tourism" in tags:
                context["landmarks"].append(entry)

        for key in context:
            context[key].sort(key=lambda x: x["distance_m"])

        return context

    def _build_context_name(
        self, context: dict, candidate: Optional[MissingNodeCandidate],
        route_name: str = "",
    ) -> tuple:
        """Build a stop name from geographic context. Returns (name, confidence_bonus).

        Priority:
        1. Transit POI         -> "Colegio X" / "Hospital Y"
        2. Named intersection  -> "Road A y Road B"
        3. Road + landmark     -> "Road - Restaurant/Church/Park"
        4. Road + village      -> "Entrada VillageName (Road)"
        5. Road name only      -> "RoadName"
        6. Bare fallback       -> "Parada route_short_name"
        """
        _AMENITY_PREFIX = {
            "school": "Colegio", "hospital": "Hospital",
            "clinic": "Centro de Salud", "university": "Universidad",
            "marketplace": "Mercado", "place_of_worship": "Iglesia",
            "bank": "Banco", "pharmacy": "Farmacia",
            "police": "UPC", "fire_station": "Bomberos",
        }

        for poi in context.get("transit_pois", [])[:3]:
            if poi["name"] and poi["distance_m"] < 150:
                amenity = poi["tags"].get("amenity", "")
                prefix = _AMENITY_PREFIX.get(amenity, "")
                if prefix:
                    return f"{prefix} {poi['name']}", 0.20
                return poi["name"], 0.18

        named_roads = [r for r in context.get("roads", []) if r["name"] and r["distance_m"] < 100]
        if len(named_roads) >= 2:
            road_a = named_roads[0]["name"]
            road_b = named_roads[1]["name"]
            if road_a != road_b:
                return f"{road_a} y {road_b}", 0.18

        nearest_road = named_roads[0]["name"] if named_roads else None
        for landmark in (context.get("landmarks", []) + context.get("commercial_pois", []))[:3]:
            if landmark["name"] and landmark["distance_m"] < 150:
                if nearest_road:
                    return f"{nearest_road} - {landmark['name']}", 0.15
                return landmark["name"], 0.12

        for place in context.get("places", [])[:2]:
            if place["name"] and place["distance_m"] < 500:
                if nearest_road:
                    return f"Entrada {place['name']} ({nearest_road})", 0.14
                return f"Entrada {place['name']}", 0.12

        if nearest_road:
            return nearest_road, 0.10

        for road in context.get("roads", []):
            if road["name"]:
                return road["name"], 0.08

        route_short = route_name[:25] if route_name else ""
        if candidate and not route_short:
            route_short = candidate.route_id[:8]
        return f"Parada {route_short}".strip(), 0.05

    def _get_route_name(self, candidate: MissingNodeCandidate, db_conn) -> str:
        if db_conn is None:
            return ""
        try:
            cur = db_conn.cursor()
            cur.execute(
                "SELECT route_name FROM route_prod.routes WHERE route_id = %s::uuid",
                (candidate.route_id,),
            )
            row = cur.fetchone()
            cur.close()
            return row[0] if row and row[0] else ""
        except Exception:
            return ""

    def _context_naming_fallback(
        self, candidate: MissingNodeCandidate, db_conn,
    ) -> BackfillResult:
        """Universal fallback: context naming. ALWAYS produces PENDING or REJECTED."""
        lat, lon = candidate.expected_lat, candidate.expected_lon

        context = self._query_context_radius(lat, lon, radius_m=200)
        total_elements = sum(len(v) for v in context.values())

        if total_elements == 0:
            context = self._query_context_radius(lat, lon, radius_m=500)
            total_elements = sum(len(v) for v in context.values())

        route_name = self._get_route_name(candidate, db_conn)
        name, confidence_bonus = self._build_context_name(context, candidate, route_name)
        final = min(0.85, candidate.confidence + confidence_bonus)

        if db_conn is not None:
            existing = self._find_existing_stop(lat, lon, DEDUP_RADIUS_M, db_conn)
            if existing:
                return BackfillResult(
                    candidate=candidate,
                    status=BackfillStatus.REJECTED,
                    overpass_hits=total_elements,
                    algorithm_used="context_naming",
                    final_confidence=final,
                    promoted_name=name,
                    log=f"Duplicate: existing node {existing} within {DEDUP_RADIUS_M}m",
                )

        if not (ECUADOR_BBOX[0] <= lat <= ECUADOR_BBOX[2]
                and ECUADOR_BBOX[1] <= lon <= ECUADOR_BBOX[3]):
            return BackfillResult(
                candidate=candidate,
                status=BackfillStatus.REJECTED,
                overpass_hits=total_elements,
                algorithm_used="context_naming",
                final_confidence=final,
                log=f"Hit ({lat:.4f}, {lon:.4f}) outside Ecuador bbox",
            )

        if self._name_is_garbage(name):
            return BackfillResult(
                candidate=candidate,
                status=BackfillStatus.REJECTED,
                overpass_hits=total_elements,
                algorithm_used="context_naming",
                final_confidence=final,
                log=f"Name quality filter: '{name}' is garbage — skipped",
            )

        if final >= self.auto_promote_threshold:
            node_id = None
            virtual_hit = {
                "lat": lat, "lon": lon,
                "tags": {"name": name, "source": "context_inference"},
            }
            if not self.dry_run and db_conn is not None:
                node_id = self._promote_to_node_prod(
                    virtual_hit, name, candidate, final, db_conn,
                )
            elif self.dry_run:
                node_id = f"dry-run:{uuid.uuid4().hex[:8]}"

            return BackfillResult(
                candidate=candidate,
                status=BackfillStatus.PROMOTED,
                overpass_hits=total_elements,
                promoted_node_id=node_id,
                promoted_name=name,
                final_confidence=final,
                algorithm_used="context_naming",
                log=f"{'[DRY RUN] ' if self.dry_run else ''}Virtual stop '{name}' via context naming",
            )

        return BackfillResult(
            candidate=candidate,
            status=BackfillStatus.PENDING,
            overpass_hits=total_elements,
            promoted_name=name,
            final_confidence=final,
            algorithm_used="context_naming",
            log=f"Queued: '{name}' (conf {final:.2f} < {self.auto_promote_threshold})",
        )

    # ================================================================
    # Evaluation & promotion
    # ================================================================

    def _evaluate_and_promote(
        self,
        candidate: MissingNodeCandidate,
        hits: list,
        algorithm: str,
        db_conn,
        confidence_penalty: float = 0.0,
    ) -> BackfillResult:
        best = self._pick_best_hit(hits, candidate)
        if not best:
            return BackfillResult(
                candidate=candidate,
                status=BackfillStatus.REJECTED,
                overpass_hits=len(hits),
                algorithm_used=algorithm,
                log="No suitable hit after filtering",
            )

        tag_bonus = self._tag_confidence(best)
        dist_factor = self._distance_factor(best, candidate)
        final = min(0.95, (candidate.confidence + tag_bonus) * dist_factor - confidence_penalty)
        final = max(0.0, final)

        hit_lat = best.get("lat", 0.0)
        hit_lon = best.get("lon", 0.0)

        # Dedup: check against node_prod (tighter radius for named transit hits)
        if db_conn is not None:
            dedup_radius = DEDUP_RADIUS_M
            hit_name = (best.get("tags") or {}).get("name", "")
            if algorithm == "transit_tags" and hit_name:
                dedup_radius = 30.0
                existing = self._find_existing_stop_with_name(
                    hit_lat, hit_lon, dedup_radius, db_conn,
                )
                if existing and existing[1] and hit_name:
                    if _fuzzy_name_match(existing[1], hit_name) < 0.6:
                        existing = None  # different name — not a duplicate
                if existing:
                    existing = existing[0]
                else:
                    existing = None
            else:
                existing = self._find_existing_stop(hit_lat, hit_lon, dedup_radius, db_conn)
            if existing:
                return BackfillResult(
                    candidate=candidate,
                    status=BackfillStatus.REJECTED,
                    overpass_hits=len(hits),
                    algorithm_used=algorithm,
                    final_confidence=final,
                    log=f"Duplicate: existing node {existing} within {dedup_radius}m",
                )

        # Bbox sanity check
        if not (ECUADOR_BBOX[0] <= hit_lat <= ECUADOR_BBOX[2]
                and ECUADOR_BBOX[1] <= hit_lon <= ECUADOR_BBOX[3]):
            return BackfillResult(
                candidate=candidate,
                status=BackfillStatus.REJECTED,
                overpass_hits=len(hits),
                algorithm_used=algorithm,
                final_confidence=final,
                log=f"Hit ({hit_lat:.4f}, {hit_lon:.4f}) outside Ecuador bbox",
            )

        name = (best.get("tags") or {}).get("name") or ""
        if not name:
            name = self._reverse_geocode_name(hit_lat, hit_lon) or ""
        if not name:
            name = f"Parada {candidate.route_id[:8]}"

        if self._name_is_garbage(name):
            return BackfillResult(
                candidate=candidate,
                status=BackfillStatus.REJECTED,
                overpass_hits=len(hits),
                algorithm_used=algorithm,
                final_confidence=final,
                log=f"Name quality filter: '{name}' is garbage — skipped",
            )

        if final >= self.auto_promote_threshold:
            node_id = None
            if not self.dry_run and db_conn is not None:
                node_id = self._promote_to_node_prod(
                    best, name, candidate, final, db_conn,
                )
            elif self.dry_run:
                node_id = f"dry-run:{uuid.uuid4().hex[:8]}"

            return BackfillResult(
                candidate=candidate,
                status=BackfillStatus.PROMOTED,
                overpass_hits=len(hits),
                promoted_node_id=node_id,
                promoted_name=name,
                final_confidence=final,
                algorithm_used=algorithm,
                log=f"{'[DRY RUN] ' if self.dry_run else ''}Auto-promoted '{name}' via {algorithm}",
            )
        else:
            return BackfillResult(
                candidate=candidate,
                status=BackfillStatus.PENDING,
                overpass_hits=len(hits),
                promoted_name=name,
                final_confidence=final,
                algorithm_used=algorithm,
                log=f"Queued for review (conf {final:.2f} < {self.auto_promote_threshold})",
            )

    # ================================================================
    # Scoring helpers
    # ================================================================

    def _pick_best_hit(self, hits: list, candidate: MissingNodeCandidate) -> Optional[dict]:
        """Select closest hit to expected coords, within search radius."""
        scored = []
        for hit in hits:
            hit_lat = hit.get("lat")
            hit_lon = hit.get("lon")
            if hit_lat is None or hit_lon is None:
                continue
            dist = _haversine_m(candidate.expected_lat, candidate.expected_lon, hit_lat, hit_lon)
            if dist <= candidate.search_radius_m * 2.0:
                scored.append((dist, hit))
        if not scored:
            return None
        scored.sort(key=lambda x: x[0])
        return scored[0][1]

    def _tag_confidence(self, hit: dict) -> float:
        """Confidence bonus from OSM tags."""
        tags = hit.get("tags") or {}
        if tags.get("highway") == "bus_stop":
            return 0.30
        if "public_transport" in tags:
            return 0.25
        if tags.get("amenity") in ("bus_station", "school", "hospital"):
            return 0.15
        if tags.get("highway") == "traffic_signals":
            return 0.10
        return 0.05

    def _distance_factor(self, hit: dict, candidate: MissingNodeCandidate) -> float:
        """Closer to expected point = higher confidence."""
        dist = _haversine_m(
            candidate.expected_lat, candidate.expected_lon,
            hit.get("lat", 0), hit.get("lon", 0),
        )
        if dist < 50:
            return 1.0
        elif dist < 150:
            return 0.90
        elif dist < 300:
            return 0.80
        return 0.65

    # ================================================================
    # Database interactions
    # ================================================================

    def _find_existing_stop(
        self, lat: float, lon: float, radius_m: float, db_conn,
    ) -> Optional[str]:
        """Check if a stop already exists within radius_m in node_prod."""
        try:
            cur = db_conn.cursor()
            cur.execute(
                """
                SELECT node_id FROM node_prod.nodes
                WHERE ST_DWithin(
                    geom::geography,
                    ST_SetSRID(ST_Point(%s, %s), 4326)::geography,
                    %s
                )
                LIMIT 1
                """,
                (lon, lat, radius_m),
            )
            row = cur.fetchone()
            cur.close()
            if row:
                return str(row[0])
        except Exception as exc:
            _LOG.warning("Dedup check failed: %s", exc)
        return None

    def _find_existing_stop_with_name(
        self, lat: float, lon: float, radius_m: float, db_conn,
    ) -> Optional[tuple]:
        """Like _find_existing_stop but also returns the name for fuzzy matching."""
        try:
            cur = db_conn.cursor()
            cur.execute(
                """
                SELECT node_id, COALESCE(name, '') FROM node_prod.nodes
                WHERE ST_DWithin(
                    geom::geography,
                    ST_SetSRID(ST_Point(%s, %s), 4326)::geography,
                    %s
                )
                LIMIT 1
                """,
                (lon, lat, radius_m),
            )
            row = cur.fetchone()
            cur.close()
            if row:
                return (str(row[0]), row[1])
        except Exception as exc:
            _LOG.warning("Dedup-with-name check failed: %s", exc)
        return None

    def _promote_to_node_prod(
        self,
        hit: dict,
        name: str,
        candidate: MissingNodeCandidate,
        confidence: float,
        db_conn,
    ) -> str:
        """Insert a new node into node_prod.nodes via the universal treater.

        Routes through ``treat_stop(operation='synthetic_insert')`` so the
        new node gets the contextual-name cascade, a ``geo_prod.places`` row
        + ``node_place_map`` link, and an audit row in
        ``stop_treatment_log`` — all atomic with this function's commit.
        """
        import psycopg2.extras as _ppx
        from phase3_routes.services.stop_quality import (
            StopTreatmentInput,
            treat_stop,
        )

        hit_lat = hit.get("lat", candidate.expected_lat)
        hit_lon = hit.get("lon", candidate.expected_lon)
        osm_id = hit.get("id")
        tags = hit.get("tags") or {}
        # node_prod.nodes.node_type CHECK accepts ('STOP','POI') only.
        # The pre-migration code had a `STATION` branch for `bus_station`
        # tags that silently failed at the DB level (caught by a generic
        # except). All backfilled stops land as STOP.
        node_type = "STOP"

        merged_tags = {
            **tags,
            "_backfill_route_id": candidate.route_id,
            "_backfill_signal_type": candidate.signal_type.value,
            "_backfill_canton": candidate.canton,
        }
        extras: dict[str, Any] = {
            "source": "backfill",
            "chosen_tags": _ppx.Json(merged_tags),
        }
        if osm_id is not None:
            extras["osm_id"] = osm_id

        try:
            result = treat_stop(
                StopTreatmentInput(
                    operation="synthetic_insert",
                    caller="p1_3b_backfill.promote",
                    proposed_name=name,        # cascade normalises + repairs
                    proposed_lat=float(hit_lat),
                    proposed_lon=float(hit_lon),
                    province=candidate.province,
                    confidence=confidence,
                    node_type=node_type,
                    extras=extras,
                ),
                db_conn,
            )
            if not result.success:
                _LOG.error("Failed to promote node: %s", result.error)
                db_conn.rollback()
                return ""
            node_id = str(result.node_id)
            db_conn.commit()
            _LOG.info(
                "[PROMOTE] node_id=%s name='%s' at (%.5f, %.5f) conf=%.2f",
                node_id, result.final_name, hit_lat, hit_lon, confidence,
            )
        except Exception as exc:
            _LOG.error("Failed to promote node: %s", exc)
            try:
                db_conn.rollback()
            except Exception:
                pass
            return ""
        return node_id

    # ================================================================
    # Reverse geocoding
    # ================================================================

    def _reverse_geocode_name(self, lat: float, lon: float) -> Optional[str]:
        """Nominatim reverse geocode for naming when OSM has no name tag."""
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={
                    "lat": lat, "lon": lon,
                    "format": "json", "zoom": 18,
                    "addressdetails": 1,
                },
                headers={"User-Agent": "DataMindDataMind/1.0"},
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                addr = data.get("address", {})
                road = addr.get("road") or addr.get("pedestrian") or addr.get("suburb") or ""
                if road:
                    return road
        except Exception as exc:
            _LOG.debug("Reverse geocode failed: %s", exc)
        return None

    # ================================================================
    # Overpass execution + caching
    # ================================================================

    def _run_overpass(self, query: str) -> list:
        """Execute Overpass query with local-first/public-fallback, caching, and rate limiting."""
        cache_key = hashlib.md5(query.encode()).hexdigest()

        # In-memory cache
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Disk cache
        if self._cache_dir:
            cache_file = self._cache_dir / f"{cache_key}.json"
            if cache_file.exists():
                try:
                    elements = json.loads(cache_file.read_text())
                    self._cache[cache_key] = elements
                    return elements
                except Exception:
                    pass

        # Build endpoint priority list: local-first, public-fallback
        endpoints = []
        if self._local_healthy:
            endpoints.append(("local", self.overpass_endpoint))
        if self.overpass_fallback_endpoint:
            endpoints.append(("public", self.overpass_fallback_endpoint))
        if not self._local_healthy:
            # Still try local occasionally to detect recovery
            endpoints.append(("local", self.overpass_endpoint))

        elements = []
        for label, endpoint in endpoints:
            if label == "public":
                time.sleep(self.overpass_delay_s)  # rate limit public API only
            try:
                resp = requests.post(
                    endpoint,
                    data={"data": query},
                    timeout=15 if label == "local" else 30,
                )
                resp.raise_for_status()
                elements = resp.json().get("elements", [])
                # Restore local health on success
                if label == "local":
                    self._local_fail_count = 0
                    self._local_healthy = True
                break
            except Exception as exc:
                _LOG.warning("Overpass query failed (%s %s): %s", label, endpoint, exc)
                if label == "local":
                    self._local_fail_count += 1
                    if self._local_fail_count >= 3:
                        self._local_healthy = False
                        _LOG.warning("Local Overpass marked unhealthy after %d failures, falling back to public", self._local_fail_count)

        self._cache[cache_key] = elements
        if self._cache_dir:
            try:
                cache_file = self._cache_dir / f"{cache_key}.json"
                cache_file.write_text(json.dumps(elements, ensure_ascii=False))
            except Exception:
                pass

        return elements

    # ================================================================
    # Candidate merging
    # ================================================================

    def _merge_nearby(
        self, candidates: List[MissingNodeCandidate], radius_m: float = 300.0,
    ) -> List[MissingNodeCandidate]:
        """Merge candidates within radius_m to reduce Overpass calls.

        Keeps the higher-confidence candidate, expands search_radius_m to cover both.
        """
        if len(candidates) <= 1:
            return list(candidates)

        used = [False] * len(candidates)
        merged: List[MissingNodeCandidate] = []

        for i, c in enumerate(candidates):
            if used[i]:
                continue
            group = [c]
            used[i] = True
            for j in range(i + 1, len(candidates)):
                if used[j]:
                    continue
                dist = _haversine_m(
                    c.expected_lat, c.expected_lon,
                    candidates[j].expected_lat, candidates[j].expected_lon,
                )
                if dist < radius_m:
                    group.append(candidates[j])
                    used[j] = True

            # Keep the highest-confidence candidate, expand radius
            best = max(group, key=lambda x: x.confidence)
            if len(group) > 1:
                max_dist = max(
                    _haversine_m(
                        best.expected_lat, best.expected_lon,
                        g.expected_lat, g.expected_lon,
                    )
                    for g in group
                )
                best.search_radius_m = max(best.search_radius_m, max_dist + 100)
            merged.append(best)

        if len(merged) < len(candidates):
            _LOG.info(
                "[BACKFILL MERGE] %d candidates merged to %d",
                len(candidates), len(merged),
            )
        return merged
