-- 010_geo_work_core.sql
-- Creates Phase 2 work tables:
-- - candidate sets (alternative semantic solutions)
-- - place candidates + alias candidates
-- - provisional node->place mapping per candidate set

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE SCHEMA IF NOT EXISTS geo_work;

-- Unit you choose at end of Phase 2: a "semantic solution" for a given context
CREATE TABLE IF NOT EXISTS geo_work.place_candidate_sets (
  place_set_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- what evidence built this set
  source_extract_run_id UUID NOT NULL REFERENCES geo_raw.extract_runs(extract_run_id) ON DELETE CASCADE,
  context_key           TEXT NULL,                      -- e.g. 'sample_v1'

  -- knobs used to generate this candidate set (merge thresholds, clustering radii, etc.)
  params_used JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- ranker output (optional)
  rank_score     DOUBLE PRECISION NULL,
  rank_model_ver TEXT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_geo_place_sets_created_at
  ON geo_work.place_candidate_sets(created_at);

CREATE INDEX IF NOT EXISTS idx_geo_place_sets_extract_run
  ON geo_work.place_candidate_sets(source_extract_run_id);

CREATE INDEX IF NOT EXISTS idx_geo_place_sets_context_key
  ON geo_work.place_candidate_sets(context_key);

-- Place candidates inside a set (each candidate becomes a geo_prod.place after approval)
CREATE TABLE IF NOT EXISTS geo_work.place_candidates (
  place_candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  place_set_id       UUID NOT NULL REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE CASCADE,

  proposed_canonical_name TEXT NOT NULL,
  proposed_place_type     TEXT NOT NULL CHECK (proposed_place_type IN ('STOP','POI','STATION','TERMINAL','OTHER')),

  -- for debugging / UI: optional representative location
  center_geom geometry(Point, 4326) NULL,

  -- explanation / provenance of the proposal
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,

  score       DOUBLE PRECISION NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_geo_place_candidates_set
  ON geo_work.place_candidates(place_set_id);

CREATE INDEX IF NOT EXISTS idx_geo_place_candidates_type
  ON geo_work.place_candidates(proposed_place_type);

-- Alias candidates per place candidate
CREATE TABLE IF NOT EXISTS geo_work.alias_candidates (
  alias_candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  place_candidate_id UUID NOT NULL REFERENCES geo_work.place_candidates(place_candidate_id) ON DELETE CASCADE,

  alias      TEXT NOT NULL,
  alias_kind TEXT NOT NULL DEFAULT 'alt' CHECK (alias_kind IN ('official','short','alt','abbr','historic','typo_common')),

  lang       TEXT NULL,
  score      DOUBLE PRECISION NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT uq_geo_alias_candidates_place_alias UNIQUE (place_candidate_id, alias)
);

CREATE INDEX IF NOT EXISTS idx_geo_alias_candidates_place
  ON geo_work.alias_candidates(place_candidate_id);

-- Provisional mapping: which nodes belong to which place_candidate (inside a candidate set)
CREATE TABLE IF NOT EXISTS geo_work.node_place_map_work (
  place_set_id       UUID NOT NULL REFERENCES geo_work.place_candidate_sets(place_set_id) ON DELETE CASCADE,
  node_id            UUID NOT NULL,  -- references node_prod.nodes(node_id) (no FK by design)
  place_candidate_id UUID NOT NULL REFERENCES geo_work.place_candidates(place_candidate_id) ON DELETE CASCADE,

  confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  mapping_source TEXT NOT NULL DEFAULT 'auto' CHECK (mapping_source IN ('auto','manual')),

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (place_set_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_geo_node_place_work_place
  ON geo_work.node_place_map_work(place_set_id, place_candidate_id);

COMMIT;
