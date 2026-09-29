-- ============================================================
-- 028  Constructor Orchestrator
-- Extends manual_sequence_drafts / exports with lifecycle,
-- LLM-assist, and audit columns.  Adds metrics + review tables.
-- Additive schema only (safe to re-run).
-- ============================================================

-- ----------------------------------------------------------
-- 1. Add columns to manual_sequence_drafts
-- ----------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_drafts'
      AND column_name  = 'coverage_gap_id'
  ) THEN
    ALTER TABLE route_work.manual_sequence_drafts
      ADD COLUMN coverage_gap_id UUID NULL;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_drafts'
      AND column_name  = 'constructor_status'
  ) THEN
    ALTER TABLE route_work.manual_sequence_drafts
      ADD COLUMN constructor_status TEXT NOT NULL DEFAULT 'draft_saved';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_drafts'
      AND column_name  = 'preflight_classification'
  ) THEN
    ALTER TABLE route_work.manual_sequence_drafts
      ADD COLUMN preflight_classification TEXT NULL;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_drafts'
      AND column_name  = 'llm_evidence_payload'
  ) THEN
    ALTER TABLE route_work.manual_sequence_drafts
      ADD COLUMN llm_evidence_payload JSONB NULL;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_drafts'
      AND column_name  = 'llm_reasoning_output'
  ) THEN
    ALTER TABLE route_work.manual_sequence_drafts
      ADD COLUMN llm_reasoning_output JSONB NULL;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_drafts'
      AND column_name  = 'llm_confidence'
  ) THEN
    ALTER TABLE route_work.manual_sequence_drafts
      ADD COLUMN llm_confidence REAL NULL;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_drafts'
      AND column_name  = 'llm_recommended_action'
  ) THEN
    ALTER TABLE route_work.manual_sequence_drafts
      ADD COLUMN llm_recommended_action TEXT NULL;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_drafts'
      AND column_name  = 'hint_provenance'
  ) THEN
    ALTER TABLE route_work.manual_sequence_drafts
      ADD COLUMN hint_provenance JSONB NULL DEFAULT '{}';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_drafts'
      AND column_name  = 'is_deleted'
  ) THEN
    ALTER TABLE route_work.manual_sequence_drafts
      ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_drafts'
      AND column_name  = 'deleted_at'
  ) THEN
    ALTER TABLE route_work.manual_sequence_drafts
      ADD COLUMN deleted_at TIMESTAMPTZ NULL;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_drafts'
      AND column_name  = 'notes'
  ) THEN
    ALTER TABLE route_work.manual_sequence_drafts
      ADD COLUMN notes TEXT NULL;
  END IF;
END $$;

-- ----------------------------------------------------------
-- 2. Add columns to manual_sequence_exports
-- ----------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_exports'
      AND column_name  = 'constructor_status'
  ) THEN
    ALTER TABLE route_work.manual_sequence_exports
      ADD COLUMN constructor_status TEXT NOT NULL DEFAULT 'exported_sequence_ready';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_exports'
      AND column_name  = 'preflight_classification'
  ) THEN
    ALTER TABLE route_work.manual_sequence_exports
      ADD COLUMN preflight_classification TEXT NULL;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_exports'
      AND column_name  = 'llm_evidence_payload'
  ) THEN
    ALTER TABLE route_work.manual_sequence_exports
      ADD COLUMN llm_evidence_payload JSONB NULL;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_exports'
      AND column_name  = 'llm_reasoning_output'
  ) THEN
    ALTER TABLE route_work.manual_sequence_exports
      ADD COLUMN llm_reasoning_output JSONB NULL;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_exports'
      AND column_name  = 'hint_provenance'
  ) THEN
    ALTER TABLE route_work.manual_sequence_exports
      ADD COLUMN hint_provenance JSONB NULL DEFAULT '{}';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_exports'
      AND column_name  = 'export_warnings'
  ) THEN
    ALTER TABLE route_work.manual_sequence_exports
      ADD COLUMN export_warnings JSONB NULL DEFAULT '[]';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_exports'
      AND column_name  = 'is_rerun'
  ) THEN
    ALTER TABLE route_work.manual_sequence_exports
      ADD COLUMN is_rerun BOOLEAN NOT NULL DEFAULT FALSE;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'route_work'
      AND table_name   = 'manual_sequence_exports'
      AND column_name  = 'prior_export_id'
  ) THEN
    ALTER TABLE route_work.manual_sequence_exports
      ADD COLUMN prior_export_id UUID NULL;
  END IF;
END $$;

-- ----------------------------------------------------------
-- 3. Constructor metrics log
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_work.constructor_metrics_log (
  log_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type              TEXT NOT NULL,
  route_id                UUID NULL,
  draft_id                UUID NULL,
  export_id               UUID NULL,
  coverage_gap_id         UUID NULL,
  constructor_status      TEXT NULL,
  preflight_classification TEXT NULL,
  llm_task                TEXT NULL,
  llm_confidence          REAL NULL,
  llm_source              TEXT NULL,
  outcome                 TEXT NULL,
  failure_class           TEXT NULL,
  payload                 JSONB NULL DEFAULT '{}',
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by              TEXT NULL
);

-- ----------------------------------------------------------
-- 4. Constructor review audit
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_work.constructor_review_audit (
  audit_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id    UUID NOT NULL,
  draft_id    UUID NULL,
  export_id   UUID NULL,
  action      TEXT NOT NULL,   -- 'create_draft', 'export', 'approve', 'reject', 'rerun', 'llm_assist'
  actor       TEXT NULL,       -- 'operator', 'system', 'llm'
  decision    TEXT NULL,       -- 'approved', 'rejected', 'deferred'
  reasoning   TEXT NULL,
  evidence    JSONB NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------
-- 5. Indexes
-- ----------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_constructor_metrics_event
  ON route_work.constructor_metrics_log(event_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_constructor_metrics_route
  ON route_work.constructor_metrics_log(route_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_constructor_review_route
  ON route_work.constructor_review_audit(route_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_drafts_status
  ON route_work.manual_sequence_drafts(constructor_status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_drafts_not_deleted
  ON route_work.manual_sequence_drafts(is_deleted, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_drafts_gap
  ON route_work.manual_sequence_drafts(coverage_gap_id)
  WHERE coverage_gap_id IS NOT NULL;

-- ----------------------------------------------------------
-- 6. Constructor dashboard view
-- ----------------------------------------------------------
CREATE OR REPLACE VIEW route_work.constructor_dashboard_v1 AS
SELECT
  d.draft_id,
  d.route_id,
  d.constructor_status,
  d.preflight_classification,
  d.name_hint,
  d.operator_hint,
  d.variant_hint,
  d.coverage_gap_id,
  d.is_loop,
  array_length(d.ordered_stop_ids, 1) AS stop_count,
  d.llm_confidence,
  d.llm_recommended_action,
  d.created_by,
  d.created_at,
  d.updated_at,
  d.is_deleted,
  e.export_id,
  e.stop_sequence_set_id,
  e.stop_sequence_candidate_id,
  e.created_at AS exported_at
FROM route_work.manual_sequence_drafts d
LEFT JOIN route_work.manual_sequence_exports e ON e.draft_id = d.draft_id
WHERE d.is_deleted = FALSE
ORDER BY d.updated_at DESC;
