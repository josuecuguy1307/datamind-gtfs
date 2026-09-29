BEGIN;

-- Backward-compatible patch for older route_semantics schema.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.tables
    WHERE table_schema = 'route_prod'
      AND table_name = 'route_semantics'
  ) THEN
    ALTER TABLE route_prod.route_semantics
      ADD COLUMN IF NOT EXISTS model_alias_score DOUBLE PRECISION;
  END IF;
END $$;

COMMIT;
