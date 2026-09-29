BEGIN;

CREATE SCHEMA IF NOT EXISTS semantics;

CREATE TABLE IF NOT EXISTS semantics.route_name_feedback (
  feedback_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id                  uuid NOT NULL
                            REFERENCES route_raw.route_jobs(route_id)
                            ON DELETE CASCADE,
  candidate_id              uuid NOT NULL
                            REFERENCES semantics.route_name_candidates(candidate_id)
                            ON DELETE CASCADE,

  reviewer                  text NOT NULL DEFAULT 'console',
  user_score                int  NOT NULL CHECK (user_score BETWEEN 1 AND 5),
  is_winner                 boolean NOT NULL DEFAULT false,

  feature_snapshot_version  text NOT NULL DEFAULT 'v1',
  created_at                timestamptz NOT NULL DEFAULT now(),

  UNIQUE (route_id, candidate_id, reviewer)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_rnf_route_winner
  ON semantics.route_name_feedback(route_id, reviewer)
  WHERE is_winner = true;

CREATE INDEX IF NOT EXISTS idx_rnf_route_created
  ON semantics.route_name_feedback(route_id, created_at DESC);

COMMIT;
