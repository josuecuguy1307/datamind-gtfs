CREATE TABLE IF NOT EXISTS route_work.geometry_stop_recovery (
  geometry_candidate_id UUID PRIMARY KEY
    REFERENCES route_work.geometry_candidates(geometry_candidate_id) ON DELETE CASCADE,
  set_id UUID NOT NULL
    REFERENCES route_work.geometry_candidate_sets(set_id) ON DELETE CASCADE,
  route_id UUID NOT NULL
    REFERENCES route_raw.route_jobs(route_id) ON DELETE CASCADE,
  stop_sequence_candidate_id UUID NULL
    REFERENCES route_work.stop_sequence_candidates(candidate_id) ON DELETE SET NULL,

  original_stop_ids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[],
  original_stop_prior_seqs INT[] NOT NULL DEFAULT ARRAY[]::int[],
  recovered_stop_ids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[],
  ambiguous_nearby_stop_ids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[],
  rejected_nearby_stop_ids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[],
  enriched_stop_ids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[],

  insertion_proposals JSONB NOT NULL DEFAULT '[]'::jsonb,
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
  summary_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_geometry_stop_recovery_set
  ON route_work.geometry_stop_recovery (set_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_geometry_stop_recovery_route
  ON route_work.geometry_stop_recovery (route_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_geometry_stop_recovery_seq
  ON route_work.geometry_stop_recovery (stop_sequence_candidate_id);
