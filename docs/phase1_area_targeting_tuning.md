> **Note:** the area-group names used below (`valle_core`, `conocoto_corridor`, `quito_gateways`…) are those of the region the pipeline was developed for (Quito). Replace them with your own region's (see `datamind_console/common/nominatim_group_bias.json` and `phase1_nodes/catalogs/`).

# Phase 1 Area Targeting and Extractor Tuning (v1)

This document describes the deterministic Phase 1 extraction tuning flow added for Valle de los Chillos and corridor sectors.

## Files and modules

- Sector catalog (root-level config):
  - `phase1_sector_catalog.json`
- Sector matching and normalization:
  - `phase1_nodes/datamind/services/openmaps_extractor/src/pipeline/area_targeting.py`
- Action policy, bbox retry plan, scoring, and recommendation storage:
  - `phase1_nodes/datamind/services/openmaps_extractor/src/pipeline/extraction_policy.py`
- Phase 1 orchestration integration:
  - `datamind_console/phases/phase1_nodes/client.py`
- AI insights score integration + sector summary panel:
  - `datamind_console/ai_insights/scoring.py`
  - `datamind_console/ai_insights/service.py`
  - `datamind_console/views/insights_view.py`

## Sector catalog format

`phase1_sector_catalog.json` uses:

- `area_group`: one of `valle_core`, `conocoto_corridor`, `amaguana_axis`, `quito_gateways`
- `sector`: canonical name
- `aliases`: list of accentless/abbrev/name variants
- `priority`: lower = higher priority in tie-break matching
- `type`: one of `urban_center`, `corridor_node`, `terminal_zone`, `rural_axis`
- `bbox_suggestion`: optional bbox object (`south`, `west`, `north`, `east`)

You can override catalog path with:

- `DATAMIND_P1_SECTOR_CATALOG_JSON=/absolute/path/to/sector_catalog.json`

## How tuning works

The tuned runner (`Phase1Client.run_step_build_node_set_tuned`) does:

1. Match sector using deterministic text normalization + alias matching.
2. Resolve ordered actions from `area_group` policy.
3. For each action (2-4 max), run bbox retries with expansion buffers:
   - `0%`, `+10%`, `+25%`, `+50%` (configurable max retries)
4. For each attempt:
   - Build node set
   - Normalize, feature extraction, clustering, resolve, rank
   - Collect diagnostics and deterministic quality score
   - Log telemetry via `ai_insights` (`step_build_node_set_tuning_attempt`)
5. Choose best attempt by quality score.
6. Store recommendation in:
   - AI run logs (`step_build_node_set_tuning_recommendation` payload)
   - Lightweight file `data/phase1_sector_recommendations.json`

## Attempt diagnostics logged

Each tuning attempt includes:

- `raw_count`, `candidate_count`, `stop_count`, `poi_count`
- `stop_ratio`, `poi_ratio`
- `name_coverage`
- tag coverage ratios:
  - `tag_coverage_public_transport`
  - `tag_coverage_highway_bus_stop`
  - `tag_coverage_amenity_bus_station`
  - `tag_coverage_platform`
- clustering stats:
  - `n_clusters`, `noise_count`, `singletons`
- `resolved_count` proxy, `approved_count` proxy
- `spatial_spread_indicator` (center-distance based)

## Deterministic quality score

Scoring is deterministic and area-aware:

- Implemented in `extraction_policy.score_extraction_quality`
- Weighted components include:
  - candidate yield, stop balance, name coverage, tag coverage,
  - cluster quality, resolve rate, approved proxy, spatial spread
- Area group weight profiles:
  - `valle_core`, `conocoto_corridor`, `amaguana_axis`, `quito_gateways`

`ai_insights` Phase 1 quality scoring now uses this policy scorer as the primary path.

## UI usage

### Phase 1 Step 01 tab

- Optional `sector targeting` selector (from catalog)
- Optional sector bbox suggestion override
- `Run Area-Tuned Extraction` button runs action comparison + bbox retries

### Insights -> AI Bot / AI Insights -> Phase 1 Quality

New sector panel shows:

- best action template per sector
- best bbox buffer
- score trends
- recommended next extraction attempt
- latest stored recommendation configs

## Safety guarantees

- No DB migrations or contract changes
- No auto-promotion of nodes
- No destructive cleanup automation
- Recommendation + logging only; operator decides promotion/action
