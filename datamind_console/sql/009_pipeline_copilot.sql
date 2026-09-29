BEGIN;

-- --------------------------------------------------
-- Pipeline Copilot: session + message audit storage
-- --------------------------------------------------

CREATE TABLE IF NOT EXISTS console.pipeline_copilot_sessions (
  session_id         TEXT PRIMARY KEY,
  title              TEXT NOT NULL,
  mode               TEXT NOT NULL DEFAULT 'pipeline_operator',
  status             TEXT NOT NULL DEFAULT 'active',
  pinned             BOOLEAN NOT NULL DEFAULT FALSE,
  created_by         TEXT,
  context_defaults   JSONB NOT NULL DEFAULT '{}'::jsonb,
  metadata_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT chk_pipeline_copilot_sessions_status
    CHECK (status IN ('active', 'archived', 'closed'))
);

CREATE INDEX IF NOT EXISTS idx_pipeline_copilot_sessions_updated_at
  ON console.pipeline_copilot_sessions(updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_copilot_sessions_mode
  ON console.pipeline_copilot_sessions(mode);


CREATE TABLE IF NOT EXISTS console.pipeline_copilot_messages (
  message_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id         TEXT NOT NULL REFERENCES console.pipeline_copilot_sessions(session_id) ON DELETE CASCADE,
  role               TEXT NOT NULL,
  content            TEXT NOT NULL,

  task               TEXT,
  model              TEXT,
  latency_ms         INT,
  token_usage        JSONB NOT NULL DEFAULT '{}'::jsonb,

  event_type         TEXT,
  phase              TEXT,
  step_id            TEXT,
  run_id             TEXT,
  context_payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
  request_payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
  response_payload   JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_text         TEXT,
  trace_id           TEXT,

  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT chk_pipeline_copilot_messages_role
    CHECK (role IN ('system', 'user', 'assistant'))
);

CREATE INDEX IF NOT EXISTS idx_pipeline_copilot_messages_session_created
  ON console.pipeline_copilot_messages(session_id, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_pipeline_copilot_messages_run_id
  ON console.pipeline_copilot_messages(run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_copilot_messages_trace_id
  ON console.pipeline_copilot_messages(trace_id, created_at DESC);

COMMIT;

