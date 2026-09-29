-- ============================================================
-- 011  Sequence Discovery Runs
-- Stores pipeline results from the stop grounding / sequence
-- discovery workflow.  One row per pipeline execution.
-- Additive schema only (safe to re-run).
-- ============================================================

CREATE SCHEMA IF NOT EXISTS route_work;

CREATE TABLE IF NOT EXISTS route_work.discovery_runs (
  run_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Route seed identity
  route_name          TEXT NOT NULL,
  cooperative_name    TEXT NULL,
  anchor_a_hint       TEXT NULL,
  anchor_b_hint       TEXT NULL,
  intermediate_hints  TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  corridor_description TEXT NULL,
  source_catalog      TEXT NULL,
  catalog_index       INT NULL,
  idempotency_key     TEXT NOT NULL,

  -- Pipeline outcome
  status              TEXT NOT NULL DEFAULT 'pending',  -- pending, completed, failed, blocked
  scoring_mode        TEXT NULL DEFAULT 'ensemble',
  review_status       TEXT NOT NULL DEFAULT 'pending_review',  -- pending_review, approved, rejected, needs_more_data

  -- Grounding results
  grounding_confidence REAL NULL,
  anchor_a_matches    JSONB NULL DEFAULT '[]',
  anchor_b_matches    JSONB NULL DEFAULT '[]',
  unmatched_hints     TEXT[] NOT NULL DEFAULT ARRAY[]::text[],

  -- Corridor
  corridor_geojson    JSONB NULL,
  corridor_length_km  REAL NULL,
  corridor_confidence REAL NULL,

  -- Skeleton
  skeleton_stops      JSONB NULL DEFAULT '[]',  -- ordered stop dicts
  skeleton_stop_count INT NULL DEFAULT 0,
  skeleton_gaps       JSONB NULL DEFAULT '[]',
  sequence_confidence REAL NULL,

  -- Geometry
  geometry_geojson    JSONB NULL,
  geometry_length_km  REAL NULL,
  geometry_confidence REAL NULL,
  geometry_derived_from TEXT NULL,

  -- Metrics
  metrics             JSONB NULL DEFAULT '{}',

  -- Full pipeline output (for deep inspection)
  full_summary        JSONB NULL DEFAULT '{}',

  -- Seed provenance
  seed_payload        JSONB NULL DEFAULT '{}',
  source_notes        TEXT[] NOT NULL DEFAULT ARRAY[]::text[],

  -- Timestamps
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotency: only one run per seed key (latest wins)
CREATE UNIQUE INDEX IF NOT EXISTS idx_discovery_runs_idemp
  ON route_work.discovery_runs(idempotency_key);

CREATE INDEX IF NOT EXISTS idx_discovery_runs_status
  ON route_work.discovery_runs(status, review_status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_discovery_runs_catalog
  ON route_work.discovery_runs(source_catalog, catalog_index);

-- Review view
CREATE OR REPLACE VIEW route_work.discovery_review_v1 AS
SELECT
  r.run_id,
  r.route_name,
  r.cooperative_name,
  r.anchor_a_hint,
  r.anchor_b_hint,
  r.intermediate_hints,
  r.status,
  r.review_status,
  r.scoring_mode,
  r.grounding_confidence,
  r.corridor_length_km,
  r.corridor_confidence,
  r.skeleton_stop_count,
  r.sequence_confidence,
  r.geometry_length_km,
  r.geometry_confidence,
  COALESCE((r.metrics->>'total_discovered_stops')::int, 0) AS discovered_stops,
  COALESCE((r.metrics->>'total_gaps')::int, 0) AS total_gaps,
  COALESCE((r.metrics->>'avg_sequence_gap_m')::real, 0) AS avg_gap_m,
  r.source_catalog,
  r.catalog_index,
  r.created_at,
  r.updated_at,
  CASE
    WHEN r.status = 'failed' THEN 'blocked'
    WHEN r.sequence_confidence >= 0.6 AND r.corridor_confidence >= 0.8 THEN 'strong'
    WHEN r.sequence_confidence >= 0.3 THEN 'moderate'
    WHEN r.skeleton_stop_count >= 5 THEN 'weak_but_usable'
    ELSE 'weak'
  END AS strength_tier
FROM route_work.discovery_runs r
ORDER BY r.source_catalog, r.catalog_index;
