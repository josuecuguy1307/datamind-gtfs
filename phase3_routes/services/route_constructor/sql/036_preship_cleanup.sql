-- 036_preship_cleanup.sql
-- =====================================================================
-- Phase 3 — Pre-Ship Orphan Cleanup columns on approval_queue.
--
-- Pre-Ship Orphan Cleanup is the last step before a route can be
-- shipped (operator approves the swap from v1 to v2). It removes
-- orphan stops that are not aligned with the route polyline, projects
-- borderline stops onto the polyline, and keeps everything else
-- unchanged. See ``hades.enforcers.orphan_cleanup`` for the algorithm
-- and ``hades.enforcers.stop_polyline_alignment`` for the classifier.
--
-- This migration adds the satellite columns that record whether the
-- cleanup was applied for a given approval_queue row, the structured
-- per-stop report, and the timestamp of application.
--
-- Schema choices
--   * pre_ship_cleanup_applied is NOT NULL DEFAULT FALSE so the
--     reclassifier's trigger condition (``= FALSE`` for routes that
--     still need cleanup) does not need NULL-handling branches.
--   * pre_ship_cleanup_report is JSONB and nullable — it is populated
--     only after a cleanup attempt; rejected attempts (safety gate
--     hit) leave the column NULL and ``applied`` remains FALSE.
--   * pre_ship_cleanup_at is TIMESTAMPTZ and nullable — set together
--     with ``applied = TRUE``.
--
-- Index choice
--   * Partial index on (applied, quality_class, status) keys the hot
--     read path: "find me the next batch of approval_queue rows that
--     need cleanup". The WHERE clause keeps the index small — only
--     pending rows in shippable classes that have not yet been cleaned.
--
-- Reversible: DROP block at the bottom undoes the migration.
-- =====================================================================

BEGIN;

ALTER TABLE route_prod.approval_queue
    ADD COLUMN IF NOT EXISTS pre_ship_cleanup_applied BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS pre_ship_cleanup_report  JSONB,
    ADD COLUMN IF NOT EXISTS pre_ship_cleanup_at      TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_approval_queue_cleanup_needed
    ON route_prod.approval_queue (pre_ship_cleanup_applied, quality_class, status)
    WHERE status = 'pending'
      AND pre_ship_cleanup_applied = FALSE
      AND quality_class IN ('good', 'acceptable', 'ship_pending_dr');

COMMENT ON COLUMN route_prod.approval_queue.pre_ship_cleanup_applied IS
    'TRUE iff hades.enforcers.orphan_cleanup applied to this row and the safety gate accepted the result. FALSE for not-yet-attempted or rejected cleanups.';
COMMENT ON COLUMN route_prod.approval_queue.pre_ship_cleanup_report IS
    'OrphanCleanupReport serialized as JSONB — counts, projections, removed orphans, thresholds. NULL when cleanup has not run.';
COMMENT ON COLUMN route_prod.approval_queue.pre_ship_cleanup_at IS
    'Timestamp when cleanup was applied (set together with pre_ship_cleanup_applied=TRUE).';

COMMIT;

-- ---------------------------------------------------------------------
-- Reversal block (commented). Run as a single transaction to undo.
-- ---------------------------------------------------------------------
-- BEGIN;
-- DROP INDEX IF EXISTS route_prod.ix_approval_queue_cleanup_needed;
-- ALTER TABLE route_prod.approval_queue
--     DROP COLUMN IF EXISTS pre_ship_cleanup_at,
--     DROP COLUMN IF EXISTS pre_ship_cleanup_report,
--     DROP COLUMN IF EXISTS pre_ship_cleanup_applied;
-- COMMIT;
