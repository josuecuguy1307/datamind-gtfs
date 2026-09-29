CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS gtfs_work;
CREATE SCHEMA IF NOT EXISTS gtfs_prod;

-- ------------------------------------------------------------
-- Input projection from Phases 3 + 4
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW gtfs_work.v_route_inputs AS
SELECT
  r.route_id,
  COALESCE(s.route_ref, NULL) AS route_ref,
  COALESCE(s.route_name, r.route_name, 'route_' || LEFT(r.route_id::text, 8)) AS route_name,
  COALESCE(s.operator_name, NULL) AS operator_name,
  COALESCE(s.human_verified, false) AS human_verified,
  COALESCE(s.naming_confidence, 0.0) AS naming_confidence,
  COALESCE(array_length(r.stop_node_ids, 1), 0) AS n_stops,
  ST_AsEWKT(r.geom) AS geom_ewkt,
  r.created_at,
  r.updated_at
FROM route_prod.routes r
LEFT JOIN route_prod.route_semantics s ON s.route_id = r.route_id;

-- ------------------------------------------------------------
-- Scheduling authoring contract
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gtfs_work.route_schedule_profiles (
  profile_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_id UUID NOT NULL REFERENCES route_prod.routes(route_id) ON DELETE CASCADE,
  direction_id SMALLINT NOT NULL DEFAULT 0 CHECK (direction_id IN (0,1)),
  service_name TEXT NOT NULL,
  runtime_secs INT NOT NULL DEFAULT 3600,
  dwell_secs INT NOT NULL DEFAULT 20,
  shape_source TEXT NOT NULL DEFAULT 'route_prod_geom',
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(route_id, direction_id, service_name)
);

CREATE TABLE IF NOT EXISTS gtfs_work.service_windows (
  window_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id UUID NOT NULL REFERENCES gtfs_work.route_schedule_profiles(profile_id) ON DELETE CASCADE,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  headway_secs INT,
  exact_departures TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  monday BOOLEAN NOT NULL DEFAULT TRUE,
  tuesday BOOLEAN NOT NULL DEFAULT TRUE,
  wednesday BOOLEAN NOT NULL DEFAULT TRUE,
  thursday BOOLEAN NOT NULL DEFAULT TRUE,
  friday BOOLEAN NOT NULL DEFAULT TRUE,
  saturday BOOLEAN NOT NULL DEFAULT FALSE,
  sunday BOOLEAN NOT NULL DEFAULT FALSE,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (headway_secs IS NULL OR headway_secs > 0)
);

CREATE TABLE IF NOT EXISTS gtfs_work.calendar_exceptions (
  exception_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id UUID NOT NULL REFERENCES gtfs_work.route_schedule_profiles(profile_id) ON DELETE CASCADE,
  service_date DATE NOT NULL,
  exception_type SMALLINT NOT NULL CHECK (exception_type IN (1,2)),
  note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- Generated GTFS staging rows (editable before export)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gtfs_work.export_runs (
  export_run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  status TEXT NOT NULL DEFAULT 'draft',
  params JSONB NOT NULL DEFAULT '{}'::jsonb,
  summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  validator_report JSONB NOT NULL DEFAULT '{}'::jsonb,
  output_zip_path TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS gtfs_work.gtfs_agency (
  export_run_id UUID NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  agency_id TEXT NOT NULL,
  agency_name TEXT NOT NULL,
  agency_url TEXT NOT NULL,
  agency_timezone TEXT NOT NULL,
  agency_lang TEXT,
  PRIMARY KEY(export_run_id, agency_id)
);

CREATE TABLE IF NOT EXISTS gtfs_work.gtfs_stops (
  export_run_id UUID NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  stop_id TEXT NOT NULL,
  stop_name TEXT NOT NULL,
  stop_lat DOUBLE PRECISION NOT NULL,
  stop_lon DOUBLE PRECISION NOT NULL,
  location_type SMALLINT NOT NULL DEFAULT 0,
  parent_station TEXT,
  PRIMARY KEY(export_run_id, stop_id)
);

CREATE TABLE IF NOT EXISTS gtfs_work.gtfs_routes (
  export_run_id UUID NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  route_id TEXT NOT NULL,
  agency_id TEXT,
  route_short_name TEXT,
  route_long_name TEXT NOT NULL,
  route_type INT NOT NULL DEFAULT 3,
  route_color TEXT,
  route_text_color TEXT,
  PRIMARY KEY(export_run_id, route_id)
);

CREATE TABLE IF NOT EXISTS gtfs_work.gtfs_shapes (
  export_run_id UUID NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  shape_id TEXT NOT NULL,
  shape_pt_lat DOUBLE PRECISION NOT NULL,
  shape_pt_lon DOUBLE PRECISION NOT NULL,
  shape_pt_sequence INT NOT NULL,
  shape_dist_traveled DOUBLE PRECISION,
  PRIMARY KEY(export_run_id, shape_id, shape_pt_sequence)
);

CREATE TABLE IF NOT EXISTS gtfs_work.gtfs_calendar (
  export_run_id UUID NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  service_id TEXT NOT NULL,
  monday SMALLINT NOT NULL,
  tuesday SMALLINT NOT NULL,
  wednesday SMALLINT NOT NULL,
  thursday SMALLINT NOT NULL,
  friday SMALLINT NOT NULL,
  saturday SMALLINT NOT NULL,
  sunday SMALLINT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  PRIMARY KEY(export_run_id, service_id)
);

CREATE TABLE IF NOT EXISTS gtfs_work.gtfs_calendar_dates (
  export_run_id UUID NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  service_id TEXT NOT NULL,
  date TEXT NOT NULL,
  exception_type SMALLINT NOT NULL,
  PRIMARY KEY(export_run_id, service_id, date)
);

CREATE TABLE IF NOT EXISTS gtfs_work.gtfs_trips (
  export_run_id UUID NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  route_id TEXT NOT NULL,
  service_id TEXT NOT NULL,
  trip_id TEXT NOT NULL,
  trip_headsign TEXT,
  direction_id SMALLINT,
  shape_id TEXT,
  PRIMARY KEY(export_run_id, trip_id)
);

CREATE TABLE IF NOT EXISTS gtfs_work.gtfs_stop_times (
  export_run_id UUID NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  trip_id TEXT NOT NULL,
  arrival_time TEXT NOT NULL,
  departure_time TEXT NOT NULL,
  stop_id TEXT NOT NULL,
  stop_sequence INT NOT NULL,
  timepoint SMALLINT DEFAULT 1,
  PRIMARY KEY(export_run_id, trip_id, stop_sequence)
);

CREATE TABLE IF NOT EXISTS gtfs_work.gtfs_frequencies (
  export_run_id UUID NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  trip_id TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  headway_secs INT NOT NULL,
  exact_times SMALLINT NOT NULL DEFAULT 0,
  PRIMARY KEY(export_run_id, trip_id, start_time)
);

CREATE TABLE IF NOT EXISTS gtfs_work.gtfs_overrides (
  override_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  export_run_id UUID NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  table_name TEXT NOT NULL,
  row_key JSONB NOT NULL,
  patched_row JSONB NOT NULL,
  note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- Publish contract
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gtfs_prod.feed_versions (
  feed_version_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  export_run_id UUID UNIQUE NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE RESTRICT,
  gtfs_zip_path TEXT NOT NULL,
  validator_report JSONB NOT NULL DEFAULT '{}'::jsonb,
  published_by TEXT,
  published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_current BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_gtfs_work_route_schedule_profiles_route
  ON gtfs_work.route_schedule_profiles(route_id, direction_id, is_active);

CREATE INDEX IF NOT EXISTS idx_gtfs_work_service_windows_profile
  ON gtfs_work.service_windows(profile_id, is_active);

CREATE INDEX IF NOT EXISTS idx_gtfs_work_export_runs_created
  ON gtfs_work.export_runs(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_gtfs_prod_feed_versions_current
  ON gtfs_prod.feed_versions(is_current, published_at DESC);
