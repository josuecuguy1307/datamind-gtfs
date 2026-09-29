BEGIN;

-- --------------------------
-- Orchestrator Sessions
-- --------------------------
CREATE TABLE IF NOT EXISTS console.orchestrator_sessions (
  session_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID REFERENCES console.users(user_id) ON DELETE SET NULL,

  profile         TEXT NOT NULL DEFAULT 'balanced',
  status          TEXT NOT NULL DEFAULT 'pending',
  current_step_idx INT NOT NULL DEFAULT 0,
  dry_run         BOOLEAN NOT NULL DEFAULT FALSE,

  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,

  started_at      TIMESTAMPTZ,
  ended_at        TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT chk_orch_session_profile
    CHECK (profile IN ('cautious', 'balanced', 'aggressive')),

  CONSTRAINT chk_orch_session_status
    CHECK (status IN ('pending', 'running', 'paused', 'completed', 'cancelled', 'failed'))
);

DROP TRIGGER IF EXISTS trg_orch_sessions_updated_at ON console.orchestrator_sessions;
CREATE TRIGGER trg_orch_sessions_updated_at
BEFORE UPDATE ON console.orchestrator_sessions
FOR EACH ROW
EXECUTE FUNCTION console.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_orch_sessions_user_id ON console.orchestrator_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_orch_sessions_status ON console.orchestrator_sessions(status);
CREATE INDEX IF NOT EXISTS idx_orch_sessions_created_at ON console.orchestrator_sessions(created_at DESC);

-- --------------------------
-- Orchestrator Steps
-- --------------------------
CREATE TABLE IF NOT EXISTS console.orchestrator_steps (
  step_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      UUID NOT NULL REFERENCES console.orchestrator_sessions(session_id) ON DELETE CASCADE,

  step_key        TEXT NOT NULL,
  step_idx        INT NOT NULL,

  status          TEXT NOT NULL DEFAULT 'pending',
  policy          TEXT NOT NULL DEFAULT 'gate',

  started_at      TIMESTAMPTZ,
  ended_at        TIMESTAMPTZ,

  output          JSONB DEFAULT '{}'::jsonb,
  error           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT chk_orch_step_status
    CHECK (status IN (
      'pending', 'running', 'auto_approved', 'waiting_approval',
      'approved', 'rejected', 'skipped', 'completed', 'failed'
    )),

  CONSTRAINT chk_orch_step_policy
    CHECK (policy IN ('auto', 'gate', 'skip')),

  CONSTRAINT uq_orch_step_session_idx UNIQUE (session_id, step_idx)
);

CREATE INDEX IF NOT EXISTS idx_orch_steps_session_id ON console.orchestrator_steps(session_id);
CREATE INDEX IF NOT EXISTS idx_orch_steps_status ON console.orchestrator_steps(status);
CREATE INDEX IF NOT EXISTS idx_orch_steps_step_key ON console.orchestrator_steps(step_key);

-- --------------------------
-- Approval Queue
-- --------------------------
CREATE TABLE IF NOT EXISTS console.approval_queue (
  approval_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  step_id         UUID NOT NULL REFERENCES console.orchestrator_steps(step_id) ON DELETE CASCADE,
  session_id      UUID NOT NULL REFERENCES console.orchestrator_sessions(session_id) ON DELETE CASCADE,

  action          TEXT,
  reason          TEXT,
  decided_by      UUID REFERENCES console.users(user_id) ON DELETE SET NULL,
  decided_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT chk_approval_action
    CHECK (action IS NULL OR action IN ('approve', 'reject', 'defer'))
);

CREATE INDEX IF NOT EXISTS idx_approval_queue_session_id ON console.approval_queue(session_id);
CREATE INDEX IF NOT EXISTS idx_approval_queue_step_id ON console.approval_queue(step_id);
CREATE INDEX IF NOT EXISTS idx_approval_queue_pending ON console.approval_queue(created_at DESC)
  WHERE action IS NULL;

COMMIT;
