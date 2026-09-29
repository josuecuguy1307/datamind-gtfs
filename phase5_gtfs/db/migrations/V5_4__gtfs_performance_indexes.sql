BEGIN;

-- Core route/trip lookup path
CREATE INDEX IF NOT EXISTS idx_gtfs_trips_export_route_trip
  ON gtfs_work.gtfs_trips (export_run_id, route_id, trip_id);

CREATE INDEX IF NOT EXISTS idx_gtfs_trips_export_route_shape
  ON gtfs_work.gtfs_trips (export_run_id, route_id, shape_id)
  WHERE shape_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_gtfs_trips_export_service
  ON gtfs_work.gtfs_trips (export_run_id, service_id);

-- stop_times joins + ordered previews
CREATE INDEX IF NOT EXISTS idx_gtfs_stop_times_export_trip_stop
  ON gtfs_work.gtfs_stop_times (export_run_id, trip_id, stop_sequence);

CREATE INDEX IF NOT EXISTS idx_gtfs_stop_times_export_stop
  ON gtfs_work.gtfs_stop_times (export_run_id, stop_id);

-- shape and stop filtered previews
CREATE INDEX IF NOT EXISTS idx_gtfs_shapes_export_shape_seq
  ON gtfs_work.gtfs_shapes (export_run_id, shape_id, shape_pt_sequence);

CREATE INDEX IF NOT EXISTS idx_gtfs_stops_export_stop_name
  ON gtfs_work.gtfs_stops (export_run_id, stop_name, stop_id);

-- Supporting tables often filtered by export_run_id + id
CREATE INDEX IF NOT EXISTS idx_gtfs_routes_export_route
  ON gtfs_work.gtfs_routes (export_run_id, route_id);

CREATE INDEX IF NOT EXISTS idx_gtfs_calendar_export_service
  ON gtfs_work.gtfs_calendar (export_run_id, service_id);

CREATE INDEX IF NOT EXISTS idx_gtfs_calendar_dates_export_service_date
  ON gtfs_work.gtfs_calendar_dates (export_run_id, service_id, date);

CREATE INDEX IF NOT EXISTS idx_gtfs_frequencies_export_trip_start
  ON gtfs_work.gtfs_frequencies (export_run_id, trip_id, start_time);

COMMIT;
