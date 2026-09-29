-- Migration 032: Re-entry infrastructure for legacy-route v2 enhancement.
--
-- Option B (in-place swap): v2 lives in route_prod.approval_queue until
-- operator approves, then UPDATEs the route_prod.routes row in place
-- (version bumps 1→2→3). Historical v1 is preserved by the existing
-- route_prod.routes_audit trigger table. No FK surgery on downstream
-- schemas (catalog / gtfs_work / node_prod / route_review).
--
-- Adds:
--   - route_prod.routes.last_swap_at         (TIMESTAMPTZ)
--   - route_prod.routes.last_swap_from_version (SMALLINT)
--   - route_prod.re_entry_queue              (per-route enhancement backlog)
--   - route_prod.fix_reports                 (before/after audit of fixes)
--
-- Preconditions verified on datamind_ml (2026-04-21):
--   - routes.version SMALLINT present, CHECK version >= 1
--   - PK composite (route_id, version); also routes_uq_route_id UNIQUE on
--     route_id alone (kept intact; 9 FKs depend on it)
--   - approval_queue present (migration 031 from Prompt 7b)
--   - 1,112 rows grandfathered with grandfathered_until=2026-07-20

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Swap lineage columns on route_prod.routes
-- -----------------------------------------------------------------------------
--
-- These track the in-place UPDATE that replaces v_n with v_{n+1} on operator
-- approval. The full v_n snapshot is captured by the routes_audit trigger
-- (pre-existing), so rollback reads that table; these columns just provide
-- fast-path lineage for UI + fix_reports joins.

ALTER TABLE route_prod.routes
  ADD COLUMN IF NOT EXISTS last_swap_at TIMESTAMPTZ;

ALTER TABLE route_prod.routes
  ADD COLUMN IF NOT EXISTS last_swap_from_version SMALLINT;

-- -----------------------------------------------------------------------------
-- 2. re_entry_queue — backlog of grandfathered routes awaiting enhancement
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS route_prod.re_entry_queue (
  queue_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NOT NULL,
  current_version SMALLINT NOT NULL,
  priority INTEGER NOT NULL,
  classification TEXT NOT NULL CHECK (classification IN (
    'osm_relation_severe',
    'osm_relation_clean',
    'discovery_legacy',
    'constructor_canonical_legacy',
    'manual_constructor_legacy',
    'structural_repair',
    'unclassified'
  )),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
    'pending',
    'in_progress',
    'v2_ready',
    'approved',
    'swapped',
    'failed',
    'quarantined'
  )),
  priority_reason TEXT,
  scheduled_at TIMESTAMPTZ,
  attempted_at TIMESTAMPTZ,
  last_error TEXT,
  attempts SMALLINT NOT NULL DEFAULT 0,
  enqueued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at TIMESTAMPTZ,
  CONSTRAINT fk_re_entry_queue_route
    FOREIGN KEY (route_id, current_version)
    REFERENCES route_prod.routes (route_id, version)
    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_re_entry_queue_status_priority
  ON route_prod.re_entry_queue (status, priority, scheduled_at);

CREATE INDEX IF NOT EXISTS ix_re_entry_queue_route
  ON route_prod.re_entry_queue (route_id);

-- At most one non-terminal queue row per (route_id, current_version). Lets
-- us re-enqueue after terminal states (swapped / quarantined) without
-- duplicating the active work item.
CREATE UNIQUE INDEX IF NOT EXISTS ix_re_entry_queue_active_unique
  ON route_prod.re_entry_queue (route_id, current_version)
  WHERE status IN ('pending', 'in_progress', 'v2_ready');

-- -----------------------------------------------------------------------------
-- 3. fix_reports — audit of what the Fixers changed on each swap
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS route_prod.fix_reports (
  report_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NOT NULL,
  version_before SMALLINT NOT NULL,
  version_after SMALLINT NOT NULL,
  fix_category TEXT NOT NULL CHECK (fix_category IN (
    'geometry',
    'stop_coverage',
    'structural'
  )),
  fixes_applied JSONB NOT NULL,
  metrics_before JSONB NOT NULL,
  metrics_after JSONB NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  applied_by TEXT,
  CONSTRAINT chk_fix_reports_version_bump
    CHECK (version_after > version_before)
);
-- Note: no FK to route_prod.routes(route_id, version) — the v_before row is
-- gone after the in-place swap (its snapshot lives in routes_audit). We
-- index by route_id only so lineage can still be walked.

CREATE INDEX IF NOT EXISTS ix_fix_reports_route
  ON route_prod.fix_reports (route_id);

CREATE INDEX IF NOT EXISTS ix_fix_reports_applied_at
  ON route_prod.fix_reports (applied_at DESC);

COMMIT;
