BEGIN;

CREATE TABLE IF NOT EXISTS console.pipeline_autopilot_patch_registry (
  patch_task_id                    TEXT PRIMARY KEY,
  run_id                           TEXT NOT NULL REFERENCES console.pipeline_autopilot_runs(run_id) ON DELETE CASCADE,
  trace_id                         TEXT NOT NULL,
  phase                            TEXT NOT NULL,
  step_id                          TEXT NOT NULL,
  attempt_no                       INT NOT NULL DEFAULT 0,
  patch_branch                     TEXT,
  patch_type                       TEXT,
  status                           TEXT NOT NULL,
  policy_profile                   TEXT,
  trigger_reason_class             TEXT,
  origin_interpreter_snapshot_id   TEXT,
  origin_block_reason_code         TEXT,
  comparator_outcome               TEXT,
  operator_outcome_decision        TEXT,
  dispatch_approval_state          TEXT,
  dispatch_metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
  baseline_run_ref                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  retest_run_refs                  JSONB NOT NULL DEFAULT '[]'::jsonb,
  comparator_result_ref            JSONB NOT NULL DEFAULT '{}'::jsonb,
  impact_summary                   JSONB NOT NULL DEFAULT '{}'::jsonb,
  record_json                      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at                       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT chk_pipeline_autopilot_patch_registry_status
    CHECK (status IN (
      'generated',
      'pending_approval',
      'dispatched',
      'failed',
      'retest_pending',
      'compared',
      'accepted',
      'rolled_back',
      'observe_more'
    )),
  CONSTRAINT chk_pipeline_autopilot_patch_registry_outcome
    CHECK (
      comparator_outcome IS NULL
      OR comparator_outcome IN ('improved', 'regressed', 'inconclusive', 'not_run')
    ),
  CONSTRAINT chk_pipeline_autopilot_patch_registry_operator_decision
    CHECK (
      operator_outcome_decision IS NULL
      OR operator_outcome_decision IN ('accepted', 'rolled_back', 'observe_more', 'unknown')
    )
);

CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_patch_registry_run
  ON console.pipeline_autopilot_patch_registry(run_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_patch_registry_status
  ON console.pipeline_autopilot_patch_registry(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_patch_registry_phase_step
  ON console.pipeline_autopilot_patch_registry(phase, step_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_autopilot_patch_registry_branch
  ON console.pipeline_autopilot_patch_registry(patch_branch, patch_type, updated_at DESC);

COMMIT;
