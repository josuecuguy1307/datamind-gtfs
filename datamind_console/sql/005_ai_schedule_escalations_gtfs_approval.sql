BEGIN;

CREATE SCHEMA IF NOT EXISTS ai;
CREATE SCHEMA IF NOT EXISTS gtfs;

CREATE TABLE IF NOT EXISTS ai.ai_agent_schedule (
  schedule_id INTEGER PRIMARY KEY DEFAULT 1,
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  timezone TEXT NOT NULL DEFAULT 'America/Guayaquil',
  days_of_week INTEGER[] NOT NULL DEFAULT ARRAY[1,2,3,4,5,6,7],
  start_time_local TIME NOT NULL DEFAULT TIME '09:00',
  end_time_local TIME NOT NULL DEFAULT TIME '18:00',
  interval_seconds INTEGER NOT NULL DEFAULT 600,
  run_once_now BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_by TEXT NULL,
  CONSTRAINT chk_ai_agent_schedule_window
    CHECK (end_time_local > start_time_local),
  CONSTRAINT chk_ai_agent_schedule_interval
    CHECK (interval_seconds BETWEEN 60 AND 86400),
  CONSTRAINT chk_ai_agent_schedule_days
    CHECK (
      array_length(days_of_week, 1) >= 1
      AND days_of_week <@ ARRAY[1,2,3,4,5,6,7]::INTEGER[]
    )
);

INSERT INTO ai.ai_agent_schedule (
  schedule_id,
  enabled,
  timezone,
  days_of_week,
  start_time_local,
  end_time_local,
  interval_seconds,
  run_once_now,
  updated_by
)
VALUES
  (1, FALSE, 'America/Guayaquil', ARRAY[1,2,3,4,5,6,7], TIME '09:00', TIME '18:00', 600, FALSE, NULL)
ON CONFLICT (schedule_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS ai.ai_escalations (
  escalation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'sent', 'resolved', 'failed')),
  error_type TEXT NOT NULL,
  error_message TEXT NOT NULL,
  stacktrace TEXT NOT NULL,
  context JSONB NOT NULL DEFAULT '{}'::jsonb,
  codex_request JSONB NULL,
  codex_response JSONB NULL,
  resolution_notes TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_escalations_status_created
  ON ai.ai_escalations(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_escalations_created
  ON ai.ai_escalations(created_at DESC);

CREATE TABLE IF NOT EXISTS gtfs.gtfs_artifacts (
  artifact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status TEXT NOT NULL DEFAULT 'pending_approval'
    CHECK (status IN ('pending_approval', 'approved', 'rejected', 'expired', 'downloaded')),
  zip_path TEXT NOT NULL,
  file_hash TEXT NOT NULL,
  summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  approval_token TEXT NOT NULL,
  approved_at TIMESTAMPTZ NULL,
  approved_by TEXT NULL,
  rejected_at TIMESTAMPTZ NULL,
  rejected_by TEXT NULL,
  downloaded_at TIMESTAMPTZ NULL,
  notes TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_gtfs_artifacts_status_created
  ON gtfs.gtfs_artifacts(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_gtfs_artifacts_hash
  ON gtfs.gtfs_artifacts(file_hash);

CREATE INDEX IF NOT EXISTS idx_gtfs_artifacts_approval_token
  ON gtfs.gtfs_artifacts(approval_token);

CREATE TABLE IF NOT EXISTS gtfs.gtfs_notifications (
  notification_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  artifact_id UUID NOT NULL REFERENCES gtfs.gtfs_artifacts(artifact_id) ON DELETE CASCADE,
  channel TEXT NOT NULL CHECK (channel IN ('whatsapp')),
  provider TEXT NOT NULL CHECK (provider IN ('meta_cloud_api')),
  to_address TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'failed')),
  provider_message_id TEXT NULL,
  error_text TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_gtfs_notifications_artifact
  ON gtfs.gtfs_notifications(artifact_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_gtfs_notifications_status
  ON gtfs.gtfs_notifications(status, created_at DESC);

CREATE TABLE IF NOT EXISTS gtfs.gtfs_audit_log (
  audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  artifact_id UUID NULL REFERENCES gtfs.gtfs_artifacts(artifact_id) ON DELETE SET NULL,
  action TEXT NOT NULL,
  actor TEXT NOT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_gtfs_audit_log_artifact
  ON gtfs.gtfs_audit_log(artifact_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_gtfs_audit_log_action
  ON gtfs.gtfs_audit_log(action, created_at DESC);

COMMIT;
