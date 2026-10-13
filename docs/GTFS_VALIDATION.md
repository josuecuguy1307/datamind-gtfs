# GTFS validation

The console used to say "validated" for checks that were not GTFS validation.
There are now three separate layers. They are shown side by side, each with
its own state, and none of them is called *valid GTFS* except the canonical
validator.

| Layer | What it checks | Where it runs | Code |
|---|---|---|---|
| **Structure check** | the ZIP opens; required files and columns exist; UTF-8; coordinates parse | GTFS operations → *1 · Validate*, on the selected file | `datamind_console/ops/gtfs_validate.py` |
| **DataMind quality check** | every GTFS table of a Phase 5 export has rows, and `stop_times` reference existing stops and trips | Phases → 5 → *Step 06*, on export runs in the DB | Phase 5 step 06 (`phase5_gtfs/client.py`) |
| **MobilityData validator** | full GTFS conformance: references, times, shapes, calendars, feed info, ~200 rules | GTFS operations, behind an explicit button | `datamind_console/ops/gtfs_canonical_validator.py`, `tools/gtfs-validator.jar` |

**Publish-ready** = structure check passed **and** the MobilityData validator
reported **0 errors** on the same file (same resolved path). Anything else is
shown as *Not publish-ready yet*.

Each layer state is one of `passed`, `failed`, `not run`, or (for the quality
check on an arbitrary ZIP) *runs on Phase 5 exports*. A layer that did not run
is never shown as passed.

## The MobilityData validator

The wrapper defaults to `tools/gtfs-validator.jar` (MobilityData 7.x), but
the public checkout does not include that binary. Configure `GTFS_VALIDATOR_JAR`
or supply the JAR locally. It runs only when the operator presses the button:

```bash
java -jar tools/gtfs-validator.jar -i <feed.zip> -o <report dir> -svu -p [-c <country>]
```

`run_validator()` reports `error`, never `pass`, when:

* the input file does not exist;
* `java` or the jar is missing;
* the process exits non-zero, cannot launch, or times out;
* reports are missing, malformed, lack a validator version, or the output
  directory already contains reports from another run;
* `system_errors.json` contains any notice (the validator writes an empty
  `report.json` and exit code 255 for a missing input; an earlier version of
  the wrapper read that as a pass).

Otherwise the result is `pass` when there are 0 `ERROR` notices, `fail`
otherwise, with counts by severity and the top notice codes.

Configuration (all optional): `GTFS_VALIDATOR_JAR`,
`GTFS_VALIDATOR_COUNTRY_CODE` (e.g. `ec`), `GTFS_VALIDATOR_TIMEOUT_S`
(default 600), `JAVA_HOME`.

## Publishing

Publishing remains a human action.

* Local publish requires a passing structure check.
* If the MobilityData validator has not passed on this file, the operator must
  tick *I understand this file has not passed the MobilityData GTFS
  validator* before the publish button is enabled. Nothing is blocked
  silently, and nothing is published automatically.
* AWS deploy additionally requires its confirmation box and the pre-deploy
  enforcer (`ops/pre_deploy_enforcer.py`). With no upload it auto-detects
  `<otp router>/latest_upload_gtfs.zip`; that file now needs the same
  acknowledgment unless the validator passed on it.
* **Quarantine.** `phase5_gtfs/quarantine.json` lists GTFS files (by SHA-256,
  so copies and renames are caught) that failed the validator. Local publish
  and AWS full deploy refuse them outright, with no acknowledgment override,
  both in the page and inside the publish actions
  (`datamind_console/ops/gtfs_quarantine.py`). An unreadable registry refuses
  every publish. Remove an entry only after the artifact passes with 0 errors.

## Reproducible public consumer fixture

See [Synthetic GTFS proof](SYNTHETIC_GTFS_PROOF.md) for a DB-free, CC0 fixture,
checksum-pinned validator command, independent parser smoke check, raw evidence
and explicit limits. This validates a synthetic consumer feed; it does not
reconstruct a reviewed Phase 5 database export or authorize publication.
