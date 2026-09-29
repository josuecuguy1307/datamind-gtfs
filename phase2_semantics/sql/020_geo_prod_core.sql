-- 020_geo_prod_core.sql
-- Phase 2 final output:
-- - geo_prod.places
-- - geo_prod.place_aliases
-- - geo_prod.node_place_map

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE SCHEMA IF NOT EXISTS geo_prod;

-- Canonical places (meaning entities)
CREATE TABLE IF NOT EXISTS geo_prod.places (
  place_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  canonical_name TEXT NOT NULL,
  place_type     TEXT NOT NULL CHECK (place_type IN ('STOP','POI','STATION','TERMINAL','OTHER')),
  geom           geometry(Point,4326) NULL,

  region     TEXT NULL, -- optional (city/parish)
  status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated')),

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE geo_prod.places
  ADD COLUMN IF NOT EXISTS geom geometry(Point,4326);

CREATE INDEX IF NOT EXISTS idx_geo_places_type
  ON geo_prod.places(place_type);

CREATE INDEX IF NOT EXISTS idx_geo_places_name
  ON geo_prod.places(canonical_name);

CREATE INDEX IF NOT EXISTS idx_geo_places_geom_gist
  ON geo_prod.places USING GIST (geom);

-- Aliases that should match search
CREATE TABLE IF NOT EXISTS geo_prod.place_aliases (
  alias_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  place_id UUID NOT NULL REFERENCES geo_prod.places(place_id) ON DELETE CASCADE,

  alias            TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,

  alias_kind TEXT NOT NULL DEFAULT 'alt' CHECK (alias_kind IN ('official','short','alt','abbr','historic','typo_common')),
  lang       TEXT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT uq_geo_place_aliases_place_norm UNIQUE (place_id, normalized_alias),
  CONSTRAINT uq_geo_place_aliases_norm_global UNIQUE (normalized_alias, place_id)  -- same as above, keeps intent explicit
);

CREATE INDEX IF NOT EXISTS idx_geo_place_aliases_place
  ON geo_prod.place_aliases(place_id);

CREATE INDEX IF NOT EXISTS idx_geo_place_aliases_norm
  ON geo_prod.place_aliases(normalized_alias);

-- Node -> Place mapping (meaning link)
CREATE TABLE IF NOT EXISTS geo_prod.node_place_map (
  node_id  UUID PRIMARY KEY,  -- references node_prod.nodes(node_id) (no FK by design)
  place_id UUID NOT NULL REFERENCES geo_prod.places(place_id) ON DELETE RESTRICT,

  confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  mapping_source TEXT NOT NULL DEFAULT 'phase2_auto'
    CHECK (mapping_source IN ('phase2_auto','user_selected','manual_override')),

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_geo_node_place_map_place
  ON geo_prod.node_place_map(place_id);

COMMIT;
