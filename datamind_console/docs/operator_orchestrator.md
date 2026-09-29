# Operator Orchestrator (Supervised Pipeline Autopilot UI)

## Purpose

The Operator Orchestrator UI is now pipeline-first for **Phase 1 / Phase 2 / Phase 3**.

It is designed to run technical substeps automatically and interrupt the operator only at real checkpoints:

- gate/block conditions from runtime validators,
- approval-required critical actions,
- manual resolution workflows (for example Step20 unmatched/ambiguous).

## Authority Model

- Runtime + Validators: execution authority and gate authority
- AI Bot: scoring, telemetry, comparisons, proposals
- ChatGPT API: advisory interpretation/prioritization only
- Operator: critical confirmations only

## Hard Constraints Enforced in UI Flow

- No validator/gate bypass
- No automatic merge bind
- No automatic reorder apply
- No destructive cleanup auto-execution
- No sensitive approve/promote without human confirmation
- No silent mutations without event trace
- No infinite retry loops

## Main UI Sections

1. Runtime configuration

- Policy profile (`conservative`, `balanced`, `aggressive_supervised`)
- Executor mode (`stub_runtime`, `live_runtime_bridge`)
- AI Bot mode and ChatGPT advisory mode
- Start step and pipeline scope inputs

2. Run controls

- Start run
- Advance one step
- Run burst (bounded steps)
- Reset engine

3. Active run overview

- Status, current phase/step, pending approvals, progress
- Step registry with runtime status and attempts
- Current structured block reason

4. Approval queue

- Pending approval items with evidence payload
- Approve/reject actions with operator notes
- Step20 diversion approval requires explicit `promote_confirmed`
- Approval history trace

5. Diversion/resume visibility

- Diversion stack records
- Explicit visibility into inter-phase resume context

6. Traceability panels

- Step attempt log
- Event stream (`run_started`, `step_blocked`, `resume_triggered`, etc.)
- Artifact registry view
- ChatGPT API Inbox + Output panel (phase/step/event-type filters)
- Export selected ChatGPT inbox/output payload as JSON
- Copy prompt package button for operator triage handoff
- Read-only audit timeline (filter by `run_id` / `trace_id`)
- Evidence bundle export for compliance review

7. Manual-assisted resolution controls

- Explicit operator action to mark `MANUAL_ASSISTED` checkpoints resolved
- Safe resume back into sequential pipeline execution

8. Background continuation controls

- Queue run to DB-backed worker (`pipeline_autopilot_queue`)
- Worker executes bursts without requiring active Streamlit browser session

## Critical Loop Visibility

The UI explicitly supports and shows this loop:

`P3 Step20 blocked` -> `P1 New Nodes` -> operator resolution/approval -> optional `P2 partial rerun` -> `P3 Step20 rerun` -> continue

## Step20 Diagnostic Precision

Step20 now emits a versioned detector/scoring diagnostics payload for better triage:

- `sequence_diagnostic_profile_version`
- `sequence_warning_tags` / `sequence_warning_subtypes`
- `step20_diagnostics_payload.dominant_cause` (`node_db_gap`, `matching_ambiguity`, `detector_thresholds`, `merge_evidence`, `unknown`)
- `step20_diagnostics_payload.triage_route`
- threshold snapshot (`step20_diagnostics_payload.threshold_profile`)

The validator still owns gate authority and does not auto-bypass gate decisions.

## Manual Workflow Jump Buttons

The screen includes direct jump actions for human checkpoint work:

- Open P1 New Nodes
- Open P1 Promote
- Open P2 Workspace
- Open P3 Sequence

These are for critical supervised interventions only, not for replacing autopilot execution.

## App Navigation Alignment

- Default landing page is now `Operator Orchestrator`
- `Phases` remains available as manual workspace
- Debug-only sidebar testing controls were removed

## Operator Identity Enforcement

Approval and manual-resolution actions require:

- authenticated `operator_id`
- authorized operator role (configured allow-list, no implicit role fallback)
- signed decision payload persisted in audit records

This is enforced in runtime before approval resolution/apply flows continue.
