-- 001_node_raw.sql
-- Creates: node_raw.overpass_actions / overpass_queries / overpass_runs / overpass_elements

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS node_raw;

-- A) Catalog of actions (Bandit chooses these)
CREATE TABLE IF NOT EXISTS node_raw.overpass_actions (
  action_id      TEXT PRIMARY KEY,                 -- e.g. 'NodesStopsBroad_bbox'
  template_path  TEXT NOT NULL,                    -- e.g. 'templates/stops_broad_bbox.ql'
  default_params JSONB NOT NULL DEFAULT '{}'::jsonb,
  outputs        TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- B) Instantiated query (rendered template + params)
CREATE TABLE IF NOT EXISTS node_raw.overpass_queries (
  query_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  action_id   TEXT NOT NULL REFERENCES node_raw.overpass_actions(action_id) ON DELETE RESTRICT,
  query_text  TEXT NOT NULL,
  params      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_node_overpass_queries_action_id
  ON node_raw.overpass_queries(action_id);

CREATE INDEX IF NOT EXISTS idx_node_overpass_queries_params_gin
  ON node_raw.overpass_queries USING GIN (params);

-- C) Run / execution
CREATE TABLE IF NOT EXISTS node_raw.overpass_runs (
  run_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  query_id       UUID NOT NULL REFERENCES node_raw.overpass_queries(query_id) ON DELETE CASCADE,

  bbox           JSONB NULL,         -- {"south":..,"west":..,"north":..,"east":..}
  area_id        TEXT NULL,          -- optional label or osm area id
  status         TEXT NOT NULL,
  runtime_ms     INT  NULL,
  element_count  INT  NULL,
  response_bytes INT  NULL,
  fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT chk_node_overpass_runs_status
    CHECK (status IN ('ok','timeout','error'))
);

CREATE INDEX IF NOT EXISTS idx_node_overpass_runs_query_id
  ON node_raw.overpass_runs(query_id);

CREATE INDEX IF NOT EXISTS idx_node_overpass_runs_fetched_at
  ON node_raw.overpass_runs(fetched_at);

-- D) Raw elements
CREATE TABLE IF NOT EXISTS node_raw.overpass_elements (
  run_id      UUID NOT NULL REFERENCES node_raw.overpass_runs(run_id) ON DELETE CASCADE,
  osm_type    TEXT NOT NULL,             -- 'node' | 'way' | 'relation'
  osm_id      BIGINT NOT NULL,

  lat         DOUBLE PRECISION NULL,
  lon         DOUBLE PRECISION NULL,
  center_lat  DOUBLE PRECISION NULL,
  center_lon  DOUBLE PRECISION NULL,

  tags        JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- optional point geometry (recommended)
  geom        geometry(Point, 4326) NULL,

  inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT chk_node_overpass_elements_osm_type
    CHECK (osm_type IN ('node','way','relation')),

  CONSTRAINT uq_node_overpass_elements_run_osm UNIQUE (run_id, osm_type, osm_id)
);

CREATE INDEX IF NOT EXISTS idx_node_overpass_elements_run_id
  ON node_raw.overpass_elements(run_id);

CREATE INDEX IF NOT EXISTS idx_node_overpass_elements_osm
  ON node_raw.overpass_elements(osm_type, osm_id);

CREATE INDEX IF NOT EXISTS idx_node_overpass_elements_tags_gin
  ON node_raw.overpass_elements USING GIN (tags);

CREATE INDEX IF NOT EXISTS idx_node_overpass_elements_geom_gist
  ON node_raw.overpass_elements USING GIST (geom);

COMMIT;
