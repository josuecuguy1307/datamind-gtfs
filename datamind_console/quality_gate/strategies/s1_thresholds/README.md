# S1 — Hard Thresholds

Simplest of the four HADES Quality Gate strategies. For each detected issue, the corresponding fixer proposes a repair with a confidence score. If the confidence meets or exceeds a per-rule threshold, the fix is committed; otherwise the entity is rejected and routed back to its origin phase. Decisions are stateless and independent — no retries, no cross-issue memory, no second opinions.

## Threshold rationale

Thresholds are set based on fix reversibility and risk:
- **Low (0.50–0.60):** Safe, nearly deterministic fixes — clearing garbage refs, stripping non-ASCII from IDs, reverse-geocoding placeholder names
- **Medium (0.65–0.80):** Fixes requiring some judgment — shape gap re-tracing, operator normalization, duplicate merging
- **High (0.85–1.00):** Risky or irreversible fixes — coordinate corrections, schedule generation, agency assignment; `1.00` = always reject (null island, empty calendar)
