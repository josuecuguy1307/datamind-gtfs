BEGIN;

CREATE SCHEMA IF NOT EXISTS ai;

CREATE TABLE IF NOT EXISTS ai.ai_suggestions (
  suggestion_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  phase TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  suggestion_type TEXT NOT NULL
    CHECK (suggestion_type IN ('approve', 'reject', 'promote', 'review', 'investigate')),
  confidence DOUBLE PRECISION NOT NULL
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
  reason TEXT NOT NULL DEFAULT '',
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  features JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'reviewed', 'applied', 'dismissed')),
  model_name TEXT NULL,
  model_version TEXT NULL,
  prediction JSONB NULL,
  CONSTRAINT chk_ai_suggestions_model_pair
    CHECK (
      (model_name IS NULL AND model_version IS NULL)
      OR
      (model_name IS NOT NULL AND model_version IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_ai_suggestions_created_at
  ON ai.ai_suggestions(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_suggestions_phase_status
  ON ai.ai_suggestions(phase, status);

CREATE INDEX IF NOT EXISTS idx_ai_suggestions_entity_status
  ON ai.ai_suggestions(entity_type, entity_id, status);

CREATE TABLE IF NOT EXISTS ai.ai_label_events (
  label_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  suggestion_id UUID NOT NULL REFERENCES ai.ai_suggestions(suggestion_id) ON DELETE CASCADE,
  phase TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  human_label TEXT NOT NULL
    CHECK (human_label IN ('approve', 'reject', 'promote', 'dismiss', 'reviewed')),
  actor TEXT NOT NULL,
  notes TEXT NULL,
  ui_context JSONB NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_ai_label_events_created_at
  ON ai.ai_label_events(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_label_events_suggestion
  ON ai.ai_label_events(suggestion_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_label_events_entity
  ON ai.ai_label_events(entity_type, entity_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ai.ai_training_dataset (
  row_id BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  phase TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  features JSONB NOT NULL DEFAULT '{}'::jsonb,
  label TEXT NOT NULL
    CHECK (label IN ('approve', 'reject', 'promote', 'dismiss', 'reviewed')),
  source_suggestion_id UUID NULL REFERENCES ai.ai_suggestions(suggestion_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_training_dataset_created_at
  ON ai.ai_training_dataset(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_training_dataset_phase_label
  ON ai.ai_training_dataset(phase, label, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_training_dataset_entity
  ON ai.ai_training_dataset(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS ai.ai_model_registry (
  model_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  phase TEXT NULL,
  model_name TEXT NOT NULL,
  model_version TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  is_active BOOLEAN NOT NULL DEFAULT FALSE,
  trained_on JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_model_registry_identity
  ON ai.ai_model_registry(COALESCE(phase, ''), model_name, model_version);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_model_registry_active
  ON ai.ai_model_registry(COALESCE(phase, ''), model_name)
  WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_ai_model_registry_active_created
  ON ai.ai_model_registry(is_active, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_model_registry_phase_created
  ON ai.ai_model_registry(phase, created_at DESC);

COMMIT;
