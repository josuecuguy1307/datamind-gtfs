CREATE TABLE IF NOT EXISTS gtfs_work.route_runtime_estimate_bindings (
  route_id UUID NOT NULL REFERENCES route_prod.routes(route_id) ON DELETE CASCADE,
  direction_id SMALLINT NOT NULL CHECK (direction_id IN (0,1)),
  estimate_id UUID NOT NULL REFERENCES gtfs_work.runtime_route_estimates(estimate_id) ON DELETE CASCADE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(route_id, direction_id)
);

CREATE INDEX IF NOT EXISTS idx_route_runtime_estimate_bindings_estimate
  ON gtfs_work.route_runtime_estimate_bindings(estimate_id);

