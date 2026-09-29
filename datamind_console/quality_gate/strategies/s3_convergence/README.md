# S3 — Two-Pass Convergence Strategy

## Concept

Run the quality gate on a canton. Apply **all** proposed fixes speculatively to an in-memory working copy (never touching prod). Re-run the gate on the fixed copy. If pass 2 finds no new issues AND doesn't want to undo anything pass 1 did → **commit all fixes**. Otherwise, revert everything, tighten thresholds, retry. Max 3 iterations.

This is the mathematical idea of a **fixed point** — a fix is valid only if the system is stable after applying it.

## Architecture

```
s3_convergence/
  __init__.py         # Public API: S3ConvergenceGate
  gate.py             # Main gate loop + fixer registry
  working_copy.py     # Deep-copy entity store for speculative mutation
  convergence.py      # Pass diffing, convergence/divergence detection
  test_s3.py          # 20 unit + integration tests
```

## Key Design Decisions

- **Binary convergence at the canton level.** Either all fixes converge (commit all) or none do (revert all). No partial commits.
- **Permissive per-fix, strict at the loop.** `decide()` accepts any fix with confidence ≥ 0.5. The convergence check is the real filter — it catches fix-induced cascades.
- **Deferred fixes** stay as open issues for the next pass, letting post-fix context influence low-confidence decisions.
- **Divergence = oscillation.** If recent passes collectively undo >2 prior fixes, the system is flip-flopping. Bail out with `verdict=fail`.

## Smoke Test Results

```
Canton: cayambe (6 stops, 2 routes)
Injected: 3 bad names, 1 garbage ref, 1 short route, 1 missing schedule

Pass 1: 8 issues detected, 6 fixes applied to working copy
Pass 2: 2 issues remain (unfixable), 0 new, 0 undone → CONVERGED

Verdict: pass_with_fixes
Auto-fixed: 6 | Remaining: 2 | Iterations: 2
```

## Hypothesis

**S3 will outperform S1 (threshold-based)** when fixes interact — e.g., merging duplicate stops could break a route's stop count, or renaming a stop could create a new near-duplicate. S3 catches these cascades because the second pass re-validates after all mutations.

**S3 will fail when** fixes are self-consistent yet semantically wrong — e.g., merging two legitimately distinct stops that happen to be close together and similarly named. The merged version passes all rules fine; it's just factually incorrect. S3 has no semantic oracle — convergence validates structural consistency, not ground truth.
