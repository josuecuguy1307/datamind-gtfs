BEGIN;

CREATE SCHEMA IF NOT EXISTS semantics;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS semantics.route_name_candidates (
  candidate_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id                  uuid NOT NULL
                            REFERENCES route_raw.route_jobs(route_id)
                            ON DELETE CASCADE,
  run_id                    uuid NOT NULL DEFAULT gen_random_uuid(),
  rank_pos                  int  NOT NULL CHECK (rank_pos >= 1),

  route_name                text NOT NULL,
  route_ref                 text NULL,
  operator_name             text NULL,

  source_type               text NOT NULL DEFAULT 'heuristic',
  feature_snapshot_version  text NOT NULL DEFAULT 'v1',
  features                  jsonb NOT NULL DEFAULT '{}'::jsonb,

  heuristic_score           double precision NOT NULL DEFAULT 0.0,
  model_score               double precision NULL,
  final_score               double precision NOT NULL DEFAULT 0.0,

  metadata                  jsonb NOT NULL DEFAULT '{}'::jsonb,
  generated_at              timestamptz NOT NULL DEFAULT now(),

  UNIQUE (route_id, run_id, rank_pos)
);

CREATE INDEX IF NOT EXISTS idx_rnc_route_generated
  ON semantics.route_name_candidates(route_id, generated_at DESC, rank_pos ASC);

CREATE INDEX IF NOT EXISTS idx_rnc_route_run
  ON semantics.route_name_candidates(route_id, run_id, rank_pos ASC);

COMMIT;
