# Phase 3 Coverage Workflow Operator Runbook

## Pilot Readiness

This workflow is ready for supervised operator pilot use.

Use it in this order:

1. Run extraction first.
2. Run safe canonicalization second.
3. Review the global catalog third.
4. Review sector coverage fourth.
5. Sync and review gaps fifth.
6. Use Manual Sequence Builder only for gaps the operator decides to complete manually.

Do not treat coverage gaps as permission to skip extractor review.
Do not merge routes by name only.
Do not suppress opposite directions or meaningful variants.

## Operator Runbook

### 1. When to run safe canonicalization

Run safe canonicalization after a Step05 harvest batch has produced stored extractor review rows and before doing catalog-wide coverage reasoning.

Use it when:

- the same OSM relation appears in multiple extractor review rows
- you want one canonical review row per relation cluster
- you want duplicate attempts preserved without deleting route jobs

Do not use it as an approval mechanism. It is metadata-only clustering.

### 2. How to review the global catalog

Open `Phase 3 -> Coverage`.

Use the `Global Phase 3 Catalog` table to review:

- canonical vs suppressed duplicate state
- route family label
- chosen relation id
- service route / direction
- Step05 / Step20 / geometry / approval / prod state
- manual-origin rows
- latest linked coverage gap id

Primary catalog questions:

- Is this route only extracted, or already sequenced/approved/prod?
- Is this row canonical or suppressed?
- Is this a manual route linked to a gap?
- Is the route showing under the correct sector?

### 3. How to inspect sector coverage

Stay in `Phase 3 -> Coverage`.

Use `Sector Coverage` to review one sector at a time.

Look for:

- route families present
- extracted but not prod families
- manual families
- prod families
- incomplete family count
- direction 0 / direction 1 readiness

Use sector coverage as observed inventory, not as absolute transit truth.

### 4. How to sync coverage gaps

From `Coverage`, click `Sync coverage gaps from Phase 3 catalogs`.

Run sync:

- after extractor harvest
- after safe canonicalization
- after a batch of manual approvals

Expected result:

- open gaps stay open
- in-progress gaps remain linked
- resolved gaps stay resolved
- no runaway duplicate gap creation

### 5. How to interpret classifications

`still_extractable`

- default meaning: extractor retry or extraction patch review should still be considered first
- manual completion is allowed only if the operator decides the extractor path is not practical for the current session

`non_reliably_extractable`

- default meaning: extractor evidence is weak or unstable enough that manual completion is the safer next action

Operator rule:

- classification guides priority, but does not remove operator judgment
- any override must be written into gap notes

### 6. How to export missing-route JSON catalogs

From `Coverage`, click `Export missing-route JSON catalogs`.

Use exports when:

- handing work to another operator
- planning a sector session
- building a manual-completion queue

Treat JSON exports as planning artifacts, not approval artifacts.

### 7. How to open a gap in Manual Sequence Builder

From `Coverage`, select a gap and click `Open gap in Manual Sequence Builder`.

Confirm the builder shows:

- linked gap id
- sector
- classification
- route family hint
- start/end hints
- recommended approved stops
- related extracted/catalog rows

If classification is `still_extractable`, pause and confirm the operator really wants manual completion now.

### 8. How to continue through Step30 and Step40

In `Manual Sequence Builder`:

1. load recommended stops or assemble the stop sequence manually
2. resolve validation errors
3. review warnings
4. export the sequence into Phase 3

After export:

1. confirm the route is linked to the selected gap
2. approve the canonical sequence
3. run `Step 30`
4. run `Step 32`
5. review geometry candidates plus recovered/ambiguous nearby stop evidence
6. run `Step 35` if ranking is needed
7. run `Step 40`

`Step 40` is the point where the linked gap should become resolved.

### 9. How to confirm a gap is resolved

After `Step 40`:

- return to `Coverage`
- open the gap row again
- confirm `resolution_status = resolved`
- confirm `resolved_route_id` is set
- confirm `resolved_prod_route_id` is set

### 10. How to verify catalog and sector reflection after approval

After `Step 40` approval, verify:

- the catalog row shows `geometry_status = prod`
- the catalog row shows `approval_status = prod`
- the catalog row still shows the linked gap id
- the route appears in the intended sector
- the sector summary now counts the family correctly

If the route is approved but shows under `unassigned`, stop and investigate before continuing the pilot session.

## Pilot SOP

### Pre-Session Checks

- confirm migrations through `026_inverse_direction_search.sql` are applied
- confirm `route_review.phase3_global_catalog_v1` loads
- confirm `route_review.phase3_sector_coverage_v1` loads
- confirm `route_review.coverage_gaps` loads
- confirm Step05 stored extractor review is visible
- confirm approved Phase 2 stops are visible in Manual Sequence Builder

### Per-Session Workflow

1. Run Step05 extraction or batch harvest for the target sector.
2. Run safe canonicalization if duplicate relation clusters are present.
3. Review the global catalog for the sector.
4. Review sector coverage for the sector.
5. Sync coverage gaps.
6. Review classifications and priorities.
7. Decide whether each selected gap should stay extractor-first or move to manual completion.
8. Export missing-route JSON if the session needs a portable worklist.
9. For any manually chosen gap, open it in Manual Sequence Builder.
10. Export, approve sequence, run Step30, review geometry, and run Step40.
11. Confirm gap resolution and catalog/sector reflection before moving to the next gap.

### Approval Safety Rules

- Step40 remains operator-triggered
- service-route approval remains operator-triggered
- do not approve geometry you have not reviewed
- do not approve a service route unless directions and route state justify it

### Dedupe Caution Rules

- safe canonicalization is not deletion
- never dedupe by similar names only
- preserve opposite directions
- preserve meaningful variants
- preserve distinct review contexts
- if uncertain, leave rows unsuppressed and review later

### Gap-Resolution Checklist

- gap selected intentionally
- classification understood
- manual sequence validated
- sequence approved
- Step30 candidates generated
- geometry reviewed
- Step40 approved
- gap resolved in Coverage
- catalog row updated
- sector row updated

### Post-Session Verification

- no critical gap is left half-resolved without notes
- new manual routes still show linked gap ids
- resolved gaps show `resolved_route_id` and `resolved_prod_route_id`
- catalog sector placement looks correct
- no unexpected suppressed duplicate state appeared
- no operator bypassed approval steps

## Pilot Closeout Note

Staging E2E verification created this artifact set:

- `gap_id`: `6e3f4ec8-18f2-4615-955c-db1a88518da0`
- `service_route_id`: `3e4f0e20-c22a-4825-a9b3-e5ecfabdb162`
- `route_job_id`: `6f0e0483-376c-47ee-bdad-af45b22f362a`
- `export_id`: `d08c0a2d-64e2-4841-812c-0f241a1f0548`

Recommendation:

- preserve it if the team wants a known-good staging fixture for demos, training, or regression checks
- clear it before operator pilot only if the pilot must start from a completely clean staging dataset

Default recommendation: preserve it as a fixture in staging, not in production.
