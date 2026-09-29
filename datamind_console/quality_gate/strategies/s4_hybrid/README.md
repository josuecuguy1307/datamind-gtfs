# S4 Hybrid — Threshold + Shadow Cross-Validator + Convergence

## Overview

S4 layers **all three** defense mechanisms. Every proposed fix must survive:

1. **Layer 1 — Threshold (S1-style):** confidence >= per-rule threshold. Cheapest check, runs first to short-circuit.
2. **Layer 2 — Shadow Cross-Validator (S2-style):** independent structural/semantic check on a post-fix shadow copy.
3. **Layer 3 — Convergence (S3-style):** after all L1+L2-approved fixes are applied, re-run the full gate. Commit only if the system reaches a fixed point (no new issues, no oscillation).

## Files

| File | LOC | Purpose |
|------|-----|---------|
| `__init__.py` | 8 | Module entry point |
| `gate.py` | ~380 | `S4HybridGate(StrategyBase)` — main gate loop |
| `thresholds.py` | 48 | Per-rule confidence thresholds (L1) |
| `cross_validators.py` | 230 | 18 cross-validators, one per rule (L2) |
| `layered_decision.py` | 130 | `LayeredDecision` dataclass + L1/L2 functions |
| `test_s4.py` | 185 | Unit + integration tests |
| `README.md` | — | This file |

## Design

### Layer ordering = performance

Threshold is an integer comparison (nanoseconds). Cross-validator does local structure checks (microseconds). Convergence re-runs the full gate (milliseconds). Running them in order means most rejections happen at L1, saving L2/L3 work.

### L3 is canton-level

Convergence cannot reject individual fixes — it accepts or rejects the entire batch. If L3 fails (divergence or max iterations), ALL fixes are reverted and the canton fails the gate.

### Self-contained

S4 copies the threshold table, cross-validator registry, working copy, convergence logic, and fixer registry inline. No imports from `s1_thresholds/`, `s2_shadow/`, or `s3_convergence/`. This keeps the benchmark clean.

### Rich diagnostics

Every `GateReport` includes `layer_diagnostics` with:
- `l1_rejections`, `l2_rejections` counts
- `l3_converged`, `l3_diverged` booleans
- `per_layer_breakdown` — count of decisions by `LayerResult` type
- Full `convergence_trace` (issues/fixes/undone per pass)

## Hypothesis

S4 will beat S2 on corruption rate by ~30-50% (threshold pre-filter catches low-confidence garbage before cross-validators even run), but lose ~10-15% autonomy (more rejections = more route-backs). This makes S4 preferable when **data integrity is the primary concern** and operator review capacity exists to handle the increased route-back volume — e.g., initial GTFS exports for a new canton where corruption in the first published feed would damage public trust.
