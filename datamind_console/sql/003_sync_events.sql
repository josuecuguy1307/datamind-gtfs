BEGIN;

CREATE SCHEMA IF NOT EXISTS console;

CREATE TABLE IF NOT EXISTS console.sync_events (
  sync_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  synced_by TEXT,
  source_mode TEXT NOT NULL,
  target_mode TEXT NOT NULL,
  comment TEXT,
  status TEXT NOT NULL DEFAULT 'logged',
  tables_changed JSONB NOT NULL DEFAULT '[]'::jsonb,
  row_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_console_sync_events_synced_at
  ON console.sync_events(synced_at DESC);

COMMIT;
