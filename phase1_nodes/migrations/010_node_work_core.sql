-- 010_node_work_core.sql
-- Creates Phase 1 work tables (candidate sets + candidates + features + clustering + resolved)

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS node_work;

-- Unit you choose at end of Phase 1
CREATE TABLE IF NOT EXISTS node_work.node_candidate_sets (
  node_set_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- raw evidence used to build this set
  source_run_ids UUID[] NOT NULL,

  -- contextual bandit actions (templates)
  action_ids     TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  params_used    JSONB  NOT NULL DEFAULT '{}'::jsonb,

  -- ranker output (dashboard bars)
  rank_score     DOUBLE PRECISION NULL,
  rank_model_ver TEXT NULL,

  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_node_sets_created_at
  ON node_work.node_candidate_sets(created_at);

-- Normalized node candidates (STOP + POI candidates live here)
CREATE TABLE IF NOT EXISTS node_work.node_candidates (
  node_candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  node_set_id       UUID NOT NULL REFERENCES node_work.node_candidate_sets(node_set_id) ON DELETE CASCADE,

  source_run_id     UUID NOT NULL REFERENCES node_raw.overpass_runs(run_id) ON DELETE CASCADE,

  osm_type          TEXT NOT NULL,
  osm_id            BIGINT NOT NULL,

  geom              geometry(Point, 4326) NOT NULL,
  tags              JSONB NOT NULL DEFAULT '{}'::jsonb,

  tag_kind          TEXT NOT NULL,  -- bus_stop/platform/stop_position/station/tram_stop/other
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT chk_node_candidates_osm_type
    CHECK (osm_type IN ('node','way','relation')),

  -- allow same OSM object in multiple node_sets
  CONSTRAINT uq_node_candidates_set_osm UNIQUE (node_set_id, osm_type, osm_id)
);

CREATE INDEX IF NOT EXISTS idx_node_candidates_set
  ON node_work.node_candidates(node_set_id);

CREATE INDEX IF NOT EXISTS idx_node_candidates_run
  ON node_work.node_candidates(source_run_id);

CREATE INDEX IF NOT EXISTS idx_node_candidates_geom_gist
  ON node_work.node_candidates USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_node_candidates_tags_gin
  ON node_work.node_candidates USING GIN (tags);

CREATE INDEX IF NOT EXISTS idx_node_candidates_tag_kind
  ON node_work.node_candidates(tag_kind);

-- Features + STOP vs POI model outputs (LightGBM classifier)
CREATE TABLE IF NOT EXISTS node_work.node_features (
  node_candidate_id UUID PRIMARY KEY REFERENCES node_work.node_candidates(node_candidate_id) ON DELETE CASCADE,

  has_name      BOOLEAN NOT NULL DEFAULT FALSE,
  has_ref       BOOLEAN NOT NULL DEFAULT FALSE,
  has_operator  BOOLEAN NOT NULL DEFAULT FALSE,

  confidence_v0 DOUBLE PRECISION NOT NULL DEFAULT 0.0,

  model_version   TEXT NULL,  -- 'stop_poi_lgbm_v1'
  prob_stop       DOUBLE PRECISION NULL,
  prob_poi        DOUBLE PRECISION NULL,
  node_class_pred TEXT NULL CHECK (node_class_pred IN ('STOP','POI')),

  computed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_node_features_confidence
  ON node_work.node_features(confidence_v0);

-- DBSCAN assignment (per node_set)
CREATE TABLE IF NOT EXISTS node_work.node_clusters (
  node_set_id       UUID NOT NULL REFERENCES node_work.node_candidate_sets(node_set_id) ON DELETE CASCADE,
  node_candidate_id UUID NOT NULL REFERENCES node_work.node_candidates(node_candidate_id) ON DELETE CASCADE,

  cluster_id UUID NOT NULL,  -- internal UUID for cluster
  eps_m      INT NOT NULL,
  min_pts    INT NOT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (node_set_id, node_candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_node_clusters_cluster
  ON node_work.node_clusters(node_set_id, cluster_id);

-- Resolved nodes: one chosen node per cluster inside a node_set
CREATE TABLE IF NOT EXISTS node_work.nodes_resolved (
  node_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- stable internal DataMind id (promotes to prod)
  node_set_id       UUID NOT NULL REFERENCES node_work.node_candidate_sets(node_set_id) ON DELETE CASCADE,

  cluster_id        UUID NOT NULL,
  geom              geometry(Point, 4326) NOT NULL,

  chosen_candidate_id UUID NOT NULL REFERENCES node_work.node_candidates(node_candidate_id) ON DELETE RESTRICT,
  chosen_tags         JSONB NOT NULL DEFAULT '{}'::jsonb,

  confidence        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  status            TEXT NOT NULL DEFAULT 'work' CHECK (status IN ('work','approved','rejected')),
  resolved_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT uq_nodes_resolved_set_cluster UNIQUE (node_set_id, cluster_id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_resolved_set
  ON node_work.nodes_resolved(node_set_id);

CREATE INDEX IF NOT EXISTS idx_nodes_resolved_geom_gist
  ON node_work.nodes_resolved USING GIST (geom);

COMMIT;
