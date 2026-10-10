# Review workflow

## The Review page

`datamind_console/views/review_view.py` (navigation: *Review → Review*) lists
everything waiting for a person, in one place. It is **read-only**: it counts,
explains and links to the screen where the decision is made, and it never
changes state. Decisions are made on the screens it links to.

| Queue | Source | Where decisions are made |
|---|---|---|
| Route approval | `route_prod` approval queue, by quality band; split into re-entry candidates and others | Phase 3 → Re-Entry Review (re-entry candidates); **Review → Other proposals** (the rest) |
| Re-entry | re-entry queue, by outcome with the next action for each outcome | Phase 3 → Re-Entry Review |
| Synthetic stops | synthetic stops awaiting review | **Review → Synthetic stops** |
| Needs work | deployed route directions with `cleanliness_status = 'dirty'` | Sample Region Audit (inspection only) |
| Coverage gaps | open coverage gaps | Phase 3 → Coverage Improvement |
| Decision log | `console.phase_decisions` | n/a (history) |

## The decision pattern: preview → confirm → override with reason

Every action that changes route data follows the same three steps.

1. **Preview.** The screen computes what would change (for re-entry, the
   GREEK swap preview: stops in/out, coverage before/after, blockers) and
   changes nothing.
2. **Confirm.** A separate button applies it. Clean previews can be confirmed
   directly.
3. **Override with reason.** When the preview has blockers, confirming
   requires an explicit override with a written reason of at least 20
   characters. The reason goes to `route_prod.cleanup_override_audit`, and
   the decision log records `reason_code = 'operator_override'`.

Reject and quarantine always require a reason.

After an action, the result message survives the page rerun (a one-shot
flash in session state). Previously `st.rerun()` erased it before the
operator could read it.

Errors are shown with `error_state`: a plain-language message, with the
technical detail in a collapsed expander. Raw exceptions and function names
are not shown on the page.

## Decision log (`console.phase_decisions`)

The table existed but was **empty**: the approval tabs that wrote to it were
deleted in commit `e0fd494a`, and nothing else wrote to it.

Phase 2 adds best-effort logging from Re-Entry Review
(`_log_decision` in `views/phases/phase3/tabs/re_entry_review_tab.py`):

| Action | `decision` | `reason_code` | `notes` |
|---|---|---|---|
| Confirm swap | `APPROVE` | NULL, or `operator_override` | the override reason, or `re-entry swap vN -> vM` |
| GREEK ship | `APPROVE` | NULL, or `operator_override` | same |
| Reject | `REJECT` | `reentry_reject` | the operator's reason |
| Quarantine | `REJECT` | `reentry_quarantine` | the operator's reason |

Rows use `phase = 3` and `item_id = queue id`. `user_id` is the operator's
UUID when the session has one, otherwise NULL.

Logging is **best effort**. It runs after the swap service's own transaction
has committed. If the insert fails, the action still stands, a warning is
logged, and the page shows a caption saying the decision was not recorded.
The swap service keeps its own authoritative audit in the queue rows; the
decision log is the cross-phase history that the Review page reads.

This is a **BEHAVIOR CHANGE**: these actions now insert one row each into
`console.phase_decisions`. It writes only when an operator acts. No
historical rows were backfilled.

## Review → Synthetic stops

`datamind_console/views/review_synthetic_stops_view.py`, with the logic in
`datamind_console/review/human_review.py`.

* The queue lists `node_prod.nodes` rows with `synthetic_review_state = 'pending'`
 . Stops used by deployed route directions come first.
  Filters: confidence, used / unused (427 are used by no route direction),
  superseded (128), name.
* Selecting a stop shows it on the map next to every stop within 80 m (a real
  stop nearby usually means a duplicate), its facts (method, created by,
  distance to the path), and the route directions that use it, with the
  stop's position in each sequence.
* Decision: **Verify** or **Reject**. Rejection needs a reason of at least
  10 characters. The reviewer must tick "I checked this stop's location on the
  map".
* The write sets `synthetic_review_state` to `verified` or `rejected`. The
  schema already defines those values: "operator approved" and "operator
  rejected, row retained". Route stop sequences are not edited by this screen.
* **Effect on exports** (owner decision, 2026-10-09): a `rejected` stop is
  left out of every Phase 5 export (`stops.txt`, `stop_times.txt`, trip
  headsigns); its routes run through the remaining stops. The rule is one SQL
  condition, `phase5_gtfs/common/exportable_stops.py`, and
  `datamind_console/tests/test_phase5_rejected_synthetic_stops.py` fails if an
  export query that reads `node_prod.nodes` omits it. The production row and
  the route's stored sequence are unchanged.

## Review → Other proposals

`datamind_console/views/review_proposals_view.py`.

* The queue lists the pending `route_prod.approval_queue` rows that have no
  `re_entry_queue_id`. All of them are route candidates
  from `route_raw.route_jobs` ("variant_d shards") that never reached
  production: 103 `unroutable` across 48 candidates, 26 unclassified,
  1 `degraded`. Several candidates have more than one version pending.
* **They cannot be approved from the console.** The swap service refuses rows
  without a re-entry link, and `unroutable` is not a shippable class. The page
  says so, shows the evidence (proposed shape on the map, the queueing
  reasons translated, coverage gaps, geometry severity), and offers one
  decision: **reject with a reason**. Several rows can be selected (for
  example, every older version of a candidate). It is all-or-nothing, with
  one audit row per proposal.
* `sent_to_phase2` exists in the schema, but nothing consumes it, so it is not
  offered.

## Guarantees (both screens)

* The pipeline proposes; a person decides. No automatic or bulk-by-default
  decisions. Every write needs a selected item, a decision, a confirmation
  tick and, for rejections, a reason.
* **Audit is atomic.** The state change and its `console.phase_decisions` row
  are written in one transaction. If the audit insert fails, the decision is
  rolled back.
* Every update is guarded on the row still being `pending`. A second
  decision is refused ("decided elsewhere"), never overwritten.
* Reviewer identity: the signed-in user when there is one. In local mode,
  which has no accounts, a reviewer name is required and written as
  `[reviewer: <name>]` in the audit notes, and approval rows get
  `resolved_by = console:<name>`.
* Audit vocabulary: `phase = 1`, `item_id = node:<uuid>`, `APPROVE/REJECT`,
  `synthetic_stop_verified/rejected`; `phase = 3`,
  `item_id = approval:<uuid>`, `REJECT`, `proposal_rejected`.
* Tests: `test_human_review.py` (fakes: guards, atomic rollback, reason and
  reviewer rules, no approve path) and `test_human_review_integration.py`
  (real Postgres, opt-in with `DATAMIND_DISPOSABLE_TEST_DSN`; passed against a
  disposable schema-only copy on 2026-10-09).
