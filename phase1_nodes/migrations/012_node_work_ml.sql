-- 012_node_work_ml.sql
-- Phase 1 learning support:
-- - metrics for LightGBM ranker (candidate sets)
-- - selection_log (training signal for bandit + ranker + later classifier)
-- - model_registry (versions)
-- - bandit_state (contextual bandit)

BEGIN;

CREATE SCHEMA IF NOT EXISTS node_work;

-- Candidate-set metrics (ranker features)
CREATE TABLE IF NOT EXISTS node_work.node_set_metrics (
  node_set_id UUID PRIMARY KEY REFERENCES node_work.node_candidate_sets(node_set_id) ON DELETE CASCADE,

  n_candidates     INT NOT NULL DEFAULT 0,
  n_resolved_nodes INT NOT NULL DEFAULT 0,

  dup_rate         DOUBLE PRECISION NOT NULL DEFAULT 0.0,  -- you compute and store
  avg_prob_stop    DOUBLE PRECISION NULL,
  stop_poi_balance DOUBLE PRECISION NULL,
  spatial_spread_m DOUBLE PRECISION NULL,

  computed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Selection log (your approval is the reward / supervision)
CREATE TABLE IF NOT EXISTS node_work.selection_log (
  selection_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  phase            INT NOT NULL CHECK (phase = 1),
  object_type      TEXT NOT NULL CHECK (object_type = 'nodes'),

  chosen_set_id    UUID NOT NULL REFERENCES node_work.node_candidate_sets(node_set_id) ON DELETE RESTRICT,
  rejected_set_ids UUID[] NOT NULL DEFAULT '{}'::uuid[],

  bandit_action_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],

  -- snapshots at decision time (so training is reproducible)
  params    JSONB NOT NULL DEFAULT '{}'::jsonb,
  metrics   JSONB NOT NULL DEFAULT '{}'::jsonb,
  context   JSONB NOT NULL DEFAULT '{}'::jsonb,

  selected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_selection_log_selected_at
  ON node_work.selection_log(selected_at);

CREATE INDEX IF NOT EXISTS idx_selection_log_chosen_set
  ON node_work.selection_log(chosen_set_id);

-- Model registry (store artifact metadata + feature schema)
CREATE TABLE IF NOT EXISTS node_work.model_registry (
  model_name TEXT PRIMARY KEY,  -- 'bandit_templates' | 'stop_poi_lgbm' | 'set_ranker_lgbm'
  version    TEXT NOT NULL,
  artifact   JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Bandit state (per city / context key)
CREATE TABLE IF NOT EXISTS node_work.bandit_state (
  key        TEXT PRIMARY KEY,  -- e.g. 'sample_v1'
  state      JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
