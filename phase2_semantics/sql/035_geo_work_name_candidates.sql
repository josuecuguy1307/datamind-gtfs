-- 035_geo_work_name_candidates.sql
-- Candidate-level naming supervision for Phase 2.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS geo_work;

CREATE TABLE IF NOT EXISTS geo_work.place_name_candidates (
  name_candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  place_set_id UUID NOT NULL REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE CASCADE,
  place_candidate_id UUID NOT NULL REFERENCES geo_work.place_candidates(place_candidate_id) ON DELETE CASCADE,

  candidate_name TEXT NOT NULL,
  candidate_name_norm TEXT NOT NULL,
  source_kind TEXT NOT NULL DEFAULT 'generated'
    CHECK (source_kind IN ('generated','alias','tag','model','manual','custom')),

  score DOUBLE PRECISION NULL,
  model_score DOUBLE PRECISION NULL,
  model_rank INT NULL,
  selected_by_model BOOLEAN NOT NULL DEFAULT FALSE,

  features JSONB NOT NULL DEFAULT '{}'::jsonb,
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT uq_place_name_candidate_norm UNIQUE (place_candidate_id, candidate_name_norm)
);

CREATE INDEX IF NOT EXISTS idx_geo_name_candidates_set
  ON geo_work.place_name_candidates(place_set_id, place_candidate_id);

CREATE INDEX IF NOT EXISTS idx_geo_name_candidates_rank
  ON geo_work.place_name_candidates(place_set_id, place_candidate_id, model_rank, model_score DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS geo_work.place_name_feedback (
  feedback_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  place_set_id UUID NOT NULL REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE CASCADE,
  place_candidate_id UUID NOT NULL REFERENCES geo_work.place_candidates(place_candidate_id) ON DELETE CASCADE,

  chosen_name_candidate_id UUID NULL REFERENCES geo_work.place_name_candidates(name_candidate_id) ON DELETE SET NULL,
  chosen_name TEXT NOT NULL,
  chosen_name_norm TEXT NOT NULL,
  chosen_source TEXT NOT NULL DEFAULT 'user_pick'
    CHECK (chosen_source IN ('model_pick','user_pick','custom')),

  rejected_name_candidate_ids UUID[] NOT NULL DEFAULT '{}'::uuid[],
  reviewer TEXT NULL,
  context JSONB NOT NULL DEFAULT '{}'::jsonb,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_geo_name_feedback_set
  ON geo_work.place_name_feedback(place_set_id, place_candidate_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_geo_name_feedback_chosen
  ON geo_work.place_name_feedback(chosen_name_candidate_id);

COMMIT;
