CREATE TABLE IF NOT EXISTS gtfs_work.runtime_route_estimates (
  estimate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NOT NULL REFERENCES route_prod.routes(route_id) ON DELETE CASCADE,
  direction_id SMALLINT NOT NULL CHECK (direction_id IN (0,1)),
  area_profile_code TEXT,
  speed_profile_code TEXT,
  dwell_profile_code TEXT,
  intersection_profile_code TEXT,
  peak_profile_code TEXT,
  confidence_profile_code TEXT,
  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
  model_inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
  fallback_reason JSONB NOT NULL DEFAULT '[]'::jsonb,
  route_prior_match JSONB NOT NULL DEFAULT '{}'::jsonb,
  estimated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_runtime_route_estimates_route
  ON gtfs_work.runtime_route_estimates(route_id, direction_id, estimated_at DESC);

CREATE TABLE IF NOT EXISTS gtfs_work.runtime_route_leg_features (
  leg_feature_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  estimate_id UUID NOT NULL REFERENCES gtfs_work.runtime_route_estimates(estimate_id) ON DELETE CASCADE,
  leg_idx INT NOT NULL,
  from_seq INT,
  to_seq INT,
  distance_m DOUBLE PRECISION,
  elev_from_m DOUBLE PRECISION,
  elev_to_m DOUBLE PRECISION,
  grade_pct DOUBLE PRECISION,
  slope_mult DOUBLE PRECISION,
  slope_bin TEXT,
  signal_count INT,
  offpeak_secs DOUBLE PRECISION,
  peak_secs DOUBLE PRECISION,
  offpeak_kmh_effective DOUBLE PRECISION,
  peak_kmh_effective DOUBLE PRECISION,
  attrs JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_runtime_route_leg_features_est
  ON gtfs_work.runtime_route_leg_features(estimate_id, leg_idx);

