-- Block 2.1: Active places view
-- All downstream consumers should use this instead of geo_prod.places directly.

CREATE OR REPLACE VIEW geo_prod.v_active_places AS
SELECT
    p.place_id,
    p.canonical_name,
    p.place_type,
    p.region,
    p.status,
    p.geom,
    p.created_at,
    p.updated_at
FROM geo_prod.places p
WHERE p.status = 'active';

-- Active aliases (only for active places)
CREATE OR REPLACE VIEW geo_prod.v_active_aliases AS
SELECT
    a.alias_id,
    a.place_id,
    a.alias,
    a.normalized_alias,
    a.alias_kind,
    a.lang,
    a.created_at,
    a.updated_at
FROM geo_prod.place_aliases a
JOIN geo_prod.places p ON p.place_id = a.place_id
WHERE p.status = 'active';

-- Active alias embeddings (only for active places)
CREATE OR REPLACE VIEW geo_prod.v_active_alias_embeddings AS
SELECT
    e.alias_id,
    e.place_id,
    e.model_name,
    e.dim,
    e.embedding,
    e.created_at,
    e.updated_at
FROM geo_prod.place_alias_embeddings e
JOIN geo_prod.place_aliases a ON a.alias_id = e.alias_id
JOIN geo_prod.places p ON p.place_id = a.place_id
WHERE p.status = 'active';
