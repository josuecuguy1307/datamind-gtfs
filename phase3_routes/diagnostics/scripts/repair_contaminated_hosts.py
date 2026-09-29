#!/usr/bin/env python3
"""
One-off repair script for historically contaminated Phase 3 extractor-review hosts.

Strategy: SPLIT-BY-RELATION
  For each multi-attempt host whose attempt_history contains attempts that chose
  a DIFFERENT osm_relation_id than the host's current chosen_osm_relation_id,
  create new independent route_jobs rows — one per distinct (relation_id, source_document)
  pair that diverges from the host.

Safety:
  - Does NOT delete or modify existing host rows (except trimming their attempt_history
    to remove the migrated attempts, to avoid double-counting in the audit).
  - Each new row carries full provenance (split_from_host, original attempt payload).
  - Supports --dry-run (default) and --apply modes.
  - Targets LOCAL DB only (DB_DSN env var must be set).
  - Conservative: only splits attempts whose chosen_osm_relation_id differs from host.

Usage:
  export DB_DSN="$LOCAL_DB_DSN"
  python3 phase3_routes/diagnostics/scripts/repair_contaminated_hosts.py --dry-run
  python3 phase3_routes/diagnostics/scripts/repair_contaminated_hosts.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _clean_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _source_name(value: Any) -> Optional[str]:
    text = _clean_text(value)
    return Path(text).name if text else None


def _route_hint_signature(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text)
    parts = [
        re.sub(r"[^a-z0-9]+", " ", part.lower()).strip()
        for part in re.split(r"\s*(?:-|/|>| to | a )\s*", cleaned, flags=re.IGNORECASE)
    ]
    parts = [p for p in parts if p]
    if len(parts) >= 2:
        return f"{parts[0]} -> {parts[-1]}"
    return parts[0] if parts else None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SplitCandidate:
    """A group of attempts inside a contaminated host that should become a new row."""
    host_route_id: str
    host_relation_id: Optional[int]
    host_extractor_source: Optional[str]
    split_relation_id: int
    split_source_document: Optional[str]
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    distinct_places: List[str] = field(default_factory=list)
    distinct_hints: List[str] = field(default_factory=list)
    distinct_hint_signatures: List[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class RepairPlan:
    host_route_id: str
    host_relation_id: Optional[int]
    host_extractor_source: Optional[str]
    total_attempts: int
    kept_attempts: int
    splits: List[SplitCandidate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def fetch_contaminated_hosts(dsn: str, *, min_history: int = 2) -> List[Dict[str, Any]]:
    """Fetch multi-attempt hosts from local DB."""
    sql = """
        SELECT
          rj.route_id::text AS route_id,
          rj.extractor_source,
          rj.chosen_osm_relation_id,
          rj.status,
          rj.extractor_review,
          rj.bbox,
          rj.known_ref,
          rj.area_key,
          rj.notes
        FROM route_raw.route_jobs rj
        WHERE rj.extractor_review IS NOT NULL
          AND jsonb_array_length(
                COALESCE(rj.extractor_review->'attempt_history', '[]'::jsonb)
              ) >= %s
        ORDER BY jsonb_array_length(
                   COALESCE(rj.extractor_review->'attempt_history', '[]'::jsonb)
                 ) DESC
    """
    with psycopg2.connect(dsn) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (min_history,))
            return [dict(row) for row in (cur.fetchall() or [])]


def build_repair_plan(host: Dict[str, Any]) -> Optional[RepairPlan]:
    """
    Analyze a single host and decide which attempts to split out.

    Rule: split attempts whose chosen_osm_relation_id differs from the host's
    current chosen_osm_relation_id. Group splits by (relation_id, source_document_name).
    """
    review = dict(host.get("extractor_review") or {})
    history = list(review.get("attempt_history") or [])
    host_relation = _to_int(host.get("chosen_osm_relation_id"))

    if not history or host_relation is None:
        return None

    # Partition attempts: those matching host relation vs those diverging
    kept: List[Dict[str, Any]] = []
    divergent: List[Dict[str, Any]] = []

    for attempt in history:
        attempt_relation = _to_int(attempt.get("chosen_osm_relation_id"))
        if attempt_relation is None or attempt_relation == host_relation:
            kept.append(attempt)
        else:
            divergent.append(attempt)

    if not divergent:
        return None

    # Group divergent attempts by (relation_id, source_document_name)
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for attempt in divergent:
        rel_id = int(attempt["chosen_osm_relation_id"])
        source = _source_name(attempt.get("source_document")) or "unknown"
        key = (rel_id, source)
        groups.setdefault(key, []).append(attempt)

    splits: List[SplitCandidate] = []
    for (rel_id, source), attempts in sorted(groups.items()):
        places = sorted({
            str(a.get("place") or "").strip()
            for a in attempts if _clean_text(a.get("place"))
        })
        hints = sorted({
            str(a.get("route_hint_raw") or "").strip()
            for a in attempts if _clean_text(a.get("route_hint_raw"))
        })
        hint_sigs = sorted({
            sig for sig in (
                _route_hint_signature(a.get("route_hint_raw"))
                for a in attempts
            ) if sig
        })

        reasons = []
        reasons.append(f"attempt_relation={rel_id} != host_relation={host_relation}")
        if len(places) > 0:
            reasons.append(f"places={places}")
        if len(hint_sigs) > 0:
            reasons.append(f"hint_signatures={hint_sigs}")

        splits.append(SplitCandidate(
            host_route_id=str(host["route_id"]),
            host_relation_id=host_relation,
            host_extractor_source=_source_name(host.get("extractor_source")),
            split_relation_id=rel_id,
            split_source_document=source,
            attempts=attempts,
            distinct_places=places,
            distinct_hints=hints,
            distinct_hint_signatures=hint_sigs,
            reason="; ".join(reasons),
        ))

    return RepairPlan(
        host_route_id=str(host["route_id"]),
        host_relation_id=host_relation,
        host_extractor_source=_source_name(host.get("extractor_source")),
        total_attempts=len(history),
        kept_attempts=len(kept),
        splits=splits,
    )


def _build_split_row_review(
    split: SplitCandidate,
    representative_attempt: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the extractor_review JSONB for a newly split row."""
    return {
        "source_document": representative_attempt.get("source_document"),
        "batch_id": representative_attempt.get("batch_id"),
        "target": {
            "place": representative_attempt.get("place"),
            "group": representative_attempt.get("group"),
            "priority": representative_attempt.get("priority"),
            "place_bundle": representative_attempt.get("place_bundle"),
            "seed_origin": representative_attempt.get("seed_origin") or "repair_split",
            "attempt_type": representative_attempt.get("attempt_type"),
        },
        "geography": {
            "place_input": representative_attempt.get("place"),
            "bbox_used": representative_attempt.get("bbox_used"),
            "interpretation_source": representative_attempt.get("interpretation_source"),
        },
        "hints": {
            "route_hint_raw": representative_attempt.get("route_hint_raw"),
            "cooperative_hint": representative_attempt.get("cooperative_hint"),
        },
        "discover": {
            "chosen_osm_relation_id": split.split_relation_id,
            "relation_extraction_success": True,
            "novelty_status": "repair_split_from_contaminated_host",
            "candidate_preview": [],
        },
        "fetch": {
            "fetch_relation_stored": False,
            "fetch_status": "not_fetched_split_row",
        },
        "dedupe": {
            "novelty_status": "repair_split_from_contaminated_host",
        },
        "attempt_history": split.attempts,
        "repair_provenance": {
            "repair_type": "split_by_relation",
            "split_from_host_route_id": split.host_route_id,
            "host_chosen_osm_relation_id": split.host_relation_id,
            "split_relation_id": split.split_relation_id,
            "split_source_document": split.split_source_document,
            "attempt_count_migrated": len(split.attempts),
            "reason": split.reason,
        },
    }


def execute_repair(
    dsn: str,
    plans: List[RepairPlan],
    *,
    apply: bool = False,
) -> List[Dict[str, Any]]:
    """
    Execute the repair: create new split rows and trim host attempt_history.

    Returns a summary of actions taken.
    """
    actions: List[Dict[str, Any]] = []

    if not apply:
        for plan in plans:
            for split in plan.splits:
                actions.append({
                    "action": "DRY_RUN_WOULD_CREATE",
                    "host_route_id": plan.host_route_id,
                    "host_relation_id": plan.host_relation_id,
                    "split_relation_id": split.split_relation_id,
                    "split_source": split.split_source_document,
                    "attempt_count": len(split.attempts),
                    "places": split.distinct_places,
                    "hints": split.distinct_hints,
                    "reason": split.reason,
                })
            if plan.splits:
                actions.append({
                    "action": "DRY_RUN_WOULD_TRIM_HOST",
                    "host_route_id": plan.host_route_id,
                    "original_attempts": plan.total_attempts,
                    "kept_attempts": plan.kept_attempts,
                    "removed_attempts": plan.total_attempts - plan.kept_attempts,
                })
        return actions

    # Real apply
    with psycopg2.connect(dsn) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for plan in plans:
                if not plan.splits:
                    continue

                # Collect all divergent attempt_keys to remove from host
                migrated_attempt_keys = set()

                for split in plan.splits:
                    # Pick the best representative attempt (highest confidence)
                    attempts_sorted = sorted(
                        split.attempts,
                        key=lambda a: -float(a.get("selection_confidence") or 0.0),
                    )
                    representative = attempts_sorted[0]

                    new_route_id = uuid.uuid4()
                    review_payload = _build_split_row_review(split, representative)
                    source_name = split.split_source_document or representative.get("source_document")

                    # Determine bbox from representative attempt
                    bbox_used = representative.get("bbox_used")
                    bbox_json = json.dumps(bbox_used, ensure_ascii=False) if bbox_used else None

                    cur.execute(
                        """
                        INSERT INTO route_raw.route_jobs
                          (route_id, status, extractor_source, chosen_osm_relation_id,
                           extractor_review, bbox, notes, created_by)
                        VALUES
                          (%s, 'new', %s, %s, %s::jsonb, %s::jsonb,
                           %s, 'repair_contaminated_hosts')
                        RETURNING route_id::text AS route_id
                        """,
                        (
                            str(new_route_id),
                            _source_name(source_name),
                            split.split_relation_id,
                            json.dumps(review_payload, ensure_ascii=False, default=str),
                            bbox_json,
                            f"Split from contaminated host {plan.host_route_id[:12]}... "
                            f"(relation {split.split_relation_id} != host {plan.host_relation_id})",
                        ),
                    )
                    created = cur.fetchone()

                    for attempt in split.attempts:
                        ak = attempt.get("attempt_key")
                        if ak:
                            migrated_attempt_keys.add(ak)

                    actions.append({
                        "action": "CREATED_SPLIT_ROW",
                        "new_route_id": str(created["route_id"]) if created else str(new_route_id),
                        "host_route_id": plan.host_route_id,
                        "host_relation_id": plan.host_relation_id,
                        "split_relation_id": split.split_relation_id,
                        "split_source": split.split_source_document,
                        "attempt_count": len(split.attempts),
                        "places": split.distinct_places,
                        "reason": split.reason,
                    })

                # Trim host attempt_history: remove migrated attempts
                if migrated_attempt_keys:
                    cur.execute(
                        """
                        SELECT extractor_review FROM route_raw.route_jobs
                        WHERE route_id = %s::uuid
                        """,
                        (plan.host_route_id,),
                    )
                    host_row = cur.fetchone()
                    if host_row:
                        current_review = dict(host_row["extractor_review"] or {})
                        current_history = list(current_review.get("attempt_history") or [])
                        trimmed_history = [
                            a for a in current_history
                            if a.get("attempt_key") not in migrated_attempt_keys
                        ]
                        # Also keep attempts without attempt_key that match host relation
                        kept_no_key = [
                            a for a in current_history
                            if not a.get("attempt_key")
                            and _to_int(a.get("chosen_osm_relation_id")) == plan.host_relation_id
                        ]
                        # Combine: keyed non-migrated + keyless matching host
                        final_history = [
                            a for a in trimmed_history if a.get("attempt_key")
                        ] + kept_no_key

                        # If trimming removed everything, keep at least keyless attempts
                        if not final_history and current_history:
                            final_history = [
                                a for a in current_history
                                if not a.get("attempt_key")
                                or _to_int(a.get("chosen_osm_relation_id")) == plan.host_relation_id
                            ]

                        current_review["attempt_history"] = final_history
                        # Add repair audit trail to host
                        current_review.setdefault("repair_history", []).append({
                            "repair_type": "trim_migrated_attempts",
                            "migrated_attempt_count": len(migrated_attempt_keys),
                            "remaining_attempt_count": len(final_history),
                            "split_relation_ids": sorted({
                                s.split_relation_id for s in plan.splits
                            }),
                        })

                        cur.execute(
                            """
                            UPDATE route_raw.route_jobs
                            SET extractor_review = %s::jsonb
                            WHERE route_id = %s::uuid
                            """,
                            (
                                json.dumps(current_review, ensure_ascii=False, default=str),
                                plan.host_route_id,
                            ),
                        )

                        actions.append({
                            "action": "TRIMMED_HOST_HISTORY",
                            "host_route_id": plan.host_route_id,
                            "original_attempts": plan.total_attempts,
                            "remaining_attempts": len(final_history),
                            "migrated_count": len(migrated_attempt_keys),
                        })

    return actions


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(plans: List[RepairPlan], actions: List[Dict[str, Any]], *, mode: str) -> None:
    total_hosts = len(plans)
    total_splits = sum(len(p.splits) for p in plans)
    total_migrated = sum(len(s.attempts) for p in plans for s in p.splits)

    print(f"\n{'='*60}")
    print(f"REPAIR SUMMARY ({mode})")
    print(f"{'='*60}")
    print(f"Contaminated hosts analyzed: {total_hosts}")
    print(f"Hosts requiring splits: {sum(1 for p in plans if p.splits)}")
    print(f"Total split groups to create: {total_splits}")
    print(f"Total attempts to migrate: {total_migrated}")
    print()

    for plan in plans:
        if not plan.splits:
            continue
        print(f"  Host {plan.host_route_id[:12]}... "
              f"(relation={plan.host_relation_id}, source={plan.host_extractor_source})")
        print(f"    total_attempts={plan.total_attempts}, "
              f"kept={plan.kept_attempts}, "
              f"split_groups={len(plan.splits)}")
        for split in plan.splits:
            print(f"      -> relation={split.split_relation_id} "
                  f"source={split.split_source_document} "
                  f"attempts={len(split.attempts)} "
                  f"places={split.distinct_places}")

    print(f"\n--- Actions ({len(actions)}) ---")
    for action in actions:
        print(f"  {json.dumps(action, ensure_ascii=False, default=str)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repair historically contaminated Phase 3 extractor-review hosts."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Analyze and report without changing data.")
    group.add_argument("--apply", action="store_true", help="Apply the repair to the local DB.")
    parser.add_argument(
        "--min-history", type=int, default=2,
        help="Minimum attempt_history length to consider a host (default: 2).",
    )
    parser.add_argument(
        "--json-output", type=str, default=None,
        help="Optional path to write the repair plan/results as JSON.",
    )
    args = parser.parse_args(argv)

    dsn = (os.environ.get("DB_DSN") or "").strip()
    if not dsn:
        print("ERROR: DB_DSN not set. Set it to LOCAL_DB_DSN.", file=sys.stderr)
        return 2

    # Safety: verify we are on local DB
    if "127.0.0.1" not in dsn and "localhost" not in dsn:
        print(f"ERROR: DB_DSN does not point to local DB (127.0.0.1). Refusing to run.", file=sys.stderr)
        return 2

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print(f"Mode: {mode}")
    print(f"DB target: local (verified 127.0.0.1)")

    hosts = fetch_contaminated_hosts(dsn, min_history=args.min_history)
    print(f"Multi-attempt hosts found: {len(hosts)}")

    plans = [
        plan for plan in (build_repair_plan(host) for host in hosts)
        if plan is not None and plan.splits
    ]
    print(f"Hosts requiring repair: {len(plans)}")

    actions = execute_repair(dsn, plans, apply=args.apply)
    print_summary(plans, actions, mode=mode)

    if args.json_output:
        output_path = Path(args.json_output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": mode.lower().replace(" ", "_"),
            "hosts_analyzed": len(hosts),
            "hosts_repaired": len(plans),
            "total_splits": sum(len(p.splits) for p in plans),
            "total_attempts_migrated": sum(
                len(s.attempts) for p in plans for s in p.splits
            ),
            "plans": [
                {
                    "host_route_id": p.host_route_id,
                    "host_relation_id": p.host_relation_id,
                    "total_attempts": p.total_attempts,
                    "kept_attempts": p.kept_attempts,
                    "splits": [
                        {
                            "split_relation_id": s.split_relation_id,
                            "split_source_document": s.split_source_document,
                            "attempt_count": len(s.attempts),
                            "distinct_places": s.distinct_places,
                            "distinct_hints": s.distinct_hints,
                            "reason": s.reason,
                        }
                        for s in p.splits
                    ],
                }
                for p in plans
            ],
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
