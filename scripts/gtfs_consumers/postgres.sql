-- gtfs-via-postgres 4.12.0; all dates are SERVICE dates unless noted.
-- Known upstream SRID loss: shapes_aggregated is SRID 0. The join below
-- explicitly restores the source WGS84 SRID; shape_srid records the defect.
WITH metrics AS (
  SELECT
    (SELECT count(*) FROM agency) AS agency,
    (SELECT count(*) FROM routes) AS routes,
    (SELECT count(*) FROM stops) AS stops,
    (SELECT count(*) FROM trips) AS trips,
    (SELECT count(*) FROM stop_times) AS stop_times,
    (SELECT count(*) FROM shapes) AS shape_points,
    (SELECT count(*) FROM shapes_aggregated) AS shapes,
    (SELECT min(ST_SRID(shape)) FROM shapes_aggregated) AS shape_srid,
    (SELECT min(ST_SRID(stop_loc::geometry)) FROM stops) AS stop_srid,
    (SELECT count(*) FROM calendar) AS calendar,
    (SELECT count(*) FROM calendar_dates) AS calendar_dates,
    (SELECT count(*) FROM feed_info) AS feed_info,
    (SELECT count(*) FROM service_days) AS service_days,
    (SELECT count(*) FROM arrivals_departures) AS arrivals_departures,
    (SELECT count(*) FROM connections) AS connections,
    (SELECT count(*) FROM stop_times st LEFT JOIN stops s USING(stop_id) LEFT JOIN trips t USING(trip_id)
      WHERE s.stop_id IS NULL OR t.trip_id IS NULL) AS orphan_stop_times,
    (SELECT count(*) FROM (SELECT 'MISSING' AS stop_id, 'day_0' AS trip_id UNION ALL SELECT stop_id,trip_id FROM stop_times) st
      LEFT JOIN stops s USING(stop_id) LEFT JOIN trips t USING(trip_id)
      WHERE s.stop_id IS NULL OR t.trip_id IS NULL) AS orphan_negative_control,
    (SELECT count(*) FROM trips t LEFT JOIN routes r USING(route_id)
      LEFT JOIN (SELECT DISTINCT service_id FROM service_days) d USING(service_id)
      LEFT JOIN shapes_aggregated s USING(shape_id)
      WHERE r.route_id IS NULL OR d.service_id IS NULL OR s.shape_id IS NULL) AS orphan_trips,
    (SELECT count(*) FROM routes r LEFT JOIN agency a USING(agency_id) WHERE a.agency_id IS NULL) AS orphan_routes,
    (SELECT count(*) FROM service_days WHERE date = DATE '2026-10-23') AS exception_service_days,
    (SELECT count(*) FROM arrivals_departures WHERE date = DATE '2026-10-23') AS exception_departures,
    (SELECT count(*) FROM arrivals_departures WHERE date = DATE '2026-10-24') AS next_service_departures,
    (SELECT count(*) FROM arrivals_departures WHERE date = DATE '2026-10-13') AS first_service_departures,
    (SELECT count(*) FROM arrivals_departures WHERE trip_id='night_1' AND stop_sequence=3
      AND date=DATE '2026-10-13' AND t_departure=TIMESTAMPTZ '2026-10-14 00:35:00-06') AS after_midnight_expanded,
    (SELECT count(*) FROM arrivals_departures WHERE t_departure >= TIMESTAMPTZ '2026-10-23 00:00:00-06'
      AND t_departure < TIMESTAMPTZ '2026-10-23 01:00:00-06') AS prior_service_on_exception_clock_date,
    (SELECT EXTRACT(EPOCH FROM max(departure_time)) FROM stop_times) AS max_departure_seconds,
    (SELECT max(departure_time)::text FROM stop_times) AS max_departure_interval,
    (SELECT count(*) FROM stops WHERE ST_DWithin(stop_loc, ST_SetSRID(ST_Point(-99.14,19.43),4326)::geography,500)) AS near_stops,
    (SELECT count(*) FROM stops WHERE ST_DWithin(stop_loc, ST_SetSRID(ST_Point(-99.2,19.5),4326)::geography,500)) AS far_stops,
    (SELECT ST_Distance(a.stop_loc,b.stop_loc) FROM stops a CROSS JOIN stops b WHERE a.stop_id='S1' AND b.stop_id='S2') AS adjacent_distance_m,
    (SELECT count(*) FROM shapes_aggregated sh JOIN stops s ON s.stop_id=CASE WHEN sh.shape_id='shape_0' THEN 'S1' ELSE 'S3' END
      WHERE ST_Equals(ST_StartPoint(ST_SetSRID(sh.shape,4326)),s.stop_loc::geometry)) AS correct_shape_starts,
    (SELECT count(*) FROM (SELECT trip_id, departure_time, lag(departure_time) OVER(PARTITION BY trip_id ORDER BY stop_sequence) AS previous FROM stop_times) x
      WHERE departure_time < previous) AS reversed_times
)
SELECT row_to_json(metrics)::text FROM metrics;
