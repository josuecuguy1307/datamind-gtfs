# Executed three-consumer proof

Original source fixture: PR1, SHA-256
`602b8192c4f91f5f064942e582db4bb6bbded89c83c1ddde49207ad1eeb6eea5`.
No fixture bytes or original MobilityData evidence were changed.

A source snapshot exported from the staged DataMind Git tree had no `.env`,
node_modules, databases or downloaded binaries. Commands below were run from
`work/datamind-gtfs` using a separate clean snapshot in `work/consumer-clean-checkout`:

```sh
npm ci --prefix ../consumer-clean-checkout/scripts/gtfs_consumers --legacy-peer-deps
python3 ../consumer-clean-checkout/scripts/gtfs_consumers/run.py \
  --feed ../../outputs/synthetic-gtfs-proof/synthetic-gtfs.zip \
  --output ../consumer-clean-checkout/.cache/verified-consumers \
  --postgres-bin /opt/homebrew/opt/postgresql@17/bin \
  --transitland ../consumer-clean-checkout/.cache/transitland-v1.3.4 \
  --download-transitland
```

Clean run: **2026-10-13T20:43:35.452848Z → 20:43:48.645425Z**.
Runner exit **0**, explicit aggregate result **pass_with_known_upstream_defect**.
Every resolved CLI invocation, timestamp, return code, log path, private
PostgreSQL connection override, package integrity and tool version is in
`evidence.json`. Both socket and disposable cluster were removed after stopping;
no existing DB connection was used. The commands recreate all three proofs.

| Consumer | Actual result |
|---|---|
| gtfs-via-postgres 4.12.0 / PostgreSQL 17.7 / PostGIS 3.6.1 | Counts, references, dates, time/geography controls verified; known upstream shape SRID defect reproduced and explicit query workaround applied |
| gtfs-via-duckdb 5.0.1 / native DuckDB v1.4.4 / Node API 1.4.4-r.4 | All equivalent assertions pass; released `materialized-view` flag fails as expected (exit 1) |
| Transitland v1.3.4, release commit 64f508789d6ae75bfe913fb46e9f86556ee621d7 | Validation success; source counts reconciled; no error/warning groups; local copy preserves semantic source fields and ordered shapes |

Shared SQL assertions: 1 agency, 1 route, 3 stops, 4 trips, 12 stop times,
2 shapes/6 source points, 1 calendar, 1 calendar exception, 1 feed_info,
364 service days, 4,368 arrivals/departures and 2,912 connections.
No orphan references; injected query-only orphan control 1; near/far controls
1/0; S1-S2 geodesic distance 553.48644774 m; correct directional starts 2;
monotonic times; maximum interval 24:35:00 / 88,500 seconds. Removed service
date 2026-10-23 has zero service-day rows/departures; following service day 12;
5 prior-service after-midnight departures correctly occur on its clock date.

PostgreSQL `shapes_aggregated.shape` SRID **0**, stop geography SRID **4326**.
`shape-srid-repro.sql` fails with exit **3**, SQLSTATE **XX000** and
`ST_Equals: Operation on mixed SRID geometries (Point, 0) != (Point, 4326)`.
Raw error is `postgres-srid-error.txt`. A separate non-mutating fix-expression
control returns source SRID 4326 and correct shape start true. Upstream
[issue #48](https://github.com/public-transport/gtfs-via-postgres/issues/48) already
tracks this; the campaign prepares that contribution separately. No duplicate
issue/fix was submitted, and the pinned importer remains unmodified.

Transitland changes decimal formatting and shape sequence identifiers
[1,2,3] to [0,3,6]. The harness initially required literal field equality and
failed on these valid normalizations. It was corrected to compare decimal
values and ordered shape points while still refusing changes to coordinates,
references, calendar exceptions or after-midnight times. Tests cover those
failure cases. The copy is a local derived artifact with its own recorded hash,
not the exact source ZIP and not covered by the original canonical validation.

The first Docker attempt was unavailable until Docker Desktop started; the
`postgis/postgis:17-3.5` pull then reported no ARM64 manifest. Native PostgreSQL
17.7/PostGIS 3.6.1 succeeded in fresh isolated clusters, so Docker is not a
prerequisite or a claimed completed proof. A clean-snapshot preparation attempt
also caught whitespace in literal patch context; a zero-context candidate patch
resolved that artifact-format issue before the successful clean run.

## Tests

```sh
.venv/bin/python -m pytest -q -rs \
  datamind_console/tests/test_gtfs_consumer_proofs.py \
  datamind_console/tests/test_gtfs_canonical_validator.py \
  datamind_console/tests/test_synthetic_gtfs_proof.py \
  datamind_console/tests/test_gtfs_quarantine.py \
  datamind_console/tests/test_phase5_rejected_synthetic_stops.py \
  datamind_console/tests/test_phase5_db_boundary.py \
  datamind_console/tests/test_human_review.py \
  datamind_console/tests/test_human_review_integration.py
```

**94 passed, 2 skipped**, raw output `tests.txt`. Skipped tests require
`DATAMIND_DISPOSABLE_TEST_DSN`; they were not run on either the existing
machine's databases or the consumer cluster. No full DataMind pipeline or
full repository suite is claimed. The new tests verify metric/report failure
states, semantic copy reconciliation, environment isolation, wrong-hash/stale
output refusal and process failure/timeout handling.

## Source review and standalone candidate

Inspected target main commits:

- public-transport/gtfs-via-postgres `aeb96aa9d1fda926f70ba7ab3ec9f1b5735e81f4`;
  README Contributing section and lib/shapes.js/stops.js/stop_times.js.
- public-transport/gtfs-via-duckdb `74c1e819492bf115dd0d9e7d5fc50902be633dce`;
  README Contributing section, package bin, CLI, spatial/calendar/time modules.
- interline-io/transitland-lib `0ec8a829b0ee1b8438d5517823e2adaf86f2a779`;
  README Development/Licenses, current validator/copy command/report code and
  release v1.3.4 CLI docs. Binary hash checked against the official release
  API digest `c8da1716051b3a56bc475c75ee6fc0a5dbd385d7fe917ac278b2b7903d421c32`.

No separate CONTRIBUTING.md/AGENTS.md appeared in those checkouts. No target
fork/branch, upstream issue, upstream PR or message was created by this work.
The candidate `upstream-candidates/duckdb-command-name.patch` changes only four
README/help lines to use the installed `gtfs-to-duckdb` executable, fixing the
stale `import-gtfs-into-duckdb` text. It applies against the inspected main
with `git apply --unidiff-zero`; the reverse application check succeeded in
the locally edited clone. It is intended for the project's Apache 2.0
contribution terms. It includes no promotional link, synthetic dataset or
DataMind dependency. The campaign's feed-by-route-date doc correction is
not duplicated. No Transitland documentation gap/bug was demonstrated.

All binaries, DBs, generated ZIPs, SQL import and full process logs are local
artifacts. This public PR commits only recipes, lockfile, tests, candidate text
and compact evidence. Licenses and existing human review/publication gates
remain intact. The DataMind docs include the authorized city/community intake
links; candidate upstream text does not.
