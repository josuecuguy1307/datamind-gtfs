-- 011_node_work_views.sql
-- View: raw -> normalized point + tag_kind (Phase 1 normalize step reads this)

BEGIN;

CREATE OR REPLACE VIEW node_work.v_raw_to_node_candidate AS
SELECT
  e.run_id AS source_run_id,
  e.osm_type,
  e.osm_id,

  COALESCE(
    e.geom,
    CASE
      WHEN e.lon IS NOT NULL AND e.lat IS NOT NULL
        THEN ST_SetSRID(ST_MakePoint(e.lon, e.lat), 4326)
      WHEN e.center_lon IS NOT NULL AND e.center_lat IS NOT NULL
        THEN ST_SetSRID(ST_MakePoint(e.center_lon, e.center_lat), 4326)
      ELSE NULL
    END
  ) AS geom,

  e.tags,

  CASE
    WHEN e.tags ? 'highway' AND e.tags->>'highway' = 'bus_stop' THEN 'bus_stop'
    WHEN e.tags ? 'public_transport' AND e.tags->>'public_transport' = 'platform' THEN 'platform'
    WHEN e.tags ? 'public_transport' AND e.tags->>'public_transport' = 'stop_position' THEN 'stop_position'
    WHEN e.tags ? 'amenity' AND e.tags->>'amenity' = 'bus_station' THEN 'station'
    WHEN e.tags ? 'public_transport' AND e.tags->>'public_transport' = 'station' THEN 'station'
    WHEN e.tags ? 'railway' AND e.tags->>'railway' = 'tram_stop' THEN 'tram_stop'
    WHEN e.tags ? 'railway' AND e.tags->>'railway' IN ('station', 'halt') THEN 'station'
    WHEN e.tags ?| ARRAY['amenity', 'shop', 'tourism', 'office', 'leisure'] THEN 'poi'
    ELSE 'other'
  END AS tag_kind

FROM node_raw.overpass_elements e;

COMMIT;
