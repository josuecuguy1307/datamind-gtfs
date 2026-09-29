# Phase 3 Merge Assist Scoring (v1)

## Core distinction
This helper does not treat opposite directions as exact reverse stop-ID sequences only.

It uses a layered correspondence model:
1. Exact overlap evidence: shared canonical stop IDs and reverse exact sequence when available.
2. Paired-stop correspondence: nearby corridor-equivalent stop pairing (even when stop IDs differ).
3. Corridor reverse evidence: shared geometry/corridor overlap and reverse progression along route shape.

This better handles opposite-side stops, offset platforms, one-way detours, and terminal bay differences.

## Feature groups used in v1
- Sequence/pairing: exact overlap, reverse exact sequence, paired-stop alignment, reverse paired order, endpoint region swap, shared middle corridor alignment.
- Geometry/corridor: shared corridor overlap, reverse corridor progression, path similarity, shape direction opposition, length ratio.
- Metadata/overpass: ref/operator/network match, overpass name similarity, from-to swapped match, relation tag consistency.
- Naming/semantics: normalized route name similarity, endpoint name swap similarity, alias similarity, direction-word conflict flag, name-family match.
- Risk penalties: per-route sequence quality penalties, unmatched/ambiguous penalties, loop/branch suspicion, low evidence coverage.

## Output schema
Each evaluated route pair returns:
- `same_route_family_score` (0-1)
- `opposite_direction_score` (0-1)
- `merge_readiness_score` (0-1)
- `proposed_direction_assignment` (suggestion only)
- `evidence_breakdown` (features, component weights, penalties, diagnostics)
- `review_flags[]`
- `requires_operator_confirmation = true`

## Confidence and limitations
- v1 is heuristic scoring, not supervised pair classification.
- Missing geometry, sparse stop priors, or missing semantics reduce evidence coverage and readiness.
- Ambiguous stop-pair context is approximated when explicit ambiguity labels are unavailable.

## Safety requirement
This module is proposal-only.

It does not auto-bind direction slots, does not bypass merge workflow, and does not perform silent route ownership rewiring. Operator confirmation is always required before any bind action.
