from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from datamind_console.ai_agent.decision_engine import (
    DecisionResult,
    LoadedModel,
    decide_heuristics,
    decide_model,
)
from datamind_console.ai_agent.escalation import escalate_worker_exception
from datamind_console.db.ai_repo import (
    consume_run_once_now,
    create_ai_suggestion,
    find_existing_open_suggestion,
    get_active_ai_model,
    get_schedule,
)
from datamind_console.db.db import db_conn, fetch_all
from datamind_console.db.console_repo import log_audit_event


@dataclass
class WorkerConfig:
    decider: str
    active_model_version: Optional[str]
    min_confidence: float
    limit: int
    dry_run: bool
    loop: bool

    @staticmethod
    def from_env(*, limit: int = 200, dry_run: bool = False, loop: bool = False) -> "WorkerConfig":
        decider = str(os.getenv("AI_DECIDER", "heuristics") or "heuristics").strip().lower()
        if decider not in {"heuristics", "model"}:
            decider = "heuristics"
        raw_conf = str(os.getenv("AI_MIN_CONFIDENCE", "0.30") or "0.30").strip()
        try:
            min_conf = float(raw_conf)
        except Exception:
            min_conf = 0.30
        min_conf = max(0.0, min(1.0, min_conf))
        model_version = str(os.getenv("AI_ACTIVE_MODEL_VERSION", "") or "").strip() or None
        return WorkerConfig(
            decider=decider,
            active_model_version=model_version,
            min_confidence=min_conf,
            limit=max(1, int(limit)),
            dry_run=bool(dry_run),
            loop=bool(loop),
        )


def _phase_to_int(value: Any) -> Optional[int]:
    try:
        iv = int(str(value).strip())
    except Exception:
        return None
    return iv if 1 <= iv <= 4 else None


def _collect_candidates(*, limit: int) -> list[dict]:
    with db_conn(readonly=True) as conn:
        rows = fetch_all(
            conn,
            """
            WITH base AS (
              SELECT
                phase::text AS phase,
                item_id::text AS entity_id,
                COUNT(*)::int AS total_decisions,
                SUM(CASE WHEN decision = 'APPROVE' THEN 1 ELSE 0 END)::int AS n_approve,
                SUM(CASE WHEN decision = 'REJECT' THEN 1 ELSE 0 END)::int AS n_reject,
                SUM(CASE WHEN decision = 'PUBLISH' THEN 1 ELSE 0 END)::int AS n_publish,
                SUM(CASE WHEN decision = 'EDIT' THEN 1 ELSE 0 END)::int AS n_edit,
                MAX(created_at) AS last_decision_at,
                EXTRACT(EPOCH FROM (NOW() - MAX(created_at))) / 3600.0 AS hours_since_last_decision
              FROM console.phase_decisions
              GROUP BY phase, item_id
            )
            SELECT
              phase,
              'phase_item'::text AS entity_type,
              entity_id,
              total_decisions,
              n_approve,
              n_reject,
              n_publish,
              n_edit,
              COALESCE(hours_since_last_decision, 9999.0) AS hours_since_last_decision,
              last_decision_at
            FROM base
            ORDER BY last_decision_at DESC NULLS LAST
            LIMIT %s
            """,
            (int(limit),),
        )
    return rows


def _resolve_model_row(*, phase: str, cfg: WorkerConfig) -> Optional[dict]:
    if cfg.active_model_version:
        return get_active_ai_model(model_version=cfg.active_model_version)
    return get_active_ai_model(phase=phase)


def _build_feature_snapshot(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": str(candidate.get("phase") or ""),
        "entity_type": str(candidate.get("entity_type") or ""),
        "entity_id": str(candidate.get("entity_id") or ""),
        "total_decisions": int(candidate.get("total_decisions") or 0),
        "n_approve": int(candidate.get("n_approve") or 0),
        "n_reject": int(candidate.get("n_reject") or 0),
        "n_publish": int(candidate.get("n_publish") or 0),
        "n_edit": int(candidate.get("n_edit") or 0),
        "hours_since_last_decision": float(candidate.get("hours_since_last_decision") or 9999.0),
        "last_decision_at": (
            candidate.get("last_decision_at").isoformat()
            if isinstance(candidate.get("last_decision_at"), datetime)
            else candidate.get("last_decision_at")
        ),
    }


def run_learning_worker_once(*, cfg: WorkerConfig) -> dict[str, Any]:
    rows = _collect_candidates(limit=cfg.limit)
    if not rows:
        return {
            "ok": True,
            "decider": cfg.decider,
            "active_model_version": cfg.active_model_version,
            "scanned": 0,
            "created": 0,
            "skipped_existing": 0,
            "skipped_low_confidence": 0,
            "errors": [],
            "last_entities": [],
        }

    created = 0
    skipped_existing = 0
    skipped_low_confidence = 0
    errors: list[dict[str, Any]] = []
    model_cache: dict[str, LoadedModel] = {}
    last_entities: list[dict[str, Any]] = []

    for row in rows:
        phase = str(row.get("phase") or "").strip()
        entity_type = str(row.get("entity_type") or "").strip()
        entity_id = str(row.get("entity_id") or "").strip()
        features = _build_feature_snapshot(row)
        decision: DecisionResult
        mode_used = cfg.decider
        fallback_reason: Optional[str] = None

        if cfg.decider == "model":
            try:
                model_row = _resolve_model_row(phase=phase, cfg=cfg)
                if not model_row:
                    raise RuntimeError("No active model found; using heuristics fallback.")
                model_key = f"{model_row.get('model_name')}::{model_row.get('model_version')}"
                loaded = model_cache.get(model_key)
                if loaded is None:
                    loaded = LoadedModel(
                        model_name=str(model_row.get("model_name") or ""),
                        model_version=str(model_row.get("model_version") or ""),
                        artifact_path=str(model_row.get("artifact_path") or ""),
                    )
                    model_cache[model_key] = loaded
                decision = decide_model(features=features, loaded_model=loaded)
            except Exception as e:
                mode_used = "heuristics_fallback"
                fallback_reason = str(e)
                decision = decide_heuristics(features)
                decision.reason = f"{decision.reason} | fallback_reason={fallback_reason}"
        else:
            decision = decide_heuristics(features)

        last_entities.append(
            {
                "phase": phase,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "suggestion_type": decision.suggestion_type,
                "confidence": float(decision.confidence),
            }
        )
        if len(last_entities) > 25:
            last_entities = last_entities[-25:]

        audit_payload = {
            "mode_requested": cfg.decider,
            "mode_used": mode_used,
            "phase": phase,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "features": features,
            "decision": {
                "suggestion_type": decision.suggestion_type,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "model_name": decision.model_name,
                "model_version": decision.model_version,
                "prediction": decision.prediction,
                "evidence": decision.evidence,
                "fallback_reason": fallback_reason,
            },
            "threshold": cfg.min_confidence,
        }
        log_audit_event(
            user_id=None,
            action="AI_DECISION_EVALUATED",
            phase=_phase_to_int(phase),
            item_id=entity_id,
            payload=audit_payload,
        )

        if float(decision.confidence) < cfg.min_confidence:
            skipped_low_confidence += 1
            continue

        existing = find_existing_open_suggestion(
            phase=phase,
            entity_type=entity_type,
            entity_id=entity_id,
            suggestion_type=decision.suggestion_type,
        )
        if existing:
            skipped_existing += 1
            continue

        if cfg.dry_run:
            created += 1
            continue

        try:
            suggestion = create_ai_suggestion(
                phase=phase,
                entity_type=entity_type,
                entity_id=entity_id,
                suggestion_type=decision.suggestion_type,
                confidence=float(decision.confidence),
                reason=decision.reason,
                evidence={
                    **(decision.evidence or {}),
                    "mode_requested": cfg.decider,
                    "mode_used": mode_used,
                    "min_confidence": cfg.min_confidence,
                },
                features=features,
                model_name=decision.model_name,
                model_version=decision.model_version,
                prediction=decision.prediction,
            )
            created += 1
            log_audit_event(
                user_id=None,
                action="AI_SUGGESTION_CREATED",
                phase=_phase_to_int(phase),
                item_id=entity_id,
                payload={
                    "suggestion_id": suggestion.get("suggestion_id"),
                    "mode_used": mode_used,
                    "suggestion_type": decision.suggestion_type,
                    "confidence": decision.confidence,
                    "model_name": decision.model_name,
                    "model_version": decision.model_version,
                },
            )
        except Exception as e:
            errors.append({"entity_id": entity_id, "error": str(e)})

    return {
        "ok": True,
        "decider": cfg.decider,
        "active_model_version": cfg.active_model_version,
        "scanned": len(rows),
        "created": created,
        "skipped_existing": skipped_existing,
        "skipped_low_confidence": skipped_low_confidence,
        "errors": errors,
        "last_entities": last_entities,
    }


def _env_schedule_enabled_default() -> bool:
    value = str(os.getenv("AI_SCHEDULE_ENABLED", "") or "").strip().lower()
    if not value:
        return False
    return value in {"1", "true", "yes", "y", "on"}


def _load_schedule_safe() -> dict[str, Any]:
    try:
        schedule = get_schedule()
        if schedule:
            return schedule
    except Exception:
        pass
    return {
        "enabled": _env_schedule_enabled_default(),
        "timezone": "America/Guayaquil",
        "days_of_week": [1, 2, 3, 4, 5, 6, 7],
        "start_time_local": datetime.strptime("09:00:00", "%H:%M:%S").time(),
        "end_time_local": datetime.strptime("18:00:00", "%H:%M:%S").time(),
        "interval_seconds": 600,
        "run_once_now": False,
    }


def _schedule_allows_now(schedule: dict[str, Any], now_local: datetime) -> bool:
    days = sorted({int(x) for x in (schedule.get("days_of_week") or []) if 1 <= int(x) <= 7})
    if days and int(now_local.isoweekday()) not in days:
        return False

    st = schedule.get("start_time_local")
    et = schedule.get("end_time_local")
    start_hour = int(getattr(st, "hour", 0))
    start_min = int(getattr(st, "minute", 0))
    end_hour = int(getattr(et, "hour", 23))
    end_min = int(getattr(et, "minute", 59))
    current_minutes = int(now_local.hour * 60 + now_local.minute)
    start_minutes = int(start_hour * 60 + start_min)
    end_minutes = int(end_hour * 60 + end_min)
    return start_minutes <= current_minutes < end_minutes


def _seconds_until_next_window(schedule: dict[str, Any], now_local: datetime) -> int:
    days = sorted({int(x) for x in (schedule.get("days_of_week") or []) if 1 <= int(x) <= 7})
    if not days:
        days = [1, 2, 3, 4, 5, 6, 7]

    st = schedule.get("start_time_local")
    start_hour = int(getattr(st, "hour", 9))
    start_min = int(getattr(st, "minute", 0))

    for delta in range(0, 14):
        d = now_local + timedelta(days=delta)
        if int(d.isoweekday()) not in days:
            continue
        candidate = d.replace(hour=start_hour, minute=start_min, second=0, microsecond=0)
        if candidate > now_local:
            return max(60, int((candidate - now_local).total_seconds()))
    return 60


def run_learning_worker_loop(*, cfg: WorkerConfig) -> None:
    loop_count = 0
    last_result: Optional[dict[str, Any]] = None
    while True:
        loop_count += 1
        schedule = _load_schedule_safe()
        interval_seconds = max(60, int(schedule.get("interval_seconds") or 600))
        tz_name = str(schedule.get("timezone") or "America/Guayaquil")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        now_local = datetime.now(tz)
        run_once_flag = bool(schedule.get("run_once_now"))

        if run_once_flag:
            try:
                out = run_learning_worker_once(cfg=cfg)
                last_result = out
                print(out)
            except Exception as exc:
                esc = escalate_worker_exception(
                    exc=exc,
                    context={
                        "module_names": ["datamind_console.ai_agent.worker"],
                        "loop_count": loop_count,
                        "config": {
                            "decider": cfg.decider,
                            "active_model_version": cfg.active_model_version,
                            "min_confidence": cfg.min_confidence,
                            "limit": cfg.limit,
                            "dry_run": cfg.dry_run,
                        },
                        "schedule": schedule,
                        "now_local": now_local.isoformat(),
                        "last_result": last_result,
                    },
                )
                print({"ok": False, "error": str(exc), "escalation": esc})
            finally:
                try:
                    consume_run_once_now()
                except Exception:
                    pass
            time.sleep(min(60, interval_seconds))
            continue

        if not bool(schedule.get("enabled")):
            time.sleep(60)
            continue

        if not _schedule_allows_now(schedule, now_local):
            sleep_s = min(60, _seconds_until_next_window(schedule, now_local))
            time.sleep(max(15, int(sleep_s)))
            continue

        try:
            out = run_learning_worker_once(cfg=cfg)
            last_result = out
            print(out)
        except Exception as exc:
            esc = escalate_worker_exception(
                exc=exc,
                context={
                    "module_names": ["datamind_console.ai_agent.worker"],
                    "loop_count": loop_count,
                    "config": {
                        "decider": cfg.decider,
                        "active_model_version": cfg.active_model_version,
                        "min_confidence": cfg.min_confidence,
                        "limit": cfg.limit,
                        "dry_run": cfg.dry_run,
                    },
                    "schedule": schedule,
                    "now_local": now_local.isoformat(),
                    "last_result": last_result,
                },
            )
            print({"ok": False, "error": str(exc), "escalation": esc})
            time.sleep(60)
            continue

        time.sleep(interval_seconds)


def main() -> None:
    ap = argparse.ArgumentParser(description="Run AI learning worker.")
    ap.add_argument("--limit", type=int, default=200, help="Max entities to scan from decision history.")
    ap.add_argument("--dry-run", action="store_true", help="Compute decisions but do not insert suggestions.")
    ap.add_argument("--loop", action="store_true", help="Run continuously and honor ai_agent_schedule.")
    args = ap.parse_args()
    cfg = WorkerConfig.from_env(limit=args.limit, dry_run=bool(args.dry_run), loop=bool(args.loop))
    if cfg.loop:
        run_learning_worker_loop(cfg=cfg)
        return
    out = run_learning_worker_once(cfg=cfg)
    print(out)


if __name__ == "__main__":
    main()
