-- 013_path_aware_synthesis.sql  (rewritten — DO NOT APPLY until operator review)
-- Adds path-aware synthesis infrastructure to node_prod and route_prod.
-- See workspace/skills/hades-path-aware-synthesis.md         (synthesis stages 3a-3d, stage 4)
-- See workspace/skills/hades-osm-route-node-fill.md          (OSM-relation polyline gap filling)
-- See workspace/skills/03_PHASE3_ROUTES_SKILL.md §A2         (synthesis fallback wiring)
-- See workspace/skills/07_INGESTION_CONTRACT.md §4b          (synthetic-node confidence merge)
-- See workspace/skills/hades-quality-gate/SKILL.md Soft/Hard (synthesis gate rules)
--
-- Differences vs. the first draft (which failed on apply on 2026-04-19):
--
--   1. Existing node_prod.nodes.source column is LEFT ALONE. No CHECK change.
--      It remains free-text and keeps every legacy value ('work_review',
--      'review_request', 'overpass_*', 'synthesized_from_shape/<uuid>',
--      'deep_research_*', 'synth_06a', 'manual_samborondon_trolley', etc).
--      That column is now officially the "human-readable audit string".
--
--   2. A NEW column source_type TEXT is added alongside. It is nullable and
--      constrained to the 8-value enum (osm, backfill, + 6 synthesis values).
--      All legacy rows get source_type = NULL. All future synthesis-stage
--      writes populate BOTH source (audit string, e.g. "poi_anchored_path_
--      projected:route_CAL-03:anchor_Y_de_Calsig") AND source_type (enum value).
--
--   3. FKs on node_prod.synthesis_events.{node_id,route_id} and
--      node_prod.nodes.superseded_by are UUID, not BIGINT. They reference
--      node_prod.nodes(node_id) and route_prod.routes(route_id). Confirmed
--      against live schema on 2026-04-19.
--
--   4. A NEW column osm_id BIGINT (nullable) is added to node_prod.nodes.
--      Legacy rows leave it NULL (OSM provenance currently lives indirectly
--      through chosen_candidate_id → node_work.node_candidates.osm_id).
--      Synthesis-stage writes populate osm_id from synthetic_osm_id_seq
--      (always < -1,000,000). This enables the quality-gate hard rule
--      `synthetic_osm_id_in_positive_range` to be a trivial SQL check:
--          WHERE source_type IN (<synthesis values>) AND osm_id > 0
--      Without this column, the hard rule would have to dig into chosen_tags
--      JSONB. Adding the column is purely additive.
--
--   5. Partial indexes + quality-gate queries all filter on source_type, not
--      source. This keeps the index tight (only true synthesis rows) and
--      means adding a legacy source value later never pollutes the index.
--
-- Guardrails (unchanged from first draft):
--   * Purely additive. No DROP COLUMN, no ALTER...DROP, no DELETE,
--     no UPDATE of existing rows. `source` column untouched.
--   * Existing rows (all 13k of them, regardless of legacy source value)
--     are preserved unchanged and remain valid.
--   * Idempotent: ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS,
--     CREATE TABLE IF NOT EXISTS, CREATE SEQUENCE IF NOT EXISTS. Safe to re-run.
--
-- Rollback procedure (only if migration must be undone before any synthesis
-- writes land):
--   BEGIN;
--     DROP INDEX IF EXISTS node_prod.idx_synthesis_events_unit_stage;
--     DROP TABLE IF EXISTS node_prod.synthesis_events;
--     DROP INDEX IF EXISTS node_prod.idx_nodes_osm_id_notnull;
--     DROP INDEX IF EXISTS node_prod.idx_nodes_synthetic_by_coords;
--     DROP INDEX IF EXISTS node_prod.idx_nodes_synthetic_pending;
--     ALTER TABLE route_prod.routes
--       DROP COLUMN IF EXISTS inferred_path_source,
--       DROP COLUMN IF EXISTS inferred_path_computed_at,
--       DROP COLUMN IF EXISTS inferred_path_polyline;
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
--       DROP COLUMN IF EXISTS synthetic_created_at,
--       DROP COLUMN IF EXISTS osm_id,
--       DROP COLUMN IF EXISTS source_type;
--     -- NOTE: `source` column is left as-is on rollback; this migration
--     -- never touched it.
--     DROP SEQUENCE IF EXISTS node_prod.synthetic_osm_id_seq;
--   COMMIT;
--
-- Verification queries (run AFTER COMMIT to confirm a clean apply):
--   SELECT COUNT(*) FROM node_prod.nodes WHERE source IS NULL;           -- expect 0 (untouched)
--   SELECT COUNT(*) FROM node_prod.nodes WHERE source_type IS NOT NULL;  -- expect 0 initially
--   SELECT COUNT(*) FROM node_prod.synthesis_events;                     -- expect 0 initially
--   SELECT last_value, is_called FROM node_prod.synthetic_osm_id_seq;    -- expect (-1000000, false)
--   SELECT nextval('node_prod.synthetic_osm_id_seq');                    -- expect -1000000; then reset:
--   SELECT setval('node_prod.synthetic_osm_id_seq', -999999);            -- next real alloc = -1000000
--   SELECT conname, pg_get_constraintdef(oid)
--     FROM pg_constraint WHERE conname = 'nodes_source_type_check';      -- expect the 8-value CHECK body


BEGIN;

------------------------------------------------------------------------
-- 1. node_prod.nodes — add source_type, osm_id, and synthetic provenance
------------------------------------------------------------------------

-- A: New columns. All nullable. Legacy rows leave them NULL.
-- Note: we do NOT touch the existing `source` column. It keeps its
-- free-text semantics and its NOT NULL + default 'work_review' behavior.

ALTER TABLE node_prod.nodes
  ADD COLUMN IF NOT EXISTS source_type                       TEXT,
  ADD COLUMN IF NOT EXISTS osm_id                            BIGINT,
  ADD COLUMN IF NOT EXISTS synthetic_created_at              TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS synthetic_created_by              TEXT,
  ADD COLUMN IF NOT EXISTS synthetic_confidence              TEXT,
  ADD COLUMN IF NOT EXISTS synthetic_review_state            TEXT,
  ADD COLUMN IF NOT EXISTS superseded_by                     UUID,
  ADD COLUMN IF NOT EXISTS poi_anchor_osm_id                 BIGINT,
  ADD COLUMN IF NOT EXISTS poi_anchor_class                  TEXT,
  ADD COLUMN IF NOT EXISTS poi_anchor_access_point           TEXT,
  ADD COLUMN IF NOT EXISTS poi_to_path_distance_m            REAL,
  ADD COLUMN IF NOT EXISTS path_projection_distance_m        REAL,
  ADD COLUMN IF NOT EXISTS research_to_projection_distance_m REAL,
  ADD COLUMN IF NOT EXISTS semantic_spatial_conflict         BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS osm_route_fill_context            TEXT;

-- B: Add CHECK constraints. Because ADD CONSTRAINT doesn't support
-- IF NOT EXISTS in Postgres 17, we guard with a DO block. Each constraint
-- is checked for existence before being added, making the block idempotent.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'nodes_source_type_check'
      AND conrelid = 'node_prod.nodes'::regclass
  ) THEN
    ALTER TABLE node_prod.nodes
      ADD CONSTRAINT nodes_source_type_check CHECK (
        source_type IS NULL OR source_type IN (
          'osm',
          'backfill',
          'poi_anchored_path_projected',
          'path_corridor_projected',
          'path_intersection',
          'research_coords_path_snapped',
          'pure_synthesis',
          'gps_trace'
        )
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'nodes_synthetic_confidence_check'
      AND conrelid = 'node_prod.nodes'::regclass
  ) THEN
    ALTER TABLE node_prod.nodes
      ADD CONSTRAINT nodes_synthetic_confidence_check CHECK (
        synthetic_confidence IS NULL
        OR synthetic_confidence IN ('low','medium','high')
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'nodes_synthetic_review_state_check'
      AND conrelid = 'node_prod.nodes'::regclass
  ) THEN
    ALTER TABLE node_prod.nodes
      ADD CONSTRAINT nodes_synthetic_review_state_check CHECK (
        synthetic_review_state IS NULL
        OR synthetic_review_state IN ('pending','verified','rejected')
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'nodes_poi_anchor_access_point_check'
      AND conrelid = 'node_prod.nodes'::regclass
  ) THEN
    ALTER TABLE node_prod.nodes
      ADD CONSTRAINT nodes_poi_anchor_access_point_check CHECK (
        poi_anchor_access_point IS NULL
        OR poi_anchor_access_point IN ('entrance_node','road_projection','centroid_fallback')
      );
  END IF;

  -- superseded_by FK is defined via ALTER TABLE ADD CONSTRAINT because
  -- ADD COLUMN IF NOT EXISTS doesn't support inline REFERENCES on re-run.
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'nodes_superseded_by_fkey'
      AND conrelid = 'node_prod.nodes'::regclass
  ) THEN
    ALTER TABLE node_prod.nodes
      ADD CONSTRAINT nodes_superseded_by_fkey
      FOREIGN KEY (superseded_by) REFERENCES node_prod.nodes(node_id)
      ON DELETE SET NULL;
  END IF;
END $$;

-- C: Column comments (idempotent — COMMENT overwrites).
COMMENT ON COLUMN node_prod.nodes.source_type IS
  '8-value enum capturing the provenance class: osm | backfill | <6 synthesis values>. Distinct from the free-text `source` column, which carries a human-readable audit string (e.g. "poi_anchored_path_projected:route_CAL-03:anchor_Y_de_Calsig"). Legacy rows have source_type = NULL.';
COMMENT ON COLUMN node_prod.nodes.osm_id IS
  'OSM element id. Positive for real OSM elements; negative (from synthetic_osm_id_seq) for synthesis-stage rows. Legacy rows have osm_id = NULL — the indirect link via chosen_candidate_id remains.';
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

-- D: Negative sequence for synthetic osm_id values. Starts at -1000000 and
-- counts DOWN (INCREMENT -1). Magnitude well above any realistic real OSM
-- element id (positive; currently ~1.2e10).

CREATE SEQUENCE IF NOT EXISTS node_prod.synthetic_osm_id_seq
  AS BIGINT
  START -1000000
  INCREMENT -1
  MINVALUE -9223372036854775807
  MAXVALUE -1000000
  CACHE 100;

COMMENT ON SEQUENCE node_prod.synthetic_osm_id_seq IS
  'Negative bigint sequence for synthetic osm_id. Callers: SELECT nextval(''node_prod.synthetic_osm_id_seq''). First real allocation returns -1000000.';

-- E: Partial indexes, keyed on source_type (the enum column), so they stay
-- tiny (only true synthesis rows) even as free-text `source` legacy values
-- grow.

CREATE INDEX IF NOT EXISTS idx_nodes_synthetic_pending
  ON node_prod.nodes (source_type, synthetic_review_state)
  WHERE source_type IN (
    'poi_anchored_path_projected',
    'path_corridor_projected',
    'path_intersection',
    'research_coords_path_snapped',
    'pure_synthesis'
  ) AND synthetic_review_state = 'pending';

CREATE INDEX IF NOT EXISTS idx_nodes_synthetic_by_coords
  ON node_prod.nodes USING gist (geom)
  WHERE source_type IN (
    'poi_anchored_path_projected',
    'path_corridor_projected',
    'path_intersection',
    'research_coords_path_snapped',
    'pure_synthesis'
  );

-- Partial index on osm_id — only indexes rows that have an id set (almost
-- exclusively synthesis-stage rows in the near term, since legacy rows have
-- osm_id NULL). Supports the quality-gate hard rule
-- `synthetic_osm_id_in_positive_range` and fast lookup by synthetic id.
CREATE INDEX IF NOT EXISTS idx_nodes_osm_id_notnull
  ON node_prod.nodes (osm_id)
  WHERE osm_id IS NOT NULL;

------------------------------------------------------------------------
-- 2. route_prod.routes — cached inferred path polyline
------------------------------------------------------------------------

ALTER TABLE route_prod.routes
  ADD COLUMN IF NOT EXISTS inferred_path_polyline    TEXT,
  ADD COLUMN IF NOT EXISTS inferred_path_computed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS inferred_path_source      TEXT;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'routes_inferred_path_source_check'
      AND conrelid = 'route_prod.routes'::regclass
  ) THEN
    ALTER TABLE route_prod.routes
      ADD CONSTRAINT routes_inferred_path_source_check CHECK (
        inferred_path_source IS NULL OR inferred_path_source IN (
          'valhalla_terminus_only',
          'valhalla_with_anchors',
          'osm_relation_geometry'
        )
      );
  END IF;
END $$;

COMMENT ON COLUMN route_prod.routes.inferred_path_polyline IS
  'Encoded polyline (Google polyline6 format) of the route path used for synthesis projections. NULL = not computed yet or invalidated.';
COMMENT ON COLUMN route_prod.routes.inferred_path_computed_at IS
  'When the cached polyline was computed. Invalidate and recompute when new anchors ground, or after 7 days.';
COMMENT ON COLUMN route_prod.routes.inferred_path_source IS
  'valhalla_terminus_only = only termini fed to Valhalla; valhalla_with_anchors = termini + grounded stops + must_pass_through centroids; osm_relation_geometry = straight from the OSM relation.';

------------------------------------------------------------------------
-- 3. node_prod.synthesis_events — append-only audit log
------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS node_prod.synthesis_events (
  id                    BIGSERIAL PRIMARY KEY,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  node_id               UUID REFERENCES node_prod.nodes(node_id)  ON DELETE SET NULL,
  route_id              UUID REFERENCES route_prod.routes(route_id) ON DELETE SET NULL,
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

-- End of migration 013_path_aware_synthesis.sql (rewritten)
