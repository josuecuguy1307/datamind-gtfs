# How DataMind is operated: application, tooling and agents

DataMind runs in two ways:
- a Streamlit console for operators;
- an operator/agent workflow in a terminal (skills, scripts, MCP, Claude Code or Codex).

Both are real. This page keeps them separate so docs and UI never confuse "an agent can do this in the repository" with "the product does this".

Verified against the code on 2026-10-08. Detail per component is in `docs/CURRENT_CAPABILITIES.md`.

## A. Native DataMind application functionality

What the Streamlit console (`datamind_console/app.py`) and its APIs do by themselves.

- **Monitoring and review surfaces per phase.**
  - Phase 1 node sets.
  - Phase 2 places and evidence.
  - Phase 3 route constructor, coverage, re-entry review and GREEK ship.
  - Phase 4 naming.
  - Phase 5 GTFS build steps.
- **Human gates with explicit confirmation.**
  - Re-Entry Review: preview → confirm, or override with a required reason.
  - Revise Current GTFS edits behind confirmation checkboxes.
  - GTFS Ops: structure check → local publish; AWS deploy behind a checkbox plus the pre-deploy enforcer.
- **Read-only audits and overviews**: Sample Region audit, dashboard state, review overview.
- **FastAPI services**: Geo API with hybrid semantic search, GTFS exports, an advisory copilot (mock by default).
- **LLM calls the product can make**: advisory only (constructor advisor, copilot), mock by default. They return suggestions and never write production data.

## B. Developer / operator orchestration tooling (in the repository, not a screen)

- **Skills** (`workspace/skills/`, 50 files): the procedures used to run phases end to end, including deep-research templates (06a/06b/06c, 14, 17) and the HADES skills.
- **Operational scripts**: about 128 in `scripts/`, plus per-phase `scripts/` folders.
  - Many mutate data: `bulk_approve_promote.py`, `bulk_ship_greek.py`, `force_promote_*.py`, `fix_*.py`.
  - They are administrative/bulk tools. They **bypass the console's per-item review** and are the main path by which production data changed in 2026.
- **Schema administration**: `python -m datamind_console.db.schema_cli status|plan|apply`. This is the only place schema units are applied (added 2026-10-08).
- **Pipeline autopilot** (`datamind_console/orchestrator/pipeline_autopilot.py`): a step catalog for Phases 1–3 with automation levels (AUTO_SAFE, AUTO_WITH_CHECKPOINT, APPROVAL_REQUIRED, MANUAL_ASSISTED) and gate-bypass detection.
  - Run via `autopilot_worker.py`.
  - **It has no Streamlit screen.** The "Operator Orchestrator" view it refers to was never wired (commit `1fb6a471`).
  - Last session: 2026-03-03.
- **Local runner** (`local_runner/`): headless prompt automation (`claude -p`, `codex exec`); last jobs 2026-03.

## C. External Claude / Codex operation

- Claude Code (and Codex) work **in the repository**, guided by `AGENTS.md`, `CLAUDE.md` and the skills.
  - They run scripts and engines from the terminal with the operator's approval.
  - The phases were in practice run this way.
- The console's **AI Assistance** page can *open* such a terminal locally with a reviewed scope. Only when `ML_DATAMIND_LOCAL_CLI_LAUNCH_ENABLED=true`, in LOCAL mode, with a written scope. It grants no extra permission; the CLI keeps its own approvals.
- **Deep research** is human-run. HADES writes prompt batches, a person runs them in a research assistant and pastes the answers back, and code validates every landmark.
  - The pipeline never calls a research service by itself.

## D. Read-only MCP database tools

- `tools/mcp_server.py` (FastMCP `datamind`, stdio), registered for Claude Code in this project.
- 18 tools: KPIs, health, catalogs, inventories, schema explorer, `query_db`.
- Read-only by construction:
  - session-level `default_transaction_read_only=on` and a 30 s statement timeout;
  - `SET TRANSACTION READ ONLY` per call;
  - `query_db` accepts exactly one SELECT/WITH/VALUES/TABLE/SHOW/EXPLAIN statement and rejects COPY, dblink and other side-effecting functions (`tools/mcp_sql_guard.py`).
- Purpose: give a coding agent *visibility* into pipeline state. **No engine runs through MCP, and DataMind is not an MCP product feature.**

## E. Controlled commands and pipeline execution

| Engine | Entrypoint (CLI or worker) | Writes | Human gate |
|---|---|---|---|
| Phase 1 extraction → promote | `openmaps_extractor/scripts/run_phase1.py`, `batch_pipeline.py` | `node_*` | `nodes_resolved.status='approved'` (bulk-approvable) |
| Phase 2 semantics | `phase2_semantics/scripts/run_all.py` (steps 00–60) | `geo_*` | `30_approve.py` |
| Phase 3 constructor | `route_constructor/scripts/*` (Constructor V2), `canton_pipeline.py` | `route_*` | Promote scripts; Re-Entry Review in the UI |
| HADES enforcers and re-entry | `hades/enforcers/re_entry_worker.py`, DR exporter/importer/validator | `approval_queue`, `re_entry_queue` | Re-Entry Review confirm / override; refill decisions per item |
| Phase 4 naming | `phase4_semantics/scripts/*`, API | `catalog.*`, route names | review / finalize |
| Phase 5 GTFS | console Phase 5 steps (buttons), `phase5_gtfs/*` | `gtfs_work`, `gtfs_prod` | GTFS Ops publish buttons |

## Approval boundaries (summary)

- Writing production data requires either an explicit console action with confirmation, or an operator-approved terminal command. Rendering a page never writes (enforced since 2026-10-08; see `docs/DB_ACCESS.md`).
- LLM output in the product is advisory. Deterministic validators and the GREEK transaction decide what reaches `route_prod`.
- Publication of a GTFS feed is always a human action.
