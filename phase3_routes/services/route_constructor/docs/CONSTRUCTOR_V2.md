# Constructor V2

Constructor V2 is an isolated Phase 3 ordering backbone under `src/constructor_v2/`.

It does not replace the current constructor path. The existing Step 20/30 pipeline remains unchanged. V2 is file-driven and benchmark-first:

1. Normalize candidate stops while preserving source mappings and duplicate diagnostics.
2. Try a road-network baseline order:
   - `optimized_route` when the local Valhalla exposes it
   - otherwise a road-network greedy fallback using pairwise `/route` costs
3. If the baseline is weak, build a full road-cost matrix:
   - `sources_to_targets` when available
   - otherwise pairwise `/route` fallback with cacheable matrix output
4. Solve fixed-start / fixed-end ordering with OR-Tools, with optional-node penalties for weak candidates.
5. Run a disciplined local refiner on the accepted order.
6. Generate final Valhalla geometry.
7. Score the result with explicit validators and confidence labels.

Primary benchmark inputs are the existing Valle artifacts in `CONSTRUCTIOR/`, especially:

- `valle_v5_4_sequences_final*.json` as current sequence input
- `valle_v5_4_COHERENT_v17*.json` as reviewed reference
- `sequence_overrides_v1.json` for route context

Generated benchmark outputs land under `constructor_artifacts/constructor_v2_v1/`.
