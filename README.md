<div align="center">

[Español](README.es.md) · **English**

<img src="docs/media/logo.svg" alt="ML DataMind logo" width="84"><a href="#mcp-with-claude-code"><img src="docs/media/claude-mark.svg" alt="with Claude" height="84"></a>

# ML DataMind

### Rebuild a city's bus network as a GTFS feed, with a person signing off on every decision.

An open-source pipeline and operator console. It turns open map data and field evidence into the routes, stops, shapes and timetables that transit apps and planners need.

[![License: MIT](https://img.shields.io/badge/license-MIT-17145e)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-17145e)
![PostgreSQL + PostGIS](https://img.shields.io/badge/PostgreSQL-PostGIS%20%C2%B7%20pgvector-17145e)
![Output: GTFS](https://img.shields.io/badge/output-GTFS-d99a2b)
![Human in the loop](https://img.shields.io/badge/human-in%20the%20loop-d99a2b)
![Origin: Antari](https://img.shields.io/badge/born%20from-Antari%20transit%20app-5b5f73)
[![MCP with Claude Code](https://img.shields.io/badge/MCP%20with-Claude%20Code-D97757?logo=claude&logoColor=white)](#mcp-with-claude-code)

[**▶ Watch the console tour**](#console-tour) &nbsp;·&nbsp;
[MCP with Claude Code](#mcp-with-claude-code) &nbsp;·&nbsp;
[How it works](#how-it-works) &nbsp;·&nbsp;
[Typical workflow](#typical-workflow) &nbsp;·&nbsp;
[Quickstart](#quickstart) &nbsp;·&nbsp;
[Docs](#documentation)

<a href="#console-tour">
  <img src="docs/media/hero-route-map.webp" alt="The DataMind console: a reconstructed bus route in Quito drawn over the map, with its 11 stops listed in order" width="900">
</a>

<sub>A route rebuilt by DataMind (*Bolivia – Tejar – Chorrera*, Quito): the geometry follows the streets and the stops come in order. Click to watch a 70-second tour of the console.</sub>

</div>

## Console tour

Seventy seconds in the real console, no narration: a route on the map, a cleanup before and after, the review queues, the GTFS validation gate and the AI scope.

https://github.com/user-attachments/assets/f634cf72-a150-4647-8c69-1a7f9836e610

<sub>Can't see the player? [Download the video](docs/media/datamind-console-demo.mp4).</sub>

---

## Why DataMind exists

<table>
<tr>
<td width="20%" align="center">🚌<br><b>1 · An app</b></td>
<td width="20%" align="center">🧩<br><b>2 · A wall</b></td>
<td width="20%" align="center">🗺️<br><b>3 · The real problem</b></td>
<td width="20%" align="center">🛠️<br><b>4 · DataMind</b></td>
<td width="20%" align="center">📲<br><b>5 · Back to riders</b></td>
</tr>
<tr>
<td valign="top">We set out to build a transit app for riders in Ecuador.</td>
<td valign="top">The app needed routes, stops and schedules. No complete, reliable public feed existed.</td>
<td valign="top">The hard part was the data: rebuilding each route's stops, their order and the path it follows on the streets.</td>
<td valign="top">So we built the infrastructure to rebuild that data with evidence and human review.</td>
<td valign="top">The result is a standard GTFS feed that any app or planning tool can use.</td>
</tr>
</table>

> [!NOTE]
> **Built from a real transit product need. Downstream use case: Antari.**
> Antari is the rider-facing mobility app that exposed the data problem. DataMind grew out of it as the **upstream data infrastructure**: Antari consumes transit data, and DataMind produces it. They are separate products: Antari is where the problem was found, and it is the downstream use that DataMind's output is built for.

---

## MCP with Claude Code

<a href="#mcp-with-claude-code"><img src="https://img.shields.io/badge/MCP%20with-Claude%20Code-D97757?style=for-the-badge&logo=claude&logoColor=white" alt="MCP with Claude Code"></a> <img src="https://img.shields.io/badge/MCP%20server-18%20tools%20·%20read--only-1f1b4d?style=for-the-badge&logo=modelcontextprotocol&logoColor=white" alt="MCP server: 18 tools, read-only">

For two months we filled the gaps in the data mostly by hand, and it was slow. On 25 February 2026 we brought **Claude Code** into the project as the orchestrator of the pipeline. It ran our engines phase by phase, following our own playbooks, and reached the database through an **MCP server** we wrote for it. The pace changed in days.

https://github.com/user-attachments/assets/9d37f051-21b7-4c58-8954-e519f9e1f29c

<sub>«The Story», 90 seconds, with the real dates and counts from our database and git history.</sub>

| | Before Claude <br><sub>January – February 2026</sub> | With Claude as orchestrator |
|---|---|---|
| **OpenStreetMap extraction runs** | 42 in two months | **1,392** in the week of 2 March |
| **Clean route records** | 7 | **241** built in four weeks (30 March – 26 April) |
| **Gap research** | by hand | **2,075** gaps researched and **3,592** landmarks proposed (23 April – 6 May), every one checked by our code |
| **Result** | | **218 clean bus lines** in the Quito region (275 one-direction records) |
| **Guayaquil** | | **301** route directions built with the same pipeline **in three days** (9 – 11 April 2026), **98** deployed. Their cleanliness review is still pending |

**Why MCP was essential.** MCP gave Claude a safe window into the work: `tools/mcp_server.py` exposes **18 tools, all read-only** (route catalog, route detail, phase summaries, extraction runs, recent decisions, `query_db`…). Claude could inspect the real state of every route and every phase, compare it against references and decide what to run next, **without being able to change a single row on its own**. The engines it ran are our own code, and our playbooks (skills, one per phase) told it how to run them.

> [!IMPORTANT]
> **Claude did the work at speed. We stayed in control.** Nothing is published without a person's approval, and the 794 stops the system filled on clean routes are still marked as pending review. These are our project's own dates and counts from Ecuador; we make no comparison with other teams or institutions.

---

## How it works

DataMind is a five-phase pipeline with a person in the loop. Machines propose and reconcile. People decide what ships.

```mermaid
flowchart LR
    A["📥 Evidence<br/><sub>OpenStreetMap, field notes,<br/>operator schedules</sub>"] --> B["🗺️ Reconstruction<br/><sub>stops, places, route<br/>sequence and geometry</sub>"]
    B --> C["🧑‍⚖️ Human review<br/><sub>verify, correct or reject,<br/>with an audit trail</sub>"]
    C --> D["📦 GTFS export<br/><sub>compile, validate<br/>in layers, publish</sub>"]
    D --> E["📲 Downstream<br/><sub>transit apps, trip planners,<br/>planning tools</sub>"]
```

| | What goes in | What comes out |
|---|---|---|
| **1 · Nodes** | Map data for an area (OpenStreetMap via Overpass) | Clean stops and points of interest |
| **2 · Semantics** | Raw place names | Canonical places, aliases and search |
| **3 · Routes** | Stops plus the road network (Valhalla) | Each route as an ordered stop sequence with a street-following geometry, run through quality gates |
| **4 · Naming** | Evidence (OSM relations, schedules, operator statements) | Route names, each tied to the evidence behind it |
| **5 · GTFS** | Approved routes and stops | `agency`, `routes`, `trips`, `stop_times`, `shapes`… as a feed you validate, then publish |

> [!TIP]
> **Nothing ships on autopilot.** The orchestration engine (**HADES**) automates the repetitive steps but stops at approval gates. Every human decision is stored with an audit row, and publishing a feed is always an explicit human action.

---

## A look inside

<sub>These screenshots come from the console running on the original project's private data in Ecuador. **This repository ships no real data**, only a tiny synthetic sample. See [What's included](#whats-included-and-what-isnt).</sub>

<table>
<tr>
<td width="50%" valign="top">
<img src="docs/media/en/before-after.jpg" alt="Before and after maps of the same route in Guayaquil: stops snapped onto the canonical line"><br>
<b>Clean up with constraints, not guesses.</b> Each change shows the route before and after: aligned stops stay put, near-misses are snapped onto the line, and orphans are removed.
</td>
<td width="50%" valign="top">
<img src="docs/media/en/synthetic-stops.jpg" alt="Synthetic stops review screen: 2,423 pending, 0 verified, 0 rejected"><br>
<b>The pipeline proposes; a person decides.</b> Stops the pipeline generated to fill coverage gaps wait in a queue until someone verifies or rejects each one. The counters are honest: none has been decided yet.
</td>
</tr>
<tr>
<td width="50%" valign="top">
<img src="docs/media/en/gtfs-validation.jpg" alt="GTFS operations: layered validation with a structure check and the MobilityData validator"><br>
<b>Validation in layers.</b> A structure check and the MobilityData GTFS validator run before anything can be published. A feed that has not passed is labelled "not publish-ready".
</td>
<td width="50%" valign="top">
<img src="docs/media/en/ai-scope.jpg" alt="AI assistance: a reviewed scope and the exact context handed to a local agent"><br>
<b>AI with a boundary.</b> You can hand a local AI agent a reviewed and scoped context. That context is information, not authorization: it grants no database, cloud or Git permission.
</td>
</tr>
</table>

<details>
<summary><b>More: the operational overview</b></summary>
<br>
<img src="docs/media/en/overview.jpg" alt="Overview: canonical route counts per province and the work that needs attention" width="760">

The home screen shows canonical counts (every number is defined in [docs/METRICS.md](docs/METRICS.md)) and every queue that is waiting for a person.
</details>

---

## What you can do with DataMind

- 🗺️ **Reconstruct routes** from open data: stops, their order and a geometry that follows real streets.
- 🔎 **Audit what exists**: inspect any route direction, its stops and its quality class on a map.
- 🧑‍⚖️ **Run human review queues** for synthetic stops, re-entry cleanups and route proposals, with a reviewer name and an audit row on every decision.
- 🏷️ **Name routes with evidence**: attach OSM relations, schedules or operator statements, and get candidates from a deterministic, explained matcher.
- 📦 **Build and validate GTFS**: compile the feed, check it in layers, and publish it locally or to AWS behind explicit confirmation.
- 🤖 **Query it from AI tools**: a read-only MCP server ([details](#under-the-hood)) exposes the database to MCP clients.
- 🌎 **Fork it for your city**: swap the regional configuration and run the same pipeline elsewhere.

---

## Typical workflow

This is what an operator's day looks like in the console:

| Step | Where in the console | What you do |
|---|---|---|
| **1. Get your bearings** | *Overview* | See pipeline freshness, canonical counts and what needs attention. |
| **2. Bring in evidence** | *Phases → 1 Nodes / 2 Semantics* | Extract stops and places for your area and review the clusters. |
| **3. Build routes** | *Phases → 3 Routes* | Construct routes, inspect the geometry, and send weak ones back through re-entry. |
| **4. Review** | *Review · Synthetic stops · Other proposals* | Verify, correct or reject. Each decision records who made it. |
| **5. Name** | *Phases → 4 Naming (Evidence and Matching)* | Attach evidence and label candidate routes. |
| **6. Ship** | *Phases → 5 GTFS*, then *GTFS operations* | Compile the feed, validate it, and publish only when it passes. |
| **7. Use it** | Outside DataMind | Load the GTFS into a trip planner, a rider app or a planning tool. |

---

## Quickstart

> [!IMPORTANT]
> You need **Python 3.10+** and **PostgreSQL 15+** with the `postgis`, `vector` (pgvector), `pgcrypto`, `pg_trgm`, `citext`, `unaccent` and `dblink` extensions. Overpass, Valhalla, OpenSearch, an OpenAI key and AWS are optional. You only need them for the features that use them.

**1. Clone and install**

```bash
git clone <YOUR-REPO-URL>.git datamind-gtfs
cd datamind-gtfs
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

**2. Configure.** Copy the example file and set **your** database connection (`DB_DSN`). Nothing ships with defaults: if the connection is missing, every script and the console stop and name the variable to set. `.env` is already git-ignored.

```bash
cp .env.example .env
```

Minimum for local mode with the sample data:

```ini
DB_DSN=postgresql://USER:PASSWORD@127.0.0.1:5432/datamind_ml
LOCAL_DB_DSN=postgresql://USER:PASSWORD@127.0.0.1:5432/datamind_ml
DATA_MODE=local
DATAMIND_LOCAL_ONLY_MODE=true
```

**3. Create the empty database** (run `createdb datamind_ml` first)

```bash
python scripts/init_db.py
```

This creates the extensions and the full schema (19 schemas, 134 tables). It runs in one transaction and refuses to overwrite an existing database. `--reset --yes` drops the DataMind schemas and recreates them.

**4. Load the synthetic sample**

```bash
python scripts/load_sample_data.py
```

It loads 3 routes, 26 stops and 6 places of an invented city. It is repeatable, and `--remove` deletes only the sample rows.

**5. Start the console**

```bash
streamlit run datamind_console/app.py --server.address 127.0.0.1
```

Open <http://127.0.0.1:8501> and go to **Sample Region Audit** to see the sample. Local mode has no login, so Streamlit must listen only on `127.0.0.1` (which `.streamlit/config.toml` already sets). For a remote deployment with users and sessions, see `ML_DATAMIND_REMOTE_UI` in `.env.example`.

---

## Under the hood

<details>
<summary><b>The 5-phase architecture</b></summary>

```
 OpenStreetMap (Overpass)                                     Valhalla (routing)
        │                                                            │
        ▼                                                            ▼
 ┌────────────┐   ┌────────────────┐   ┌───────────────┐   ┌────────────────┐   ┌──────────────┐
 │ Phase 1    │──►│ Phase 2        │──►│ Phase 3       │──►│ Phase 4        │──►│ Phase 5      │
 │ NODES      │   │ SEMANTICS      │   │ ROUTES        │   │ NAMING         │   │ GTFS         │
 │ stops and  │   │ places, aliases│   │ sequence +    │   │ evidence,      │   │ compile and  │
 │ POIs       │   │ embeddings,    │   │ geometry +    │   │ candidate      │   │ publish      │
 │ (ML+DBSCAN)│   │ search         │   │ quality gates │   │ ranking        │   │ (local / AWS)│
 └────────────┘   └────────────────┘   └───────────────┘   └────────────────┘   └──────────────┘
   node_raw/work/prod   geo_raw/work/prod  route_raw/work/prod   semantics             gtfs_work / gtfs
        ▲                 ▲                    ▲                   ▲                     ▲
        └─────────────────┴────────────────────┴───────────────────┴─────────────────────┘
                     Operator console (Streamlit)  ·  HADES (autopilot + enforcers)
                     PostgreSQL + PostGIS + pgvector  ·  every phase: raw → work → prod
```

| Phase | Folder | Schemas | What it produces |
|---|---|---|---|
| 1 · Nodes | `phase1_nodes/` | `node_raw`, `node_work`, `node_prod` | Clean stops and POIs (OSM extraction, clustering, ML classification) |
| 2 · Semantics | `phase2_semantics/` | `geo_raw`, `geo_work`, `geo_prod` | Canonical places, aliases, embeddings and search |
| 3 · Routes | `phase3_routes/` | `route_raw`, `route_work`, `route_prod`, `route_review`, `route_trash` | Routes with a stop sequence and geometry, with quality checks |
| 4 · Naming | `phase4_naming/` | `semantics` | Route names with evidence and a candidate *ranker* |
| 5 · GTFS | `phase5_gtfs/` | `gtfs_work`, `gtfs`, `gtfs_prod`, `catalog` | The compiled, validated and published GTFS feed |

Cross-cutting components:

- `datamind_console/`: the console (per-phase views, orchestrator, services, persistence, AI assistant).
- `hades/`: *enforcers* that apply quality rules (geometry, stop coverage, reclassification…).
- `pipeline/`: snappers and phase 4.5 (audit and *commit* of corrections).
- `local_runner/`: non-interactive prompt automation (optional).
- `datamind_core/`: global configuration (DSN, schemas, services) and `dsn.py`.
- `db/schema.sql`: **the complete database schema** (structure only).

</details>

<details>
<summary><b>The console, page by page</b></summary>

The console is bilingual (English / Español). You switch languages in the sidebar, and the choice is kept in the `?lang=` URL parameter. Every page has a deep link (`/review`, `/phases?phase=3&tab=re_entry`, …).

| Section | Surface | Purpose |
| --- | --- | --- |
| Operate | Overview | Operational home built only from real data: pipeline freshness, what needs attention, canonical route counts, recent activity, current feed. Without a database it shows a clear "database unavailable" state and the rest of the console still starts. It never selects a region or work. |
| Operate | Phases | Phase-specific pipeline workspaces (1 Nodes · 2 Semantics · 3 Routes · 4 Naming · 5 GTFS). Phase 4 includes **Evidence** and **Matching**. |
| Operate | GTFS operations | Validate a GTFS ZIP (structure check, MobilityData validator on demand), publish locally, AWS deploy behind explicit confirmation. See [docs/GTFS_VALIDATION.md](docs/GTFS_VALIDATION.md). |
| Operate | Revise current GTFS | Review an existing feed. |
| Review | Review | Read-only overview of every queue waiting for a person, with links to where decisions are made. |
| Review | Synthetic stops | Stops the pipeline generated to fill coverage gaps: verify or reject each one, on a map. Rejected stops are left out of every GTFS export. |
| Review | Other proposals | Route proposals outside Re-Entry Review: inspect the evidence and reject with a reason. Nothing is approved automatically. |
| Audit | Sample Region audit | Read-only inspection of Sample Region route directions, stops and cohorts on the canonical metrics. |
| Integration | Geo API pages | Geo API, production and fusion views. |
| System | AI Assistance | Shows a reviewable selected-work context and opens an interactive local Claude Code or Codex terminal only after explicit scope review; it does not authenticate or expand permissions. |
| System | Settings | Appearance and language; whether the autopilot signs with your own key; in local mode, the configuration in effect (secrets shown only as configured / not set); in remote mode, users and roles. |

**Human review.** Every decision on those screens names its reviewer and is written in the same transaction as its audit row in `console.phase_decisions`. Nothing is decided on page load or automatically. See [docs/REVIEW.md](docs/REVIEW.md).

**Phase 4 · Evidence and Matching.** Browse evidence records (OSM relations, schedules, operator statements), add one by hand, ask for candidate routes scored by a deterministic, explained matcher, and label each candidate (correct / partial / not this route). Labels are human decisions with an audit row. There is no trained model, so model prediction is not offered. The two tables come from migration `V4_8` (schema unit `phase4.evidence_matching`). See [docs/PHASE4_EVIDENCE_MATCHING.md](docs/PHASE4_EVIDENCE_MATCHING.md).

</details>

<details>
<summary><b>Database access and schema</b></summary>

Opening a page never changes the database. Pages verify the schema they need and, if it is missing, say so. Migrations are applied only on request:

```bash
python -m datamind_console.db.schema_cli status
```
```bash
python -m datamind_console.db.schema_cli plan <unit>
```
```bash
python -m datamind_console.db.schema_cli apply <unit> --yes
```

`apply` runs migration SQL against the configured database. `plan` shows what each file would do, including data changes. Code reads through the helpers in `datamind_console/db/db.py` (`query_all`, `read_only_connection`, `transaction`, …). See [docs/DB_ACCESS.md](docs/DB_ACCESS.md).

To browse the console against a live database with no risk of writes, give the session a read-only default: `options=-c default_transaction_read_only=on` in the DSN (URL-encoded), or `PGOPTIONS="-c default_transaction_read_only=on"`.

</details>

<details>
<summary><b>MCP server (read-only)</b></summary>

`tools/mcp_server.py` exposes the database to MCP clients. Every session is read-only with a 30 s statement timeout. `query_db` accepts a single `SELECT` / `WITH` / `EXPLAIN` / `SHOW` / `VALUES` / `TABLE` statement and rejects side-effect functions (`dblink*`, `pg_terminate_backend`, `lo_*`, `set_config`, `pg_sleep`, …). Results are capped at 500 rows.

</details>

<details>
<summary><b>Adapt the pipeline to your region</b></summary>

The sample lives in an invented region (`sample_region`, "Sample City"). To use your own region, replace this configuration:

| What | Where |
|---|---|
| Active regions, operational bounding box, geocoding bias | `workspace/config/supported_provinces.json` |
| Extraction boxes per sector, anchors, POI catalog and OSM tag map | `phase1_nodes/catalogs/*.json` |
| Geographic search groups | `datamind_console/common/nominatim_group_bias.json` |
| Jurisdictions and quality rules for grounding stops | `datamind_console/phases/phase3_routes/stop_grounding/catalogs/` |
| Operators and name templates | `phase4_naming/catalogs/` |
| External services (Overpass, Valhalla, OpenSearch) | `.env` (`OVERPASS_URL`, `VALHALLA_URL`, `OPENSEARCH_URL`) |

Overpass and Valhalla can be started with `docker-compose.yml`. Edit it to point at an OSM extract of your region in `./data/overpass/`.

</details>

<details>
<summary><b>Tests</b></summary>

Run the suites **one folder at a time**, because several `tests/` packages share a name. For example:

```bash
python -B -m pytest -p no:cacheprovider datamind_console --ignore=datamind_console/phases/phase3_routes/tests
```
```bash
python -B -m pytest -p no:cacheprovider tests
```

The other folders are `datamind_console/phases/phase3_routes/tests`, `tools/tests`, `phase1_nodes`, `phase2_semantics`, `phase3_routes/diagnostics`, `local_runner/tests`, and `phase3_routes/services/route_constructor/tests` (run from that directory). None of them needs a database, AWS or an external API. See [docs/TEST_CLASSIFICATION.md](docs/TEST_CLASSIFICATION.md).

- Tests that **need a populated database** skip themselves. Enable them with `DB_DSN=... DATAMIND_RUN_DB_TESTS=1` (they assume real data of a region, not the sample). Tests that write (trash repository, human review, Phase 4 evidence) run only with `DATAMIND_DISPOSABLE_TEST_DSN` pointing at a database whose name contains `test`, `scratch` or `disposable`.
- Tests written against the original region's data files (place catalog, Phase 3 catalogs, bbox catalog) are reported as **skipped** while those files are absent; see `conftest.py`. With your own region's files they run.

</details>

<details>
<summary><b>Security</b></summary>

- No hardcoded credentials: the connection always comes from `DB_DSN` / `.env`. If it is missing, the code fails with a clear message (`datamind_core/dsn.py`).
- In local mode, expose the console only on `127.0.0.1`. For remote access, use the login-and-sessions mode.
- The AI assistant only opens a local terminal with an explicit flag (`ML_DATAMIND_LOCAL_CLI_LAUNCH_ENABLED`) and after you review the scope.
- The phase 4 API requires `X-API-Key` (`PHASE4_API_KEY`) on the endpoints that write, and restricts CORS to the local console.
- Secrets: `python tools/secret_scan.py --tracked` scans every tracked file and prints fingerprints, never values. `tools/git-hooks/pre-push` refuses a push whose new commits add a secret; install it with `cp tools/git-hooks/pre-push .git/hooks/pre-push`. See [docs/SECURITY_HYGIENE.md](docs/SECURITY_HYGIENE.md).
- The autopilot signs operator decisions with `HADES_OPERATOR_SIGNING_SECRET`. Create your own with `python -m datamind_console.orchestrator.signing_key --write .env`. Without it, the console warns that the public development key is in use.
- Before publishing your fork, run `gitleaks detect` and make sure you don't commit `.env`, dumps or data.

</details>

---

## What's included and what isn't

| ✅ Included | 🚫 Deliberately not included |
|---|---|
| The code of all five phases, the console and HADES | Real data, or catalogs of any specific region |
| The complete database schema (`db/schema.sql`) | Database dumps, run outputs and logs |
| A tiny synthetic sample (3 lines, 26 stops, 6 places of an invented city) | Trained models (retrain the LightGBM models with `ai_training/` on your own database) |
| Example configuration and the console logo (`datamind_console/ui/assets/`) | Valhalla tiles and OSM extracts |

> [!WARNING]
> **Limitations you should know**
> - The pipeline was developed for **one specific region** (Quito, Ecuador). The configuration is now sample data, but some place-name heuristics (for example in `geography_guardrails.py` and `geography_input_resolver.py`) and several comments and tests still mention that region. Adapt them to yours.
> - `db/schema.sql` is a sanitized dump of the real schema. The `*/migrations` and `*/sql` files are kept as a **change history**, but they cannot rebuild the full database on their own. Use `scripts/init_db.py`.
> - Phases 3 and 5 need real services (Valhalla, OTP) to run end to end. Without them you can explore the console and the sample, but you cannot build new routes.
> - The original project's GTFS exports have not passed the MobilityData validator yet, so no DataMind feed is presented here as validated. The console shows that gate instead of hiding it.

---

## Documentation

| Topic | Read |
|---|---|
| What each capability really does today (active, heuristic, experimental, legacy) | [docs/CURRENT_CAPABILITIES.md](docs/CURRENT_CAPABILITIES.md) |
| What every number means | [docs/METRICS.md](docs/METRICS.md) · [docs/TERMINOLOGY.md](docs/TERMINOLOGY.md) |
| Human review and the audit trail | [docs/REVIEW.md](docs/REVIEW.md) |
| GTFS validation and publishing | [docs/GTFS_VALIDATION.md](docs/GTFS_VALIDATION.md) |
| Phase 4 evidence and matching | [docs/PHASE4_EVIDENCE_MATCHING.md](docs/PHASE4_EVIDENCE_MATCHING.md) |
| Orchestration (HADES) | [docs/ORCHESTRATION.md](docs/ORCHESTRATION.md) · [docs/hades_pipeline_autopilot.md](docs/hades_pipeline_autopilot.md) |
| Database access and security | [docs/DB_ACCESS.md](docs/DB_ACCESS.md) · [docs/SECURITY_HYGIENE.md](docs/SECURITY_HYGIENE.md) |
| Operations | [OPS.md](OPS.md) |

## License

[MIT](LICENSE)

## Authors

Josué Arcos and Jhair Jiménez
