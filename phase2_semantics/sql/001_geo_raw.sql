-- 001_geo_raw.sql
-- Creates: geo_raw.extract_runs / geo_raw.name_evidence
-- Raw capture of semantic evidence (names) extracted from node_prod.nodes + tags

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE SCHEMA IF NOT EXISTS geo_raw;

-- A) Extraction run (traceability: "what input snapshot did we extract from?")
CREATE TABLE IF NOT EXISTS geo_raw.extract_runs (
  extract_run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- optional: tie to a selected Phase 1 node_set_id or a named snapshot key
  source_node_set_id UUID NULL,
  context_key        TEXT NULL,            -- e.g. 'sample_v1', 'valle_v1'

  status        TEXT NOT NULL DEFAULT 'ok',
  runtime_ms    INT  NULL,
  n_nodes_seen  INT  NULL,
  n_evidence    INT  NULL,

  extracted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT chk_geo_extract_runs_status
    CHECK (status IN ('ok','error'))
);

CREATE INDEX IF NOT EXISTS idx_geo_extract_runs_extracted_at
  ON geo_raw.extract_runs(extracted_at);

CREATE INDEX IF NOT EXISTS idx_geo_extract_runs_context_key
  ON geo_raw.extract_runs(context_key);

-- B) Name evidence harvested per node (many rows per node)
CREATE TABLE IF NOT EXISTS geo_raw.name_evidence (
  extract_run_id UUID NOT NULL REFERENCES geo_raw.extract_runs(extract_run_id) ON DELETE CASCADE,

  node_id     UUID NOT NULL,               -- references node_prod.nodes(node_id) (no FK by design)
  source      TEXT NOT NULL,               -- 'name','name:es','official_name','short_name','alt_name','ref','operator','network',etc.
  raw_text    TEXT NOT NULL,
  lang        TEXT NULL,                   -- 'es','en', etc.
  weight_hint DOUBLE PRECISION NOT NULL DEFAULT 1.0,

  tags_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,

  inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT chk_geo_name_evidence_source
    CHECK (source <> '')
);

CREATE INDEX IF NOT EXISTS idx_geo_name_evidence_run
  ON geo_raw.name_evidence(extract_run_id);

CREATE INDEX IF NOT EXISTS idx_geo_name_evidence_node
  ON geo_raw.name_evidence(node_id);

CREATE INDEX IF NOT EXISTS idx_geo_name_evidence_source
  ON geo_raw.name_evidence(source);

CREATE INDEX IF NOT EXISTS idx_geo_name_evidence_tags_gin
  ON geo_raw.name_evidence USING GIN (tags_snapshot);

COMMIT;
