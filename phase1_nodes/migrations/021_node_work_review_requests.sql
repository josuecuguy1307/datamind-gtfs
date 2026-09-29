-- 021_node_work_review_requests.sql
-- Request queue for manual/new nodes review (Phase 3 requests + free create)

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS node_work;

CREATE TABLE IF NOT EXISTS node_work.node_review_requests (
  request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source TEXT NOT NULL DEFAULT 'free_create',
  route_id UUID NULL,
  seq INT NULL,
  status TEXT NOT NULL DEFAULT 'requested',

  lat DOUBLE PRECISION NOT NULL,
  lon DOUBLE PRECISION NOT NULL,
  node_type TEXT NOT NULL DEFAULT 'STOP',

  name TEXT NULL,
  ref TEXT NULL,
  operator TEXT NULL,
  tags JSONB NOT NULL DEFAULT '{}'::jsonb,

  requested_by TEXT NULL,
  reviewed_by TEXT NULL,
  reviewed_at TIMESTAMPTZ NULL,
  approved_node_id UUID NULL,

  notes TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT chk_node_review_requests_source
    CHECK (source IN ('phase3_route', 'free_create')),
  CONSTRAINT chk_node_review_requests_status
    CHECK (status IN ('requested', 'approved', 'rejected')),
  CONSTRAINT chk_node_review_requests_type
    CHECK (node_type IN ('STOP', 'POI'))
);

CREATE INDEX IF NOT EXISTS idx_node_review_requests_created
  ON node_work.node_review_requests(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_node_review_requests_source_status
  ON node_work.node_review_requests(source, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_node_review_requests_route_seq
  ON node_work.node_review_requests(route_id, seq);

COMMIT;

