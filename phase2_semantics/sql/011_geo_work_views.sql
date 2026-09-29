-- 011_geo_work_views.sql
-- Views + helper function:
-- - normalize_alias(text)
-- - v_node_semantic_evidence (extract common name fields from node_prod.nodes.chosen_tags)

BEGIN;

CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE SCHEMA IF NOT EXISTS geo_work;

-- Helper: normalize alias for robust matching (typos aside)
-- NOTE: keep it stable, because it becomes part of your search identity.
CREATE OR REPLACE FUNCTION geo_work.normalize_alias(s TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT
    regexp_replace(
      regexp_replace(
        lower(unaccent(coalesce(s,''))),
        '[^a-z0-9\s]+', ' ', 'g'
      ),
      '\s+', ' ', 'g'
    )::text
$$;


CREATE OR REPLACE VIEW geo_work.v_node_semantic_evidence AS
SELECT
  n.node_id,
  n.node_type,
  n.geom,
  n.chosen_tags,

  -- best-effort fields
  NULLIF(n.chosen_tags->>'name', '')          AS name,
  NULLIF(n.chosen_tags->>'name:es', '')       AS name_es,
  NULLIF(n.chosen_tags->>'official_name', '') AS official_name,
  NULLIF(n.chosen_tags->>'short_name', '')    AS short_name,
  NULLIF(n.chosen_tags->>'alt_name', '')      AS alt_name,
  NULLIF(n.chosen_tags->>'ref', '')           AS ref,
  NULLIF(n.chosen_tags->>'operator', '')      AS operator,
  NULLIF(n.chosen_tags->>'network', '')       AS network

FROM node_prod.nodes n;

COMMIT;
