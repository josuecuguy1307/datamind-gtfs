# Executed validation record — 2026-10-13

Base: `33d2b20059aa0ec69c4d87a98c6dd53c427cc262` on public `main`.
Branch: `proof/synthetic-gtfs-validation`.

A clean source snapshot was exported from the staged Git tree into
`work/clean-proof-checkout`; it contained no `.env`, regional files, cache, JAR,
or virtual environment. A separate Python 3.14.7 environment was created:

```sh
python3 -m venv ../proof-venv
../proof-venv/bin/python -m pip install -r scripts/requirements-gtfs-proof.txt
../proof-venv/bin/python scripts/synthetic_gtfs_proof.py \
  --output .cache/synthetic-parser-proof --start-date 2026-10-13 \
  --download-validator --parser-smoke
```

These commands ran from the clean snapshot. `evidence.json` records exact
resolved executable/script paths and validator arguments; `report.json` and
`system_errors.json` are the unmodified real MobilityData reports.
The checksum-pinned download and independent parser both succeeded.

| Result | Actual value |
|---|---|
| Clean proof began (UTC) | 2026-10-13T18:07:53.888362+00:00 |
| MobilityData report timestamp | 2026-10-13T13:07:58-05:00 |
| Validator | 7.1.0, exit 0 |
| ERROR / WARNING / INFO | 0 / 0 / 0 |
| Notice codes | none |
| System notices | none |
| Parser | Partridge 1.1.2, pass; 24:35:00 → 88,500 seconds |
| Feed SHA-256 | `602b8192c4f91f5f064942e582db4bb6bbded89c83c1ddde49207ad1eeb6eea5` |
| JAR SHA-256 | `52c2785089aaf04e7ba1bb11b2db215692e2622eb0e196b823c194d156d9b58c` |

An earlier local run and a final revalidation (`final-evidence.json`) reproduced
the same feed hash and zero notices. No fixture/export error was suppressed or
waived. **Warning disposition: none in these runs.** Future dated runs may
produce different notices, especially after the fixed service window expires.
Review and retain those reports rather than generalizing this pass.

A separate deliberately corrupt ZIP (bytes `This is deliberately not a ZIP`)
was passed to the same `run_validator()` with the same pinned JAR. The exact
input/output paths and timestamp are retained in
`negative-corrupt-zip/report.json`; the validator argument pattern was:

```sh
/usr/bin/java -jar <clean checkout>/.cache/gtfs-proof/gtfs-validator-7.1.0-cli.jar \
  -i <clean checkout>/.cache/not-a-feed.zip \
  -o <clean checkout>/.cache/negative-real-validator -svu -p
```

It returned exit **255**, GTFS counts **0/0/0**, and system notice **`i_o_error`**
(total 1). The wrapper returned **`status="error", ok=False`**. This demonstrates
why a zero error count alone cannot establish a validation pass.

## Tests actually executed

```sh
.venv/bin/python -m pytest -q -rs \
  datamind_console/tests/test_gtfs_canonical_validator.py \
  datamind_console/tests/test_synthetic_gtfs_proof.py \
  datamind_console/tests/test_gtfs_quarantine.py \
  datamind_console/tests/test_phase5_rejected_synthetic_stops.py \
  datamind_console/tests/test_phase5_db_boundary.py \
  datamind_console/tests/test_human_review.py \
  datamind_console/tests/test_human_review_integration.py
```

**55 passed, 2 skipped**; raw output is `tests.txt`. The two human-review
integration tests explicitly require `DATAMIND_DISPOSABLE_TEST_DSN`; no DB was
configured or accessed, so they are reported as skipped, never passed.
The existing quarantine test confirms AWS refusal occurs before a remote step.

Initial test collection failed because `psycopg2`, then `streamlit`, was absent
from the isolated environment. These were installed locally
(`psycopg2-binary==2.9.11`, `python-dotenv==1.2.2`, `streamlit==1.55.0`,
`pytest==8.4.2`) and the full selected suite then passed. Installing Streamlit
changed that test environment's pandas dependency to 2.3.3; the **clean consumer
proof** used the separate environment with the pinned pandas 3.0.6 requirements.
The full repository suite and DB-backed pipeline were not run.

Only source, documentation and JSON/test evidence are committed. Generated ZIPs,
validator binaries, HTML reports and environments remain local artifacts.
No production DB, private Desktop data, deployment, merge, upstream issue,
or dataset publication was performed. Existing human review/publication gates
are unchanged; only canonical failure reporting was strengthened.
