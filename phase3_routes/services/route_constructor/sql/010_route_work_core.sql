-- ============================================================
-- Phase 3 — route_work (UPDATED)
-- Supports: node/way stop prior + flexible geometry build
-- Safe to run multiple times.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS route_work;

-- -------------------------R-----------------------------------
-- 1) Stop-order prior extracted from OSM relation members
--    UPGRADE: support node/way + generic osm_ref
-- ------------------------------------------------------------

-- If table doesn't exist, create with the NEW structure
CREATE TABLE IF NOT EXISTS route_work.relation_stop_prior (
  route_id UUID NOT NULL REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  seq INT NOT NULL,

  -- NEW: generalized member identity
  member_type TEXT,         -- 'node' | 'way'
  osm_ref BIGINT,           -- member id (node id or way id)

  -- Backward-compat (your old column name)
  osm_node_id BIGINT,       -- optional legacy (can be filled when member_type='node')

  role TEXT,
  lat DOUBLE PRECISION NOT NULL,
  lon DOUBLE PRECISION NOT NULL,

  -- Matching into your cleaned stop nodes (geo_prod)
  matched_stop_node_id UUID,
  match_dist_m DOUBLE PRECISION,
  match_state TEXT,

  PRIMARY KEY (route_id, seq)
);

-- Safe adds if it already exists
ALTER TABLE route_work.relation_stop_prior
  ADD COLUMN IF NOT EXISTS member_type TEXT,
  ADD COLUMN IF NOT EXISTS osm_ref BIGINT,
  ADD COLUMN IF NOT EXISTS osm_node_id BIGINT,
  ADD COLUMN IF NOT EXISTS role TEXT,
  ADD COLUMN IF NOT EXISTS matched_stop_node_id UUID,
  ADD COLUMN IF NOT EXISTS match_dist_m DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS match_state TEXT;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_relation_stop_prior_route
  ON route_work.relation_stop_prior (route_id);

CREATE INDEX IF NOT EXISTS idx_relation_stop_prior_osm_ref
  ON route_work.relation_stop_prior (osm_ref);

-- ------------------------------------------------------------
-- 2) Stop-sequence candidate sets (same as yours)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_work.stop_sequence_candidate_sets (
  set_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NOT NULL REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by TEXT,
  generator_version TEXT NOT NULL DEFAULT 'v1',
  notes TEXT
);

-- ------------------------------------------------------------
-- 3) Stop-sequence candidates (same as yours)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_work.stop_sequence_candidates (
  candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  set_id UUID NOT NULL REFERENCES route_work.stop_sequence_candidate_sets(set_id) ON DELETE CASCADE,
  rank INT NOT NULL,
  stop_node_ids UUID[] NOT NULL,

  matched_stops INT,
  avg_match_dist_m DOUBLE PRECISION,
  max_match_dist_m DOUBLE PRECISION,

  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stop_sequence_candidates_set_rank
  ON route_work.stop_sequence_candidates (set_id, rank);

-- ------------------------------------------------------------
-- 4) Valhalla presets library (same as yours)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_work.valhalla_presets (
  preset_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL UNIQUE,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  params JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- 5) Geometry candidate sets
--    UPGRADE: allow geometry sets that are built from:
--      A) stop_sequence_set_id (your current flow)
--      B) direct stop prior (future raw-first flow)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_work.geometry_candidate_sets (
  set_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NOT NULL REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,

  stop_sequence_set_id UUID REFERENCES route_work.stop_sequence_candidate_sets(set_id),

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by TEXT,
  generator_version TEXT NOT NULL DEFAULT 'v1',
  notes TEXT
);

-- Helpful index
CREATE INDEX IF NOT EXISTS idx_geometry_candidate_sets_route
  ON route_work.geometry_candidate_sets (route_id, created_at DESC);

-- ------------------------------------------------------------
-- 6) Geometry candidates (shapes)
--    UPGRADE: make stop_sequence_candidate_id nullable
--    so you can insert candidates built directly from prior later.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_work.geometry_candidates (
  geometry_candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  set_id UUID NOT NULL REFERENCES route_work.geometry_candidate_sets(set_id) ON DELETE CASCADE,

  -- CHANGE: allow NULL for future raw-first
  stop_sequence_candidate_id UUID REFERENCES route_work.stop_sequence_candidates(candidate_id) ON DELETE CASCADE,

  engine TEXT NOT NULL DEFAULT 'valhalla_route',
  preset_id UUID REFERENCES route_work.valhalla_presets(preset_id),
  params JSONB NOT NULL DEFAULT '{}'::jsonb,

  geom geometry(LineString, 4326) NOT NULL,

  length_m DOUBLE PRECISION,
  avg_stop_dist_m DOUBLE PRECISION,
  max_stop_dist_m DOUBLE PRECISION,
  score DOUBLE PRECISION NOT NULL DEFAULT 0,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- If table existed with NOT NULL stop_sequence_candidate_id, relax it safely:
ALTER TABLE route_work.geometry_candidates
  ALTER COLUMN stop_sequence_candidate_id DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_geometry_candidates_set_score
  ON route_work.geometry_candidates (set_id, score DESC);

CREATE INDEX IF NOT EXISTS idx_geometry_candidates_geom_gist
  ON route_work.geometry_candidates USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_geometry_candidates_seq
  ON route_work.geometry_candidates (stop_sequence_candidate_id);

-- ------------------------------------------------------------
-- Optional: guardrails (soft constraints you can enforce later)
-- ------------------------------------------------------------

-- You CAN optionally enforce valid member_type values once stable:
-- ALTER TABLE route_work.relation_stop_prior
--   ADD CONSTRAINT chk_relation_stop_prior_member_type
--   CHECK (member_type IS NULL OR member_type IN ('node','way'));



ALTER TABLE route_work.stop_sequence_candidates
  ADD COLUMN IF NOT EXISTS stop_prior_seqs INT[];  -- ordered seqs into relation_stop_prior

-- Make stop_node_ids optional (pick ONE approach)

-- Approach A: allow NULL
ALTER TABLE route_work.stop_sequence_candidates
  ALTER COLUMN stop_node_ids DROP NOT NULL;

-- Approach B: keep NOT NULL but allow empty default
ALTER TABLE route_work.stop_sequence_candidates
  ALTER COLUMN stop_node_ids SET DEFAULT ARRAY[]::uuid[];

-- (Optional but helpful)
ALTER TABLE route_work.stop_sequence_candidates
  ADD COLUMN IF NOT EXISTS canonical_coverage DOUBLE PRECISION;  -- 0..1
