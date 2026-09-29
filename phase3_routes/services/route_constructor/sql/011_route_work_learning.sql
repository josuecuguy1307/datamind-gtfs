-- ============================================================
-- Phase 3 — route_work (APPROVALS + LOGS + LABELS) UPDATED
-- Safe ALTERs + indexes
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS route_work;

-- ------------------------------------------------------------
-- A) Approvals
--    Keep stop_sequence optional (already optional)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_work.route_approvals (
  approval_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NOT NULL REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,

  chosen_geometry_candidate_id UUID NOT NULL
    REFERENCES route_work.geometry_candidates(geometry_candidate_id),

  -- Optional (works for both seq-based and raw-first pipelines)
  chosen_stop_sequence_candidate_id UUID
    REFERENCES route_work.stop_sequence_candidates(candidate_id),

  approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  approved_by TEXT,
  notes TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_route_approvals_route
  ON route_work.route_approvals(route_id);

CREATE INDEX IF NOT EXISTS idx_route_approvals_geom
  ON route_work.route_approvals(chosen_geometry_candidate_id);

-- ------------------------------------------------------------
-- B) Context / features (fine as-is)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_work.route_context_features (
  route_id UUID PRIMARY KEY REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  stop_count INT,
  avg_stop_spacing_m DOUBLE PRECISION,
  stop_density_per_km2 DOUBLE PRECISION,
  features JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Optional: useful index if you query recency
CREATE INDEX IF NOT EXISTS idx_route_context_features_computed_at
  ON route_work.route_context_features(computed_at DESC);

-- ------------------------------------------------------------
-- C) Valhalla run logs
--    UPGRADE: keep as-is, but add indexes that help debugging/training
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_work.valhalla_run_logs (
  run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NOT NULL REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,

  geometry_candidate_set_id UUID
    REFERENCES route_work.geometry_candidate_sets(set_id) ON DELETE CASCADE,

  preset_id UUID REFERENCES route_work.valhalla_presets(preset_id),

  engine TEXT NOT NULL DEFAULT 'valhalla_route',
  request_json JSONB NOT NULL,
  response_meta JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- reward can be your geometry score or bandit reward later
  reward DOUBLE PRECISION,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_valhalla_run_logs_route
  ON route_work.valhalla_run_logs(route_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_valhalla_run_logs_set
  ON route_work.valhalla_run_logs(geometry_candidate_set_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_valhalla_run_logs_preset
  ON route_work.valhalla_run_logs(preset_id, created_at DESC);

-- Optional JSONB index if you filter inside response_meta frequently
-- CREATE INDEX IF NOT EXISTS idx_valhalla_run_logs_response_meta_gin
--   ON route_work.valhalla_run_logs USING GIN (response_meta);

-- ------------------------------------------------------------
-- D) Geometry ranking labels (for ML)
--    Keep, but add index for fast training queries
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_work.geometry_ranking_labels (
  set_id UUID NOT NULL REFERENCES route_work.geometry_candidate_sets(set_id) ON DELETE CASCADE,
  geometry_candidate_id UUID NOT NULL REFERENCES route_work.geometry_candidates(geometry_candidate_id) ON DELETE CASCADE,
  label INT NOT NULL,
  PRIMARY KEY (set_id, geometry_candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_geometry_ranking_labels_set_label
  ON route_work.geometry_ranking_labels(set_id, label DESC);
