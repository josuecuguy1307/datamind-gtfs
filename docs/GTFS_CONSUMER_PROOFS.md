# Three consumers of the PR #1 synthetic fixture

Dependency: [DataMind draft PR #1](https://github.com/josuecuguy1307/datamind-gtfs/pull/1),
proof commit `34856474e8e520ae7e12d429a9d4d584aa12f28b`. This integration PR is
stacked on that branch, so it adds consumer recipes without repeating the original
proof diff. Nothing here merges PR #1 or authorizes publication.

All three consumers read the **same original CC0 synthetic ZIP**, SHA-256
`602b8192c4f91f5f064942e582db4bb6bbded89c83c1ddde49207ad1eeb6eea5`.
It remains a **consumer fixture**, not a reviewed Phase 5 export or real-city
output. Original MobilityData 7.1.0 evidence (0 ERROR / 0 WARNING / 0 INFO) is
referenced by hash and preserved. A Transitland-derived local ZIP has a different
hash and must not inherit that canonical validation claim.

## Reproduce

Prerequisites: Python 3.12+, Node 22+, PostgreSQL **17** server/client binaries,
and its matching PostGIS extension. The recorded run used Node 22.20.0,
PostgreSQL 17.7, PostGIS 3.6.1 and Python 3.14.7 on macOS ARM64.
All npm dependencies (including native DuckDB bindings) are locked with package
integrities. `gtfs-via-postgres@4.12.0` and `gtfs-via-duckdb@5.0.1` were the
published npm latest releases when inspected; DuckDB repository main already
identifies an unpublished 5.0.2. We tested the published package, not that checkout.

From a checkout containing both PRs:

```sh
# If you already have the PR1 ZIP, use it directly; do not regenerate or modify it.
# Otherwise the fixed-date command below creates byte-identical source data.
python3 scripts/synthetic_gtfs_proof.py \
  --output .cache/pr1-fixture --start-date 2026-10-13 --download-validator
npm ci --prefix scripts/gtfs_consumers --legacy-peer-deps
python3 scripts/gtfs_consumers/run.py \
  --feed .cache/pr1-fixture/synthetic-gtfs.zip \
  --output .cache/three-consumers \
  --postgres-bin /path/to/postgresql-17/bin \
  --download-transitland
```

The consumer script does **not** read `.env` or accept a DSN, database URL,
existing socket, host or server port. It scrubs inherited `PG*`, `PSQL*`,
`TL_*`, DSN and secret configuration before launching processes. It initializes
its own cluster with a new superuser `proof` and database `postgres`, binds a
private mode-0700 Unix socket under `/tmp`, and disables TCP. This deliberately
uses native `initdb`/`pg_ctl`, not the machine's existing PostgreSQL service.
It stops that cluster in `finally` and removes only its own cluster/socket;
SQL, process logs and evidence remain. PostgreSQL is required only for that
consumer: use `--consumer duckdb` or `--consumer transitland` for standalone runs.
Every proof requires a **new output directory**; failures, malformed/missing
reports, wrong hashes/versions or unmet assertions return nonzero.

Upstream documented interfaces are used unmodified:

```sh
# Executed by the runner, with the exact resolved paths in evidence.json:
gtfs-to-sql --require-dependencies -- gtfs/*.txt > import.sql
psql -X -v ON_ERROR_STOP=1 -f import.sql
gtfs-to-duckdb --require-dependencies -- feed.duckdb gtfs/*.txt
transitland validate --include-entities --out transitland-report.json original.zip
transitland copy original.zip transitland-roundtrip.zip
```

The runner buffers generated PostgreSQL SQL before applying it and checks both
processes independently. `psql -X` avoids user psql startup files. DuckDB uses
upstream's native Node CLI and the same locked Node API to query the resulting
DB **read-only**. Its ICU and spatial extensions may be downloaded by the
upstream importer on first use; normal DuckDB extension-cache behavior applies.
The Transitland download is explicit, release **v1.3.4**, with official GitHub
asset SHA-256 pins for macOS Apple/Intel and Linux x86_64 in the runner.
Unsupported platforms fail; no catalog, fetch, sync, import-to-server, credentials
or network publication operation is invoked. The native PostgreSQL route was
used after `postgis/postgis:17-3.5` had no ARM64 Docker manifest; no emulated
Docker result is claimed.

## Assertions and portable SQL

The checked-in [`postgres.sql`](../scripts/gtfs_consumers/postgres.sql) and
[`duckdb.sql`](../scripts/gtfs_consumers/duckdb.sql) give runnable analysis examples.
Both assert agency 1, routes 1, stops 3, trips 4, stop_times 12, shapes 2 with
6 source points, calendar 1, exception 1, feed_info 1, service_days **364**,
arrivals/departures **4,368**, connections **2,912**, and no orphan stop, trip,
route, service or shape references. A query-only fake orphan supplies a negative
control (expected count 1) without changing the imported fixture.

Both spatial controls return 1 stop within 500 metres of the invented S1 point,
0 within 500 metres of a far point, and S1→S2 distance **553.48644774 metres**.
PostGIS uses geography. DuckDB stores lon/lat geometries; its
`ST_Distance_Spheroid` takes lat/lon, so the queries explicitly flip coordinates.
No degree-based distance is described as metres. Both directional shape starts
match their stops; stop times remain monotonic and max departure is **88,500
seconds = 24:35:00**.

The removed service date **2026-10-23** has 0 service days and 0 departures when
filtered by **service date**; the next service date has 12. The first service day's
`night_1` terminus is **2026-10-14 00:35:00-06**. There are nevertheless **5** early
clock-time departures on October 23 from October 22's after-midnight trips.
That is correct GTFS behavior: removal of a service day does not cancel the tail
of the previous service day. The positive/negative calendar controls check both
interpretations explicitly.

Transitland reports all nine source file counts with `success=true`, no errors
or warnings, then copies locally. Every original field's semantic value is
reconciled: decimal precision normalizes, and shape point sequence identifiers
change **[1,2,3] → [0,3,6]**, while ordered coordinates and distances remain equal.
Stop-time sequence numbers, >=24-hour times, references and calendar exceptions
must remain exact. No entity/reference-error override is used. This is not byte
identity or a claim that the derived copy has passed MobilityData.

## Known upstream behavior and candidate opportunities

**PostgreSQL: verified with known defect**, not an unconditional clean result.
The pinned importer emits `ST_AsText(shape_pt_loc)::geometry`, dropping the WGS84
SRID from aggregated shapes (observed **0**) while stops retain **4326**. This is
already tracked in [upstream issue #48](https://github.com/public-transport/gtfs-via-postgres/issues/48).
The exact failing query is saved as `shape-srid-repro.sql`:

```sql
SELECT ST_Equals(ST_StartPoint(sh.shape), s.stop_loc::geometry)
FROM shapes_aggregated sh JOIN stops s ON s.stop_id='S1'
WHERE sh.shape_id='shape_0';
```

Actual `psql` exit **3**, SQLSTATE **XX000**:
`ST_Equals: Operation on mixed SRID geometries (Point, 0) != (Point, 4326)`.
The production importer/view is left untouched. Our shape-start assertion uses
an explicit `ST_SetSRID(shape,4326)` workaround because the source fixture is
known WGS84. A separate query verifies that direct `shape_pt_loc::geometry`
retains 4326 and restores the matching start point. The campaign is preparing
the issue-linked upstream fix separately; this PR contains no duplicate fix.
Disposable clusters are stopped and removed; the command above recreates one.

**DuckDB:** `--stats-by-route-date materialized-view` genuinely fails in the
released CLI, exit **1**, with `materialized-view is currently not supported`.
The campaign owns the separate `docs/analysis/feed-by-route-date.md` correction;
this PR does not duplicate that patch. An additional small candidate,
`upstream-candidates/duckdb-command-name.patch`, corrects the stale CLI name
`import-gtfs-into-duckdb` in README/help to the actual npm executable
`gtfs-to-duckdb`. The installed bin works and `--help` demonstrates the mismatch.
It is a standalone candidate against inspected main, **not submitted upstream**,
with no promotional links or DataMind dependency.

**Transitland:** no defect demonstrated by this small fixture. Its documented
local reader interface already works; a promotional docs PR is not justified.
The report and copy proof are available for maintainers if a concrete local
read/normalization question arises.

README contribution sections/current code were inspected for all targets; no
separate CONTRIBUTING.md or AGENTS.md was present in the relevant checkouts.
Both public-transport tools retain their Prosperity 3.0 / Apache 2.0 licensing;
Transitland retains GPLv3 or its commercial license. No upstream binary/code is
vendored or relicensed; CC0 applies only to the invented fixture. Prospective
upstream changes should follow each project's contribution license requirements.

## Review and next data

These proofs exercise consumer compatibility, not real-world route accuracy,
scale, performance, publication rights or a complete DataMind pipeline. Existing
human review, synthetic-stop exclusion, quarantine and publication gates are
unchanged. No existing database, private Desktop data or real feed catalog was
used. No PR was merged and no upstream submission is made from this integration.

For work on an actual city, [Bring your city](https://github.com/josuecuguy1307/datamind-gtfs/issues/new?template=city-data.md)
with available sources and the failing workflow, or discuss it in the
[ML DataMind community](https://discord.gg/58qExtuE5p). Those intake paths do not
turn this fixture into approved real-city output.
