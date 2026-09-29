[Español](README.es.md) | **English**

# DataMind GTFS — pipeline and console template for building GTFS feeds

An open-source template of an **operator console and a five-phase pipeline** that builds, validates and publishes **public-transit GTFS feeds** from open data (OpenStreetMap), with human review at the critical points.

> **Extracted from a real public transit analysis project in Quito.**

> The bundled database is **empty**; it ships with a **tiny synthetic sample** (3 lines, 26 stops, 6 places of an invented city). There is no real data, no catalogs of any region, no trained models, no map tiles and no map extracts. See [What's included and what isn't](#whats-included-and-what-isnt).

## What it does

It turns "loose stops and routes on a map" into a consistent GTFS feed:

1. **Finds the stops** and reference places of an area.
2. **Gives them meaning** (canonical names, aliases, semantic search).
3. **Builds the routes** (stop sequence + geometry on the road network) and runs them through quality gates.
4. **Names the routes** with evidence (operator, endpoints, references).
5. **Compiles the GTFS** (`agency`, `routes`, `trips`, `stop_times`, `shapes`…) and publishes it.

All the work goes through a **web console (Streamlit)** where an operator approves, corrects or rejects. An orchestration engine (**HADES**) automates the repetitive steps behind "approval gates" so nothing is published without permission.

## Screenshots

The console in local mode with the synthetic sample:

| Home | Audit · inventory |
|---|---|
| ![Home](docs/screenshots/01-consola-inicio.jpg) | ![Inventory](docs/screenshots/02-auditoria-inventario.jpg) |
| **Audit · routes** | |
| ![Routes](docs/screenshots/03-auditoria-rutas.jpg) | |

## The 5-phase pipeline architecture

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

- `datamind_console/` — the console (per-phase views, orchestrator, services, persistence, AI assistant).
- `hades/` — *enforcers* that apply quality rules (geometry, stop coverage, reclassification…).
- `pipeline/` — snappers and phase 4.5 (audit and *commit* of corrections).
- `local_runner/` — non-interactive prompt automation (optional).
- `datamind_core/` — global configuration (DSN, schemas, services) and `dsn.py`.
- `db/schema.sql` — **the complete database schema** (structure only).

## Requirements

- Python 3.10+.
- PostgreSQL 15+ (tested with 17) with the **postgis**, **vector (pgvector)**, `pgcrypto`, `pg_trgm`, `citext`, `unaccent` and `dblink` extensions.
- Optional, depending on the feature you use: Overpass and Valhalla (real extraction and routing), OpenSearch (semantic search), an OpenAI key (AI assistant), SSH/AWS access (remote publishing).

## Step-by-step setup

**1. Clone and install dependencies**

```bash
git clone <YOUR-REPO-URL>.git datamind-gtfs
cd datamind-gtfs
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

**2. Copy `.env.example` to `.env` and put in your own keys**

```bash
cp .env.example .env
```

Open `.env` and set **your** database connection (`DB_DSN`). The file is commented variable by variable, and its last section lists every other variable the code reads. **Nothing ships with defaults**: if the connection is missing, every script and the console stop with a message that says which variable to set. Do not commit `.env` (it is already git-ignored).

Minimum for local mode with the sample data:

```ini
DB_DSN=postgresql://USER:PASSWORD@127.0.0.1:5432/datamind_ml
LOCAL_DB_DSN=postgresql://USER:PASSWORD@127.0.0.1:5432/datamind_ml
DATA_MODE=local
DATAMIND_LOCAL_ONLY_MODE=true
```

**3. Create the empty database** (the `datamind_ml` database must exist: `createdb datamind_ml`)

```bash
python scripts/init_db.py          # extensions + full schema (19 schemas, 134 tables)
```

It is transactional (if something fails, nothing is left half-applied) and refuses to overwrite an existing database. `--reset --yes` drops the DataMind schemas and recreates them.

**4. Load the synthetic sample**

```bash
python scripts/load_sample_data.py           # 3 routes · 26 stops · 6 places
python scripts/load_sample_data.py --remove  # removes only the sample rows
```

It is repeatable: running it again replaces the sample without duplicating it.

**5. Start the console**

```bash
streamlit run datamind_console/app.py --server.address 127.0.0.1
```

(The console and the scripts read your `.env` automatically.)

Open <http://127.0.0.1:8501>. In local mode there is no login (Streamlit must listen only on `127.0.0.1`, which is what `.streamlit/config.toml` sets). Go to **Sample Region Audit** to see the sample. For a remote deployment with users and sessions, see `ML_DATAMIND_REMOTE_UI` in `.env.example`.

## Adapt the pipeline to your region

The sample lives in an invented region (`sample_region`, "Sample City"). For your region, replace the configuration:

| What | Where |
|---|---|
| Active regions, operational bounding box, geocoding bias | `workspace/config/supported_provinces.json` |
| Extraction boxes per sector, anchors, POI catalog and OSM tag map | `phase1_nodes/catalogs/*.json` |
| Geographic search groups | `datamind_console/common/nominatim_group_bias.json` |
| Jurisdictions and quality rules for grounding stops | `datamind_console/phases/phase3_routes/stop_grounding/catalogs/` |
| Operators and name templates | `phase4_naming/catalogs/` |
| External services (Overpass, Valhalla, OpenSearch) | `.env` (`OVERPASS_URL`, `VALHALLA_URL`, `OPENSEARCH_URL`) |

Overpass and Valhalla can be started with `docker-compose.yml` (edit it to point at an OSM extract of your region in `./data/overpass/`).

## Tests

```bash
python -B -m pytest -p no:cacheprovider local_runner/tests datamind_console/orchestrator/tests
# → 248 passed, 5 skipped
```

- Run them **per folder**: there are several `tests/` packages with the same name and pytest doesn't mix them well in a single invocation.
- Tests that **need a populated database** are marked as integration tests and skip themselves. Enable them with `DB_DSN=... DATAMIND_RUN_DB_TESTS=1` (they assume real data of a region, not the sample).
- 3 geographic-resolution tests check behaviors of the original region's catalog and are skipped (`@unittest.skip`) until you adapt them to yours.

## What's included and what isn't

**Included:** the code of the five phases, the console, HADES, the complete database schema, a tiny synthetic sample and example configuration. The console logo is the original project's logo (`datamind_console/ui/assets/`).

**Deliberately not included:** real data, catalogs of any specific region, dumps, trained models (LightGBM: retrain them with `ai_training/` and your own database), Valhalla tiles, OSM extracts, run outputs, or logs.

**Limitations you should know**

- The pipeline was developed for **one specific region** (Quito). Although the configuration is now sample data, some place-name heuristics (for example in `geography_guardrails.py` and `geography_input_resolver.py`) and several comments and tests still mention that region. Adapt them to yours.
- `db/schema.sql` is a (sanitized) dump of the real schema. The `*/migrations` and `*/sql` files are kept as a **change history**, but on their own they do not rebuild the full database: use `scripts/init_db.py`.
- Phases 3 and 5 expect real services (Valhalla, OTP) to run end to end; without them you can explore the console and the sample, but you cannot build new routes.

## Security

- No hardcoded credentials: the connection always comes from `DB_DSN` / `.env`. If it is missing, the code fails with a clear message (`datamind_core/dsn.py`).
- The console in local mode must only be exposed on `127.0.0.1`. For remote access, use the login-and-sessions mode.
- The AI assistant only opens a local terminal with an explicit flag (`ML_DATAMIND_LOCAL_CLI_LAUNCH_ENABLED`) and after reviewing the scope.
- The phase 4 API requires `X-API-Key` (`PHASE4_API_KEY`) on the endpoints that write, and restricts CORS to the local console.
- Before publishing your fork: run `gitleaks detect` and make sure you don't commit `.env`, dumps or data.

## License

[MIT](LICENSE)

## Authors

Josué Arcos and Jhair Jiménez
