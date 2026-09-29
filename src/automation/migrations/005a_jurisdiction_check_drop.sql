-- ═══════════════════════════════════════════════════════════════════
-- 005a_jurisdiction_check_drop.sql — applied 2026-04-09
--
-- Purpose
-- -------
-- Drop the hard-coded jurisdiction whitelist on
-- catalog.route_semantics. The previous CHECK constraint rejected
-- any jurisdiction string other than 'DMQ', 'ANT', 'MIXED', which
-- blocks every non-Sample Region province (ATM_GUAYAQUIL, ATD_DURAN,
-- GAD_DURAN, EMT_*, etc.) from writing rows into the shared catalog.
--
-- This is the NON-BREAKING half of migration 005. The breaking
-- half (dropping route_prod.routes.province DEFAULT='sample_region')
-- is split into 005b and gated on a writer audit that is NOT yet
-- complete — see workspace/_audit/migration_005_writer_audit_20260409.md.
--
-- Safety
-- ------
-- - Step is a pure constraint relaxation: any row already valid
--   remains valid; new rows with previously-rejected jurisdictions
--   can now be inserted.
-- - No data modification, no column change, no row touched.
-- - A replacement soft sanity check (non-empty trimmed string) is
--   added so the column still rejects obvious garbage.
-- - Trivially reversible (see rollback block at bottom).
--
-- Skill citation
-- --------------
-- 11_MULTI_PROVINCE_OPERATIONS.md §6: "Postgres province column has
-- no CHECK, no enum, no FK. Any string is accepted." The same rule
-- applies by analogy to jurisdiction — jurisdictions live in
-- supported_provinces.json / territorial_keys.json, not in a CHECK
-- constraint.
--
-- Pre-apply snapshot (2026-04-09T20:20Z, local datamind_ml)
-- ---------------------------------------------------------
--   catalog.route_semantics rows: 466 total
--     DMQ   352
--     MIXED  57
--     ANT    57
-- ═══════════════════════════════════════════════════════════════════

BEGIN;

ALTER TABLE catalog.route_semantics
    DROP CONSTRAINT IF EXISTS route_semantics_jurisdiction_check;

ALTER TABLE catalog.route_semantics
    ADD CONSTRAINT route_semantics_jurisdiction_nonempty
    CHECK (length(trim(jurisdiction)) > 0);

COMMIT;

-- Rollback (for incident recovery):
--
-- BEGIN;
-- ALTER TABLE catalog.route_semantics
--     DROP CONSTRAINT IF EXISTS route_semantics_jurisdiction_nonempty;
-- ALTER TABLE catalog.route_semantics
--     ADD CONSTRAINT route_semantics_jurisdiction_check
--     CHECK (jurisdiction = ANY (ARRAY['DMQ'::text, 'ANT'::text, 'MIXED'::text]));
-- COMMIT;
