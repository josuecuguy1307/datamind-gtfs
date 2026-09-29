# Orchestrator Replan Migration (Role Model V2)

## Scope

This migration retires the old orchestration assumptions and applies the role-based Operator Orchestrator model.

## What Was Removed/Disabled

- Legacy UI behavior based on `Use DB Backend` toggle and dual file/DB runtime path in active Operator Orchestrator view.
- Legacy step naming/flow that mixed advisory interpretation and execution semantics.
- Implicit dispatch path without explicit operator dispatch decision metadata.
- Package-level dependency on the old orchestrator exports as the active runtime surface.

## What Was Replaced

- New role-safe policy guard module:
  - `datamind_console/orchestrator/role_policy.py`
- Explicit session state machine contracts:
  - `datamind_console/orchestrator/session_state_machine.py`
- Structured V2 event logging schema (`events_v2.jsonl`):
  - `datamind_console/orchestrator/session_event_log.py`
- Session artifact registry with typed artifacts:
  - `datamind_console/orchestrator/artifact_registry.py`
- Backend V2 role-model orchestration service:
  - `datamind_console/orchestrator/role_model_service.py`
  - Includes strict state prerequisites and transition validation per step.
  - Includes explicit blocked-health operator override path before advisory continuation.
- Updated DB session runner flow with policy checks, event logging, artifact registration, and dispatch confirmation guard:
  - `datamind_console/orchestrator/session_runner.py`
  - Mandatory steps are non-skippable; skip attempts fail the session.
  - Dispatch execution is denied without recorded operator dispatch decision.
- Updated Operator Orchestrator UI labels/flow to role model and V2-only mode:
  - `datamind_console/views/operator_orchestrator_view.py`

## Compatibility Notes

- Legacy `datamind_console/orchestrator/service.py` remains in repository for compatibility with existing tests/modules, but it is no longer the active orchestrator model for the dashboard role workflow.
- Session/event artifacts are now logged under V2 paths in `datamind_console/orchestrator_logs`.

## Feature Flag

- `ORCHESTRATOR_V2_ROLE_MODEL=true` (default behavior in current implementation).
- When disabled (`false`), the dashboard view blocks orchestration and shows that legacy mode is retired.

## Boundary Guarantees Enforced

- No gate bypass.
- No auto-merge bind.
- No auto-reorder commit.
- No silent destructive cleanup.
- No auto-dispatch patch task without operator confirmation.
- No auto-apply generated code without operator confirmation.
- Advisory layers cannot execute critical actions.
- `n8n_external` role is notify-only.
- Out-of-order step execution is denied by state prerequisites.
