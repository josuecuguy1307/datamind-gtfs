from __future__ import annotations

import json
import threading
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse
from uuid import UUID

from . import config

try:
    from datamind_console.db.ai_repo import (
        create_ai_bot_model_metric,
        create_ai_bot_run_log,
        create_ai_bot_train_event,
        list_ai_bot_model_metrics,
        list_ai_bot_run_logs,
        list_ai_bot_train_events,
    )
except Exception:
    create_ai_bot_model_metric = None
    create_ai_bot_run_log = None
    create_ai_bot_train_event = None
    list_ai_bot_model_metrics = None
    list_ai_bot_run_logs = None
    list_ai_bot_train_events = None


_JSONL_LOCK = threading.Lock()
_DIAG_LOCK = threading.Lock()
_RUNTIME_DIAG: Dict[str, Any] = {
    "db_writes": {},
    "db_reads": {},
    "jsonl_read_stats": {},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _ts_key(row: Dict[str, Any]) -> str:
    return str(row.get("timestamp") or "")


def _diag_db_write(kind: str, *, ok: bool, error: Optional[str] = None) -> None:
    with _DIAG_LOCK:
        _RUNTIME_DIAG["db_writes"][str(kind)] = {
            "timestamp": _now_iso(),
            "ok": bool(ok),
            "error": (str(error)[:500] if error else None),
        }


def _diag_db_read(kind: str, *, ok: bool, source: str, rows: int, error: Optional[str] = None) -> None:
    with _DIAG_LOCK:
        _RUNTIME_DIAG["db_reads"][str(kind)] = {
            "timestamp": _now_iso(),
            "ok": bool(ok),
            "source": str(source),
            "rows": int(rows),
            "error": (str(error)[:500] if error else None),
        }


def _diag_jsonl_read(path: Path, *, total_lines: int, valid_rows: int, invalid_lines: int) -> None:
    with _DIAG_LOCK:
        _RUNTIME_DIAG["jsonl_read_stats"][str(path)] = {
            "timestamp": _now_iso(),
            "total_lines": int(total_lines),
            "valid_rows": int(valid_rows),
            "invalid_lines": int(invalid_lines),
        }


def ensure_ai_insights_dirs() -> None:
    config.AI_INSIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)


def _active_db_target() -> Dict[str, Any]:
    try:
        from datamind_console.common.config import CFG, resolve_active_db_dsn

        mode = os.getenv("DATA_MODE") or CFG.data_mode
        dsn = resolve_active_db_dsn(mode=mode)
        parsed = urlparse(str(dsn or ""))
        database = parsed.path[1:] if str(parsed.path).startswith("/") else parsed.path
        return {
            "data_mode": str(mode or ""),
            "dsn_present": bool(dsn),
            "host": parsed.hostname,
            "port": parsed.port,
            "database": database,
        }
    except Exception:
        return {
            "data_mode": str(os.getenv("DATA_MODE") or ""),
            "dsn_present": False,
            "host": None,
            "port": None,
            "database": None,
        }


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    ensure_ai_insights_dirs()
    payload = dict(row)
    payload.setdefault("timestamp", _now_iso())
    line = json.dumps(_jsonable(payload), ensure_ascii=True, separators=(",", ":"))
    with _JSONL_LOCK:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_jsonl(path: Path, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if not path.exists():
        _diag_jsonl_read(path, total_lines=0, valid_rows=0, invalid_lines=0)
        return []
    out: List[Dict[str, Any]] = []
    total_lines = 0
    invalid_lines = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            total_lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                invalid_lines += 1
                continue
            if isinstance(obj, dict):
                out.append(obj)
            else:
                invalid_lines += 1
    _diag_jsonl_read(
        path,
        total_lines=total_lines,
        valid_rows=len(out),
        invalid_lines=invalid_lines,
    )
    if limit is None or limit <= 0:
        return out
    return out[-int(limit) :]


def append_run_log(row: Dict[str, Any]) -> None:
    payload = dict(row)
    append_jsonl(config.RUN_LOG_FILE, payload)
    if config.ENABLE_AI_INSIGHTS_DB and callable(create_ai_bot_run_log):
        try:
            create_ai_bot_run_log(
                created_at=(str(payload.get("timestamp") or "") or None),
                phase=str(payload.get("phase") or "").strip().lower(),
                stage=(str(payload.get("stage")).strip() if payload.get("stage") is not None else None),
                event_type=str(payload.get("event_type") or "run").strip().lower(),
                status=str(payload.get("status") or "success").strip().lower(),
                run_id=(str(payload.get("run_id")).strip() if payload.get("run_id") is not None else None),
                node_set_id=(str(payload.get("node_set_id")).strip() if payload.get("node_set_id") is not None else None),
                route_id=(str(payload.get("route_id")).strip() if payload.get("route_id") is not None else None),
                service_route_id=(
                    str(payload.get("service_route_id")).strip()
                    if payload.get("service_route_id") is not None
                    else None
                ),
                direction_id=(int(payload.get("direction_id")) if payload.get("direction_id") is not None else None),
                quality_score=(
                    float(payload.get("quality_score"))
                    if payload.get("quality_score") is not None
                    else None
                ),
                sequence_quality_score=(
                    float(payload.get("sequence_quality_score"))
                    if payload.get("sequence_quality_score") is not None
                    else None
                ),
                reorder_recommended=(
                    bool(payload.get("reorder_recommended"))
                    if payload.get("reorder_recommended") is not None
                    else None
                ),
                reorder_confidence=(
                    float(payload.get("reorder_confidence"))
                    if payload.get("reorder_confidence") is not None
                    else None
                ),
                warnings=[str(x) for x in (payload.get("warnings") or [])],
                notes=[str(x) for x in (payload.get("notes") or [])],
                payload=payload,
            )
            _diag_db_write("run_logs", ok=True)
        except Exception as e:
            _diag_db_write("run_logs", ok=False, error=e)
    elif config.ENABLE_AI_INSIGHTS_DB:
        _diag_db_write("run_logs", ok=False, error="ai_repo_run_log_function_unavailable")


def append_model_metrics(row: Dict[str, Any]) -> None:
    payload = dict(row)
    append_jsonl(config.MODEL_METRICS_FILE, payload)
    if config.ENABLE_AI_INSIGHTS_DB and callable(create_ai_bot_model_metric):
        try:
            create_ai_bot_model_metric(
                created_at=(str(payload.get("timestamp") or "") or None),
                task=str(payload.get("task") or "").strip(),
                event_type=str(payload.get("event_type") or "eval").strip().lower(),
                status=str(payload.get("status") or "success").strip().lower(),
                ok=bool(payload.get("ok", True)),
                metric_primary_name=(
                    str(payload.get("metric_primary_name")).strip()
                    if payload.get("metric_primary_name") is not None
                    else None
                ),
                metric_primary_value=(
                    float(payload.get("metric_primary_value"))
                    if payload.get("metric_primary_value") is not None
                    else None
                ),
                metric_higher_better=(
                    bool(payload.get("metric_higher_better"))
                    if payload.get("metric_higher_better") is not None
                    else None
                ),
                payload=payload,
            )
            _diag_db_write("model_metrics", ok=True)
        except Exception as e:
            _diag_db_write("model_metrics", ok=False, error=e)
    elif config.ENABLE_AI_INSIGHTS_DB:
        _diag_db_write("model_metrics", ok=False, error="ai_repo_model_metric_function_unavailable")


def append_train_event(row: Dict[str, Any]) -> None:
    payload = dict(row)
    append_jsonl(config.TRAIN_EVENTS_FILE, payload)
    if config.ENABLE_AI_INSIGHTS_DB and callable(create_ai_bot_train_event):
        try:
            create_ai_bot_train_event(
                created_at=(str(payload.get("timestamp") or "") or None),
                task=str(payload.get("task") or "").strip(),
                event_type=str(payload.get("event_type") or "manual").strip().lower(),
                status=str(payload.get("status") or "success").strip().lower(),
                ok=bool(payload.get("ok", True)),
                payload=payload,
            )
            _diag_db_write("train_events", ok=True)
        except Exception as e:
            _diag_db_write("train_events", ok=False, error=e)
    elif config.ENABLE_AI_INSIGHTS_DB:
        _diag_db_write("train_events", ok=False, error="ai_repo_train_event_function_unavailable")


def load_run_logs(*, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    db_error: Optional[str] = None
    if config.ENABLE_AI_INSIGHTS_DB and callable(list_ai_bot_run_logs):
        try:
            rows = list_ai_bot_run_logs(limit=(int(limit) if limit is not None else 50000))
            out: List[Dict[str, Any]] = []
            for r in rows:
                payload = dict(r.get("payload") or {})
                payload.setdefault("timestamp", r.get("timestamp"))
                payload.setdefault("phase", r.get("phase"))
                payload.setdefault("stage", r.get("stage"))
                payload.setdefault("event_type", r.get("event_type"))
                payload.setdefault("status", r.get("status"))
                payload.setdefault("run_id", r.get("run_id"))
                payload.setdefault("node_set_id", r.get("node_set_id"))
                payload.setdefault("route_id", r.get("route_id"))
                payload.setdefault("service_route_id", r.get("service_route_id"))
                payload.setdefault("direction_id", r.get("direction_id"))
                payload.setdefault("quality_score", r.get("quality_score"))
                payload.setdefault("sequence_quality_score", r.get("sequence_quality_score"))
                payload.setdefault("reorder_recommended", r.get("reorder_recommended"))
                payload.setdefault("reorder_confidence", r.get("reorder_confidence"))
                if "warnings" not in payload:
                    payload["warnings"] = list(r.get("warnings") or [])
                if "notes" not in payload:
                    payload["notes"] = list(r.get("notes") or [])
                out.append(payload)
            out.sort(key=_ts_key)
            _diag_db_read("run_logs", ok=True, source="db", rows=len(out))
            if limit is None or int(limit) <= 0:
                return out
            return out[-int(limit) :]
        except Exception as e:
            db_error = str(e)
            _diag_db_read("run_logs", ok=False, source="db", rows=0, error=db_error)
    elif config.ENABLE_AI_INSIGHTS_DB:
        db_error = "ai_repo_run_log_function_unavailable"
        _diag_db_read("run_logs", ok=False, source="db", rows=0, error=db_error)
    out = read_jsonl(config.RUN_LOG_FILE, limit=limit)
    _diag_db_read("run_logs", ok=True, source="jsonl", rows=len(out), error=db_error)
    return out


def load_model_metrics(*, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    db_error: Optional[str] = None
    if config.ENABLE_AI_INSIGHTS_DB and callable(list_ai_bot_model_metrics):
        try:
            rows = list_ai_bot_model_metrics(limit=(int(limit) if limit is not None else 50000))
            out: List[Dict[str, Any]] = []
            for r in rows:
                payload = dict(r.get("payload") or {})
                payload.setdefault("timestamp", r.get("timestamp"))
                payload.setdefault("task", r.get("task"))
                payload.setdefault("event_type", r.get("event_type"))
                payload.setdefault("status", r.get("status"))
                payload.setdefault("ok", r.get("ok"))
                payload.setdefault("metric_primary_name", r.get("metric_primary_name"))
                payload.setdefault("metric_primary_value", r.get("metric_primary_value"))
                payload.setdefault("metric_higher_better", r.get("metric_higher_better"))
                out.append(payload)
            out.sort(key=_ts_key)
            _diag_db_read("model_metrics", ok=True, source="db", rows=len(out))
            if limit is None or int(limit) <= 0:
                return out
            return out[-int(limit) :]
        except Exception as e:
            db_error = str(e)
            _diag_db_read("model_metrics", ok=False, source="db", rows=0, error=db_error)
    elif config.ENABLE_AI_INSIGHTS_DB:
        db_error = "ai_repo_model_metric_function_unavailable"
        _diag_db_read("model_metrics", ok=False, source="db", rows=0, error=db_error)
    out = read_jsonl(config.MODEL_METRICS_FILE, limit=limit)
    _diag_db_read("model_metrics", ok=True, source="jsonl", rows=len(out), error=db_error)
    return out


def load_train_events(*, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    db_error: Optional[str] = None
    if config.ENABLE_AI_INSIGHTS_DB and callable(list_ai_bot_train_events):
        try:
            rows = list_ai_bot_train_events(limit=(int(limit) if limit is not None else 50000))
            out: List[Dict[str, Any]] = []
            for r in rows:
                payload = dict(r.get("payload") or {})
                payload.setdefault("timestamp", r.get("timestamp"))
                payload.setdefault("task", r.get("task"))
                payload.setdefault("event_type", r.get("event_type"))
                payload.setdefault("status", r.get("status"))
                payload.setdefault("ok", r.get("ok"))
                out.append(payload)
            out.sort(key=_ts_key)
            _diag_db_read("train_events", ok=True, source="db", rows=len(out))
            if limit is None or int(limit) <= 0:
                return out
            return out[-int(limit) :]
        except Exception as e:
            db_error = str(e)
            _diag_db_read("train_events", ok=False, source="db", rows=0, error=db_error)
    elif config.ENABLE_AI_INSIGHTS_DB:
        db_error = "ai_repo_train_event_function_unavailable"
        _diag_db_read("train_events", ok=False, source="db", rows=0, error=db_error)
    out = read_jsonl(config.TRAIN_EVENTS_FILE, limit=limit)
    _diag_db_read("train_events", ok=True, source="jsonl", rows=len(out), error=db_error)
    return out


def get_storage_diagnostics() -> Dict[str, Any]:
    fn_availability = {
        "create_ai_bot_run_log": bool(callable(create_ai_bot_run_log)),
        "list_ai_bot_run_logs": bool(callable(list_ai_bot_run_logs)),
        "create_ai_bot_model_metric": bool(callable(create_ai_bot_model_metric)),
        "list_ai_bot_model_metrics": bool(callable(list_ai_bot_model_metrics)),
        "create_ai_bot_train_event": bool(callable(create_ai_bot_train_event)),
        "list_ai_bot_train_events": bool(callable(list_ai_bot_train_events)),
    }
    fn_ready = all(fn_availability.values())
    with _DIAG_LOCK:
        writes = dict(_RUNTIME_DIAG.get("db_writes") or {})
        reads = dict(_RUNTIME_DIAG.get("db_reads") or {})
        jsonl_stats = dict(_RUNTIME_DIAG.get("jsonl_read_stats") or {})

    mode = "file_only"
    mode_detail = "DB mirroring disabled by config."
    run_read = dict(reads.get("run_logs") or {})
    if bool(config.ENABLE_AI_INSIGHTS_DB):
        if not fn_ready:
            mode = "file_fallback"
            mode_detail = "DB mode enabled, but ai_repo DB functions are unavailable."
        else:
            src = str(run_read.get("source") or "").strip().lower()
            if src == "db" and bool(run_read.get("ok")):
                mode = "db"
                mode_detail = "DB-backed reads active (JSONL remains mirrored append-only backup)."
            elif src == "jsonl":
                mode = "file_fallback"
                err = str(run_read.get("error") or "").strip()
                mode_detail = "DB read failed; using JSONL fallback." if err else "Using JSONL fallback."
            else:
                mode = "db_unverified"
                mode_detail = "DB mode enabled, but read path is not yet validated in this process."

    invalid_total = int(sum(int((v or {}).get("invalid_lines") or 0) for v in jsonl_stats.values()))
    return {
        "mode": mode,
        "mode_detail": mode_detail,
        "db_enabled": bool(config.ENABLE_AI_INSIGHTS_DB),
        "active_db_target": _active_db_target(),
        "db_function_availability": fn_availability,
        "ai_insights_dir": str(config.AI_INSIGHTS_DIR),
        "files": {
            "run_logs": str(config.RUN_LOG_FILE),
            "model_metrics": str(config.MODEL_METRICS_FILE),
            "train_events": str(config.TRAIN_EVENTS_FILE),
        },
        "db_write_status": writes,
        "db_read_status": reads,
        "jsonl_read_stats": jsonl_stats,
        "jsonl_invalid_lines_total": invalid_total,
    }


def count_runs_for_key(
    rows: Iterable[Dict[str, Any]],
    *,
    phase: str,
    stage: Optional[str] = None,
    key_name: Optional[str] = None,
    key_value: Optional[str] = None,
) -> int:
    count = 0
    phase_l = str(phase or "").strip().lower()
    stage_l = str(stage or "").strip().lower()
    for r in rows:
        if str(r.get("phase") or "").strip().lower() != phase_l:
            continue
        if stage_l and str(r.get("stage") or "").strip().lower() != stage_l:
            continue
        if key_name and key_value is not None:
            if str(r.get(key_name) or "").strip() != str(key_value):
                continue
        count += 1
    return count
