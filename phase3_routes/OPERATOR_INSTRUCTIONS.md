# Phase 3 Operator Instructions (Route Constructor: Sequence + Geometry)

## What Phase 3 Does

Phase 3 builds route geometry from route evidence (usually an OSM relation) plus canonical stops from Phases 1 and 2.

It discovers/fetches route relations, builds stop-sequence candidates, builds/ranks geometry candidates with Valhalla, and approves the final route geometry into `route_prod.routes`.

## What Phase 3 Achieves

- Converts OSM route relations into usable route geometries
- Produces approved route shapes in `route_prod.routes`
- Stores stop sequence and geometry evidence for review and reruns
- Prepares routes for Phase 4 naming and Phase 5 GTFS export

## Main Inputs

- OSM route relations (Overpass)
- Canonical STOP nodes from Phase 1 + Phase 2 (`node_prod` + `geo_prod`)
- Valhalla routing service

## Main Outputs

- `route_raw.*` relation raw data and jobs
- `route_work.relation_stop_prior`
- `route_work.stop_sequence_candidates`
- `route_work.geometry_candidates`
- `route_work.route_approvals`
- `route_prod.routes` (approved geometry + stop node ids)

## Standard Step Order

Explicit mode:

1. `10_fetch_relation.py`
2. `15_inverse_completion` gate
3. `20_build_stop_sequences.py`
4. `30_build_geometry_candidates.py`
5. `32_geometry_stop_recovery.py`
6. `35_rank_geometry_candidates.py`
7. `40_approve_geometry.py`

Discover mode (recommended for expansion):

1. `05_discover_relation.py`
2. `10_fetch_relation.py`
3. `15_inverse_completion` gate
4. `20_build_stop_sequences.py`
5. `30_build_geometry_candidates.py`
6. `32_geometry_stop_recovery.py`
7. `35_rank_geometry_candidates.py`
8. `40_approve_geometry.py`

## Phase 3 Steps: What to Follow and What Each Step Achieves

## Step 05 - Discover Route Relation (Optional but useful for expansion)

How to follow:

- Use bbox (Valle first) and optional filters (`refs`, `operator`, `name`).
- Review the top candidate output before continuing.
- Expand bbox strategically when coverage is sparse.

What it achieves:

- Finds OSM route relation candidates in the target area.
- Scores them by route quality signals and stop prior count.
- Can store candidate selection metadata (best effort).

## Step 10 - Fetch Relation Raw

How to follow:

- Run with route id + OSM relation id (or use chosen relation from Step 05).
- Confirm relation raw was stored.

What it achieves:

- Fetches Overpass JSON for the route relation.
- Stores source evidence in `route_raw`.

## Step 15 - Direction Readiness / Inverse Completion Gate

How to follow:

- Run after Step 10 and before Step 20.
- Do not bypass this gate if inverse direction completion is still blocking.
- Use it to stabilize direction context before sequence construction.

What it achieves:

- Validates direction readiness before Step 20
- Surfaces missing inverse-direction evidence when required
- Prevents unstable direction context from propagating into sequence/geometry work

## Step 20 - Build Stop Sequences (Critical Gate Before Valhalla)

How to follow:

- Run after Step 10.
- Read the printed results carefully:
  - `unmatched_stops`
  - candidate list ranks
  - `matched_stops`
  - distance metrics
- Do not continue blindly if the sequence looks unreasonable.

What it achieves:

- Extracts or reuses `relation_stop_prior`
- Matches route prior stops to canonical STOP nodes using Phase 2 final mappings
- Builds strict/relaxed stop-sequence candidates
- Always creates a fallback raw-prior candidate (`rank=99`)
- Creates Phase 1 review requests for unresolved stops (script path handles unmatched; client sync handles unmatched + ambiguous)

## Step 30 - Build Geometry Candidates (Valhalla)

How to follow:

- Run only after a reasonable stop sequence candidate is chosen.
- Prefer sequence candidates with good matched stop counts and sensible order.
- Block fallback-only bad sequences before this step.

What it achieves:

- Calls Valhalla with multiple costing variants
- Builds geometry candidate set
- Computes geometry quality metrics and score
- Penalizes non-tight direction behavior

## Step 32 - Geometry-based Stop Recovery

How to follow:

- Run immediately after Step 30 and before Step 35.
- Treat recovered stops as explicit geometry-guided evidence, not as a silent overwrite of Step 20.
- Review ambiguous nearby stops before trusting completeness gains.

What it achieves:

- Scans canonical stops already in the DB against each geometry candidate corridor
- Proposes conservative stop recoveries that fit the geometry and sequence ordering
- Keeps ambiguous nearby stops visible instead of forcing them into the route
- Persists recovery provenance for downstream ranking and audit

## Step 35 - Rank Geometry Candidates

How to follow:

- Run after Step 30.
- Run after Step 32 so ranking can use recovered/ambiguous stop evidence.
- Use `--explain` when geometry quality is questionable.

What it achieves:

- Ranks geometry candidates (writes `ml_rank`/`ml_score` in metrics)
- Makes approval selection more consistent

## Step 40 - Approve Geometry

How to follow:

- Run after reviewing geometry ranking/output.
- Confirm correct direction and reasonable route shape.

What it achieves:

- Selects the best geometry candidate
- Writes route approval
- Upserts approved geometry into `route_prod.routes`

## Hard Operator Rule: Sequence Quality Gate Before Valhalla

Do not send a route to Valhalla if the stop sequence is not reasonable.

Block and reroute to Phase 1/2 when:

- only fallback candidate is usable (`rank=99`, `raw_prior_fallback`)
- `matched_stops=0`
- sequence order is clearly unreasonable
- unresolved stops (unmatched/ambiguous) materially affect route plausibility

## Required Resolution Procedure (Phase 3 -> Phase 1 -> Phase 2 -> Phase 3)

Use this procedure whenever Step 20 is weak:

1. Run/review `Step 20`
2. Sync unresolved stops to Phase 1 review requests (prefer client sync for unmatched + ambiguous)
3. Resolve in Phase 1:
   - approve/create missing stop nodes
   - resolve ambiguities to the correct existing node
4. If new/updated nodes were completed, rerun Phase 2 (minimum Steps 10-30)
5. Rerun Phase 3 `Step 20`
6. Only then continue to Phase 3 `Step 30`, `Step 32`, and downstream ranking/approval

## What to Check in Step 20 Before Approving a Sequence Candidate

- Candidate rank is not fallback-only (`rank=99` should not be the only acceptable option)
- `matched_stops` is high enough to represent the route
- Distance metrics are reasonable (`avg_m`, `max_m`)
- Sequence direction/order makes geographic sense
- Unmatched/ambiguous stops are not clustered at critical segments (terminals, branches, loops)

## What to Check in Geometry Before Approval

- Route shape follows the corridor and does not loop absurdly
- Start/end align with expected terminals
- Direction is consistent with the selected stop sequence
- Candidate scores and ranking are not hiding a visibly wrong path

## Operational Tips for Valle de los Chillos Expansion

- Use discover mode with expanding bbox windows around Valle center/corridors
- Add routes in directional pairs, but validate each direction separately
- Reuse the same sequence-quality gate for every route
- Prioritize Phase 1/2 fixes when Step 20 quality is weak instead of forcing Valhalla

## Handoff to Phase 4 / Phase 5

After Step 40 approval:

1. Finalize route naming/semantics in Phase 4
2. Integrate route into Phase 5 (shapes, trips, stop_times, validation, packaging)

Phase 5 quality depends heavily on:

- reasonable stop sequence ordering
- usable route geometry
- canonical stop coverage from Phase 1 + Phase 2
