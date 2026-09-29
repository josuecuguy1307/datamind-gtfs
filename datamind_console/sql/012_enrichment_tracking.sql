-- 012_enrichment_tracking.sql
-- Adds progressive-enrichment columns to route_prod.routes.
-- See workspace/skills/continuous_grounding_enrichment.md §1 (5-component score formula).
-- See workspace/skills/07_INGESTION_CONTRACT.md §4a (confidence-merge rules).
-- See workspace/skills/hades-quality-gate/SKILL.md Soft Rules (0.60 deploy floor; no staleness rule).

BEGIN;

ALTER TABLE route_prod.routes
  ADD COLUMN IF NOT EXISTS enrichment_score      double precision NOT NULL DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS last_enriched_at      timestamptz,
  ADD COLUMN IF NOT EXISTS enrichment_history    jsonb            NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS pending_human_review  boolean          NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS source_count          smallint         NOT NULL DEFAULT 1;

COMMENT ON COLUMN route_prod.routes.enrichment_score IS
  'Progressive enrichment score 0.0-1.0 per continuous_grounding_enrichment.md §1 (5-component formula).';
COMMENT ON COLUMN route_prod.routes.last_enriched_at IS
  'NULL = never enriched. Updated on every operator-run enrichment merge.';
COMMENT ON COLUMN route_prod.routes.enrichment_history IS
  'Append-only array of enrichment events {timestamp, skill, prior_score, new_score, fields_updated, fields_rejected, evidence_sources, flags}. Flags include terminus_drift_detected, code_disagreement, no_op.';
COMMENT ON COLUMN route_prod.routes.pending_human_review IS
  'TRUE when a confidence-merge resolved to equal-confidence disagreement. Non-overridable passenger-deploy block per hades-quality-gate Soft Rules.';
COMMENT ON COLUMN route_prod.routes.source_count IS
  'Count of distinct research sources that have contributed to this route. Feeds diversity component of enrichment_score.';

-- Index to support batch-mode queries in continuous_grounding_enrichment (lowest score first,
-- oldest enriched first, active only). No scheduler runs against this — the operator does.
CREATE INDEX IF NOT EXISTS routes_enrichment_queue_idx
  ON route_prod.routes (enrichment_score ASC, last_enriched_at ASC NULLS FIRST)
  WHERE deploy_status = 'active';

-- Seed existing routes with an honest baseline score.
-- Component floors per continuous_grounding_enrichment.md §1:
--   completeness (0.30) = 0.00  — catalogs not yet inspected at seed time
--   freshness    (0.20) = 1.0 if created_at within 30d, decays linearly to 0 at 365d
--   diversity    (0.15) = 0.00  — source_count = 1 → log2(2)/log2(8) = 0.33 would be honest;
--                                using 0 to force enrichment to lift it
--   OSM confirm  (0.15) = 0.50 if source LIKE 'osm_relation_%'; 0 otherwise
--                                (osm_edit_age unknown at seed; 0.50 is a conservative mid)
--   stability    (0.20) = 1.00  — no prior-pass contradictions yet
-- Expected range: ~0.20 (non-OSM, stale) to ~0.50 (fresh OSM).
UPDATE route_prod.routes
SET
  last_enriched_at = created_at,
  source_count = 1,
  enrichment_score = ROUND(CAST(
        0.20 * GREATEST(0.0, 1.0 - GREATEST(0, EXTRACT(DAY FROM (now() - created_at))::int - 30)::float / 335.0)
      + 0.15 * (CASE WHEN source LIKE 'osm_relation_%' THEN 0.50 ELSE 0.00 END)
      + 0.20 * 1.0
  AS numeric), 3)
WHERE enrichment_score = 0.0;

-- Sanity check — expect 953 rows touched, scores between ~0.20 and ~0.50.
-- SELECT COUNT(*), MIN(enrichment_score), AVG(enrichment_score)::numeric(5,3), MAX(enrichment_score) FROM route_prod.routes;

COMMIT;
