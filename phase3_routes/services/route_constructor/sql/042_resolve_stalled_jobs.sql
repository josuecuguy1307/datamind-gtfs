-- Block 6: Resolve 290 stalled non-trashed jobs
-- These jobs are >7 days old and stuck in non-terminal states.
-- Strategy:
--   - 'new' with no relation → mark failed (never started)
--   - 'relation_fetched' → mark failed (stuck after fetch)
--   - 'needs_rebuild' → mark failed (rebuild never happened)
--   - 'extraction_failed' → already terminal, just ensure consistency

BEGIN;

-- Mark 'new' jobs with no relation as failed
UPDATE route_raw.route_jobs
SET status = 'failed',
    notes = COALESCE(notes, '') || ' [auto-failed by cleanup 2026-03-19: stale new job]'
WHERE is_trashed = FALSE
  AND status = 'new'
  AND created_at < NOW() - INTERVAL '7 days';

-- Mark 'relation_fetched' as failed (stuck mid-pipeline)
UPDATE route_raw.route_jobs
SET status = 'failed',
    notes = COALESCE(notes, '') || ' [auto-failed by cleanup 2026-03-19: stale relation_fetched]'
WHERE is_trashed = FALSE
  AND status = 'relation_fetched'
  AND created_at < NOW() - INTERVAL '7 days';

-- Mark 'needs_rebuild' as failed
UPDATE route_raw.route_jobs
SET status = 'failed',
    notes = COALESCE(notes, '') || ' [auto-failed by cleanup 2026-03-19: stale needs_rebuild]'
WHERE is_trashed = FALSE
  AND status = 'needs_rebuild'
  AND created_at < NOW() - INTERVAL '7 days';

COMMIT;
