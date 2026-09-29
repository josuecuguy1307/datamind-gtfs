-- 038_refill_columns.sql
-- =====================================================================
-- Phase 3 — Stop Refill columns on approval_queue + refill audit table.
--
-- Stop Refill is the inverse complement to Pre-Ship Orphan Cleanup. While
-- cleanup REMOVES stops that drift from the polyline, refill ADDS stops
-- that the polyline visits but proposed_stops omits — sourcing them from
-- routes already shipped to route_prod.routes.
--
-- The refill phase is operator-driven (per-candidate accept/skip/review,
-- no bulk acceptance) and runs after cleanup but before swap inside
-- confirm_swap_with_cleanup. This migration adds the satellite columns
-- that record:
--
--   * proposed_refill_candidates — JSONB list of scored candidates
--     produced by hades.enforcers.stop_refill at preview time.
--   * refill_decisions — JSONB map of stop_id → decision
--     ("accepted" | "skipped" | "reviewed_later") populated as the
--     operator clicks through the candidate list.
--   * refill_applied — TRUE iff the swap was confirmed AND at least one
--     refill candidate was accepted (so the swapped v2 stops include the
--     refilled stops). FALSE for rows where refill was previewed but no
--     candidate accepted, or rows that never reached refill.
--   * refill_at — timestamp of refill application (set with
--     refill_applied = TRUE).
--
-- The companion table route_prod.refill_audit logs every per-candidate
-- decision so we can later analyse acceptance rates, the distribution of
-- candidate scores, and whether operators trust the algorithm. One row
-- per (queue_id, candidate_stop_id, decision) tuple.
--
-- Schema choices
--   * proposed_refill_candidates and refill_decisions are JSONB and
--     nullable — populated only after preview/decisions begin.
--   * refill_applied is NOT NULL DEFAULT FALSE so callers can filter
--     "rows that need refill" without NULL handling.
--   * refill_audit.decision is CHECK-constrained to the three valid
--     values; refill_audit.candidate_score is NUMERIC so high-precision
--     scoring math survives round-trip.
--
-- Index choices
--   * ix_approval_queue_refill_pending on
--     (refill_applied, quality_class, status) WHERE status='pending' AND
--     refill_applied=FALSE — mirrors 036's cleanup-needed index for the
--     "find me rows ready for refill review" hot path.
--   * ix_refill_audit_queue on (queue_id, decided_at DESC) for the audit
--     panel that lists decisions per route.
--   * ix_refill_audit_stop on (candidate_stop_id) for the rare query
--     "show me everywhere this stop has ever been considered for refill".
--
-- Reversible: DROP block at the bottom undoes the migration.
-- =====================================================================

BEGIN;

ALTER TABLE route_prod.approval_queue
    ADD COLUMN IF NOT EXISTS proposed_refill_candidates JSONB,
    ADD COLUMN IF NOT EXISTS refill_decisions           JSONB,
    ADD COLUMN IF NOT EXISTS refill_applied             BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS refill_at                  TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_approval_queue_refill_pending
    ON route_prod.approval_queue (refill_applied, quality_class, status)
    WHERE status = 'pending'
      AND refill_applied = FALSE
      AND quality_class IN ('good', 'acceptable', 'ship_pending_dr');

COMMENT ON COLUMN route_prod.approval_queue.proposed_refill_candidates IS
    'List of RefillCandidate dicts produced by hades.enforcers.stop_refill at preview time. NULL until preview runs.';
COMMENT ON COLUMN route_prod.approval_queue.refill_decisions IS
    'Map of stop_id → operator decision ("accepted" | "skipped" | "reviewed_later"). NULL until at least one decision recorded.';
COMMENT ON COLUMN route_prod.approval_queue.refill_applied IS
    'TRUE iff the swap was confirmed AND at least one refill candidate was accepted into v2.';
COMMENT ON COLUMN route_prod.approval_queue.refill_at IS
    'Timestamp when refill decisions were applied (set together with refill_applied=TRUE).';

CREATE TABLE IF NOT EXISTS route_prod.refill_audit (
    id                  BIGSERIAL PRIMARY KEY,
    queue_id            UUID         NOT NULL,
    route_code          TEXT         NOT NULL,
    candidate_stop_id   UUID         NOT NULL,
    decision            TEXT         NOT NULL
        CHECK (decision IN ('accepted', 'skipped', 'reviewed_later')),
    decided_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    decided_by          TEXT,
    candidate_score     NUMERIC,
    candidate_metadata  JSONB
);

CREATE INDEX IF NOT EXISTS ix_refill_audit_queue
    ON route_prod.refill_audit (queue_id, decided_at DESC);

CREATE INDEX IF NOT EXISTS ix_refill_audit_stop
    ON route_prod.refill_audit (candidate_stop_id);

COMMENT ON TABLE route_prod.refill_audit IS
    'One row per operator decision on a refill candidate. Lets us replay why a stop was/was not refilled into a given route, and analyse algorithm acceptance over time.';

COMMIT;

-- ---------------------------------------------------------------------
-- Reversal block (commented). Run as a single transaction to undo.
-- ---------------------------------------------------------------------
-- BEGIN;
-- DROP INDEX IF EXISTS route_prod.ix_refill_audit_stop;
-- DROP INDEX IF EXISTS route_prod.ix_refill_audit_queue;
-- DROP TABLE IF EXISTS route_prod.refill_audit;
-- DROP INDEX IF EXISTS route_prod.ix_approval_queue_refill_pending;
-- ALTER TABLE route_prod.approval_queue
--     DROP COLUMN IF EXISTS refill_at,
--     DROP COLUMN IF EXISTS refill_applied,
--     DROP COLUMN IF EXISTS refill_decisions,
--     DROP COLUMN IF EXISTS proposed_refill_candidates;
-- COMMIT;
