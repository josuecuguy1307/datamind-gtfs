# AI Insights Foundation (Phase 1 + Phase 3)

## Scope implemented
- Append-only AI telemetry for Phase 1 and Phase 3 runs.
- Composite baseline scoring for Phase 1 and Phase 3 quality.
- Phase 3 sequence quality heuristic detector (suggestion-only).
- Optional LightGBM adapter with safe fallback when model/artifact is missing.
- Model-readiness gate counters for baseline vs advanced-model progression.
- AI Bot panel in Insights view with trends, readiness, manual train/eval hooks, and supervision tools.
- Operator grading capture linked to explicit run/set context.
- Self-review/meta-quality metrics for warning usefulness/noise proxies.
- Latest-vs-recent run comparison with configurable regression flags.
- Telemetry sanity diagnostics (ordering, duplicates, malformed/missing fields).
- Codex patch-task context generator (structured JSON package).

## Storage paths
- Run logs: `data/ai_insights/run_logs.jsonl`
- Model metric history: `data/ai_insights/model_metrics.jsonl`
- Train/eval events: `data/ai_insights/train_events.jsonl`
- Baseline model artifacts: `data/ai_insights/models/*.txt`

## DB tables (optional but supported)
- Migration file: `datamind_console/sql/006_ai_insights_bot_logs.sql`
- Tables:
  - `ai.ai_bot_run_logs`
  - `ai.ai_bot_model_metrics`
  - `ai.ai_bot_train_events`

If DB tables exist, AI Insights writes there and still mirrors to JSONL.
If DB tables are missing, the system falls back to JSONL only.

## Key modules
- `datamind_console/ai_insights/config.py`
- `datamind_console/ai_insights/storage.py`
- `datamind_console/ai_insights/scoring.py`
- `datamind_console/ai_insights/sequence_quality.py`
- `datamind_console/ai_insights/ml_adapter.py`
- `datamind_console/ai_insights/readiness.py`
- `datamind_console/ai_insights/telemetry.py`
- `datamind_console/ai_insights/service.py`

## Hook points
- Phase 1 run wrappers: `datamind_console/phases/phase1_nodes/client.py`
- Phase 3 run wrappers and sequence-edit methods: `datamind_console/phases/phase3_routes/client.py`
- AI panel UI integration: `datamind_console/views/insights_view.py`

## Safety model
- Recommendation-only analytics.
- No auto-commit of sequence reorder/merge/cleanup actions.
- No phase gate bypass.
- ML prediction/training calls fail gracefully if LightGBM/artifacts are missing.

## Supervision additions
- Operator grading storage path:
  - UI: `Insights -> AI Bot / AI Insights -> Operator Grading`
  - Persisted as append-only run-log events:
    - `event_type = system`
    - `stage = operator_feedback`
    - `payload.feedback` + `payload.target_context`
  - Backed by DB/JSONL using existing AI bot run-log storage.
- Self-review metrics path:
  - Service API: `AIInsightsService.self_review_metrics(...)`
  - Includes warning volume/usefulness trends, false-warning proxy, score stability, low-value suggestion rates, missing-field rate.
- Regression detection:
  - Service API: `AIInsightsService.compare_latest_vs_recent(...)`
  - Includes score/count deltas, template drift, parameter deltas, repeated-failure pattern flags.
- Sequence warning subtype calibration:
  - Phase 3 logs now carry `warning_tags` and `warning_subtypes`.
  - Trend table/chart exposed in AI Bot panel.
- Telemetry sanity checks:
  - Service API: `AIInsightsService.telemetry_sanity_checks(...)`
  - Read-only checks for ordering, duplicate run IDs/contexts, malformed fields, impossible metrics.
- Codex patch-task context:
  - UI action: `Generate Codex patch task context`
  - Service API: `AIInsightsService.generate_codex_patch_context(run_context_key=...)`
  - Output includes phase/stage/run ids, metrics snapshot, warning subtype breakdown, operator feedback, suspected issue type, suggested patch scope, and safety reminders.

## Manual train/eval flow
- Open `Insights` page.
- In `AI Bot / AI Insights` -> `Manual Train/Eval` tab:
  - Choose task: `phase1_quality_score` or `phase3_sequence_risk`.
  - Click `Train baseline (LightGBM)` or `Evaluate baseline`.
- Results are written to:
  - `ai.ai_bot_model_metrics` / `model_metrics.jsonl`
  - `ai.ai_bot_train_events` / `train_events.jsonl`

## Suggestion-only constraints (still enforced)
- Sequence detector outputs warnings/scores/reorder recommendation only.
- No automatic reorder commit.
- No automatic merge/direction-binding/cleanup commit.
- No destructive automation added.

## Apply migration
```bash
python datamind_console/scripts/00_migrate.py
```
