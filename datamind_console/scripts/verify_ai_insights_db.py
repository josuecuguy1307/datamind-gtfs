from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _active_dsn_meta() -> Dict[str, Any]:
    from datamind_console.common.config import CFG, resolve_active_db_dsn

    mode = os.getenv("DATA_MODE") or CFG.data_mode
    dsn = resolve_active_db_dsn(mode=mode)
    parsed = urlparse(str(dsn or ""))
    return {
        "mode": str(mode),
        "dsn": dsn,
        "host": parsed.hostname,
        "port": parsed.port,
        "database": (parsed.path[1:] if str(parsed.path).startswith("/") else parsed.path),
    }


def _print_json(label: str, payload: Dict[str, Any]) -> None:
    print(f"{label}: {json.dumps(payload, ensure_ascii=True, sort_keys=True)}")


def _probe_connection() -> Dict[str, Any]:
    from datamind_console.db.db import db_conn

    with db_conn(readonly=True) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                  current_database() AS db,
                  COALESCE(inet_server_addr()::text, 'local_socket') AS host,
                  inet_server_port() AS port,
                  to_regclass('ai.ai_bot_run_logs')::text AS run_logs_table,
                  to_regclass('ai.ai_bot_model_metrics')::text AS model_metrics_table,
                  to_regclass('ai.ai_bot_train_events')::text AS train_events_table
                """
            )
            row = dict(cur.fetchone() or {})
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*)::int AS n FROM ai.ai_bot_run_logs")
            run_n = int((dict(cur.fetchone() or {})).get("n") or 0)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*)::int AS n FROM ai.ai_bot_model_metrics")
            metric_n = int((dict(cur.fetchone() or {})).get("n") or 0)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*)::int AS n FROM ai.ai_bot_train_events")
            train_n = int((dict(cur.fetchone() or {})).get("n") or 0)

    return {
        "db": row.get("db"),
        "host": row.get("host"),
        "port": row.get("port"),
        "tables": {
            "ai_bot_run_logs": row.get("run_logs_table"),
            "ai_bot_model_metrics": row.get("model_metrics_table"),
            "ai_bot_train_events": row.get("train_events_table"),
        },
        "row_counts": {
            "run_logs": run_n,
            "model_metrics": metric_n,
            "train_events": train_n,
        },
    }


def _write_read_cleanup_smoke(*, verify_token: str) -> Dict[str, Any]:
    from datamind_console.ai_insights import storage
    from datamind_console.db.db import db_conn

    now = datetime.now(timezone.utc)
    ts1 = (now - timedelta(seconds=1)).isoformat()
    ts2 = now.isoformat()

    row_a = {
        "timestamp": ts1,
        "phase": "phase3",
        "stage": "verify_db_step",
        "event_type": "run",
        "status": "success",
        "run_id": f"verify-{verify_token}",
        "route_id": str(uuid.uuid4()),
        "prior_stop_count": 10,
        "matched_count": 8,
        "unmatched_count": 2,
        "ambiguous_count": 0,
        "sequence_quality_score": 82.0,
        "quality_score": 79.5,
        "quality_breakdown": [{"component": "smoke", "score": 79.5}],
        "warnings": ["verify_warning", "verify_warning"],
        "payload": {"verify_token": verify_token, "ordinal": 1},
    }
    row_b = {
        **row_a,
        "timestamp": ts2,
        "quality_score": 83.5,
        "payload": {"verify_token": verify_token, "ordinal": 2},
    }

    storage.append_run_log(row_a)
    storage.append_run_log(row_b)
    storage.append_model_metrics(
        {
            "timestamp": ts2,
            "task": "verify_ai_bot_db",
            "event_type": "eval",
            "status": "success",
            "ok": True,
            "metric_primary_name": "smoke_metric",
            "metric_primary_value": 0.9,
            "metric_higher_better": True,
            "payload": {"verify_token": verify_token},
        }
    )
    storage.append_train_event(
        {
            "timestamp": ts2,
            "task": "verify_ai_bot_db",
            "event_type": "manual",
            "status": "success",
            "ok": True,
            "payload": {"verify_token": verify_token},
        }
    )

    run_rows = storage.load_run_logs(limit=2000)
    metric_rows = storage.load_model_metrics(limit=2000)
    train_rows = storage.load_train_events(limit=2000)

    run_hits = [r for r in run_rows if str((r.get("payload") or {}).get("verify_token") or "") == verify_token]
    metric_hits = [r for r in metric_rows if str((r.get("payload") or {}).get("verify_token") or "") == verify_token]
    train_hits = [r for r in train_rows if str((r.get("payload") or {}).get("verify_token") or "") == verify_token]

    ordered_ok = False
    if len(run_hits) >= 2:
        ts_vals = [str(r.get("timestamp") or "") for r in run_hits]
        ordered_ok = ts_vals == sorted(ts_vals)

    diag = storage.get_storage_diagnostics()

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM ai.ai_bot_run_logs
                WHERE run_id = %s
                   OR payload->>'verify_token' = %s
                   OR payload#>>'{payload,verify_token}' = %s
                """,
                (f"verify-{verify_token}", verify_token, verify_token),
            )
            del_runs = int(cur.rowcount or 0)
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM ai.ai_bot_model_metrics
                WHERE task = 'verify_ai_bot_db'
                   OR payload->>'verify_token' = %s
                   OR payload#>>'{payload,verify_token}' = %s
                """,
                (verify_token, verify_token),
            )
            del_metrics = int(cur.rowcount or 0)
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM ai.ai_bot_train_events
                WHERE task = 'verify_ai_bot_db'
                   OR payload->>'verify_token' = %s
                   OR payload#>>'{payload,verify_token}' = %s
                """,
                (verify_token, verify_token),
            )
            del_train = int(cur.rowcount or 0)

    return {
        "storage_mode": diag.get("mode"),
        "storage_mode_detail": diag.get("mode_detail"),
        "read_sources": {
            "run_logs": ((diag.get("db_read_status") or {}).get("run_logs") or {}).get("source"),
            "model_metrics": ((diag.get("db_read_status") or {}).get("model_metrics") or {}).get("source"),
            "train_events": ((diag.get("db_read_status") or {}).get("train_events") or {}).get("source"),
        },
        "db_write_status": diag.get("db_write_status") or {},
        "hits": {
            "run_logs": len(run_hits),
            "model_metrics": len(metric_hits),
            "train_events": len(train_hits),
        },
        "ordering": {
            "stored_oldest_to_newest": bool(ordered_ok),
            "latest_quality_score": (run_hits[-1].get("quality_score") if run_hits else None),
        },
        "cleanup": {
            "run_logs_deleted": del_runs,
            "model_metrics_deleted": del_metrics,
            "train_events_deleted": del_train,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify AI Insights DB-backed path safely.")
    parser.add_argument("--expect-db", default="", help="Expected database name (recommended, e.g. datamind_ml).")
    parser.add_argument("--allow-write", action="store_true", help="Run write/read/cleanup smoke. Default is read-only.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if DB-backed read/write is not fully validated.",
    )
    args = parser.parse_args()

    meta = _active_dsn_meta()
    _print_json(
        "ACTIVE_DSN",
        {
            "mode": meta.get("mode"),
            "host": meta.get("host"),
            "port": meta.get("port"),
            "database": meta.get("database"),
            "dsn_present": bool(meta.get("dsn")),
        },
    )

    if not meta.get("dsn"):
        print("RESULT: FAIL no DSN resolved")
        return 2

    if args.expect_db:
        actual = str(meta.get("database") or "")
        if actual != str(args.expect_db):
            print(f"RESULT: FAIL expected DB '{args.expect_db}' but active DB is '{actual}'")
            return 3

    try:
        probe = _probe_connection()
        _print_json("DB_PROBE", probe)
    except Exception as e:
        print(f"RESULT: DB_UNAVAILABLE {type(e).__name__}: {e}")
        return 4 if args.strict else 0

    if not args.allow_write:
        print("RESULT: OK read-only probe complete (no writes performed).")
        return 0

    if not args.expect_db:
        print("RESULT: FAIL --allow-write requires --expect-db to avoid wrong-target writes.")
        return 5

    temp_dir = Path(tempfile.mkdtemp(prefix="ai_insights_verify_"))
    verify_token = f"verify-{uuid.uuid4()}"
    os.environ["DATAMIND_AI_INSIGHTS_DIR"] = str(temp_dir)

    try:
        smoke = _write_read_cleanup_smoke(verify_token=verify_token)
        _print_json("WRITE_READ_SMOKE", smoke)

        read_sources = smoke.get("read_sources") or {}
        db_ok = (
            str(read_sources.get("run_logs")) == "db"
            and str(read_sources.get("model_metrics")) == "db"
            and str(read_sources.get("train_events")) == "db"
            and int((smoke.get("hits") or {}).get("run_logs") or 0) >= 2
            and int((smoke.get("hits") or {}).get("model_metrics") or 0) >= 1
            and int((smoke.get("hits") or {}).get("train_events") or 0) >= 1
        )
        if db_ok:
            print("RESULT: OK db-backed write/read/ordering/cleanup verified.")
            return 0
        print("RESULT: PARTIAL DB reachable but storage path not fully DB-backed.")
        return 6 if args.strict else 0
    except Exception as e:
        print(f"RESULT: FAIL write/read smoke failed: {type(e).__name__}: {e}")
        return 7
    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
