-- 020_node_prod_core.sql
-- Phase 1 final output: node_prod.nodes (approved nodes: STOP + POI)

BEGIN;

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS node_prod;

CREATE TABLE IF NOT EXISTS node_prod.nodes (
  node_id   UUID PRIMARY KEY,                   -- stable internal id from node_work.nodes_resolved
  geom      geometry(Point, 4326) NOT NULL,

  node_type TEXT NOT NULL CHECK (node_type IN ('STOP','POI')),

  name      TEXT NULL,
  ref       TEXT NULL,
  operator  TEXT NULL,

  tag_kind  TEXT NOT NULL DEFAULT 'bus_stop',
  source    TEXT NOT NULL DEFAULT 'work_review',

  source_node_set_id UUID NULL,                 -- traceability (no FK on purpose)
  chosen_candidate_id UUID NULL,
  chosen_tags JSONB NOT NULL DEFAULT '{}'::jsonb,

  confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,

  approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_node_prod_nodes_geom_gist
  ON node_prod.nodes USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_node_prod_nodes_type
  ON node_prod.nodes(node_type);

CREATE INDEX IF NOT EXISTS idx_node_prod_nodes_name
  ON node_prod.nodes(name);

CREATE INDEX IF NOT EXISTS idx_node_prod_nodes_tags_gin
  ON node_prod.nodes USING GIN (chosen_tags);

COMMIT;

BEGIN;

-- View: node_work.v_node_sets
-- Purpose:
--   One row per node_set_id with:
--     - created_at (from node_candidate_sets)
--     - status (derived from nodes_resolved.status)
--     - resolved_at (latest resolved timestamp)
--     - n_resolved and breakdown of statuses
--
-- Status logic:
--   - if no resolved nodes => 'empty'
--   - else if ANY rejected => 'rejected'
--   - else if ANY work     => 'work'
--   - else                 => 'approved'  (all approved)
CREATE OR REPLACE VIEW node_work.v_node_sets AS
WITH agg AS (
  SELECT
    r.node_set_id,
    COUNT(*) AS n_resolved,
    MAX(r.resolved_at) AS resolved_at_max,
    SUM(CASE WHEN r.status = 'approved' THEN 1 ELSE 0 END) AS n_approved,
    SUM(CASE WHEN r.status = 'work'     THEN 1 ELSE 0 END) AS n_work,
    SUM(CASE WHEN r.status = 'rejected' THEN 1 ELSE 0 END) AS n_rejected
  FROM node_work.nodes_resolved r
  GROUP BY r.node_set_id
)
SELECT
  s.node_set_id,
  s.created_at,
  COALESCE(a.resolved_at_max, NULL) AS resolved_at,
  COALESCE(a.n_resolved, 0) AS n_resolved,

  CASE
    WHEN a.node_set_id IS NULL OR a.n_resolved = 0 THEN 'empty'
    WHEN a.n_rejected > 0 THEN 'rejected'
    WHEN a.n_work > 0 THEN 'work'
    ELSE 'approved'
  END AS status,

  COALESCE(a.n_approved, 0) AS n_approved,
  COALESCE(a.n_work, 0)     AS n_work,
  COALESCE(a.n_rejected, 0) AS n_rejected

FROM node_work.node_candidate_sets s   -- ✅ CORRECT TABLE
LEFT JOIN agg a
  ON a.node_set_id = s.node_set_id
ORDER BY s.created_at DESC;
