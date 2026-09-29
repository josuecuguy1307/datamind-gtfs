-- ============================================================
-- ROUTE PROD (Phase 3 outputs) + SEMANTICS SEARCH (Phase 4)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS route_prod;
CREATE SCHEMA IF NOT EXISTS semantics;

-- ------------------------------------------------------------
-- Core prod table
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_prod.routes (
  -- Identity
  route_id UUID PRIMARY KEY
    REFERENCES route_raw.route_jobs(route_id) ON DELETE RESTRICT,

  -- Phase 3 outputs
  chosen_geometry_candidate_id UUID NOT NULL,
  geom geometry(LineString, 4326) NOT NULL,

  stop_node_ids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[],
  source TEXT NOT NULL DEFAULT 'route_constructor',

  -- Phase 4 semantics (naming)
  route_name TEXT,
  route_aliases TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  landmark_tags TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  direction_semantics JSONB NOT NULL DEFAULT '{}'::jsonb,
  naming_confidence DOUBLE PRECISION,
  human_verified BOOLEAN NOT NULL DEFAULT FALSE,
  semantics_updated_at TIMESTAMPTZ,

  -- Search support
  search_tsv tsvector,

  -- Timestamps
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- Safety constraints
  CONSTRAINT chk_routes_geom_srid CHECK (ST_SRID(geom) = 4326),
  CONSTRAINT chk_routes_geom_type CHECK (GeometryType(geom) = 'LINESTRING'::text),
  CONSTRAINT chk_routes_geom_points CHECK (ST_NPoints(geom) >= 2)
);

-- ------------------------------------------------------------
-- Ensure columns exist (safe re-run)
-- ------------------------------------------------------------
ALTER TABLE route_prod.routes
  ADD COLUMN IF NOT EXISTS chosen_geometry_candidate_id UUID,
  ADD COLUMN IF NOT EXISTS geom geometry(LineString, 4326),
  ADD COLUMN IF NOT EXISTS stop_node_ids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[],
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'route_constructor',

  ADD COLUMN IF NOT EXISTS route_name TEXT,
  ADD COLUMN IF NOT EXISTS route_aliases TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  ADD COLUMN IF NOT EXISTS landmark_tags TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  ADD COLUMN IF NOT EXISTS direction_semantics JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS naming_confidence DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS human_verified BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS semantics_updated_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS search_tsv tsvector,

  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- ------------------------------------------------------------
-- FK: chosen geometry must exist (add only if route_work exists)
-- ------------------------------------------------------------
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='route_work' AND table_name='geometry_candidates'
  ) THEN
    ALTER TABLE route_prod.routes
      DROP CONSTRAINT IF EXISTS fk_routes_chosen_geom;

    ALTER TABLE route_prod.routes
      ADD CONSTRAINT fk_routes_chosen_geom
      FOREIGN KEY (chosen_geometry_candidate_id)
      REFERENCES route_work.geometry_candidates(geometry_candidate_id)
      ON DELETE RESTRICT;
  END IF;
END $$;

-- ------------------------------------------------------------
-- Indexes
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_route_prod_geom_gist
  ON route_prod.routes USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_route_prod_human_verified
  ON route_prod.routes (human_verified);

CREATE INDEX IF NOT EXISTS idx_route_prod_naming_confidence
  ON route_prod.routes (naming_confidence DESC);

-- ------------------------------------------------------------
-- updated_at trigger
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION route_prod.touch_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_route_prod_touch ON route_prod.routes;

CREATE TRIGGER trg_route_prod_touch
BEFORE UPDATE ON route_prod.routes
FOR EACH ROW
EXECUTE FUNCTION route_prod.touch_updated_at();

-- ------------------------------------------------------------
-- search_tsv sync trigger (keeps index IMMUTABLE-safe)
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION semantics.routes_search_tsv_sync()
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
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_routes_search_tsv_sync ON route_prod.routes;

CREATE TRIGGER trg_routes_search_tsv_sync
BEFORE INSERT OR UPDATE OF route_name, route_aliases, landmark_tags
ON route_prod.routes
FOR EACH ROW
EXECUTE FUNCTION semantics.routes_search_tsv_sync();

-- Backfill existing rows (safe)
UPDATE route_prod.routes
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

CREATE INDEX IF NOT EXISTS idx_routes_search_tsv_gin
  ON route_prod.routes
  USING GIN (search_tsv);

-- ------------------------------------------------------------
-- OPTIONAL: fast partial matches (only if you want it)
--   CREATE EXTENSION IF NOT EXISTS pg_trgm;
--   CREATE INDEX IF NOT EXISTS idx_routes_route_name_trgm
--     ON route_prod.routes USING GIN (route_name gin_trgm_ops);
-- ------------------------------------------------------------
