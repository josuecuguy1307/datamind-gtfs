"""
Route deduplication: compare Deep Research routes against existing route_prod entries.

Prevents the same route being built twice (once from OSM extraction, once from
Deep Research seed catalogs).  Each research route is scored against every
existing route in the same canton bbox and triaged into MATCHED / AMBIGUOUS / NEW.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from datamind_console.persistence import patch_route_prod_fields
from phase3_routes.services.route_constructor.src.db.conn import db_cursor

log = logging.getLogger(__name__)

# ── thresholds ──────────────────────────────────────────────────────
MATCH_THRESHOLD = 0.70
AMBIGUOUS_THRESHOLD = 0.40

WEIGHTS = {
    "ref": 0.35,
    "operator": 0.15,
    "terminus": 0.25,
    "geometry": 0.15,
    "stops": 0.10,
}

# tokens stripped when normalising operator names
_OPERATOR_NOISE = {
    "cooperativa", "de", "transporte", "transportes", "cia", "sa",
    "ltda", "s.a.", "c.a.", "compania", "compañia", "empresa",
    "publica", "ep", "urbano", "interprovincial", "intracantonal",
}

# ── text helpers ────────────────────────────────────────────────────

def _strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _normalise_ref(ref: str | None) -> str | None:
    if not ref:
        return None
    return re.sub(r"[\s\-_]+", "", ref).upper().strip()


def _tokenise_operator(name: str | None) -> set[str]:
    if not name:
        return set()
    name = _strip_accents(name.lower())
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    return {t for t in name.split() if t and t not in _OPERATOR_NOISE}


def _token_overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    shared = a & b
    return len(shared) / max(len(a), len(b))


def _normalise_terminus(name: str | None) -> str:
    if not name:
        return ""
    name = _strip_accents(name.lower().strip())
    # strip common prefixes
    for prefix in ("terminal ", "parada ", "estacion ", "p. "):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return re.sub(r"\s+", " ", name).strip()


def _terminus_score(a: str, b: str) -> float:
    """Score two terminus names, accounting for containment and fuzzy ratio."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # containment
    if a in b or b in a:
        return 0.85
    # token overlap
    ta, tb = set(a.split()), set(b.split())
    tok = _token_overlap(ta, tb)
    # Levenshtein-ish via SequenceMatcher
    seq = SequenceMatcher(None, a, b).ratio()
    return max(tok, seq)


# ── individual matchers ─────────────────────────────────────────────

def _match_ref(research_ref: str | None, existing_ref: str | None) -> float:
    """Exact or normalised match on route_ref / route_code."""
    nr = _normalise_ref(research_ref)
    ne = _normalise_ref(existing_ref)
    if nr is None or ne is None:
        return 0.0
    if nr == ne:
        return 1.0
    # partial: same prefix family (e.g. CAY01 vs CAY1)
    if nr.rstrip("0") == ne.rstrip("0"):
        return 0.9
    return 0.0


def _match_operator(research_op: str | None, existing_op: str | None) -> float:
    """Fuzzy match on operator / cooperative name."""
    tr = _tokenise_operator(research_op)
    te = _tokenise_operator(existing_op)
    if not tr or not te:
        return 0.0
    overlap = _token_overlap(tr, te)
    if overlap >= 0.8:
        return 1.0
    if overlap >= 0.5:
        return 0.7
    # fallback: containment on raw lowered strings
    lr = _strip_accents((research_op or "").lower())
    le = _strip_accents((existing_op or "").lower())
    if lr and le and (lr in le or le in lr):
        return 0.9
    return overlap


def _match_terminus_names(
    research_origin: str | None,
    research_dest: str | None,
    existing_origin: str | None,
    existing_dest: str | None,
) -> float:
    """Fuzzy match on terminus names, handling direction reversal."""
    ro = _normalise_terminus(research_origin)
    rd = _normalise_terminus(research_dest)
    eo = _normalise_terminus(existing_origin)
    ed = _normalise_terminus(existing_dest)

    if not (ro or rd) or not (eo or ed):
        return 0.0

    # collect all pairwise scores, only counting non-empty pairs
    def _avg_nonzero_pairs(pairs: list[tuple[str, str]]) -> float:
        valid = [(a, b) for a, b in pairs if a and b]
        if not valid:
            return 0.0
        return sum(_terminus_score(a, b) for a, b in valid) / len(valid)

    # forward: research_origin↔existing_origin, research_dest↔existing_dest
    fwd = _avg_nonzero_pairs([(ro, eo), (rd, ed)])
    # reverse: research_origin↔existing_dest, research_dest↔existing_origin
    rev = _avg_nonzero_pairs([(ro, ed), (rd, eo)])

    return max(fwd, rev)


def _match_geometry_overlap(
    research_bbox: dict | list | None,
    existing_route_id: str,
    conn,
) -> float:
    """Check envelope overlap between research bbox and existing route geometry."""
    if not research_bbox:
        return 0.0

    # accept both dict {south,west,north,east} and list [west,south,east,north]
    if isinstance(research_bbox, dict):
        west = research_bbox.get("west", research_bbox.get("min_lon"))
        south = research_bbox.get("south", research_bbox.get("min_lat"))
        east = research_bbox.get("east", research_bbox.get("max_lon"))
        north = research_bbox.get("north", research_bbox.get("max_lat"))
    elif isinstance(research_bbox, (list, tuple)) and len(research_bbox) == 4:
        west, south, east, north = research_bbox
    else:
        return 0.0

    if None in (west, south, east, north):
        return 0.0

    sql = """
    WITH research AS (
        SELECT ST_MakeEnvelope(%s, %s, %s, %s, 4326) AS env
    ),
    existing AS (
        SELECT ST_Envelope(geom) AS env
        FROM route_prod.routes
        WHERE route_id = %s::uuid AND geom IS NOT NULL
    )
    SELECT
        CASE
            WHEN ST_Area(e.env) = 0 OR ST_Area(r.env) = 0 THEN 0
            ELSE ST_Area(ST_Intersection(r.env, e.env))
                 / GREATEST(ST_Area(r.env), ST_Area(e.env))
        END AS overlap_ratio
    FROM research r, existing e
    WHERE ST_Intersects(r.env, e.env)
    """
    # Savepoint isolates this query so a failure (bad geom, bad uuid, etc.)
    # does not poison the outer transaction for subsequent dedup queries.
    try:
        with db_cursor(conn) as cur:
            cur.execute("SAVEPOINT sp_geom_overlap")
            try:
                cur.execute(sql, (west, south, east, north, str(existing_route_id)))
                row = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT sp_geom_overlap")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_geom_overlap")
                log.warning(
                    "geometry overlap check failed for %s",
                    existing_route_id, exc_info=True,
                )
                return 0.0
            if not row:
                return 0.0
            ratio = float(row["overlap_ratio"])
            if ratio >= 0.7:
                return 1.0
            if ratio >= 0.3:
                return 0.6
            return ratio
    except Exception:
        log.warning("geometry overlap check failed for %s", existing_route_id, exc_info=True)
        return 0.0


def _match_stop_overlap(
    research_stops: list[dict] | None,
    existing_route_id: str,
    conn,
) -> float:
    """Check how many research stops are within 200 m of existing route stops."""
    if not research_stops:
        return 0.0

    # build list of (lat, lon) from research stops
    coords = []
    for s in research_stops:
        lat = s.get("lat") or s.get("latitude")
        lon = s.get("lon") or s.get("lng") or s.get("longitude")
        if lat is not None and lon is not None:
            coords.append((float(lat), float(lon)))
    if not coords:
        return 0.0

    # For each research stop, check if any stop in the existing route is within 200 m
    sql = """
    WITH existing_stops AS (
        SELECT unnest(stop_node_ids) AS node_id
        FROM route_prod.routes
        WHERE route_id = %s::uuid
    ),
    stop_geoms AS (
        SELECT n.geom
        FROM existing_stops es
        JOIN node_prod.stops n ON n.stop_node_id = es.node_id
        WHERE n.geom IS NOT NULL
    )
    SELECT EXISTS (
        SELECT 1 FROM stop_geoms sg
        WHERE ST_DWithin(
            sg.geom::geography,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
            200
        )
    ) AS matched
    """
    matched = 0
    # Per-stop savepoint: one bad point (e.g. missing node_prod.stops row,
    # bad uuid cast) must not abort the outer transaction and leave every
    # subsequent dedup query raising InFailedSqlTransaction.
    try:
        with db_cursor(conn) as cur:
            for lat, lon in coords:
                cur.execute("SAVEPOINT sp_stop_overlap")
                try:
                    cur.execute(sql, (str(existing_route_id), lon, lat))
                    row = cur.fetchone()
                    cur.execute("RELEASE SAVEPOINT sp_stop_overlap")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_stop_overlap")
                    log.warning(
                        "stop overlap check failed for %s at (%s,%s)",
                        existing_route_id, lat, lon, exc_info=True,
                    )
                    continue
                if row and row["matched"]:
                    matched += 1
    except Exception:
        log.warning("stop overlap check failed for %s", existing_route_id, exc_info=True)
        return 0.0

    return matched / len(coords)


# ── route name → terminus splitting ─────────────────────────────────

_TERMINUS_SEP = re.compile(r"\s*[–—\-/]\s*")


def _split_route_name_termini(name: str | None) -> tuple[str, str]:
    """
    Split a route name like "Sangolqui - ESPE - Quito" into (first, last) termini.
    Returns ("", "") if not splittable.
    """
    if not name:
        return ("", "")
    parts = [p.strip() for p in _TERMINUS_SEP.split(name) if p.strip()]
    if len(parts) >= 2:
        return (parts[0], parts[-1])
    return (name.strip(), "")


# ── composite score ─────────────────────────────────────────────────

def compute_match_score(
    research_route: dict,
    existing_route: dict,
    conn,
) -> dict:
    """Weighted composite of all matchers."""

    # extract research fields
    seed = research_route.get("seed_catalog") or {}
    geo = research_route.get("geography_catalog") or research_route.get("expected_geographic_envelope") or {}

    r_origin = (seed.get("terminus_origin") or {}).get("name", "") or research_route.get("anchor_a_hint", "")
    r_dest = (seed.get("terminus_destination") or {}).get("name", "") or research_route.get("anchor_b_hint", "")
    # fallback: derive from route_name
    if not r_origin and not r_dest:
        r_origin, r_dest = _split_route_name_termini(
            research_route.get("route_name") or research_route.get("display_name")
        )

    r_stops = seed.get("intermediate_stops") or seed.get("sequence_tokens") or []
    r_bbox = geo.get("envelope_bbox") or geo.get("bbox")

    # existing route terminus: prefer osm_from/to, fall back to route_name split
    e_origin = existing_route.get("public_origin") or existing_route.get("osm_from") or ""
    e_dest = existing_route.get("public_destination") or existing_route.get("osm_to") or ""
    if not e_origin and not e_dest:
        e_origin, e_dest = _split_route_name_termini(
            existing_route.get("route_name") or existing_route.get("service_route_name")
        )

    scores = {
        "ref": _match_ref(
            research_route.get("route_code") or research_route.get("route_ref"),
            existing_route.get("route_ref") or existing_route.get("service_route_ref"),
        ),
        "operator": _match_operator(
            research_route.get("cooperative") or research_route.get("cooperative_name"),
            existing_route.get("operator") or existing_route.get("operator_hint"),
        ),
        "terminus": _match_terminus_names(
            r_origin, r_dest,
            e_origin, e_dest,
        ),
        "geometry": _match_geometry_overlap(
            r_bbox,
            existing_route.get("route_id"),
            conn,
        ),
        "stops": _match_stop_overlap(
            r_stops,
            existing_route.get("route_id"),
            conn,
        ),
    }

    # Determine which signals are "applicable" (both sides have data).
    # A matcher returns exactly 0.0 when either side lacks data — but also
    # when both sides have data that simply doesn't match.  We mark a signal
    # as inapplicable only when at least one side is absent.
    r_ref = research_route.get("route_code") or research_route.get("route_ref")
    e_ref = existing_route.get("route_ref") or existing_route.get("service_route_ref")
    r_op = research_route.get("cooperative") or research_route.get("cooperative_name")
    e_op = existing_route.get("operator") or existing_route.get("operator_hint")

    applicable = {
        "ref": bool(r_ref and e_ref),
        "operator": bool(r_op and e_op),
        "terminus": bool((r_origin or r_dest) and (e_origin or e_dest)),
        "geometry": bool(r_bbox),
        "stops": bool(r_stops),
    }

    # redistribute weights: drop inapplicable signals, normalise remainder
    active_weight = sum(WEIGHTS[k] for k in WEIGHTS if applicable[k])
    if active_weight > 0:
        composite = sum(
            scores[k] * (WEIGHTS[k] / active_weight)
            for k in WEIGHTS
            if applicable[k]
        )
    else:
        composite = 0.0

    reasons = [k for k, v in scores.items() if v >= 0.7]

    return {
        "composite_score": round(composite, 3),
        "component_scores": scores,
        "match_reasons": reasons,
        "applicable_signals": [k for k in WEIGHTS if applicable[k]],
    }


# ── fetch existing routes for a canton bbox ─────────────────────────

_EXISTING_ROUTES_SQL = """
SELECT
    r.route_id::text AS route_id,
    r.route_name,
    r.source,
    r.geom IS NOT NULL AS has_geom,
    r.stop_node_ids,
    r.naming_confidence,
    -- service route semantics
    sr.route_ref AS service_route_ref,
    sr.operator_name AS operator_hint,
    sr.route_name AS service_route_name,
    -- OSM from/to via relation candidates
    (SELECT NULLIF(BTRIM(rc.ref), '')
     FROM route_raw.relation_candidates rc
     WHERE rc.route_id = r.route_id
     ORDER BY rc.found_at DESC LIMIT 1
    ) AS route_ref,
    -- from/to from OSM tags
    (SELECT NULLIF(BTRIM(
        (SELECT elem.value->'tags'->>'from'
         FROM jsonb_array_elements(orr.overpass_json->'elements') AS elem(value)
         WHERE elem.value->>'type' = 'relation' LIMIT 1)
     ), '')
     FROM route_raw.osm_relations_raw orr
     WHERE orr.route_id = r.route_id LIMIT 1
    ) AS osm_from,
    (SELECT NULLIF(BTRIM(
        (SELECT elem.value->'tags'->>'to'
         FROM jsonb_array_elements(orr.overpass_json->'elements') AS elem(value)
         WHERE elem.value->>'type' = 'relation' LIMIT 1)
     ), '')
     FROM route_raw.osm_relations_raw orr
     WHERE orr.route_id = r.route_id LIMIT 1
    ) AS osm_to,
    (SELECT NULLIF(BTRIM(rc.operator), '')
     FROM route_raw.relation_candidates rc
     WHERE rc.route_id = r.route_id
     ORDER BY rc.found_at DESC LIMIT 1
    ) AS osm_operator
FROM route_prod.routes r
LEFT JOIN route_raw.route_jobs rj ON rj.route_id = r.route_id
LEFT JOIN route_raw.service_route_directions srd ON srd.route_id = r.route_id
LEFT JOIN route_raw.service_routes sr
    ON sr.service_route_id = COALESCE(rj.service_route_id, srd.service_route_id)
WHERE r.geom IS NOT NULL
  AND ST_Intersects(
      r.geom,
      ST_MakeEnvelope(%s, %s, %s, %s, 4326)
  )
ORDER BY r.route_name
"""


def _fetch_existing_routes(canton_bbox: dict, conn) -> list[dict]:
    """Load all route_prod routes whose geometry intersects the canton bbox."""
    west = canton_bbox["west"]
    south = canton_bbox["south"]
    east = canton_bbox["east"]
    north = canton_bbox["north"]

    with db_cursor(conn) as cur:
        cur.execute(_EXISTING_ROUTES_SQL, (west, south, east, north))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def _get_canton_bbox(canton: str, conn) -> dict | None:
    """Resolve a canton name to a bbox via the geography catalogs or known areas."""
    # Try to get bbox from route_raw.route_jobs area_key entries
    sql = """
    SELECT bbox FROM route_raw.route_jobs
    WHERE area_key = %s AND bbox IS NOT NULL
    LIMIT 1
    """
    with db_cursor(conn) as cur:
        cur.execute(sql, (canton,))
        row = cur.fetchone()
        if row and row["bbox"]:
            bbox = row["bbox"]
            if isinstance(bbox, str):
                import json
                bbox = json.loads(bbox)
            return bbox
    return None


# ── main entry point ────────────────────────────────────────────────

def match_research_against_existing(
    research_routes: list[dict],
    canton: str,
    conn,
    *,
    canton_bbox: dict | None = None,
) -> dict:
    """
    Compare Deep Research route entries against routes already in route_prod
    (built from OSM or previous research batches).

    Parameters
    ----------
    research_routes : list[dict]
        Research route dicts (from seed catalog JSON).
    canton : str
        Canton area key (e.g. 'valle_de_los_chillos').
    conn
        psycopg2 connection.
    canton_bbox : dict, optional
        Explicit bbox {south, west, north, east}. Resolved from DB if omitted.

    Returns
    -------
    dict with keys:
        matched  – list of dicts with research_route, existing_route_id,
                   match_score, match_reasons, action='ENRICH'
        new      – list of dicts with research_route, action='BUILD'
        ambiguous – list of dicts with research_route, candidates, best_score,
                    action='REVIEW'
    """
    if canton_bbox is None:
        canton_bbox = _get_canton_bbox(canton, conn)
    if canton_bbox is None:
        # fallback: build bbox from research routes themselves
        canton_bbox = _bbox_from_research(research_routes)
    if canton_bbox is None:
        log.warning("No bbox resolved for canton=%s, treating all as NEW", canton)
        return {
            "matched": [],
            "new": [{"research_route": r, "action": "BUILD"} for r in research_routes],
            "ambiguous": [],
        }

    existing = _fetch_existing_routes(canton_bbox, conn)
    log.info("Loaded %d existing routes in canton=%s for dedup", len(existing), canton)

    if not existing:
        return {
            "matched": [],
            "new": [{"research_route": r, "action": "BUILD"} for r in research_routes],
            "ambiguous": [],
        }

    matched = []
    new = []
    ambiguous = []

    for rr in research_routes:
        best_score = 0.0
        best_result = None
        best_existing = None
        candidates = []

        for ex in existing:
            result = compute_match_score(rr, ex, conn)
            score = result["composite_score"]

            if score >= AMBIGUOUS_THRESHOLD:
                candidates.append({
                    "existing_route_id": ex["route_id"],
                    "route_name": ex.get("route_name") or ex.get("service_route_name"),
                    "score": score,
                    "reasons": result["match_reasons"],
                    "components": result["component_scores"],
                })

            if score > best_score:
                best_score = score
                best_result = result
                best_existing = ex

        rr_label = (
            rr.get("route_code")
            or rr.get("route_ref")
            or rr.get("route_name")
            or rr.get("display_name")
            or "unknown"
        )

        if best_score >= MATCH_THRESHOLD:
            log.info(
                "MATCHED %s → %s (score=%.3f, reasons=%s)",
                rr_label,
                best_existing["route_id"],
                best_score,
                best_result["match_reasons"],
            )
            matched.append({
                "research_route": rr,
                "existing_route_id": best_existing["route_id"],
                "match_score": best_score,
                "match_reasons": best_result["match_reasons"],
                "component_scores": best_result["component_scores"],
                "action": "ENRICH",
            })
        elif best_score >= AMBIGUOUS_THRESHOLD:
            log.info(
                "AMBIGUOUS %s (best=%.3f, %d candidates)",
                rr_label, best_score, len(candidates),
            )
            ambiguous.append({
                "research_route": rr,
                "candidates": sorted(candidates, key=lambda c: c["score"], reverse=True),
                "best_score": best_score,
                "action": "REVIEW",
            })
        else:
            log.info("NEW %s (best=%.3f)", rr_label, best_score)
            new.append({
                "research_route": rr,
                "action": "BUILD",
            })

    return {"matched": matched, "new": new, "ambiguous": ambiguous}


def _bbox_from_research(routes: list[dict]) -> dict | None:
    """Build an encompassing bbox from research routes' geography catalogs."""
    lats, lons = [], []
    for r in routes:
        geo = r.get("geography_catalog") or r.get("expected_geographic_envelope") or {}
        bbox = geo.get("envelope_bbox") or geo.get("bbox")
        if isinstance(bbox, dict):
            if bbox.get("south") is not None:
                lats.extend([bbox["south"], bbox["north"]])
                lons.extend([bbox["west"], bbox["east"]])
    if not lats:
        return None
    # pad by ~5 km
    pad = 0.05
    return {
        "south": min(lats) - pad,
        "north": max(lats) + pad,
        "west": min(lons) - pad,
        "east": max(lons) + pad,
    }


# ── enrichment ──────────────────────────────────────────────────────

def enrich_existing_route(
    existing_route_id: str,
    research_route: dict,
    conn,
) -> dict[str, bool]:
    """
    Update an existing OSM-built route with richer data from Deep Research.
    Does NOT rebuild the route — just adds metadata.

    Returns dict indicating which enrichments were applied.
    """
    applied = {}

    display_name = (
        research_route.get("display_name")
        or research_route.get("route_name")
    )
    if display_name:
        applied["name"] = _update_route_name_if_better(
            existing_route_id, display_name, conn
        )

    # bump naming confidence: Deep Research confirmation → 0.75
    applied["confidence"] = _update_naming_confidence(
        existing_route_id, 0.75, "deep_research_confirmed", conn
    )

    # store enrichment metadata in direction_semantics jsonb
    seed = research_route.get("seed_catalog") or {}
    if seed:
        applied["semantics"] = _upsert_route_semantics(
            existing_route_id, research_route, conn
        )

    log.info("Enriched route %s: %s", existing_route_id, applied)
    return applied


_GENERIC_NAME_RE = re.compile(r"^[A-Z0-9\-]+$")


def _update_route_name_if_better(
    route_id: str, new_name: str, conn
) -> bool:
    """Set route_name only if currently NULL or generic.

    Migrated off direct UPDATE: reads the current route_name first, applies
    the "NULL / empty / all-uppercase-alphanumeric" predicate in Python,
    then patches via the canonical wrapper.
    """
    with db_cursor(conn) as cur:
        cur.execute(
            "SELECT route_name FROM route_prod.routes WHERE route_id = %s::uuid",
            (route_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        current = row["route_name"] if isinstance(row, dict) else row[0]

    if current and current.strip() and not _GENERIC_NAME_RE.match(current):
        return False

    res = patch_route_prod_fields(
        conn=conn,
        route_id=route_id,
        fields={"route_name": new_name},
        source_type="route_matcher.name_if_better",
        pipeline_version="phase3_dedup.route_matcher",
    )
    return res.success and res.rows_affected.get("route_prod.routes", 0) > 0


def _update_naming_confidence(
    route_id: str, confidence: float, source: str, conn
) -> bool:
    """Bump naming_confidence if the new value is higher.

    Migrated off direct UPDATE: reads current naming_confidence +
    direction_semantics in Python, computes GREATEST + jsonb_set equivalents,
    then patches via the canonical wrapper.
    """
    with db_cursor(conn) as cur:
        cur.execute(
            "SELECT naming_confidence, direction_semantics "
            "FROM route_prod.routes WHERE route_id = %s::uuid",
            (route_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        if isinstance(row, dict):
            existing_conf = row["naming_confidence"]
            existing_ds = row["direction_semantics"]
        else:
            existing_conf, existing_ds = row

    new_conf = max(existing_conf or 0.0, confidence)
    merged_ds = dict(existing_ds or {})
    merged_ds["confidence_source"] = source

    from datetime import datetime, timezone
    res = patch_route_prod_fields(
        conn=conn,
        route_id=route_id,
        fields={
            "naming_confidence": new_conf,
            "direction_semantics": merged_ds,
            "semantics_updated_at": datetime.now(timezone.utc),
        },
        source_type="route_matcher.naming_confidence",
        pipeline_version="phase3_dedup.route_matcher",
    )
    return res.success and res.rows_affected.get("route_prod.routes", 0) > 0


def _upsert_route_semantics(
    route_id: str, research_route: dict, conn
) -> bool:
    """Store Deep Research semantics in direction_semantics jsonb.

    Migrated off direct UPDATE: reads current direction_semantics, merges
    the enrichment dict in Python (equivalent to ``||`` jsonb concat with
    right-hand wins), then patches via the canonical wrapper.
    """
    seed = research_route.get("seed_catalog") or {}
    geo = research_route.get("geography_catalog") or {}

    enrichment = {}
    origin = (seed.get("terminus_origin") or {}).get("name")
    dest = (seed.get("terminus_destination") or {}).get("name")
    if origin:
        enrichment["research_origin"] = origin
    if dest:
        enrichment["research_destination"] = dest
    if research_route.get("cooperative") or research_route.get("cooperative_name"):
        enrichment["research_operator"] = (
            research_route.get("cooperative") or research_route.get("cooperative_name")
        )
    if geo.get("normalized_sector_key"):
        enrichment["sector_key"] = geo["normalized_sector_key"]

    if not enrichment:
        return False

    with db_cursor(conn) as cur:
        cur.execute(
            "SELECT direction_semantics FROM route_prod.routes WHERE route_id = %s::uuid",
            (route_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        existing_ds = row["direction_semantics"] if isinstance(row, dict) else row[0]

    merged = dict(existing_ds or {})
    merged.update(enrichment)

    from datetime import datetime, timezone
    res = patch_route_prod_fields(
        conn=conn,
        route_id=route_id,
        fields={
            "direction_semantics": merged,
            "semantics_updated_at": datetime.now(timezone.utc),
        },
        source_type="route_matcher.research_semantics",
        pipeline_version="phase3_dedup.route_matcher",
    )
    return res.success and res.rows_affected.get("route_prod.routes", 0) > 0
