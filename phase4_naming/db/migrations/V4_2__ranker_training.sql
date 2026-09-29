-- V4_2__ranker_training.sql
-- LightGBM LambdaRank: labels + model registry + prediction logging
-- pgAdmin-friendly (single transaction, no DO $$ blocks)

BEGIN;

CREATE SCHEMA IF NOT EXISTS semantics;

-- ------------------------------------------------------------
-- 1) match_labels
-- Ground-truth labels for training.
-- Query-group for LambdaRank is record_id (rank routes per record).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS semantics.match_labels (
  label_id      bigserial PRIMARY KEY,

  record_id     uuid NOT NULL
               REFERENCES semantics.route_evidence_records(record_id)
               ON DELETE CASCADE,

  route_id      uuid NOT NULL
               REFERENCES route_raw.route_jobs(route_id)
               ON DELETE CASCADE,

  -- relevance for LambdaRank:
  -- 2 = correct match
  -- 1 = partial (optional)
  -- 0 = negative
  relevance     int  NOT NULL DEFAULT 2 CHECK (relevance IN (0,1,2)),

  label_source  text NOT NULL DEFAULT 'admin_ui',
  notes         text NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),

  UNIQUE(record_id, route_id)
);

-- For training queries:
-- get all routes for one record_id fast
CREATE INDEX IF NOT EXISTS idx_match_labels_record
  ON semantics.match_labels(record_id, relevance DESC);

-- sometimes you’ll want all labels for a route_id
CREATE INDEX IF NOT EXISTS idx_match_labels_route
  ON semantics.match_labels(route_id, created_at DESC);


-- ------------------------------------------------------------
-- 2) ranker_models
-- Model registry: saves versions, params, metrics, artifact path.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS semantics.ranker_models (
  model_id        bigserial PRIMARY KEY,
  model_name      text NOT NULL DEFAULT 'lgbm_lambdarank',
  model_version   text NOT NULL,                 -- e.g. "v1", "v1.1"
  feature_version text NOT NULL DEFAULT 'v1',     -- bump when you change features
  artifact_path   text NOT NULL,                 -- e.g. "phase4_models/lgbm_ranker_v1.txt"

  train_rows      int  NOT NULL DEFAULT 0,
  metrics         jsonb NOT NULL DEFAULT '{}'::jsonb,
  params          jsonb NOT NULL DEFAULT '{}'::jsonb,

  trained_at      timestamptz NOT NULL DEFAULT now(),

  UNIQUE(model_name, model_version)
);

CREATE INDEX IF NOT EXISTS idx_ranker_models_name_version
  ON semantics.ranker_models(model_name, model_version);


-- ------------------------------------------------------------
-- 3) ranker_predictions
-- Prediction logs per model run.
-- Query-group is record_id (rank routes per record).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS semantics.ranker_predictions (
  pred_id       bigserial PRIMARY KEY,

  -- Identify one inference run (so you can compare runs cleanly)
  run_id        uuid NOT NULL DEFAULT gen_random_uuid(),

  model_id      bigint NOT NULL
               REFERENCES semantics.ranker_models(model_id)
               ON DELETE CASCADE,

  record_id     uuid NOT NULL
               REFERENCES semantics.route_evidence_records(record_id)
               ON DELETE CASCADE,

  route_id      uuid NOT NULL
               REFERENCES route_raw.route_jobs(route_id)
               ON DELETE CASCADE,

  rank_pos      int  NOT NULL,                   -- 1 = best
  score         double precision NOT NULL,
  score_parts   jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Fast debugging per run:
CREATE INDEX IF NOT EXISTS idx_ranker_preds_run
  ON semantics.ranker_predictions(run_id, rank_pos);

-- Fast “latest predictions for a record”:
CREATE INDEX IF NOT EXISTS idx_ranker_preds_record
  ON semantics.ranker_predictions(record_id, created_at DESC, rank_pos);

-- Fast “latest predictions for a model”:
CREATE INDEX IF NOT EXISTS idx_ranker_preds_model
  ON semantics.ranker_predictions(model_id, created_at DESC);

COMMIT;
