-- ============================================================
-- Phase 3 — inverse completion proposal enrichment
-- Safe additive migration.
-- ============================================================

ALTER TABLE route_work.inverse_direction_status
  ADD COLUMN IF NOT EXISTS top_candidate_scores JSONB NOT NULL DEFAULT '{}'::JSONB,
  ADD COLUMN IF NOT EXISTS proposal_payload JSONB NOT NULL DEFAULT '{}'::JSONB,
  ADD COLUMN IF NOT EXISTS proposal_source TEXT NULL,
  ADD COLUMN IF NOT EXISTS proposal_evaluated_at TIMESTAMPTZ NULL;

ALTER TABLE route_work.inverse_direction_status
  DROP CONSTRAINT IF EXISTS inverse_direction_status_inverse_status_check;

ALTER TABLE route_work.inverse_direction_status
  DROP CONSTRAINT IF EXISTS chk_inverse_direction_status_inverse_status;

ALTER TABLE route_work.inverse_direction_status
  ADD CONSTRAINT chk_inverse_direction_status_inverse_status
  CHECK (
    inverse_status IN (
      'unknown',
      'structurally_ready',
      'structurally_blocked',
      'reliable_opposite_candidate',
      'plausible_opposite_candidate',
      'no_candidate_found'
    )
  );

CREATE INDEX IF NOT EXISTS idx_inverse_direction_status_top_candidate
  ON route_work.inverse_direction_status(top_candidate_route_id)
  WHERE top_candidate_route_id IS NOT NULL;

DROP VIEW IF EXISTS route_work.v_direction_readiness;

CREATE VIEW route_work.v_direction_readiness AS
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
  slot.direction_id::int AS direction_id,
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
  COALESCE(ids.top_candidate_scores, '{}'::JSONB) AS top_candidate_scores,
  COALESCE(ids.proposal_payload, '{}'::JSONB) AS proposal_payload,
  ids.proposal_source,
  ids.proposal_evaluated_at,
  ids.analysis_version,
  ids.last_evaluated_at,
  ids.created_at,
  ids.updated_at
FROM route_raw.service_routes sr
CROSS JOIN (VALUES (0), (1)) AS slot(direction_id)
LEFT JOIN route_raw.service_route_directions srd
  ON srd.service_route_id = sr.service_route_id
 AND srd.direction_id = slot.direction_id
LEFT JOIN route_work.inverse_direction_status ids
  ON ids.service_route_id = sr.service_route_id
 AND ids.direction_id = slot.direction_id;
