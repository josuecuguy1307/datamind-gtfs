-- Expand node_features with tag-based and spatial columns for ML

-- Tag-based features
ALTER TABLE node_work.node_features
    ADD COLUMN IF NOT EXISTS has_shelter boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS has_bench boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS has_route_ref boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS primary_tag_category text DEFAULT 'UNKNOWN',
    ADD COLUMN IF NOT EXISTS tag_richness integer DEFAULT 0;

-- Spatial features
ALTER TABLE node_work.node_features
    ADD COLUMN IF NOT EXISTS distance_to_nearest_road_m double precision,
    ADD COLUMN IF NOT EXISTS distance_to_nearest_stop_m double precision,
    ADD COLUMN IF NOT EXISTS nearby_stop_density_100m integer DEFAULT 0,
    ADD COLUMN IF NOT EXISTS nearby_poi_density_100m integer DEFAULT 0,
    ADD COLUMN IF NOT EXISTS road_type_nearest text,
    ADD COLUMN IF NOT EXISTS on_road_way boolean DEFAULT false;
