-- V4_1__semantics_views.sql
-- Phase 4 Admin/Search views + full-text search support
-- IMPORTANT:
-- - Phase 3 owns: route_prod.routes (geom truth)
-- - Phase 4 owns: route_prod.route_semantics (naming + verification + search)
-- - Phase 5 owns publishing/GTFS

CREATE SCHEMA IF NOT EXISTS semantics;
CREATE SCHEMA IF NOT EXISTS route_prod;

-- ------------------------------------------------------------
-- 0) Phase 4 "approved semantics" table (DO NOT store this in routes)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_prod.route_semantics (
  route_id            uuid PRIMARY KEY
                      REFERENCES route_prod.routes(route_id) ON DELETE CASCADE,

  route_name          text NOT NULL,
  route_ref           text NULL,
  operator_name       text NULL,

  route_aliases       text[] NOT NULL DEFAULT ARRAY[]::text[],
  landmark_tags       text[] NOT NULL DEFAULT ARRAY[]::text[],
  direction_semantics jsonb NOT NULL DEFAULT '{}'::jsonb,

  naming_confidence   double precision NOT NULL DEFAULT 0.0,
  human_verified      boolean NOT NULL DEFAULT false,
  semantics_updated_at timestamptz NOT NULL DEFAULT now(),

  -- Stored tsvector (indexable)
  search_tsv          tsvector
);

CREATE INDEX IF NOT EXISTS idx_route_semantics_verified
  ON route_prod.route_semantics (human_verified);

CREATE INDEX IF NOT EXISTS idx_route_semantics_confidence
  ON route_prod.route_semantics (naming_confidence DESC);


-- ------------------------------------------------------------
-- 1) Full-text search (stored column + trigger + GIN index)
-- ------------------------------------------------------------

-- Remove any old indexes that referenced route_prod.routes
DROP INDEX IF EXISTS route_prod.idx_routes_search_tsv_gin;
DROP INDEX IF EXISTS idx_routes_search_tsv_gin;

-- Backfill search_tsv (safe)
UPDATE route_prod.route_semantics
SET search_tsv =
  to_tsvector(
    'simple'::regconfig,
    concat_ws(' ',
      COALESCE(route_name, ''),
      array_to_string(COALESCE(route_aliases, ARRAY[]::text[]), ' '),
      array_to_string(COALESCE(landmark_tags, ARRAY[]::text[]), ' ')
    )
  )
WHERE search_tsv IS NULL;

-- Trigger function
CREATE OR REPLACE FUNCTION semantics.route_semantics_search_tsv_sync()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.search_tsv :=
    to_tsvector(
      'simple'::regconfig,
      concat_ws(' ',
        COALESCE(NEW.route_name, ''),
        array_to_string(COALESCE(NEW.route_aliases, ARRAY[]::text[]), ' '),
        array_to_string(COALESCE(NEW.landmark_tags, ARRAY[]::text[]), ' ')
      )
    );
  NEW.semantics_updated_at := now();
  RETURN NEW;
END;
$$;

-- Drop/recreate trigger safely
DROP TRIGGER IF EXISTS trg_route_semantics_search_tsv_sync ON route_prod.route_semantics;

CREATE TRIGGER trg_route_semantics_search_tsv_sync
BEFORE INSERT OR UPDATE OF route_name, route_aliases, landmark_tags
ON route_prod.route_semantics
FOR EACH ROW
EXECUTE FUNCTION semantics.route_semantics_search_tsv_sync();

-- Index stored tsvector
CREATE INDEX IF NOT EXISTS idx_route_semantics_search_tsv_gin
ON route_prod.route_semantics
USING GIN (search_tsv);


-- ------------------------------------------------------------
-- 2) Pending routes view (no verified semantics yet)
--    LEFT JOIN so routes with no semantics row appear as pending.
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW semantics.v_routes_pending AS
SELECT
  r.route_id,
  r.source,
  r.created_at,
  r.updated_at,

  s.route_name,
  s.route_ref,
  s.operator_name,
  s.route_aliases,
  s.landmark_tags,
  s.direction_semantics,
  s.naming_confidence,
  s.human_verified,
  s.semantics_updated_at,

  ST_AsEWKT(r.geom) AS geometry_ewkt
FROM route_prod.routes r
LEFT JOIN route_prod.route_semantics s
  ON s.route_id = r.route_id
WHERE COALESCE(s.human_verified, false) = false;


-- ------------------------------------------------------------
-- 3) Search view (single doc-like row)
--    Only routes that have semantics rows are searchable.
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW semantics.v_routes_search AS
SELECT
  r.route_id,
  s.route_name,
  s.route_ref,
  s.operator_name,
  s.route_aliases,
  s.landmark_tags,
  s.direction_semantics,
  s.naming_confidence,
  s.human_verified,
  s.semantics_updated_at,

  concat_ws(' ',
    COALESCE(s.route_name, ''),
    array_to_string(COALESCE(s.route_aliases, ARRAY[]::text[]), ' '),
    array_to_string(COALESCE(s.landmark_tags, ARRAY[]::text[]), ' ')
  ) AS search_text
FROM route_prod.routes r
JOIN route_prod.route_semantics s
  ON s.route_id = r.route_id;
