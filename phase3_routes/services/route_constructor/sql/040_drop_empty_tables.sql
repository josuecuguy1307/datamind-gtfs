-- Block 2: Drop confirmed-empty tables
-- Verified 2026-03-19: all four tables have exactly 0 rows.
-- Tables with data were preserved (inverse_direction_status, manual_sequence_exports,
-- route_approvals, service_route_approvals, delete_events).

BEGIN;

DROP TABLE IF EXISTS route_raw.unmatched_stop_points;
DROP TABLE IF EXISTS route_work.geometry_ranking_labels;
DROP TABLE IF EXISTS route_work.route_context_features;
DROP TABLE IF EXISTS route_work.valhalla_run_logs;

COMMIT;
