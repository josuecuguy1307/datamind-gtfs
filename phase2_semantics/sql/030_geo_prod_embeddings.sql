-- 030_geo_prod_embeddings.sql
-- Embeddings storage in Postgres using pgvector (recommended for reproducibility / reindex)
-- NOTE: set DIM to your model dimension (example: 384).

BEGIN;

-- pgvector extension (must be installed in Postgres instance)
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS geo_prod;

-- Embeddings for canonical places (optional but useful)
CREATE TABLE IF NOT EXISTS geo_prod.place_embeddings (
  place_id UUID PRIMARY KEY REFERENCES geo_prod.places(place_id) ON DELETE CASCADE,

  model_name    TEXT NOT NULL,  -- e.g. 'multilingual-e5-small'
  model_version TEXT NULL,
  dim           INT  NOT NULL,

  embedding vector(384) NOT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Embeddings for aliases (usually what you search over)
CREATE TABLE IF NOT EXISTS geo_prod.place_alias_embeddings (
  alias_id UUID PRIMARY KEY REFERENCES geo_prod.place_aliases(alias_id) ON DELETE CASCADE,

  place_id UUID NOT NULL REFERENCES geo_prod.places(place_id) ON DELETE CASCADE,

  model_name    TEXT NOT NULL,
  model_version TEXT NULL,
  dim           INT  NOT NULL,

  embedding vector(384) NOT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_geo_alias_emb_place
  ON geo_prod.place_alias_embeddings(place_id);

-- Vector indexes (tune lists based on corpus size; needs ANALYZE for best results)
-- For cosine similarity
CREATE INDEX IF NOT EXISTS idx_geo_alias_emb_ivfflat_cosine
  ON geo_prod.place_alias_embeddings USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_geo_place_emb_ivfflat_cosine
  ON geo_prod.place_embeddings USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);

COMMIT;


CREATE OR REPLACE VIEW geo_prod.v_place_points AS
SELECT
  p.place_id,
  p.canonical_name,
  p.place_type,
  m.node_id,
  COALESCE(m.confidence, 1.0) AS confidence,
  COALESCE(m.mapping_source, 'place_geom') AS mapping_source,
  n.node_type,
  COALESCE(n.geom, p.geom) AS geom,
  ST_Y(COALESCE(n.geom, p.geom))::float8 AS lat,
  ST_X(COALESCE(n.geom, p.geom))::float8 AS lon
FROM geo_prod.places p
LEFT JOIN geo_prod.node_place_map m
  ON m.place_id = p.place_id
LEFT JOIN node_prod.nodes n
  ON n.node_id = m.node_id
WHERE COALESCE(n.geom, p.geom) IS NOT NULL;


CREATE OR REPLACE VIEW geo_prod.v_place_summary AS
SELECT
  p.place_id,
  p.canonical_name,
  p.place_type,
  COUNT(m.node_id)::int AS n_nodes,
  AVG(m.confidence)::float8 AS avg_confidence,
  MIN(m.confidence)::float8 AS min_confidence,
  MAX(m.confidence)::float8 AS max_confidence,
  COALESCE(
    CASE
      WHEN COUNT(n.geom) > 0 THEN ST_Centroid(ST_Collect(n.geom))::geometry(Point,4326)
      ELSE NULL::geometry(Point,4326)
    END,
    p.geom
  ) AS center_geom,
  CASE
    WHEN COUNT(n.geom) > 0 THEN ST_Extent(n.geom)::text
    WHEN p.geom IS NOT NULL THEN ST_AsText(ST_Envelope(p.geom))
    ELSE NULL::text
  END AS bbox
FROM geo_prod.places p
LEFT JOIN geo_prod.node_place_map m
  ON m.place_id = p.place_id
LEFT JOIN node_prod.nodes n
  ON n.node_id = m.node_id
GROUP BY
  p.place_id, p.canonical_name, p.place_type, p.geom;



CREATE OR REPLACE VIEW geo_work.v_place_set_points AS
SELECT
  m.place_set_id,
  m.place_candidate_id,
  pc.proposed_canonical_name,
  pc.proposed_place_type,
  m.node_id,
  m.confidence,
  m.mapping_source,
  n.node_type,
  n.geom,
  ST_Y(n.geom)::float8 AS lat,
  ST_X(n.geom)::float8 AS lon
FROM geo_work.node_place_map_work m
JOIN node_prod.nodes n
  ON n.node_id = m.node_id
JOIN geo_work.place_candidates pc
  ON pc.place_candidate_id = m.place_candidate_id;


CREATE INDEX IF NOT EXISTS idx_node_prod_nodes_geom_gist
  ON node_prod.nodes USING GIST (geom);


CREATE INDEX IF NOT EXISTS idx_geo_node_place_map_work_set
  ON geo_work.node_place_map_work(place_set_id);

CREATE INDEX IF NOT EXISTS idx_geo_node_place_map_work_place
  ON geo_work.node_place_map_work(place_set_id, place_candidate_id);
