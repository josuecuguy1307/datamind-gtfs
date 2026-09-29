# S2: Shadow + Cross-Validator Strategy

## Concept

For each proposed fix, S2 applies it to an in-memory **shadow** copy of the entity, then runs an **independent cross-validator** — a check that is deliberately different from the detection rule. Commit only if the cross-validator passes; otherwise reject and reroute.

## Why This Works

The detection rule says "this entity is broken." The fixer proposes a repair. But the fixer might produce garbage (hallucinated geocode, wrong coord swap, meaningless name). The cross-validator catches that:

- A merged stop must lie inside Ecuador bbox (not just be close to its duplicate)
- A reverse-geocoded name must be >= 5 chars and not a generic street type
- A swapped coord must land on the correct side of the continental divide
- A clamped runtime must be within 15% of `route_length / 25 km/h`
- A default calendar is **never** auto-committed (too risky)

## Architecture

```
gate.py              S2ShadowGate(StrategyBase) — orchestrates detect/fix/decide
shadow.py            Shadow dataclass + _apply_fix() — in-memory entity mutation
cross_validators.py  18 cross-validators + CROSS_VALIDATORS registry
```

## Cross-Validator Coverage

| Rule Name | Cross-Validator | Always Rejects? |
|-----------|-----------------|-----------------|
| stop_name_placeholder | xv_reverse_geocode | No |
| stop_name_empty | xv_reverse_geocode | No |
| stop_name_uuid_prefix | xv_reverse_geocode | No |
| stop_coords_outside_bbox | xv_swapped_coords | No |
| stop_coords_null_island | xv_null_island | Yes |
| stop_ref_garbage | xv_garbage_ref | Yes |
| stop_duplicate_nearby | xv_merged_stop | No |
| route_too_few_stops | xv_merged_route_fragment | No |
| route_no_schedule | xv_no_schedule | Yes |
| shape_gap_too_large | xv_shape_retrace | No |
| shape_self_intersection | xv_shape_self_intersection | Yes |
| route_name_garbage | xv_reconstructed_route_name | No |
| short_name_collision | xv_short_name_dedup | No |
| operator_inconsistency | xv_canonical_operator | No |
| unrealistic_runtime | xv_clamped_runtime | No |
| calendar_no_active_days | xv_default_calendar | Yes |
| fare_missing_agency | xv_assigned_agency | No |
| non_ascii_id | xv_normalized_ascii_id | No |

## Expected Benchmark Behavior

- **Higher precision than S1**: cross-validators catch fixes that pass a confidence threshold but are geometrically/semantically wrong
- **Lower autonomy than S1**: more rejections when cross-validator disagrees with fixer
- **Moderate wall time**: cross-validators are fast (no API calls, just local checks)
- **Best safety-per-autonomy ratio**: the hypothesis is that S2's precision gain outweighs its autonomy loss

## Hypothesis

S2 will differ from S1 primarily in rejecting fixes that S1 would accept on confidence alone — specifically reverse-geocoded names that are too short or generic, coordinate swaps that land in the wrong province, and runtime clamps that don't match route geometry. The cross-validators add ~5 "always reject" rules for fix types that are genuinely unsafe to auto-commit (null island, garbage ref, self-intersection, missing schedule, default calendar), which S1 might accidentally commit if the fixer returns a high confidence. The net effect: S2 will auto-fix fewer issues but the ones it does fix will be correct more often.
