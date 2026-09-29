-- ============================================================
-- Phase 3 direction-aware route gating
-- service_route_id (logical route) + direction slots (0/1)
-- ============================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS route_raw;
CREATE SCHEMA IF NOT EXISTS route_work;
CREATE SCHEMA IF NOT EXISTS route_prod;

-- ------------------------------------------------------------
-- Logical route identity (shared across both directions)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_raw.service_routes (
  service_route_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_ref TEXT NULL,
  route_name TEXT NULL,
  operator_name TEXT NULL,
  created_by TEXT NULL,
  notes TEXT NULL,
  route_approval_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (route_approval_status IN ('pending', 'in_progress', 'ready', 'approved', 'rejected')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE route_raw.service_routes
  ADD COLUMN IF NOT EXISTS route_ref TEXT NULL,
  ADD COLUMN IF NOT EXISTS route_name TEXT NULL,
  ADD COLUMN IF NOT EXISTS operator_name TEXT NULL,
  ADD COLUMN IF NOT EXISTS created_by TEXT NULL,
  ADD COLUMN IF NOT EXISTS notes TEXT NULL,
  ADD COLUMN IF NOT EXISTS route_approval_status TEXT NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- ------------------------------------------------------------
-- Per-direction progress under one logical route
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_raw.service_route_directions (
  service_route_id UUID NOT NULL
    REFERENCES route_raw.service_routes(service_route_id) ON DELETE CASCADE,
  direction_id SMALLINT NOT NULL
    CHECK (direction_id IN (0, 1)),
  route_id UUID NULL
    REFERENCES route_raw.route_jobs(route_id) ON DELETE SET NULL,

  phase3_progress_step INT NOT NULL DEFAULT 0
    CHECK (phase3_progress_step BETWEEN 0 AND 40),
  progress_notes TEXT NULL,

  direction_approval_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (direction_approval_status IN ('pending', 'in_progress', 'ready', 'approved', 'rejected')),
  geom_source TEXT NOT NULL DEFAULT 'unknown'
    CHECK (geom_source IN ('unknown', 'observed', 'reversed', 'inferred', 'manual')),

  approved_at TIMESTAMPTZ NULL,
  approved_by TEXT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (service_route_id, direction_id)
);

ALTER TABLE route_raw.service_route_directions
  ADD COLUMN IF NOT EXISTS route_id UUID NULL,
  ADD COLUMN IF NOT EXISTS phase3_progress_step INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS progress_notes TEXT NULL,
  ADD COLUMN IF NOT EXISTS direction_approval_status TEXT NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS geom_source TEXT NOT NULL DEFAULT 'unknown',
  ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS approved_by TEXT NULL,
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE route_raw.service_route_directions
  ALTER COLUMN phase3_progress_step SET DEFAULT 0,
  ALTER COLUMN direction_approval_status SET DEFAULT 'pending',
  ALTER COLUMN geom_source SET DEFAULT 'unknown',
  ALTER COLUMN created_at SET DEFAULT now(),
  ALTER COLUMN updated_at SET DEFAULT now();

UPDATE route_raw.service_route_directions
SET
  phase3_progress_step = COALESCE(phase3_progress_step, 0),
  direction_approval_status = COALESCE(NULLIF(direction_approval_status, ''), 'pending'),
  geom_source = COALESCE(NULLIF(geom_source, ''), 'unknown'),
  created_at = COALESCE(created_at, now()),
  updated_at = COALESCE(updated_at, now())
WHERE
  phase3_progress_step IS NULL
  OR direction_approval_status IS NULL
  OR geom_source IS NULL
  OR created_at IS NULL
  OR updated_at IS NULL;

ALTER TABLE route_raw.service_route_directions
  ALTER COLUMN phase3_progress_step SET NOT NULL,
  ALTER COLUMN direction_approval_status SET NOT NULL,
  ALTER COLUMN geom_source SET NOT NULL,
  ALTER COLUMN created_at SET NOT NULL,
  ALTER COLUMN updated_at SET NOT NULL;

ALTER TABLE route_raw.service_route_directions
  DROP CONSTRAINT IF EXISTS chk_service_route_directions_progress_step;

ALTER TABLE route_raw.service_route_directions
  ADD CONSTRAINT chk_service_route_directions_progress_step
  CHECK (phase3_progress_step BETWEEN 0 AND 40);

ALTER TABLE route_raw.service_route_directions
  DROP CONSTRAINT IF EXISTS chk_service_route_directions_direction_approval_status;

ALTER TABLE route_raw.service_route_directions
  ADD CONSTRAINT chk_service_route_directions_direction_approval_status
  CHECK (direction_approval_status IN ('pending', 'in_progress', 'ready', 'approved', 'rejected'));

ALTER TABLE route_raw.service_route_directions
  DROP CONSTRAINT IF EXISTS chk_service_route_directions_geom_source;

ALTER TABLE route_raw.service_route_directions
  ADD CONSTRAINT chk_service_route_directions_geom_source
  CHECK (geom_source IN ('unknown', 'observed', 'reversed', 'inferred', 'manual'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_service_route_direction_route_id
  ON route_raw.service_route_directions(route_id)
  WHERE route_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_service_route_directions_service_route
  ON route_raw.service_route_directions(service_route_id, direction_id);

-- ------------------------------------------------------------
-- Route-level approval after both directions are ready
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_work.service_route_approvals (
  approval_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  service_route_id UUID NOT NULL UNIQUE
    REFERENCES route_raw.service_routes(service_route_id) ON DELETE CASCADE,
  approved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  approved_by TEXT NULL,
  notes TEXT NULL
);

-- ------------------------------------------------------------
-- Attach direction identity to existing route entities
-- ------------------------------------------------------------
ALTER TABLE route_raw.route_jobs
  ADD COLUMN IF NOT EXISTS service_route_id UUID NULL,
  ADD COLUMN IF NOT EXISTS direction_id SMALLINT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.table_constraints
    WHERE table_schema='route_raw'
      AND table_name='route_jobs'
      AND constraint_name='fk_route_jobs_service_route'
  ) THEN
    ALTER TABLE route_raw.route_jobs
      ADD CONSTRAINT fk_route_jobs_service_route
      FOREIGN KEY (service_route_id)
      REFERENCES route_raw.service_routes(service_route_id)
      ON DELETE SET NULL;
  END IF;
END $$;

ALTER TABLE route_raw.route_jobs
  DROP CONSTRAINT IF EXISTS chk_route_jobs_direction_id;

ALTER TABLE route_raw.route_jobs
  ADD CONSTRAINT chk_route_jobs_direction_id
  CHECK (direction_id IS NULL OR direction_id IN (0, 1));

CREATE INDEX IF NOT EXISTS idx_route_jobs_service_direction
  ON route_raw.route_jobs(service_route_id, direction_id, created_at DESC);

ALTER TABLE route_prod.routes
  ADD COLUMN IF NOT EXISTS service_route_id UUID NULL,
  ADD COLUMN IF NOT EXISTS direction_id SMALLINT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.table_constraints
    WHERE table_schema='route_prod'
      AND table_name='routes'
      AND constraint_name='fk_route_prod_service_route'
  ) THEN
    ALTER TABLE route_prod.routes
      ADD CONSTRAINT fk_route_prod_service_route
      FOREIGN KEY (service_route_id)
      REFERENCES route_raw.service_routes(service_route_id)
      ON DELETE SET NULL;
  END IF;
END $$;

ALTER TABLE route_prod.routes
  DROP CONSTRAINT IF EXISTS chk_route_prod_direction_id;

ALTER TABLE route_prod.routes
  ADD CONSTRAINT chk_route_prod_direction_id
  CHECK (direction_id IS NULL OR direction_id IN (0, 1));

CREATE INDEX IF NOT EXISTS idx_route_prod_service_direction
  ON route_prod.routes(service_route_id, direction_id);

-- ------------------------------------------------------------
-- Backfill existing route jobs into service-route model
-- ------------------------------------------------------------

-- One legacy route job => one logical service_route (as baseline).
INSERT INTO route_raw.service_routes (
  service_route_id, route_ref, created_by, notes, route_approval_status
)
SELECT
  rj.route_id,
  NULLIF(rj.known_ref, ''),
  rj.created_by,
  rj.notes,
  CASE WHEN rj.status = 'approved' THEN 'ready' ELSE 'pending' END
FROM route_raw.route_jobs rj
WHERE rj.service_route_id IS NULL
ON CONFLICT (service_route_id) DO NOTHING;

UPDATE route_raw.route_jobs rj
SET
  service_route_id = COALESCE(rj.service_route_id, rj.route_id),
  direction_id = COALESCE(rj.direction_id, 0)
WHERE rj.service_route_id IS NULL
   OR rj.direction_id IS NULL;

INSERT INTO route_raw.service_route_directions (
  service_route_id,
  direction_id,
  route_id,
  phase3_progress_step,
  direction_approval_status,
  geom_source
)
SELECT DISTINCT ON (rj.service_route_id, rj.direction_id)
  rj.service_route_id,
  rj.direction_id,
  rj.route_id,
  CASE
    WHEN EXISTS (
      SELECT 1 FROM route_work.geometry_candidate_sets gcs
      WHERE gcs.route_id = rj.route_id
    ) THEN 3
    WHEN EXISTS (
      SELECT 1 FROM route_work.stop_sequence_candidate_sets scs
      WHERE scs.route_id = rj.route_id
    ) THEN 2
    WHEN rj.chosen_osm_relation_id IS NOT NULL THEN 1
    ELSE 0
  END AS phase3_progress_step,
  CASE
    WHEN EXISTS (
      SELECT 1 FROM route_work.route_approvals ra
      WHERE ra.route_id = rj.route_id
    ) THEN 'approved'
    WHEN EXISTS (
      SELECT 1 FROM route_work.geometry_candidate_sets gcs
      WHERE gcs.route_id = rj.route_id
    ) THEN 'ready'
    ELSE 'in_progress'
  END AS direction_approval_status,
  'observed' AS geom_source
FROM route_raw.route_jobs rj
WHERE rj.service_route_id IS NOT NULL
  AND rj.direction_id IN (0, 1)
  AND NOT EXISTS (
    SELECT 1
    FROM route_raw.service_route_directions srd_existing
    WHERE srd_existing.route_id = rj.route_id
  )
ORDER BY
  rj.service_route_id,
  rj.direction_id,
  CASE WHEN rj.status = 'approved' THEN 0 ELSE 1 END,
  rj.created_at DESC
ON CONFLICT (service_route_id, direction_id) DO UPDATE SET
  route_id = EXCLUDED.route_id,
  phase3_progress_step = GREATEST(route_raw.service_route_directions.phase3_progress_step, EXCLUDED.phase3_progress_step),
  direction_approval_status = CASE
    WHEN EXCLUDED.direction_approval_status = 'approved' THEN 'approved'
    WHEN route_raw.service_route_directions.direction_approval_status = 'approved' THEN 'approved'
    WHEN EXCLUDED.direction_approval_status = 'ready' THEN 'ready'
    ELSE route_raw.service_route_directions.direction_approval_status
  END,
  updated_at = now();

-- Ensure both direction slots exist for every logical service route.
INSERT INTO route_raw.service_route_directions (service_route_id, direction_id, direction_approval_status)
SELECT sr.service_route_id, d.direction_id, 'pending'
FROM route_raw.service_routes sr
CROSS JOIN (VALUES (0), (1)) AS d(direction_id)
WHERE NOT EXISTS (
  SELECT 1
  FROM route_raw.service_route_directions srd
  WHERE srd.service_route_id = sr.service_route_id
    AND srd.direction_id = d.direction_id
);

-- Backfill route_prod routes mapping.
UPDATE route_prod.routes rp
SET
  service_route_id = COALESCE(rp.service_route_id, rj.service_route_id, rp.route_id),
  direction_id = COALESCE(rp.direction_id, rj.direction_id, 0)
FROM route_raw.route_jobs rj
WHERE rj.route_id = rp.route_id
  AND (
    rp.service_route_id IS NULL
    OR rp.direction_id IS NULL
  );

-- Update logical route readiness when both directions have at least Step 30 progress.
UPDATE route_raw.service_routes sr
SET route_approval_status = CASE
  WHEN EXISTS (
    SELECT 1
    FROM route_raw.service_route_directions d0
    JOIN route_raw.service_route_directions d1
      ON d1.service_route_id = d0.service_route_id
     AND d1.direction_id = 1
    WHERE d0.service_route_id = sr.service_route_id
      AND d0.direction_id = 0
      AND COALESCE(d0.phase3_progress_step, 0) >= 3
      AND COALESCE(d1.phase3_progress_step, 0) >= 3
  ) THEN 'ready'
  ELSE COALESCE(sr.route_approval_status, 'pending')
END,
updated_at = now();

-- Route-level approved status if an approval row already exists.
UPDATE route_raw.service_routes sr
SET route_approval_status = 'approved',
    updated_at = now()
WHERE EXISTS (
  SELECT 1
  FROM route_work.service_route_approvals sra
  WHERE sra.service_route_id = sr.service_route_id
);

COMMIT;
