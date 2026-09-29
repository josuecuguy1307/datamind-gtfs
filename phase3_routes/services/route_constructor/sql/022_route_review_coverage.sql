-- ============================================================
-- Phase 3 review + coverage layer
-- Non-destructive canonicalization, global catalog, coverage gaps
-- Safe to re-run.
-- ============================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS route_review;

CREATE OR REPLACE FUNCTION route_review.touch_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS route_review.route_job_dedupe_groups (
  dedupe_group_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_route_id UUID NOT NULL
    REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  chosen_osm_relation_id BIGINT NULL,
  dedupe_source TEXT NOT NULL DEFAULT 'extractor_relation_id',
  dedupe_reason TEXT NOT NULL DEFAULT 'same_chosen_osm_relation_id',
  evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  group_status TEXT NOT NULL DEFAULT 'proposed'
    CHECK (group_status IN ('proposed', 'confirmed', 'rejected', 'archived')),
  reviewable BOOLEAN NOT NULL DEFAULT TRUE,
  created_by_system BOOLEAN NOT NULL DEFAULT TRUE,
  reviewed_at TIMESTAMPTZ NULL,
  reviewed_by TEXT NULL,
  notes TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_route_job_dedupe_groups_relation
  ON route_review.route_job_dedupe_groups(chosen_osm_relation_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_route_job_dedupe_groups_canonical
  ON route_review.route_job_dedupe_groups(canonical_route_id, updated_at DESC);

DROP TRIGGER IF EXISTS trg_route_review_touch_dedupe_groups
  ON route_review.route_job_dedupe_groups;

CREATE TRIGGER trg_route_review_touch_dedupe_groups
BEFORE UPDATE ON route_review.route_job_dedupe_groups
FOR EACH ROW
EXECUTE FUNCTION route_review.touch_updated_at();

CREATE TABLE IF NOT EXISTS route_review.route_job_dedupe_memberships (
  route_id UUID PRIMARY KEY
    REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  dedupe_group_id UUID NOT NULL
    REFERENCES route_review.route_job_dedupe_groups(dedupe_group_id) ON DELETE CASCADE,
  canonical_route_id UUID NOT NULL
    REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  membership_role TEXT NOT NULL DEFAULT 'canonical'
    CHECK (membership_role IN ('canonical', 'duplicate')),
  membership_status TEXT NOT NULL DEFAULT 'active'
    CHECK (membership_status IN ('active', 'suppressed', 'restored')),
  review_status TEXT NOT NULL DEFAULT 'proposed'
    CHECK (review_status IN ('proposed', 'confirmed', 'rejected', 'restored')),
  dedupe_reason TEXT NOT NULL DEFAULT 'same_chosen_osm_relation_id',
  reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by_system BOOLEAN NOT NULL DEFAULT TRUE,
  reviewed_at TIMESTAMPTZ NULL,
  reviewed_by TEXT NULL,
  notes TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_route_job_dedupe_memberships_group
  ON route_review.route_job_dedupe_memberships(dedupe_group_id, membership_role, membership_status);

CREATE INDEX IF NOT EXISTS idx_route_job_dedupe_memberships_canonical
  ON route_review.route_job_dedupe_memberships(canonical_route_id, membership_status);

DROP TRIGGER IF EXISTS trg_route_review_touch_dedupe_memberships
  ON route_review.route_job_dedupe_memberships;

CREATE TRIGGER trg_route_review_touch_dedupe_memberships
BEFORE UPDATE ON route_review.route_job_dedupe_memberships
FOR EACH ROW
EXECUTE FUNCTION route_review.touch_updated_at();

CREATE TABLE IF NOT EXISTS route_review.coverage_gaps (
  gap_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dedupe_key TEXT NOT NULL UNIQUE,
  source_catalog TEXT NOT NULL,
  sector_key TEXT NOT NULL,
  sector_label TEXT NULL,
  route_family_hint TEXT NOT NULL,
  known_aliases TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  start_hint TEXT NULL,
  end_hint TEXT NULL,
  direction_hint TEXT NULL,
  evidence_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  related_route_ids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[],
  classification_status TEXT NOT NULL DEFAULT 'needs_review'
    CHECK (classification_status IN ('needs_review', 'still_extractable', 'non_reliably_extractable')),
  classification_confidence DOUBLE PRECISION NULL,
  classification_source TEXT NOT NULL DEFAULT 'system',
  operator_override_classification TEXT NULL
    CHECK (operator_override_classification IN ('still_extractable', 'non_reliably_extractable')),
  manual_priority TEXT NOT NULL DEFAULT 'medium'
    CHECK (manual_priority IN ('low', 'medium', 'high', 'critical')),
  recommended_next_action TEXT NULL,
  heuristic_notes JSONB NOT NULL DEFAULT '{}'::jsonb,
  resolution_status TEXT NOT NULL DEFAULT 'open'
    CHECK (resolution_status IN ('open', 'in_progress', 'resolved', 'dismissed')),
  resolved_route_id UUID NULL
    REFERENCES route_raw.route_jobs(route_id) ON DELETE SET NULL,
  resolved_prod_route_id UUID NULL
    REFERENCES route_prod.routes(route_id) ON DELETE SET NULL,
  reviewed_at TIMESTAMPTZ NULL,
  reviewed_by TEXT NULL,
  notes TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_coverage_gaps_sector
  ON route_review.coverage_gaps(sector_key, resolution_status, manual_priority, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_coverage_gaps_classification
  ON route_review.coverage_gaps(classification_status, resolution_status, updated_at DESC);

DROP TRIGGER IF EXISTS trg_route_review_touch_coverage_gaps
  ON route_review.coverage_gaps;

CREATE TRIGGER trg_route_review_touch_coverage_gaps
BEFORE UPDATE ON route_review.coverage_gaps
FOR EACH ROW
EXECUTE FUNCTION route_review.touch_updated_at();

ALTER TABLE route_work.relation_stop_prior
  ADD COLUMN IF NOT EXISTS match_state TEXT;

ALTER TABLE route_work.manual_sequence_exports
  ADD COLUMN IF NOT EXISTS coverage_gap_id UUID NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.table_constraints
    WHERE table_schema='route_work'
      AND table_name='manual_sequence_exports'
      AND constraint_name='fk_manual_sequence_exports_gap'
  ) THEN
    ALTER TABLE route_work.manual_sequence_exports
      ADD CONSTRAINT fk_manual_sequence_exports_gap
      FOREIGN KEY (coverage_gap_id)
      REFERENCES route_review.coverage_gaps(gap_id)
      ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_manual_sequence_exports_gap
  ON route_work.manual_sequence_exports(coverage_gap_id, created_at DESC);

CREATE OR REPLACE VIEW route_review.route_job_canonicalization_v1 AS
SELECT
  rj.route_id::text AS route_id,
  COALESCE(m.canonical_route_id, rj.route_id)::text AS canonical_route_id,
  m.dedupe_group_id::text AS dedupe_group_id,
  COALESCE(m.membership_role, 'canonical') AS membership_role,
  COALESCE(m.membership_status, 'active') AS membership_status,
  COALESCE(m.review_status, 'confirmed') AS review_status,
  COALESCE(m.dedupe_reason, 'none') AS dedupe_reason,
  COALESCE(g.group_status, 'confirmed') AS group_status,
  COALESCE(g.reviewable, FALSE) AS reviewable,
  g.chosen_osm_relation_id,
  g.evidence_json
FROM route_raw.active_route_jobs rj
LEFT JOIN route_review.route_job_dedupe_memberships m
  ON m.route_id = rj.route_id
LEFT JOIN route_review.route_job_dedupe_groups g
  ON g.dedupe_group_id = m.dedupe_group_id;

DROP VIEW IF EXISTS route_review.phase3_sector_coverage_v1 CASCADE;
DROP VIEW IF EXISTS route_review.phase3_global_catalog_v1 CASCADE;
CREATE OR REPLACE VIEW route_review.phase3_global_catalog_v1 AS
WITH relation_counts AS (
  SELECT
    route_id,
    COUNT(*)::int AS relation_candidate_count
  FROM route_raw.relation_candidates
  GROUP BY route_id
),
chosen_rel_from_candidates AS (
  SELECT DISTINCT ON (rc.route_id)
    rc.route_id,
    NULLIF(BTRIM(rc.name), '') AS chosen_rel_name,
    NULLIF(BTRIM(rc.ref), '')  AS chosen_rel_ref,
    NULLIF(BTRIM(rc.operator), '') AS chosen_rel_operator,
    rc.osm_relation_id AS chosen_rel_osm_id
  FROM route_raw.relation_candidates rc
  INNER JOIN route_raw.active_route_jobs rj
    ON rj.route_id = rc.route_id
    AND rj.chosen_osm_relation_id = rc.osm_relation_id
  ORDER BY rc.route_id, rc.found_at DESC
),
chosen_rel_from_raw AS (
  SELECT
    orr.route_id,
    NULLIF(BTRIM(rel_elem.value->'tags'->>'name'), '') AS chosen_rel_name,
    NULLIF(BTRIM(rel_elem.value->'tags'->>'ref'), '')  AS chosen_rel_ref,
    NULLIF(BTRIM(rel_elem.value->'tags'->>'operator'), '') AS chosen_rel_operator,
    orr.osm_relation_id AS chosen_rel_osm_id
  FROM route_raw.osm_relations_raw orr
  INNER JOIN route_raw.active_route_jobs rj
    ON rj.route_id = orr.route_id
    AND rj.chosen_osm_relation_id = orr.osm_relation_id
  CROSS JOIN LATERAL (
    SELECT elem.value
    FROM jsonb_array_elements(orr.overpass_json->'elements') AS elem(value)
    WHERE elem.value->>'type' = 'relation'
    LIMIT 1
  ) rel_elem
  WHERE NOT EXISTS (
    SELECT 1 FROM route_raw.relation_candidates rc
    WHERE rc.route_id = orr.route_id AND rc.osm_relation_id = orr.osm_relation_id
  )
),
chosen_relation_info AS (
  SELECT * FROM chosen_rel_from_candidates
  UNION ALL
  SELECT * FROM chosen_rel_from_raw
),
osm_from_to AS (
  SELECT
    orr.route_id,
    NULLIF(BTRIM(rel_elem.value->'tags'->>'from'), '') AS osm_from,
    NULLIF(BTRIM(rel_elem.value->'tags'->>'to'), '') AS osm_to,
    NULLIF(BTRIM(rel_elem.value->'tags'->>'network'), '') AS osm_network,
    CASE
      WHEN NULLIF(BTRIM(rel_elem.value->'tags'->>'from'), '') IS NOT NULL
       AND NULLIF(BTRIM(rel_elem.value->'tags'->>'to'), '') IS NOT NULL
      THEN BTRIM(rel_elem.value->'tags'->>'from') || ' – ' || BTRIM(rel_elem.value->'tags'->>'to')
      ELSE NULL
    END AS osm_from_to_label
  FROM route_raw.osm_relations_raw orr
  INNER JOIN route_raw.active_route_jobs rj ON rj.route_id = orr.route_id
  CROSS JOIN LATERAL (
    SELECT elem.value
    FROM jsonb_array_elements(orr.overpass_json->'elements') AS elem(value)
    WHERE elem.value->>'type' = 'relation'
    LIMIT 1
  ) rel_elem
),
prior_stats AS (
  SELECT
    route_id,
    COUNT(*)::int AS prior_stop_count,
    SUM(CASE WHEN matched_stop_node_id IS NOT NULL THEN 1 ELSE 0 END)::int AS matched_count,
    SUM(CASE WHEN COALESCE(match_state, '') = 'ambiguous' THEN 1 ELSE 0 END)::int AS ambiguous_count,
    SUM(
      CASE
        WHEN matched_stop_node_id IS NULL AND COALESCE(match_state, '') <> 'ambiguous' THEN 1
        ELSE 0
      END
    )::int AS unmatched_count
  FROM route_work.relation_stop_prior
  GROUP BY route_id
),
sequence_counts AS (
  SELECT
    scs.route_id,
    COUNT(*)::int AS stop_sequence_set_count,
    COALESCE(SUM(COALESCE(scc.n_candidates, 0)), 0)::int AS stop_sequence_candidate_count
  FROM route_work.stop_sequence_candidate_sets scs
  LEFT JOIN (
    SELECT
      set_id,
      COUNT(*)::int AS n_candidates
    FROM route_work.stop_sequence_candidates
    GROUP BY set_id
  ) scc
    ON scc.set_id = scs.set_id
  GROUP BY scs.route_id
),
geometry_counts AS (
  SELECT
    gcs.route_id,
    COUNT(*)::int AS geometry_set_count,
    COALESCE(SUM(COALESCE(gcc.n_candidates, 0)), 0)::int AS geometry_candidate_count
  FROM route_work.geometry_candidate_sets gcs
  LEFT JOIN (
    SELECT
      set_id,
      COUNT(*)::int AS n_candidates
    FROM route_work.geometry_candidates
    GROUP BY set_id
  ) gcc
    ON gcc.set_id = gcs.set_id
  GROUP BY gcs.route_id
),
route_approval AS (
  SELECT
    route_id,
    MAX(approved_at) AS route_approved_at
  FROM route_work.route_approvals
  GROUP BY route_id
),
manual_stats AS (
  SELECT
    route_id,
    COUNT(*)::int AS manual_export_count,
    BOOL_OR(COALESCE(source, '') = 'manual_builder') AS manual_origin,
    MAX(created_at) AS latest_manual_export_at,
    (ARRAY_AGG(export_id::text ORDER BY created_at DESC))[1] AS latest_manual_export_id,
    (ARRAY_AGG(coverage_gap_id::text ORDER BY created_at DESC))[1] AS latest_coverage_gap_id
  FROM route_work.manual_sequence_exports
  GROUP BY route_id
),
dedupe AS (
  SELECT
    route_id::uuid AS route_id,
    canonical_route_id::uuid AS canonical_route_id,
    dedupe_group_id::uuid AS dedupe_group_id,
    membership_role,
    membership_status,
    review_status,
    dedupe_reason,
    group_status,
    reviewable
  FROM route_review.route_job_canonicalization_v1
),
catalog AS (
  SELECT
    rj.route_id,
    COALESCE(d.canonical_route_id, rj.route_id) AS canonical_route_id,
    d.dedupe_group_id,
    COALESCE(d.membership_role, 'canonical') AS dedupe_membership_role,
    COALESCE(d.membership_status, 'active') AS dedupe_membership_status,
    COALESCE(d.review_status, 'confirmed') AS dedupe_review_status,
    COALESCE(d.group_status, 'confirmed') AS dedupe_group_status,
    COALESCE(d.reviewable, FALSE) AS dedupe_reviewable,
    COALESCE(d.dedupe_reason, 'none') AS dedupe_reason,
    rj.route_id::text AS route_job_id,
    rj.created_at AS route_job_created_at,
    rj.created_by,
    COALESCE(rj.status, 'new') AS route_job_status,
    rj.notes,
    rj.area_key,
    rj.bbox,
    rj.known_ref,
    rj.chosen_osm_relation_id,
    rj.extractor_source,
    rj.extractor_review,
    COALESCE(rc.relation_candidate_count, 0) AS relation_candidate_count,
    COALESCE(sr.service_route_id, rj.service_route_id)::text AS service_route_id,
    COALESCE(rj.direction_id, srd.direction_id)::int AS direction_id,
    COALESCE(sr.route_ref, NULLIF(rj.known_ref, '')) AS service_route_ref,
    sr.route_name AS service_route_name,
    sr.operator_name AS service_route_operator,
    COALESCE(sr.route_approval_status, 'pending') AS service_route_status,
    COALESCE(srd.phase3_progress_step, 0)::int AS phase3_progress_step,
    COALESCE(srd.direction_approval_status, 'pending') AS direction_approval_status,
    COALESCE(srd.geom_source, 'unknown') AS geom_source,
    (rp.route_id IS NOT NULL) AS has_prod_route,
    rp.route_name AS prod_route_name,
    rp.route_aliases,
    rp.human_verified,
    ps.prior_stop_count,
    ps.matched_count,
    ps.unmatched_count,
    ps.ambiguous_count,
    COALESCE(sc.stop_sequence_set_count, 0) AS stop_sequence_set_count,
    COALESCE(sc.stop_sequence_candidate_count, 0) AS stop_sequence_candidate_count,
    COALESCE(gc.geometry_set_count, 0) AS geometry_set_count,
    COALESCE(gc.geometry_candidate_count, 0) AS geometry_candidate_count,
    (ra.route_id IS NOT NULL) AS has_route_approval,
    ms.manual_export_count,
    COALESCE(ms.manual_origin, FALSE) AS manual_origin,
    ms.latest_manual_export_at,
    ms.latest_manual_export_id,
    ms.latest_coverage_gap_id,
    NULLIF(BTRIM(rj.extractor_review #>> '{target,group}'), '') AS target_group,
    NULLIF(BTRIM(rj.extractor_review #>> '{target,place_bundle}'), '') AS target_place_bundle,
    NULLIF(BTRIM(rj.extractor_review #>> '{target,place}'), '') AS target_place,
    NULLIF(BTRIM(rj.extractor_review #>> '{geography,sector_hint}'), '') AS sector_hint,
    NULLIF(BTRIM(rj.extractor_review #>> '{geography,corridor_hint}'), '') AS corridor_hint,
    NULLIF(BTRIM(rj.extractor_review #>> '{hints,route_hint_raw}'), '') AS route_hint,
    NULLIF(BTRIM(rj.extractor_review #>> '{hints,cooperative_hint}'), '') AS cooperative_hint,
    NULLIF(BTRIM(rj.extractor_review #>> '{source_document}'), '') AS source_document,
    NULLIF(BTRIM(rj.extractor_review #>> '{dedupe,novelty_status}'), '') AS extractor_novelty_status,
    NULLIF(BTRIM(rj.extractor_review #>> '{fetch,fetch_status}'), '') AS fetch_status,
    NULLIF(BTRIM(rj.extractor_review #>> '{discover,signal_strength}'), '') AS extractor_signal_strength,
    cri.chosen_rel_name,
    cri.chosen_rel_ref,
    cri.chosen_rel_operator,
    oft.osm_from,
    oft.osm_to,
    oft.osm_from_to_label,
    oft.osm_network
  FROM route_raw.active_route_jobs rj
  LEFT JOIN relation_counts rc
    ON rc.route_id = rj.route_id
  LEFT JOIN chosen_relation_info cri
    ON cri.route_id = rj.route_id
  LEFT JOIN osm_from_to oft
    ON oft.route_id = rj.route_id
  LEFT JOIN route_review.route_job_canonicalization_v1 d0
    ON d0.route_id::uuid = rj.route_id
  LEFT JOIN dedupe d
    ON d.route_id = rj.route_id
  LEFT JOIN route_raw.service_route_directions srd
    ON srd.route_id = rj.route_id
  LEFT JOIN route_raw.service_routes sr
    ON sr.service_route_id = COALESCE(rj.service_route_id, srd.service_route_id)
  LEFT JOIN route_prod.routes rp
    ON rp.route_id = rj.route_id
  LEFT JOIN prior_stats ps
    ON ps.route_id = rj.route_id
  LEFT JOIN sequence_counts sc
    ON sc.route_id = rj.route_id
  LEFT JOIN geometry_counts gc
    ON gc.route_id = rj.route_id
  LEFT JOIN route_approval ra
    ON ra.route_id = rj.route_id
  LEFT JOIN manual_stats ms
    ON ms.route_id = rj.route_id
)
SELECT
  c.route_job_id,
  c.canonical_route_id::text AS canonical_route_job_id,
  c.dedupe_group_id::text AS dedupe_group_id,
  c.dedupe_membership_role,
  c.dedupe_membership_status,
  c.dedupe_review_status,
  c.dedupe_group_status,
  c.dedupe_reviewable,
  c.dedupe_reason,
  (c.route_job_id = c.canonical_route_id::text) AS is_canonical_route_job,
  (c.dedupe_membership_status = 'suppressed') AS is_suppressed_duplicate,
  c.service_route_id,
  c.direction_id,
  c.service_route_ref,
  c.service_route_name,
  c.prod_route_name,
  c.service_route_operator,
  COALESCE(
    NULLIF(BTRIM(c.service_route_name), ''),
    NULLIF(BTRIM(c.prod_route_name), ''),
    NULLIF(BTRIM(c.service_route_ref), ''),
    NULLIF(BTRIM(c.chosen_rel_name), ''),
    NULLIF(BTRIM(c.chosen_rel_ref), ''),
    NULLIF(BTRIM(c.osm_from_to_label), ''),
    NULLIF(BTRIM(c.route_hint), ''),
    NULLIF(BTRIM(c.target_place), ''),
    ('route_' || LEFT(c.route_job_id, 8))
  ) AS route_family_label,
  CASE
    WHEN c.dedupe_group_id IS NOT NULL THEN c.canonical_route_id::text
    ELSE COALESCE(c.service_route_id, c.canonical_route_id::text, c.route_job_id)
  END AS route_family_key,
  c.route_job_created_at,
  c.created_by,
  c.route_job_status,
  c.notes,
  c.chosen_osm_relation_id,
  c.extractor_source,
  c.area_key,
  c.bbox,
  c.known_ref,
  c.target_group,
  c.target_place_bundle,
  c.target_place,
  c.sector_hint,
  c.corridor_hint,
  c.route_hint,
  c.cooperative_hint,
  c.source_document,
  c.extractor_novelty_status,
  c.fetch_status,
  c.extractor_signal_strength,
  COALESCE(c.relation_candidate_count, 0) AS relation_candidate_count,
  COALESCE(c.prior_stop_count, 0) AS prior_stop_count,
  COALESCE(c.matched_count, 0) AS matched_count,
  COALESCE(c.unmatched_count, 0) AS unmatched_count,
  COALESCE(c.ambiguous_count, 0) AS ambiguous_count,
  COALESCE(c.stop_sequence_set_count, 0) AS stop_sequence_set_count,
  COALESCE(c.stop_sequence_candidate_count, 0) AS stop_sequence_candidate_count,
  COALESCE(c.geometry_set_count, 0) AS geometry_set_count,
  COALESCE(c.geometry_candidate_count, 0) AS geometry_candidate_count,
  COALESCE(c.phase3_progress_step, 0) AS phase3_progress_step,
  c.direction_approval_status,
  c.geom_source,
  c.service_route_status,
  CASE
    WHEN c.chosen_osm_relation_id IS NOT NULL THEN 'complete'
    WHEN COALESCE(c.relation_candidate_count, 0) > 0 THEN 'candidates_found'
    ELSE 'not_started'
  END AS step05_state,
  CASE
    WHEN COALESCE(c.prior_stop_count, 0) <= 0 THEN 'not_started'
    WHEN COALESCE(c.ambiguous_count, 0) > 0 THEN 'blocked_ambiguous'
    WHEN COALESCE(c.unmatched_count, 0) > 0 THEN 'blocked_unmatched'
    WHEN COALESCE(c.matched_count, 0) >= COALESCE(c.prior_stop_count, 0) THEN 'complete'
    ELSE 'partial'
  END AS step20_state,
  CASE
    WHEN c.has_prod_route THEN 'prod'
    WHEN c.has_route_approval THEN 'approved'
    WHEN COALESCE(c.geometry_candidate_count, 0) > 0 THEN 'generated'
    WHEN COALESCE(c.stop_sequence_candidate_count, 0) > 0 THEN 'sequence_ready'
    WHEN c.chosen_osm_relation_id IS NOT NULL THEN 'extracted'
    ELSE 'not_started'
  END AS geometry_status,
  CASE
    WHEN c.has_prod_route THEN 'prod'
    WHEN c.has_route_approval THEN 'approved'
    WHEN COALESCE(c.direction_approval_status, 'pending') = 'ready' THEN 'ready'
    ELSE 'pending'
  END AS approval_status,
  CASE
    WHEN c.has_prod_route THEN 'in_prod'
    ELSE 'not_in_prod'
  END AS prod_status,
  (COALESCE(c.manual_origin, FALSE) OR COALESCE(c.geom_source, 'unknown') = 'manual') AS manual_origin,
  COALESCE(c.manual_export_count, 0) AS manual_export_count,
  c.latest_manual_export_at,
  c.latest_manual_export_id,
  c.latest_coverage_gap_id,
  COALESCE(
    NULLIF(BTRIM(c.sector_hint), ''),
    NULLIF(BTRIM(REPLACE(c.target_group, '_', ' ')), ''),
    NULLIF(BTRIM(REPLACE(c.target_place_bundle, '_', ' ')), ''),
    NULLIF(BTRIM(REPLACE(c.area_key, '_', ' ')), ''),
    'Unassigned'
  ) AS sector_label,
  COALESCE(
    NULLIF(BTRIM(c.sector_hint), ''),
    NULLIF(BTRIM(c.target_group), ''),
    NULLIF(BTRIM(c.target_place_bundle), ''),
    NULLIF(BTRIM(c.area_key), ''),
    'unassigned'
  ) AS sector_key,
  jsonb_build_object(
    'extractor_novelty_status', c.extractor_novelty_status,
    'fetch_status', c.fetch_status,
    'prior_stop_count', COALESCE(c.prior_stop_count, 0),
    'unmatched_count', COALESCE(c.unmatched_count, 0),
    'ambiguous_count', COALESCE(c.ambiguous_count, 0),
    'geometry_candidate_count', COALESCE(c.geometry_candidate_count, 0)
  ) AS diagnostics_summary,
  c.chosen_rel_name,
  c.chosen_rel_ref,
  c.chosen_rel_operator,
  c.osm_from,
  c.osm_to,
  c.osm_from_to_label,
  c.osm_network,
  -- operator_hint: strongest operator evidence wins
  -- Precedence: chosen_rel_operator (OSM relation) > service_route_operator > cooperative_hint (stale fallback)
  COALESCE(
    NULLIF(BTRIM(c.chosen_rel_operator), ''),
    NULLIF(BTRIM(c.service_route_operator), ''),
    NULLIF(BTRIM(c.cooperative_hint), '')
  ) AS operator_hint,
  -- Preserve raw hint for auditability
  NULLIF(BTRIM(c.cooperative_hint), '') AS operator_hint_raw,
  -- Conflict flag: TRUE when cooperative_hint or service_route_operator would have shown a different operator
  CASE
    WHEN NULLIF(BTRIM(c.chosen_rel_operator), '') IS NOT NULL
     AND (
       NULLIF(BTRIM(c.cooperative_hint), '') IS NOT NULL
         AND UPPER(BTRIM(c.chosen_rel_operator)) <> UPPER(BTRIM(c.cooperative_hint))
       OR
       NULLIF(BTRIM(c.service_route_operator), '') IS NOT NULL
         AND UPPER(BTRIM(c.chosen_rel_operator)) <> UPPER(BTRIM(c.service_route_operator))
     )
    THEN TRUE
    ELSE FALSE
  END AS operator_conflict_flag,
  -- Which source won
  CASE
    WHEN NULLIF(BTRIM(c.chosen_rel_operator), '') IS NOT NULL THEN 'chosen_rel_operator'
    WHEN NULLIF(BTRIM(c.service_route_operator), '') IS NOT NULL THEN 'service_route_operator'
    WHEN NULLIF(BTRIM(c.cooperative_hint), '') IS NOT NULL THEN 'cooperative_hint'
    ELSE NULL
  END AS operator_source_used,
  CASE
    WHEN LOWER(COALESCE(c.notes, '')) LIKE '%test%'
      OR LOWER(COALESCE(c.notes, '')) LIKE '%trash%'
      OR LOWER(COALESCE(c.known_ref, '')) LIKE '%test%'
      OR LOWER(COALESCE(c.route_job_status, '')) LIKE '%test%'
      THEN 'test_quarantine'
    WHEN c.route_job_status = 'merged_duplicate'
      THEN 'merged_duplicate'
    WHEN c.route_job_status = 'extraction_failed'
      THEN 'extraction_failed'
    ELSE 'active'
  END AS inventory_status
FROM catalog c;

CREATE OR REPLACE VIEW route_review.phase3_sector_coverage_v1 AS
WITH family_rollup AS (
  SELECT
    sector_key,
    COALESCE(
      NULLIF(BTRIM(REPLACE(sector_label, '_', ' ')), ''),
      NULLIF(BTRIM(REPLACE(sector_key, '_', ' ')), ''),
      'unassigned'
    ) AS sector_label,
    route_family_key,
    MAX(route_family_label) AS route_family_label,
    BOOL_OR(step05_state = 'complete') AS any_extracted,
    BOOL_OR(step20_state = 'complete') AS any_step20_complete,
    BOOL_OR(geometry_status IN ('generated', 'approved', 'prod')) AS any_geometry,
    BOOL_OR(approval_status IN ('approved', 'prod')) AS any_approved,
    BOOL_OR(prod_status = 'in_prod') AS any_prod,
    BOOL_OR(dedupe_membership_status = 'suppressed') AS any_suppressed,
    BOOL_OR(manual_origin) AS any_manual,
    BOOL_OR(direction_id = 0 AND direction_approval_status IN ('ready', 'approved')) AS direction0_ready,
    BOOL_OR(direction_id = 1 AND direction_approval_status IN ('ready', 'approved')) AS direction1_ready
  FROM route_review.phase3_global_catalog_v1
  WHERE inventory_status = 'active'
  GROUP BY sector_key, sector_label, route_family_key
)
SELECT
  sector_key,
  MAX(sector_label) AS sector_label,
  COUNT(*)::int AS route_family_count,
  SUM(CASE WHEN any_extracted THEN 1 ELSE 0 END)::int AS extracted_family_count,
  SUM(CASE WHEN any_step20_complete THEN 1 ELSE 0 END)::int AS step20_complete_family_count,
  SUM(CASE WHEN any_geometry THEN 1 ELSE 0 END)::int AS geometry_ready_family_count,
  SUM(CASE WHEN any_approved THEN 1 ELSE 0 END)::int AS approved_family_count,
  SUM(CASE WHEN any_prod THEN 1 ELSE 0 END)::int AS prod_family_count,
  SUM(CASE WHEN any_manual THEN 1 ELSE 0 END)::int AS manual_family_count,
  SUM(CASE WHEN any_suppressed THEN 1 ELSE 0 END)::int AS suppressed_family_count,
  SUM(CASE WHEN any_extracted AND NOT any_prod THEN 1 ELSE 0 END)::int AS extracted_not_prod_count,
  SUM(CASE WHEN direction0_ready THEN 1 ELSE 0 END)::int AS direction0_ready_count,
  SUM(CASE WHEN direction1_ready THEN 1 ELSE 0 END)::int AS direction1_ready_count,
  SUM(CASE WHEN NOT any_prod OR NOT (direction0_ready AND direction1_ready) THEN 1 ELSE 0 END)::int AS incomplete_family_count,
  ARRAY_REMOVE(ARRAY_AGG(DISTINCT route_family_label ORDER BY route_family_label), NULL) AS route_families_present
FROM family_rollup
GROUP BY sector_key
ORDER BY MAX(sector_label) ASC, sector_key ASC;

CREATE OR REPLACE VIEW route_review.phase3_coverage_gap_catalog_v1 AS
SELECT
  cg.gap_id::text AS gap_id,
  cg.dedupe_key,
  cg.source_catalog,
  cg.sector_key,
  cg.sector_label,
  cg.route_family_hint,
  cg.known_aliases,
  cg.start_hint,
  cg.end_hint,
  cg.direction_hint,
  cg.evidence_summary,
  cg.related_route_ids::text[] AS related_route_ids,
  CARDINALITY(cg.related_route_ids) AS related_route_count,
  cg.classification_status,
  COALESCE(cg.operator_override_classification, cg.classification_status) AS effective_classification,
  cg.classification_confidence,
  cg.classification_source,
  cg.operator_override_classification,
  cg.manual_priority,
  cg.recommended_next_action,
  cg.heuristic_notes,
  cg.resolution_status,
  cg.resolved_route_id::text AS resolved_route_id,
  cg.resolved_prod_route_id::text AS resolved_prod_route_id,
  cg.reviewed_at,
  cg.reviewed_by,
  cg.notes,
  cg.created_at,
  cg.updated_at
FROM route_review.coverage_gaps cg
ORDER BY cg.sector_label ASC NULLS LAST, cg.manual_priority DESC, cg.route_family_hint ASC;

-- ============================================================
-- Interpretation layer views (non-authoritative suggestion surfaces)
-- These views produce suggestions only — never overwrite canonical truth.
-- ============================================================

-- A. Route interpretation: resolve synthetic labels using OSM raw evidence
CREATE OR REPLACE VIEW route_review.route_interpretation_v1 AS
WITH osm_rel_tags AS (
  SELECT DISTINCT ON (orr.route_id)
    orr.route_id,
    orr.osm_relation_id,
    NULLIF(BTRIM(rel_elem.value->'tags'->>'name'), '') AS osm_name,
    NULLIF(BTRIM(rel_elem.value->'tags'->>'ref'), '') AS osm_ref,
    NULLIF(BTRIM(rel_elem.value->'tags'->>'operator'), '') AS osm_operator,
    NULLIF(BTRIM(rel_elem.value->'tags'->>'from'), '') AS osm_from,
    NULLIF(BTRIM(rel_elem.value->'tags'->>'to'), '') AS osm_to,
    NULLIF(BTRIM(rel_elem.value->'tags'->>'network'), '') AS osm_network
  FROM route_raw.osm_relations_raw orr
  INNER JOIN route_raw.active_route_jobs rj
    ON rj.route_id = orr.route_id
    AND rj.chosen_osm_relation_id = orr.osm_relation_id
  CROSS JOIN LATERAL (
    SELECT elem.value
    FROM jsonb_array_elements(orr.overpass_json->'elements') AS elem(value)
    WHERE elem.value->>'type' = 'relation'
    LIMIT 1
  ) rel_elem
  ORDER BY orr.route_id, orr.fetched_at DESC
)
SELECT
  c.route_job_id,
  c.route_family_label,
  c.sector_key,
  c.sector_label,
  c.inventory_status,
  c.chosen_rel_name,
  c.chosen_rel_ref,
  c.chosen_rel_operator,
  c.operator_hint,
  c.target_place,
  c.target_group,
  c.target_place_bundle,
  c.cooperative_hint,
  c.known_ref,
  c.route_hint,
  c.osm_from,
  c.osm_to,
  c.osm_from_to_label,
  c.osm_network,
  c.route_job_status,
  c.chosen_osm_relation_id,

  -- Interpreted label: best available human-readable name
  COALESCE(
    NULLIF(BTRIM(c.chosen_rel_name), ''),
    ort.osm_name,
    NULLIF(BTRIM(c.service_route_name), ''),
    NULLIF(BTRIM(c.prod_route_name), ''),
    CASE WHEN ort.osm_from IS NOT NULL AND ort.osm_to IS NOT NULL
      THEN ort.osm_from || ' – ' || ort.osm_to ELSE NULL END,
    NULLIF(BTRIM(c.route_hint), ''),
    NULLIF(BTRIM(c.target_place), '')
  ) AS interpreted_name,

  -- Interpreted ref
  COALESCE(
    NULLIF(BTRIM(c.chosen_rel_ref), ''),
    ort.osm_ref,
    NULLIF(BTRIM(c.service_route_ref), ''),
    NULLIF(BTRIM(c.known_ref), '')
  ) AS interpreted_ref,

  -- Interpreted operator
  COALESCE(
    NULLIF(BTRIM(c.chosen_rel_operator), ''),
    ort.osm_operator,
    NULLIF(BTRIM(c.service_route_operator), ''),
    NULLIF(BTRIM(c.cooperative_hint), '')
  ) AS interpreted_operator,

  -- From/to from OSM
  COALESCE(c.osm_from, ort.osm_from) AS interpreted_from,
  COALESCE(c.osm_to, ort.osm_to) AS interpreted_to,

  -- Label quality classification
  CASE
    WHEN c.route_family_label !~ '^route_[0-9a-f]+' THEN 'canonical'
    WHEN ort.osm_name IS NOT NULL THEN 'osm_recoverable'
    WHEN ort.osm_from IS NOT NULL AND ort.osm_to IS NOT NULL THEN 'from_to_recoverable'
    WHEN ort.osm_ref IS NOT NULL THEN 'ref_recoverable'
    WHEN c.route_hint IS NOT NULL OR c.target_place IS NOT NULL THEN 'hint_recoverable'
    ELSE 'unresolved_synthetic'
  END AS label_quality,

  -- Confidence score (0-1)
  CASE
    WHEN c.route_family_label !~ '^route_[0-9a-f]+' THEN 1.0
    WHEN ort.osm_name IS NOT NULL AND ort.osm_operator IS NOT NULL THEN 0.95
    WHEN ort.osm_name IS NOT NULL THEN 0.9
    WHEN ort.osm_from IS NOT NULL AND ort.osm_to IS NOT NULL THEN 0.85
    WHEN ort.osm_ref IS NOT NULL THEN 0.8
    WHEN c.route_hint IS NOT NULL THEN 0.6
    WHEN c.target_place IS NOT NULL THEN 0.4
    ELSE 0.0
  END AS label_confidence,

  -- Evidence source
  CASE
    WHEN c.route_family_label !~ '^route_[0-9a-f]+' THEN 'catalog_canonical'
    WHEN ort.osm_name IS NOT NULL THEN 'osm_relation_raw_tags'
    WHEN ort.osm_from IS NOT NULL THEN 'osm_relation_raw_from_to'
    WHEN ort.osm_ref IS NOT NULL THEN 'osm_relation_raw_ref'
    WHEN c.route_hint IS NOT NULL THEN 'extractor_hint'
    WHEN c.target_place IS NOT NULL THEN 'extractor_target'
    ELSE 'none'
  END AS label_evidence_source,

  -- Unresolved reason
  CASE
    WHEN c.route_family_label !~ '^route_[0-9a-f]+' THEN NULL
    WHEN ort.osm_name IS NOT NULL OR ort.osm_from IS NOT NULL OR ort.osm_ref IS NOT NULL THEN NULL
    WHEN c.route_hint IS NOT NULL OR c.target_place IS NOT NULL THEN NULL
    WHEN c.chosen_osm_relation_id IS NOT NULL AND ort.osm_relation_id IS NULL THEN 'relation_raw_not_fetched'
    WHEN c.chosen_osm_relation_id IS NULL THEN 'no_chosen_relation'
    ELSE 'relation_has_no_tags'
  END AS unresolved_reason

FROM route_review.phase3_global_catalog_v1 c
LEFT JOIN osm_rel_tags ort ON ort.route_id = c.route_job_id::uuid
WHERE c.inventory_status = 'active';

-- B. Sector suggestion: suggest sector assignment for Unassigned routes
CREATE OR REPLACE VIEW route_review.sector_suggestion_v1 AS
WITH operator_sector_map AS (
  -- Build operator → sector mapping from already-assigned routes
  SELECT
    LOWER(BTRIM(operator_hint)) AS operator_key,
    sector_key,
    sector_label,
    COUNT(*) AS assignment_count
  FROM route_review.phase3_global_catalog_v1
  WHERE inventory_status = 'active'
    AND operator_hint IS NOT NULL
    AND sector_key IS NOT NULL
    AND sector_key <> 'unassigned'
    AND BTRIM(sector_key) <> ''
  GROUP BY LOWER(BTRIM(operator_hint)), sector_key, sector_label
),
operator_best_sector AS (
  SELECT DISTINCT ON (operator_key)
    operator_key,
    sector_key AS op_suggested_sector,
    sector_label AS op_suggested_sector_label,
    assignment_count AS op_sector_evidence_count
  FROM operator_sector_map
  ORDER BY operator_key, assignment_count DESC
),
interp AS (
  SELECT * FROM route_review.route_interpretation_v1
),
-- Route-name/from-to pattern-based sector inference for operators not in the existing map
route_pattern_sector AS (
  SELECT
    i.route_job_id,
    CASE
      -- Tumbaco/Cumbaya corridor: routes mentioning Cumbaya, Tumbaco, Lumbisi, Floresta-Cumbaya
      WHEN LOWER(COALESCE(i.interpreted_name, '') || ' ' || COALESCE(i.interpreted_from, '') || ' ' || COALESCE(i.interpreted_to, ''))
        ~ '(cumbay|tumbaco|lumbisi|floresta.*cumbay|cumbay.*floresta|naiq.*cumbay|trans tumbaco|transfloresta)'
        THEN 'Tumbaco-Cumbaya'
      -- Valle de los Chillos: routes mentioning Conocoto, Sangolqui, San Pedro de Taboada, Trebol, El Giron
      WHEN LOWER(COALESCE(i.interpreted_name, '') || ' ' || COALESCE(i.interpreted_from, '') || ' ' || COALESCE(i.interpreted_to, ''))
        ~ '(conocoto|sangolqui|san pedro de taboada|trebol|el giron|buenos aires|los cuarteles)'
        THEN 'Valle de Los Chillos'
      -- Calderon corridor: routes mentioning Calderon, Carapungo, Zabala, San Juan de Calderon
      WHEN LOWER(COALESCE(i.interpreted_name, '') || ' ' || COALESCE(i.interpreted_from, '') || ' ' || COALESCE(i.interpreted_to, ''))
        ~ '(calderon|carapungo|zabala|san juan de calderon)'
        THEN 'calderon'
      -- Guamani corridor: routes mentioning Guamani, Nuevos Horizontes, Ciudadela Lozada
      WHEN LOWER(COALESCE(i.interpreted_name, '') || ' ' || COALESCE(i.interpreted_from, '') || ' ' || COALESCE(i.interpreted_to, ''))
        ~ '(guamani|nuevos horizontes|ciudadela lozada|trinidad.*guamani)'
        THEN 'guamani'
      -- Quito Urbano: central Quito routes (Marin, Comite del Pueblo, Condado, Universidad Central, Ejido)
      WHEN LOWER(COALESCE(i.interpreted_name, '') || ' ' || COALESCE(i.interpreted_from, '') || ' ' || COALESCE(i.interpreted_to, ''))
        ~ '(comite del pueblo|condado|quintana|carcelen bajo|guapulo|alcdatamindlla|dolorosa|estadio olimpico)'
        THEN 'Quito Urbano'
      -- Puembo / El Quinche corridor
      WHEN LOWER(COALESCE(i.interpreted_name, '') || ' ' || COALESCE(i.interpreted_from, '') || ' ' || COALESCE(i.interpreted_to, ''))
        ~ '(el quinche|guayllabamba|puembo|pifo)'
        THEN 'puembo'
      -- Quitumbe / sur corridor
      WHEN LOWER(COALESCE(i.interpreted_name, '') || ' ' || COALESCE(i.interpreted_from, '') || ' ' || COALESCE(i.interpreted_to, ''))
        ~ '(quitumbe|chillogallo|camal metropolitano|forestal alta)'
        THEN 'quito_sur_chillogallo_corridor'
      ELSE NULL
    END AS pattern_sector
  FROM interp i
  WHERE i.sector_key IS NULL OR i.sector_key = 'unassigned' OR BTRIM(i.sector_key) = ''
)
SELECT
  i.route_job_id,
  i.route_family_label,
  i.sector_key AS current_sector,
  i.sector_label AS current_sector_label,
  i.interpreted_name,
  i.interpreted_ref,
  i.interpreted_operator,
  i.interpreted_from,
  i.interpreted_to,
  i.label_quality,
  i.label_confidence,
  i.label_evidence_source,
  i.unresolved_reason,

  -- Sector suggestion: operator match > interpreted operator match > route pattern match
  COALESCE(
    obs.op_suggested_sector,
    CASE
      WHEN i.interpreted_operator IS NOT NULL THEN
        (SELECT os2.op_suggested_sector FROM operator_best_sector os2
         WHERE os2.operator_key = LOWER(BTRIM(i.interpreted_operator)) LIMIT 1)
      ELSE NULL
    END,
    rps.pattern_sector
  ) AS suggested_sector,
  COALESCE(
    obs.op_suggested_sector_label,
    CASE
      WHEN i.interpreted_operator IS NOT NULL THEN
        (SELECT os2.op_suggested_sector_label FROM operator_best_sector os2
         WHERE os2.operator_key = LOWER(BTRIM(i.interpreted_operator)) LIMIT 1)
      ELSE NULL
    END,
    REPLACE(COALESCE(rps.pattern_sector, ''), '_', ' ')
  ) AS suggested_sector_label,

  -- Suggestion reason
  CASE
    WHEN obs.op_suggested_sector IS NOT NULL THEN 'operator_hint_match'
    WHEN i.interpreted_operator IS NOT NULL AND EXISTS (
      SELECT 1 FROM operator_best_sector os2
      WHERE os2.operator_key = LOWER(BTRIM(i.interpreted_operator))
    ) THEN 'interpreted_operator_match'
    WHEN rps.pattern_sector IS NOT NULL THEN 'route_name_pattern'
    WHEN i.target_place IS NOT NULL THEN 'target_place_available'
    WHEN i.target_group IS NOT NULL THEN 'target_group_available'
    ELSE 'no_evidence'
  END AS suggestion_reason,

  -- Confidence
  CASE
    WHEN obs.op_suggested_sector IS NOT NULL AND COALESCE(obs.op_sector_evidence_count, 0) >= 3 THEN 0.9
    WHEN obs.op_suggested_sector IS NOT NULL THEN 0.7
    WHEN i.interpreted_operator IS NOT NULL AND EXISTS (
      SELECT 1 FROM operator_best_sector os2
      WHERE os2.operator_key = LOWER(BTRIM(i.interpreted_operator))
    ) THEN 0.75
    WHEN rps.pattern_sector IS NOT NULL THEN 0.65
    WHEN i.target_place IS NOT NULL THEN 0.5
    ELSE 0.0
  END AS sector_confidence,

  -- Evidence summary
  jsonb_build_object(
    'operator_hint', i.operator_hint,
    'interpreted_operator', i.interpreted_operator,
    'interpreted_from', i.interpreted_from,
    'interpreted_to', i.interpreted_to,
    'target_place', i.target_place,
    'target_group', i.target_group,
    'cooperative_hint', i.cooperative_hint,
    'known_ref', i.known_ref,
    'current_sector', i.sector_key,
    'label_quality', i.label_quality,
    'pattern_sector', rps.pattern_sector
  ) AS evidence_summary,

  'deterministic' AS suggestion_source,
  'pending' AS operator_review_status

FROM interp i
LEFT JOIN operator_best_sector obs
  ON obs.operator_key = LOWER(BTRIM(i.operator_hint))
LEFT JOIN route_pattern_sector rps
  ON rps.route_job_id = i.route_job_id
WHERE i.sector_key IS NULL
   OR i.sector_key = 'unassigned'
   OR BTRIM(i.sector_key) = '';

-- C. Coverage gap reclassification view
CREATE OR REPLACE VIEW route_review.gap_reclassification_v1 AS
WITH gap_related_routes AS (
  SELECT
    cg.gap_id,
    COUNT(DISTINCT rj.route_id) FILTER (WHERE rj.route_id IS NOT NULL) AS active_related_count,
    COUNT(DISTINCT rj.route_id) FILTER (WHERE rj.status = 'relation_fetched') AS fetched_related_count,
    COUNT(DISTINCT rp.route_id) FILTER (WHERE rp.route_id IS NOT NULL) AS prod_related_count,
    BOOL_OR(rj.chosen_osm_relation_id IS NOT NULL) AS any_has_relation,
    ARRAY_AGG(DISTINCT COALESCE(rj.status, 'unknown') ORDER BY COALESCE(rj.status, 'unknown'))
      FILTER (WHERE rj.route_id IS NOT NULL) AS related_statuses
  FROM route_review.coverage_gaps cg
  CROSS JOIN LATERAL unnest(cg.related_route_ids) AS rid(route_id)
  LEFT JOIN route_raw.active_route_jobs rj ON rj.route_id = rid.route_id
  LEFT JOIN route_prod.routes rp ON rp.route_id = rid.route_id
  GROUP BY cg.gap_id
)
SELECT
  cg.gap_id::text,
  cg.sector_key,
  cg.sector_label,
  cg.route_family_hint,
  cg.classification_status AS current_gap_status,
  cg.resolution_status,

  -- Suggested gap class
  CASE
    WHEN cg.resolution_status = 'resolved' THEN 'resolved'
    WHEN grr.prod_related_count > 0 THEN 'promotion_backlog'
    WHEN grr.fetched_related_count > 0 AND grr.active_related_count > 0 THEN 'matching_backlog'
    WHEN grr.any_has_relation AND grr.active_related_count > 0 THEN 'canonicalization_backlog'
    WHEN grr.active_related_count > 0 THEN 'sectorization_backlog'
    WHEN CARDINALITY(cg.related_route_ids) > 0 AND grr.active_related_count = 0 THEN 'fetch_backlog'
    ELSE 'true_extraction_gap'
  END AS suggested_gap_class,

  -- Reason
  CASE
    WHEN cg.resolution_status = 'resolved' THEN 'gap already resolved'
    WHEN grr.prod_related_count > 0 THEN format('%s related routes in prod, gap may be stale', grr.prod_related_count)
    WHEN grr.fetched_related_count > 0 THEN format('%s related routes fetched but not promoted', grr.fetched_related_count)
    WHEN grr.any_has_relation THEN 'related routes have chosen relations, need canonicalization'
    WHEN grr.active_related_count > 0 THEN format('%s active related routes exist, need sector/family assignment', grr.active_related_count)
    WHEN CARDINALITY(cg.related_route_ids) > 0 AND grr.active_related_count = 0 THEN 'related route_ids exist but routes are trashed/missing'
    ELSE 'no related routes found, true extraction needed'
  END AS suggested_gap_reason,

  COALESCE(grr.active_related_count, 0) AS supporting_route_count,
  COALESCE(grr.prod_related_count, 0) AS prod_related_count,
  COALESCE(grr.fetched_related_count, 0) AS fetched_related_count,
  grr.related_statuses AS strongest_related_evidence,

  -- Operator action hint
  CASE
    WHEN cg.resolution_status = 'resolved' THEN 'none'
    WHEN grr.prod_related_count > 0 THEN 'review_and_resolve_gap'
    WHEN grr.fetched_related_count > 0 THEN 'advance_related_routes_through_pipeline'
    WHEN grr.any_has_relation THEN 'canonicalize_related_routes'
    WHEN grr.active_related_count > 0 THEN 'assign_sector_and_fetch'
    WHEN CARDINALITY(cg.related_route_ids) > 0 THEN 'investigate_trashed_related_routes'
    ELSE 'extract_from_overpass'
  END AS operator_action_hint,

  cg.evidence_summary,
  cg.related_route_ids::text[] AS related_route_ids,
  CARDINALITY(cg.related_route_ids) AS related_route_count,
  cg.classification_confidence,
  cg.notes

FROM route_review.coverage_gaps cg
LEFT JOIN gap_related_routes grr ON grr.gap_id = cg.gap_id
ORDER BY cg.sector_label ASC NULLS LAST, cg.route_family_hint ASC;

COMMIT;
