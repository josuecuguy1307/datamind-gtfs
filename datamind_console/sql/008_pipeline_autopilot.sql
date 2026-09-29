BEGIN;

-- --------------------------------------------------
-- Pipeline Autopilot: durable run state + audit trail
-- --------------------------------------------------

CREATE TABLE IF NOT EXISTS console.pipeline_autopilot_runs (
  run_id             TEXT PRIMARY KEY,
  trace_id           TEXT NOT NULL,
  status             TEXT NOT NULL,
  policy_profile     TEXT NOT NULL,
  current_phase      TEXT,
  current_step_id    TEXT,

  pipeline_scope     JSONB NOT NULL DEFAULT '{}'::jsonb,
  operator_context   JSONB NOT NULL DEFAULT '{}'::jsonb,
  attempt_counters   JSONB NOT NULL DEFAULT '{}'::jsonb,
  diversion_stack    JSONB NOT NULL DEFAULT '[]'::jsonb,
  resume_context     JSONB NOT NULL DEFAULT '{}'::jsonb,
  artifacts          JSONB NOT NULL DEFAULT '{}'::jsonb,
  state_json         JSONB NOT NULL DEFAULT '{}'::jsonb,

  started_at         TIMESTAMPTZ,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at       TIMESTAMPTZ,

  CONSTRAINT chk_pipeline_autopilot_runs_status
    CHECK (status IN ('running', 'paused', 'waiting_for_approval', 'completed', 'failed')),

  CONSTRAINT chk_pipeline_autopilot_runs_policy
    CHECK (policy_profile IN ('conservative', 'balanced', 'aggressive_supervised'))
);

CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_runs_status
  ON console.pipeline_autopilot_runs(status);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_runs_trace
  ON console.pipeline_autopilot_runs(trace_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_runs_updated_at
  ON console.pipeline_autopilot_runs(updated_at DESC);


CREATE TABLE IF NOT EXISTS console.pipeline_autopilot_step_attempts (
  record_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id                  TEXT NOT NULL REFERENCES console.pipeline_autopilot_runs(run_id) ON DELETE CASCADE,
  trace_id                TEXT NOT NULL,

  phase                   TEXT NOT NULL,
  step_id                 TEXT NOT NULL,
  attempt_no              INT NOT NULL,
  status                  TEXT NOT NULL,

  executor_result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  validator_result        JSONB NOT NULL DEFAULT '{}'::jsonb,
  ai_bot_snapshot         JSONB NOT NULL DEFAULT '{}'::jsonb,
  chatgpt_snapshot        JSONB NOT NULL DEFAULT '{}'::jsonb,
  block_reason            JSONB NOT NULL DEFAULT '{}'::jsonb,
  artifacts               JSONB NOT NULL DEFAULT '[]'::jsonb,
  timings                 JSONB NOT NULL DEFAULT '{}'::jsonb,

  idempotency_key         TEXT,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT chk_pipeline_autopilot_step_attempts_status
    CHECK (status IN (
      'completed',
      'blocked',
      'retry_scheduled',
      'waiting_for_approval',
      'paused_warning',
      'paused_manual',
      'manual_resolved'
    ))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_autopilot_step_attempt_dedupe
  ON console.pipeline_autopilot_step_attempts(run_id, step_id, attempt_no, status, created_at);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_step_attempts_run
  ON console.pipeline_autopilot_step_attempts(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_step_attempts_trace
  ON console.pipeline_autopilot_step_attempts(trace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_step_attempts_idempotency
  ON console.pipeline_autopilot_step_attempts(idempotency_key);


CREATE TABLE IF NOT EXISTS console.pipeline_autopilot_events (
  event_id         TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES console.pipeline_autopilot_runs(run_id) ON DELETE CASCADE,
  trace_id         TEXT NOT NULL,
  event_type       TEXT NOT NULL,
  phase            TEXT,
  step_id          TEXT,
  correlation_id   TEXT,
  event_payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
  event_ts         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_events_run
  ON console.pipeline_autopilot_events(run_id, event_ts DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_events_trace
  ON console.pipeline_autopilot_events(trace_id, event_ts DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_events_type
  ON console.pipeline_autopilot_events(event_type, event_ts DESC);


CREATE TABLE IF NOT EXISTS console.pipeline_autopilot_approvals (
  approval_id         TEXT PRIMARY KEY,
  run_id              TEXT NOT NULL REFERENCES console.pipeline_autopilot_runs(run_id) ON DELETE CASCADE,
  trace_id            TEXT NOT NULL,

  phase               TEXT NOT NULL,
  step_id             TEXT NOT NULL,
  approval_type       TEXT NOT NULL,
  status              TEXT NOT NULL DEFAULT 'pending',
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by_system   BOOLEAN NOT NULL DEFAULT TRUE,

  evidence_payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
  risk_summary        TEXT,
  recommended_action  TEXT,

  operator_decision   TEXT,
  operator_id         TEXT,
  operator_role       TEXT,
  decision_at         TIMESTAMPTZ,
  decision_signature  TEXT,

  CONSTRAINT chk_pipeline_autopilot_approvals_status
    CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'superseded')),

  CONSTRAINT chk_pipeline_autopilot_approvals_type
    CHECK (approval_type IN (
      'RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS',
      'APPLY_REORDER_PROPOSAL',
      'RUN_DESTRUCTIVE_CLEANUP',
      'APPROVE_FINAL_ROUTE_OR_MERGE_BIND',
      'PROMOTE_NODE_BATCH',
      'DISPATCH_PATCH_TASK'
    ))
);

CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_approvals_run
  ON console.pipeline_autopilot_approvals(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_approvals_pending
  ON console.pipeline_autopilot_approvals(created_at DESC)
  WHERE status = 'pending';


CREATE TABLE IF NOT EXISTS console.pipeline_autopilot_idempotency (
  idempotency_key   TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL REFERENCES console.pipeline_autopilot_runs(run_id) ON DELETE CASCADE,
  scope             TEXT NOT NULL,
  action            TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'completed',
  payload_hash      TEXT,
  result_payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at        TIMESTAMPTZ,

  CONSTRAINT chk_pipeline_autopilot_idempotency_status
    CHECK (status IN ('pending', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_idempotency_run
  ON console.pipeline_autopilot_idempotency(run_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_idempotency_scope
  ON console.pipeline_autopilot_idempotency(scope, action, updated_at DESC);


CREATE TABLE IF NOT EXISTS console.pipeline_autopilot_queue (
  job_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id            TEXT NOT NULL REFERENCES console.pipeline_autopilot_runs(run_id) ON DELETE CASCADE,
  status            TEXT NOT NULL DEFAULT 'pending',
  requested_by      TEXT,
  idempotency_key   TEXT UNIQUE,
  locked_by         TEXT,
  locked_at         TIMESTAMPTZ,
  lease_expires_at  TIMESTAMPTZ,
  last_error        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT chk_pipeline_autopilot_queue_status
    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_queue_status
  ON console.pipeline_autopilot_queue(status, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_queue_lease
  ON console.pipeline_autopilot_queue(lease_expires_at);


CREATE TABLE IF NOT EXISTS console.pipeline_autopilot_alerts (
  alert_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id            TEXT NOT NULL REFERENCES console.pipeline_autopilot_runs(run_id) ON DELETE CASCADE,
  trace_id          TEXT,
  alert_code        TEXT NOT NULL,
  severity          TEXT NOT NULL,
  summary           TEXT NOT NULL,
  details           JSONB NOT NULL DEFAULT '{}'::jsonb,
  status            TEXT NOT NULL DEFAULT 'open',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at       TIMESTAMPTZ,

  CONSTRAINT chk_pipeline_autopilot_alerts_severity
    CHECK (severity IN ('info', 'warning', 'critical')),

  CONSTRAINT chk_pipeline_autopilot_alerts_status
    CHECK (status IN ('open', 'acknowledged', 'resolved'))
);

CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_alerts_run
  ON console.pipeline_autopilot_alerts(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_alerts_open
  ON console.pipeline_autopilot_alerts(created_at DESC)
  WHERE status = 'open';

COMMIT;
