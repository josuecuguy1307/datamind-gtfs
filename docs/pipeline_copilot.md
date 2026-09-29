# ML DATAMIND GTFS Pipeline Copilot (Phase-Aware Advisory API)

This document describes the phase-aware Pipeline Copilot backend implementation for ML DATAMIND GTFS operations. Existing `hades_*` and `datamind_*` technical identifiers are retained for compatibility.

## Objective

Provide an operational assistant for Phase 1 / Phase 2 / Phase 3 pipeline work:

- Phase 1 extraction tuning and node-quality triage
- Phase 2 semantics + dedup/cleanup risk interpretation
- Phase 3 Step20 blocker triage, sequence quality, reorder proposal review, merge evidence interpretation

The copilot is advisory-only by default and does not execute destructive pipeline actions.

## Security Architecture

The frontend never calls OpenAI directly.

Request flow:

1. Dashboard Copilot view (Streamlit frontend)
2. Backend Copilot API (`/api/copilot/*`)
3. Existing server-side advisory service (OpenAI API/mocks)

Backend responsibilities:

- API key handling
- prompt/context injection
- request validation
- safety-mode enforcement (`advisory_only`)
- response + metadata logging
- session/message persistence

## Backend Endpoints

Implemented endpoints:

- `POST /api/copilot/chat`
- `GET /api/copilot/sessions`
- `POST /api/copilot/sessions`
- `GET /api/copilot/sessions/{id}/messages`

`POST /api/copilot/chat` supports:

- non-stream response (JSON)
- stream mode (SSE-compatible `text/event-stream`) with `message_start`, `delta`, `message_end`

## Data Model and Persistence

Service file:

- `datamind_console/api_chatgpt/services/copilot_service.py`

Storage mode:

- DB-first, file fallback
- DB tables are optional and feature-compatible with fallback mode

Migration:

- `datamind_console/sql/009_pipeline_copilot.sql`

Tables:

- `console.pipeline_copilot_sessions`
- `console.pipeline_copilot_messages`

Fallback file store:

- `datamind_console/orchestrator_logs/pipeline_copilot/sessions/*.json`

## Context Injection Model

Each chat request can inject operational context:

- `active_phase`
- `active_step`
- `run_id`
- `entity_ids`
- `validator_status`
- `block_reason`
- `ai_bot_snapshot`
- `approval_items`
- `artifacts_summary`
- `logs`
- `previous_runs_compare`
- include toggles for logs/metrics/block_reason/artifacts/compare

Run-aware enrichment:

- when `run_id` is provided, the backend reads autopilot run snapshots from `orchestrator_logs/autopilot_runs`
- latest step record, approvals, artifacts, and event excerpts are used to fill missing context fields

## ChatGPT Task Routing

Copilot requests are mapped to pipeline-specific advisory tasks:

- `hades_pipeline_interpreter` (default for blocker/anomaly/risk interpretation)
- `hades_evidence_consistency_checker` (contradiction-focused checks)
- `hades_patch_task_generator` (patch-task drafting)
- `hades_retest_comparator` (baseline vs retest comparison)
- Legacy tasks are available only via explicit legacy template selection.
- `review_ai_bot_quality` / `analyze_latest_run` remain available for meta-analysis flows.

Template buttons map directly to these tasks; free-text messages use phase/keyword routing.

## Frontend status

View file:

- `datamind_console/views/pipeline_copilot_view.py`

The current console does not expose this backend as a `Pipeline Copilot` navigation page. The visible **AI Assistance** screen only opens an opt-in local interactive CLI; it does not call these backend endpoints. Keep this API behind its normal server-side configuration and authentication path until a separately approved frontend integration is added.

Layout:

- Left: Sessions + quick templates
- Center: Chat thread + streaming response rendering + retry + copy + optional "Send to Codex task"
- Right: Context panel + context toggles + run snapshot prefill

## Advisory Safety Boundaries

The copilot does not grant execution authority.

- no gate override
- no reorder apply execution
- no destructive cleanup execution
- no merge bind execution

Recommendations remain operator-reviewed.

## Rollout Notes

1. Apply migrations (`datamind_console/scripts/00_migrate.py`) to enable DB persistence.
2. Keep `DATAMIND_CHATGPT_MOCK_MODE=true` for dry runs; switch to real mode only after backend key/config validation.
3. Optionally set `DATAMIND_COPILOT_API_BASE_URL` for frontend-to-backend routing.
4. Use `DATAMIND_API_KEY` if backend auth guard is enabled.
