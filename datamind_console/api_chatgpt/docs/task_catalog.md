# Task Catalog (HADES-first)

## Canonical HADES pipeline tasks (default routing)

## 1) `hades_evidence_consistency_checker`

- Primary usage: contradiction detection across executor/validator/AI Bot/block reason.
- Primary callers:
  - `AdvisoryChatGPTInterpreter` precheck for pipeline warnings/blockers/anomalies.
  - `POST /api/ai-bot/check-evidence-consistency`.

## 2) `hades_pipeline_interpreter`

- Primary usage: dominant-cause classification, branch recommendation, concrete next actions, patch-task recommendation (advisory only).
- Primary callers:
  - Autopilot pipeline diagnostic paths (Phase1/Phase2/Phase3 warning/blocker/anomaly).
  - `POST /api/ai-bot/interpret-pipeline-blocker` (default).
  - `POST /api/ai-bot/prioritize-pipeline-resolution` (default).
  - `POST /api/ai-bot/explain-cleanup-risk` (default).
  - `POST /api/ai-bot/interpret-merge-evidence` (default).
  - Pipeline Copilot diagnostic templates.

## 3) `hades_patch_task_generator`

- Primary usage: controlled patch-task package generation from pipeline evidence.
- Primary callers:
  - `POST /api/ai-bot/generate-codex-task` (default).
  - Pipeline Copilot patch-task template.
  - Orchestrator advisory patch task handler.

## 4) `hades_retest_comparator`

- Primary usage: compare baseline vs retest evidence after tuning/patch changes and recommend `accept|rollback|observe_more|manual_review`.
- Primary callers:
  - `POST /api/ai-bot/compare-retest`.
  - Pipeline Copilot retest comparison template.

## Meta advisory tasks (non-interpreter)

## 5) `analyze_latest_run`

- Endpoint: `POST /api/ai-bot/analyze-run`
- Objective: interpret one normalized AI Bot run snapshot.

## 6) `review_ai_bot_quality`

- Endpoint: `POST /api/ai-bot/review-bot-quality`
- Objective: meta-review warning quality/noise/coverage and tuning bottlenecks.

## Legacy compatibility tasks (opt-in only)

These are retained for backward compatibility and should only be selected explicitly via legacy flags/templates.

- `generate_codex_patch_task`
- `interpret_pipeline_blocker`
- `prioritize_pipeline_resolution`
- `explain_cleanup_risk`
- `interpret_merge_evidence`
