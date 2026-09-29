-- 031_approval_queue.sql
-- =====================================================================
-- Phase 3 approval queue — satellite table for the enforcer pipeline.
--
-- When the policy engine returns ``queue_for_approval`` (either because
-- the geometry enforcer flagged severe mid-shape anomalies, or the stop
-- coverage enforcer hit unroutable / degraded-with-tier5-majority), the
-- coordinator enqueues the route here instead of writing to
-- ``route_prod.routes``. Operator action via the Control Tower panel
-- promotes, rejects, or sends back to Phase 2.
--
-- Separate table (not a column on ``routes``) because:
--   * a pending approval entry is not a promoted route, so it must not
--     participate in GTFS / naming / any downstream trigger that joins
--     ``route_prod.routes``;
--   * queue mutations happen frequently (accept / reject / re-queue)
--     and should not generate row-level churn on the promoted table;
--   * the audit JSONB payloads are fat and benefit from their own
--     TOAST storage.
--
-- Reversible: ``DROP`` block at the bottom undoes the entire migration.
-- =====================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS route_prod;

CREATE TABLE IF NOT EXISTS route_prod.approval_queue (
    queue_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    route_code          TEXT        NOT NULL,
    version             SMALLINT    NOT NULL DEFAULT 1,
    enqueued_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    policy_profile      TEXT        NOT NULL
                        CHECK (policy_profile IN (
                            'conservative',
                            'balanced',
                            'aggressive_supervised'
                        )),
    decision_reasons    JSONB       NOT NULL,
    policy_flags        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    geometry_report     JSONB       NOT NULL,
    stop_coverage_report JSONB      NOT NULL,
    proposed_stops      JSONB       NOT NULL,
    proposed_shape      JSONB       NOT NULL,
    dr_queries_queued   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    dr_queries_deferred JSONB       NOT NULL DEFAULT '[]'::jsonb,
    status              TEXT        NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending',
                            'approved',
                            'rejected',
                            'sent_to_phase2'
                        )),
    resolved_at         TIMESTAMPTZ,
    resolved_by         TEXT,
    resolution_notes    TEXT,
    crashed             BOOLEAN     NOT NULL DEFAULT FALSE,
    crash_payload       JSONB
);

-- Pending routes are the hottest read path for the UI dashboard.
CREATE INDEX IF NOT EXISTS ix_approval_queue_status
    ON route_prod.approval_queue(status);

-- Chronological ordering for the operator inbox.
CREATE INDEX IF NOT EXISTS ix_approval_queue_enqueued
    ON route_prod.approval_queue(enqueued_at DESC);

-- Reverse-lookup for "is this route already waiting for me?" dedupe
-- when the coordinator considers re-enqueueing the same route_code
-- at a bumped version.
CREATE INDEX IF NOT EXISTS ix_approval_queue_route_version
    ON route_prod.approval_queue(route_code, version);

-- Sanity: resolved rows must carry a timestamp.
ALTER TABLE route_prod.approval_queue
    ADD CONSTRAINT approval_queue_resolved_consistency
    CHECK (
        (status IN ('pending')) OR
        (status IN ('approved', 'rejected', 'sent_to_phase2') AND resolved_at IS NOT NULL)
    );

COMMENT ON TABLE  route_prod.approval_queue IS
    'Phase 3 enforcer + policy engine approval queue. Satellite to route_prod.routes; queued routes are not promoted.';
COMMENT ON COLUMN route_prod.approval_queue.decision_reasons IS
    'Human-readable reason strings from hades.enforcers.policy_engine.';
COMMENT ON COLUMN route_prod.approval_queue.proposed_stops IS
    'Stops as they would land in route_prod.routes.stop_node_ids after enforcer enhancement.';
COMMENT ON COLUMN route_prod.approval_queue.dr_queries_queued IS
    'DR Type 2 queries the coordinator successfully booked against the unit budget for this route.';
COMMENT ON COLUMN route_prod.approval_queue.dr_queries_deferred IS
    'DR queries not booked — carries per-query deferred_reason (zone / gap / budget).';

COMMIT;

-- ---------------------------------------------------------------------
-- Reversal block (commented). Run as a single statement to undo.
-- ---------------------------------------------------------------------
-- BEGIN;
-- DROP INDEX IF EXISTS route_prod.ix_approval_queue_route_version;
-- DROP INDEX IF EXISTS route_prod.ix_approval_queue_enqueued;
-- DROP INDEX IF EXISTS route_prod.ix_approval_queue_status;
-- DROP TABLE IF EXISTS route_prod.approval_queue;
-- COMMIT;
