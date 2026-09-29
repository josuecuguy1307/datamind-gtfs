-- 013_path_aware_synthesis.sql
-- Adds path-aware synthesis infrastructure to node_prod and route_prod.
-- See workspace/skills/hades-path-aware-synthesis.md         (synthesis stages 3a-3d, stage 4)
-- See workspace/skills/hades-osm-route-node-fill.md          (OSM-relation polyline gap filling)
-- See workspace/skills/03_PHASE3_ROUTES_SKILL.md §A2         (synthesis fallback wiring)
-- See workspace/skills/07_INGESTION_CONTRACT.md §4b          (synthetic-node confidence merge)
-- See workspace/skills/hades-quality-gate/SKILL.md Soft/Hard (synthesis gate rules)
--
-- Guardrails:
--   * Purely additive. No DROP COLUMN, no ALTER...DROP, no DELETE, no UPDATE of existing rows.
--   * Existing `source = 'osm'` and `source = 'backfill'` rows are preserved unchanged.
--   * Idempotent: ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS, CREATE TABLE IF NOT EXISTS,
--     CREATE SEQUENCE IF NOT EXISTS. Safe to re-run.
--
-- Rollback procedure (only if migration must be undone before any synthetic data is written):
--   BEGIN;
--     -- Drop added objects in reverse dependency order.
--     DROP INDEX IF EXISTS node_prod.idx_synthesis_events_unit_stage;
--     DROP TABLE IF EXISTS node_prod.synthesis_events;
--     DROP INDEX IF EXISTS node_prod.idx_nodes_synthetic_by_coords;
--     DROP INDEX IF EXISTS node_prod.idx_nodes_synthetic_pending;
--     -- Drop routes columns (only safe if no rows populated them yet).
--     ALTER TABLE route_prod.routes
--       DROP COLUMN IF EXISTS inferred_path_source,
--       DROP COLUMN IF EXISTS inferred_path_computed_at,
--       DROP COLUMN IF EXISTS inferred_path_polyline;
--     -- Drop node columns (only safe if no rows populated them yet).
--     ALTER TABLE node_prod.nodes
--       DROP COLUMN IF EXISTS osm_route_fill_context,
--       DROP COLUMN IF EXISTS semantic_spatial_conflict,
--       DROP COLUMN IF EXISTS research_to_projection_distance_m,
--       DROP COLUMN IF EXISTS path_projection_distance_m,
--       DROP COLUMN IF EXISTS poi_to_path_distance_m,
--       DROP COLUMN IF EXISTS poi_anchor_access_point,
--       DROP COLUMN IF EXISTS poi_anchor_class,
--       DROP COLUMN IF EXISTS poi_anchor_osm_id,
--       DROP COLUMN IF EXISTS superseded_by,
--       DROP COLUMN IF EXISTS synthetic_review_state,
--       DROP COLUMN IF EXISTS synthetic_confidence,
--       DROP COLUMN IF EXISTS synthetic_created_by,
--       DROP COLUMN IF EXISTS synthetic_created_at;
--     -- Restore prior source CHECK (only if still the 2-value flavor). If you don't know
--     -- the prior constraint body verbatim, leave the expanded 8-value CHECK in place -- it
--     -- is backwards-compatible: every existing 'osm'/'backfill' row passes.
--     ALTER TABLE node_prod.nodes DROP CONSTRAINT IF EXISTS nodes_source_check;
--     ALTER TABLE node_prod.nodes
--       ADD CONSTRAINT nodes_source_check CHECK (source IN ('osm','backfill'));
--     DROP SEQUENCE IF EXISTS node_prod.synthetic_osm_id_seq;
--   COMMIT;
--
-- Verification queries (run AFTER COMMIT to confirm a clean apply):
--   SELECT COUNT(*) FROM node_prod.nodes WHERE source IS NULL;        -- expect 0
--   SELECT COUNT(*) FROM node_prod.synthesis_events;                  -- expect 0 initially
--   SELECT nextval('node_prod.synthetic_osm_id_seq');                 -- expect -1000000 (then -1000001...)
--     -- NOTE: nextval consumes the value. To peek without consuming:
--     -- SELECT last_value, is_called FROM node_prod.synthetic_osm_id_seq;  -- is_called=false before first nextval
--   SELECT conname, pg_get_constraintdef(oid)
--     FROM pg_constraint WHERE conname = 'nodes_source_check';        -- expect the 8-value CHECK body


BEGIN;

------------------------------------------------------------------------
-- 1. node_prod.nodes  — expand source enum + synthetic/POI provenance
------------------------------------------------------------------------

-- Expand the allowed `source` values from {'osm','backfill'} to the full
-- 8-value synthesis taxonomy. Existing rows remain valid because 'osm' and
-- 'backfill' are both still accepted.
ALTER TABLE node_prod.nodes
  DROP CONSTRAINT IF EXISTS nodes_source_check;

ALTER TABLE node_prod.nodes
  ADD CONSTRAINT nodes_source_check CHECK (source IN (
    'osm',
    'backfill',
    'poi_anchored_path_projected',
    'path_corridor_projected',
    'path_intersection',
    'research_coords_path_snapped',
    'pure_synthesis',
    'gps_trace'
  ));

-- Synthetic-node provenance columns. All nullable -- only populated for rows
-- whose `source` is one of the five synthesis values (or 'gps_trace'). OSM
-- and backfill rows leave these NULL.
ALTER TABLE node_prod.nodes
  ADD COLUMN IF NOT EXISTS synthetic_created_at             TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS synthetic_created_by             TEXT,
  ADD COLUMN IF NOT EXISTS synthetic_confidence             TEXT
    CHECK (synthetic_confidence IS NULL OR synthetic_confidence IN ('low','medium','high')),
  ADD COLUMN IF NOT EXISTS synthetic_review_state           TEXT
    CHECK (synthetic_review_state IS NULL OR synthetic_review_state IN ('pending','verified','rejected')),
  ADD COLUMN IF NOT EXISTS superseded_by                    BIGINT REFERENCES node_prod.nodes(id),
  ADD COLUMN IF NOT EXISTS poi_anchor_osm_id                BIGINT,
  ADD COLUMN IF NOT EXISTS poi_anchor_class                 TEXT,
  ADD COLUMN IF NOT EXISTS poi_anchor_access_point          TEXT
    CHECK (poi_anchor_access_point IS NULL OR poi_anchor_access_point IN ('entrance_node','road_projection','centroid_fallback')),
  ADD COLUMN IF NOT EXISTS poi_to_path_distance_m           REAL,
  ADD COLUMN IF NOT EXISTS path_projection_distance_m       REAL,
  ADD COLUMN IF NOT EXISTS research_to_projection_distance_m REAL,
  ADD COLUMN IF NOT EXISTS semantic_spatial_conflict        BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS osm_route_fill_context           TEXT;

COMMENT ON COLUMN node_prod.nodes.synthetic_created_at IS
  'Timestamp the synthetic node was written by hades-path-aware-synthesis. NULL for osm/backfill rows.';
COMMENT ON COLUMN node_prod.nodes.synthetic_created_by IS
  'Skill or operator name that produced the synthetic node (e.g. "hades-path-aware-synthesis:3a_poi_on_path").';
COMMENT ON COLUMN node_prod.nodes.synthetic_confidence IS
  'Initial synthesis confidence per hades-path-aware-synthesis §7 tier mapping. Promotion updates this.';
COMMENT ON COLUMN node_prod.nodes.synthetic_review_state IS
  'pending = awaits operator review, verified = operator approved, rejected = operator rejected (row retained; FK nulled).';
COMMENT ON COLUMN node_prod.nodes.superseded_by IS
  'Set when a canonical OSM node (or higher-confidence synthetic) replaces this row in the route stop sequence. Row is retained for audit; no DELETE.';
COMMENT ON COLUMN node_prod.nodes.poi_anchor_osm_id IS
  'OSM element id of the POI used as anchor (positive for real OSM element; NULL for stages that did not use a POI anchor).';
COMMENT ON COLUMN node_prod.nodes.poi_anchor_class IS
  'OSM class of the POI anchor (e.g. amenity=marketplace, shop=supermarket, amenity=place_of_worship).';
COMMENT ON COLUMN node_prod.nodes.poi_anchor_access_point IS
  'entrance_node = used explicit entrance=yes; road_projection = nearest-road projection of POI centroid; centroid_fallback = POI centroid used directly (only for point POIs).';
COMMENT ON COLUMN node_prod.nodes.poi_to_path_distance_m IS
  'Haversine distance (metres) from POI anchor centroid to the route inferred_path_polyline. Hard ceiling 80m per §5 stage 3a.';
COMMENT ON COLUMN node_prod.nodes.path_projection_distance_m IS
  'Distance (metres) between the raw POI/research coord and its final projection onto the inferred path.';
COMMENT ON COLUMN node_prod.nodes.research_to_projection_distance_m IS
  'Distance (metres) between Deep Research provided coords and the final snapped position. Stage 3d: <=30m full-confidence, 30-80m low-confidence, >80m fail.';
COMMENT ON COLUMN node_prod.nodes.semantic_spatial_conflict IS
  'TRUE when POI inference and path inference produced divergent positions (> 80m apart, or contradictory road). Routed to synthetic_review/semantic_spatial_conflicts/.';
COMMENT ON COLUMN node_prod.nodes.osm_route_fill_context IS
  'For osm_route_fill-origin synthetics: "relation_<osm_id>_gap_<idx>". NULL for other synthesis stages.';

-- Synthetic OSM IDs use the OSM convention of negative integers for client-
-- synthesized features. Sequence starts at -1000000 and counts *down*, so
-- the value is always < 0 and has magnitude well above any realistic real
-- OSM element id (positive; currently ~1.2e10 and growing).
CREATE SEQUENCE IF NOT EXISTS node_prod.synthetic_osm_id_seq
  AS BIGINT
  START -1000000
  INCREMENT -1
  MINVALUE -9223372036854775807
  MAXVALUE -1000000
  CACHE 100;

COMMENT ON SEQUENCE node_prod.synthetic_osm_id_seq IS
  'Negative bigint sequence for synthetic osm_id. Callers: SELECT nextval(''node_prod.synthetic_osm_id_seq'').';

-- Hot path: pending review queue + promotion checks. Partial so it stays
-- tiny (only pending-review synthetics).
CREATE INDEX IF NOT EXISTS idx_nodes_synthetic_pending
  ON node_prod.nodes (source, synthetic_review_state)
  WHERE source IN (
    'poi_anchored_path_projected',
    'path_corridor_projected',
    'path_intersection',
    'research_coords_path_snapped',
    'pure_synthesis'
  ) AND synthetic_review_state = 'pending';

-- Spatial index for confidence promotion (multi-route confirmation check:
-- "does an OSM bus_stop exist within 15m of this synthetic?"). Also partial
-- to stay small.
CREATE INDEX IF NOT EXISTS idx_nodes_synthetic_by_coords
  ON node_prod.nodes USING gist (geom)
  WHERE source IN (
    'poi_anchored_path_projected',
    'path_corridor_projected',
    'path_intersection',
    'research_coords_path_snapped',
    'pure_synthesis'
  );

------------------------------------------------------------------------
-- 2. route_prod.routes  — cached inferred path polyline
------------------------------------------------------------------------

-- Caching the inferred path on the route itself avoids re-calling Valhalla
-- on every synthesis event. Invalidated (set NULL) when new anchors are
-- grounded; see hades-path-aware-synthesis §4.
ALTER TABLE route_prod.routes
  ADD COLUMN IF NOT EXISTS inferred_path_polyline    TEXT,
  ADD COLUMN IF NOT EXISTS inferred_path_computed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS inferred_path_source      TEXT
    CHECK (inferred_path_source IS NULL OR inferred_path_source IN (
      'valhalla_terminus_only',
      'valhalla_with_anchors',
      'osm_relation_geometry'
    ));

COMMENT ON COLUMN route_prod.routes.inferred_path_polyline IS
  'Encoded polyline (Google polyline6 format) of the route path used for synthesis projections. NULL = not computed yet or invalidated.';
COMMENT ON COLUMN route_prod.routes.inferred_path_computed_at IS
  'When the cached polyline was computed. Invalidate and recompute when new anchors ground, or after 7 days.';
COMMENT ON COLUMN route_prod.routes.inferred_path_source IS
  'valhalla_terminus_only = only termini fed to Valhalla; valhalla_with_anchors = termini + grounded stops + must_pass_through centroids; osm_relation_geometry = straight from the OSM relation.';

------------------------------------------------------------------------
-- 3. node_prod.synthesis_events  — append-only audit log
------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS node_prod.synthesis_events (
  id                    BIGSERIAL PRIMARY KEY,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  node_id               BIGINT REFERENCES node_prod.nodes(id) ON DELETE SET NULL,
  route_id              BIGINT REFERENCES route_prod.routes(id),
  unit                  TEXT NOT NULL,
  province              TEXT NOT NULL,
  stage                 TEXT NOT NULL CHECK (stage IN (
    '3a_poi_on_path',
    '3b_path_corridor',
    '3c_path_intersection',
    '3d_research_coords_snapped',
    '4_pure_synthesis',
    'osm_route_fill'
  )),
  anchor_name           TEXT,
  research_coords_lat   REAL,
  research_coords_lon   REAL,
  final_coords_lat      REAL,
  final_coords_lon      REAL,
  match_score           REAL,
  rejected_reason       TEXT,
  triggered_by_skill    TEXT,
  research_output_file  TEXT
);

COMMENT ON TABLE  node_prod.synthesis_events IS
  'Append-only audit log of every synthesis attempt (successful or rejected). Retention: indefinite. Never UPDATE, never DELETE.';
COMMENT ON COLUMN node_prod.synthesis_events.node_id IS
  'FK to the created synthetic node. NULL if the attempt was rejected (no node created) or if the node was later hard-deleted (preserved for audit).';
COMMENT ON COLUMN node_prod.synthesis_events.stage IS
  'Which pipeline stage produced this event. See hades-path-aware-synthesis §5 for 3a-3d + stage 4, and hades-osm-route-node-fill for osm_route_fill.';
COMMENT ON COLUMN node_prod.synthesis_events.research_output_file IS
  'Filename under workspace/research_queue/responses/ or ingested/ that triggered this synthesis (foreign-key-by-filename per 07_INGESTION_CONTRACT).';

CREATE INDEX IF NOT EXISTS idx_synthesis_events_unit_stage
  ON node_prod.synthesis_events (unit, stage, created_at DESC);

COMMIT;

-- End of migration 013_path_aware_synthesis.sql
