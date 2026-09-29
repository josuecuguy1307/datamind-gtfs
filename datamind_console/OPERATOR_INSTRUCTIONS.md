# Phase 1 Operator Instructions (Nodes / Stops Extraction + Review)

## What Phase 1 Does

Phase 1 extracts raw map elements (mainly stops/platforms and related transit nodes), converts them into candidate stop nodes, clusters and resolves duplicates, and publishes approved nodes into `node_prod.nodes`.

This phase is the source of truth for canonical stop nodes before Phase 2 semantics and Phase 3 route matching.

## What Phase 1 Achieves

- Increases stop/node recall in the target area (Valle de los Chillos, Quito).
- Creates or updates canonical stop nodes in `node_prod.nodes`.
- Provides the review workflow used to resolve Phase 3 unmatched/ambiguous route stops.

## Main Inputs

- Overpass extraction actions and templates (`actions.json`)
- Bounding box and action parameters
- Manual review decisions in the Phase 1 UI/client

## Main Outputs

- `node_work.*` candidate / cluster / resolved work tables
- `node_prod.nodes` (approved canonical nodes)
- `node_work.node_review_requests` (including `source='phase3_route'` requests)

## Standard Phase 1 Pipeline (CLI)

Primary runner:

- `phase1_nodes/datamind/services/openmaps_extractor/scripts/run_phase1.py`

Main sequence executed by the runner:

1. Build node set (select extraction action + run Overpass)
2. Normalize raw elements to node candidates
3. Compute features / stop-vs-poi signals
4. Cluster nearby candidates
5. Resolve one representative per cluster
6. Rank the node set
7. Review and approve
8. Promote approved nodes to `node_prod.nodes`

## Step-by-Step: What to Follow and What It Achieves

### 1) Build Node Set / Extract

How to follow:

- Choose an extraction action (for coverage use `stops_broad_bbox`, `platforms_bbox`, `terminals_and_stations_bbox`, etc.).
- Run with the target bbox.
- Use broader bbox runs when route stop coverage is sparse.

What it achieves:

- Executes Overpass queries and stores raw elements (`node_raw.overpass_elements`).
- Creates a `node_set_id` that tracks the extraction batch.

## 2) Normalize

How to follow:

- Run immediately after extraction for the `node_set_id`.
- Confirm non-zero candidate count.

What it achieves:

- Converts raw Overpass elements into normalized node candidates in `node_work.node_candidates`.
- Filters out unusable/other tags and keeps georeferenced candidates.

## 3) Features

How to follow:

- Run after normalize.
- Use the default pipeline behavior (ML if present, fallback heuristic otherwise).

What it achieves:

- Computes deterministic features (name/ref/operator presence, confidence proxy).
- Classifies candidates as STOP vs POI signals.

## 4) Cluster

How to follow:

- Run DBSCAN clustering on the `node_set_id`.
- Tune only exposed params (`eps_m`, `min_pts`) if needed.

What it achieves:

- Groups nearby candidates that likely represent the same real stop.
- Reduces duplicate points before resolution.

## 5) Resolve

How to follow:

- Run after clustering.
- Review resolved count; zero usually means bad extraction or bad clustering.

What it achieves:

- Chooses a representative candidate per cluster.
- Writes resolved work rows into `node_work.nodes_resolved`.

## 6) Rank Node Set

How to follow:

- Run to compare multiple extraction attempts/actions.
- Prefer sets with stronger resolved count and metadata quality.

What it achieves:

- Produces a heuristic quality score for the node set.
- Helps decide which extraction run is better for promotion.

## 7) Review (Critical for Phase 3 Unmatched/Ambiguous Stops)

How to follow:

- In the Phase 1 review lab, process `node_work.node_review_requests`.
- For `source='phase3_route'`, resolve requests created from Phase 3 Step 20.
- Treat these as route-blocking work when sequence quality depends on them.

Two important request types:

- `unmatched`: no canonical stop found nearby
- `ambiguous`: multiple canonical stops found nearby

What it achieves:

- Converts unresolved route prior stops into usable canonical nodes or confirmed matches.
- Reduces Phase 3 sequence fallback behavior.

## 8) Approve and Promote

How to follow:

- Approve reviewed nodes that are correct and useful.
- Promote approved nodes to `node_prod.nodes`.
- Reject obvious duplicates/noise.

What it achieves:

- Publishes canonical stop nodes for downstream use.
- Makes new/updated nodes available to Phase 2 semantics.

## Special Procedure for Phase 3 Route Stop Resolution (Required)

When Phase 3 Step 20 reports unmatched/ambiguous stops:

1. Open Phase 1 review requests for `source='phase3_route'`
2. Resolve ambiguous requests by selecting the correct existing node
3. Approve/create nodes for valid unmatched stops
4. Promote changes to `node_prod.nodes`
5. If new or updated nodes were completed, rerun Phase 2 (at least Steps 10-30)
6. Rerun Phase 3 Step 20 before Valhalla

## Common Operator Goals in Phase 1

- Maximize stop recall in Valle de los Chillos first
- Add missing connector stops that block route sequencing
- Resolve ambiguous terminal/Marin/Conocoto corridor stops carefully
- Keep node positions and names reasonable for Phase 2 approval

## Handoff to Phase 2

Run Phase 2 after Phase 1 whenever:

- New stop nodes were approved
- Existing stop nodes were moved/renamed materially
- Phase 3 Step 20 still cannot match stops that should now exist

