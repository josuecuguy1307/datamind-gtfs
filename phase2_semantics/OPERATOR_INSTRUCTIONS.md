# Phase 2 Operator Instructions (Semantics / Geocoder / Canonical Mapping)

## What Phase 2 Does

Phase 2 turns Phase 1 canonical nodes into semantic places and aliases, then publishes final node-to-place mappings in `geo_prod`.

This phase is the canonical matching layer that Phase 3 uses to match route prior stops to approved STOP nodes.

## What Phase 2 Achieves

- Creates active semantic places (`geo_prod.places`)
- Creates aliases for search and naming consistency (`geo_prod.place_aliases`)
- Publishes canonical node-to-place mappings (`geo_prod.node_place_map`)
- Enables Phase 3 Step 20 stop matching against approved STOPs

## Critical Cross-Phase Rule

Phase 3 Step 20 does not match against raw Phase 1 nodes alone. It matches against:

- `geo_prod.node_place_map`
- `geo_prod.places` with active status
- `node_prod.nodes` (STOP)

So if you create new stops in Phase 1 and skip Phase 2 approval, Phase 3 Step 20 may still show unmatched stops.

## Main Inputs

- `node_prod.nodes` from Phase 1
- Existing Phase 2 feedback/model artifacts (optional but useful)

## Main Outputs

- `geo_raw.*` extracted evidence
- `geo_work.*` candidates and work tables
- `geo_prod.places`
- `geo_prod.place_aliases`
- `geo_prod.node_place_map`

## Standard Runner

- `phase2_semantics/scripts/run_all.py`

Supports scoped runs:

- `--from-step`
- `--to-step`
- `--continue-on-error`

## Phase 2 Steps: What to Follow and What Each Step Achieves

## Step 00 - Migrate

How to follow:

- Run when setting up or after schema changes/missing table errors.
- Safe to rerun.

What it achieves:

- Applies SQL migrations for `geo_raw`, `geo_work`, `geo_prod`, views, and indexes.

## Step 10 - Extract Evidence

How to follow:

- Run after Phase 1 changes (new/updated nodes).
- This is the first required step for propagating Phase 1 changes into Phase 2.

What it achieves:

- Reads canonical nodes from `node_prod.nodes`.
- Extracts semantic/name evidence into `geo_raw.name_evidence`.

## Step 15 - Build Geo Context

How to follow:

- Run after Step 10.
- Uses latest extract run for the configured context.

What it achieves:

- Builds density/context features (transit/poi around each node).
- Improves place-type and naming candidate quality.

## Step 20 - Build Candidates

How to follow:

- Run after Steps 10 and 15.
- Review output counts (`places`, `aliases`, `node_maps`).

What it achieves:

- Builds place candidates, alias candidates, and node-place work mappings in `geo_work`.
- Seeds naming candidates for the same place set.

## Step 25 - Build Name Candidates

How to follow:

- Run after Step 20 (or rerun when naming artifacts/feedback changed).

What it achieves:

- Builds and scores canonical name candidates for the latest place set.

## Step 30 - Approve (Most Important for Phase 3)

How to follow:

- Run after Step 20/25 for the target place set.
- Confirm approval counts are non-zero.
- This is required before retrying Phase 3 Step 20 matching.

What it achieves:

- Promotes approved places to `geo_prod.places`
- Promotes aliases to `geo_prod.place_aliases`
- Promotes node-place mappings to `geo_prod.node_place_map`

## Step 35 - Train / Rescore Models (Optional for Route Matching)

How to follow:

- Run when enough feedback exists or you want improved naming/type predictions.
- Not required for basic Phase 3 stop matching.

What it achieves:

- Trains/rescores name ranker and place type model using feedback.

## Step 40 - Build Embeddings (Optional for Route Matching)

How to follow:

- Run for search quality/semantic retrieval refresh.

What it achieves:

- Builds pgvector embeddings from `geo_prod.place_aliases`.

## Step 50 - Reindex / Analyze (Optional for Route Matching)

How to follow:

- Run after large updates to improve query planning/search performance.

What it achieves:

- Runs `ANALYZE` on Phase 2 semantic tables.

## Step 60 - Search Demo (Validation / Smoke Test)

How to follow:

- Run when you want a quick semantic search sanity check.

What it achieves:

- Verifies semantic search behavior against current `geo_prod` data.

## Minimum Phase 2 Rerun After Phase 1 Node Changes (Recommended Operator Path)

Use this after resolving Phase 3 unmatched/ambiguous stops in Phase 1:

1. Step 10
2. Step 15
3. Step 20
4. Step 25 (recommended)
5. Step 30

CLI pattern:

- `python scripts/run_all.py --from-step 10 --to-step 30 --continue-on-error`

## Success Signals Before Returning to Phase 3

- New/updated Phase 1 nodes appear in `geo_prod.node_place_map`
- `geo_prod.places` entries are active
- Step 30 reports non-zero approved counts
- Phase 3 Step 20 unmatched/ambiguous counts decrease on rerun

## Handoff to Phase 3

After Step 30 is complete:

1. Rerun `Phase 3 Step 20`
2. Recheck sequence quality and unresolved stop counts
3. Only proceed to Valhalla (`Step 30` in Phase 3) when sequence is reasonable

