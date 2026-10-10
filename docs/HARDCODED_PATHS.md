# Hard-coded paths

No tracked Python file contains a user's home path any more.
`datamind_console/tests/test_runtime_paths.py` fails if one comes back: it
checks every tracked `*.py` for `/Users/<name>/(Desktop|Downloads|Documents)`.

## How paths are resolved

`datamind_console/common/paths.py`:

| Need | Helper | Behavior |
|---|---|---|
| a file inside the repo | `project_path(...)`, or `_REPO_ROOT` in scripts | relative to the checkout, wherever it is |
| an input the operator provides (catalogs, reference JSON) | `resolve_input(ENV_VAR, *candidates)` | `ENV_VAR` wins and must exist; else the first existing candidate (for example `~/Downloads/<file>`, home-relative so any macOS user works); else `PathNotConfigured`, naming the variable to set. Never a silent fallback |
| the Phase 3 Desktop mirror | `phase3_catalog_mirror_dir()` | `PHASE3_CATALOG_MIRROR_DIR`, default `~/phase3_route_catalog` |

`PathNotConfigured` is a `FileNotFoundError`. Scripts that treat an input as
optional (inside `except Exception`) keep that behavior; elsewhere it stops
the script with the message.

Scripts start with the same bootstrap: compute `_REPO_ROOT` from
`__file__`, put it on `sys.path`, import the helpers.

## Environment variables for script inputs

| Variable | Default candidate | Used by |
|---|---|---|
| `ROUTE_SEMANTICS_JSON` | `~/Downloads/01_route_semantics.json` | `populate_catalog_tables.py`, `prepare_and_compile_gtfs.py` |
| `ROUTE_SERVICE_DAYS_JSON` | `~/Downloads/02_route_service_days.json` | same |
| `ROUTE_RUNTIME_REFERENCE_JSON` | `~/Downloads/03_runtime_and_reference_data.json` | same |
| `VALLE_HINTS_CATALOG_V5_1` … `_V5_4` | `~/Downloads/valle_de_los_chillos_hints_catalog_v5*.json` | `run_v5_*`, `test_bug011_bug013.py`, `test_prompt_b.py` |
| `VALLE_CATALOG` | `~/Downloads/valle_de_los_chillos_hints_catalog_researched_updated (4).json` | `valle_rerun_v4.py` |
| `VALLE_TYPED_CATALOG_JSON` | `~/Desktop/valle_chillos_catalog_typed_rules_v3.json` | `build_typed_catalog_reports.py` |
| `SI2_SEQUENCES_JSON` | `<mirror>/SI2.json` | `load_si2_sequences_to_db.py` |
| `ROUTE_HINTS_V6_JSON` | `~/Downloads/route_hints_v6.json` | `route_constructor/scripts/construct_tuned.py` |
| `PHASE3_CATALOG_MIRROR_DIR` | `~/phase3_route_catalog` | `constructor_runner.py`, `valle_rerun_v4.py`, SI2 loader |
| `GTFS_APP_ROOT` / `LOCAL_GTFS_EXTRACT_DIR` | none: the check fails clearly | `ops/pre_deploy_enforcer.py` |
| `VALHALLA_RUNTIME_DATA` | `<repo>/valhalla_runtime_data` | `phase5_gtfs/scripts/start_valhalla_runtime.sh` |

On this machine every default resolves to the same file as before, so
behavior is unchanged where the files exist.
