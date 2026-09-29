-- Migration 037 — operator override + reset audit tables for pre-ship cleanup
--
-- Two audit tables that support the cleanup trigger refactor of
-- 2026-04-27 (auto-post-reclassify → operator-confirmed pre-swap):
--
--   1. cleanup_override_audit
--      Logs each time an operator clicks "override and ship" on a
--      cleanup whose safety gate (>20% removal) fired. Carries the
--      written reason and the cleanup report at the moment of
--      override so we can later analyse why operators bypassed the
--      gate and whether those overrides held up.
--
--   2. cleanup_v2_to_postapproval_resets
--      Records rows that were reset out of an "applied" state when
--      the cleanup trigger model changed. The 2026-04-27 reset of 3
--      rows is documented in JSON snapshot at
--      workspace/_audit/preship_cleanup_v2_to_postapproval_reset_2026-04-27.snapshot.json
--      because this table did not yet exist at reset time. Future
--      trigger changes will write directly to this table.

BEGIN;

CREATE TABLE IF NOT EXISTS route_prod.cleanup_override_audit (
  id                          BIGSERIAL PRIMARY KEY,
  queue_id                    UUID         NOT NULL,
  route_code                  TEXT         NOT NULL,
  override_reason             TEXT         NOT NULL,
  override_by                 TEXT,
  override_at                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  cleanup_report_at_override  JSONB
);

CREATE INDEX IF NOT EXISTS ix_cleanup_override_audit_queue
  ON route_prod.cleanup_override_audit (queue_id, override_at DESC);

CREATE TABLE IF NOT EXISTS route_prod.cleanup_v2_to_postapproval_resets (
  id                    BIGSERIAL PRIMARY KEY,
  queue_id              UUID         NOT NULL,
  route_code            TEXT         NOT NULL,
  reset_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  prior_cleanup_report  JSONB
);

CREATE INDEX IF NOT EXISTS ix_cleanup_resets_queue
  ON route_prod.cleanup_v2_to_postapproval_resets (queue_id, reset_at DESC);

COMMIT;
