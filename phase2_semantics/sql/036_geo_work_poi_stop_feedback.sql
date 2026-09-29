-- 036_geo_work_poi_stop_feedback.sql
-- Candidate-level place type supervision and model outputs.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS geo_work;

CREATE TABLE IF NOT EXISTS geo_work.poi_stop_feedback (
  feedback_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  place_set_id UUID NOT NULL REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE CASCADE,
  place_candidate_id UUID NOT NULL REFERENCES geo_work.place_candidates(place_candidate_id) ON DELETE CASCADE,

  chosen_place_type TEXT NOT NULL
    CHECK (chosen_place_type IN ('STOP','POI','STATION','TERMINAL','OTHER')),
  chosen_source TEXT NOT NULL DEFAULT 'user_pick'
    CHECK (chosen_source IN ('model_pick','user_pick','custom')),

  reviewer TEXT NULL,
  context JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_geo_poi_stop_feedback_set
  ON geo_work.poi_stop_feedback(place_set_id, place_candidate_id, created_at DESC);

ALTER TABLE geo_work.place_candidates
  ADD COLUMN IF NOT EXISTS model_place_type TEXT NULL
    CHECK (model_place_type IN ('STOP','POI','STATION','TERMINAL','OTHER'));

ALTER TABLE geo_work.place_candidates
  ADD COLUMN IF NOT EXISTS model_place_type_score DOUBLE PRECISION NULL;

COMMIT;
