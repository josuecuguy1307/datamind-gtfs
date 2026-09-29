"""
Step 26b — Fast Batch Contextual Name Disambiguation

SQL-based batch approach that processes all garbage names at once using
LATERAL joins instead of per-place Python loops.

Three-tier cascade:
  1. Recover: use node_prod tags if they have a useful name
  2. Proximity: nearest named non-garbage place + sequence number
  3. Sector: geohash-based fallback

Handles ~85K garbage-named places in minutes instead of hours.

Usage:
    cd phase2_semantics
    python -m scripts.26b_fast_contextual_names            # dry-run
    python -m scripts.26b_fast_contextual_names --apply     # real
"""
from __future__ import annotations

import argparse
import logging
from time import perf_counter

from src.db.conn import db_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("phase2.step26b")

GARBAGE_NAMES = [
    '(sin nombre)', 'SN', 'sn', 'S/N', 's/n',
    'sin nombre', 'Sin Nombre',
    'Parada', 'parada', 'PARADA',
    'Parada Sin Nombre', 'parada sin nombre',
    'Stop', 'stop', 'STOP',
    'Bus Stop', 'bus stop',
    'Unnamed', 'unnamed',
    'N/A', 'n/a', 'NA', 'na',
    'unknown', 'Unknown', 'UNKNOWN',
    'Puente', 'puente',
    'La y', 'la y', 'La Y',
    'Esquina', 'esquina',
    'Cruce', 'cruce',
]

# Cascade 1: Recover name from node_prod tags
_SQL_TAG_RECOVER = """
WITH recoverable AS (
    SELECT DISTINCT ON (p.place_id)
        p.place_id,
        COALESCE(
            NULLIF(n.chosen_tags->>'name', ''),
            NULLIF(n.chosen_tags->>'name:es', ''),
            NULLIF(n.chosen_tags->>'official_name', ''),
            NULLIF(n.chosen_tags->>'short_name', ''),
            NULLIF(n.chosen_tags->>'alt_name', ''),
            NULLIF(n.chosen_tags->>'loc_name', '')
        ) AS recovered_name
    FROM geo_prod.places p
    JOIN geo_prod.node_place_map npm ON npm.place_id = p.place_id
    JOIN node_prod.nodes n ON n.node_id = npm.node_id
    WHERE p.status = 'active'
      AND p.canonical_name = ANY(%(garbage_names)s)
      AND COALESCE(
          NULLIF(n.chosen_tags->>'name', ''),
          NULLIF(n.chosen_tags->>'name:es', ''),
          NULLIF(n.chosen_tags->>'official_name', ''),
          NULLIF(n.chosen_tags->>'short_name', ''),
          NULLIF(n.chosen_tags->>'alt_name', ''),
          NULLIF(n.chosen_tags->>'loc_name', '')
      ) IS NOT NULL
      AND COALESCE(
          NULLIF(n.chosen_tags->>'name', ''),
          NULLIF(n.chosen_tags->>'name:es', ''),
          NULLIF(n.chosen_tags->>'official_name', ''),
          NULLIF(n.chosen_tags->>'short_name', ''),
          NULLIF(n.chosen_tags->>'alt_name', ''),
          NULLIF(n.chosen_tags->>'loc_name', '')
      ) NOT IN (SELECT unnest(%(garbage_names)s))
    ORDER BY p.place_id, n.confidence DESC NULLS LAST
)
UPDATE geo_prod.places p
SET canonical_name = r.recovered_name,
    updated_at = now()
FROM recoverable r
WHERE p.place_id = r.place_id
  AND p.status = 'active'
"""

# Cascade 2: Proximity — nearest named non-garbage place
# Adds a sequence number to disambiguate duplicates within the same reference
_SQL_PROXIMITY_RENAME = """
WITH garbage_places AS (
    SELECT place_id, geom
    FROM geo_prod.places
    WHERE status = 'active'
      AND geom IS NOT NULL
      AND canonical_name = ANY(%(garbage_names)s)
),
nearest_named AS (
    SELECT DISTINCT ON (gp.place_id)
        gp.place_id,
        p2.canonical_name AS ref_name,
        ST_Distance(gp.geom::geography, p2.geom::geography) AS dist_m
    FROM garbage_places gp
    CROSS JOIN LATERAL (
        SELECT p2.canonical_name, p2.geom
        FROM geo_prod.places p2
        WHERE p2.status = 'active'
          AND p2.canonical_name NOT IN (SELECT unnest(%(garbage_names)s))
          AND LENGTH(p2.canonical_name) > 2
          AND p2.geom IS NOT NULL
          AND p2.geom && ST_Expand(gp.geom, 0.008)
          AND ST_DWithin(gp.geom::geography, p2.geom::geography, 800)
          AND p2.place_id != gp.place_id
        ORDER BY gp.geom <-> p2.geom
        LIMIT 1
    ) p2
),
numbered AS (
    SELECT place_id, ref_name,
           ref_name || ' - P' || ROW_NUMBER() OVER (PARTITION BY ref_name ORDER BY place_id) AS new_name
    FROM nearest_named
)
UPDATE geo_prod.places p
SET canonical_name = n.new_name,
    updated_at = now()
FROM numbered n
WHERE p.place_id = n.place_id
  AND p.status = 'active'
"""

# Cascade 3: Geohash sector fallback
_SQL_SECTOR_RENAME = """
WITH still_garbage AS (
    SELECT p.place_id, p.geom,
           ST_GeoHash(p.geom, 6) AS gh6
    FROM geo_prod.places p
    WHERE p.status = 'active'
      AND p.geom IS NOT NULL
      AND p.canonical_name = ANY(%(garbage_names)s)
),
numbered AS (
    SELECT place_id, gh6,
           'Sector ' || gh6 || '-' || ROW_NUMBER() OVER (PARTITION BY gh6 ORDER BY place_id) AS new_name
    FROM still_garbage
)
UPDATE geo_prod.places p
SET canonical_name = n.new_name,
    updated_at = now()
FROM numbered n
WHERE p.place_id = n.place_id
  AND p.status = 'active'
"""

# Cascade 4: final fallback for places without geom
_SQL_UUID_FALLBACK = """
UPDATE geo_prod.places p
SET canonical_name = 'Parada ' || LEFT(p.place_id::text, 8),
    updated_at = now()
WHERE p.status = 'active'
  AND p.canonical_name = ANY(%(garbage_names)s)
"""


def main():
    parser = argparse.ArgumentParser(description="Step 26b — Fast Batch Contextual Names")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    dry_run = not args.apply
    mode = "DRY RUN" if dry_run else "APPLY"

    print(f"\n{'='*60}")
    print(f"  Step 26b — Fast Batch Contextual Names [{mode}]")
    print(f"{'='*60}\n")

    t0 = perf_counter()
    params = {"garbage_names": GARBAGE_NAMES}

    with db_conn() as conn:
        # Count garbage
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM geo_prod.places WHERE status = 'active' AND canonical_name = ANY(%s)",
                (GARBAGE_NAMES,),
            )
            total_garbage = cur.fetchone()["cnt"]
        print(f"Total garbage-named places: {total_garbage:,}")

        if total_garbage == 0:
            print("No garbage names to fix!")
            return

        if dry_run:
            print("\n[DRY RUN] Would process with 3-tier cascade. Use --apply to execute.")
            elapsed = perf_counter() - t0
            print(f"Elapsed: {elapsed:.1f}s\n")
            return

        # ── CASCADE 1: Recover from node tags ──
        print("\n--- Cascade 1: Tag recovery (name from node_prod.chosen_tags) ---")
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.changed_by = 'step26b_tag_recovery'")
            cur.execute("SET LOCAL app.change_reason = 'Recover name from node OSM tags'")
            cur.execute(_SQL_TAG_RECOVER, params)
            tag_recovered = cur.rowcount
        conn.commit()
        print(f"  Tag recovered: {tag_recovered:,}")

        # ── CASCADE 2: Proximity naming ──
        print("\n--- Cascade 2: Proximity naming (NearestPlace - PN) ---")
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.changed_by = 'step26b_proximity'")
            cur.execute("SET LOCAL app.change_reason = 'Proximity-based name from nearest named place'")
            cur.execute(_SQL_PROXIMITY_RENAME, params)
            proximity_renamed = cur.rowcount
        conn.commit()
        print(f"  Proximity renamed: {proximity_renamed:,}")

        # ── CASCADE 3: Sector fallback ──
        print("\n--- Cascade 3: Sector fallback (Sector gh6-N) ---")
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.changed_by = 'step26b_sector'")
            cur.execute("SET LOCAL app.change_reason = 'Sector-based name fallback'")
            cur.execute(_SQL_SECTOR_RENAME, params)
            sector_renamed = cur.rowcount
        conn.commit()
        print(f"  Sector renamed: {sector_renamed:,}")

        # ── CASCADE 4: UUID fallback ──
        print("\n--- Cascade 4: UUID fallback (Parada xxxxxxxx) ---")
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.changed_by = 'step26b_uuid_fallback'")
            cur.execute("SET LOCAL app.change_reason = 'UUID-based name final fallback'")
            cur.execute(_SQL_UUID_FALLBACK, params)
            uuid_renamed = cur.rowcount
        conn.commit()
        print(f"  UUID fallback: {uuid_renamed:,}")

        # ── Final check ──
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM geo_prod.places WHERE status = 'active' AND canonical_name = ANY(%s)",
                (GARBAGE_NAMES,),
            )
            remaining = cur.fetchone()["cnt"]

    elapsed = perf_counter() - t0

    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"  Tag recovery:  {tag_recovered:>8,}")
    print(f"  Proximity:     {proximity_renamed:>8,}")
    print(f"  Sector:        {sector_renamed:>8,}")
    print(f"  UUID fallback: {uuid_renamed:>8,}")
    total = tag_recovered + proximity_renamed + sector_renamed + uuid_renamed
    print(f"  Total renamed: {total:>8,}")
    print(f"  Still garbage: {remaining:>8,}")
    print(f"  Elapsed:       {elapsed:.1f}s")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
