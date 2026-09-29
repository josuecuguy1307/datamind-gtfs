# Route Constructor Pipeline — Architecture & Workflow

## Stack
- **Frontend:** Streamlit dashboard
- **Backend:** Python
- **DB:** PostgreSQL + PostGIS (single DB, 3 schemas: raw/work/prod)
- **Routing engine:** Valhalla (localhost:8002)
- **ML:** LightGBM (stop candidate scoring)
- **Search:** OpenSearch (stop name fuzzy matching)

## Pipeline Steps (Phase 3)

```
Step 05 — Discover: Find route_id candidates for a service_route
Step 10 — Fetch: Pull OSM/GTFS data for the route corridor
Step 15 — Inverse: Complete missing direction data
Step 20 — Sequences: Build ordered stop sequences from candidates
Step 30 — Geometry: Build geometry candidates via Valhalla ← CURRENT FOCUS
Step 32 — Stop Recovery: Find stops missed by initial sequence
Step 35 — Rank: Score and rank geometry/sequence candidates
Step 40 — Approve: Human review and approval
```

## Seed Catalog

The seed catalog (`valle_de_los_chillos_hints_catalog_v5_4.json`) contains 39 routes with:
- `cooperative`: operator name
- `route`: route name  
- `sequence_seed`: ordered array of waypoint names
- `explicit_anchors`: terminus coordinates and intermediate anchors
- `researched_anchors`: verified geographic points

### Catalog Version History
- **v5.2:** 19 routes, text-based waypoints
- **v5.3:** 39 routes, added coordinate waypoints (caused regressions in rural routes)
- **v5.4:** Hybrid best-of v5.2+v5.3: kept coords where they helped, reverted where they hurt, stripped to terminus-only for routes where Valhalla finds the only road naturally

## Sequence Override System (Prompt E)

Post-processing layer that applies manual corrections to auto-generated sequences.

### Override Actions
- `replace_sequence`: Full replacement with explicit keep/remove per stop
- `reorder`: Same stops, different order
- `remove_stops`: Remove specific stops by stop_id

## Sequence Coherence Validator (Prompt H)

Runs BEFORE Valhalla geometry generation to catch stops that break the corridor pattern.

### Three Tests
1. **Triangle detour test:** If going prev→curr→next costs >2.5x more than prev→next, the stop is an outlier
2. **Direction reversal test:** If the bus direction reverses at a stop (dot product negative) with significant distances, it's a backtrack
3. **Corridor flow test:** If a stop goes against the running average direction of the last 3 accepted stops, it's off-corridor

## Valhalla Geometry Production (Prompts F/G)

### Linear Routes (37 routes)
- First stop = `break` point, Last stop = `break` point
- Intermediate stops = `through` points
- Costing: `bus` with fallback to `auto`

### Circular Routes (2 routes)
- Same as linear BUT append first stop at the end as `break` to close the loop

### Key Lessons
- Valhalla FAITHFULLY follows the stop order — if stops are wrong, geometry is wrong
- The coherence validator must run before Valhalla
- Using Step 30 from the existing pipeline is better than standalone scripts
- Rural mountain routes work better with fewer waypoints

## Known Road Order Rules

Stop pairs that have been systematically wrong. FIRST should come BEFORE second (Marín→Valle):

```
Jardín del Valle → Orquídeas
Desvío Simón Bolívar → Antiguo Peaje
Puertas del Sol → Puente 1
La Glacial → Sta. Barbara
Poncho Verde → El Colibrí
Fabrica → Espe
Hiper Market → Super Market
Las Vallas → Humberto Vacas Gómez
Carolina 2 → Centro Educativo Khipu
Iglesia de Capelo → Capelo
Portal → La Ribera → Los Arupos
```
