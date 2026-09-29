-- ============================================================
-- 030_route_trash.sql
-- Recoverable deletion / papelera architecture for Phase 3 routes
-- ============================================================

-- Schema for trash / archive storage
CREATE SCHEMA IF NOT EXISTS route_trash;

-- -----------------------------------------------------------
-- 1. route_trash.trash_items
--    Stores full snapshots of trashed route-level objects.
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_trash.trash_items (
    trash_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- identity of the trashed object
    original_table      TEXT        NOT NULL,
    original_primary_key TEXT       NOT NULL,
    route_id            UUID        NOT NULL,

    -- key identifiers for quick lookup without parsing JSON
    chosen_osm_relation_id  BIGINT,
    route_ref               TEXT,
    route_name              TEXT,
    operator_name           TEXT,
    original_status         TEXT,

    -- deletion context
    deletion_reason             TEXT,
    deletion_source_workflow    TEXT        NOT NULL,
    deleted_by                  TEXT,
    deleted_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- replacement tracking
    replaced_by_route_id    UUID,

    -- restore tracking
    restore_status          TEXT DEFAULT 'trashed'
                            CHECK (restore_status IN ('trashed', 'restored', 'purged')),
    restored_at             TIMESTAMPTZ,
    restored_by             TEXT,

    -- full snapshot for recovery
    full_snapshot_jsonb     JSONB       NOT NULL,

    -- extra structured metadata
    metadata_jsonb          JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_trash_items_route_id
    ON route_trash.trash_items (route_id);
CREATE INDEX IF NOT EXISTS idx_trash_items_restore_status
    ON route_trash.trash_items (restore_status);
CREATE INDEX IF NOT EXISTS idx_trash_items_deleted_at
    ON route_trash.trash_items (deleted_at DESC);
CREATE INDEX IF NOT EXISTS idx_trash_items_workflow
    ON route_trash.trash_items (deletion_source_workflow);


-- -----------------------------------------------------------
-- 2. route_trash.delete_events
--    Audit log for every deletion / cleanup action.
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_trash.delete_events (
    delete_event_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- reference to the affected object
    route_id            UUID,
    original_table      TEXT,
    original_primary_key TEXT,

    -- action details
    action_type         TEXT NOT NULL
                        CHECK (action_type IN (
                            'delete', 'deactivate', 'merge',
                            'replace', 'cleanup', 'purge',
                            'restore', 'suppress'
                        )),
    workflow_source      TEXT        NOT NULL,
    reason               TEXT,
    actor                TEXT,
    event_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- links
    trash_id                UUID REFERENCES route_trash.trash_items(trash_id),
    replacement_route_id    UUID,
    canonical_route_id      UUID,

    -- structured metadata
    metadata_jsonb          JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_delete_events_route_id
    ON route_trash.delete_events (route_id);
CREATE INDEX IF NOT EXISTS idx_delete_events_action_type
    ON route_trash.delete_events (action_type);
CREATE INDEX IF NOT EXISTS idx_delete_events_event_at
    ON route_trash.delete_events (event_at DESC);
CREATE INDEX IF NOT EXISTS idx_delete_events_trash_id
    ON route_trash.delete_events (trash_id);


-- -----------------------------------------------------------
-- 3. Soft-delete column on route_raw.route_jobs
--    Allows active views to exclude trashed routes without
--    breaking FK references.
-- -----------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'route_raw'
          AND table_name   = 'route_jobs'
          AND column_name  = 'is_trashed'
    ) THEN
        ALTER TABLE route_raw.route_jobs
            ADD COLUMN is_trashed   BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN trashed_at   TIMESTAMPTZ,
            ADD COLUMN trash_id     UUID;
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_route_jobs_is_trashed
    ON route_raw.route_jobs (is_trashed)
    WHERE is_trashed = TRUE;


-- -----------------------------------------------------------
-- 4. View: route_raw.active_route_jobs
--    Drop-in replacement for queries that should exclude trash.
-- -----------------------------------------------------------
CREATE OR REPLACE VIEW route_raw.active_route_jobs AS
SELECT *
FROM route_raw.route_jobs
WHERE is_trashed = FALSE;


-- -----------------------------------------------------------
-- 5. View: route_trash.trash_summary
--    Quick overview of trashed items with key fields.
-- -----------------------------------------------------------
CREATE OR REPLACE VIEW route_trash.trash_summary AS
SELECT
    t.trash_id,
    t.route_id,
    t.chosen_osm_relation_id,
    t.route_ref,
    t.route_name,
    t.original_status,
    t.deletion_reason,
    t.deletion_source_workflow,
    t.deleted_by,
    t.deleted_at,
    t.replaced_by_route_id,
    t.restore_status,
    t.restored_at
FROM route_trash.trash_items t
ORDER BY t.deleted_at DESC;
