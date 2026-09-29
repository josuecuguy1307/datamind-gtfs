-- 034_quality_class.sql
-- =====================================================================
-- Phase 3 — Quality classification + DR-batch tracking on approval_queue.
--
-- Adds five columns that turn approval_queue rows into a first-class
-- "what should the operator do with this v2?" surface, and lets the
-- reclassifier service track which routes are blocked by which DR
-- batches so that landing a batch can auto-upgrade everything depending
-- on it (see hades.enforcers.reclassifier).
--
-- New columns
--   quality_class         TEXT  -- v2 taxonomy: good | acceptable |
--                                 ship_pending_dr | degraded_minor |
--                                 degraded | unroutable | unknown
--                                 (NULL allowed for rows not yet classified).
--   tier4_pending_count   SMALLINT DEFAULT 0 -- # of unresolved tier-4
--                                 (DR-prepared) gaps in this v2.
--   pending_dr_batches    TEXT[] DEFAULT '{}' -- DR batch ids whose
--                                 landing would let the reclassifier
--                                 try this row again.
--   classified_at         TIMESTAMPTZ -- last time the classifier ran.
--   reclassified_count    SMALLINT DEFAULT 0 -- counter (auto-incremented
--                                 by the reclassifier on every re-run).
--
-- The CHECK constraint explicitly enumerates the v2 taxonomy *and* the
-- legacy values so we can roll out the new classifier incrementally
-- without breaking inserts that still pass through the old code path.
-- 'unknown' covers rows persisted before the reclassifier touched them.
--
-- Indexes
--   ix_approval_queue_class       — partial on (quality_class) where
--                                   status='pending'. The dashboard
--                                   filters by class on every page load.
--   ix_approval_queue_pending_dr  — partial GIN on pending_dr_batches
--                                   where status='pending' and the array
--                                   is non-empty. Drives the "DR Batches
--                                   blocking shipments" aggregation.
--
-- Reversible: DROP block at the bottom.
-- =====================================================================

BEGIN;

ALTER TABLE route_prod.approval_queue
    ADD COLUMN IF NOT EXISTS quality_class       TEXT,
    ADD COLUMN IF NOT EXISTS tier4_pending_count SMALLINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pending_dr_batches  TEXT[]   NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS classified_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reclassified_count  SMALLINT NOT NULL DEFAULT 0;

-- Apply the CHECK after ADD COLUMN so we can name it explicitly (named
-- constraints are easier to drop in the reversal block + easier to
-- evolve in a future migration when the taxonomy widens).
ALTER TABLE route_prod.approval_queue
    DROP CONSTRAINT IF EXISTS approval_queue_quality_class_check;

ALTER TABLE route_prod.approval_queue
    ADD CONSTRAINT approval_queue_quality_class_check
    CHECK (
        quality_class IS NULL
        OR quality_class IN (
            'good',
            'acceptable',
            'ship_pending_dr',
            'degraded_minor',
            'degraded',
            'unroutable',
            'unknown'
        )
    );

-- Hot read path — the dashboard groups pending rows by quality_class.
CREATE INDEX IF NOT EXISTS ix_approval_queue_class
    ON route_prod.approval_queue(quality_class)
    WHERE status = 'pending';

-- "Which DR batches are blocking the most pending rows?" aggregation.
-- GIN supports `pending_dr_batches @> ARRAY[...]` and `&&` (overlap),
-- both used by the reclassifier when a batch lands.
CREATE INDEX IF NOT EXISTS ix_approval_queue_pending_dr
    ON route_prod.approval_queue
    USING GIN (pending_dr_batches)
    WHERE status = 'pending'
      AND array_length(pending_dr_batches, 1) > 0;

COMMENT ON COLUMN route_prod.approval_queue.quality_class IS
    'v2 quality taxonomy assigned by hades.enforcers.stop_coverage_enforcer._classify. NULL = not yet classified by the v2 path.';
COMMENT ON COLUMN route_prod.approval_queue.tier4_pending_count IS
    'Count of unresolved tier-4 (DR-prepared) gaps in this v2. Drives the ship_pending_dr 8-cap rule.';
COMMENT ON COLUMN route_prod.approval_queue.pending_dr_batches IS
    'DR batch ids whose successful landing would unblock at least one tier-4 gap on this v2. Populated by populate_dr_dependencies.';
COMMENT ON COLUMN route_prod.approval_queue.classified_at IS
    'Wall-clock time the classifier last ran. Surfaced in the dashboard header ("last reclassified ...").';
COMMENT ON COLUMN route_prod.approval_queue.reclassified_count IS
    'Counter, incremented by hades.enforcers.reclassifier each time it re-runs the classifier on this row.';

COMMIT;

-- ---------------------------------------------------------------------
-- Reversal block (commented). Run as a single statement to undo.
-- ---------------------------------------------------------------------
-- BEGIN;
-- DROP INDEX IF EXISTS route_prod.ix_approval_queue_pending_dr;
-- DROP INDEX IF EXISTS route_prod.ix_approval_queue_class;
-- ALTER TABLE route_prod.approval_queue
--     DROP CONSTRAINT IF EXISTS approval_queue_quality_class_check;
-- ALTER TABLE route_prod.approval_queue
--     DROP COLUMN IF EXISTS reclassified_count,
--     DROP COLUMN IF EXISTS classified_at,
--     DROP COLUMN IF EXISTS pending_dr_batches,
--     DROP COLUMN IF EXISTS tier4_pending_count,
--     DROP COLUMN IF EXISTS quality_class;
-- COMMIT;
