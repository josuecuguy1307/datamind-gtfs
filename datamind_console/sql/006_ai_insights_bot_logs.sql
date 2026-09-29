BEGIN;

CREATE SCHEMA IF NOT EXISTS ai;

CREATE TABLE IF NOT EXISTS ai.ai_bot_run_logs (
  log_id BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  phase TEXT NOT NULL
    CHECK (phase IN ('phase1', 'phase3')),
  stage TEXT NULL,
  event_type TEXT NOT NULL DEFAULT 'run'
    CHECK (event_type IN ('run', 'sequence_edit', 'system')),
  status TEXT NOT NULL DEFAULT 'success'
    CHECK (status IN ('success', 'partial', 'failed')),
  run_id TEXT NULL,
  node_set_id TEXT NULL,
  route_id TEXT NULL,
  service_route_id TEXT NULL,
  direction_id INTEGER NULL,
  quality_score DOUBLE PRECISION NULL,
  sequence_quality_score DOUBLE PRECISION NULL,
  reorder_recommended BOOLEAN NULL,
  reorder_confidence DOUBLE PRECISION NULL
    CHECK (reorder_confidence IS NULL OR (reorder_confidence >= 0.0 AND reorder_confidence <= 1.0)),
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  warnings TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  notes TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]
);

CREATE INDEX IF NOT EXISTS idx_ai_bot_run_logs_created_at
  ON ai.ai_bot_run_logs(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_bot_run_logs_phase_stage
  ON ai.ai_bot_run_logs(phase, stage, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_bot_run_logs_route
  ON ai.ai_bot_run_logs(route_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_bot_run_logs_node_set
  ON ai.ai_bot_run_logs(node_set_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_bot_run_logs_event
  ON ai.ai_bot_run_logs(event_type, created_at DESC);

CREATE TABLE IF NOT EXISTS ai.ai_bot_model_metrics (
  metric_id BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  task TEXT NOT NULL,
  event_type TEXT NOT NULL DEFAULT 'eval'
    CHECK (event_type IN ('train', 'eval', 'manual', 'system')),
  status TEXT NOT NULL DEFAULT 'success'
    CHECK (status IN ('success', 'partial', 'failed')),
  ok BOOLEAN NOT NULL DEFAULT TRUE,
  metric_primary_name TEXT NULL,
  metric_primary_value DOUBLE PRECISION NULL,
  metric_higher_better BOOLEAN NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_ai_bot_model_metrics_task_created
  ON ai.ai_bot_model_metrics(task, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_bot_model_metrics_created
  ON ai.ai_bot_model_metrics(created_at DESC);

CREATE TABLE IF NOT EXISTS ai.ai_bot_train_events (
  event_id BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  task TEXT NOT NULL,
  event_type TEXT NOT NULL DEFAULT 'manual'
    CHECK (event_type IN ('train', 'eval', 'manual', 'system')),
  status TEXT NOT NULL DEFAULT 'success'
    CHECK (status IN ('success', 'partial', 'failed')),
  ok BOOLEAN NOT NULL DEFAULT TRUE,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_ai_bot_train_events_task_created
  ON ai.ai_bot_train_events(task, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_bot_train_events_created
  ON ai.ai_bot_train_events(created_at DESC);

COMMIT;
