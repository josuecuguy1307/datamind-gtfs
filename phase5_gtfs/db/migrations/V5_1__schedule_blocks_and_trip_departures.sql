BEGIN;

-- Number of vehicle blocks used by a profile (round-robin assignment in Step 04).
ALTER TABLE gtfs_work.route_schedule_profiles
  ADD COLUMN IF NOT EXISTS n_blocks INT NOT NULL DEFAULT 1;

ALTER TABLE gtfs_work.route_schedule_profiles
  DROP CONSTRAINT IF EXISTS chk_route_schedule_profiles_n_blocks;

ALTER TABLE gtfs_work.route_schedule_profiles
  ADD CONSTRAINT chk_route_schedule_profiles_n_blocks
  CHECK (n_blocks >= 1);

-- Optional peak override at window level.
ALTER TABLE gtfs_work.service_windows
  ADD COLUMN IF NOT EXISTS is_peak BOOLEAN NOT NULL DEFAULT FALSE;

-- Internal departure-time map per generated trip (not exported to GTFS text files).
CREATE TABLE IF NOT EXISTS gtfs_work.trip_departures (
  export_run_id UUID NOT NULL REFERENCES gtfs_work.export_runs(export_run_id) ON DELETE CASCADE,
  trip_id TEXT NOT NULL,
  departure_time TEXT NOT NULL,
  PRIMARY KEY (export_run_id, trip_id)
);

CREATE INDEX IF NOT EXISTS idx_trip_departures_export
  ON gtfs_work.trip_departures(export_run_id);

-- GTFS optional field: block_id (safe in trips.txt).
ALTER TABLE gtfs_work.gtfs_trips
  ADD COLUMN IF NOT EXISTS block_id TEXT;

COMMIT;
