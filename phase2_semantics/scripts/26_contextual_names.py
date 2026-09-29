"""
Step 26 — Contextual Name Disambiguation

Classifies duplicate place names and generates contextual replacements.
Runs between Step 25 (name candidates) and Step 30 (approve).

Usage:
    python -m scripts.26_contextual_names --dry-run
    python -m scripts.26_contextual_names --apply [--threshold 50] [--limit 1000]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from time import perf_counter
from typing import Any, Dict, List

from src.db.conn import db_conn
from src.pipeline.naming.name_classifier import (
    PlaceRecord,
    classify_all_duplicates,
    load_classification_rules,
    strip_route_code,
)
from src.pipeline.naming.contextual_name_generator import (
    ContextualName,
    generate_contextual_name,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("phase2.step26")


def _fetch_duplicate_name_groups(conn, threshold: int) -> Dict[str, List[PlaceRecord]]:
    """Fetch all active place names with occurrence >= threshold."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT canonical_name, COUNT(*) AS cnt
            FROM geo_prod.places
            WHERE status = 'active'
              AND canonical_name IS NOT NULL
            GROUP BY canonical_name
            HAVING COUNT(*) >= %s
            ORDER BY cnt DESC
            """,
            (threshold,),
        )
        dup_names = [row["canonical_name"] for row in cur.fetchall()]

    logger.info("Found %d duplicate names above threshold %d", len(dup_names), threshold)

    groups: Dict[str, List[PlaceRecord]] = {}
    # Fetch in batches to avoid memory issues
    batch_size = 100
    for i in range(0, len(dup_names), batch_size):
        batch = dup_names[i : i + batch_size]
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.place_id::text,
                    p.canonical_name,
                    p.place_type,
                    ST_Y(p.geom) AS lat,
                    ST_X(p.geom) AS lon
                FROM geo_prod.places p
                WHERE p.status = 'active'
                  AND p.canonical_name = ANY(%s)
                  AND p.geom IS NOT NULL
                """,
                (batch,),
            )
            for row in cur.fetchall():
                name = row["canonical_name"]
                if name not in groups:
                    groups[name] = []
                groups[name].append(
                    PlaceRecord(
                        place_id=row["place_id"],
                        canonical_name=name,
                        place_type=row["place_type"],
                        lat=float(row["lat"]),
                        lon=float(row["lon"]),
                    )
                )

    return groups


def _apply_route_code_cleanup(
    conn, groups: Dict[str, List[PlaceRecord]], catalog: Dict[str, Any], *, dry_run: bool,
) -> Dict[str, Any]:
    """Strip route code prefixes from names and update in-place."""
    patterns = catalog.get("route_code_leak_patterns", [])
    cleaned = []

    for name, places in list(groups.items()):
        stripped = strip_route_code(name, patterns)
        if stripped != name and stripped:
            for p in places:
                cleaned.append({"place_id": p.place_id, "old_name": name, "new_name": stripped})

    if not dry_run and cleaned:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.changed_by = 'step26_route_code_cleanup'")
            cur.execute("SET LOCAL app.change_reason = 'Strip route code prefix leak'")
            for item in cleaned:
                cur.execute(
                    "UPDATE geo_prod.places SET canonical_name = %s, updated_at = now() WHERE place_id = %s::uuid",
                    (item["new_name"], item["place_id"]),
                )

    return {"route_code_cleaned": len(cleaned), "samples": cleaned[:10]}


def _generate_names_for_group(
    conn,
    name: str,
    places: List[PlaceRecord],
    category: str,
    *,
    limit: int = 0,
) -> List[ContextualName]:
    """Generate contextual names for all places in a group."""
    centroid = None
    if category == "DIRECTIONAL_PAIR" and places:
        clat = sum(p.lat for p in places) / len(places)
        clon = sum(p.lon for p in places) / len(places)
        centroid = (clat, clon)

    results = []
    target_places = places[:limit] if limit > 0 else places
    for p in target_places:
        ctx_name = generate_contextual_name(
            conn,
            place_id=p.place_id,
            original_name=p.canonical_name,
            lat=p.lat,
            lon=p.lon,
            category=category,
            group_centroid=centroid,
        )
        results.append(ctx_name)
    return results


def _check_uniqueness(conn, names: List[ContextualName], radius_m: float = 500) -> List[ContextualName]:
    """Within radius_m, no two active places should share the new name."""
    # Simple dedup: if new_name collides, append place_id fragment
    seen: Dict[str, str] = {}  # new_name -> first place_id
    deduped = []
    for cn in names:
        key = cn.new_name.lower().strip()
        if key in seen and seen[key] != cn.place_id:
            # Append disambiguator
            cn = ContextualName(
                place_id=cn.place_id,
                original_name=cn.original_name,
                new_name=f"{cn.new_name} ({cn.place_id[:6]})",
                category=cn.category,
                cascade_level=cn.cascade_level,
                confidence=cn.confidence * 0.9,
            )
        else:
            seen[key] = cn.place_id
        deduped.append(cn)
    return deduped


def _apply_names(conn, names: List[ContextualName], *, dry_run: bool) -> Dict[str, int]:
    """Write generated names to geo_prod.places and geo_prod.place_aliases."""
    if dry_run or not names:
        return {"applied": 0}

    applied = 0
    with conn.cursor() as cur:
        cur.execute("SET LOCAL app.changed_by = 'step26_contextual_names'")
        cur.execute("SET LOCAL app.change_reason = 'Contextual name disambiguation'")

        for cn in names:
            # Update canonical_name
            cur.execute(
                "UPDATE geo_prod.places SET canonical_name = %s, updated_at = now() WHERE place_id = %s::uuid AND status = 'active'",
                (cn.new_name, cn.place_id),
            )
            if cur.rowcount > 0:
                applied += 1

            # Add old name as alt alias (preserve searchability)
            if cn.original_name and cn.original_name != cn.new_name:
                cur.execute(
                    """INSERT INTO geo_prod.place_aliases
                       (alias_id, place_id, alias, normalized_alias, alias_kind, updated_at)
                       VALUES (gen_random_uuid(), %s::uuid, %s, lower(%s), 'historic', now())
                       ON CONFLICT (place_id, normalized_alias) DO NOTHING""",
                    (cn.place_id, cn.original_name, cn.original_name),
                )

            # Add new name as official alias
            cur.execute(
                """INSERT INTO geo_prod.place_aliases
                   (alias_id, place_id, alias, normalized_alias, alias_kind, updated_at)
                   VALUES (gen_random_uuid(), %s::uuid, %s, lower(%s), 'official', now())
                   ON CONFLICT (place_id, normalized_alias) DO UPDATE SET
                     alias = EXCLUDED.alias, alias_kind = 'official', updated_at = now()""",
                (cn.place_id, cn.new_name, cn.new_name),
            )

    return {"applied": applied}


def main():
    parser = argparse.ArgumentParser(description="Step 26 — Contextual Name Disambiguation")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    parser.add_argument("--threshold", type=int, default=50, help="Min occurrences to classify as duplicate")
    parser.add_argument("--limit", type=int, default=0, help="Max places to rename per group (0=all)")
    parser.add_argument("--catalog", type=str, default=None, help="Path to classification rules catalog")
    args = parser.parse_args()

    dry_run = not args.apply
    mode = "DRY RUN" if dry_run else "APPLY"

    print(f"\n{'='*60}")
    print(f"  Step 26 — Contextual Name Disambiguation [{mode}]")
    print(f"{'='*60}\n")

    catalog = load_classification_rules(args.catalog)
    t0 = perf_counter()

    with db_conn() as conn:
        # 1. Fetch duplicate groups
        groups = _fetch_duplicate_name_groups(conn, args.threshold)
        logger.info("Loaded %d name groups with %d total places",
                     len(groups), sum(len(v) for v in groups.values()))

        # 2. Route code cleanup (runs first)
        rc_result = _apply_route_code_cleanup(conn, groups, catalog, dry_run=dry_run)
        print(f"Route code cleanup: {rc_result['route_code_cleaned']} names cleaned")
        if not dry_run:
            conn.commit()
            # Refresh groups after cleanup
            groups = _fetch_duplicate_name_groups(conn, args.threshold)

        # 3. Classify all duplicate names
        classifications = classify_all_duplicates(groups, catalog, threshold=args.threshold)
        logger.info("Classified %d name groups", len(classifications))

        # Stats
        cat_counts: Dict[str, int] = defaultdict(int)
        cat_places: Dict[str, int] = defaultdict(int)
        for name, info in classifications.items():
            cat_counts[info["category"]] += 1
            cat_places[info["category"]] += info["count"]

        print("\n--- Classification Summary ---")
        for cat in ["GARBAGE", "OVER_APPLIED", "DIRECTIONAL_PAIR", "UNIQUE"]:
            print(f"  {cat:20s}: {cat_counts.get(cat, 0):>6,} names, {cat_places.get(cat, 0):>8,} places")

        # 4. Generate contextual names for non-UNIQUE categories
        all_names: List[ContextualName] = []
        for name, info in classifications.items():
            if info["category"] == "UNIQUE":
                continue
            places = groups.get(name, [])
            generated = _generate_names_for_group(
                conn, name, places, info["category"], limit=args.limit,
            )
            all_names.extend(generated)

        logger.info("Generated %d contextual names", len(all_names))

        # 5. Uniqueness check
        all_names = _check_uniqueness(conn, all_names)

        # 6. Report by cascade level
        level_counts: Dict[str, int] = defaultdict(int)
        for cn in all_names:
            level_counts[cn.cascade_level] += 1

        print("\n--- Cascade Level Breakdown ---")
        for level in ["intersection", "landmark", "sector", "direction", "fallback"]:
            print(f"  {level:15s}: {level_counts.get(level, 0):>8,}")

        # 7. Apply
        if not dry_run:
            apply_result = _apply_names(conn, all_names, dry_run=False)
            conn.commit()
            print(f"\n--- Applied: {apply_result['applied']} place names updated ---")
        else:
            print(f"\n--- DRY RUN: {len(all_names)} names would be updated ---")
            # Show samples
            for cat in ["GARBAGE", "OVER_APPLIED", "DIRECTIONAL_PAIR"]:
                samples = [cn for cn in all_names if cn.category == cat][:5]
                if samples:
                    print(f"\n  Samples ({cat}):")
                    for s in samples:
                        print(f"    {s.original_name!r:30s} → {s.new_name!r:50s} [{s.cascade_level}]")

    elapsed = perf_counter() - t0
    print(f"\nTotal elapsed: {elapsed:.1f}s")
    print("Done.\n")


if __name__ == "__main__":
    main()
