from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from datamind_console.db.db import db_conn, exec_many, exec_sql, fetch_all, fetch_one

SUGGESTION_TYPES = {"approve", "reject", "promote", "review", "investigate"}
SUGGESTION_STATUSES = {"open", "reviewed", "applied", "dismissed"}
HUMAN_LABELS = {"approve", "reject", "promote", "dismiss", "reviewed"}

LABEL_TO_STATUS = {
    "approve": "applied",
    "reject": "applied",
    "promote": "applied",
    "dismiss": "dismissed",
    "reviewed": "reviewed",
}

_AI_SCHEMA_READY = False
_AI_SCHEMA_LOCK = threading.Lock()


def _to_json(value: Optional[dict[str, Any]]) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _as_phase(phase: Any) -> str:
    return str(phase).strip()


def _sql_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "sql"


def _ensure_ai_schema() -> None:
    global _AI_SCHEMA_READY
    if _AI_SCHEMA_READY:
        return
    with _AI_SCHEMA_LOCK:
        if _AI_SCHEMA_READY:
            return
        with db_conn() as conn:
            probe = fetch_one(
                conn,
                """
                SELECT
                  to_regclass('ai.ai_suggestions') IS NOT NULL AS has_suggestions,
                  to_regclass('ai.ai_agent_schedule') IS NOT NULL AS has_schedule,
                  to_regclass('ai.ai_bot_run_logs') IS NOT NULL AS has_bot_logs
                """,
            ) or {}
            if bool(probe.get("has_suggestions")) and bool(probe.get("has_schedule")) and bool(probe.get("has_bot_logs")):
                _AI_SCHEMA_READY = True
                return

            sql_files = [
                _sql_dir() / "004_ai_learning_agent.sql",
                _sql_dir() / "006_ai_insights_bot_logs.sql",
            ]
            with conn.cursor() as cur:
                for path in sql_files:
                    if not path.exists():
                        continue
                    sql = path.read_text(encoding="utf-8")
                    if sql.strip():
                        cur.execute(sql)

                # Keep schedule/escalations bootstrap local to AI schema only.
                # We intentionally avoid executing full 005 migration here because it
                # also touches GTFS schema/tables that may be managed elsewhere.
                cur.execute(
                    """
                    CREATE SCHEMA IF NOT EXISTS ai;

                    CREATE TABLE IF NOT EXISTS ai.ai_agent_schedule (
                      schedule_id INTEGER PRIMARY KEY DEFAULT 1,
                      enabled BOOLEAN NOT NULL DEFAULT FALSE,
                      timezone TEXT NOT NULL DEFAULT 'America/Guayaquil',
                      days_of_week INTEGER[] NOT NULL DEFAULT ARRAY[1,2,3,4,5,6,7],
                      start_time_local TIME NOT NULL DEFAULT TIME '09:00',
                      end_time_local TIME NOT NULL DEFAULT TIME '18:00',
                      interval_seconds INTEGER NOT NULL DEFAULT 600,
                      run_once_now BOOLEAN NOT NULL DEFAULT FALSE,
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      updated_by TEXT NULL
                    );

                    INSERT INTO ai.ai_agent_schedule (
                      schedule_id,
                      enabled,
                      timezone,
                      days_of_week,
                      start_time_local,
                      end_time_local,
                      interval_seconds,
                      run_once_now,
                      updated_by
                    )
                    VALUES
                      (1, FALSE, 'America/Guayaquil', ARRAY[1,2,3,4,5,6,7], TIME '09:00', TIME '18:00', 600, FALSE, NULL)
                    ON CONFLICT (schedule_id) DO NOTHING;

                    CREATE TABLE IF NOT EXISTS ai.ai_escalations (
                      escalation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'sent', 'resolved', 'failed')),
                      error_type TEXT NOT NULL,
                      error_message TEXT NOT NULL,
                      stacktrace TEXT NOT NULL,
                      context JSONB NOT NULL DEFAULT '{}'::jsonb,
                      codex_request JSONB NULL,
                      codex_response JSONB NULL,
                      resolution_notes TEXT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_ai_escalations_status_created
                      ON ai.ai_escalations(status, created_at DESC);

                    CREATE INDEX IF NOT EXISTS idx_ai_escalations_created
                      ON ai.ai_escalations(created_at DESC);
                    """
                )
        _AI_SCHEMA_READY = True


def _ai_conn(*, readonly: bool = False):
    _ensure_ai_schema()
    return db_conn(readonly=readonly)


def create_ai_suggestion(
    *,
    phase: Any,
    entity_type: str,
    entity_id: Any,
    suggestion_type: str,
    confidence: float,
    reason: str,
    evidence: Optional[dict[str, Any]] = None,
    features: Optional[dict[str, Any]] = None,
    status: str = "open",
    model_name: Optional[str] = None,
    model_version: Optional[str] = None,
    prediction: Optional[dict[str, Any]] = None,
) -> dict:
    s_type = str(suggestion_type or "").strip().lower()
    if s_type not in SUGGESTION_TYPES:
        raise ValueError(f"Unsupported suggestion_type: {suggestion_type}")
    s_status = str(status or "").strip().lower()
    if s_status not in SUGGESTION_STATUSES:
        raise ValueError(f"Unsupported status: {status}")
    conf = max(0.0, min(1.0, float(confidence or 0.0)))
    with _ai_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO ai.ai_suggestions
              (phase, entity_type, entity_id, suggestion_type, confidence, reason,
               evidence, features, status, model_name, model_version, prediction)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb)
            RETURNING
              suggestion_id::text AS suggestion_id,
              created_at,
              phase,
              entity_type,
              entity_id,
              suggestion_type,
              confidence,
              reason,
              evidence,
              features,
              status,
              model_name,
              model_version,
              prediction
            """,
            (
                _as_phase(phase),
                str(entity_type or "").strip(),
                str(entity_id or "").strip(),
                s_type,
                conf,
                str(reason or "").strip(),
                _to_json(evidence),
                _to_json(features),
                s_status,
                (str(model_name).strip() if model_name else None),
                (str(model_version).strip() if model_version else None),
                (_to_json(prediction) if prediction is not None else None),
            ),
        )
    return row or {}


def find_existing_open_suggestion(
    *,
    phase: Any,
    entity_type: str,
    entity_id: Any,
    suggestion_type: Optional[str] = None,
) -> Optional[dict]:
    clauses = [
        "phase = %s",
        "entity_type = %s",
        "entity_id = %s",
        "status IN ('open', 'reviewed')",
    ]
    params: list[Any] = [_as_phase(phase), str(entity_type or "").strip(), str(entity_id or "").strip()]
    if suggestion_type:
        clauses.append("suggestion_type = %s")
        params.append(str(suggestion_type or "").strip().lower())
    with _ai_conn(readonly=True) as conn:
        return fetch_one(
            conn,
            f"""
            SELECT
              suggestion_id::text AS suggestion_id,
              created_at,
              phase,
              entity_type,
              entity_id,
              suggestion_type,
              confidence,
              reason,
              evidence,
              features,
              status,
              model_name,
              model_version,
              prediction
            FROM ai.ai_suggestions
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT 1
            """,
            tuple(params),
        )


def get_ai_suggestion(suggestion_id: str) -> Optional[dict]:
    with _ai_conn(readonly=True) as conn:
        return fetch_one(
            conn,
            """
            SELECT
              suggestion_id::text AS suggestion_id,
              created_at,
              phase,
              entity_type,
              entity_id,
              suggestion_type,
              confidence,
              reason,
              evidence,
              features,
              status,
              model_name,
              model_version,
              prediction
            FROM ai.ai_suggestions
            WHERE suggestion_id = %s
            """,
            (suggestion_id,),
        )


def list_ai_suggestions(
    *,
    phase: Optional[Any] = None,
    status: Optional[str] = None,
    suggestion_type: Optional[str] = None,
    entity_type: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if phase is not None and str(phase).strip().lower() != "all":
        clauses.append("phase = %s")
        params.append(_as_phase(phase))
    if status and str(status).strip().lower() != "all":
        clauses.append("status = %s")
        params.append(str(status).strip().lower())
    if suggestion_type and str(suggestion_type).strip().lower() != "all":
        clauses.append("suggestion_type = %s")
        params.append(str(suggestion_type).strip().lower())
    if entity_type and str(entity_type).strip().lower() != "all":
        clauses.append("entity_type = %s")
        params.append(str(entity_type).strip())
    params.append(max(1, int(limit)))
    with _ai_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            f"""
            SELECT
              suggestion_id::text AS suggestion_id,
              created_at,
              phase,
              entity_type,
              entity_id,
              suggestion_type,
              confidence,
              reason,
              evidence,
              features,
              status,
              model_name,
              model_version,
              prediction
            FROM ai.ai_suggestions
            WHERE {' AND '.join(clauses)}
            ORDER BY
              CASE status
                WHEN 'open' THEN 0
                WHEN 'reviewed' THEN 1
                WHEN 'applied' THEN 2
                ELSE 3
              END,
              created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )


def create_ai_label_event(
    *,
    suggestion_id: str,
    human_label: str,
    actor: str,
    notes: Optional[str] = None,
    ui_context: Optional[dict[str, Any]] = None,
    update_status: bool = True,
) -> dict:
    label = str(human_label or "").strip().lower()
    if label not in HUMAN_LABELS:
        raise ValueError(f"Unsupported human_label: {human_label}")
    actor_name = str(actor or "").strip() or "unknown"
    with _ai_conn() as conn:
        suggestion = fetch_one(
            conn,
            """
            SELECT
              suggestion_id,
              phase,
              entity_type,
              entity_id,
              status
            FROM ai.ai_suggestions
            WHERE suggestion_id = %s
            FOR UPDATE
            """,
            (suggestion_id,),
        )
        if not suggestion:
            raise ValueError(f"Suggestion not found: {suggestion_id}")

        event = fetch_one(
            conn,
            """
            INSERT INTO ai.ai_label_events
              (suggestion_id, phase, entity_type, entity_id, human_label, actor, notes, ui_context)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING
              label_event_id::text AS label_event_id,
              created_at,
              suggestion_id::text AS suggestion_id,
              phase,
              entity_type,
              entity_id,
              human_label,
              actor,
              notes,
              ui_context
            """,
            (
                suggestion_id,
                suggestion.get("phase"),
                suggestion.get("entity_type"),
                suggestion.get("entity_id"),
                label,
                actor_name,
                notes,
                _to_json(ui_context),
            ),
        )

        new_status = LABEL_TO_STATUS.get(label, "reviewed")
        if update_status and new_status != suggestion.get("status"):
            exec_sql(
                conn,
                """
                UPDATE ai.ai_suggestions
                SET status = %s
                WHERE suggestion_id = %s
                """,
                (new_status, suggestion_id),
            )

        updated = fetch_one(
            conn,
            """
            SELECT
              suggestion_id::text AS suggestion_id,
              created_at,
              phase,
              entity_type,
              entity_id,
              suggestion_type,
              confidence,
              reason,
              evidence,
              features,
              status,
              model_name,
              model_version,
              prediction
            FROM ai.ai_suggestions
            WHERE suggestion_id = %s
            """,
            (suggestion_id,),
        )
    return {"label_event": (event or {}), "suggestion": (updated or {})}


def list_ai_label_counts(*, phase: Optional[Any] = None) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if phase is not None and str(phase).strip().lower() != "all":
        clauses.append("phase = %s")
        params.append(_as_phase(phase))
    with _ai_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            f"""
            SELECT
              human_label,
              COUNT(*)::bigint AS n
            FROM ai.ai_label_events
            WHERE {' AND '.join(clauses)}
            GROUP BY human_label
            ORDER BY human_label
            """,
            tuple(params),
        )


def list_ai_model_registry(*, phase: Optional[Any] = None, active_only: bool = False, limit: int = 50) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if phase is not None and str(phase).strip().lower() != "all":
        clauses.append("(phase = %s OR phase IS NULL)")
        params.append(_as_phase(phase))
    if active_only:
        clauses.append("is_active = TRUE")
    params.append(max(1, int(limit)))
    with _ai_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            f"""
            SELECT
              model_id::text AS model_id,
              created_at,
              phase,
              model_name,
              model_version,
              artifact_path,
              metrics,
              is_active,
              trained_on
            FROM ai.ai_model_registry
            WHERE {' AND '.join(clauses)}
            ORDER BY is_active DESC, created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )


def get_active_ai_model(*, phase: Optional[Any] = None, model_version: Optional[str] = None) -> Optional[dict]:
    if model_version:
        with _ai_conn(readonly=True) as conn:
            return fetch_one(
                conn,
                """
                SELECT
                  model_id::text AS model_id,
                  created_at,
                  phase,
                  model_name,
                  model_version,
                  artifact_path,
                  metrics,
                  is_active,
                  trained_on
                FROM ai.ai_model_registry
                WHERE model_version = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(model_version).strip(),),
            )

    with _ai_conn(readonly=True) as conn:
        if phase is None or str(phase).strip().lower() == "all":
            return fetch_one(
                conn,
                """
                SELECT
                  model_id::text AS model_id,
                  created_at,
                  phase,
                  model_name,
                  model_version,
                  artifact_path,
                  metrics,
                  is_active,
                  trained_on
                FROM ai.ai_model_registry
                WHERE is_active = TRUE
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        return fetch_one(
            conn,
            """
            SELECT
              model_id::text AS model_id,
              created_at,
              phase,
              model_name,
              model_version,
              artifact_path,
              metrics,
              is_active,
              trained_on
            FROM ai.ai_model_registry
            WHERE is_active = TRUE
              AND (phase = %s OR phase IS NULL)
            ORDER BY
              CASE WHEN phase = %s THEN 0 ELSE 1 END,
              created_at DESC
            LIMIT 1
            """,
            (_as_phase(phase), _as_phase(phase)),
        )


def register_ai_model(
    *,
    phase: Optional[Any],
    model_name: str,
    model_version: str,
    artifact_path: str,
    metrics: Optional[dict[str, Any]] = None,
    trained_on: Optional[dict[str, Any]] = None,
    activate: bool = False,
) -> dict:
    m_name = str(model_name or "").strip()
    m_version = str(model_version or "").strip()
    if not m_name:
        raise ValueError("model_name is required")
    if not m_version:
        raise ValueError("model_version is required")

    phase_value = None
    if phase is not None and str(phase).strip():
        phase_value = _as_phase(phase)

    with _ai_conn() as conn:
        if activate:
            exec_sql(
                conn,
                """
                UPDATE ai.ai_model_registry
                SET is_active = FALSE
                WHERE COALESCE(phase, '') = COALESCE(%s, '')
                  AND model_name = %s
                  AND is_active = TRUE
                """,
                (phase_value, m_name),
            )

        existing = fetch_one(
            conn,
            """
            SELECT model_id
            FROM ai.ai_model_registry
            WHERE COALESCE(phase, '') = COALESCE(%s, '')
              AND model_name = %s
              AND model_version = %s
            """,
            (phase_value, m_name, m_version),
        )

        if existing and existing.get("model_id"):
            row = fetch_one(
                conn,
                """
                UPDATE ai.ai_model_registry
                SET
                  artifact_path = %s,
                  metrics = %s::jsonb,
                  is_active = %s,
                  trained_on = %s::jsonb
                WHERE model_id = %s
                RETURNING
                  model_id::text AS model_id,
                  created_at,
                  phase,
                  model_name,
                  model_version,
                  artifact_path,
                  metrics,
                  is_active,
                  trained_on
                """,
                (
                    str(artifact_path or "").strip(),
                    _to_json(metrics),
                    bool(activate),
                    _to_json(trained_on),
                    existing["model_id"],
                ),
            )
        else:
            row = fetch_one(
                conn,
                """
                INSERT INTO ai.ai_model_registry
                  (phase, model_name, model_version, artifact_path, metrics, is_active, trained_on)
                VALUES
                  (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb)
                RETURNING
                  model_id::text AS model_id,
                  created_at,
                  phase,
                  model_name,
                  model_version,
                  artifact_path,
                  metrics,
                  is_active,
                  trained_on
                """,
                (
                    phase_value,
                    m_name,
                    m_version,
                    str(artifact_path or "").strip(),
                    _to_json(metrics),
                    bool(activate),
                    _to_json(trained_on),
                ),
            )
    return row or {}


def replace_ai_training_dataset(
    *,
    rows: list[tuple],
    phase: Optional[Any] = None,
) -> int:
    with _ai_conn() as conn:
        if phase is None or str(phase).strip().lower() == "all":
            exec_sql(conn, "DELETE FROM ai.ai_training_dataset")
        else:
            exec_sql(conn, "DELETE FROM ai.ai_training_dataset WHERE phase = %s", (_as_phase(phase),))

        if not rows:
            return 0

        exec_many(
            conn,
            """
            INSERT INTO ai.ai_training_dataset
              (created_at, phase, entity_type, entity_id, features, label, source_suggestion_id)
            VALUES
              (%s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            rows,
        )
        return len(rows)


def _ensure_default_schedule(conn) -> None:
    exec_sql(
        conn,
        """
        INSERT INTO ai.ai_agent_schedule
          (schedule_id, enabled, timezone, days_of_week, start_time_local, end_time_local, interval_seconds, run_once_now, updated_by)
        VALUES
          (1, FALSE, 'America/Guayaquil', ARRAY[1,2,3,4,5,6,7], TIME '09:00', TIME '18:00', 600, FALSE, NULL)
        ON CONFLICT (schedule_id) DO NOTHING
        """,
    )


def get_schedule() -> dict:
    with _ai_conn() as conn:
        _ensure_default_schedule(conn)
        row = fetch_one(
            conn,
            """
            SELECT
              schedule_id,
              enabled,
              timezone,
              days_of_week,
              start_time_local,
              end_time_local,
              interval_seconds,
              run_once_now,
              updated_at,
              updated_by
            FROM ai.ai_agent_schedule
            WHERE schedule_id = 1
            """,
        )
    return row or {}


def upsert_schedule(
    *,
    enabled: bool,
    timezone: str,
    days_of_week: list[int],
    start_time_local: str,
    end_time_local: str,
    interval_seconds: int,
    updated_by: Optional[str] = None,
    run_once_now: Optional[bool] = None,
) -> dict:
    days = sorted({int(x) for x in (days_of_week or []) if 1 <= int(x) <= 7})
    if not days:
        days = [1, 2, 3, 4, 5, 6, 7]
    tz = str(timezone or "").strip() or "America/Guayaquil"
    by = str(updated_by or "").strip() or None
    with _ai_conn() as conn:
        _ensure_default_schedule(conn)
        current = fetch_one(
            conn,
            "SELECT run_once_now FROM ai.ai_agent_schedule WHERE schedule_id = 1",
        ) or {}
        effective_run_once = bool(run_once_now) if run_once_now is not None else bool(current.get("run_once_now"))
        row = fetch_one(
            conn,
            """
            UPDATE ai.ai_agent_schedule
            SET
              enabled = %s,
              timezone = %s,
              days_of_week = %s::int[],
              start_time_local = %s::time,
              end_time_local = %s::time,
              interval_seconds = %s,
              run_once_now = %s,
              updated_at = NOW(),
              updated_by = %s
            WHERE schedule_id = 1
            RETURNING
              schedule_id,
              enabled,
              timezone,
              days_of_week,
              start_time_local,
              end_time_local,
              interval_seconds,
              run_once_now,
              updated_at,
              updated_by
            """,
            (
                bool(enabled),
                tz,
                days,
                str(start_time_local),
                str(end_time_local),
                int(interval_seconds),
                bool(effective_run_once),
                by,
            ),
        )
    return row or {}


def consume_run_once_now() -> bool:
    with _ai_conn() as conn:
        _ensure_default_schedule(conn)
        row = fetch_one(
            conn,
            """
            UPDATE ai.ai_agent_schedule
            SET run_once_now = FALSE,
                updated_at = NOW()
            WHERE schedule_id = 1
              AND run_once_now = TRUE
            RETURNING schedule_id
            """,
        )
    return bool(row)


def create_ai_escalation(
    *,
    error_type: str,
    error_message: str,
    stacktrace: str,
    context: Optional[dict[str, Any]] = None,
    status: str = "open",
    codex_request: Optional[dict[str, Any]] = None,
    codex_response: Optional[dict[str, Any]] = None,
    resolution_notes: Optional[str] = None,
) -> dict:
    with _ai_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO ai.ai_escalations
              (status, error_type, error_message, stacktrace, context, codex_request, codex_response, resolution_notes)
            VALUES
              (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)
            RETURNING
              escalation_id::text AS escalation_id,
              created_at,
              status,
              error_type,
              error_message,
              stacktrace,
              context,
              codex_request,
              codex_response,
              resolution_notes
            """,
            (
                str(status or "open").strip().lower(),
                str(error_type or "").strip() or "RuntimeError",
                str(error_message or "").strip(),
                str(stacktrace or "").strip(),
                _to_json(context),
                (_to_json(codex_request) if codex_request is not None else None),
                (_to_json(codex_response) if codex_response is not None else None),
                resolution_notes,
            ),
        )
    return row or {}


def update_ai_escalation(
    *,
    escalation_id: str,
    status: Optional[str] = None,
    codex_request: Optional[dict[str, Any]] = None,
    codex_response: Optional[dict[str, Any]] = None,
    resolution_notes: Optional[str] = None,
) -> dict:
    with _ai_conn() as conn:
        row = fetch_one(
            conn,
            """
            UPDATE ai.ai_escalations
            SET
              status = COALESCE(%s, status),
              codex_request = COALESCE(%s::jsonb, codex_request),
              codex_response = COALESCE(%s::jsonb, codex_response),
              resolution_notes = COALESCE(%s, resolution_notes)
            WHERE escalation_id = %s
            RETURNING
              escalation_id::text AS escalation_id,
              created_at,
              status,
              error_type,
              error_message,
              stacktrace,
              context,
              codex_request,
              codex_response,
              resolution_notes
            """,
            (
                (str(status).strip().lower() if status else None),
                (_to_json(codex_request) if codex_request is not None else None),
                (_to_json(codex_response) if codex_response is not None else None),
                resolution_notes,
                escalation_id,
            ),
        )
    return row or {}


def get_ai_escalation(escalation_id: str) -> Optional[dict]:
    with _ai_conn(readonly=True) as conn:
        return fetch_one(
            conn,
            """
            SELECT
              escalation_id::text AS escalation_id,
              created_at,
              status,
              error_type,
              error_message,
              stacktrace,
              context,
              codex_request,
              codex_response,
              resolution_notes
            FROM ai.ai_escalations
            WHERE escalation_id = %s
            """,
            (escalation_id,),
        )


def list_ai_escalations(*, status: Optional[str] = None, limit: int = 50) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if status and str(status).strip().lower() != "all":
        clauses.append("status = %s")
        params.append(str(status).strip().lower())
    params.append(max(1, int(limit)))
    with _ai_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            f"""
            SELECT
              escalation_id::text AS escalation_id,
              created_at,
              status,
              error_type,
              error_message,
              context,
              resolution_notes
            FROM ai.ai_escalations
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )


def create_ai_bot_run_log(
    *,
    phase: str,
    stage: Optional[str] = None,
    event_type: str = "run",
    status: str = "success",
    run_id: Optional[str] = None,
    node_set_id: Optional[str] = None,
    route_id: Optional[str] = None,
    service_route_id: Optional[str] = None,
    direction_id: Optional[int] = None,
    quality_score: Optional[float] = None,
    sequence_quality_score: Optional[float] = None,
    reorder_recommended: Optional[bool] = None,
    reorder_confidence: Optional[float] = None,
    warnings: Optional[list[str]] = None,
    notes: Optional[list[str]] = None,
    payload: Optional[dict[str, Any]] = None,
    created_at: Optional[str] = None,
) -> dict:
    with _ai_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO ai.ai_bot_run_logs
              (created_at, phase, stage, event_type, status, run_id, node_set_id, route_id,
               service_route_id, direction_id, quality_score, sequence_quality_score,
               reorder_recommended, reorder_confidence, warnings, notes, payload)
            VALUES
              (COALESCE(%s::timestamptz, NOW()), %s, %s, %s, %s, %s, %s, %s,
               %s, %s, %s, %s, %s, %s, COALESCE(%s::text[], ARRAY[]::text[]),
               COALESCE(%s::text[], ARRAY[]::text[]), %s::jsonb)
            RETURNING
              log_id,
              created_at,
              phase,
              stage,
              event_type,
              status,
              run_id,
              node_set_id,
              route_id,
              service_route_id,
              direction_id,
              quality_score,
              sequence_quality_score,
              reorder_recommended,
              reorder_confidence,
              warnings,
              notes,
              payload
            """,
            (
                created_at,
                str(phase or "").strip().lower(),
                (str(stage).strip() if stage else None),
                str(event_type or "run").strip().lower(),
                str(status or "success").strip().lower(),
                (str(run_id).strip() if run_id else None),
                (str(node_set_id).strip() if node_set_id else None),
                (str(route_id).strip() if route_id else None),
                (str(service_route_id).strip() if service_route_id else None),
                (int(direction_id) if direction_id is not None else None),
                (float(quality_score) if quality_score is not None else None),
                (float(sequence_quality_score) if sequence_quality_score is not None else None),
                (bool(reorder_recommended) if reorder_recommended is not None else None),
                (float(reorder_confidence) if reorder_confidence is not None else None),
                ([str(x) for x in (warnings or [])] or None),
                ([str(x) for x in (notes or [])] or None),
                _to_json(payload),
            ),
        )
    return row or {}


def list_ai_bot_run_logs(
    *,
    phase: Optional[str] = None,
    event_type: Optional[str] = None,
    stage: Optional[str] = None,
    limit: int = 5000,
) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if phase:
        clauses.append("phase = %s")
        params.append(str(phase).strip().lower())
    if event_type:
        clauses.append("event_type = %s")
        params.append(str(event_type).strip().lower())
    if stage:
        clauses.append("stage = %s")
        params.append(str(stage).strip())
    params.append(max(1, int(limit)))
    with _ai_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            f"""
            SELECT
              log_id,
              created_at AS timestamp,
              phase,
              stage,
              event_type,
              status,
              run_id,
              node_set_id,
              route_id,
              service_route_id,
              direction_id,
              quality_score,
              sequence_quality_score,
              reorder_recommended,
              reorder_confidence,
              warnings,
              notes,
              payload
            FROM ai.ai_bot_run_logs
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )


def create_ai_bot_model_metric(
    *,
    task: str,
    event_type: str = "eval",
    status: str = "success",
    ok: bool = True,
    metric_primary_name: Optional[str] = None,
    metric_primary_value: Optional[float] = None,
    metric_higher_better: Optional[bool] = None,
    payload: Optional[dict[str, Any]] = None,
    created_at: Optional[str] = None,
) -> dict:
    with _ai_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO ai.ai_bot_model_metrics
              (created_at, task, event_type, status, ok, metric_primary_name,
               metric_primary_value, metric_higher_better, payload)
            VALUES
              (COALESCE(%s::timestamptz, NOW()), %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING
              metric_id,
              created_at AS timestamp,
              task,
              event_type,
              status,
              ok,
              metric_primary_name,
              metric_primary_value,
              metric_higher_better,
              payload
            """,
            (
                created_at,
                str(task or "").strip(),
                str(event_type or "eval").strip().lower(),
                str(status or "success").strip().lower(),
                bool(ok),
                (str(metric_primary_name).strip() if metric_primary_name else None),
                (float(metric_primary_value) if metric_primary_value is not None else None),
                (bool(metric_higher_better) if metric_higher_better is not None else None),
                _to_json(payload),
            ),
        )
    return row or {}


def list_ai_bot_model_metrics(
    *,
    task: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 5000,
) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if task:
        clauses.append("task = %s")
        params.append(str(task).strip())
    if event_type:
        clauses.append("event_type = %s")
        params.append(str(event_type).strip().lower())
    params.append(max(1, int(limit)))
    with _ai_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            f"""
            SELECT
              metric_id,
              created_at AS timestamp,
              task,
              event_type,
              status,
              ok,
              metric_primary_name,
              metric_primary_value,
              metric_higher_better,
              payload
            FROM ai.ai_bot_model_metrics
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )


def create_ai_bot_train_event(
    *,
    task: str,
    event_type: str = "manual",
    status: str = "success",
    ok: bool = True,
    payload: Optional[dict[str, Any]] = None,
    created_at: Optional[str] = None,
) -> dict:
    with _ai_conn() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO ai.ai_bot_train_events
              (created_at, task, event_type, status, ok, payload)
            VALUES
              (COALESCE(%s::timestamptz, NOW()), %s, %s, %s, %s, %s::jsonb)
            RETURNING
              event_id,
              created_at AS timestamp,
              task,
              event_type,
              status,
              ok,
              payload
            """,
            (
                created_at,
                str(task or "").strip(),
                str(event_type or "manual").strip().lower(),
                str(status or "success").strip().lower(),
                bool(ok),
                _to_json(payload),
            ),
        )
    return row or {}


def list_ai_bot_train_events(
    *,
    task: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 5000,
) -> list[dict]:
    clauses = ["1=1"]
    params: list[Any] = []
    if task:
        clauses.append("task = %s")
        params.append(str(task).strip())
    if event_type:
        clauses.append("event_type = %s")
        params.append(str(event_type).strip().lower())
    params.append(max(1, int(limit)))
    with _ai_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            f"""
            SELECT
              event_id,
              created_at AS timestamp,
              task,
              event_type,
              status,
              ok,
              payload
            FROM ai.ai_bot_train_events
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
