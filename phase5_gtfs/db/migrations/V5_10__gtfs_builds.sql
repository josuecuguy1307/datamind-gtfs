-- Phase 5 logical GTFS build context (operator-facing id)
CREATE TABLE IF NOT EXISTS gtfs_work.gtfs_builds (
  gtfs_id text PRIMARY KEY,
  export_run_id uuid NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE SET NULL,
  build_name text NULL,
  status text NOT NULL DEFAULT 'draft',
  notes text NULL,
  created_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (export_run_id)
);

CREATE INDEX IF NOT EXISTS idx_gtfs_builds_created_at
  ON gtfs_work.gtfs_builds(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_gtfs_builds_export_run_id
  ON gtfs_work.gtfs_builds(export_run_id);
