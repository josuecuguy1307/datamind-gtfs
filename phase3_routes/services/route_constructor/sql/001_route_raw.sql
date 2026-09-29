-- ============================================================
-- Phase 3 (Route Constructor) — route_raw (UPDATED)
-- Raw-first: discover -> pick -> fetch -> store raw
-- Safe to run multiple times.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS route_raw;

-- ------------------------------------------------------------
-- 1) Route jobs (one job = one route we’re constructing)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_raw.route_jobs (
  route_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  notes TEXT,

  -- NEW: discovery context (El Valle, bbox, known ref)
  area_key TEXT,                 -- e.g. 'el_valle'
  bbox JSONB,                    -- {"south":..,"west":..,"north":..,"east":..}
  known_ref TEXT,                -- the ref you searched (E1/E2/...)
  chosen_osm_relation_id BIGINT, -- the selected relation id (optional convenience)
  extractor_source TEXT,
  extractor_review JSONB
);

-- Safe adds if table already exists
ALTER TABLE route_raw.route_jobs
  ADD COLUMN IF NOT EXISTS area_key TEXT,
  ADD COLUMN IF NOT EXISTS bbox JSONB,
  ADD COLUMN IF NOT EXISTS known_ref TEXT,
  ADD COLUMN IF NOT EXISTS chosen_osm_relation_id BIGINT,
  ADD COLUMN IF NOT EXISTS extractor_source TEXT,
  ADD COLUMN IF NOT EXISTS extractor_review JSONB;

CREATE INDEX IF NOT EXISTS idx_route_jobs_area_ref
  ON route_raw.route_jobs (area_key, known_ref);

CREATE INDEX IF NOT EXISTS idx_route_jobs_extractor_source_created
  ON route_raw.route_jobs (extractor_source, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_route_jobs_chosen_relation_created
  ON route_raw.route_jobs (chosen_osm_relation_id, created_at DESC)
  WHERE chosen_osm_relation_id IS NOT NULL;

-- ------------------------------------------------------------
-- 2) Discovery results (store candidate relations you found)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_raw.relation_candidates (
  candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NOT NULL REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,

  osm_relation_id BIGINT NOT NULL,
  rel_type TEXT,        -- 'route' or 'route_master'
  route_mode TEXT,      -- 'bus', 'tram', etc (from tags.route if present)
  ref TEXT,
  name TEXT,
  operator TEXT,
  tags JSONB NOT NULL DEFAULT '{}'::jsonb,

  found_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_relation_candidates_route
  ON route_raw.relation_candidates (route_id, found_at DESC);

CREATE INDEX IF NOT EXISTS idx_relation_candidates_relation
  ON route_raw.relation_candidates (osm_relation_id);

-- Optional: prevent duplicate candidates for same job
CREATE UNIQUE INDEX IF NOT EXISTS uq_relation_candidates_route_relation
  ON route_raw.relation_candidates (route_id, osm_relation_id);

-- ------------------------------------------------------------
-- 3) Raw Overpass relation payload (the chosen relation for the job)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_raw.osm_relations_raw (
  route_id UUID PRIMARY KEY,
  osm_relation_id BIGINT NOT NULL,

  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  overpass_json JSONB NOT NULL,

  -- NEW: store fetch metadata (debug + reproducibility)
  overpass_query TEXT,
  overpass_url TEXT,
  http_status INT,
  response_ms INT
);

-- Ensure FK is correct (this fixes the “route_jobs not found” FK issue)
ALTER TABLE route_raw.osm_relations_raw
  DROP CONSTRAINT IF EXISTS osm_relations_raw_route_id_fkey;

ALTER TABLE route_raw.osm_relations_raw
  ADD CONSTRAINT osm_relations_raw_route_id_fkey
  FOREIGN KEY (route_id)
  REFERENCES route_raw.route_jobs(route_id)
  ON DELETE CASCADE;

-- Safe adds if table already exists
ALTER TABLE route_raw.osm_relations_raw
  ADD COLUMN IF NOT EXISTS overpass_query TEXT,
  ADD COLUMN IF NOT EXISTS overpass_url TEXT,
  ADD COLUMN IF NOT EXISTS http_status INT,
  ADD COLUMN IF NOT EXISTS response_ms INT;

CREATE INDEX IF NOT EXISTS idx_osm_relations_raw_osm_relation_id
  ON route_raw.osm_relations_raw (osm_relation_id);

-- Handy join index
CREATE INDEX IF NOT EXISTS idx_osm_relations_raw_route_id
  ON route_raw.osm_relations_raw (route_id);

-- ------------------------------------------------------------
-- 4) Optional: unmatched points bucket (raw-first, “not cleaned yet”)
-- (Useful later; safe to include now)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_raw.unmatched_stop_points (
  route_id UUID NOT NULL REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  seq INT NOT NULL,

  osm_type TEXT,         -- 'node' or 'way'
  osm_ref BIGINT,        -- id
  role TEXT,

  lat DOUBLE PRECISION NOT NULL,
  lon DOUBLE PRECISION NOT NULL,

  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (route_id, seq)
);
