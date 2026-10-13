# Synthetic GTFS consumer proof

This is an invented **consumer fixture**, dedicated to **CC0-1.0**. It contains
one bus route, three stops, two directional shapes, four trips, twelve stop times,
a daily calendar and one removed service date. Labels explicitly say synthetic;
the coordinates are a test grid, not evidence of real stops or transit service.
No regional data, OSM extract, trained models, credentials or database is used.
The generated `LICENSE.txt` accompanies the ZIP; the dedication applies only to
the fixture data. Repository code and third-party tools retain their own licenses.

## Clean checkout

Requirements: Python 3.12+ (tested with 3.14.7), Java 17+ (tested with OpenJDK
21.0.8), and network access for the explicit first-time validator download.
Generation and canonical validation require only Python's standard library.
Run from the repository root; use a **new output directory** on each run:

```sh
python3 scripts/synthetic_gtfs_proof.py \
  --output .cache/synthetic-proof \
  --start-date 2026-10-13 --download-validator
```

For the additional independent parser smoke check:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements-gtfs-proof.txt
.venv/bin/python scripts/synthetic_gtfs_proof.py \
  --output .cache/synthetic-parser-proof \
  --start-date 2026-10-13 --download-validator --parser-smoke
```

Partridge **1.1.2** and its tested dependencies are pinned. The smoke check loads
all nine tables, asserts row counts and the service exception, and verifies that
24:35:00 is preserved as 88,500 seconds, not wrapped to the next calendar date.
Omitting the option records `not run`; requesting it without dependencies fails.

The download opt-in fetches only the public
[MobilityData v7.1.0 CLI release](https://github.com/MobilityData/gtfs-validator/releases/tag/v7.1.0).
Its SHA-256 is pinned in code:
`52c2785089aaf04e7ba1bb11b2db215692e2622eb0e196b823c194d156d9b58c`.
This is the digest measured from the official release asset; that release's API
provided no publisher digest. Every use verifies bytes against this pin. A cached
bad JAR fails rather than being silently replaced. `--cache` can select an offline
cache containing the exact `gtfs-validator-7.1.0-cli.jar`.

The command uses the existing structure checker and canonical wrapper:

```sh
java -jar <pinned jar> -i <synthetic-gtfs.zip> -o <new output>/validator -svu -p
```

No country profile is inferred. `-svu` skips update checks. The script does not
load `.env`, import DB configuration, register quarantine decisions, publish,
deploy, or modify review state. Each run writes `evidence.json`, the feed,
`LICENSE.txt`, and raw validator JSON/HTML reports. Evidence records exact argument
arrays, UTC timestamp, Java version, feed/JAR hashes, validator version, exit code,
all ERROR/WARNING/INFO totals and **all** notice codes. These are notice totals,
not the number of distinct codes. Missing dependencies, checksum mismatch,
process failure/timeout, malformed/missing reports, stale output, system errors,
or a missing/mismatched version fail the proof with a nonzero exit. Canonical
errors are retained, never suppressed. Warnings must be reviewed separately.

## Scope and limits

`build_feed_info()` and the compiler's `seconds_to_hhmmss()` are reused directly,
as are the production structure checker and canonical validator wrapper. The ZIP
uses sorted files, fixed timestamps/permissions and stored bytes so the same
explicit start date produces identical bytes across runs.

This does **not** reconstruct the DataMind pipeline or call `package_gtfs()`.
That packager and `build_calendar`, `build_routes`, `build_shapes`, `build_trips`,
and `build_stop_times` require `gtfs_work`/route/node tables in PostgreSQL.
Packaging also checks direction IDs, approved semantics and served-stop coverage.
By inspection, `scripts/load_sample_data.py` populates three routes, 26 stops and
six places, but not all chosen geometry/stop-sequence candidates, approved
semantics or active schedule inputs required by the current compiler. Mocking
those DB gates would not prove a real export, so this fixture does not do so.
A full pipeline reproduction still needs an isolated, operator-authorized DB
(PostgreSQL 15+ with PostGIS, pgvector, pgcrypto, pg_trgm, citext, unaccent, dblink),
reviewed route candidates and semantics, schedule profiles/departures and Phase 5
export state. Earlier geography/routing/AI stages can additionally need regional
resources, routing services and model/provider configuration; none was exercised.

There are no fares, accessibility guarantees, transfers, frequency service, OSM
source attribution claims, or real-world performance/coverage claims in this
small fixture. It tests references, direction/order, dates and after-midnight
time semantics from the [GTFS schedule reference](https://gtfs.org/documentation/schedule/reference/).
A pass applies to this exact file only. It does not approve synthetic nodes for
production, mark any operator review complete, bypass quarantine, or grant
permission to publish. Existing human review and publication gates remain intact.

The fixed calendar ends 2027-10-12. Later validation can legitimately report an
expired calendar; use `--start-date YYYY-MM-DD` for a fresh dated fixture and
record its new checksum. Do not alter the reference evidence to hide warnings.

## Verification

```sh
.venv/bin/python -m pip install pytest==8.4.2
.venv/bin/python -m pytest -q \
  datamind_console/tests/test_gtfs_canonical_validator.py \
  datamind_console/tests/test_synthetic_gtfs_proof.py \
  datamind_console/tests/test_gtfs_quarantine.py \
  datamind_console/tests/test_phase5_rejected_synthetic_stops.py
```

Tests assert byte reproduction, cross-table references, service bounds and
exceptions, both directional shape/stop sequences and distances, monotonically
increasing times through 24:35:00, and proof/wrapper failure states. Validator
unit tests simulate process/report failures; the recorded proof runs the real
Java validator. See `docs/proof/synthetic-gtfs/` for actual run evidence and the
executed test command/results.
