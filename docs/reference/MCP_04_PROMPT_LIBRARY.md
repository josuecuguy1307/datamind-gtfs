# Claude Code Prompt Library — Route Constructor

## Prompt Index

| Prompt | Purpose | Status |
|--------|---------|--------|
| **A** | Initial pipeline setup & catalog v5.0 | ✅ Completed |
| **B** | Bug fixes (BUG-010/012/014) + synthetic terminus | ✅ Completed |
| **C** | v5.2 full pipeline run (19 routes) | ✅ Completed |
| **C-v2** | v5.4 hybrid catalog run (39 routes) | ✅ Completed |
| **D** | v5.3→v5.4 catalog evolution (coord waypoints) | ✅ Completed |
| **E** | Sequence override architecture | ✅ Completed |
| **F** | Valhalla geometry production (standalone) | ⚠️ Superseded by G |
| **G** | Step 30 geometry via existing pipeline | ✅ Active |
| **H** | Sequence coherence validator | ✅ Active |

## Prompt E — Sequence Override Architecture

**File:** `PROMPT_E_SEQUENCE_OVERRIDE_ARCH.md`

Three action types in override JSON:
- `replace_sequence` — full replacement with keep/remove per stop
- `reorder` — same stops, different order
- `remove_stops` — remove by stop_id

Pipeline: auto-generation → POST-PROCESSING layer applies overrides → sequences preserved across re-runs.

## Prompt G — Step 30 Geometry via Pipeline

**File:** `PROMPT_G_STEP30_GEOMETRY.md`

Key principle: Do NOT create standalone Valhalla scripts. Use the existing Step 30 in the pipeline. Only change the INPUT source — override sequences from `valle_v5_4_FINAL_REVIEWED.json` instead of Step 20 auto-generated sequences.

## Prompt H — Coherence Validator

**File:** `PROMPT_H_COHERENCE_VALIDATOR.md`

Three-test validator that runs before Valhalla to catch sequence outliers:
1. Triangle detour test (cost ratio)
2. Direction reversal test (dot product)
3. Corridor flow test (running average direction)

Parameters vary by route type (lineal/internal/circular).

## Loop Routes Plan

**File:** Documented in session (not yet a separate prompt)

Full implementation plan for circular route support:
- **Phase 0:** Circularity audit (H1 terminus proximity + H2 semantics + H3 geography)
- **Phase 1:** Schema update (route_topology, loop_metadata)
- **Phase 2:** Pipeline branching (angular progress for circular, closed Valhalla routes)
- **Phase 3:** GTFS output (closed shapes, same stop at start+end of stop_times)

Current finding: **39/39 routes are linear.** Only 2 candidates for circular (El Colibrí-Loreto interno, Integrado Ontaneda) and even those are likely linear.

## HADES Automation Rule

HADES = automated trigger for Claude Code (Codex) patches when extractors fail. Part of the AI-integrated workflow for the DataMind pipeline.

## Key Files

| File | Description |
|------|-------------|
| `valle_v5_4_COHERENT_v17.json` | Latest: 39 routes, reviewed sequences, with geometries |
| `valle_v5_4_FINAL_REVIEWED.json` | Sequences-only (no geometries), manually reviewed |
| `valle_de_los_chillos_hints_catalog_v5_4.json` | Seed catalog for the pipeline |
| `sequence_overrides_v1.json` | Override JSON (24 routes with manual corrections) |
| `_ALL_ROUTES_GEOMETRY.geojson` | Exported geometries for all 39 routes |
