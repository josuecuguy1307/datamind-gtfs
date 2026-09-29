-- 037_geo_prod_search_indexes.sql
-- Hybrid geocoder support indexes (PostgreSQL-only).

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_geo_place_aliases_norm_trgm
  ON geo_prod.place_aliases USING GIN (normalized_alias gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_geo_place_aliases_norm_fts
  ON geo_prod.place_aliases USING GIN (to_tsvector('simple', normalized_alias));

COMMIT;
