-- 035_dr_batch_dependencies.sql
-- =====================================================================
-- Phase 3 — DR-batch ↔ route-gap dependency tracker.
--
-- Each row says: "approval_queue row for route_id is waiting on
-- gap_idx, and we predict that DR batch <dr_batch_id> will resolve it
-- (because the gap midpoint sits inside the batch's bounding box)."
--
-- Lifecycle of a row
--   waiting              — created by populate_dr_dependencies the
--                          moment the v2 lands in approval_queue.
--   batch_processed      — the batch's response file imported, but the
--                          validator has not yet ruled on this gap.
--   landmark_found       — validated landmark exists for this gap;
--                          the reclassifier will bump quality_class.
--   landmark_not_found   — validator finished and no landmark is
--                          available; the reclassifier moves the row
--                          to degraded / unroutable as appropriate.
--
-- Schema choices
--   * route_id is UUID (matches route_prod.routes.route_id) — we
--     deliberately do NOT add an FK so a row can outlive a route deletion
--     without blocking the migration. The reclassifier silently drops
--     orphan rows.
--   * UNIQUE(dr_batch_id, route_id, gap_idx) prevents the populator
--     from writing duplicates on re-runs (it uses ON CONFLICT DO NOTHING).
--
-- Reversible: DROP block at the bottom.
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS route_prod.dr_batch_dependencies (
    dependency_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dr_batch_id     TEXT       NOT NULL,
    route_id        UUID       NOT NULL,
    gap_idx         SMALLINT   NOT NULL,
    gap_coords      JSONB,
    status          TEXT       NOT NULL DEFAULT 'waiting'
                    CHECK (status IN (
                        'waiting',
                        'batch_processed',
                        'landmark_found',
                        'landmark_not_found'
                    )),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    UNIQUE (dr_batch_id, route_id, gap_idx)
);

-- "Which routes does batch X unblock?" — used by reclassifier.batch().
CREATE INDEX IF NOT EXISTS ix_dr_batch_deps_batch
    ON route_prod.dr_batch_dependencies(dr_batch_id);

-- "What is route Y waiting on?" — used by the dashboard drill-down
-- and by the worker when it re-INSERTs a route that already had deps.
CREATE INDEX IF NOT EXISTS ix_dr_batch_deps_route
    ON route_prod.dr_batch_dependencies(route_id);

-- Partial — the "still blocked" view scans only this slice.
CREATE INDEX IF NOT EXISTS ix_dr_batch_deps_waiting
    ON route_prod.dr_batch_dependencies(status)
    WHERE status = 'waiting';

COMMENT ON TABLE  route_prod.dr_batch_dependencies IS
    'Per-gap predictions of which DR batch will unblock which approval_queue row. Populated by hades.enforcers.reclassifier.populate_dr_dependencies; consumed by the reclassifier when batches land.';
COMMENT ON COLUMN route_prod.dr_batch_dependencies.dr_batch_id IS
    'Existing multi-route bundle id (e.g. "batch_12_sangolqui_rumi") OR a unit-prefixed on-demand id (e.g. "qc_007"). Both formats coexist by design.';
COMMENT ON COLUMN route_prod.dr_batch_dependencies.gap_coords IS
    'Gap midpoint as {"lat": <decimal>, "lon": <decimal>}. Used by the dashboard to render the dependency without re-reading stop_coverage_report.';

COMMIT;

-- ---------------------------------------------------------------------
-- Reversal block (commented). Run as a single statement to undo.
-- ---------------------------------------------------------------------
-- BEGIN;
-- DROP INDEX IF EXISTS route_prod.ix_dr_batch_deps_waiting;
-- DROP INDEX IF EXISTS route_prod.ix_dr_batch_deps_route;
-- DROP INDEX IF EXISTS route_prod.ix_dr_batch_deps_batch;
-- DROP TABLE IF EXISTS route_prod.dr_batch_dependencies;
-- COMMIT;
