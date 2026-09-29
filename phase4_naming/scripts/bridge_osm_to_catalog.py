#!/usr/bin/env python3
"""
Bridge OSM relation tags → catalog.route_semantics.

Reads OSM tags from route_raw.relation_candidates (the chosen relation per route)
and populates catalog.route_semantics with baseline data at low confidence (0.3-0.7)
so Deep Research data (higher confidence) always wins on UPSERT.

Usage:
    cd phase4_naming
    PYTHONPATH=. python scripts/bridge_osm_to_catalog.py
    PYTHONPATH=. python scripts/bridge_osm_to_catalog.py --canton cayambe
    PYTHONPATH=. python scripts/bridge_osm_to_catalog.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DB_DSN = os.environ.get(
    "DB_DSN",
    "postgresql://localhost:5432/datamind_ml",
)

# Tag → catalog field mapping
TAG_MAP = {
    "ref": "route_short_name",
    "operator": "operator",
    "network": "operator",      # fallback if no operator tag
    "from": "public_origin",
    "to": "public_destination",
    "name": "route_long_name",
}


def confidence_for_field_count(n: int) -> float:
    """Low confidence so Deep Research data always wins."""
    if n <= 0:
        return 0.0
    return min(0.7, 0.3 + n * 0.1)


def extract_semantics_from_tags(tags: Dict[str, Any]) -> Dict[str, Any]:
    """Map OSM relation tags to catalog fields."""
    result: Dict[str, Any] = {}
    for osm_key, catalog_field in TAG_MAP.items():
        val = tags.get(osm_key)
        if val and isinstance(val, str) and val.strip():
            # Don't overwrite with network if operator already set
            if catalog_field == "operator" and catalog_field in result:
                continue
            result[catalog_field] = val.strip()

    # Clean up ref: some OSM refs include operator name, e.g. "COSIB SOTRANOR Terminal..."
    # If ref looks like it contains the full name, prefer just the short code
    ref = result.get("route_short_name", "")
    if ref and len(ref) > 30 and result.get("operator"):
        # Strip operator prefix from ref
        op = result["operator"]
        if ref.upper().startswith(op.upper()):
            cleaned = ref[len(op):].strip(" -:")
            if cleaned:
                result["route_short_name"] = cleaned

    return result


def detect_jurisdiction(
    route_id: str, conn: Any, default: str = "DMQ"
) -> str:
    """Detect jurisdiction from route centroid. Defaults to DMQ."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ST_Y(ST_Centroid(geom)) AS lat, ST_X(ST_Centroid(geom)) AS lon
                FROM route_prod.routes WHERE route_id = %s::uuid
                """,
                (route_id,),
            )
            row = cur.fetchone()
            if row:
                lat, lon = row
                # Rough heuristic: DMQ is roughly lat -0.05 to -0.35
                # Cantons outside this range are ANT jurisdiction
                if lat is not None and (lat > 0.0 or lat < -0.5):
                    return "ANT"
    except Exception:
        pass
    return default


def fetch_routes_with_osm_tags(
    conn: Any, canton: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Get routes that have OSM relation candidates with tags.
    Returns list of {route_id, osm_relation_id, tags, ref, name, operator}.
    """
    # Find the chosen relation per route (rank 1 or highest score)
    query = """
        SELECT DISTINCT ON (rc.route_id)
            rc.route_id::text AS route_id,
            rc.osm_relation_id,
            rc.ref,
            rc.name,
            rc.operator,
            rc.tags
        FROM route_raw.relation_candidates rc
        JOIN route_prod.routes r ON r.route_id = rc.route_id
        WHERE rc.tags IS NOT NULL
    """
    params: list = []

    if canton:
        # Filter by canton bbox if available
        bbox_file = os.path.join(
            os.path.expanduser("~"),
            "Desktop", "phase3_route_catalog", "cantons", canton, "canton_bbox.json",
        )
        if os.path.exists(bbox_file):
            with open(bbox_file) as f:
                bbox_data = json.load(f)
            bbox = bbox_data.get("bbox")
            if bbox and len(bbox) == 4:
                s, w, n, e = bbox
                query += """
                    AND ST_Intersects(
                        r.geom,
                        ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                    )
                """
                params.extend([w, s, e, n])

    query += """
        ORDER BY rc.route_id,
                 (rc.tags->>'_datamind_candidate_meta')::jsonb->>'score' DESC NULLS LAST
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def upsert_catalog_semantics(
    conn: Any,
    route_id: str,
    fields: Dict[str, Any],
    confidence: float,
    dry_run: bool = False,
) -> str:
    """
    INSERT into catalog.route_semantics with ON CONFLICT:
    only update if existing confidence is LOWER.
    Returns 'inserted', 'updated', or 'skipped'.
    """
    if dry_run:
        return "dry_run"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO catalog.route_semantics (
                route_id, operator, route_short_name, route_long_name,
                route_type, public_origin, public_destination,
                jurisdiction, evidence_source, confidence,
                created_at, updated_at
            ) VALUES (
                %s::uuid, %s, %s, %s,
                3, %s, %s,
                %s, %s, %s,
                NOW(), NOW()
            )
            ON CONFLICT (route_id) DO UPDATE SET
                operator = CASE WHEN catalog.route_semantics.confidence < EXCLUDED.confidence
                    THEN COALESCE(EXCLUDED.operator, catalog.route_semantics.operator)
                    ELSE catalog.route_semantics.operator END,
                route_short_name = CASE WHEN catalog.route_semantics.confidence < EXCLUDED.confidence
                    THEN COALESCE(EXCLUDED.route_short_name, catalog.route_semantics.route_short_name)
                    ELSE catalog.route_semantics.route_short_name END,
                route_long_name = CASE WHEN catalog.route_semantics.confidence < EXCLUDED.confidence
                    THEN COALESCE(EXCLUDED.route_long_name, catalog.route_semantics.route_long_name)
                    ELSE catalog.route_semantics.route_long_name END,
                public_origin = CASE WHEN catalog.route_semantics.confidence < EXCLUDED.confidence
                    THEN COALESCE(EXCLUDED.public_origin, catalog.route_semantics.public_origin)
                    ELSE catalog.route_semantics.public_origin END,
                public_destination = CASE WHEN catalog.route_semantics.confidence < EXCLUDED.confidence
                    THEN COALESCE(EXCLUDED.public_destination, catalog.route_semantics.public_destination)
                    ELSE catalog.route_semantics.public_destination END,
                jurisdiction = CASE WHEN catalog.route_semantics.confidence < EXCLUDED.confidence
                    THEN COALESCE(EXCLUDED.jurisdiction, catalog.route_semantics.jurisdiction)
                    ELSE catalog.route_semantics.jurisdiction END,
                confidence = GREATEST(catalog.route_semantics.confidence, EXCLUDED.confidence),
                updated_at = NOW()
            """,
            (
                route_id,
                fields.get("operator"),
                fields.get("route_short_name"),
                fields.get("route_long_name"),
                fields.get("public_origin"),
                fields.get("public_destination"),
                fields.get("jurisdiction", "DMQ"),
                "osm_bridge",
                confidence,
            ),
        )
        # Check if it was an insert or update
        return "upserted"


def bridge_all_osm_routes(
    canton: Optional[str] = None, dry_run: bool = False
) -> Dict[str, int]:
    """Main entry: bridge OSM tags → catalog for all matching routes."""
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True

    rows = fetch_routes_with_osm_tags(conn, canton)
    stats = {"total": len(rows), "bridged": 0, "skipped": 0, "errors": 0}

    for row in rows:
        route_id = row["route_id"]
        tags = row["tags"] or {}

        # Strip internal metadata
        tags_clean = {k: v for k, v in tags.items() if not k.startswith("_")}

        fields = extract_semantics_from_tags(tags_clean)
        if not fields:
            stats["skipped"] += 1
            continue

        n_fields = len(fields)
        confidence = confidence_for_field_count(n_fields)

        # Detect jurisdiction
        fields["jurisdiction"] = detect_jurisdiction(route_id, conn)

        if dry_run:
            print(
                f"  DRY: {route_id} | conf={confidence:.1f} | "
                f"fields={list(fields.keys())}"
            )
            stats["bridged"] += 1
            continue

        try:
            upsert_catalog_semantics(conn, route_id, fields, confidence)
            stats["bridged"] += 1
        except Exception as exc:
            print(f"  ERROR: {route_id}: {exc}")
            stats["errors"] += 1

    conn.close()
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Bridge OSM relation tags to catalog.route_semantics"
    )
    parser.add_argument("--canton", help="Filter by canton name")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without writing"
    )
    args = parser.parse_args()

    print(f"Bridging OSM tags -> catalog.route_semantics")
    if args.canton:
        print(f"  Canton filter: {args.canton}")
    if args.dry_run:
        print(f"  DRY RUN mode")

    stats = bridge_all_osm_routes(canton=args.canton, dry_run=args.dry_run)

    print(f"\nResults:")
    print(f"  Total routes with OSM tags: {stats['total']}")
    print(f"  Bridged: {stats['bridged']}")
    print(f"  Skipped (no useful tags): {stats['skipped']}")
    print(f"  Errors: {stats['errors']}")


if __name__ == "__main__":
    main()
