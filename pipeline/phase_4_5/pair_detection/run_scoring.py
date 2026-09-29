"""Bespoke runner: invoke pair-detection scorer over a cohort of routes.

This is NOT a permanent CLI — it's a one-shot wrapper around the migrated
``refresh_inverse_proposals`` entry point so we can populate
``route_work.inverse_direction_status.top_candidate_scores`` for the
ready-for-Phase-4.5 cohort.

Steps:
  1. Identify the distinct service_route_ids that the ready cohort spans.
  2. For each service_route_id, call ``refresh_inverse_proposals(service_route_id=...)``
     which scores candidates and persists via upsert.
  3. Log per-anchor outcomes + timings.

Usage::

    python -m pipeline.phase_4_5.pair_detection.run_scoring \\
        --province sample_region [--probe-only] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras


def _dsn() -> str:
    dsn = (
        os.getenv("DB_DSN")
        or os.getenv("LOCAL_DB_DSN")
        or ""
    )
    if "amazonaws" in dsn or "rds" in dsn:
        sys.stderr.write(
            "FATAL: DB_DSN points to AWS. run_scoring runs on local DB only.\n"
        )
        sys.exit(1)
    return dsn


def _connect():
    return psycopg2.connect(_dsn())


def _ready_service_route_ids(province: str, *, limit: Optional[int]) -> list[str]:
    sql = """
        SELECT DISTINCT r.service_route_id::text AS service_route_id
          FROM route_prod.routes r
         WHERE r.province = %s
           AND COALESCE(r.canonical_sequence_ready, FALSE) = TRUE
           AND r.chosen_stop_sequence_candidate_id IS NOT NULL
           AND r.deploy_status = 'active'
           AND r.pending_human_review = FALSE
           AND (r.semantics_updated_at IS NOT NULL OR r.naming_confidence IS NOT NULL)
           AND r.service_route_id IS NOT NULL
         ORDER BY 1
    """
    params: tuple = (province,)
    if limit is not None:
        sql += " LIMIT %s"
        params = (province, limit)
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() or []
    return [str(r["service_route_id"]) for r in rows if r.get("service_route_id")]


class _Tee:
    """Simple stdout-+-file tee."""

    def __init__(self, path: Path):
        self.path = path
        self.fp = path.open("w")

    def write(self, msg: str) -> None:
        sys.stdout.write(msg)
        sys.stdout.flush()
        self.fp.write(msg)
        self.fp.flush()

    def close(self) -> None:
        self.fp.close()


def _probe_one(sid: str, log: _Tee) -> dict:
    """Read-only probe — call analyze_inverse_proposals for a single anchor."""
    from pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion import (
        analyze_inverse_proposals,
    )

    log.write(f"[probe] analyze_inverse_proposals(service_route_id={sid})\n")
    t0 = time.time()
    result = analyze_inverse_proposals(service_route_id=sid)
    dt = time.time() - t0
    rows = list(result.results or [])
    log.write(f"[probe] returned {len(rows)} snapshot(s) in {dt:.2f}s\n")
    summary = []
    for r in rows[:5]:
        summary.append(
            {
                "service_route_id": str(r.service_route_id or ""),
                "direction_id": int(r.direction_id) if r.direction_id is not None else None,
                "proposal_status": r.proposal_status,
                "top_candidate_route_id": r.top_candidate_route_id,
                "top_candidate_scores": r.top_candidate_scores,
            }
        )
    log.write(json.dumps(summary, indent=2, default=str))
    log.write("\n")
    return {"snapshots": len(rows), "elapsed_s": dt}


def _refresh_one(sid: str, log: _Tee) -> dict:
    """Persisting call — scores + writes to inverse_direction_status."""
    from pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion import (
        refresh_inverse_proposals,
    )

    t0 = time.time()
    try:
        result = refresh_inverse_proposals(service_route_id=sid)
        dt = time.time() - t0
        n_persisted = int(getattr(result, "persisted_row_count", 0) or 0)
        n_proposals = len(list((result.proposals.results if result.proposals else []) or []))
        statuses: dict[str, int] = {}
        for r in (result.proposals.results if result.proposals else []) or []:
            key = str(getattr(r, "proposal_status", "") or "unknown")
            statuses[key] = statuses.get(key, 0) + 1
        return {
            "ok": True,
            "elapsed_s": dt,
            "persisted_rows": n_persisted,
            "proposals": n_proposals,
            "statuses": statuses,
        }
    except Exception as exc:
        dt = time.time() - t0
        log.write(f"[FAIL] {sid}: {type(exc).__name__}: {exc}\n")
        return {"ok": False, "elapsed_s": dt, "error": f"{type(exc).__name__}: {exc}"}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--province", required=True)
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Only run the read-only analyze_inverse_proposals on the first anchor; do not persist.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of service_route anchors processed.",
    )
    parser.add_argument(
        "--service-route-ids",
        default=None,
        help="CSV of explicit service_route_ids to score. Bypasses the ready-cohort "
             "query — pass exactly the anchors you want.",
    )
    args = parser.parse_args(argv)

    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    log_dir = Path("reports")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"pair_detection_scoring_{ts}.log"
    log = _Tee(log_path)
    log.write(f"# pair_detection scoring run @ {ts}\n")
    log.write(f"# province={args.province}  probe_only={args.probe_only}  limit={args.limit}\n\n")

    if args.service_route_ids:
        sids = [s.strip() for s in args.service_route_ids.split(",") if s.strip()]
        log.write(f"explicit --service-route-ids cohort: {len(sids)} anchors\n\n")
    else:
        sids = _ready_service_route_ids(args.province, limit=args.limit)
        log.write(f"distinct ready service_route_ids: {len(sids)}\n\n")
    if not sids:
        log.write("No ready service_route_ids found — nothing to score.\n")
        log.close()
        return 0

    if args.probe_only:
        result = _probe_one(sids[0], log)
        log.write(f"\n[probe-only] DONE in {result['elapsed_s']:.2f}s\n")
        log.close()
        return 0

    # First do a probe on the first anchor as a smoke check before fanning out.
    log.write("=== probe (first anchor) ===\n")
    probe = _probe_one(sids[0], log)
    log.write(f"=== probe done ({probe['elapsed_s']:.2f}s) ===\n\n")

    log.write(f"=== refreshing {len(sids)} anchors ===\n")
    aggregate = {"ok": 0, "fail": 0, "persisted_rows": 0, "elapsed_s": 0.0}
    status_total: dict[str, int] = {}
    started = time.time()
    for i, sid in enumerate(sids, start=1):
        outcome = _refresh_one(sid, log)
        aggregate["elapsed_s"] += float(outcome.get("elapsed_s") or 0.0)
        if outcome.get("ok"):
            aggregate["ok"] += 1
            aggregate["persisted_rows"] += int(outcome.get("persisted_rows") or 0)
            for k, v in (outcome.get("statuses") or {}).items():
                status_total[k] = status_total.get(k, 0) + int(v)
        else:
            aggregate["fail"] += 1
        if i % 10 == 0 or i == len(sids):
            elapsed = time.time() - started
            log.write(
                f"[{i:>3}/{len(sids)}] anchors processed  "
                f"ok={aggregate['ok']} fail={aggregate['fail']} "
                f"persisted_rows={aggregate['persisted_rows']} "
                f"wall={elapsed:.1f}s\n"
            )

    log.write("\n=== aggregate proposal_status counts ===\n")
    for k, v in sorted(status_total.items(), key=lambda kv: (-kv[1], kv[0])):
        log.write(f"  {k:<32} {v}\n")
    log.write(f"\nDONE. log: {log_path}\n")
    log.close()
    return 0 if aggregate["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
