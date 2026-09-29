# Phase 4.5 — Direction Construction (PROMETHEUS)

This package is the canonical owner of `route_prod.routes.direction_id`.
Phase 5 reads it; Phase 5 does **not** compute it. The contract is in
[`workspace/skills/direction_construction.md`](../../workspace/skills/direction_construction.md).

Four terminal states, one of which every route exits Phase 4.5 in:

- `paired` — real bidirectional pair detected, both directions registered
- `synthesized` — reverse direction constructed via lateral-vector logic
- `dr_confirmed_mono` — Deep Research confirmed one-directional service
- `operator_pending` — score in `[0.45, 0.60)` ambiguous band; queued for human review,
  `direction_id` stays NULL, route is excluded from Phase 5 export

Stages 1 (pair detection) and 2 (hard gate) are landed. Stages 3 (synthesis),
4 (DR escalation), and 6 (rollback CLI) are stubs that raise `NotImplementedError`
in the current PR.
