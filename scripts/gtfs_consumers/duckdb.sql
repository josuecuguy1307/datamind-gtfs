-- gtfs-via-duckdb 5.0.1 aggregates 6 source shape points into 2 geometry rows.
-- ST_Distance_Spheroid takes (latitude, longitude); stored stop_loc is (lon, lat).
SELECT
  (SELECT count(*) FROM agency) AS agency,
  (SELECT count(*) FROM routes) AS routes,
  (SELECT count(*) FROM stops) AS stops,
  (SELECT count(*) FROM trips) AS trips,
  (SELECT count(*) FROM stop_times) AS stop_times,
  (SELECT sum(ST_NPoints(shape)) FROM shapes) AS shape_points,
  (SELECT count(*) FROM shapes) AS shapes,
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
    LEFT JOIN shapes s USING(shape_id)
    WHERE r.route_id IS NULL OR d.service_id IS NULL OR s.shape_id IS NULL) AS orphan_trips,
  (SELECT count(*) FROM routes r LEFT JOIN agency a USING(agency_id) WHERE a.agency_id IS NULL) AS orphan_routes,
  (SELECT count(*) FROM service_days WHERE date=DATE '2026-10-23') AS exception_service_days,
  (SELECT count(*) FROM arrivals_departures WHERE date=DATE '2026-10-23') AS exception_departures,
  (SELECT count(*) FROM arrivals_departures WHERE date=DATE '2026-10-24') AS next_service_departures,
  (SELECT count(*) FROM arrivals_departures WHERE date=DATE '2026-10-13') AS first_service_departures,
  (SELECT count(*) FROM arrivals_departures WHERE trip_id='night_1' AND stop_sequence=3
    AND date=DATE '2026-10-13' AND t_departure=TIMESTAMPTZ '2026-10-14 00:35:00-06') AS after_midnight_expanded,
  (SELECT count(*) FROM arrivals_departures WHERE t_departure >= TIMESTAMPTZ '2026-10-23 00:00:00-06'
    AND t_departure < TIMESTAMPTZ '2026-10-23 01:00:00-06') AS prior_service_on_exception_clock_date,
  (SELECT epoch(max(departure_time)) FROM stop_times) AS max_departure_seconds,
  (SELECT max(departure_time)::VARCHAR FROM stop_times) AS max_departure_interval,
  (SELECT count(*) FROM stops WHERE ST_Distance_Spheroid(ST_FlipCoordinates(stop_loc),ST_Point(19.43,-99.14))<=500) AS near_stops,
  (SELECT count(*) FROM stops WHERE ST_Distance_Spheroid(ST_FlipCoordinates(stop_loc),ST_Point(19.5,-99.2))<=500) AS far_stops,
  (SELECT ST_Distance_Spheroid(ST_FlipCoordinates(a.stop_loc),ST_FlipCoordinates(b.stop_loc))
    FROM stops a CROSS JOIN stops b WHERE a.stop_id='S1' AND b.stop_id='S2') AS adjacent_distance_m,
  (SELECT count(*) FROM shapes sh JOIN stops s ON s.stop_id=CASE WHEN sh.shape_id='shape_0' THEN 'S1' ELSE 'S3' END
    WHERE ST_Equals(ST_StartPoint(sh.shape),s.stop_loc)) AS correct_shape_starts,
  (SELECT count(*) FROM (SELECT trip_id, departure_time, lag(departure_time) OVER(PARTITION BY trip_id ORDER BY stop_sequence) AS previous FROM stop_times) x
    WHERE departure_time < previous) AS reversed_times;
