BEGIN;

CREATE SCHEMA IF NOT EXISTS semantics;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS semantics.route_name_seed_runs (
  seed_run_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id                  uuid NOT NULL
                            REFERENCES route_raw.route_jobs(route_id)
                            ON DELETE CASCADE,

  seed_source               text NOT NULL CHECK (seed_source IN ('osm_relation', 'endpoint_fallback')),
  chosen_osm_relation_id    bigint NULL,

  seed_route_name           text NULL,
  seed_route_ref            text NULL,
  seed_operator_name        text NULL,
  seed_from_name            text NULL,
  seed_to_name              text NULL,

  seed_payload              jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at                timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rnsr_route_created
  ON semantics.route_name_seed_runs(route_id, created_at DESC);

COMMIT;
