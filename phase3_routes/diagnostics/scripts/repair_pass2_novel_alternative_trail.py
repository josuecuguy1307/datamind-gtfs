#!/usr/bin/env python3
"""
Second-pass repair for Phase 3 extractor-review rows (LOCAL DB only).

Problem:
  After pass 1 (relation-based splits), the remaining major audit distortion
  comes from 'novel_alternative_selected' rows. These are rows where:
    - The extractor originally discovered relation X
    - Found X was already stored (duplicate)
    - Selected a novel alternative Y instead
    - Updated the column chosen_osm_relation_id to Y
    - BUT the attempt_history entry still records chosen_osm_relation_id = X
      (the original discovery, not the final selection)

  This causes the audit to count these as 'direct_relation_mismatch' rows,
  inflating mismatch metrics (e.g., 32 out of 41 codex rows appear as mismatches
  when they are actually correctly processed novel-alternative selections).

Strategy:
  CLASS A — Novel-alternative attempt trail enrichment:
    For each novel_alternative_selected attempt_history entry where
    attempt.chosen_osm_relation_id != host.chosen_osm_relation_id:
      - Preserve the original as 'discovered_osm_relation_id'
      - Set 'chosen_osm_relation_id' to match the host column (the actual final selection)
      - Add repair provenance trail

  CLASS B — Same-relation suspicious merges:
    NOT repaired. These are true same-relation duplicates where different query
    strategies found the same OSM route. Splitting them would create noise.

Safety:
  - Only modifies attempt_history metadata, not structural row data
  - Preserves original discovery in 'discovered_osm_relation_id'
  - No row creation, no deletion
  - Supports --dry-run (default) and --apply

Usage:
  export DB_DSN="$LOCAL_DB_DSN"
  python3 phase3_routes/diagnostics/scripts/repair_pass2_novel_alternative_trail.py --dry-run
  python3 phase3_routes/diagnostics/scripts/repair_pass2_novel_alternative_trail.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _source_name(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return Path(text).name if text else None


def fetch_novel_alternative_rows(dsn: str) -> List[Dict[str, Any]]:
    """Fetch rows with novel_alternative_selected novelty status and attempt_history."""
    sql = """
        SELECT
          rj.route_id::text AS route_id,
          rj.chosen_osm_relation_id,
          rj.extractor_source,
          rj.extractor_review,
          rj.status
        FROM route_raw.route_jobs rj
        WHERE rj.extractor_review IS NOT NULL
          AND rj.chosen_osm_relation_id IS NOT NULL
          AND jsonb_array_length(
                COALESCE(rj.extractor_review->'attempt_history', '[]'::jsonb)
              ) >= 1
          AND (
            rj.extractor_review->'dedupe'->>'novelty_status' = 'novel_alternative_selected'
            OR rj.extractor_review->'discover'->>'novelty_status' = 'novel_alternative_selected'
          )
        ORDER BY rj.extractor_source, rj.created_at
    """
    with psycopg2.connect(dsn) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(row) for row in (cur.fetchall() or [])]


def analyze_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Check if this row has attempt_history entries whose chosen_osm_relation_id
    differs from the host column. If so, return a repair plan.
    """
    host_rel = _to_int(row["chosen_osm_relation_id"])
    review = dict(row.get("extractor_review") or {})
    history = list(review.get("attempt_history") or [])

    if not history or host_rel is None:
        return None

    mismatched_indices = []
    for idx, attempt in enumerate(history):
        attempt_rel = _to_int(attempt.get("chosen_osm_relation_id"))
        novelty = str(attempt.get("novelty_status") or "")
        if (
            attempt_rel is not None
            and attempt_rel != host_rel
            and "novel_alternative" in novelty
        ):
            mismatched_indices.append(idx)

    if not mismatched_indices:
        return None

    return {
        "route_id": str(row["route_id"]),
        "host_rel": host_rel,
        "extractor_source": _source_name(row.get("extractor_source")),
        "mismatched_indices": mismatched_indices,
        "total_attempts": len(history),
        "mismatched_relations": sorted({
            int(history[i]["chosen_osm_relation_id"])
            for i in mismatched_indices
        }),
    }


def apply_repair(dsn: str, candidates: List[Dict[str, Any]], *, apply: bool = False) -> List[Dict[str, Any]]:
    """Apply or dry-run the novel-alternative trail enrichment."""
    actions: List[Dict[str, Any]] = []

    if not apply:
        for cand in candidates:
            actions.append({
                "action": "DRY_RUN_WOULD_ENRICH",
                "route_id": cand["route_id"],
                "host_rel": cand["host_rel"],
                "extractor_source": cand["extractor_source"],
                "mismatched_attempt_count": len(cand["mismatched_indices"]),
                "total_attempts": cand["total_attempts"],
                "mismatched_relations": cand["mismatched_relations"],
            })
        return actions

    with psycopg2.connect(dsn) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for cand in candidates:
                # Re-fetch current review to avoid stale data
                cur.execute(
                    "SELECT extractor_review FROM route_raw.route_jobs WHERE route_id = %s::uuid",
                    (cand["route_id"],),
                )
                row = cur.fetchone()
                if not row:
                    continue

                review = dict(row["extractor_review"] or {})
                history = list(review.get("attempt_history") or [])
                host_rel = cand["host_rel"]
                enriched_count = 0

                for idx in cand["mismatched_indices"]:
                    if idx >= len(history):
                        continue
                    attempt = history[idx]
                    original_rel = _to_int(attempt.get("chosen_osm_relation_id"))
                    if original_rel is None or original_rel == host_rel:
                        continue
                    # Already enriched?
                    if attempt.get("discovered_osm_relation_id") is not None:
                        continue

                    # Enrich: preserve original, update to final
                    attempt["discovered_osm_relation_id"] = original_rel
                    attempt["chosen_osm_relation_id"] = host_rel
                    enriched_count += 1

                if enriched_count == 0:
                    continue

                # Add repair trail
                review["attempt_history"] = history
                review.setdefault("repair_history", []).append({
                    "repair_type": "novel_alternative_trail_enrichment",
                    "repair_pass": 2,
                    "enriched_attempt_count": enriched_count,
                    "host_chosen_osm_relation_id": host_rel,
                    "original_discovered_relations": cand["mismatched_relations"],
                })

                cur.execute(
                    """
                    UPDATE route_raw.route_jobs
                    SET extractor_review = %s::jsonb
                    WHERE route_id = %s::uuid
                    """,
                    (
                        json.dumps(review, ensure_ascii=False, default=str),
                        cand["route_id"],
                    ),
                )

                actions.append({
                    "action": "ENRICHED_ATTEMPTS",
                    "route_id": cand["route_id"],
                    "host_rel": host_rel,
                    "extractor_source": cand["extractor_source"],
                    "enriched_count": enriched_count,
                    "original_relations": cand["mismatched_relations"],
                })

    return actions


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pass-2 repair: enrich novel-alternative attempt trails."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    parser.add_argument("--json-output", type=str, default=None)
    args = parser.parse_args(argv)

    dsn = (os.environ.get("DB_DSN") or "").strip()
    if not dsn:
        print("ERROR: DB_DSN not set.", file=sys.stderr)
        return 2
    if "127.0.0.1" not in dsn and "localhost" not in dsn:
        print("ERROR: DB_DSN does not point to local DB.", file=sys.stderr)
        return 2

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print(f"Mode: {mode}")
    print(f"DB target: local (verified 127.0.0.1)")

    rows = fetch_novel_alternative_rows(dsn)
    print(f"Novel-alternative-selected rows: {len(rows)}")

    candidates = [c for c in (analyze_row(r) for r in rows) if c is not None]
    print(f"Rows with mismatched attempt trails: {len(candidates)}")

    by_source: Dict[str, int] = {}
    for c in candidates:
        src = c["extractor_source"] or "unknown"
        by_source[src] = by_source.get(src, 0) + 1
    for src, count in sorted(by_source.items()):
        print(f"  {src}: {count} rows")

    total_mismatched_attempts = sum(len(c["mismatched_indices"]) for c in candidates)
    print(f"Total attempt entries to enrich: {total_mismatched_attempts}")

    actions = apply_repair(dsn, candidates, apply=args.apply)

    print(f"\n{'='*60}")
    print(f"REPAIR SUMMARY ({mode})")
    print(f"{'='*60}")
    print(f"Rows analyzed: {len(rows)}")
    print(f"Rows with mismatches: {len(candidates)}")
    print(f"Attempt entries {'enriched' if args.apply else 'to enrich'}: {total_mismatched_attempts}")
    print(f"Actions: {len(actions)}")
    for action in actions:
        print(f"  {json.dumps(action, ensure_ascii=False, default=str)}")

    if args.json_output:
        output_path = Path(args.json_output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": mode.lower().replace(" ", "_"),
            "rows_analyzed": len(rows),
            "rows_with_mismatches": len(candidates),
            "total_attempts_enriched": total_mismatched_attempts,
            "by_source": by_source,
            "actions": actions,
        }
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\njson_output: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
