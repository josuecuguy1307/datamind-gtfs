-- V5_3__revise_current_gtfs.sql
-- Metadata + audit tables for "Revise Current GTFS" workspace.

CREATE TABLE IF NOT EXISTS gtfs_work.upload_runs (
  export_run_id UUID PRIMARY KEY REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  file_name TEXT NOT NULL,
  file_size_bytes BIGINT,
  uploaded_by TEXT,
  uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gtfs_work.revision_events (
  event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  export_run_id UUID NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  table_name TEXT NOT NULL,
  row_key JSONB NOT NULL DEFAULT '{}'::jsonb,
  before_row JSONB NOT NULL DEFAULT '{}'::jsonb,
  after_row JSONB NOT NULL DEFAULT '{}'::jsonb,
  edited_by TEXT,
  edited_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gtfs_work_upload_runs_uploaded_at
  ON gtfs_work.upload_runs(uploaded_at DESC);

CREATE INDEX IF NOT EXISTS idx_gtfs_work_revision_events_run_time
  ON gtfs_work.revision_events(export_run_id, edited_at DESC);
