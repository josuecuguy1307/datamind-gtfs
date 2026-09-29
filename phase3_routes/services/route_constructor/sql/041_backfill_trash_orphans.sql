-- Block 6: Backfill trash_items for 480 orphan trashed jobs
-- These jobs have is_trashed=TRUE but no corresponding route_trash.trash_items record.
-- We insert a minimal record so the trash audit trail is complete.

BEGIN;

INSERT INTO route_trash.trash_items (
    route_id,
    original_table,
    original_primary_key,
    route_ref,
    route_name,
    operator_name,
    original_status,
    deletion_reason,
    deletion_source_workflow,
    deleted_by,
    deleted_at,
    full_snapshot_jsonb
)
SELECT
    rj.route_id,
    'route_raw.route_jobs',
    rj.route_id::text,
    rj.known_ref,
    NULL,  -- route_name not on route_jobs
    NULL,  -- operator_name not on route_jobs
    rj.status,
    'backfill_orphan_cleanup_2026-03-19',
    'infrastructure_audit_block6',
    'system',
    COALESCE(rj.trashed_at, NOW()),
    jsonb_build_object(
        'route_id', rj.route_id,
        'status', rj.status,
        'known_ref', rj.known_ref,
        'chosen_osm_relation_id', rj.chosen_osm_relation_id,
        'created_at', rj.created_at,
        'trashed_at', rj.trashed_at,
        'backfill_note', 'orphan_cleanup_2026-03-19'
    )
FROM route_raw.route_jobs rj
WHERE rj.is_trashed = TRUE
  AND NOT EXISTS (
    SELECT 1 FROM route_trash.trash_items ti
    WHERE ti.route_id = rj.route_id
  );

COMMIT;
