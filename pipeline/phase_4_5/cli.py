"""Phase 4.5 orchestrator CLI.

Usage::

    python -m pipeline.phase_4_5.cli --dry-run --province sample_region --limit 10
    python -m pipeline.phase_4_5.cli --apply   --province sample_region

Loads candidate routes for a province, runs pair-detection scoring
(via the migrated ``pair_detection`` package), classifies each through the
hard gate, and either:

- ``--dry-run`` (default): prints the summary, writes audit rows with
  ``source='dry_run'``, never updates ``route_prod.routes.direction_id``
- ``--apply``: writes audit rows with the appropriate source AND updates
  ``route_prod.routes.direction_id`` for terminal states that have one

This PR ships only stages 1–2 (pair detection + hard gate) reachable via
the CLI. Stages 3–6 (synthesis, DR escalation, rollback) are stubbed —
routes that fall into ``synthesis_candidate`` or ``no_signal`` are
recorded in the audit log without committing a ``direction_id``.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from .commit import commit_state
from .hard_gate import (
    NO_SIGNAL,
    OPERATOR_PENDING,
    PAIRED,
    SYNTHESIS_CANDIDATE,
    classify_by_score,
)


def _dsn() -> str:
    """Local-only DSN. Mirrors precision_snap.py:_dsn — refuses AWS."""
    dsn = (
        os.getenv("DB_DSN")
        or os.getenv("LOCAL_DB_DSN")
        or ""
    )
    if "amazonaws" in dsn or "rds" in dsn:
        sys.stderr.write(
            "FATAL: DB_DSN points to AWS. Phase 4.5 runs on local DB only.\n"
        )
        sys.exit(1)
    return dsn


def _connect():
    return psycopg2.connect(_dsn())


def _fetch_routes(
    conn,
    *,
    province: str,
    limit: Optional[int],
    only_ready: bool = True,
) -> list[dict[str, Any]]:
    """Fetch routes in the given province + their existing inverse-completion score (if any).

    By default scopes to the "ready for Phase 4.5" cohort:
      - GREEK done (canonical_sequence_ready = TRUE)
      - has chosen stop sequence
      - deploy_status = 'active'
      - not pending_human_review
      - Phase 4 semantics applied (semantics_updated_at OR naming_confidence)

    Pass ``only_ready=False`` to widen — useful for triage / audit only.
    """
    where = ["r.province = %s"]
    params: list = [province]
    if only_ready:
        where.extend([
            "COALESCE(r.canonical_sequence_ready, FALSE) = TRUE",
            "r.chosen_stop_sequence_candidate_id IS NOT NULL",
            "r.deploy_status = 'active'",
            "r.pending_human_review = FALSE",
            "(r.semantics_updated_at IS NOT NULL OR r.naming_confidence IS NOT NULL)",
        ])
    sql = (
        """
        SELECT r.route_id::text          AS route_id,
               r.route_name,
               r.direction_id            AS prev_direction_id,
               r.province,
               r.canonical_sequence_ready,
               r.naming_confidence,
               ids.top_candidate_route_id::text AS paired_route_id,
               ids.top_candidate_scores  AS scores
          FROM route_prod.routes r
     LEFT JOIN route_work.inverse_direction_status ids
            ON ids.service_route_id = r.service_route_id
           AND ids.direction_id     = COALESCE(r.direction_id, 0)
         WHERE """
        + " AND ".join(where)
        + " ORDER BY r.created_at ASC"
    )
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def _qc_summary(conn, *, province: str) -> dict[str, int]:
    """One-shot QC counts for stdout. Mirrors the discovery query exactly."""
    sql = """
        SELECT
          count(*)                                                              AS total,
          count(*) FILTER (WHERE COALESCE(canonical_sequence_ready, FALSE) = TRUE
                           AND chosen_stop_sequence_candidate_id IS NOT NULL
                           AND deploy_status = 'active'
                           AND pending_human_review = FALSE
                           AND (semantics_updated_at IS NOT NULL
                                OR naming_confidence IS NOT NULL))               AS ready,
          count(*) FILTER (WHERE COALESCE(canonical_sequence_ready, FALSE) = FALSE) AS blocked_no_greek,
          count(*) FILTER (WHERE COALESCE(canonical_sequence_ready, FALSE) = TRUE
                           AND chosen_stop_sequence_candidate_id IS NOT NULL
                           AND semantics_updated_at IS NULL
                           AND naming_confidence IS NULL)                        AS blocked_no_semantics,
          count(*) FILTER (WHERE pending_human_review = TRUE)                    AS blocked_pending_review,
          count(*) FILTER (WHERE deploy_status <> 'active')                      AS blocked_inactive,
          count(*) FILTER (WHERE direction_id IS NULL)                           AS null_dir_id
        FROM route_prod.routes
        WHERE province = %s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (province,))
        row = cur.fetchone() or {}
        return {k: int(row.get(k) or 0) for k in (
            "total", "ready", "blocked_no_greek", "blocked_no_semantics",
            "blocked_pending_review", "blocked_inactive", "null_dir_id",
        )}


def _opposite_score_from(scores: Optional[dict[str, Any]]) -> Optional[float]:
    """Pull the opposite-direction score out of inverse_direction_status.top_candidate_scores."""
    if not scores or not isinstance(scores, dict):
        return None
    for key in ("opposite_direction_score", "opposite_direction", "opposite", "score"):
        val = scores.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def _audit_source_for(state: str) -> str:
    """Map a hard-gate branch to the audit ``source`` column value."""
    if state == PAIRED:
        return "pair_detected"
    if state == OPERATOR_PENDING:
        return "operator_set"
    return "operator_set"  # synthesis_candidate, no_signal — no commit yet, audit-only


def _summary_line(state: str, count: int, total: int) -> str:
    pct = 100.0 * count / total if total else 0.0
    return f"  {state+':':<22} {count:>5} ({pct:5.1f}%)"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.phase_4_5.cli",
        description=__doc__,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="Audit-only; do not update direction_id (default)")
    mode.add_argument("--apply", action="store_true",
                      help="Apply: update direction_id for terminal states")
    parser.add_argument("--province", required=True, help="Province filter, e.g. 'sample_region'")
    parser.add_argument("--limit", type=int, default=None, help="Cap routes scanned")
    parser.add_argument("--run-id", default=None,
                        help="Override run UUID (default: random)")
    parser.add_argument(
        "--include-not-ready",
        action="store_true",
        help="Include routes that have not yet exited GREEK / Phase 4 naming. "
             "Default behaviour scopes to the ready cohort only.",
    )
    parser.add_argument(
        "--qc-only",
        action="store_true",
        help="Print the readiness QC summary and exit; do not classify or audit.",
    )
    args = parser.parse_args(argv)

    dry_run = not args.apply
    run_id = uuid.UUID(args.run_id) if args.run_id else uuid.uuid4()

    print(f"Phase 4.5 — direction construction")
    print(f"  province       : {args.province}")
    print(f"  mode           : {'dry-run' if dry_run else 'APPLY'}")
    print(f"  scope          : {'ALL routes (--include-not-ready)' if args.include_not_ready else 'READY cohort only'}")
    print(f"  run_id         : {run_id}")
    print()

    conn = _connect()
    try:
        qc = _qc_summary(conn, province=args.province)
        print("Readiness QC:")
        print(f"  total                      : {qc['total']:>5}")
        print(f"  ready for Phase 4.5        : {qc['ready']:>5}")
        print(f"  blocked: no GREEK          : {qc['blocked_no_greek']:>5}")
        print(f"  blocked: no semantics      : {qc['blocked_no_semantics']:>5}")
        print(f"  blocked: pending review    : {qc['blocked_pending_review']:>5}")
        print(f"  blocked: inactive          : {qc['blocked_inactive']:>5}")
        print(f"  current NULL direction_id  : {qc['null_dir_id']:>5}")
        print()
        if args.qc_only:
            return 0

        counts = {PAIRED: 0, OPERATOR_PENDING: 0, SYNTHESIS_CANDIDATE: 0, NO_SIGNAL: 0}
        audit_ids: list[int] = []
        routes = _fetch_routes(
            conn,
            province=args.province,
            limit=args.limit,
            only_ready=not args.include_not_ready,
        )
        if not routes:
            print(f"No active routes found for province={args.province!r}")
            return 0
        for row in routes:
            score = _opposite_score_from(row.get("scores"))
            state = classify_by_score(score)
            counts[state] = counts.get(state, 0) + 1

            new_direction_id: Optional[int] = None
            paired_route_id = row.get("paired_route_id")
            paired_uuid = uuid.UUID(paired_route_id) if paired_route_id else None
            reason: str

            if state == PAIRED:
                # Stage 5 final assignment per direction_construction.md is geographic-
                # convention based; until we wire endpoint geometry through, we leave
                # direction_id unset in dry-run and skip the apply path. Apply for paired
                # routes is implemented in a follow-up session that adds geometry lookup.
                reason = f"paired by score={score:.4f} (>= RELIABLE)"
                if not dry_run:
                    reason += " — apply skipped: stage 5 geometry-based assignment lands in follow-up"
                    new_direction_id = None
            elif state == OPERATOR_PENDING:
                reason = f"score={score:.4f} in [PLAUSIBLE, RELIABLE) — queued for human review"
                new_direction_id = None
            elif state == SYNTHESIS_CANDIDATE:
                reason = f"score={score:.4f} below PLAUSIBLE — synthesis candidate (deferred)"
                new_direction_id = None
            else:  # NO_SIGNAL
                reason = "no candidate pair score available — no_signal"
                new_direction_id = None

            audit_id = commit_state(
                conn,
                run_id=run_id,
                route_id=uuid.UUID(row["route_id"]),
                new_state=state,
                new_direction_id=new_direction_id,
                source=_audit_source_for(state),
                prev_state=None,
                prev_direction_id=row.get("prev_direction_id"),
                paired_route_id=paired_uuid,
                pair_score=score,
                reason=reason,
                dry_run=dry_run,
            )
            audit_ids.append(audit_id)
    finally:
        conn.close()

    total = sum(counts.values())
    print(f"Routes scanned: {total}")
    for state in (PAIRED, OPERATOR_PENDING, SYNTHESIS_CANDIDATE, NO_SIGNAL):
        print(_summary_line(state, counts[state], total))
    print()
    print(f"Audit rows written: {len(audit_ids)} (mode={'dry_run' if dry_run else 'apply'}, run_id={run_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
