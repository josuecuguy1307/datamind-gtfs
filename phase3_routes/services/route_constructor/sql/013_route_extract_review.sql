ALTER TABLE route_raw.route_jobs
  ADD COLUMN IF NOT EXISTS extractor_source TEXT,
  ADD COLUMN IF NOT EXISTS extractor_review JSONB;

CREATE INDEX IF NOT EXISTS idx_route_jobs_extractor_source_created
  ON route_raw.route_jobs (extractor_source, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_route_jobs_chosen_relation_created
  ON route_raw.route_jobs (chosen_osm_relation_id, created_at DESC)
  WHERE chosen_osm_relation_id IS NOT NULL;
