# Metrics

Every route number shown in the console comes from `datamind_console/metrics/`.
Pages do not write their own `count(*)`; they call these functions so the same
word always means the same thing. Vocabulary is defined in
[TERMINOLOGY.md](TERMINOLOGY.md).

## Route metrics (`datamind_console/metrics/routes.py`)

One SQL statement, `ROUTE_COUNTS_SQL`, over `route_prod.routes`, optionally
filtered by `province`. Display order is `ROUTE_METRICS`.

| Key | Definition |
|---|---|
| `route_directions_total` | every row of `route_prod.routes`, in any deploy state |
| `route_directions_deployed` | `deploy_status = 'active'` |
| `route_directions_clean` | deployed and `cleanliness_status = 'clean'` |
| `route_directions_ship_eligible` | deployed and (clean or `dirty_reason = 'phase4_catalog_synthesized'`) |
| `route_directions_needs_work` | deployed and `cleanliness_status = 'dirty'` |
| `route_directions_unclassified` | deployed and `cleanliness_status IS NULL` |
| `route_directions_not_deployed` | `deploy_status` is anything other than `active` (including NULL) |
| `lines_deployed` | distinct `service_route_id` among deployed directions |
| `lines_clean` | distinct `service_route_id` among clean deployed directions |
| `route_directions_human_verified` | `human_verified = true`, any deploy state |

Identities that always hold:
`deployed = clean + needs_work + unclassified` and
`total = deployed + not_deployed`.

`deploy_state_breakdown()` returns the same rows grouped by
`deploy_status × cleanliness_status × dirty_reason`, so any headline number can
be traced to its components.

## Stop counts

"Total stops 12,799" on the old audit page was the sum of
`array_length(stop_node_ids)` over Sample Region route directions: **stop
references**. A stop served by several directions is counted once per
direction. On 2026-10-08:

| Sample Region | Stop references | Distinct stops |
|---|---:|---:|
| all deploy states | 12,799 | 5,093 |
| deployed only | 11,322 | 4,499 |

The page now labels the figure *Stop references* and says so in its help text.

## Pipeline metrics (`datamind_console/metrics/pipeline.py`)

| Function | Source | Meaning |
|---|---|---|
| `freshness()` | latest timestamps of extraction runs, node/place updates, review and re-entry activity, published feed | when each layer last changed; the Overview calls the pipeline *dormant* after 30 days without activity (`DORMANT_AFTER_DAYS`) |
| `attention()` | review queues, re-entry queue | pending approvals, blocked re-entry, re-entry kept-current outcomes |
| `approval_status()`, `approval_pending_by_quality()`, `approval_pending_split()` | `route_prod` approval queue | pending vs decided, by quality band; split into re-entry candidates and others |
| `reentry_status()`, `reentry_outcomes()` | re-entry queue | `last_error LIKE 'no_improvement%'` means **the current version was kept**, not a failure |
| `current_feed()` | published feed registry | current GTFS feed and whether its zip exists on disk |
| `recent_activity()` | several queues, grouped by day and kind | what happened recently; purged routes show as `#` + 8 id characters |
| `coverage_gaps()`, `synthetic_stops()` | coverage and synthetic-stop tables | open gaps; synthetic stops awaiting review (no review screen exists yet) |
| `recent_decisions()` | `console.phase_decisions` | the decision log (see [REVIEW.md](REVIEW.md)) |

Re-entry "failed" on 2026-10-08: 280 = 205 kept current (no improvement) + 49
need Phase 2 + 23 no playbook + 3 invalid current shape. The Review page shows
these outcomes separately, with the next action for each.
