-- ============================================================
-- Phase 3 Manual Sequence Builder storage
-- Additive schema only (safe to re-run).
-- ============================================================

CREATE SCHEMA IF NOT EXISTS route_work;
CREATE SCHEMA IF NOT EXISTS route_raw;

CREATE TABLE IF NOT EXISTS route_work.manual_sequence_drafts (
  draft_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NULL REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  service_route_id UUID NULL REFERENCES route_raw.service_routes(service_route_id) ON DELETE SET NULL,
  direction_id SMALLINT NULL CHECK (direction_id IN (0, 1)),
  source TEXT NOT NULL DEFAULT 'manual_builder',

  ordered_stop_ids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[],
  ordered_node_ids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[],
  ordered_coords JSONB NOT NULL DEFAULT '[]'::jsonb,
  is_loop BOOLEAN NOT NULL DEFAULT FALSE,

  name_hint TEXT NULL,
  operator_hint TEXT NULL,
  variant_hint TEXT NULL,

  created_by TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_manual_sequence_drafts_route
  ON route_work.manual_sequence_drafts(route_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS route_work.manual_sequence_exports (
  export_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NOT NULL REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  service_route_id UUID NULL REFERENCES route_raw.service_routes(service_route_id) ON DELETE SET NULL,
  direction_id SMALLINT NULL CHECK (direction_id IN (0, 1)),
  source TEXT NOT NULL DEFAULT 'manual_builder',

  ordered_stop_ids UUID[] NOT NULL,
  ordered_node_ids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[],
  ordered_coords JSONB NOT NULL DEFAULT '[]'::jsonb,
  is_loop BOOLEAN NOT NULL DEFAULT FALSE,

  name_hint TEXT NULL,
  operator_hint TEXT NULL,
  variant_hint TEXT NULL,
  created_by TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  stop_sequence_set_id UUID NULL REFERENCES route_work.stop_sequence_candidate_sets(set_id) ON DELETE SET NULL,
  stop_sequence_candidate_id UUID NULL REFERENCES route_work.stop_sequence_candidates(candidate_id) ON DELETE SET NULL,
  draft_id UUID NULL REFERENCES route_work.manual_sequence_drafts(draft_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_manual_sequence_exports_route
  ON route_work.manual_sequence_exports(route_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_manual_sequence_exports_source
  ON route_work.manual_sequence_exports(source, created_at DESC);
