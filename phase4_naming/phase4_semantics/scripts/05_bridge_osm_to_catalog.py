# phase4_semantics/scripts/05_bridge_osm_to_catalog.py
"""
Bridge OSM relation tags to catalog.route_semantics for OSM-built routes.

Run AFTER Phase 3 construction, BEFORE Deep Research enrichment.
Deep Research data will UPSERT on top with higher confidence.

Usage:
    PYTHONPATH=phase4_naming python phase4_naming/phase4_semantics/scripts/05_bridge_osm_to_catalog.py
    PYTHONPATH=phase4_naming python phase4_naming/phase4_semantics/scripts/05_bridge_osm_to_catalog.py --area-key el_valle
    PYTHONPATH=phase4_naming python phase4_naming/phase4_semantics/scripts/05_bridge_osm_to_catalog.py --dry-run
    PYTHONPATH=phase4_naming python phase4_naming/phase4_semantics/scripts/05_bridge_osm_to_catalog.py --route-id <uuid>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phase4_semantics.common.db import get_conn, fetchall, fetchone
from phase4_semantics.naming.normalize import cleanup_text, canonicalize_operator


# ============================================================
# Tag extraction
# ============================================================

def extract_semantics_from_osm(relation_tags: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse OSM relation tags into catalog.route_semantics fields.
    """
    tags = relation_tags or {}

    route_short_name = cleanup_text(
        str(tags.get("route_ref") or tags.get("ref") or "")
    )
    operator = canonicalize_operator(
        str(tags.get("operator") or tags.get("network") or "")
    )
    public_origin = cleanup_text(str(tags.get("from") or ""))
    public_destination = cleanup_text(str(tags.get("to") or ""))
    route_long_name = cleanup_text(str(tags.get("name") or ""))

    # Build route_long_name from endpoints if missing
    if not route_long_name and public_origin and public_destination:
        route_long_name = f"{public_origin} - {public_destination}"

    # Confidence: 0.30 base + 0.10 per non-empty key field, max 0.70
    filled = sum(1 for v in [
        route_short_name, operator, public_origin, public_destination,
    ] if v)
    confidence = round(0.30 + (filled * 0.10), 2)

    return {
        "route_short_name": route_short_name,
        "operator": operator,
        "public_origin": public_origin,
        "public_destination": public_destination,
        "route_long_name": route_long_name,
        "route_type": 3,  # bus
        "evidence_source": "osm_relation",
        "confidence": confidence,
    }


# ============================================================
# Relation tag loader
# ============================================================

def _extract_relation_tags_from_overpass(
    overpass_json: Dict[str, Any],
    relation_id: Optional[int],
) -> Dict[str, Any]:
    """Extract tags dict from the matching relation element in the Overpass payload."""
    elements = overpass_json.get("elements") or []
    if relation_id is not None:
        for el in elements:
            if el.get("type") == "relation" and int(el.get("id", -1)) == int(relation_id):
                return dict(el.get("tags") or {})
    # Fallback: first relation element
    for el in elements:
        if el.get("type") == "relation":
            return dict(el.get("tags") or {})
    return {}


# ============================================================
# Jurisdiction detection
# ============================================================

# Rough bbox for DMQ vs ANT jurisdictions (Quito metro)
_DMQ_BBOX = {"south": -0.40, "north": 0.10, "west": -78.65, "east": -78.30}


def detect_jurisdiction(lat: Optional[float], lon: Optional[float]) -> str:
    """Simple bbox-based jurisdiction. Falls back to DMQ."""
    if lat is None or lon is None:
        return "DMQ"
    if (_DMQ_BBOX["south"] <= lat <= _DMQ_BBOX["north"]
            and _DMQ_BBOX["west"] <= lon <= _DMQ_BBOX["east"]):
        return "DMQ"
    return "ANT"


def _get_route_centroid(route_id: str) -> Dict[str, Optional[float]]:
    """Get route centroid from route_prod.routes geometry."""
    row = fetchone(
        """
        SELECT ST_Y(ST_Centroid(geom)) AS lat, ST_X(ST_Centroid(geom)) AS lon
        FROM route_prod.routes
        WHERE route_id = %s
        """,
        (route_id,),
    )
    if row:
        return {"lat": row.get("lat"), "lon": row.get("lon")}
    return {"lat": None, "lon": None}


# ============================================================
# Catalog inserts
# ============================================================

def upsert_catalog_route_semantics(
    cur, route_id: str, semantics: Dict[str, Any], jurisdiction: str,
) -> str:
    """
    Insert/update catalog.route_semantics.
    Only overwrites if existing confidence is lower (preserves Deep Research data).
    Returns 'inserted', 'updated', or 'skipped'.
    """
    cur.execute(
        """
        SELECT confidence FROM catalog.route_semantics WHERE route_id = %s
        """,
        (route_id,),
    )
    existing = cur.fetchone()

    if existing is not None:
        existing_conf = float(existing[0] or 0)
        if existing_conf >= semantics["confidence"]:
            return "skipped"
        # Update — our data has higher confidence
        cur.execute(
            """
            UPDATE catalog.route_semantics SET
                operator            = COALESCE(NULLIF(%(operator)s, ''), operator),
                route_short_name    = COALESCE(NULLIF(%(route_short_name)s, ''), route_short_name),
                route_long_name     = COALESCE(NULLIF(%(route_long_name)s, ''), route_long_name),
                route_type          = %(route_type)s,
                public_origin       = COALESCE(NULLIF(%(public_origin)s, ''), public_origin),
                public_destination  = COALESCE(NULLIF(%(public_destination)s, ''), public_destination),
                jurisdiction        = %(jurisdiction)s,
                evidence_source     = %(evidence_source)s,
                confidence          = %(confidence)s
            WHERE route_id = %(route_id)s
            """,
            {**semantics, "route_id": route_id, "jurisdiction": jurisdiction},
        )
        return "updated"
    else:
        cur.execute(
            """
            INSERT INTO catalog.route_semantics (
                route_id, operator, route_short_name, route_long_name, route_type,
                public_origin, public_destination, aliases, jurisdiction,
                evidence_source, confidence, approved
            ) VALUES (
                %(route_id)s, %(operator)s, %(route_short_name)s, %(route_long_name)s,
                %(route_type)s, %(public_origin)s, %(public_destination)s,
                ARRAY[]::text[], %(jurisdiction)s, %(evidence_source)s, %(confidence)s, false
            )
            """,
            {**semantics, "route_id": route_id, "jurisdiction": jurisdiction},
        )
        return "inserted"


def upsert_route_prod_semantics(
    cur, route_id: str, semantics: Dict[str, Any],
) -> str:
    """
    Insert/update route_prod.route_semantics with OSM-derived data.
    Returns 'inserted', 'updated', or 'skipped'.
    """
    cur.execute(
        """
        SELECT route_id, human_verified
        FROM route_prod.route_semantics
        WHERE route_id = %s
        """,
        (route_id,),
    )
    existing = cur.fetchone()

    if existing is not None:
        # Never overwrite human-verified rows
        if existing[1]:
            return "skipped"
        cur.execute(
            """
            UPDATE route_prod.route_semantics SET
                route_name          = COALESCE(NULLIF(%(route_long_name)s, ''), route_name),
                route_ref           = COALESCE(NULLIF(%(route_short_name)s, ''), route_ref),
                operator_name       = COALESCE(NULLIF(%(operator)s, ''), operator_name),
                naming_confidence   = %(confidence)s,
                semantics_updated_at = now()
            WHERE route_id = %(route_id)s
            """,
            {**semantics, "route_id": route_id},
        )
        return "updated"
    else:
        cur.execute(
            """
            INSERT INTO route_prod.route_semantics (
                route_id, route_name, route_ref, operator_name,
                naming_confidence, human_verified, semantics_updated_at
            ) VALUES (
                %(route_id)s, %(route_long_name)s, %(route_short_name)s,
                %(operator)s, %(confidence)s, false, now()
            )
            """,
            {**semantics, "route_id": route_id},
        )
        return "inserted"


# ============================================================
# Core bridge logic
# ============================================================

def find_bridgeable_routes(
    area_key: Optional[str] = None,
    route_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Find routes built from OSM relations that lack or have low-confidence catalog data.
    """
    conditions = ["ore.overpass_json IS NOT NULL"]
    params: list = []

    if route_ids:
        conditions.append("rj.route_id = ANY(%s)")
        params.append(route_ids)
    if area_key:
        conditions.append("rj.area_key = %s")
        params.append(area_key)

    where = " AND ".join(conditions)

    return fetchall(
        f"""
        SELECT
            rj.route_id,
            rj.known_ref,
            rj.area_key,
            rj.chosen_osm_relation_id,
            ore.overpass_json
        FROM route_raw.route_jobs rj
        JOIN route_raw.osm_relations_raw ore ON ore.route_id = rj.route_id
        JOIN route_prod.routes rp ON rp.route_id = rj.route_id
        LEFT JOIN catalog.route_semantics cs ON cs.route_id = rj.route_id
        WHERE (cs.route_id IS NULL OR cs.confidence < 0.50)
          AND rj.is_trashed = false
          AND {where}
        ORDER BY rj.area_key, rj.known_ref
        """,
        params,
    )


def bridge_single_route(
    cur,
    route_id: str,
    relation_tags: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Bridge OSM tags to catalog + route_prod for a single route."""
    semantics = extract_semantics_from_osm(relation_tags)

    centroid = _get_route_centroid(route_id)
    jurisdiction = detect_jurisdiction(centroid["lat"], centroid["lon"])

    result = {
        "route_id": route_id,
        "semantics": semantics,
        "jurisdiction": jurisdiction,
    }

    if dry_run:
        result["action"] = "dry_run"
        return result

    catalog_action = upsert_catalog_route_semantics(cur, route_id, semantics, jurisdiction)
    prod_action = upsert_route_prod_semantics(cur, route_id, semantics)

    result["catalog_action"] = catalog_action
    result["prod_action"] = prod_action
    return result


def bridge_osm_tags_to_catalog(
    *,
    area_key: Optional[str] = None,
    route_ids: Optional[List[str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    For routes built from OSM relations, extract relation tags
    and populate catalog.route_semantics + route_prod.route_semantics.

    Runs AFTER Phase 3 construction and BEFORE Deep Research enrichment.
    Deep Research data will UPSERT on top with higher confidence.
    """
    routes = find_bridgeable_routes(area_key=area_key, route_ids=route_ids)
    stats = {"total": len(routes), "bridged": 0, "skipped": 0, "errors": []}
    details: List[Dict[str, Any]] = []

    if not routes:
        return {**stats, "details": details}

    with get_conn() as conn:
        cur = conn.cursor()
        try:
            for route in routes:
                try:
                    overpass_json = route["overpass_json"]
                    if not isinstance(overpass_json, dict):
                        stats["skipped"] += 1
                        continue

                    relation_id = route.get("chosen_osm_relation_id")
                    tags = _extract_relation_tags_from_overpass(overpass_json, relation_id)

                    if not tags:
                        stats["skipped"] += 1
                        continue

                    result = bridge_single_route(
                        cur, str(route["route_id"]), tags, dry_run=dry_run,
                    )
                    details.append(result)

                    if dry_run or result.get("catalog_action") in ("inserted", "updated"):
                        stats["bridged"] += 1
                    else:
                        stats["skipped"] += 1

                except Exception as e:
                    stats["errors"].append({
                        "route_id": str(route["route_id"]),
                        "error": str(e),
                    })

            if dry_run:
                conn.rollback()
        finally:
            cur.close()

    return {**stats, "details": details}


# ============================================================
# CLI
# ============================================================

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Bridge OSM relation tags to catalog.route_semantics",
    )
    ap.add_argument("--area-key", default=None, help="Filter by area_key (e.g. el_valle)")
    ap.add_argument("--route-id", default=None, help="Bridge a single route by UUID")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be bridged without writing")
    ap.add_argument("--json", action="store_true", dest="output_json", help="Output full JSON results")
    args = ap.parse_args()

    route_ids = [args.route_id] if args.route_id else None

    result = bridge_osm_tags_to_catalog(
        area_key=args.area_key,
        route_ids=route_ids,
        dry_run=args.dry_run,
    )

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{prefix}OSM → Catalog Bridge Results")
    print(f"  Total candidates: {result['total']}")
    print(f"  Bridged:          {result['bridged']}")
    print(f"  Skipped:          {result['skipped']}")
    print(f"  Errors:           {len(result['errors'])}")

    if result["errors"]:
        print("\nErrors:")
        for err in result["errors"]:
            print(f"  {err['route_id']}: {err['error']}")

    if args.dry_run and result["details"]:
        print(f"\nSample bridged routes (showing up to 10):")
        for d in result["details"][:10]:
            sem = d["semantics"]
            print(f"  {d['route_id'][:8]}.. | {sem['route_short_name'] or '?':>8} | "
                  f"{sem['operator'][:30] or '?':<30} | "
                  f"{sem['public_origin'][:20] or '?'} → {sem['public_destination'][:20] or '?'} | "
                  f"conf={sem['confidence']:.2f} | jur={d['jurisdiction']}")

    if args.output_json:
        print("\n" + json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
