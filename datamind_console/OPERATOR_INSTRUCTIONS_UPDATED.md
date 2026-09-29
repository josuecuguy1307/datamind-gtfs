# DataMind Operator Instructions (Updated with AI Bot / Workflow Roles)

This document updates the original Phase 1 operator instructions with the latest workflow doctrine, AI Bot role split, safety rules, and operational readiness notes.

## 0) Current Operational Context (Latest Updates)

- AI Bot / AI Insights foundation is implemented and operational.
- DB-backed path has been verified end-to-end (read + write/read/cleanup) on the local Postgres target used for validation.
- Real Phase 3 live coverage is now present (no longer zero), including logs from:
  - `step_20_sequences`
  - `step_30_geometry`
  - `step_35_rank`
  - `step_40_approve`
- A real E2E Phase 3 validation flow was completed successfully (Step 20/30/35/40), and Phase 3 compare/trend views now return valid results in Insights.
- Remaining major gap for ML maturity: operator-confirmed labels at scale (feedback rows were still low/zero at audit time).

## 1) Role Division (Authority vs Assistance)

### Runtime + Validators (Authority)
These components are the source of truth for execution and safety.

They control:
- phase step sequencing
- pass/fail gates
- validation checks
- destructive action boundaries
- final execution correctness

### AI Bot / AI Insights (Operator Assistance Layer)
The AI Bot is not the execution authority. It is the telemetry + scoring + insights layer.

It is responsible for:
- run logs (Phase 1 / Phase 3)
- scoring (extraction quality, sequence quality)
- trend/compare views
- regression flags
- readiness counters and model progression signals
- self-review metrics (bot quality)
- operator feedback/grading capture
- patch-task context generation for Codex
- telemetry sanity checks
- sector tuning telemetry and recommendations (where implemented)
- merge evidence helpers / proposal scoring (proposal-only)

### ChatGPT API (Analyst / Interpreter / Task Generator)
The ChatGPT API should sit on top of AI Bot snapshots and provide:
- run analysis
- AI Bot self-review interpretation
- trend/regression interpretation
- operator work prioritization suggestions
- merge evidence interpretation (advisory only)
- Codex patch-task generation
- model readiness coaching

It should NOT be the execution authority.

### Codex (Patch Engineer / Calibrator)
Codex is used to:
- patch code/config/UI/logging
- calibrate thresholds/weights
- improve AI Bot usefulness
- add safe helper modules (sequence/merge evidence, sector tuning)
- keep changes scoped and safe

### Human Operator (You)
You remain responsible for:
- final supervision
- merge/direction binding confirmation
- destructive cleanup confirmation
- ambiguous stop resolution judgment
- grading AI Bot usefulness
- operational priorities

## 2) Safety and Governance Rules (Must Preserve)

### Core rule
AI-proposed + human-confirmed automation.

### Never allow silent automation for high-impact actions
- No phase gate bypass.
- No silent auto-pass of sequence steps.
- No silent auto-commit reorder.
- No silent auto-bind merge directions.
- No silent destructive cleanup.

### Allowed pattern (recommended)
The bot/API may:
- compute scores
- generate proposals
- show evidence + confidence
- ask the operator to confirm/reject/edit
- log operator decisions

## 3) Phase 1 Operator Instructions (Original + Reinforced)

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

### 2) Normalize

How to follow:

- Run immediately after extraction for the `node_set_id`.
- Confirm non-zero candidate count.

What it achieves:

- Converts raw Overpass elements into normalized node candidates in `node_work.node_candidates`.
- Filters out unusable/other tags and keeps georeferenced candidates.

### 3) Features

How to follow:

- Run after normalize.
- Use the default pipeline behavior (ML if present, fallback heuristic otherwise).

What it achieves:

- Computes deterministic features (name/ref/operator presence, confidence proxy).
- Classifies candidates as STOP vs POI signals.

### 4) Cluster

How to follow:

- Run DBSCAN clustering on the `node_set_id`.
- Tune only exposed params (`eps_m`, `min_pts`) if needed.

What it achieves:

- Groups nearby candidates that likely represent the same real stop.
- Reduces duplicate points before resolution.

### 5) Resolve

How to follow:

- Run after clustering.
- Review resolved count; zero usually means bad extraction or bad clustering.

What it achieves:

- Chooses a representative candidate per cluster.
- Writes resolved work rows into `node_work.nodes_resolved`.

### 6) Rank Node Set

How to follow:

- Run to compare multiple extraction attempts/actions.
- Prefer sets with stronger resolved count and metadata quality.

What it achieves:

- Produces a heuristic quality score for the node set.
- Helps decide which extraction run is better for promotion.

### 7) Review (Critical for Phase 3 Unmatched/Ambiguous Stops)

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

### 8) Approve and Promote

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

### Operator naming fallback rule (reinforced)
For new unmatched stops with missing/noisy/numeric names, use a safe fallback such as `Parada` and verify location carefully. Naming can be refined later, but do not block route progress on perfect naming.

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

## 4) Phase 2 and Phase 3 Workflow Doctrine (Operational Additions)

### Phase 2 (Semantics / Cleanup / Node Quality)
Primary goal:
- run the pipeline correctly
- perform global cleanup and dedup carefully
- use Workspace Nodes for focused review

Important:
- cleanup/destructive actions can affect downstream route matching and route rebuild requirements
- treat cleanup as supervised work
- do not casually run destructive normalization/deletes

### Phase 3 (Routes / Sequences / Merge)
Primary goal:
- run route pipeline correctly
- maximize route extraction quality
- ensure sequence order is reasonable
- ensure stop matching is complete before passing sequence gates
- complete merge/direction workflow correctly for successful prod routes

#### Sequence step rules
- Do not pass if the gate is not fulfilled.
- Resolve unmatched/ambiguous stops via Phase 1 New Nodes.
- Only continue once matching is clean enough to satisfy the gate.
- If reorder is not necessary, leave the sequence as-is.

#### Reorder proposal policy
The bot may propose a reorder and confidence score, but application must require operator confirmation.

#### Merge / direction policy
The bot may propose merge candidates and dir0/dir1 suggestions with evidence and confidence, but binding must require operator confirmation in the merge workflow.

## 5) Merge / Opposite Direction Scoring Nuance (Corrected)

Do NOT assume opposite directions use the exact same stop nodes in reverse order.

In real routes, opposite directions may use:
- opposite-side stops on the same roads/highways
- offset stops (slightly ahead/behind)
- different terminal bays/platforms
- one-way segments or directional detours

### Therefore, opposite-direction scoring should use:
- corridor correspondence
- paired-stop alignment (not exact stop ID equality only)
- endpoint region swap (not exact endpoint node only)
- reverse corridor progression
- Overpass metadata / naming evidence
- confidence penalties (unmatched/ambiguous/low sequence quality)

This is proposal-only support for merge review and should not silently bind directions.

## 6) Sector-Based Extraction Tuning (Valle de los Chillos + Nearby)

A major improvement path is area-targeted extraction tuning using a sector catalog.

### Build and maintain a sector catalog with:
- canonical sector names
- aliases / spelling variants
- area groups (e.g., `valle_core`, `conocoto_corridor`, `amaguana_axis`, `quito_gateways`)
- sector type (`urban_center`, `corridor_node`, `terminal_zone`, `rural_axis`)
- priority
- optional bbox suggestions
- recommended extractor templates (later)

### Extraction tuning factors to improve over time
- bbox auto-expansion retries when coverage is low
- area-group-based action/template selection
- tag coverage diagnostics (`public_transport`, `highway=bus_stop`, etc.)
- stop-vs-poi tuning by zone
- adaptive DBSCAN params by density
- extraction quality scoring by area type
- action comparison runner (batch test multiple templates)
- performance-based recommended config memory by sector

## 7) AI Bot Operational Readiness and What Is Still Missing

### What is already operationally validated
- DB-backed AI Bot path is verified end-to-end.
- Phase 3 live telemetry coverage exists and appears in Insights.
- A real E2E Phase 3 flow (Step 20/30/35/40) has been executed and recorded.
- Phase 3 compare/trends are operational in Insights.

### What is still not "fully mature" (mainly ML-side)
- Operator-confirmed labels are not yet collected at sufficient scale.
- Phase 3 labels are still largely heuristic/bootstrap (e.g., derived from detector behavior).
- ML readiness remains low-volume (`not_enough_logged_runs` type state).
- Threshold calibration should wait until more real Phase 3 traffic accumulates.

## 8) Operator Labeling Plan (Next Frontier)

To move from heuristic/bootstrap ML to meaningful supervised ML, attach operator labels to real Phase 3 runs.

### Recommended Phase 3 labels (v1)
Attach labels to `run_id` / `phase` / `stage` / `route_id`:

- `operator_sequence_label` (`good | needs_minor_fix | needs_major_fix | bad_sequence`)
- `sequence_warning_correct` (`yes | partial | no`)
- `reorder_action_taken` (`yes | no | not_applicable`)
- `reorder_helpful` (`yes | partial | no | not_applicable`)
- `final_run_disposition` (`passed_clean | passed_after_manual_fix | blocked_unresolved_stops | blocked_sequence_quality | blocked_merge_review | abandoned`)
- `operator_grade` (1..5)
- `operator_notes`

### GTFS-derived labels (positive examples)
Trusted GTFS routes can be used as positive labels for well-formed route/sequence patterns.
Use confidence tiers such as:
- `gold`
- `silver`
- `bronze`

## 9) Dashboard vs n8n (Automation Split)

### Keep core automation in the dashboard
Use the dashboard for:
- runtime/validators
- AI Bot telemetry/scoring/readiness
- ChatGPT API panel (analysis layer)
- operator confirmations (merge/reorder)
- patch-task generation for Codex

### Use n8n for external orchestration (secondary layer)
Use n8n for:
- scheduled checks
- alerts/notifications
- daily/weekly summaries
- ticket/task creation in external systems
- periodic health checks and reminders

Do not make n8n the authority for phase logic, merge decisions, or gate-critical actions.

## 10) Operational Tooling Added (Recent)

Recent ops/check tools added around AI Bot readiness include scripts/runbooks such as:
- local Postgres health / stale lock diagnostics
- DB verifier for AI Insights path
- AI Bot go-live readiness checker
- ops runbook for local AI Bot / DB issues

Use these to avoid repeated ops downtime (e.g., stale Postgres lock/startup issues) and to re-verify readiness quickly.

## 11) Recommended Near-Term Priorities (Operational)

1. If needed, clean up validation artifacts (test routes / recent test telemetry) before label-planning work.
2. Start collecting operator-confirmed Phase 3 labels on real runs.
3. Accumulate more Phase 3 real traffic before threshold calibration.
4. Calibrate warning/regression thresholds using real traffic + operator labels.
5. Then proceed with stronger ML experiments/readiness progression.
6. Then layer in ChatGPT API integration on top of structured AI Bot snapshots.

## 12) Final Principle

The system should continue to operate as:
- Runtime/validators = authority
- AI Bot = telemetry + scoring + proposals
- ChatGPT API = interpretation + prioritization + Codex-task generation
- Codex = patching and calibration
- Operator = final supervision and confirmation

This is the intended architecture for safe scaling and trustworthy automation.
