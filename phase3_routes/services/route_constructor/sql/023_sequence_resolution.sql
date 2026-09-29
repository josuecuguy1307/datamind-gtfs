-- ============================================================
-- Phase 3 — sequence resolution / canonical sequence approval
-- Safe additive migration.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS route_work;
CREATE SCHEMA IF NOT EXISTS route_prod;

CREATE TABLE IF NOT EXISTS route_work.sequence_approvals (
  sequence_approval_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NOT NULL REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  stop_sequence_set_id UUID NULL
    REFERENCES route_work.stop_sequence_candidate_sets(set_id) ON DELETE SET NULL,
  chosen_stop_sequence_candidate_id UUID NULL
    REFERENCES route_work.stop_sequence_candidates(candidate_id) ON DELETE SET NULL,
  approval_status TEXT NOT NULL DEFAULT 'approved',
  approved_at TIMESTAMPTZ,
  approved_by TEXT,
  notes TEXT,
  invalidated_at TIMESTAMPTZ,
  invalidated_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_sequence_approvals_status
    CHECK (approval_status IN ('approved', 'invalidated'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sequence_approvals_route
  ON route_work.sequence_approvals(route_id);

CREATE INDEX IF NOT EXISTS idx_sequence_approvals_candidate
  ON route_work.sequence_approvals(chosen_stop_sequence_candidate_id);

CREATE INDEX IF NOT EXISTS idx_sequence_approvals_set
  ON route_work.sequence_approvals(stop_sequence_set_id);

CREATE OR REPLACE FUNCTION route_work.touch_sequence_approval_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sequence_approvals_touch ON route_work.sequence_approvals;

CREATE TRIGGER trg_sequence_approvals_touch
BEFORE UPDATE ON route_work.sequence_approvals
FOR EACH ROW
EXECUTE FUNCTION route_work.touch_sequence_approval_updated_at();

ALTER TABLE route_prod.routes
  ADD COLUMN IF NOT EXISTS chosen_stop_sequence_candidate_id UUID NULL,
  ADD COLUMN IF NOT EXISTS canonical_sequence_ready BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS sequence_approved_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS sequence_approved_by TEXT NULL;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='route_work' AND table_name='stop_sequence_candidates'
  ) THEN
    ALTER TABLE route_prod.routes
      DROP CONSTRAINT IF EXISTS fk_routes_chosen_stop_sequence;

    ALTER TABLE route_prod.routes
      ADD CONSTRAINT fk_routes_chosen_stop_sequence
      FOREIGN KEY (chosen_stop_sequence_candidate_id)
      REFERENCES route_work.stop_sequence_candidates(candidate_id)
      ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_route_prod_chosen_sequence
  ON route_prod.routes(chosen_stop_sequence_candidate_id);

CREATE INDEX IF NOT EXISTS idx_route_prod_canonical_sequence_ready
  ON route_prod.routes(canonical_sequence_ready);

UPDATE route_prod.routes rp
SET service_route_id = COALESCE(rp.service_route_id, rj.service_route_id),
    direction_id = COALESCE(rp.direction_id, rj.direction_id)
FROM route_raw.route_jobs rj
WHERE rj.route_id = rp.route_id
  AND (
    rp.service_route_id IS NULL
    OR rp.direction_id IS NULL
  );

INSERT INTO route_work.sequence_approvals (
  route_id,
  stop_sequence_set_id,
  chosen_stop_sequence_candidate_id,
  approval_status,
  approved_at,
  approved_by,
  notes,
  invalidated_at,
  invalidated_reason
)
SELECT
  ra.route_id,
  ssc.set_id,
  ra.chosen_stop_sequence_candidate_id,
  'approved',
  ra.approved_at,
  ra.approved_by,
  COALESCE(ra.notes, 'backfilled_from_route_approvals'),
  NULL,
  NULL
FROM route_work.route_approvals ra
LEFT JOIN route_work.stop_sequence_candidates ssc
  ON ssc.candidate_id = ra.chosen_stop_sequence_candidate_id
WHERE ra.chosen_stop_sequence_candidate_id IS NOT NULL
ON CONFLICT (route_id)
DO UPDATE SET
  stop_sequence_set_id = EXCLUDED.stop_sequence_set_id,
  chosen_stop_sequence_candidate_id = EXCLUDED.chosen_stop_sequence_candidate_id,
  approval_status = 'approved',
  approved_at = COALESCE(EXCLUDED.approved_at, route_work.sequence_approvals.approved_at),
  approved_by = COALESCE(EXCLUDED.approved_by, route_work.sequence_approvals.approved_by),
  notes = COALESCE(EXCLUDED.notes, route_work.sequence_approvals.notes),
  invalidated_at = NULL,
  invalidated_reason = NULL,
  updated_at = now();

UPDATE route_prod.routes rp
SET chosen_stop_sequence_candidate_id = COALESCE(rp.chosen_stop_sequence_candidate_id, ra.chosen_stop_sequence_candidate_id),
    canonical_sequence_ready = CASE
      WHEN COALESCE(rp.chosen_stop_sequence_candidate_id, ra.chosen_stop_sequence_candidate_id) IS NOT NULL THEN TRUE
      ELSE rp.canonical_sequence_ready
    END,
    sequence_approved_at = COALESCE(rp.sequence_approved_at, sa.approved_at, ra.approved_at),
    sequence_approved_by = COALESCE(rp.sequence_approved_by, sa.approved_by, ra.approved_by)
FROM route_work.route_approvals ra
LEFT JOIN route_work.sequence_approvals sa
  ON sa.route_id = ra.route_id
WHERE rp.route_id = ra.route_id
  AND (
    rp.chosen_stop_sequence_candidate_id IS DISTINCT FROM ra.chosen_stop_sequence_candidate_id
    OR rp.sequence_approved_at IS NULL
    OR rp.sequence_approved_by IS NULL
    OR rp.canonical_sequence_ready = FALSE
  );
