# Phase 4 · Evidence and Matching

Owner decision 2026-10-09: make the Phase 4 Evidence and Matching tabs work
with the in-process architecture. The HTTP service they were written for is
not restored.

## Migration plan (V4_8)

`phase4_naming/db/migrations/V4_8__evidence_matching.sql`, schema unit
`phase4.evidence_matching`. DDL only: 2 tables and 4 indexes, no rows written,
no existing object altered.

* `semantics.route_evidence_matches`: V4's columns (`match_id`, `record_id`,
  `route_id`, `score`, `score_parts`, `is_best`, `created_at`) plus `matcher`
  and `requested_by`; `score` checked to 0–1; unique per
  (record, route, matcher), so re-running a matcher updates in place.
* `semantics.match_labels`: V4_2's columns and relevance scale (2 correct,
  1 partial, 0 not this route) plus `reviewed_by` (required), `match_id`
  (optional link to the proposal) and `updated_at`; unique per
  (record, route).
* Foreign keys: to `route_evidence_records` (cascade) and
  `route_raw.route_jobs` (cascade), as V4/V4_2 defined them. All 759
  production routes exist in `route_jobs`.

records and `console.phase_decisions`). To undo V4_8:
`DROP TABLE semantics.match_labels, semantics.route_evidence_matches;`

Apply (explicit, never on page load):

```bash
python -m datamind_console.db.schema_cli plan phase4.evidence_matching
python -m datamind_console.db.schema_cli apply phase4.evidence_matching --yes
```

The tabs verify the unit read-only; if it is missing they show the command
instead of creating anything.

## How it works

`datamind_console/phases/phase4_naming/evidence_matching.py`.

**Evidence**
* List with search (ref, name, operator, endpoints, source id, title),
  source-type filter and a review status derived from labels: *pending*
  (no label), *linked* (a route marked correct), *partial*, *rejected*.
* Detail shows every field, provenance (source type and id, who created
  it, when) and the labels it has received.
* Manual creation is a human action: reviewer name required, at least one of
  ref / name / operator / endpoints, confidence 0–1, note ≤ 4,000 chars.
  Insert and audit row (`phase 4`, `evidence:<id>`, `EDIT`,
  `evidence_created`) commit together or not at all.

**Matching**
* *Generate candidates* is requested by a reviewer and stores a proposal,
  never an acceptance. Matcher `deterministic_v1` scores every production
  route against the record using only what the record provides: route-id
  hint, ref, name, endpoints (either orientation), geometry (share of the
  evidence line within 150 m of the route, or bbox overlap) and operator.
  Missing signals are left out and the remaining weights renormalised.
  Proposals keep their per-signal explanation in `score_parts`. Re-running
  updates in place and withdraws proposals that no longer qualify; labels
  are kept.
* *Label* is the human decision: correct (2), partial (1) or not this route
  (0, needs a note). Label and audit row (`phase 4`,
  `evidence_match:<record>:<route>`, `APPROVE` / `EDIT` / `REJECT`) commit
  together. A later label replaces the earlier one; the audit keeps both.
* **Model prediction is not offered.** There is no trained model; the labels
  are the training data one would need.

Not included: seeding evidence from Overpass (the old pipeline action calls
live Overpass and writes tables that were never created) and turning a
confirmed match into route naming; both are separate pipeline steps.
