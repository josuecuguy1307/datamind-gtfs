-- Make gtfs_id the operator-facing primary context across Phase 5 tables.
-- export_run_id remains internal linkage for loading/build scripts.

ALTER TABLE gtfs_work.export_runs
  ADD COLUMN IF NOT EXISTS gtfs_id text;

-- Backfill export_runs.gtfs_id from gtfs_builds first, then legacy fallback.
UPDATE gtfs_work.export_runs e
SET gtfs_id = b.gtfs_id
FROM gtfs_work.gtfs_builds b
WHERE b.export_run_id = e.export_run_id
  AND (e.gtfs_id IS NULL OR e.gtfs_id = '');

UPDATE gtfs_work.export_runs e
SET gtfs_id = 'legacy-' || left(e.export_run_id::text, 8)
WHERE e.gtfs_id IS NULL OR e.gtfs_id = '';

ALTER TABLE gtfs_work.export_runs
  ALTER COLUMN gtfs_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_export_runs_gtfs_id
  ON gtfs_work.export_runs(gtfs_id);

-- Ensure gtfs_builds has mapping for every export run.
INSERT INTO gtfs_work.gtfs_builds (gtfs_id, export_run_id, status, notes, created_by)
SELECT e.gtfs_id, e.export_run_id, COALESCE(e.status, 'draft'), 'auto-linked from export_runs', 'migration_v5_11'
FROM gtfs_work.export_runs e
LEFT JOIN gtfs_work.gtfs_builds b
  ON b.export_run_id = e.export_run_id
WHERE b.export_run_id IS NULL
ON CONFLICT (gtfs_id) DO NOTHING;

-- Add gtfs_id across GTFS staging/revision tables.
ALTER TABLE gtfs_work.upload_runs           ADD COLUMN IF NOT EXISTS gtfs_id text;
ALTER TABLE gtfs_work.revision_events       ADD COLUMN IF NOT EXISTS gtfs_id text;
ALTER TABLE gtfs_work.gtfs_agency           ADD COLUMN IF NOT EXISTS gtfs_id text;
ALTER TABLE gtfs_work.gtfs_stops            ADD COLUMN IF NOT EXISTS gtfs_id text;
ALTER TABLE gtfs_work.gtfs_routes           ADD COLUMN IF NOT EXISTS gtfs_id text;
ALTER TABLE gtfs_work.gtfs_shapes           ADD COLUMN IF NOT EXISTS gtfs_id text;
ALTER TABLE gtfs_work.gtfs_calendar         ADD COLUMN IF NOT EXISTS gtfs_id text;
ALTER TABLE gtfs_work.gtfs_calendar_dates   ADD COLUMN IF NOT EXISTS gtfs_id text;
ALTER TABLE gtfs_work.gtfs_trips            ADD COLUMN IF NOT EXISTS gtfs_id text;
ALTER TABLE gtfs_work.gtfs_stop_times       ADD COLUMN IF NOT EXISTS gtfs_id text;
ALTER TABLE gtfs_work.gtfs_frequencies      ADD COLUMN IF NOT EXISTS gtfs_id text;
ALTER TABLE gtfs_work.gtfs_overrides        ADD COLUMN IF NOT EXISTS gtfs_id text;
ALTER TABLE gtfs_prod.feed_versions         ADD COLUMN IF NOT EXISTS gtfs_id text;

-- Backfill from export_runs.
UPDATE gtfs_work.upload_runs u
SET gtfs_id = e.gtfs_id
FROM gtfs_work.export_runs e
WHERE e.export_run_id = u.export_run_id
  AND (u.gtfs_id IS NULL OR u.gtfs_id = '');

UPDATE gtfs_work.revision_events r
SET gtfs_id = e.gtfs_id
FROM gtfs_work.export_runs e
WHERE e.export_run_id = r.export_run_id
  AND (r.gtfs_id IS NULL OR r.gtfs_id = '');

UPDATE gtfs_work.gtfs_agency t SET gtfs_id = e.gtfs_id FROM gtfs_work.export_runs e WHERE e.export_run_id = t.export_run_id AND (t.gtfs_id IS NULL OR t.gtfs_id = '');
UPDATE gtfs_work.gtfs_stops t SET gtfs_id = e.gtfs_id FROM gtfs_work.export_runs e WHERE e.export_run_id = t.export_run_id AND (t.gtfs_id IS NULL OR t.gtfs_id = '');
UPDATE gtfs_work.gtfs_routes t SET gtfs_id = e.gtfs_id FROM gtfs_work.export_runs e WHERE e.export_run_id = t.export_run_id AND (t.gtfs_id IS NULL OR t.gtfs_id = '');
UPDATE gtfs_work.gtfs_shapes t SET gtfs_id = e.gtfs_id FROM gtfs_work.export_runs e WHERE e.export_run_id = t.export_run_id AND (t.gtfs_id IS NULL OR t.gtfs_id = '');
UPDATE gtfs_work.gtfs_calendar t SET gtfs_id = e.gtfs_id FROM gtfs_work.export_runs e WHERE e.export_run_id = t.export_run_id AND (t.gtfs_id IS NULL OR t.gtfs_id = '');
UPDATE gtfs_work.gtfs_calendar_dates t SET gtfs_id = e.gtfs_id FROM gtfs_work.export_runs e WHERE e.export_run_id = t.export_run_id AND (t.gtfs_id IS NULL OR t.gtfs_id = '');
UPDATE gtfs_work.gtfs_trips t SET gtfs_id = e.gtfs_id FROM gtfs_work.export_runs e WHERE e.export_run_id = t.export_run_id AND (t.gtfs_id IS NULL OR t.gtfs_id = '');
UPDATE gtfs_work.gtfs_stop_times t SET gtfs_id = e.gtfs_id FROM gtfs_work.export_runs e WHERE e.export_run_id = t.export_run_id AND (t.gtfs_id IS NULL OR t.gtfs_id = '');
UPDATE gtfs_work.gtfs_frequencies t SET gtfs_id = e.gtfs_id FROM gtfs_work.export_runs e WHERE e.export_run_id = t.export_run_id AND (t.gtfs_id IS NULL OR t.gtfs_id = '');
UPDATE gtfs_work.gtfs_overrides t SET gtfs_id = e.gtfs_id FROM gtfs_work.export_runs e WHERE e.export_run_id = t.export_run_id AND (t.gtfs_id IS NULL OR t.gtfs_id = '');
UPDATE gtfs_prod.feed_versions t SET gtfs_id = e.gtfs_id FROM gtfs_work.export_runs e WHERE e.export_run_id = t.export_run_id AND (t.gtfs_id IS NULL OR t.gtfs_id = '');

-- Helpful indexes for gtfs_id-centric workflows.
CREATE INDEX IF NOT EXISTS idx_upload_runs_gtfs_id ON gtfs_work.upload_runs(gtfs_id);
CREATE INDEX IF NOT EXISTS idx_revision_events_gtfs_id ON gtfs_work.revision_events(gtfs_id, edited_at DESC);
CREATE INDEX IF NOT EXISTS idx_gtfs_routes_gtfs_id ON gtfs_work.gtfs_routes(gtfs_id, route_id);
CREATE INDEX IF NOT EXISTS idx_gtfs_trips_gtfs_id ON gtfs_work.gtfs_trips(gtfs_id, route_id, direction_id);
CREATE INDEX IF NOT EXISTS idx_gtfs_stop_times_gtfs_id ON gtfs_work.gtfs_stop_times(gtfs_id, trip_id, stop_sequence);

-- Keep gtfs_id synced automatically from export_run_id for writes done by existing scripts.
CREATE OR REPLACE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_gtfs_id text;
BEGIN
  IF NEW.gtfs_id IS NULL OR NEW.gtfs_id = '' THEN
    SELECT e.gtfs_id INTO v_gtfs_id
    FROM gtfs_work.export_runs e
    WHERE e.export_run_id = NEW.export_run_id
    LIMIT 1;
    NEW.gtfs_id := COALESCE(v_gtfs_id, NEW.gtfs_id);
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sync_gtfs_id_upload_runs ON gtfs_work.upload_runs;
CREATE TRIGGER trg_sync_gtfs_id_upload_runs
BEFORE INSERT OR UPDATE ON gtfs_work.upload_runs
FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();

DROP TRIGGER IF EXISTS trg_sync_gtfs_id_revision_events ON gtfs_work.revision_events;
CREATE TRIGGER trg_sync_gtfs_id_revision_events
BEFORE INSERT OR UPDATE ON gtfs_work.revision_events
FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();

DROP TRIGGER IF EXISTS trg_sync_gtfs_id_agency ON gtfs_work.gtfs_agency;
CREATE TRIGGER trg_sync_gtfs_id_agency
BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_agency
FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();

DROP TRIGGER IF EXISTS trg_sync_gtfs_id_stops ON gtfs_work.gtfs_stops;
CREATE TRIGGER trg_sync_gtfs_id_stops
BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_stops
FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();

DROP TRIGGER IF EXISTS trg_sync_gtfs_id_routes ON gtfs_work.gtfs_routes;
CREATE TRIGGER trg_sync_gtfs_id_routes
BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_routes
FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();

DROP TRIGGER IF EXISTS trg_sync_gtfs_id_shapes ON gtfs_work.gtfs_shapes;
CREATE TRIGGER trg_sync_gtfs_id_shapes
BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_shapes
FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();

DROP TRIGGER IF EXISTS trg_sync_gtfs_id_calendar ON gtfs_work.gtfs_calendar;
CREATE TRIGGER trg_sync_gtfs_id_calendar
BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_calendar
FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();

DROP TRIGGER IF EXISTS trg_sync_gtfs_id_calendar_dates ON gtfs_work.gtfs_calendar_dates;
CREATE TRIGGER trg_sync_gtfs_id_calendar_dates
BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_calendar_dates
FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();

DROP TRIGGER IF EXISTS trg_sync_gtfs_id_trips ON gtfs_work.gtfs_trips;
CREATE TRIGGER trg_sync_gtfs_id_trips
BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_trips
FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();

DROP TRIGGER IF EXISTS trg_sync_gtfs_id_stop_times ON gtfs_work.gtfs_stop_times;
CREATE TRIGGER trg_sync_gtfs_id_stop_times
BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_stop_times
FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();

DROP TRIGGER IF EXISTS trg_sync_gtfs_id_frequencies ON gtfs_work.gtfs_frequencies;
CREATE TRIGGER trg_sync_gtfs_id_frequencies
BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_frequencies
FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();

DROP TRIGGER IF EXISTS trg_sync_gtfs_id_overrides ON gtfs_work.gtfs_overrides;
CREATE TRIGGER trg_sync_gtfs_id_overrides
BEFORE INSERT OR UPDATE ON gtfs_work.gtfs_overrides
FOR EACH ROW EXECUTE FUNCTION gtfs_work.fn_sync_gtfs_id_from_export_run();

-- Track GTFS loading operations explicitly.
CREATE TABLE IF NOT EXISTS gtfs_work.gtfs_loadings (
  loading_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  gtfs_id text NOT NULL,
  export_run_id uuid NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  source text NOT NULL DEFAULT 'uploaded_gtfs',
  file_name text,
  file_size_bytes bigint,
  status text NOT NULL DEFAULT 'loaded',
  summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gtfs_loadings_gtfs_id ON gtfs_work.gtfs_loadings(gtfs_id, created_at DESC);
