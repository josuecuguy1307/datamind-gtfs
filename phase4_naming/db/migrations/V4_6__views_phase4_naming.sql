BEGIN;

CREATE SCHEMA IF NOT EXISTS semantics;

CREATE OR REPLACE VIEW semantics.v_phase4_name_candidates_latest AS
WITH latest_run AS (
  SELECT route_id, run_id, max(generated_at) AS generated_at
  FROM semantics.route_name_candidates
  GROUP BY route_id, run_id
), chosen AS (
  SELECT DISTINCT ON (route_id)
    route_id,
    run_id,
    generated_at
  FROM latest_run
  ORDER BY route_id, generated_at DESC
)
SELECT
  c.route_id,
  c.run_id,
  c.candidate_id,
  c.rank_pos,
  c.route_name,
  c.route_ref,
  c.operator_name,
  c.source_type,
  c.feature_snapshot_version,
  c.features,
  c.heuristic_score,
  c.model_score,
  c.final_score,
  c.metadata,
  c.generated_at
FROM semantics.route_name_candidates c
JOIN chosen x
  ON x.route_id = c.route_id
 AND x.run_id = c.run_id
ORDER BY c.route_id, c.rank_pos;

CREATE OR REPLACE VIEW semantics.v_phase4_review_queue AS
SELECT
  c.route_id,
  c.run_id,
  count(*)::int AS n_candidates,
  max(c.generated_at) AS candidates_generated_at,
  bool_or(f.is_winner) AS has_winner,
  max(f.created_at) AS reviewed_at
FROM semantics.v_phase4_name_candidates_latest c
LEFT JOIN semantics.route_name_feedback f
  ON f.route_id = c.route_id
 AND f.candidate_id = c.candidate_id
GROUP BY c.route_id, c.run_id
ORDER BY candidates_generated_at DESC;

COMMIT;
