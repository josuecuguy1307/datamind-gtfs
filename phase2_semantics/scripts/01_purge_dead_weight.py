"""
Block 1 — Purge Dead Weight (Safe Deletes)

Removes deprecated places, never-approved candidate sets, and active orphans.
All operations are idempotent and safe to re-run.

Usage:
    python -m scripts.01_purge_dead_weight --dry-run
    python -m scripts.01_purge_dead_weight --apply
"""
from __future__ import annotations

import argparse
import sys
from time import perf_counter

from src.db.conn import db_conn


def _count(conn, sql: str, params=None) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()["count"]


def purge_deprecated_places(conn, *, dry_run: bool) -> dict:
    """Delete all deprecated places. CASCADE handles aliases + embeddings."""
    t0 = perf_counter()

    n_deprecated = _count(conn, "SELECT COUNT(*) FROM geo_prod.places WHERE status = 'deprecated'")
    n_dep_aliases = _count(
        conn,
        """SELECT COUNT(*) FROM geo_prod.place_aliases a
           JOIN geo_prod.places p ON p.place_id = a.place_id
           WHERE p.status = 'deprecated'""",
    )
    n_dep_embeddings = _count(
        conn,
        """SELECT COUNT(*) FROM geo_prod.place_alias_embeddings e
           JOIN geo_prod.place_aliases a ON a.alias_id = e.alias_id
           JOIN geo_prod.places p ON p.place_id = a.place_id
           WHERE p.status = 'deprecated'""",
    )

    # Pre-check: verify no node_place_map entries for deprecated places
    n_dep_mappings = _count(
        conn,
        """SELECT COUNT(*) FROM geo_prod.node_place_map m
           JOIN geo_prod.places p ON p.place_id = m.place_id
           WHERE p.status = 'deprecated'""",
    )

    # Check Phase 4 references
    n_phase4_refs = 0
    try:
        n_phase4_refs = _count(
            conn,
            """SELECT COUNT(*) FROM semantics.route_name_evidence rne
               JOIN geo_prod.places p ON rne.place_id = p.place_id
               WHERE p.status = 'deprecated'""",
        )
    except Exception:
        pass  # Phase 4 table may not exist

    result = {
        "deprecated_places": n_deprecated,
        "deprecated_aliases_cascade": n_dep_aliases,
        "deprecated_embeddings_cascade": n_dep_embeddings,
        "deprecated_with_node_mappings": n_dep_mappings,
        "phase4_references": n_phase4_refs,
        "est_freed_mb": round(n_dep_embeddings * 1536 / 1024 / 1024, 1),
        "dry_run": dry_run,
        "deleted": 0,
    }

    if n_dep_mappings > 0:
        # node_place_map has ON DELETE RESTRICT — must remove mappings first
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM geo_prod.node_place_map
                       WHERE place_id IN (
                           SELECT place_id FROM geo_prod.places WHERE status = 'deprecated'
                       )"""
                )
                result["node_mappings_removed"] = cur.rowcount

    if not dry_run and n_deprecated > 0:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM geo_prod.places WHERE status = 'deprecated'")
            result["deleted"] = cur.rowcount

    result["elapsed_ms"] = round((perf_counter() - t0) * 1000)
    return result


def purge_stale_candidate_sets(conn, *, dry_run: bool) -> dict:
    """Delete candidate sets that were never approved."""
    t0 = perf_counter()

    with conn.cursor() as cur:
        cur.execute(
            """SELECT pcs.place_set_id::text, pcs.context_key, pcs.created_at::text
               FROM geo_work.place_candidate_sets pcs
               WHERE pcs.place_set_id NOT IN (
                   SELECT DISTINCT chosen_set_id FROM geo_work.selection_log
                   WHERE chosen_set_id IS NOT NULL
               )
               ORDER BY pcs.created_at"""
        )
        stale_sets = [dict(r) for r in cur.fetchall()]

    stale_ids = [s["place_set_id"] for s in stale_sets]

    n_candidates = 0
    n_alias_candidates = 0
    if stale_ids:
        n_candidates = _count(
            conn,
            "SELECT COUNT(*) FROM geo_work.place_candidates WHERE place_set_id = ANY(%s::uuid[])",
            (stale_ids,),
        )
        n_alias_candidates = _count(
            conn,
            """SELECT COUNT(*) FROM geo_work.alias_candidates ac
               JOIN geo_work.place_candidates pc ON pc.place_candidate_id = ac.place_candidate_id
               WHERE pc.place_set_id = ANY(%s::uuid[])""",
            (stale_ids,),
        )

    result = {
        "stale_sets": len(stale_ids),
        "stale_candidates": n_candidates,
        "stale_alias_candidates": n_alias_candidates,
        "set_details": stale_sets[:10],
        "dry_run": dry_run,
        "deleted_sets": 0,
    }

    if not dry_run and stale_ids:
        with conn.cursor() as cur:
            # Also clean any rejected_set_ids arrays referencing these sets
            for sid in stale_ids:
                cur.execute(
                    """UPDATE geo_work.selection_log
                       SET rejected_set_ids = array_remove(rejected_set_ids, %s::uuid)
                       WHERE %s::uuid = ANY(rejected_set_ids)""",
                    (sid, sid),
                )
            # CASCADE handles place_candidates, alias_candidates, name_candidates, feedback
            cur.execute(
                "DELETE FROM geo_work.place_candidate_sets WHERE place_set_id = ANY(%s::uuid[])",
                (stale_ids,),
            )
            result["deleted_sets"] = cur.rowcount

    result["elapsed_ms"] = round((perf_counter() - t0) * 1000)
    return result


def deprecate_active_orphans(conn, *, dry_run: bool) -> dict:
    """Set status='deprecated' for active places with no node mapping."""
    t0 = perf_counter()

    with conn.cursor() as cur:
        cur.execute(
            """SELECT p.place_id::text, p.canonical_name, p.place_type
               FROM geo_prod.places p
               LEFT JOIN geo_prod.node_place_map m ON m.place_id = p.place_id
               WHERE p.status = 'active' AND m.place_id IS NULL
               LIMIT 100"""
        )
        samples = [dict(r) for r in cur.fetchall()]

    n_orphans = _count(
        conn,
        """SELECT COUNT(*) FROM geo_prod.places p
           LEFT JOIN geo_prod.node_place_map m ON m.place_id = p.place_id
           WHERE p.status = 'active' AND m.place_id IS NULL""",
    )

    result = {
        "active_orphans": n_orphans,
        "samples": samples[:20],
        "dry_run": dry_run,
        "deprecated": 0,
    }

    if not dry_run and n_orphans > 0:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE geo_prod.places
                   SET status = 'deprecated', updated_at = now()
                   WHERE status = 'active'
                     AND place_id NOT IN (SELECT place_id FROM geo_prod.node_place_map)"""
            )
            result["deprecated"] = cur.rowcount

    result["elapsed_ms"] = round((perf_counter() - t0) * 1000)
    return result


def main():
    parser = argparse.ArgumentParser(description="Phase 2 — Purge Dead Weight")
    parser.add_argument("--apply", action="store_true", help="Execute real deletes (default is dry-run)")
    parser.add_argument("--skip-deprecated", action="store_true")
    parser.add_argument("--skip-stale-sets", action="store_true")
    parser.add_argument("--skip-orphans", action="store_true")
    args = parser.parse_args()

    dry_run = not args.apply
    mode = "DRY RUN" if dry_run else "APPLY"
    print(f"\n{'='*60}")
    print(f"  Phase 2 — Purge Dead Weight [{mode}]")
    print(f"{'='*60}\n")

    with db_conn() as conn:
        # 1. Deprecated places
        if not args.skip_deprecated:
            print("--- 1.1 Deprecated Places ---")
            r = purge_deprecated_places(conn, dry_run=dry_run)
            print(f"  Deprecated places:    {r['deprecated_places']:>10,}")
            print(f"  Aliases (cascade):    {r['deprecated_aliases_cascade']:>10,}")
            print(f"  Embeddings (cascade): {r['deprecated_embeddings_cascade']:>10,}")
            print(f"  Est. freed:           {r['est_freed_mb']:>10.1f} MB")
            print(f"  Node mappings:        {r['deprecated_with_node_mappings']:>10,}")
            print(f"  Phase 4 refs:         {r['phase4_references']:>10,}")
            if not dry_run:
                print(f"  DELETED:              {r['deleted']:>10,}")
            print(f"  Elapsed: {r['elapsed_ms']}ms\n")
            if not dry_run:
                conn.commit()

        # 2. Stale candidate sets
        if not args.skip_stale_sets:
            print("--- 1.2 Never-Approved Candidate Sets ---")
            r = purge_stale_candidate_sets(conn, dry_run=dry_run)
            print(f"  Stale sets:           {r['stale_sets']:>10,}")
            print(f"  Candidates:           {r['stale_candidates']:>10,}")
            print(f"  Alias candidates:     {r['stale_alias_candidates']:>10,}")
            if not dry_run:
                print(f"  DELETED sets:         {r['deleted_sets']:>10,}")
            print(f"  Elapsed: {r['elapsed_ms']}ms\n")
            if not dry_run:
                conn.commit()

        # 3. Active orphans
        if not args.skip_orphans:
            print("--- 1.3 Active Orphaned Places ---")
            r = deprecate_active_orphans(conn, dry_run=dry_run)
            print(f"  Active orphans:       {r['active_orphans']:>10,}")
            if not dry_run:
                print(f"  DEPRECATED:           {r['deprecated']:>10,}")
            print(f"  Elapsed: {r['elapsed_ms']}ms\n")
            if not dry_run:
                conn.commit()

    print("Done.\n")


if __name__ == "__main__":
    main()
