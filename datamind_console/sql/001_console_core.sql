BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE SCHEMA IF NOT EXISTS console;

-- --------------------------
-- Updated-at trigger helper
-- --------------------------
CREATE OR REPLACE FUNCTION console.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;

-- --------------------------
-- Users
-- --------------------------
CREATE TABLE IF NOT EXISTS console.users (
  user_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email          CITEXT NOT NULL UNIQUE,
  display_name   TEXT NOT NULL,
  role           TEXT NOT NULL DEFAULT 'admin',
  password_hash  TEXT NOT NULL,
  is_active      BOOLEAN NOT NULL DEFAULT TRUE,

  last_login_at  TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT chk_console_users_role
    CHECK (role IN ('admin', 'editor', 'viewer'))
);

DROP TRIGGER IF EXISTS trg_console_users_updated_at ON console.users;
CREATE TRIGGER trg_console_users_updated_at
BEFORE UPDATE ON console.users
FOR EACH ROW
EXECUTE FUNCTION console.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_console_users_role ON console.users(role);
CREATE INDEX IF NOT EXISTS idx_console_users_active ON console.users(is_active);

-- --------------------------
-- Sessions (cookie/session auth)
-- token_hash = SHA256(token) stored, never store raw token
-- --------------------------
CREATE TABLE IF NOT EXISTS console.sessions (
  session_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES console.users(user_id) ON DELETE CASCADE,
  token_hash   TEXT NOT NULL UNIQUE,

  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at   TIMESTAMPTZ NOT NULL,
  revoked_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_console_sessions_user ON console.sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_console_sessions_expires ON console.sessions(expires_at);

-- --------------------------
-- API Keys (optional, useful for Phase4Client, etc.)
-- key_hash = SHA256(key)
-- key_prefix = first 8 chars for display only
-- --------------------------
CREATE TABLE IF NOT EXISTS console.api_keys (
  key_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES console.users(user_id) ON DELETE CASCADE,

  key_prefix    TEXT NOT NULL,
  key_hash      TEXT NOT NULL UNIQUE,

  label         TEXT,
  scopes        TEXT[] NOT NULL DEFAULT '{}',

  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_used_at  TIMESTAMPTZ,
  revoked_at    TIMESTAMPTZ,
  expires_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_console_api_keys_user ON console.api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_console_api_keys_revoked ON console.api_keys(revoked_at);

-- --------------------------
-- Audit Events (everything, always)
-- phase can be null for non-phase events (login, settings, exports)
-- item_id / candidate_id are TEXT for cross-phase compatibility
-- --------------------------
CREATE TABLE IF NOT EXISTS console.audit_events (
  event_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        UUID REFERENCES console.users(user_id) ON DELETE SET NULL,

  phase          SMALLINT,
  action         TEXT NOT NULL,

  item_type      TEXT,
  item_id        TEXT,
  candidate_id   TEXT,

  ok             BOOLEAN NOT NULL DEFAULT TRUE,
  error_message  TEXT,

  ip             INET,
  user_agent     TEXT,

  payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT chk_console_audit_phase
    CHECK (phase IS NULL OR (phase >= 1 AND phase <= 4))
);

CREATE INDEX IF NOT EXISTS idx_console_audit_created_at ON console.audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_console_audit_user ON console.audit_events(user_id);
CREATE INDEX IF NOT EXISTS idx_console_audit_phase ON console.audit_events(phase);

-- --------------------------
-- Phase Decisions (universal human judgment history)
-- decision values: APPROVE, REJECT, EDIT, PUBLISH
-- reason_code required for REJECT/EDIT
-- --------------------------
CREATE TABLE IF NOT EXISTS console.phase_decisions (
  decision_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  phase              SMALLINT NOT NULL,
  item_id            TEXT NOT NULL,
  candidate_id       TEXT,
  score_at_decision  DOUBLE PRECISION,
  decision           TEXT NOT NULL,
  reason_code        TEXT,
  notes              TEXT,
  user_id            UUID REFERENCES console.users(user_id) ON DELETE SET NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT chk_console_phase_decisions_phase
    CHECK (phase >= 1 AND phase <= 4),

  CONSTRAINT chk_console_phase_decision_value
    CHECK (decision IN ('APPROVE', 'REJECT', 'EDIT', 'PUBLISH')),

  CONSTRAINT chk_reason_required_for_reject_edit
    CHECK (
      (decision IN ('REJECT', 'EDIT') AND reason_code IS NOT NULL)
      OR
      (decision IN ('APPROVE', 'PUBLISH'))
    )
);

CREATE INDEX IF NOT EXISTS idx_console_decisions_created_at ON console.phase_decisions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_console_decisions_phase_item ON console.phase_decisions(phase, item_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_console_decisions_user ON console.phase_decisions(user_id);

-- --------------------------
-- Export Jobs (download center)
-- --------------------------
CREATE TABLE IF NOT EXISTS console.export_jobs (
  job_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID REFERENCES console.users(user_id) ON DELETE SET NULL,

  phase         SMALLINT,
  item_id       TEXT,

  job_type      TEXT NOT NULL,
  params        JSONB NOT NULL DEFAULT '{}'::jsonb,

  status        TEXT NOT NULL DEFAULT 'queued',
  result_path   TEXT,

  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ,
  error_message TEXT,

  CONSTRAINT chk_console_export_phase
    CHECK (phase IS NULL OR (phase >= 1 AND phase <= 4)),

  CONSTRAINT chk_console_export_status
    CHECK (status IN ('queued', 'running', 'done', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_console_exports_created_at ON console.export_jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_console_exports_status ON console.export_jobs(status);

-- --------------------------
-- Analytics Views (for Streamlit charts)
-- --------------------------
CREATE OR REPLACE VIEW console.v_decisions_daily AS
SELECT
  DATE_TRUNC('day', created_at) AS day,
  phase,
  decision,
  COUNT(*) AS n
FROM console.phase_decisions
GROUP BY 1,2,3
ORDER BY 1 DESC, 2, 3;

CREATE OR REPLACE VIEW console.v_audit_daily AS
SELECT
  DATE_TRUNC('day', created_at) AS day,
  phase,
  ok,
  COUNT(*) AS n
FROM console.audit_events
GROUP BY 1,2,3
ORDER BY 1 DESC, 2, 3;

COMMIT;


CREATE TABLE IF NOT EXISTS console.workspace_state (
  user_id            UUID PRIMARY KEY REFERENCES console.users(user_id) ON DELETE CASCADE,
  current_phase      SMALLINT,
  current_subtab     TEXT,
  current_route_id   TEXT,
  current_stop_id    TEXT,
  last_candidate_id  TEXT,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE OR REPLACE VIEW geo_work.place_set_overview AS
WITH ranked_candidates AS (
    SELECT
        pc.place_set_id,
        pc.place_candidate_id,
        pc.proposed_canonical_name,
        pc.score,
        ROW_NUMBER() OVER (
            PARTITION BY pc.place_set_id
            ORDER BY
                pc.score DESC NULLS LAST,
                pc.proposed_canonical_name ASC
        ) AS rn
    FROM geo_work.place_candidates pc
)
SELECT
    pcs.place_set_id,
    pcs.created_at,
    pcs.context_key,
    pcs.source_extract_run_id,

    -- ⭐ human-friendly label
    rc.proposed_canonical_name AS display_name,

    rc.score AS display_score,

    COUNT(pc2.place_candidate_id) AS n_candidates
FROM geo_work.place_candidate_sets pcs
JOIN ranked_candidates rc
  ON rc.place_set_id = pcs.place_set_id
 AND rc.rn = 1
JOIN geo_work.place_candidates pc2
  ON pc2.place_set_id = pcs.place_set_id
GROUP BY
    pcs.place_set_id,
    pcs.created_at,
    pcs.context_key,
    pcs.source_extract_run_id,
    rc.proposed_canonical_name,
    rc.score;
