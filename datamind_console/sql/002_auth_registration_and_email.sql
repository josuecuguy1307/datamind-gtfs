BEGIN;

ALTER TABLE console.users
  ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS console.email_verification_tokens (
  token_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES console.users(user_id) ON DELETE CASCADE,
  email         CITEXT NOT NULL,
  token         TEXT NOT NULL UNIQUE,
  requested_by  UUID REFERENCES console.users(user_id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at    TIMESTAMPTZ NOT NULL,
  consumed_at   TIMESTAMPTZ,
  CONSTRAINT chk_console_email_verification_expiry CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_console_email_verif_user
  ON console.email_verification_tokens(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_console_email_verif_active
  ON console.email_verification_tokens(expires_at)
  WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS console.email_outbox (
  email_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  to_email      CITEXT NOT NULL,
  subject       TEXT NOT NULL,
  body          TEXT NOT NULL,
  provider      TEXT NOT NULL DEFAULT 'mock',
  status        TEXT NOT NULL DEFAULT 'queued',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  sent_at       TIMESTAMPTZ,
  error_message TEXT,
  CONSTRAINT chk_console_email_outbox_status CHECK (status IN ('queued', 'sent', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_console_email_outbox_created
  ON console.email_outbox(created_at DESC);

COMMIT;
