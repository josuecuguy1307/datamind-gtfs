-- V4__create_semantics_schema.sql
-- Phase 4 Semantics core schema (route-driven seed + intersection fallback)

-- Notes:
-- - Phase3 tables must exist:
--   - route_raw.route_jobs
--   - route_raw.osm_relations_raw
--   - route_prod.routes (for geom usage later)
-- - This migration does NOT alter route_prod.routes.
-- - Phase 5 handles publishing/GTFS, so Phase 4 has NO "published" state.

CREATE SCHEMA IF NOT EXISTS semantics;

-- Required because this schema uses geometry(Point/LineString/Polygon)
CREATE EXTENSION IF NOT EXISTS postgis;

-- Needed for gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1) Sample points used for intersection (reproducible)
CREATE TABLE IF NOT EXISTS semantics.route_sample_points (
  route_id        uuid        NOT NULL,
  sample_kind     text        NOT NULL CHECK (sample_kind IN ('stops','geom_interpolate')),
  sample_version  text        NOT NULL,
  idx             int         NOT NULL,
  pt              geometry(Point, 4326) NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (route_id, sample_version, idx),

  CONSTRAINT fk_rsp_route
    FOREIGN KEY (route_id)
    REFERENCES route_raw.route_jobs(route_id)
    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rsp_pt_gist
  ON semantics.route_sample_points USING GIST (pt);

CREATE INDEX IF NOT EXISTS idx_rsp_route_version
  ON semantics.route_sample_points (route_id, sample_version);


-- 2) Overpass candidate relations per route (the search space)
CREATE TABLE IF NOT EXISTS semantics.route_overpass_candidates (
  route_id        uuid        NOT NULL,
  sample_version  text        NOT NULL,
  relation_id     bigint      NOT NULL,
  tags            jsonb       NOT NULL DEFAULT '{}'::jsonb,
  raw             jsonb       NOT NULL DEFAULT '{}'::jsonb,
  fetched_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (route_id, sample_version, relation_id),

  CONSTRAINT fk_roc_route
    FOREIGN KEY (route_id)
    REFERENCES route_raw.route_jobs(route_id)
    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_roc_route_version
  ON semantics.route_overpass_candidates (route_id, sample_version);

CREATE INDEX IF NOT EXISTS idx_roc_relation_id
  ON semantics.route_overpass_candidates (relation_id);


-- 3) Intersection results (maximize coverage)
CREATE TABLE IF NOT EXISTS semantics.route_relation_intersections (
  route_id        uuid        NOT NULL,
  sample_version  text        NOT NULL,
  relation_id     bigint      NOT NULL,

  eps_m           int         NOT NULL DEFAULT 70,
  points_total    int         NOT NULL,
  points_matched  int         NOT NULL,
  coverage        double precision NOT NULL,
  match_idxs      int[]       NOT NULL DEFAULT ARRAY[]::int[],

  score           double precision NOT NULL,
  score_parts     jsonb       NOT NULL DEFAULT '{}'::jsonb,

  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (route_id, sample_version, relation_id),

  CHECK (points_total >= 0),
  CHECK (points_matched >= 0),
  CHECK (coverage >= 0.0 AND coverage <= 1.0),

  CONSTRAINT fk_rri_route
    FOREIGN KEY (route_id)
    REFERENCES route_raw.route_jobs(route_id)
    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rri_best_per_route
  ON semantics.route_relation_intersections (route_id, sample_version, score DESC);

CREATE INDEX IF NOT EXISTS idx_rri_relation_id
  ON semantics.route_relation_intersections (relation_id);


-- 4) Normalized evidence records (OSM/GIS/Docs/Manual)
CREATE TABLE IF NOT EXISTS semantics.route_evidence_records (
  record_id       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  source_type     text        NOT NULL,
  source_id       text        NOT NULL,

  route_id_hint   uuid        NULL,   -- seed can set this

  route_ref       text        NULL,
  route_name      text        NULL,
  operator_name   text        NULL,
  from_name       text        NULL,
  to_name         text        NULL,
  via             text[]      NULL,

  confidence_hint double precision NOT NULL DEFAULT 0.50,

  bbox            geometry(Polygon, 4326) NULL,
  geom            geometry(LineString, 4326) NULL,

  raw             jsonb       NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),

  UNIQUE (source_type, source_id),

  -- Optional FK: only enforce when hint is present (keeps NULL allowed)
  CONSTRAINT fk_rer_route_hint
    FOREIGN KEY (route_id_hint)
    REFERENCES route_raw.route_jobs(route_id)
    ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_rer_route_hint
  ON semantics.route_evidence_records (route_id_hint);

CREATE INDEX IF NOT EXISTS idx_rer_bbox_gist
  ON semantics.route_evidence_records USING GIST (bbox);

CREATE INDEX IF NOT EXISTS idx_rer_geom_gist
  ON semantics.route_evidence_records USING GIST (geom);


-- 5) Evidence matches (record <-> route_id)
CREATE TABLE IF NOT EXISTS semantics.route_evidence_matches (
  match_id        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  record_id       uuid        NOT NULL REFERENCES semantics.route_evidence_records(record_id) ON DELETE CASCADE,
  route_id        uuid        NOT NULL,

  score           double precision NOT NULL,
  score_parts     jsonb       NOT NULL DEFAULT '{}'::jsonb,
  is_best         boolean     NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT fk_rem_route
    FOREIGN KEY (route_id)
    REFERENCES route_raw.route_jobs(route_id)
    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rem_route_score
  ON semantics.route_evidence_matches (route_id, score DESC);

CREATE INDEX IF NOT EXISTS idx_rem_record
  ON semantics.route_evidence_matches (record_id);


-- 6) Draft outputs (compiler output before Phase-4 approval)
CREATE TABLE IF NOT EXISTS semantics.route_semantics_drafts (
  draft_id        uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id        uuid        NOT NULL,
  picked_record_id uuid       NULL REFERENCES semantics.route_evidence_records(record_id) ON DELETE SET NULL,

  route_name      text        NOT NULL,
  route_ref       text        NULL,
  operator_name   text        NULL,
  route_aliases   text[]      NOT NULL DEFAULT ARRAY[]::text[],
  landmark_tags   text[]      NOT NULL DEFAULT ARRAY[]::text[],
  direction_semantics jsonb   NOT NULL DEFAULT '{}'::jsonb,

  confidence      double precision NOT NULL DEFAULT 0.0,
  draft_reason    jsonb       NOT NULL DEFAULT '{}'::jsonb,

  -- Phase 4 has NO publishing; Phase 5 publishes GTFS.
  status          text        NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','approved','rejected')),

  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT fk_rsd_route
    FOREIGN KEY (route_id)
    REFERENCES route_raw.route_jobs(route_id)
    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rsd_route_status
  ON semantics.route_semantics_drafts (route_id, status);

CREATE INDEX IF NOT EXISTS idx_rsd_updated
  ON semantics.route_semantics_drafts (updated_at DESC);


-- ------------------------------------------------------------
-- Phase 3 → Phase 4 SEED VIEW (the real starting point)
-- Uses the chosen relation id + raw overpass payload captured in Phase 3.
-- ------------------------------------------------------------
-- IMPORTANT: Uses active_route_jobs (filters is_trashed = FALSE)
-- to prevent trashed/test routes from leaking into Phase 4 semantics.
CREATE OR REPLACE VIEW semantics.v_phase4_seed AS
SELECT
  j.route_id,
  j.known_ref,
  COALESCE(j.chosen_osm_relation_id, r.osm_relation_id) AS osm_relation_id,
  (r.overpass_json->'elements'->0->'tags') AS tags,
  r.overpass_json AS raw_overpass_json
FROM route_raw.active_route_jobs j
LEFT JOIN route_raw.osm_relations_raw r
  ON r.route_id = j.route_id;
