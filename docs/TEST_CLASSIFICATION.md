# Test classification

127 tracked test files. Each is classified by the most dangerous thing it can
do. The classification is heuristic (source scan for connections, SQL writes,
network clients, subprocesses, filesystem writes, mocks), corrected by hand
for the DB-backed files.

| Category | Files | What it means | Safe to run on this machine? |
|---|---:|---|---|
| Pure unit | 71 | no I/O beyond imports; fakes and mocks | yes |
| Filesystem | 23 | writes under `tmp_path` / tempfiles | yes |
| Integration (subprocess) | 4 | spawns a process (e.g. `java -jar` with a fake jar, the CLI launcher) | yes |
| Network | 9 | would call Overpass / Nominatim / HTTP APIs; most are mocked or guarded | yes, with proxies pointed at a dead port |
| DB read-only | 8 | reads from a DB (several also have pure tests in the same file) | only with an unreachable DSN, or against a copy |
| DB write | 10 | inserts fixture rows; some COMMIT; one applies migration SQL | **only** against a disposable database |
| Scripts named `test_*.py` | 2 | `scripts/test_prompt_b.py`, `scripts/test_bug011_bug013.py`: operational scripts, not pytest tests | do not collect |

## DB-backed tests and the live database

Found in Phase 2:

* Sixteen modules under `tests/` fell back to the **live** operational DSN
  when `DB_DSN` was unset. `tests/enforcers/test_re_entry_swap_service.py` and
  `tests/enforcers/test_prompt8_e2e.py` insert fixture rows and **COMMIT**
  (with their own cleanup). Because their reachability guard found the live DB,
  a plain `pytest tests/` ran them against it.
  **Fixed** (commit `fix(tests): stop DB-backed tests from defaulting to the
  live database`). They now fall back to
  `postgresql://unset@127.0.0.1:1/set_DB_DSN_to_a_disposable_database`. The
  enforcer suites' guards skip, and
  `tests/phase3_routes/services/stop_quality/conftest.py` skips the suites
  whose fixtures connect unconditionally.
* `phase3_routes/services/route_constructor/tests/test_trash_repo.py` applies
  `sql/030_route_trash.sql` and commits it, then runs delete, purge and restore
  flows. It used to read `DB_DSN` or `DATABASE_URL`. **Isolated:** it now
  ignores every operational DSN variable and runs only against
  `DATAMIND_DISPOSABLE_TEST_DSN`. That database's name must contain `test`,
  `scratch` or `disposable`, and must differ from every operational DSN's
  database (and from `datamind_ml`). Otherwise all 18 tests skip with the
  reason. Three guard tests check this without a database.
* Phase 3 console unit tests used to execute migration SQL through
  `Phase3Client.__init__`. With the schema guard they only verify the schema,
  and their conftest replaces verification with a no-op.

## How to run the suites safely

Suites must be run one at a time. Several directories are packages named
`tests` with non-package parents, and per-service `src` roots collide when
collected together.

```bash
python -m pytest -q datamind_console --ignore=datamind_console/phases/phase3_routes/tests
```
```bash
python -m pytest -q datamind_console/phases/phase3_routes/tests
```
```bash
python -m pytest -q tools/tests
```
```bash
python -m pytest -q tests
```
```bash
cd phase3_routes/services/route_constructor && python -m pytest -q tests
```

The other suites (`phase1_nodes`, `phase2_semantics`,
`phase3_routes/diagnostics`, `local_runner/tests`, `labels/tests`) are run the
same way.

For the Phase 2 runs, every DSN variable the code reads was pointed at the
unreachable placeholder: `DB_DSN`, `DATABASE_URL`, `LOCAL_DB_DSN`,
`DB_DSN_LOCAL`, `DATAMIND_LOCAL_DB_DSN`, `SERVER_DB_DSN`, `DATAMIND_CONSOLE_DB_DSN`,
`LOCAL_GTFS_DB_DSN`, `AWS_DB_DSN`, `PG_DSN`, `DATAMIND_DB_DSN`, `CORE_DB_DSN`,
`DATAMIND_SERVER_DB_DSN`, `POSTGRES_DSN`, `LOCAL_TRANSIT_DB_DSN`,
`DB_DSN_SOURCE`, `SUPABASE_DB_URL`. Setting them, rather than unsetting
them, matters: `.env` is loaded with `override=False`, so an unset variable
would be filled back in with the live DSN. The runs also used `PGHOST=127.0.0.1
PGPORT=1`, `PGOPTIONS="-c default_transaction_read_only=on"` for any
hard-coded DSN that still reached a server, and HTTP(S) proxies pointed at a
dead port. Afterwards, `pg_stat_database` showed 0 inserted, updated and
deleted tuples.
