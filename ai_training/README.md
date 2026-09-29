# AI Learning Agent (Minimal Loop)

## 1) Migrate DB (adds `ai.*` tables)

```bash
python datamind_console/scripts/00_migrate.py
```

## 2) Create labels from dashboard usage

1. Open Streamlit console and go to `Training -> AI Agent -> Suggestions`.
2. Review each suggestion and click:
   - `Mark Reviewed`
   - `Dismiss`
   - `Apply` (only for `approve/reject/promote` suggestion types)
3. Each click writes one row into `ai.ai_label_events` and updates `ai.ai_suggestions.status`.

## 3) Build dataset + train baseline

```bash
python -m ai_training.build_dataset --latest-only
python -m ai_training.train_baseline --activate
```

## 4) Optional neural-network upgrade (MLP)

```bash
python -m ai_training.train_mlp --activate
```

## 5) Evaluate a model

```bash
python -m ai_training.evaluate
```

## 6) Run worker

Default (heuristics):

```bash
AI_DECIDER=heuristics python datamind_console/scripts/run_ai_worker.py --limit 200
```

Scheduled loop mode (reads `ai.ai_agent_schedule`):

```bash
AI_DECIDER=heuristics python datamind_console/scripts/run_ai_worker.py --loop --limit 200
```

Model mode (falls back to heuristics if model missing/fails):

```bash
AI_DECIDER=model python datamind_console/scripts/run_ai_worker.py --limit 200
```

Optional explicit version pin:

```bash
AI_DECIDER=model AI_ACTIVE_MODEL_VERSION=<model_version> python datamind_console/scripts/run_ai_worker.py
```

## 7) Configure schedule + run once from dashboard

1. Open `Training -> AI Agent -> Schedule`.
2. Set `Enable schedule`, `Days of week`, `Start/End time`, and `Interval seconds`.
3. Click `Save schedule`.
4. Click `Run now` to set `run_once_now=true` for one immediate loop iteration.

## 8) Optional escalation-to-Codex integration (off by default)

```bash
AI_ESCALATION_ENABLED=true
CODEX_API_URL=https://your-agent-endpoint.example/v1/escalate
CODEX_API_KEY=...
CODEX_MODEL=...
```

Use `Training -> AI Agent -> Escalations` to review stack traces, context, and `Retry send to Codex`.
