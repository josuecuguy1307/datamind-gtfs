-- 015_geo_work_geo_context.sql
-- Node-level geo-context features used before candidate scoring.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS geo_work;

CREATE TABLE IF NOT EXISTS geo_work.node_geo_context (
  extract_run_id UUID NOT NULL REFERENCES geo_raw.extract_runs(extract_run_id) ON DELETE CASCADE,
  context_key TEXT NULL,
  node_id UUID NOT NULL,

  lat DOUBLE PRECISION NULL,
  lon DOUBLE PRECISION NULL,
  geohash7 TEXT NULL,

  transit_density_300m INT NOT NULL DEFAULT 0,
  poi_density_300m INT NOT NULL DEFAULT 0,

  tag_stop_weight DOUBLE PRECISION NOT NULL DEFAULT 0,
  tag_poi_weight DOUBLE PRECISION NOT NULL DEFAULT 0,

  features JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (extract_run_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_geo_node_ctx_context
  ON geo_work.node_geo_context(context_key, extract_run_id);

CREATE INDEX IF NOT EXISTS idx_geo_node_ctx_geohash
  ON geo_work.node_geo_context(geohash7);

COMMIT;
