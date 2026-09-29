"""
Stage B2 — Territorial Token Resolution

Classifies route seed tokens as territorial vs stop-like, generates
candidate stops for territorial tokens via spatial search (bbox-based,
no text similarity required), and optimizes the full waypoint chain
using beam search / Viterbi-style dynamic programming.

This stage runs between Stage B (stop grounding) and Stage C (corridor
building). It enriches the waypoint set so that territorial tokens like
"Sangolquí", "Rumiloma", "Loreto" produce real stop candidates instead
of relying on weak text matches or proxy waypoints.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from datamind_console.db.db import db_conn, fetch_all
from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    RouteSeed,
    StopGroundingResult,
    StopMatch,
)
from datamind_console.phases.phase3_routes.stop_grounding.geography_guardrails import (
    PLACE_ALIASES,
    PLACE_BBOXES,
    PLACE_DISPLAY_NAMES,
    VALLE_LOCALITY_KEYS,
    bbox_center,
    infer_locality_keys,
    normalize_text,
    point_in_bbox,
    resolve_jurisdiction,
)

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token types
# ---------------------------------------------------------------------------

class TokenType(str, Enum):
    REAL_STOP_LIKE = "real_stop_like"
    URBAN_ANCHOR = "urban_anchor"
    SECTOR = "sector"
    CORRIDOR = "corridor"
    ROAD_ONLY = "road_only"
    TERMINAL_ZONE = "terminal_zone"
    LOCALITY_STAGE = "locality_stage"


# Patterns that indicate corridor/road hints rather than places
_CORRIDOR_PATTERNS = {
    "acceso hacia", "corredor de", "corredor hacia", "via a",
    "carretera", "autopista", "av ", "av.", "avenida",
    "camino a", "camino hacia", "ruta hacia",
}

# Patterns that indicate terminal-like endpoints
_TERMINAL_PATTERNS = {
    "terminal", "parada", "redondel", "parque", "plaza",
    "estacion", "estación", "mercado", "centro",
}


@dataclass
class TokenClassification:
    """Classification result for a single route seed token."""
    token_text: str
    token_index: int
    token_type: TokenType
    is_endpoint: bool  # first or last token in sequence
    matched_place_key: Optional[str] = None
    place_bbox: Optional[Dict[str, float]] = None
    grounding_score: float = 0.0  # best composite score from Stage B
    grounding_source: str = ""  # "db_stop", "proxy_waypoint", etc.
    classification_reason: str = ""
    search_mode: str = "text_match"  # "text_match" or "territorial_search"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token_text": self.token_text,
            "token_index": self.token_index,
            "token_type": self.token_type.value,
            "is_endpoint": self.is_endpoint,
            "matched_place_key": self.matched_place_key,
            "place_bbox": self.place_bbox,
            "grounding_score": round(self.grounding_score, 4),
            "grounding_source": self.grounding_source,
            "classification_reason": self.classification_reason,
            "search_mode": self.search_mode,
        }


@dataclass
class TerritorialCandidate:
    """A stop candidate found via territorial (bbox) search."""
    stop_id: str
    stop_name: str
    lat: float
    lon: float
    locality: str = ""
    operator: str = ""
    place_id: Optional[str] = None
    ref: Optional[str] = None
    # Scoring components
    token_text: str = ""
    token_type: TokenType = TokenType.LOCALITY_STAGE
    token_match_score: float = 0.0
    geographic_fit_score: float = 0.0
    terminal_behavior_score: float = 0.0
    operator_match_score: float = 0.0
    node_score: float = 0.0
    source: str = "territorial_search"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stop_id": self.stop_id,
            "stop_name": self.stop_name,
            "lat": self.lat,
            "lon": self.lon,
            "locality": self.locality,
            "token_text": self.token_text,
            "token_type": self.token_type.value,
            "token_match_score": round(self.token_match_score, 4),
            "geographic_fit_score": round(self.geographic_fit_score, 4),
            "terminal_behavior_score": round(self.terminal_behavior_score, 4),
            "operator_match_score": round(self.operator_match_score, 4),
            "node_score": round(self.node_score, 4),
            "source": self.source,
        }


@dataclass
class ChainCandidate:
    """A complete candidate chain across all tokens."""
    stops: List[TerritorialCandidate]
    total_score: float = 0.0
    node_score_sum: float = 0.0
    transition_score_sum: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stops": [s.to_dict() for s in self.stops],
            "total_score": round(self.total_score, 4),
            "node_score_sum": round(self.node_score_sum, 4),
            "transition_score_sum": round(self.transition_score_sum, 4),
        }


@dataclass
class TerritorialResolutionResult:
    """Result of the territorial resolution stage (B2)."""
    token_classifications: List[TokenClassification] = field(default_factory=list)
    token_candidate_sets: Dict[int, List[TerritorialCandidate]] = field(default_factory=dict)
    best_chain: Optional[ChainCandidate] = None
    top_chains: List[ChainCandidate] = field(default_factory=list)
    enriched_waypoints: List[StopMatch] = field(default_factory=list)
    # Metrics
    total_tokens: int = 0
    territorial_tokens: int = 0
    resolved_territorial_tokens: int = 0
    avg_candidates_per_token: float = 0.0
    chain_confidence: float = 0.0
    unresolved_tokens: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token_classifications": [t.to_dict() for t in self.token_classifications],
            "token_candidate_sets": {
                str(k): [c.to_dict() for c in v]
                for k, v in self.token_candidate_sets.items()
            },
            "best_chain": self.best_chain.to_dict() if self.best_chain else None,
            "top_chains": [c.to_dict() for c in self.top_chains[:3]],
            "enriched_waypoints": [w.to_dict() for w in self.enriched_waypoints],
            "total_tokens": self.total_tokens,
            "territorial_tokens": self.territorial_tokens,
            "resolved_territorial_tokens": self.resolved_territorial_tokens,
            "avg_candidates_per_token": round(self.avg_candidates_per_token, 2),
            "chain_confidence": round(self.chain_confidence, 4),
            "unresolved_tokens": self.unresolved_tokens,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Haversine
# ---------------------------------------------------------------------------

def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2.0 * r * math.asin(math.sqrt(min(1.0, a)))


# ---------------------------------------------------------------------------
# A. Token classification
# ---------------------------------------------------------------------------

def _is_specific_stop_name(token_text: str) -> bool:
    """
    Determine if a token looks like a specific stop/landmark name rather
    than a broad territorial reference.

    Specific: "Hospital del INFA", "Santa Isabel", "Parque Turismo",
              "Administracion Zonal Los Chillos", "San Juan de Conocoto"
    Territorial: "Sangolquí", "Loreto", "Rumiloma", "Vallecito", "La Moca"
    """
    norm = normalize_text(token_text)
    tokens = norm.split()

    # Multi-word tokens with qualifying words are usually specific stops
    specific_qualifiers = {
        "hospital", "parque", "plaza", "mercado", "centro",
        "estacion", "terminal", "redondel", "administracion",
        "coliseo", "iglesia", "colegio", "escuela", "universidad",
        "estadio", "cementerio", "museo", "entrada", "parada",
        "playón", "playon", "viaducto",
    }
    if any(tok in specific_qualifiers for tok in tokens):
        return True

    # "San X de Y" patterns with 3+ words are usually specific places/stops
    # But bare "San Fernando", "San Vicente" are territorial
    if len(tokens) >= 3 and tokens[0] in ("san", "santa"):
        return True

    # Compound tokens with "/" are usually stop descriptions
    if "/" in norm:
        return True

    # Tokens with "de" connecting parts are usually specific
    if "de" in tokens and len(tokens) >= 3:
        return True

    return False


# Bare territorial place keys — these are the tokens that genuinely
# represent territories/sectors rather than specific stop names.
# Only these should get territorial search treatment.
_BARE_TERRITORIAL_KEYS = {
    "sangolqui", "conocoto", "pintag", "amaguana", "la merced",
    "loreto", "rumiloma", "rumipamba", "tanipamba", "vallecito",
    "la moca", "el carmen", "la libertad", "san fernando",
    "san vicente", "inchalillo", "curipungo", "los tubos",
    "san antonio", "la armenia", "guangopolo", "puengasi",
    "el triangulo", "san rafael",
}


def classify_token(
    token_text: str,
    token_index: int,
    total_tokens: int,
    *,
    grounding_candidates: List[StopMatch],
    expected_envelope: Optional[Dict[str, Any]] = None,
    province: Optional[str] = None,
) -> TokenClassification:
    """
    Classify a route seed token as one of the TokenType values.

    KEY PRINCIPLE: Only use territorial search for tokens that genuinely
    cannot be resolved as specific stops. Tokens like "La Marin",
    "Hospital del INFA", "Santa Isabel" are real stop names — they should
    use text matching, not territorial search.

    Territorial search is for broad place names like "Sangolquí",
    "Loreto", "Rumiloma" that name a zone, not a specific stop.
    """
    is_endpoint = (token_index == 0 or token_index == total_tokens - 1)
    norm = normalize_text(token_text)

    # Find best grounding match
    best_score = 0.0
    best_source = ""
    if grounding_candidates:
        best_score = grounding_candidates[0].composite_score
        best_source = grounding_candidates[0].match_source

    # Check if token matches a known place
    # Camino D PIEZA 7: province-aware locality/bbox lookup. None == sample_region.
    place_keys = infer_locality_keys([token_text], province=province)
    matched_key = place_keys[0] if place_keys else None
    place_bbox = None
    if province is None or str(province).strip().lower() in ("", "sample_region"):
        _prov_bboxes = PLACE_BBOXES
    else:
        from datamind_console.phases.phase3_routes.stop_grounding.place_geography_loader import (
            get_place_bboxes,
        )
        _prov_bboxes = get_place_bboxes(province)
    if matched_key and matched_key in _prov_bboxes:
        bbox = _prov_bboxes[matched_key]
        place_bbox = {
            "south": bbox[0], "west": bbox[1],
            "north": bbox[2], "east": bbox[3],
        }

    # Check for corridor/road patterns
    is_corridor_hint = any(pat in norm for pat in _CORRIDOR_PATTERNS)

    # Check if the token looks like a specific stop name
    is_specific = _is_specific_stop_name(token_text)

    # Check if the matched key is a bare territorial name
    is_bare_territorial = bool(
        matched_key
        and matched_key in _BARE_TERRITORIAL_KEYS
        and not is_specific
    )

    # ---- Classification logic ----

    # Rule 1: Any grounding with a real DB stop match → keep it
    if best_score >= 0.45 and best_source == "db_stop":
        token_type = TokenType.REAL_STOP_LIKE
        reason = f"DB stop match (score={best_score:.2f})"
        search_mode = "text_match"

    # Rule 2: Specific stop names always use text match, never territorial
    elif is_specific:
        token_type = TokenType.REAL_STOP_LIKE
        reason = f"specific stop/landmark name pattern"
        search_mode = "text_match"

    # Rule 3: Corridor hints ("acceso hacia X", "corredor de X")
    elif is_corridor_hint:
        if is_bare_territorial:
            token_type = TokenType.CORRIDOR
            reason = f"corridor hint toward territory '{matched_key}'"
            search_mode = "territorial_search"
        else:
            token_type = TokenType.CORRIDOR
            reason = "corridor pattern"
            search_mode = "text_match"

    # Rule 4: Bare territorial name with weak/no grounding → territorial search
    elif is_bare_territorial and best_score < 0.45:
        if is_endpoint:
            token_type = TokenType.TERMINAL_ZONE
            reason = f"endpoint bare territory '{matched_key}', weak grounding ({best_score:.2f})"
        else:
            token_type = TokenType.LOCALITY_STAGE
            reason = f"bare territory '{matched_key}', weak grounding ({best_score:.2f})"
        search_mode = "territorial_search"

    # Rule 5: Bare territorial name resolved only via proxy/OSM → territorial search
    elif is_bare_territorial and best_source in ("proxy_waypoint", "osm_fallback"):
        if is_endpoint:
            token_type = TokenType.TERMINAL_ZONE
        else:
            token_type = TokenType.LOCALITY_STAGE
        reason = f"bare territory '{matched_key}' with {best_source} fallback"
        search_mode = "territorial_search"

    # Rule 6: Everything else → real stop (text match)
    else:
        token_type = TokenType.REAL_STOP_LIKE
        reason = f"default real_stop_like (score={best_score:.2f}, src={best_source})"
        search_mode = "text_match"

    return TokenClassification(
        token_text=token_text,
        token_index=token_index,
        token_type=token_type,
        is_endpoint=is_endpoint,
        matched_place_key=matched_key,
        place_bbox=place_bbox,
        grounding_score=best_score,
        grounding_source=best_source,
        classification_reason=reason,
        search_mode=search_mode,
    )


# ---------------------------------------------------------------------------
# B. Territorial stop search (spatial, not text-based)
# ---------------------------------------------------------------------------

_TERRITORIAL_SEARCH_SQL = """
SELECT
    n.node_id::text    AS stop_id,
    COALESCE(
        NULLIF(BTRIM(p.canonical_name), ''),
        NULLIF(BTRIM(n.name), ''),
        NULLIF(BTRIM(n.ref), ''),
        'stop_' || LEFT(n.node_id::text, 8)
    ) AS stop_name,
    n.operator,
    p.region AS locality,
    p.place_id::text   AS place_id,
    n.ref,
    ST_Y(n.geom)       AS lat,
    ST_X(n.geom)       AS lon
FROM geo_prod.node_place_map m
JOIN node_prod.nodes n ON n.node_id = m.node_id
JOIN geo_prod.places p ON p.place_id = m.place_id
WHERE n.node_type = 'STOP'
  AND p.status = 'active'
  AND ST_Y(n.geom) BETWEEN %(bbox_south)s AND %(bbox_north)s
  AND ST_X(n.geom) BETWEEN %(bbox_west)s AND %(bbox_east)s
ORDER BY ST_Y(n.geom), ST_X(n.geom)
LIMIT %(max_results)s
"""


def _score_territorial_candidate(
    row: Dict[str, Any],
    *,
    token_text: str,
    token_type: TokenType,
    place_key: Optional[str],
    place_bbox: Optional[Dict[str, float]],
    operator_name: Optional[str],
    cooperative_name: Optional[str],
    is_endpoint: bool,
) -> TerritorialCandidate:
    """Score a DB stop found via territorial search."""
    lat = float(row.get("lat") or 0)
    lon = float(row.get("lon") or 0)
    stop_name = str(row.get("stop_name") or "")
    locality = str(row.get("locality") or "")
    operator = str(row.get("operator") or "")
    norm_name = normalize_text(stop_name)
    norm_locality = normalize_text(locality)
    norm_operator = normalize_text(operator)

    # Geographic fit: distance to territory center
    geographic_fit = 0.5  # default
    if place_bbox:
        center_lon = (place_bbox["west"] + place_bbox["east"]) / 2.0
        center_lat = (place_bbox["south"] + place_bbox["north"]) / 2.0
        dist_to_center_m = _haversine_m(lon, lat, center_lon, center_lat)
        # Closer to center = higher fit (for terminal zones)
        # But not too close (center might be residential, not transit)
        if token_type == TokenType.TERMINAL_ZONE:
            # For terminal zones, prefer stops near center of territory
            geographic_fit = max(0.0, 1.0 - (dist_to_center_m / 2500.0))
        else:
            # For stages, anywhere in the bbox is fine
            geographic_fit = max(0.0, 1.0 - (dist_to_center_m / 5000.0))

    # Token match: does the stop name contain the token text or vice versa?
    norm_token = normalize_text(token_text)
    token_tokens = set(norm_token.split())
    name_tokens = set(norm_name.split()) | set(norm_locality.split())
    if token_tokens and name_tokens:
        overlap = len(token_tokens & name_tokens)
        token_match = overlap / max(len(token_tokens), 1)
    else:
        token_match = 0.0

    # Also check place alias match
    if place_key:
        aliases = PLACE_ALIASES.get(place_key, [place_key])
        for alias in aliases:
            norm_alias = normalize_text(alias)
            if norm_alias in norm_name or norm_alias in norm_locality:
                token_match = max(token_match, 0.5)
                break

    # Terminal behavior: does the stop look like a terminal/turnaround?
    terminal_score = 0.0
    if is_endpoint:
        terminal_keywords = {
            "terminal", "parada", "redondel", "parque", "plaza",
            "estacion", "mercado", "centro", "base", "inicio",
            "final", "retorno",
        }
        for kw in terminal_keywords:
            if kw in norm_name:
                terminal_score = 0.7
                break
        # Stops with references (numbered stops) are less terminal-like
        if row.get("ref") and not terminal_score:
            terminal_score = 0.1

    # Operator/cooperative match
    op_score = 0.0
    if operator_name:
        norm_op_hint = normalize_text(operator_name)
        if norm_op_hint and norm_operator and (
            norm_op_hint in norm_operator or norm_operator in norm_op_hint
        ):
            op_score = 1.0
    if cooperative_name and not op_score:
        norm_coop = normalize_text(cooperative_name)
        if norm_coop and norm_operator and (
            norm_coop in norm_operator or norm_operator in norm_coop
        ):
            op_score = 0.7

    # Locality consistency: does the stop's locality contain the place key?
    locality_bonus = 0.0
    if place_key:
        norm_place = normalize_text(place_key)
        if norm_place in norm_locality or norm_place in norm_name:
            locality_bonus = 0.3

    # Combined node score
    if is_endpoint:
        node_score = (
            0.25 * geographic_fit
            + 0.15 * token_match
            + 0.15 * locality_bonus
            + 0.25 * terminal_score
            + 0.20 * op_score
        )
    else:
        node_score = (
            0.30 * geographic_fit
            + 0.20 * token_match
            + 0.20 * locality_bonus
            + 0.10 * terminal_score
            + 0.20 * op_score
        )

    # Minimum score for stops that are at least in the right territory
    if geographic_fit > 0.3 and node_score < 0.10:
        node_score = 0.10

    return TerritorialCandidate(
        stop_id=str(row["stop_id"]),
        stop_name=stop_name,
        lat=lat,
        lon=lon,
        locality=locality,
        operator=operator,
        place_id=row.get("place_id"),
        ref=row.get("ref"),
        token_text=token_text,
        token_type=token_type,
        token_match_score=token_match,
        geographic_fit_score=geographic_fit,
        terminal_behavior_score=terminal_score,
        operator_match_score=op_score,
        node_score=node_score,
        source="territorial_search",
    )


def territorial_stop_search(
    token_text: str,
    *,
    token_type: TokenType,
    place_key: Optional[str],
    place_bbox: Optional[Dict[str, float]],
    operator_name: Optional[str] = None,
    cooperative_name: Optional[str] = None,
    is_endpoint: bool = False,
    max_results: int = 25,
    top_k: int = 8,
    conn=None,
) -> List[TerritorialCandidate]:
    """
    Search for stop candidates within a territorial bbox.

    Unlike text-based grounding, this finds ALL active stops inside the
    place bbox and scores them by geographic fit, terminal behavior,
    and operator match. No text similarity threshold is applied.
    """
    if not place_bbox:
        _LOG.debug("No bbox for token '%s', skipping territorial search", token_text)
        return []

    # Expand bbox slightly for edge cases
    lat_pad = (place_bbox["north"] - place_bbox["south"]) * 0.08
    lon_pad = (place_bbox["east"] - place_bbox["west"]) * 0.08
    search_bbox = {
        "south": place_bbox["south"] - lat_pad,
        "west": place_bbox["west"] - lon_pad,
        "north": place_bbox["north"] + lat_pad,
        "east": place_bbox["east"] + lon_pad,
    }

    def _do_query(connection) -> List[TerritorialCandidate]:
        try:
            rows = fetch_all(connection, _TERRITORIAL_SEARCH_SQL, {
                "bbox_south": search_bbox["south"],
                "bbox_west": search_bbox["west"],
                "bbox_north": search_bbox["north"],
                "bbox_east": search_bbox["east"],
                "max_results": max_results,
            })
        except Exception as exc:
            _LOG.warning("Territorial search failed for '%s': %s", token_text, exc)
            return []

        candidates = []
        for row in rows:
            cand = _score_territorial_candidate(
                row,
                token_text=token_text,
                token_type=token_type,
                place_key=place_key,
                place_bbox=place_bbox,
                operator_name=operator_name,
                cooperative_name=cooperative_name,
                is_endpoint=is_endpoint,
            )
            candidates.append(cand)

        # Sort by node_score descending and take top_k
        candidates.sort(key=lambda c: c.node_score, reverse=True)
        return candidates[:top_k]

    if conn is not None:
        return _do_query(conn)

    with db_conn(readonly=True) as connection:
        return _do_query(connection)


# ---------------------------------------------------------------------------
# C. Convert existing grounding matches to TerritorialCandidates
# ---------------------------------------------------------------------------

def _grounding_to_territorial(
    matches: List[StopMatch],
    token_text: str,
    token_type: TokenType,
    is_endpoint: bool,
) -> List[TerritorialCandidate]:
    """Convert StopMatch results from Stage B into TerritorialCandidates."""
    candidates = []
    for m in matches:
        # Use composite_score as the node_score basis
        terminal_score = 0.0
        if is_endpoint:
            norm_name = normalize_text(m.stop_name)
            for kw in ("terminal", "parada", "redondel", "parque", "plaza",
                        "estacion", "mercado", "centro"):
                if kw in norm_name:
                    terminal_score = 0.5
                    break

        # Grounding matches get a boost since they were text-matched
        node_score = min(1.0, m.composite_score * 1.1)

        candidates.append(TerritorialCandidate(
            stop_id=m.stop_id,
            stop_name=m.stop_name,
            lat=m.lat,
            lon=m.lon,
            locality=m.locality,
            operator="",
            place_id=m.place_id,
            token_text=token_text,
            token_type=token_type,
            token_match_score=m.text_alignment_score,
            geographic_fit_score=m.geography_score,
            terminal_behavior_score=terminal_score,
            operator_match_score=1.0 if m.operator_match else 0.0,
            node_score=node_score,
            source=m.match_source,
        ))
    return candidates


# ---------------------------------------------------------------------------
# D. Chain optimization (beam search)
# ---------------------------------------------------------------------------

def _transition_score(
    prev: TerritorialCandidate,
    curr: TerritorialCandidate,
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
) -> float:
    """
    Score the transition between two consecutive candidates in the chain.

    Considers:
    - Geographic distance plausibility (500m-8km between waypoints is ideal)
    - Direction consistency (stops should be in different locations)
    - Envelope compliance (both should be within expected geography)
    """
    dist_m = _haversine_m(prev.lon, prev.lat, curr.lon, curr.lat)

    # Distance plausibility: ideal range is 500m to 8km
    if dist_m < 100:
        # Too close — probably same location
        dist_score = 0.05
    elif dist_m < 500:
        # Close but acceptable
        dist_score = 0.3 + 0.4 * (dist_m / 500.0)
    elif dist_m <= 8000:
        # Ideal range
        dist_score = 0.7 + 0.3 * (1.0 - (dist_m - 500) / 7500.0)
    elif dist_m <= 15000:
        # Getting far but possible for Valle routes
        dist_score = max(0.1, 0.7 * (1.0 - (dist_m - 8000) / 7000.0))
    else:
        # Very far — unlikely for same route
        dist_score = 0.05

    # Check that both are in expected geography
    geo_score = 1.0
    if expected_envelope:
        bbox = expected_envelope.get("bbox")
        if bbox:
            prev_in = point_in_bbox(prev.lon, prev.lat, bbox)
            curr_in = point_in_bbox(curr.lon, curr.lat, bbox)
            if not prev_in or not curr_in:
                geo_score = 0.3

    # Same stop dedup
    if prev.stop_id == curr.stop_id:
        return 0.0

    return dist_score * 0.75 + geo_score * 0.25


def optimize_chain(
    token_candidates: List[List[TerritorialCandidate]],
    *,
    beam_width: int = 6,
    expected_envelope: Optional[Dict[str, Any]] = None,
) -> List[ChainCandidate]:
    """
    Beam search over token candidate sets to find the best stop chain.

    For N tokens, each with K candidates, finds the chain that maximizes:
        total_score = Σ node_score(i) + Σ transition_score(i, i+1)

    Returns top beam_width chains sorted by total_score descending.
    """
    if not token_candidates:
        return []

    # Filter out empty token sets
    non_empty = [(i, cands) for i, cands in enumerate(token_candidates) if cands]
    if not non_empty:
        return []

    # Initialize beams with first non-empty token
    first_idx, first_cands = non_empty[0]
    beams: List[Tuple[List[TerritorialCandidate], float, float, float]] = []
    for cand in first_cands[:beam_width * 2]:
        beams.append(([cand], cand.node_score, cand.node_score, 0.0))

    # Extend through remaining tokens
    for token_idx, candidates in non_empty[1:]:
        new_beams: List[Tuple[List[TerritorialCandidate], float, float, float]] = []
        for chain, total, node_sum, trans_sum in beams:
            for cand in candidates[:beam_width * 2]:
                trans = _transition_score(
                    chain[-1], cand,
                    expected_envelope=expected_envelope,
                )
                new_total = total + cand.node_score + trans
                new_beams.append((
                    chain + [cand],
                    new_total,
                    node_sum + cand.node_score,
                    trans_sum + trans,
                ))

        # Keep top beam_width chains
        new_beams.sort(key=lambda x: x[1], reverse=True)
        beams = new_beams[:beam_width]

    # Convert to ChainCandidate objects
    results = []
    for chain, total, node_sum, trans_sum in beams:
        results.append(ChainCandidate(
            stops=chain,
            total_score=total,
            node_score_sum=node_sum,
            transition_score_sum=trans_sum,
        ))

    return results


# ---------------------------------------------------------------------------
# E. Convert chain to enriched waypoints (StopMatch objects)
# ---------------------------------------------------------------------------

def _chain_to_waypoints(chain: ChainCandidate) -> List[StopMatch]:
    """Convert a ChainCandidate to StopMatch objects for corridor building."""
    waypoints = []
    for cand in chain.stops:
        # Skip proxy/synthetic IDs
        if cand.stop_id.startswith("proxy:"):
            continue

        waypoints.append(StopMatch(
            stop_id=cand.stop_id,
            stop_name=cand.stop_name,
            lat=cand.lat,
            lon=cand.lon,
            locality=cand.locality,
            composite_score=cand.node_score,
            name_similarity=cand.token_match_score,
            geography_score=cand.geographic_fit_score,
            in_expected_geography=True,
            match_source=f"territorial:{cand.token_type.value}",
            text_alignment_score=cand.token_match_score,
            locality_match=bool(cand.locality),
            operator_match=cand.operator_match_score > 0.5,
        ))

    return waypoints


# ---------------------------------------------------------------------------
# F. Main entry point
# ---------------------------------------------------------------------------

def resolve_territorial_tokens(
    seed: RouteSeed,
    grounding: StopGroundingResult,
    *,
    conn=None,
) -> TerritorialResolutionResult:
    """
    Main entry point for territorial token resolution.

    1. Classifies all tokens in the route seed
    2. For territorial tokens, runs spatial search to find candidates
    3. Merges territorial candidates with grounding results
    4. Runs beam search to find optimal waypoint chain
    5. Returns enriched waypoints for corridor building

    This should be called between Stage B and Stage C.
    """
    result = TerritorialResolutionResult()
    expected_envelope = seed.expected_geographic_envelope

    # Collect all tokens from seed
    tokens: List[str] = []
    if seed.sequence_seed_fragments:
        tokens = list(seed.sequence_seed_fragments)
    else:
        if seed.anchor_a_hint:
            tokens.append(seed.anchor_a_hint)
        for hint in seed.intermediate_hints:
            tokens.append(hint)
        if seed.anchor_b_hint:
            tokens.append(seed.anchor_b_hint)

    if not tokens:
        result.notes = "No tokens to resolve"
        return result

    result.total_tokens = len(tokens)

    # Build mapping from token text to grounding candidates
    grounding_map: Dict[str, List[StopMatch]] = {}
    if grounding.matched_anchor_a_candidates:
        grounding_map[seed.anchor_a_hint] = grounding.matched_anchor_a_candidates
    if grounding.matched_anchor_b_candidates:
        grounding_map[seed.anchor_b_hint] = grounding.matched_anchor_b_candidates
    for hint, matches in grounding.matched_intermediate_candidates.items():
        grounding_map[hint] = matches

    # Step 1: Classify all tokens
    # Camino D PIEZA 7: propagate province from the seed.
    _province = getattr(seed, "province", None)
    classifications: List[TokenClassification] = []
    for i, token in enumerate(tokens):
        grnd_matches = grounding_map.get(token, [])
        cls = classify_token(
            token, i, len(tokens),
            grounding_candidates=grnd_matches,
            expected_envelope=expected_envelope,
            province=_province,
        )
        classifications.append(cls)

    result.token_classifications = classifications
    territorial_count = sum(
        1 for c in classifications if c.search_mode == "territorial_search"
    )
    result.territorial_tokens = territorial_count

    if territorial_count == 0:
        result.notes = "All tokens are real_stop_like — no territorial resolution needed"
        # Still populate enriched_waypoints from grounding
        _populate_from_grounding(result, seed, grounding, tokens, classifications)
        return result

    _LOG.info(
        "Token classification: %d total, %d territorial, types=%s",
        len(tokens),
        territorial_count,
        [c.token_type.value for c in classifications],
    )

    # Step 2: Generate candidates per token
    all_token_candidates: List[List[TerritorialCandidate]] = []
    resolved_count = 0

    for cls in classifications:
        token = cls.token_text
        grnd_matches = grounding_map.get(token, [])

        # Start with grounding candidates (converted to TerritorialCandidate)
        grnd_cands = _grounding_to_territorial(
            grnd_matches, token, cls.token_type, cls.is_endpoint,
        )

        if cls.search_mode == "territorial_search" and cls.place_bbox:
            # Run territorial spatial search
            terr_cands = territorial_stop_search(
                token,
                token_type=cls.token_type,
                place_key=cls.matched_place_key,
                place_bbox=cls.place_bbox,
                operator_name=seed.operator_name,
                cooperative_name=seed.cooperative_name,
                is_endpoint=cls.is_endpoint,
                top_k=8,
                conn=conn,
            )

            # Merge: deduplicate by stop_id, keep highest score
            seen_ids: Set[str] = set()
            merged = []
            for c in sorted(grnd_cands + terr_cands,
                           key=lambda x: x.node_score, reverse=True):
                if c.stop_id not in seen_ids:
                    seen_ids.add(c.stop_id)
                    merged.append(c)

            if merged:
                resolved_count += 1

            all_token_candidates.append(merged[:10])
            result.token_candidate_sets[cls.token_index] = merged[:10]

            _LOG.info(
                "  Token %d '%s' (%s): %d grounding + %d territorial = %d merged",
                cls.token_index, token, cls.token_type.value,
                len(grnd_cands), len(terr_cands), len(merged),
            )
        else:
            # Use grounding candidates only
            all_token_candidates.append(grnd_cands[:10])
            result.token_candidate_sets[cls.token_index] = grnd_cands[:10]
            if grnd_cands:
                resolved_count += 1

    result.resolved_territorial_tokens = resolved_count

    # Compute avg candidates per token
    candidate_counts = [len(cands) for cands in all_token_candidates]
    result.avg_candidates_per_token = (
        sum(candidate_counts) / len(candidate_counts)
        if candidate_counts else 0.0
    )

    # Track unresolved tokens
    result.unresolved_tokens = [
        cls.token_text
        for cls, cands in zip(classifications, all_token_candidates)
        if not cands
    ]

    # Step 3: Chain optimization (beam search)
    chains = optimize_chain(
        all_token_candidates,
        beam_width=6,
        expected_envelope=expected_envelope,
    )

    if chains:
        result.best_chain = chains[0]
        result.top_chains = chains[:3]
        result.chain_confidence = chains[0].total_score / max(len(tokens), 1)

        # Convert best chain to waypoints
        result.enriched_waypoints = _chain_to_waypoints(chains[0])
    else:
        result.chain_confidence = 0.0

    # Build notes
    notes_parts = [f"{len(tokens)} tokens"]
    notes_parts.append(f"{territorial_count} territorial")
    notes_parts.append(f"{resolved_count} resolved")
    if result.unresolved_tokens:
        notes_parts.append(f"{len(result.unresolved_tokens)} unresolved")
    if result.best_chain:
        notes_parts.append(f"chain_score={result.best_chain.total_score:.2f}")
    result.notes = ", ".join(notes_parts)

    _LOG.info("Territorial resolution: %s", result.notes)

    return result


def _populate_from_grounding(
    result: TerritorialResolutionResult,
    seed: RouteSeed,
    grounding: StopGroundingResult,
    tokens: List[str],
    classifications: List[TokenClassification],
) -> None:
    """Populate enriched waypoints from grounding when no territorial resolution needed."""
    waypoints = []
    if grounding.best_anchor_a():
        waypoints.append(grounding.best_anchor_a())
    for hint in seed.intermediate_hints:
        matches = grounding.matched_intermediate_candidates.get(hint, [])
        if matches:
            waypoints.append(matches[0])
    if grounding.best_anchor_b():
        waypoints.append(grounding.best_anchor_b())
    result.enriched_waypoints = waypoints


# ---------------------------------------------------------------------------
# G. Enrich grounding with territorial resolution
# ---------------------------------------------------------------------------

def enrich_grounding_with_territorial(
    seed: RouteSeed,
    grounding: StopGroundingResult,
    resolution: TerritorialResolutionResult,
) -> StopGroundingResult:
    """
    Merge territorial resolution results back into the grounding result.

    For tokens that were resolved territorially, replace or supplement
    the grounding candidates with the chain-selected candidates.
    """
    if not resolution.best_chain or not resolution.enriched_waypoints:
        return grounding

    # Build a mapping from token text to chain-selected candidate
    chain_map: Dict[str, TerritorialCandidate] = {}
    for cls, cand in zip(resolution.token_classifications, resolution.best_chain.stops):
        chain_map[cls.token_text] = cand

    # Helper to create a StopMatch from a TerritorialCandidate
    def _to_stop_match(cand: TerritorialCandidate) -> Optional[StopMatch]:
        if cand.stop_id.startswith("proxy:"):
            return None
        return StopMatch(
            stop_id=cand.stop_id,
            stop_name=cand.stop_name,
            lat=cand.lat,
            lon=cand.lon,
            locality=cand.locality,
            composite_score=max(0.55, cand.node_score),
            name_similarity=cand.token_match_score,
            geography_score=cand.geographic_fit_score,
            in_expected_geography=True,
            match_source=f"territorial:{cand.token_type.value}",
            text_alignment_score=cand.token_match_score,
        )

    # Safety: only enrich anchors if the existing grounding is weak.
    # Strong existing grounding should not be replaced — the territorial
    # candidate might route to a different location and cause corridor inflation.
    anchor_a_weak = (
        not grounding.matched_anchor_a_candidates
        or grounding.matched_anchor_a_candidates[0].composite_score < 0.55
        or grounding.matched_anchor_a_candidates[0].match_source in ("proxy_waypoint", "osm_fallback")
    )
    anchor_b_weak = (
        not grounding.matched_anchor_b_candidates
        or grounding.matched_anchor_b_candidates[0].composite_score < 0.55
        or grounding.matched_anchor_b_candidates[0].match_source in ("proxy_waypoint", "osm_fallback")
    )

    # Update anchor A if grounding is weak and chain resolved it
    if anchor_a_weak and seed.anchor_a_hint in chain_map:
        new_match = _to_stop_match(chain_map[seed.anchor_a_hint])
        if new_match:
            existing = grounding.matched_anchor_a_candidates
            if not any(m.stop_id == new_match.stop_id for m in existing):
                grounding.matched_anchor_a_candidates = [new_match] + existing
                grounding.matched_anchor_a_candidates.sort(
                    key=lambda m: m.composite_score, reverse=True
                )

    # Update anchor B if grounding is weak
    if anchor_b_weak and seed.anchor_b_hint in chain_map:
        new_match = _to_stop_match(chain_map[seed.anchor_b_hint])
        if new_match:
            existing = grounding.matched_anchor_b_candidates
            if not any(m.stop_id == new_match.stop_id for m in existing):
                grounding.matched_anchor_b_candidates = [new_match] + existing
                grounding.matched_anchor_b_candidates.sort(
                    key=lambda m: m.composite_score, reverse=True
                )

    # Update intermediates — always enrich (intermediates don't cause corridor inflation)
    for hint in seed.intermediate_hints:
        if hint in chain_map:
            new_match = _to_stop_match(chain_map[hint])
            if new_match:
                existing = grounding.matched_intermediate_candidates.get(hint, [])
                if not any(m.stop_id == new_match.stop_id for m in existing):
                    grounding.matched_intermediate_candidates[hint] = [new_match] + existing
                    grounding.matched_intermediate_candidates[hint].sort(
                        key=lambda m: m.composite_score, reverse=True
                    )
                # Remove from unmatched if it was there
                if hint in grounding.unmatched_hints:
                    grounding.unmatched_hints.remove(hint)

    # Recalculate overall confidence
    total_hints = 2 + len(seed.intermediate_hints)
    matched = (
        (1 if grounding.matched_anchor_a_candidates else 0)
        + (1 if grounding.matched_anchor_b_candidates else 0)
        + len(grounding.matched_intermediate_candidates)
    )
    match_rate = matched / max(total_hints, 1)
    best_scores = []
    if grounding.matched_anchor_a_candidates:
        best_scores.append(grounding.matched_anchor_a_candidates[0].composite_score)
    if grounding.matched_anchor_b_candidates:
        best_scores.append(grounding.matched_anchor_b_candidates[0].composite_score)
    for matches in grounding.matched_intermediate_candidates.values():
        if matches:
            best_scores.append(matches[0].composite_score)
    avg_best = sum(best_scores) / len(best_scores) if best_scores else 0.0
    grounding.overall_grounding_confidence = 0.5 * match_rate + 0.5 * avg_best

    if grounding.grounding_notes:
        grounding.grounding_notes += "; "
    grounding.grounding_notes += (
        f"territorial_enrichment: {resolution.territorial_tokens} tokens resolved, "
        f"chain_conf={resolution.chain_confidence:.2f}"
    )

    return grounding
