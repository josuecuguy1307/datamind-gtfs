-- 012_geo_work_ml.sql
-- Phase 2 learning support:
-- - candidate-set metrics (for ranker)
-- - selection_log (approval = supervision)
-- - model_registry

BEGIN;

CREATE SCHEMA IF NOT EXISTS geo_work;

-- Candidate-set metrics (ranker features)
CREATE TABLE IF NOT EXISTS geo_work.place_set_metrics (
  place_set_id UUID PRIMARY KEY REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE CASCADE,

  n_places        INT NOT NULL DEFAULT 0,
  n_aliases       INT NOT NULL DEFAULT 0,
  n_nodes_mapped  INT NOT NULL DEFAULT 0,

  alias_conflict_rate DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  avg_confidence      DOUBLE PRECISION NULL,

  computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Selection log (your approval becomes training signal)
CREATE TABLE IF NOT EXISTS geo_work.selection_log (
  selection_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  phase       INT  NOT NULL CHECK (phase = 2),
  object_type TEXT NOT NULL CHECK (object_type = 'places'),

  chosen_set_id    UUID NOT NULL REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE RESTRICT,
  rejected_set_ids UUID[] NOT NULL DEFAULT '{}'::uuid[],

  -- snapshot for reproducibility
  params  JSONB NOT NULL DEFAULT '{}'::jsonb,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  context JSONB NOT NULL DEFAULT '{}'::jsonb,

  selected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_geo_selection_log_selected_at
  ON geo_work.selection_log(selected_at);

CREATE INDEX IF NOT EXISTS idx_geo_selection_log_chosen_set
  ON geo_work.selection_log(chosen_set_id);

-- Model registry (ranker, embedding model metadata, etc.)
CREATE TABLE IF NOT EXISTS geo_work.model_registry (
  model_name TEXT PRIMARY KEY,  -- 'place_set_ranker_lgbm' | 'embeddings_e5' | etc.
  version    TEXT NOT NULL,
  artifact   JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
