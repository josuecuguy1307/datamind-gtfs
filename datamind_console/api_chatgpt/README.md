# DataMind OpenAI Advisory Layer (MVP)

This module implements an advisory-only OpenAI integration for AI Bot / AI Insights snapshots.

## Scope

- Read-only snapshot normalization via `snapshot_builder`.
- Responses API wrapper with structured JSON output.
- Strict schema validation + policy guard enforcement.
- FastAPI routes:
  - `POST /api/ai-bot/analyze-run`
  - `POST /api/ai-bot/review-bot-quality`
  - `POST /api/ai-bot/generate-codex-task`
  - `POST /api/ai-bot/compare-retest`
  - `POST /api/copilot/chat`
  - `GET /api/copilot/sessions`
  - `POST /api/copilot/sessions`
  - `GET /api/copilot/sessions/{id}/messages`
- Audit logging for each advisory call.

## Safety

All requests must include:

- `safety_context.mode = advisory_only`
- `safety_context.execution_authority = runtime_validators_operator`
- `safety_context.destructive_actions_allowed = false`

The module rejects responses that violate advisory-only doctrine.

## Configuration

- `OPENAI_API_KEY`: OpenAI API key (optional if running in mock mode).
- `DATAMIND_CHATGPT_MODEL`: model name (default: `gpt-5.2`).
- `DATAMIND_CHATGPT_MOCK_MODE`: `1/true` for local mock fallback (default: true).
- `DATAMIND_CHATGPT_SNAPSHOT_MODE`: `fixture` (default), `live_try`, or `live_required`.
- `DATAMIND_CHATGPT_AUDIT_FILE`: optional audit JSONL path.

## Local usage

Run API server:

```bash
uvicorn datamind_console.api.server:app --reload --port 8010
```

Example request envelope:

```json
{
  "task": "analyze_latest_run",
  "snapshot": {},
  "operator_context": {
    "note": "post-cleanup verification"
  },
  "safety_context": {
    "mode": "advisory_only",
    "execution_authority": "runtime_validators_operator",
    "destructive_actions_allowed": false
  }
}
```

## Testing

Run module tests:

```bash
pytest -q datamind_console/api_chatgpt/tests
```

Tests default to mock mode and fixture snapshots.

## Dashboard UI Integration Note

- Treat responses as advisory cards only.
- Always surface `confidence`, `risk_flags`, and `insufficient_data_flags`.
- For high-impact recommendations, require explicit operator confirmation in UI before any downstream action.

## Pipeline Copilot Note

`/api/copilot/*` is a phase-aware operational assistant backend. It persists chat sessions/messages, injects pipeline context (phase/step/run/blockers/approvals/artifacts/logs), and routes requests to advisory tasks. The frontend should never hold OpenAI keys or call OpenAI directly.
