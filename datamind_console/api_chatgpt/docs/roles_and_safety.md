# Roles and Safety

## Authority Split

- Runtime + validators: execution authority and safety gates.
- AI Bot / AI Insights: telemetry, scoring, readiness, trends.
- OpenAI advisory layer (this module): analyst/explainer/prioritizer/patch-task generator.
- Codex: patch implementation agent.
- Human operator: final confirmation for high-impact actions.

## Hard Boundaries

This module must not:
- bypass phase gates
- auto-pass sequence steps
- auto-commit reorder
- auto-bind merge directions
- perform destructive cleanup
- write critical workflow actions to DB

This module may:
- analyze snapshots
- explain risks and evidence
- prioritize and suggest next actions
- generate Codex patch-task prompts

## Enforcement Layers

- `policy_guard.validate_safety_context`: blocks non-advisory request envelopes.
- `response_validator`: strict JSON schema validation for all task outputs.
- `policy_guard.enforce_post_response`: rejects execution framing, task mismatch, invalid mode, and unknown evidence refs.
- `audit_logger`: records call metadata and validation outcomes for traceability.
