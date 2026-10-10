# Database access standard

Status: adopted in Phase 2 (2026-10-08).
Applies to all new or touched code in `datamind_console/`. Engines and scripts converge over time; see the inventory below.

## Rules

1. **Rendering never writes.**
   - A Streamlit page render, or the construction of a service client, must not run migrations, DDL, `INSERT`/`UPDATE`/`DELETE`, or "ensure schema" SQL.
   - Writes happen only inside an explicit user action: a button with confirmation, a CLI, or a worker.
2. **Runtime code verifies schema; it does not create it.**
   - Use `datamind_console.db.schema_guard.require(unit, connection_factory)`. It reads `pg_catalog` once per process and raises `SchemaNotReadyError` with operator instructions.
   - Applying SQL is an administrative action, available only through:

     ```bash
     python -m datamind_console.db.schema_cli status          # read-only check of every unit
     python -m datamind_console.db.schema_cli plan <unit>     # what apply would run, with a data-change count
     python -m datamind_console.db.schema_cli apply <unit> --yes
     ```

   - Any `apply` against an operational database is a **data migration**: it needs owner approval. Several units contain backfills; `plan` flags them with "⚠ data-changing statements".
3. **Connections are short-lived.**
   - Never cache a connection (`st.cache_resource`), keep one in session state, or reuse one across reruns. Cache *results* with `st.cache_data`.
4. **Use the canonical helpers** in `datamind_console/db/db.py`:

| Need | Use |
|---|---|
| One read query | `query_all(sql, params)`, `query_one(...)`, `query_scalar(...)`: a READ ONLY transaction per call, always ended and closed; optional `statement_timeout_ms` |
| Several reads on one snapshot | `with read_only_connection() as conn:` plus `fetch_all(conn, ...)` |
| A write | `with transaction() as conn:` (commits on success, rolls back on any exception, always closes) |
| A DSN for a service that manages its own transaction | `active_dsn()` (honours local-only mode; never log or display it) |

5. **Local-only mode never reaches a server.**
   - When `DATAMIND_LOCAL_ONLY_MODE=true` and `LOCAL_DB_DSN` is missing, `_get_db_config()` raises. It no longer falls back to `CFG.db_dsn`.
6. **Parameter convention (unchanged):** SQL passed to these helpers is always interpolated by psycopg2.
   - Write a literal `%` as `%%`, for example `LIKE 'Cerca de %%'`.
   - The MCP server is the exception: it executes free-form SQL without interpolation.

## Pitfalls this standard removes

| Pattern | Problem | Where it was |
|---|---|---|
| `@st.cache_resource def _conn(): return psycopg2.connect(...)` with no commit | Stayed **idle in transaction** for the server's lifetime and held locks on `route_prod.routes`. One SQL error left the cached transaction aborted. | `sample_region_audit_view.py` (fixed) |
| `with psycopg2.connect(dsn) as conn:` | psycopg2's context manager ends the *transaction* but **does not close** the connection | Re-Entry Review, Coverage Improvement, GREEK Ship Overview, GREEK-Validated Catalog tabs (fixed) |
| `_ensure_*_schema()` executing `.sql` files from read methods | Page views ran backfills: an unfiltered `UPDATE route_raw.service_routes`, `UPDATE route_prod.routes`, and re-approval of invalidated sequence approvals | `Phase3Client` (fixed) |
| Probe that only runs when "already ready" | Every new process re-applied all V5 migrations, string-sorted | `Phase5Client._ensure_schema` (fixed) |
| `cur.execute(sql, ())` on SQL containing `%` | "tuple index out of range" | MCP server (fixed) |
| A private `_dsn()` per page | About 20 different resolution rules; some ignored local-only mode | 4 Phase 3 tabs and the audit view now delegate to `active_dsn()` |

## Guard tests

| Test file | What it guards |
|---|---|
| `datamind_console/tests/test_ui_db_boundary.py` | No raw `psycopg2.connect(` and no cached connections in `views/`, `widgets/`, `ui/` (AST-based) |
| `datamind_console/tests/test_schema_guard.py` | Phase 3, Phase 5 and the AI repo only read the catalog |
| `datamind_console/tests/test_db_access.py` | The canonical helpers end transactions and close connections |
