-- AI schedule + escalations bootstrap.
-- Moved verbatim from datamind_console/db/ai_repo.py::_ensure_ai_schema (Phase 2,
-- 2026-10-08) so runtime code no longer executes DDL. Applied only through:
--   python -m datamind_console.db.schema_cli apply ai.learning --yes
-- (It intentionally does not run the full 005 migration, which also touches
-- GTFS tables managed elsewhere.)

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
