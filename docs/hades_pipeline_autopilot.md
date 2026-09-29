# HADES Supervised Pipeline Autopilot (Phase 1/2/3)

This document describes the supervised autopilot runtime added in:

- `datamind_console/orchestrator/pipeline_autopilot.py`
- `datamind_console/orchestrator/tests/test_pipeline_autopilot.py`

The implementation is pipeline-first: execute technical substeps, enforce gates/validators, route blockers, manage bounded retries, and resume after human resolution.

## Architecture

### Core runtime

`SupervisedPipelineAutopilot` is the coordinator. For each step attempt it performs:

1. `step_started` event + attempt counter increment
2. runtime executor call
3. validator call
4. AI Bot hook call (always on)
5. ChatGPT interpretation call (warning/block/anomaly only)
6. policy + authority decision (`auto-advance`, `retry`, `pause`, `divert`)
7. persistence to in-memory run state:
   - step execution record
   - event log
   - artifacts
   - block reason (if blocked)
   - approval queue items (if required)

### Authority model (enforced)

- Runtime + Validators: execution/gate authority
- AI Bot: telemetry/scoring/proposals only
- ChatGPT API: interpretation/prioritization only
- Operator: required for critical approvals

Guardrails implemented:

- no gate bypass (`GATE_BYPASS_ATTEMPT` blocks and logs)
- no automatic reorder apply without approval
- no automatic destructive cleanup without approval
- no automatic merge bind without approval
- no silent state mutation (all major transitions emit events)
- no infinite retries (bounded retry engine)

## Step Registry Format

Step registry is declarative (`build_default_pipeline_step_registry()`), each step declaring:

- `phase`
- `step_id`
- `name`
- `automation_level`
- `executor`
- `validator`
- `ai_bot_hooks`
- `chatgpt_interpretation_triggers`
- `retry_policy`
- `pause_conditions`
- `resume_behavior`
- `artifacts_expected`
- `approval_type` (when applicable)
- `next_step_on_success`
- `diversion_rules`

Registry covers Phase 1/2/3 baseline steps including the critical `P3.2_SEQUENCE_STEP20` path and `P3.2 -> P1.3b -> optional P2.1 partial -> P3.2` resume loop.

Default sequential routing now includes manual/approval checkpoints:

- `P1.1 -> P1.2 -> P1.3a -> P1.4`
- `P2.1 -> P2.2 -> P2.3 -> P3.1`
- `P3.2` may branch to `P3.3` when reorder proposal requires approval.

## Automation Levels

- `AUTO_SAFE`
- `AUTO_WITH_CHECKPOINT`
- `APPROVAL_REQUIRED`
- `MANUAL_ASSISTED`

Runtime behavior:

- `AUTO_*`: executes and auto-advances unless policy/validator blocks
- `APPROVAL_REQUIRED`: technical prep may run, then approval item is created and run pauses
- `MANUAL_ASSISTED`: runtime assists (prefill/prioritization) then pauses for human work

Manual-assisted continuation is explicit:

- `mark_manual_step_resolved(...)` records operator resolution and resumes safely.

## Policy Profiles

Implemented profiles:

- `conservative`
- `balanced`
- `aggressive_supervised`

Profile controls:

- warning-only pass behavior (pause vs auto-advance)
- retry budget (`max_retry_bonus`)
- non-critical degradation tolerance metadata

## Block Reason Codes

Structured `block_reason` is persisted for blocked attempts with:

- `code`
- `severity`
- `summary`
- `validator_evidence`
- `ai_bot_metrics_snapshot`
- `chatgpt_interpretation`
- `recommended_next_action`
- `required_approval_type`
- `diversion_target`

Implemented code enum includes:

- extraction failures (`EXTRACTION_EMPTY`, `EXTRACTION_LOW_COVERAGE`, `REPEATED_EXTRACTOR_FAILURE`)
- phase chain failures (`NORMALIZE_FAILED`, `FEATURES_INVALID`, `CLUSTERING_DEGENERATE`, `RESOLVE_ZERO_RESULTS`)
- contract guard (`VALIDATOR_PAYLOAD_CONTRACT_MISMATCH`) for executor/validator payload disagreement
- semantic failures (`SEMANTIC_PIPELINE_FAILED`, `SEMANTIC_REGRESSION_HIGH`)
- Step20 blockers (`STEP20_UNMATCHED_BLOCKING`, `STEP20_AMBIGUOUS_BLOCKING`, `STEP20_SEQUENCE_QUALITY_LOW`, `STEP20_REORDER_SUGGESTED_BLOCKING`)
- geometry/rank failures (`GEOMETRY_FAILED`, `GEOMETRY_QUALITY_CRITICAL_LOW`, `RANKING_FAILED`)
- critical-action blockers (`APPROVAL_REQUIRED`, `DESTRUCTIVE_CLEANUP_APPROVAL_REQUIRED`, `MERGE_BIND_APPROVAL_REQUIRED`)
- governance (`GATE_BYPASS_ATTEMPT`)

## Approval Item Schema

`ApprovalItem` fields:

- `approval_id`
- `run_id`
- `phase`
- `step_id`
- `approval_type`
- `status` (`pending|approved|rejected|expired|superseded`)
- `created_at`
- `created_by_system`
- `evidence_payload`
- `risk_summary`
- `recommended_action`
- `operator_decision`
- `operator_id`
- `decision_at`

Approval types:

- `RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS`
- `APPLY_REORDER_PROPOSAL`
- `RUN_DESTRUCTIVE_CLEANUP`
- `APPROVE_FINAL_ROUTE_OR_MERGE_BIND`
- `PROMOTE_NODE_BATCH`

## Resume Loop Behavior

### Critical inter-phase loop

When `P3.2_SEQUENCE_STEP20` is blocked by unmatched/ambiguous:

1. step is blocked with structured reason
2. diversion starts to `P1.3B_NEW_NODES_FROM_PHASE3`
3. approval item is created (`RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS`)
4. operator resolves/approves
5. runtime emits `resume_triggered`
6. optional `P2.1_SEMANTIC_PIPELINE_RUN` partial rerun (rule-based or explicit override)
7. runtime reruns `P3.2_SEQUENCE_STEP20`
8. emits `resume_completed` and continues if gate passes

Additional enforcement:

- Step20 diversion approval now requires `promote_confirmed=true` before resume.

Events emitted for traceability:

- `diversion_started`
- `diversion_completed`
- `resume_triggered`
- `run_resumed`
- `resume_completed`

## Event Contracts

Each event includes:

- `timestamp`
- `run_id`
- `phase`
- `step_id`
- `payload`
- `correlation_id`
- `trace_id`

Event types implemented:

- `run_started`
- `run_resumed`
- `step_started`
- `step_completed`
- `step_warning`
- `step_retry_scheduled`
- `step_blocked`
- `approval_item_created`
- `approval_item_resolved`
- `diversion_started`
- `diversion_completed`
- `resume_triggered`
- `resume_completed`
- `run_completed`
- `run_failed`

## Rollout / Feature Flag

Autopilot class supports feature flag:

- env var: `HADES_PIPELINE_AUTOPILOT_ENABLED`
- default: enabled (`true`) unless explicitly set off

Recommended rollout sequence:

1. deploy with `HADES_PIPELINE_AUTOPILOT_ENABLED=false`
2. enable for dry-run/test tenants
3. monitor event stream + approval queue behavior
4. progressively enable broader runtime integration

## Persistence

Run/session state is persisted as JSON snapshots under:

- `datamind_console/orchestrator_logs/autopilot_runs/`

Persistence includes run state, attempts, events, approvals, diversion stack, and artifacts for reload/resume continuity.

DB persistence is also supported (feature-flagged) through dedicated tables:

- `console.pipeline_autopilot_runs`
- `console.pipeline_autopilot_step_attempts`
- `console.pipeline_autopilot_events`
- `console.pipeline_autopilot_approvals`
- `console.pipeline_autopilot_idempotency`
- `console.pipeline_autopilot_queue`
- `console.pipeline_autopilot_alerts`

Schema migration file:

- `datamind_console/sql/008_pipeline_autopilot.sql`

This enables multi-user concurrency, queryable audit trails, and stronger restart recovery than JSON-only snapshots.

## Background Worker / Queue

Autonomous execution (without active Streamlit session) is provided by:

- `datamind_console/orchestrator/autopilot_worker.py`

Worker behavior:

- claims queued runs from `pipeline_autopilot_queue`
- advances runs in bounded bursts
- refreshes lease/heartbeat while processing
- records completion/failure on queue rows
- can re-enqueue still-running runs for continued progress

## Operator Identity + Decision Signing

Manual resolutions and approval resolutions now require authenticated operator identity:

- mandatory `operator_id`
- role check against allowed roles (`HADES_AUTOPILOT_ALLOWED_OPERATOR_ROLES`)
- no implicit fallback role injection when `operator_roles` is missing
- signed decision payload (`HADES_OPERATOR_SIGNING_SECRET`)

Signed payload hash is persisted in approval records (`decision_signature`) and manual resolution metadata.

## Idempotency

Idempotency keys are persisted for:

- step execution attempts (`step:<run_id>:<step_id>:<attempt_no>`)
- approval resolution actions
- approval apply executors (`approval_apply:<approval_id>`)
- manual-assisted resolution confirmations

Duplicate replays are suppressed and logged instead of re-applying critical actions.

## Real Validator Adapters

Validator adapters are now runtime-side (not UI-parsed heuristics) via:

- `build_phase_client_validator_bridge()` in `pipeline_autopilot.py`

Executors produce `validator_payload` directly from runtime outputs; validators consume that payload to produce structured gate outcomes.

## Full Live Executor Bridge Coverage

`build_phase_client_executor_bridge()` now provides concrete runtime paths for all registry executor names (Phase1/2/3), including:

- manual-assisted prefill/support executors
- approval preparation executors
- cleanup preview/apply paths
- Step20 sync to Phase1 review request queue
- reorder proposal/apply paths
- Step40 approve apply path (`phase3_step40_approve_apply`)
- merge proposal/apply paths

Unsupported/missing runtime clients fail closed with explicit runtime errors (no permissive placeholder pass).

## SLO / Alerting

Autopilot exposes orchestration health checks (`evaluate_slo_alerts`) for:

- stuck run detection
- retry exhaustion
- long-paused approvals
- missing events/artifacts

Alerts are emitted as events and can be persisted to `pipeline_autopilot_alerts`.

## Feature Flags

## Pipeline Copilot Integration

A phase-aware operator chat panel is now available in dashboard navigation:

- `Pipeline Copilot` page in `datamind_console/views/pipeline_copilot_view.py`

Backend endpoints:

- `POST /api/copilot/chat`
- `GET /api/copilot/sessions`
- `POST /api/copilot/sessions`
- `GET /api/copilot/sessions/{id}/messages`

Security model:

- frontend calls backend only
- backend keeps API keys and injects policy/context
- responses remain advisory-only (no gate override, no destructive auto-execute)

Context panel supports phase/step/run-aware payloads (block reasons, AI metrics, logs, artifacts, approvals) and can prefill from persisted autopilot run snapshots.

Capability rollout controls are environment-driven:

- `HADES_AUTOPILOT_FF_LIVE_BRIDGE`
- `HADES_AUTOPILOT_FF_DB_PERSISTENCE`
- `HADES_AUTOPILOT_FF_AUTO_RESUME`
- `HADES_AUTOPILOT_FF_POLICY_PROFILES`
- `HADES_AUTOPILOT_FF_WORKER`
- `HADES_AUTOPILOT_FF_SLO_ALERTING`

Persistence backend mode:

- `HADES_AUTOPILOT_PERSISTENCE_BACKEND=json|db|hybrid`

## Runtime Integration Hooks

`build_phase_client_executor_bridge(...)` provides optional bridge functions so autopilot can call existing phase clients instead of replacing runtime implementations.

This keeps execution authority with current phase runtimes while centralizing orchestration mechanics in the autopilot layer.

Additional integration adapters:

- `AdvisoryChatGPTInterpreter`: calls ChatGPT advisory API (`AdvisoryService`) only for warning/block/anomaly interpretation.
- `build_ai_bot_telemetry_hook()`: builds AI Bot hook from existing AI Insights telemetry comparison service.

## ChatGPT Step Metadata Persistence

Each step attempt now persists enriched ChatGPT metadata inside `chatgpt_snapshot`, including:

- `task`
- `trigger`
- `model`
- `latency_ms`
- `token_usage`
- `snapshot_id`
- `source`
- `schema_name`
- `prompt_package` (task/safety/snapshot/operator context + rendered prompts)

This enables step-level auditability of advisory calls and faster triage/export in UI.

## P1.2 Resolve Contract Guard

To prevent false `RESOLVE_ZERO_RESULTS` blocks caused by payload-field mismatch:

- executor now emits canonical `validator_payload.resolved_count` and alias `resolved_total`
- validator reads aliases (`resolved_count`, `resolved_total`, `n_resolved`) during migration
- validator performs schema + consistency checks before zero-result blocking
- when payload and execution outputs disagree, runtime emits `VALIDATOR_PAYLOAD_CONTRACT_MISMATCH` instead of `RESOLVE_ZERO_RESULTS`
