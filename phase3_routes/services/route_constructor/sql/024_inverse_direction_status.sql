-- ============================================================
-- Phase 3 — inverse completion / direction readiness work-state
-- Safe additive migration.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS route_work;

CREATE TABLE IF NOT EXISTS route_work.inverse_direction_status (
  service_route_id UUID NOT NULL
    REFERENCES route_raw.service_routes(service_route_id) ON DELETE CASCADE,
  direction_id SMALLINT NOT NULL
    CHECK (direction_id IN (0, 1)),
  anchor_route_id UUID NULL
    REFERENCES route_raw.route_jobs(route_id) ON DELETE SET NULL,
  bound_route_id UUID NULL
    REFERENCES route_raw.route_jobs(route_id) ON DELETE SET NULL,
  top_candidate_route_id UUID NULL
    REFERENCES route_raw.route_jobs(route_id) ON DELETE SET NULL,
  inverse_status TEXT NOT NULL DEFAULT 'unknown'
    CHECK (inverse_status IN ('unknown', 'structurally_ready', 'structurally_blocked')),
  search_status TEXT NOT NULL DEFAULT 'not_started'
    CHECK (search_status IN ('not_started', 'not_applicable')),
  manual_required BOOLEAN NOT NULL DEFAULT FALSE,
  direction_ready BOOLEAN NOT NULL DEFAULT FALSE,
  blocker_codes TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  blocker_messages TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  evidence_summary JSONB NOT NULL DEFAULT '{}'::JSONB,
  analysis_version TEXT NULL,
  last_evaluated_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (service_route_id, direction_id)
);

CREATE INDEX IF NOT EXISTS idx_inverse_direction_status_ready
  ON route_work.inverse_direction_status(direction_ready, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_inverse_direction_status_bound_route
  ON route_work.inverse_direction_status(bound_route_id)
  WHERE bound_route_id IS NOT NULL;

CREATE OR REPLACE FUNCTION route_work.touch_inverse_direction_status_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_inverse_direction_status_touch ON route_work.inverse_direction_status;

CREATE TRIGGER trg_inverse_direction_status_touch
BEFORE UPDATE ON route_work.inverse_direction_status
FOR EACH ROW
EXECUTE FUNCTION route_work.touch_inverse_direction_status_updated_at();

CREATE OR REPLACE VIEW route_work.v_direction_readiness AS
SELECT
  sr.service_route_id::text AS service_route_id,
  COALESCE(sr.route_ref, '') AS route_short_name,
  CASE
    WHEN NULLIF(sr.route_ref, '') IS NOT NULL AND NULLIF(sr.route_name, '') IS NOT NULL
      THEN sr.route_ref || ' | ' || sr.route_name
    WHEN NULLIF(sr.route_ref, '') IS NOT NULL
      THEN sr.route_ref
    ELSE NULLIF(sr.route_name, '')
  END AS route_label,
  COALESCE(sr.route_name, '') AS route_name,
  COALESCE(sr.operator_name, '') AS operator_name,
  srd.direction_id::int AS direction_id,
  srd.route_id::text AS logical_route_id,
  COALESCE(ids.bound_route_id, srd.route_id)::text AS bound_route_id,
  ids.anchor_route_id::text AS anchor_route_id,
  ids.top_candidate_route_id::text AS top_candidate_route_id,
  COALESCE(srd.phase3_progress_step, 0)::int AS phase3_progress_step,
  COALESCE(srd.direction_approval_status, 'pending') AS direction_approval_status,
  COALESCE(srd.geom_source, 'unknown') AS geom_source,
  COALESCE(ids.inverse_status, 'unknown') AS inverse_status,
  COALESCE(ids.search_status, 'not_started') AS search_status,
  COALESCE(ids.manual_required, FALSE) AS manual_required,
  COALESCE(ids.direction_ready, FALSE) AS direction_ready,
  COALESCE(ids.blocker_codes, ARRAY[]::TEXT[]) AS blocker_codes,
  COALESCE(ids.blocker_messages, ARRAY[]::TEXT[]) AS blocker_messages,
  COALESCE(ids.evidence_summary, '{}'::JSONB) AS evidence_summary,
  ids.analysis_version,
  ids.last_evaluated_at,
  ids.created_at,
  ids.updated_at
FROM route_raw.service_routes sr
JOIN route_raw.service_route_directions srd
  ON srd.service_route_id = sr.service_route_id
LEFT JOIN route_work.inverse_direction_status ids
  ON ids.service_route_id = srd.service_route_id
 AND ids.direction_id = srd.direction_id;
