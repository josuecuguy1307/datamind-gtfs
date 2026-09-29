from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from datamind_console.db.db import db_conn, fetch_all, fetch_one


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AutopilotDBStore:
    """DB persistence backend for supervised pipeline autopilot state and audit data."""

    def __init__(self, *, schema: str = "console", enabled: bool = True) -> None:
        self.schema = str(schema or "console")
        self.enabled = bool(enabled)
        self.t_runs = f"{self.schema}.pipeline_autopilot_runs"
        self.t_steps = f"{self.schema}.pipeline_autopilot_step_attempts"
        self.t_events = f"{self.schema}.pipeline_autopilot_events"
        self.t_approvals = f"{self.schema}.pipeline_autopilot_approvals"
        self.t_idempotency = f"{self.schema}.pipeline_autopilot_idempotency"
        self.t_queue = f"{self.schema}.pipeline_autopilot_queue"
        self.t_alerts = f"{self.schema}.pipeline_autopilot_alerts"
        self.t_patch_registry = f"{self.schema}.pipeline_autopilot_patch_registry"
        self._available_checked = False
        self._available = False
        self._patch_registry_checked = False
        self._patch_registry_available = False

    @property
    def available(self) -> bool:
        if not self.enabled:
            return False
        if self._available_checked:
            return bool(self._available)
        self._available_checked = True
        self._available = self._check_tables_exist()
        return bool(self._available)

    def _check_tables_exist(self) -> bool:
        try:
            with db_conn(readonly=True) as conn:
                row = fetch_one(
                    conn,
                    """
                    SELECT
                      to_regclass(%s) IS NOT NULL AS runs_ok,
                      to_regclass(%s) IS NOT NULL AS steps_ok,
                      to_regclass(%s) IS NOT NULL AS events_ok,
                      to_regclass(%s) IS NOT NULL AS approvals_ok,
                      to_regclass(%s) IS NOT NULL AS idem_ok,
                      to_regclass(%s) IS NOT NULL AS queue_ok,
                      to_regclass(%s) IS NOT NULL AS alerts_ok
                    """,
                    (
                        self.t_runs,
                        self.t_steps,
                        self.t_events,
                        self.t_approvals,
                        self.t_idempotency,
                        self.t_queue,
                        self.t_alerts,
                    ),
                )
            row = row or {}
            return bool(
                row.get("runs_ok")
                and row.get("steps_ok")
                and row.get("events_ok")
                and row.get("approvals_ok")
                and row.get("idem_ok")
                and row.get("queue_ok")
                and row.get("alerts_ok")
            )
        except Exception:
            return False

    def _has_patch_registry_table(self) -> bool:
        if not self.available:
            return False
        if self._patch_registry_checked:
            return bool(self._patch_registry_available)
        self._patch_registry_checked = True
        try:
            with db_conn(readonly=True) as conn:
                row = fetch_one(
                    conn,
                    "SELECT to_regclass(%s) IS NOT NULL AS patch_ok",
                    (self.t_patch_registry,),
                )
            self._patch_registry_available = bool((row or {}).get("patch_ok"))
        except Exception:
            self._patch_registry_available = False
        return bool(self._patch_registry_available)

    # ------------------------------------------------------------------
    # Run/session persistence
    # ------------------------------------------------------------------
    def upsert_run_state(self, state: Dict[str, Any]) -> None:
        if not self.available:
            return
        payload = dict(state or {})
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            return

        with db_conn() as conn:
            fetch_one(
                conn,
                f"""
                INSERT INTO {self.t_runs}
                  (run_id, trace_id, status, policy_profile, current_phase, current_step_id,
                   pipeline_scope, operator_context, attempt_counters, diversion_stack,
                   resume_context, artifacts, state_json, started_at, updated_at, completed_at)
                VALUES
                  (%s, %s, %s, %s, %s, %s,
                   %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                   %s::jsonb, %s::jsonb, %s::jsonb, %s::timestamptz, %s::timestamptz, %s::timestamptz)
                ON CONFLICT (run_id) DO UPDATE SET
                  trace_id = EXCLUDED.trace_id,
                  status = EXCLUDED.status,
                  policy_profile = EXCLUDED.policy_profile,
                  current_phase = EXCLUDED.current_phase,
                  current_step_id = EXCLUDED.current_step_id,
                  pipeline_scope = EXCLUDED.pipeline_scope,
                  operator_context = EXCLUDED.operator_context,
                  attempt_counters = EXCLUDED.attempt_counters,
                  diversion_stack = EXCLUDED.diversion_stack,
                  resume_context = EXCLUDED.resume_context,
                  artifacts = EXCLUDED.artifacts,
                  state_json = EXCLUDED.state_json,
                  started_at = COALESCE({self.t_runs}.started_at, EXCLUDED.started_at),
                  updated_at = EXCLUDED.updated_at,
                  completed_at = EXCLUDED.completed_at
                RETURNING run_id
                """,
                (
                    run_id,
                    str(payload.get("trace_id") or ""),
                    str(payload.get("status") or ""),
                    str(payload.get("policy_profile") or "balanced"),
                    payload.get("current_phase"),
                    payload.get("current_step_id"),
                    _to_json(payload.get("pipeline_scope") or {}),
                    _to_json(payload.get("operator_context") or {}),
                    _to_json(payload.get("attempt_counters") or {}),
                    _to_json(payload.get("diversion_stack") or []),
                    _to_json(payload.get("resume_context") or {}),
                    _to_json(payload.get("artifacts") or {}),
                    _to_json(payload),
                    payload.get("started_at"),
                    payload.get("updated_at") or _utc_now_iso(),
                    payload.get("completed_at"),
                ),
            )

    def load_run_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        with db_conn(readonly=True) as conn:
            row = fetch_one(
                conn,
                f"""
                SELECT state_json
                FROM {self.t_runs}
                WHERE run_id = %s
                """,
                (str(run_id),),
            )
        if not row:
            return None
        value = row.get("state_json")
        return dict(value or {}) if isinstance(value, dict) else None

    def list_run_states(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        with db_conn(readonly=True) as conn:
            rows = fetch_all(
                conn,
                f"""
                SELECT state_json
                FROM {self.t_runs}
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (int(limit),),
            )
        out: List[Dict[str, Any]] = []
        for row in rows:
            obj = row.get("state_json") if isinstance(row, dict) else None
            if isinstance(obj, dict):
                out.append(dict(obj))
        return out

    # ------------------------------------------------------------------
    # Event / attempt / approval persistence
    # ------------------------------------------------------------------
    def insert_event(self, event: Dict[str, Any]) -> None:
        if not self.available:
            return
        row = dict(event or {})
        with db_conn() as conn:
            fetch_one(
                conn,
                f"""
                INSERT INTO {self.t_events}
                  (event_id, run_id, trace_id, event_type, phase, step_id, correlation_id,
                   event_payload, event_ts)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::timestamptz)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                (
                    str(row.get("event_id") or ""),
                    str(row.get("run_id") or ""),
                    str(row.get("trace_id") or ""),
                    str(row.get("event_type") or ""),
                    row.get("phase"),
                    row.get("step_id"),
                    str(row.get("correlation_id") or ""),
                    _to_json(row.get("payload") or {}),
                    row.get("timestamp") or _utc_now_iso(),
                ),
            )

    def insert_step_attempt(self, record: Dict[str, Any], *, trace_id: str) -> None:
        if not self.available:
            return
        row = dict(record or {})
        with db_conn() as conn:
            fetch_one(
                conn,
                f"""
                INSERT INTO {self.t_steps}
                  (run_id, trace_id, phase, step_id, attempt_no, status,
                   executor_result_summary, validator_result, ai_bot_snapshot,
                   chatgpt_snapshot, block_reason, artifacts, timings, created_at,
                   idempotency_key)
                VALUES
                  (%s, %s, %s, %s, %s, %s,
                   %s::jsonb, %s::jsonb, %s::jsonb,
                   %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::timestamptz,
                   %s)
                ON CONFLICT (run_id, step_id, attempt_no, status, created_at) DO NOTHING
                RETURNING record_id
                """,
                (
                    str(row.get("run_id") or ""),
                    str(trace_id or ""),
                    str(row.get("phase") or ""),
                    str(row.get("step_id") or ""),
                    int(row.get("attempt_no") or 0),
                    str(row.get("status") or ""),
                    _to_json(row.get("executor_result_summary") or {}),
                    _to_json(row.get("validator_result") or {}),
                    _to_json(row.get("ai_bot_snapshot") or {}),
                    _to_json(row.get("chatgpt_snapshot") or {}),
                    _to_json(row.get("block_reason") or {}),
                    _to_json(row.get("artifacts") or []),
                    _to_json(row.get("timings") or {}),
                    row.get("created_at") or _utc_now_iso(),
                    (str(row.get("idempotency_key")) if row.get("idempotency_key") else None),
                ),
            )

    def upsert_approval(self, approval: Dict[str, Any], *, trace_id: str) -> None:
        if not self.available:
            return
        row = dict(approval or {})
        with db_conn() as conn:
            fetch_one(
                conn,
                f"""
                INSERT INTO {self.t_approvals}
                  (approval_id, run_id, trace_id, phase, step_id, approval_type,
                   status, created_at, created_by_system, evidence_payload, risk_summary,
                   recommended_action, operator_decision, operator_id, operator_role, decision_at,
                   decision_signature)
                VALUES
                  (%s, %s, %s, %s, %s, %s,
                   %s, %s::timestamptz, %s, %s::jsonb, %s,
                   %s, %s, %s, %s, %s::timestamptz,
                   %s)
                ON CONFLICT (approval_id) DO UPDATE SET
                  run_id = EXCLUDED.run_id,
                  trace_id = EXCLUDED.trace_id,
                  phase = EXCLUDED.phase,
                  step_id = EXCLUDED.step_id,
                  approval_type = EXCLUDED.approval_type,
                  status = EXCLUDED.status,
                  created_at = EXCLUDED.created_at,
                  created_by_system = EXCLUDED.created_by_system,
                  evidence_payload = EXCLUDED.evidence_payload,
                  risk_summary = EXCLUDED.risk_summary,
                  recommended_action = EXCLUDED.recommended_action,
                  operator_decision = EXCLUDED.operator_decision,
                  operator_id = EXCLUDED.operator_id,
                  operator_role = EXCLUDED.operator_role,
                  decision_at = EXCLUDED.decision_at,
                  decision_signature = EXCLUDED.decision_signature
                RETURNING approval_id
                """,
                (
                    str(row.get("approval_id") or ""),
                    str(row.get("run_id") or ""),
                    str(trace_id or ""),
                    str(row.get("phase") or ""),
                    str(row.get("step_id") or ""),
                    str(row.get("approval_type") or ""),
                    str(row.get("status") or "pending"),
                    row.get("created_at") or _utc_now_iso(),
                    bool(row.get("created_by_system", True)),
                    _to_json(row.get("evidence_payload") or {}),
                    str(row.get("risk_summary") or ""),
                    str(row.get("recommended_action") or "review"),
                    (str(row.get("operator_decision")) if row.get("operator_decision") else None),
                    (str(row.get("operator_id")) if row.get("operator_id") else None),
                    (str(row.get("operator_role")) if row.get("operator_role") else None),
                    row.get("decision_at"),
                    (str(row.get("decision_signature")) if row.get("decision_signature") else None),
                ),
            )

    def upsert_patch_registry_record(self, record: Dict[str, Any], *, trace_id: str) -> None:
        if not self._has_patch_registry_table():
            return
        row = dict(record or {})
        patch_task_id = str(row.get("patch_task_id") or "").strip()
        if not patch_task_id:
            return
        with db_conn() as conn:
            fetch_one(
                conn,
                f"""
                INSERT INTO {self.t_patch_registry}
                  (patch_task_id, run_id, trace_id, phase, step_id, attempt_no,
                   patch_branch, patch_type, status, policy_profile, trigger_reason_class,
                   origin_interpreter_snapshot_id, origin_block_reason_code,
                   comparator_outcome, operator_outcome_decision,
                   dispatch_approval_state, dispatch_metadata, baseline_run_ref,
                   retest_run_refs, comparator_result_ref, impact_summary, record_json,
                   created_at, updated_at)
                VALUES
                  (%s, %s, %s, %s, %s, %s,
                   %s, %s, %s, %s, %s,
                   %s, %s,
                   %s, %s,
                   %s, %s::jsonb, %s::jsonb,
                   %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                   %s::timestamptz, %s::timestamptz)
                ON CONFLICT (patch_task_id) DO UPDATE SET
                  run_id = EXCLUDED.run_id,
                  trace_id = EXCLUDED.trace_id,
                  phase = EXCLUDED.phase,
                  step_id = EXCLUDED.step_id,
                  attempt_no = EXCLUDED.attempt_no,
                  patch_branch = EXCLUDED.patch_branch,
                  patch_type = EXCLUDED.patch_type,
                  status = EXCLUDED.status,
                  policy_profile = EXCLUDED.policy_profile,
                  trigger_reason_class = EXCLUDED.trigger_reason_class,
                  origin_interpreter_snapshot_id = EXCLUDED.origin_interpreter_snapshot_id,
                  origin_block_reason_code = EXCLUDED.origin_block_reason_code,
                  comparator_outcome = EXCLUDED.comparator_outcome,
                  operator_outcome_decision = EXCLUDED.operator_outcome_decision,
                  dispatch_approval_state = EXCLUDED.dispatch_approval_state,
                  dispatch_metadata = EXCLUDED.dispatch_metadata,
                  baseline_run_ref = EXCLUDED.baseline_run_ref,
                  retest_run_refs = EXCLUDED.retest_run_refs,
                  comparator_result_ref = EXCLUDED.comparator_result_ref,
                  impact_summary = EXCLUDED.impact_summary,
                  record_json = EXCLUDED.record_json,
                  updated_at = EXCLUDED.updated_at
                RETURNING patch_task_id
                """,
                (
                    patch_task_id,
                    str(row.get("run_id") or ""),
                    str(row.get("trace_id") or trace_id or ""),
                    str(row.get("phase") or ""),
                    str(row.get("step_id") or ""),
                    int(row.get("attempt_no") or 0),
                    (str(row.get("patch_branch")) if row.get("patch_branch") else None),
                    (str(row.get("patch_type")) if row.get("patch_type") else None),
                    str(row.get("status") or ""),
                    str(row.get("policy_profile") or ""),
                    (str(row.get("trigger_reason_class")) if row.get("trigger_reason_class") else None),
                    (str(row.get("origin_interpreter_snapshot_id")) if row.get("origin_interpreter_snapshot_id") else None),
                    (str(row.get("origin_block_reason_code")) if row.get("origin_block_reason_code") else None),
                    (str(row.get("comparator_outcome")) if row.get("comparator_outcome") else None),
                    (str(row.get("operator_outcome_decision")) if row.get("operator_outcome_decision") else None),
                    (str(row.get("dispatch_approval_state")) if row.get("dispatch_approval_state") else None),
                    _to_json(row.get("dispatch_metadata") or {}),
                    _to_json(row.get("baseline_run_ref") or {}),
                    _to_json(row.get("retest_run_refs") or []),
                    _to_json(row.get("comparator_result_ref") or {}),
                    _to_json(row.get("impact_summary") or {}),
                    _to_json(row),
                    row.get("created_at") or _utc_now_iso(),
                    row.get("updated_at") or _utc_now_iso(),
                ),
            )

    def list_patch_registry_records(self, *, run_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        if not self._has_patch_registry_table():
            return []
        with db_conn(readonly=True) as conn:
            rows = fetch_all(
                conn,
                f"""
                SELECT record_json
                FROM {self.t_patch_registry}
                WHERE run_id = %s
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (str(run_id or ""), int(limit)),
            )
        out: List[Dict[str, Any]] = []
        for row in rows:
            obj = row.get("record_json") if isinstance(row, dict) else None
            if isinstance(obj, dict):
                out.append(dict(obj))
        return out

    # ------------------------------------------------------------------
    # Idempotency store
    # ------------------------------------------------------------------
    def get_idempotency(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        k = str(key or "").strip()
        if not k:
            return None
        with db_conn(readonly=True) as conn:
            return fetch_one(
                conn,
                f"""
                SELECT idempotency_key, run_id, scope, action, status,
                       payload_hash, result_payload, created_at, updated_at, expires_at
                FROM {self.t_idempotency}
                WHERE idempotency_key = %s
                """,
                (k,),
            )

    def upsert_idempotency(
        self,
        *,
        key: str,
        run_id: str,
        scope: str,
        action: str,
        status: str,
        payload_hash: Optional[str] = None,
        result_payload: Optional[Dict[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        if not self.available:
            return
        k = str(key or "").strip()
        if not k:
            return
        expires_at = None
        if ttl_seconds is not None and int(ttl_seconds) > 0:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(ttl_seconds))).isoformat()

        with db_conn() as conn:
            fetch_one(
                conn,
                f"""
                INSERT INTO {self.t_idempotency}
                  (idempotency_key, run_id, scope, action, status, payload_hash,
                   result_payload, expires_at)
                VALUES
                  (%s, %s, %s, %s, %s, %s,
                   %s::jsonb, %s::timestamptz)
                ON CONFLICT (idempotency_key) DO UPDATE SET
                  run_id = EXCLUDED.run_id,
                  scope = EXCLUDED.scope,
                  action = EXCLUDED.action,
                  status = EXCLUDED.status,
                  payload_hash = EXCLUDED.payload_hash,
                  result_payload = EXCLUDED.result_payload,
                  expires_at = EXCLUDED.expires_at,
                  updated_at = now()
                RETURNING idempotency_key
                """,
                (
                    k,
                    str(run_id or ""),
                    str(scope or ""),
                    str(action or ""),
                    str(status or "completed"),
                    (str(payload_hash) if payload_hash else None),
                    _to_json(result_payload or {}),
                    expires_at,
                ),
            )

    # ------------------------------------------------------------------
    # Queue / worker helpers
    # ------------------------------------------------------------------
    def enqueue_run(
        self,
        *,
        run_id: str,
        requested_by: Optional[str],
        idempotency_key: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        with db_conn() as conn:
            return fetch_one(
                conn,
                f"""
                INSERT INTO {self.t_queue}
                  (run_id, status, requested_by, idempotency_key)
                VALUES
                  (%s, 'pending', %s, %s)
                ON CONFLICT (idempotency_key) DO UPDATE SET
                  run_id = EXCLUDED.run_id,
                  requested_by = EXCLUDED.requested_by,
                  updated_at = now()
                RETURNING job_id, run_id, status, requested_by, idempotency_key, created_at, updated_at
                """,
                (
                    str(run_id),
                    (str(requested_by) if requested_by else None),
                    (str(idempotency_key) if idempotency_key else None),
                ),
            )

    def claim_next_queue_job(self, *, worker_id: str, lease_seconds: int = 120) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        lease_seconds = max(30, int(lease_seconds))
        wid = str(worker_id or "worker")
        with db_conn() as conn:
            return fetch_one(
                conn,
                f"""
                WITH next_job AS (
                  SELECT job_id
                  FROM {self.t_queue}
                  WHERE status = 'pending'
                    AND (lease_expires_at IS NULL OR lease_expires_at < now())
                  ORDER BY created_at ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE {self.t_queue} q
                SET status = 'running',
                    locked_by = %s,
                    locked_at = now(),
                    lease_expires_at = now() + make_interval(secs => %s),
                    updated_at = now()
                FROM next_job
                WHERE q.job_id = next_job.job_id
                RETURNING q.job_id, q.run_id, q.status, q.locked_by, q.locked_at, q.lease_expires_at
                """,
                (wid, lease_seconds),
            )

    def complete_queue_job(self, *, job_id: str, status: str, error_message: Optional[str] = None) -> None:
        if not self.available:
            return
        final_status = str(status or "completed").strip().lower()
        if final_status not in {"completed", "failed", "cancelled"}:
            final_status = "completed"
        with db_conn() as conn:
            fetch_one(
                conn,
                f"""
                UPDATE {self.t_queue}
                SET status = %s,
                    last_error = %s,
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE job_id = %s::uuid
                RETURNING job_id
                """,
                (final_status, (str(error_message) if error_message else None), str(job_id)),
            )

    def heartbeat_queue_job(self, *, job_id: str, worker_id: str, lease_seconds: int = 120) -> None:
        if not self.available:
            return
        with db_conn() as conn:
            fetch_one(
                conn,
                f"""
                UPDATE {self.t_queue}
                SET locked_by = %s,
                    lease_expires_at = now() + make_interval(secs => %s),
                    updated_at = now()
                WHERE job_id = %s::uuid
                RETURNING job_id
                """,
                (str(worker_id), int(max(30, lease_seconds)), str(job_id)),
            )

    # ------------------------------------------------------------------
    # SLO / alerts
    # ------------------------------------------------------------------
    def record_alert(
        self,
        *,
        run_id: str,
        trace_id: Optional[str],
        alert_code: str,
        severity: str,
        summary: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        with db_conn() as conn:
            return fetch_one(
                conn,
                f"""
                INSERT INTO {self.t_alerts}
                  (run_id, trace_id, alert_code, severity, summary, details)
                VALUES
                  (%s, %s, %s, %s, %s, %s::jsonb)
                RETURNING alert_id, run_id, trace_id, alert_code, severity, summary, details, status, created_at
                """,
                (
                    str(run_id or ""),
                    (str(trace_id) if trace_id else None),
                    str(alert_code or "unknown"),
                    str(severity or "warning"),
                    str(summary or ""),
                    _to_json(details or {}),
                ),
            )

    def list_open_alerts(self, *, run_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        if run_id:
            sql = f"""
            SELECT alert_id, run_id, trace_id, alert_code, severity, summary, details, status, created_at
            FROM {self.t_alerts}
            WHERE status = 'open' AND run_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """
            params = (str(run_id), int(limit))
        else:
            sql = f"""
            SELECT alert_id, run_id, trace_id, alert_code, severity, summary, details, status, created_at
            FROM {self.t_alerts}
            WHERE status = 'open'
            ORDER BY created_at DESC
            LIMIT %s
            """
            params = (int(limit),)
        with db_conn(readonly=True) as conn:
            return fetch_all(conn, sql, params)

    # ------------------------------------------------------------------
    # Audit timeline / evidence export
    # ------------------------------------------------------------------
    def fetch_timeline(
        self,
        *,
        run_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        if not run_id and not trace_id:
            return []

        where: List[str] = []
        params: List[Any] = []
        if run_id:
            where.append("run_id = %s")
            params.append(str(run_id))
        if trace_id:
            where.append("trace_id = %s")
            params.append(str(trace_id))
        base_params = list(params)
        where_sql = " AND ".join(where)
        include_patch_registry = self._has_patch_registry_table()
        patch_union = ""
        if include_patch_registry:
            patch_union = f"""

          UNION ALL

          SELECT
            created_at AS ts,
            run_id,
            trace_id,
            phase,
            step_id,
            ('patch_registry:' || status) AS kind,
            jsonb_build_object(
              'patch_task_id', patch_task_id,
              'status', status,
              'patch_branch', patch_branch,
              'patch_type', patch_type,
              'comparator_outcome', comparator_outcome,
              'operator_outcome_decision', operator_outcome_decision
            ) AS payload,
            NULL::text AS correlation_id,
            patch_task_id::text AS ref_id
          FROM {self.t_patch_registry}
          WHERE {where_sql}
            """

        sql = f"""
        SELECT * FROM (
          SELECT
            event_ts AS ts,
            run_id,
            trace_id,
            phase,
            step_id,
            event_type AS kind,
            event_payload AS payload,
            correlation_id,
            event_id::text AS ref_id
          FROM {self.t_events}
          WHERE {where_sql}

          UNION ALL

          SELECT
            created_at AS ts,
            run_id,
            trace_id,
            phase,
            step_id,
            ('step_attempt:' || status) AS kind,
            jsonb_build_object(
              'attempt_no', attempt_no,
              'status', status,
              'validator_result', validator_result,
              'block_reason', block_reason,
              'idempotency_key', idempotency_key
            ) AS payload,
            NULL::text AS correlation_id,
            record_id::text AS ref_id
          FROM {self.t_steps}
          WHERE {where_sql}

          UNION ALL

          SELECT
            created_at AS ts,
            run_id,
            trace_id,
            phase,
            step_id,
            ('approval:' || status) AS kind,
            jsonb_build_object(
              'approval_type', approval_type,
              'status', status,
              'operator_id', operator_id,
              'operator_role', operator_role,
              'decision_signature', decision_signature
            ) AS payload,
            NULL::text AS correlation_id,
            approval_id::text AS ref_id
          FROM {self.t_approvals}
          WHERE {where_sql}
          {patch_union}
        ) t
        ORDER BY ts DESC
        LIMIT %s
        """

        params = base_params + base_params + base_params
        if include_patch_registry:
            params = params + base_params
        params = params + [int(limit)]
        with db_conn(readonly=True) as conn:
            return fetch_all(conn, sql, tuple(params))

    def export_evidence_bundle(
        self,
        *,
        run_id: str,
        out_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.available:
            return {"ok": False, "error": "db_store_unavailable"}

        rid = str(run_id or "").strip()
        if not rid:
            return {"ok": False, "error": "run_id_required"}

        with db_conn(readonly=True) as conn:
            run_row = fetch_one(conn, f"SELECT * FROM {self.t_runs} WHERE run_id = %s", (rid,))
            steps = fetch_all(conn, f"SELECT * FROM {self.t_steps} WHERE run_id = %s ORDER BY created_at ASC", (rid,))
            events = fetch_all(conn, f"SELECT * FROM {self.t_events} WHERE run_id = %s ORDER BY event_ts ASC", (rid,))
            approvals = fetch_all(conn, f"SELECT * FROM {self.t_approvals} WHERE run_id = %s ORDER BY created_at ASC", (rid,))
            alerts = fetch_all(conn, f"SELECT * FROM {self.t_alerts} WHERE run_id = %s ORDER BY created_at ASC", (rid,))
            patch_registry = (
                fetch_all(conn, f"SELECT * FROM {self.t_patch_registry} WHERE run_id = %s ORDER BY updated_at DESC", (rid,))
                if self._has_patch_registry_table()
                else []
            )

        bundle = {
            "run": run_row or {},
            "steps": steps,
            "events": events,
            "approvals": approvals,
            "alerts": alerts,
            "patch_registry": patch_registry,
            "exported_at": _utc_now_iso(),
        }

        base = Path(out_dir or (Path(__file__).resolve().parents[1] / "orchestrator_logs" / "autopilot_evidence"))
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{rid}_evidence_bundle.json"
        path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "ok": True,
            "run_id": rid,
            "path": str(path),
            "counts": {
                "steps": len(steps),
                "events": len(events),
                "approvals": len(approvals),
                "alerts": len(alerts),
                "patch_registry": len(patch_registry),
            },
        }
