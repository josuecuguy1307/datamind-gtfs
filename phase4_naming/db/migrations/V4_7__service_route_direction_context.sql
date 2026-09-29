BEGIN;

CREATE SCHEMA IF NOT EXISTS route_prod;
CREATE SCHEMA IF NOT EXISTS semantics;

ALTER TABLE route_prod.route_semantics
  ADD COLUMN IF NOT EXISTS service_route_id uuid,
  ADD COLUMN IF NOT EXISTS direction_id smallint;

ALTER TABLE route_prod.route_semantics
  DROP CONSTRAINT IF EXISTS chk_route_semantics_direction_id;

ALTER TABLE route_prod.route_semantics
  ADD CONSTRAINT chk_route_semantics_direction_id
  CHECK (direction_id IS NULL OR direction_id IN (0, 1));

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.tables
    WHERE table_schema = 'route_raw' AND table_name = 'service_routes'
  ) THEN
    ALTER TABLE route_prod.route_semantics
      DROP CONSTRAINT IF EXISTS fk_route_semantics_service_route;

    ALTER TABLE route_prod.route_semantics
      ADD CONSTRAINT fk_route_semantics_service_route
      FOREIGN KEY (service_route_id)
      REFERENCES route_raw.service_routes(service_route_id)
      ON DELETE SET NULL;
  END IF;
END $$;

UPDATE route_prod.route_semantics rs
SET
  service_route_id = r.service_route_id,
  direction_id = r.direction_id
FROM route_prod.routes r
WHERE r.route_id = rs.route_id
  AND (
    rs.service_route_id IS DISTINCT FROM r.service_route_id
    OR rs.direction_id IS DISTINCT FROM r.direction_id
  );

CREATE INDEX IF NOT EXISTS idx_route_semantics_service_direction
  ON route_prod.route_semantics (service_route_id, direction_id);

CREATE OR REPLACE FUNCTION semantics.sync_route_semantics_direction_context()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  SELECT r.service_route_id, r.direction_id
  INTO NEW.service_route_id, NEW.direction_id
  FROM route_prod.routes r
  WHERE r.route_id = NEW.route_id;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_route_semantics_sync_direction_context ON route_prod.route_semantics;

CREATE TRIGGER trg_route_semantics_sync_direction_context
BEFORE INSERT OR UPDATE OF route_id
ON route_prod.route_semantics
FOR EACH ROW
EXECUTE FUNCTION semantics.sync_route_semantics_direction_context();

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
  ST_AsEWKT(r.geom) AS geometry_ewkt,
  r.service_route_id,
  r.direction_id
FROM route_prod.routes r
LEFT JOIN route_prod.route_semantics s
  ON s.route_id = r.route_id
WHERE COALESCE(s.human_verified, false) = false;

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
  ) AS search_text,
  r.service_route_id,
  r.direction_id
FROM route_prod.routes r
JOIN route_prod.route_semantics s
  ON s.route_id = r.route_id;

DO $$
BEGIN
  IF to_regclass('semantics.v_phase4_name_candidates_latest') IS NOT NULL THEN
    EXECUTE $v$
      CREATE OR REPLACE VIEW semantics.v_phase4_review_queue AS
      SELECT
        c.route_id,
        c.run_id,
        count(*)::int AS n_candidates,
        max(c.generated_at) AS candidates_generated_at,
        bool_or(f.is_winner) AS has_winner,
        max(f.created_at) AS reviewed_at,
        r.service_route_id,
        r.direction_id
      FROM semantics.v_phase4_name_candidates_latest c
      JOIN route_prod.routes r
        ON r.route_id = c.route_id
      LEFT JOIN semantics.route_name_feedback f
        ON f.route_id = c.route_id
       AND f.candidate_id = c.candidate_id
      GROUP BY c.route_id, c.run_id, r.service_route_id, r.direction_id
      ORDER BY candidates_generated_at DESC
    $v$;
  END IF;
END $$;

COMMIT;
