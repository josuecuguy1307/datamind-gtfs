# Security hygiene: secrets in the repository

State on 2026-10-09, measured with `tools/secret_scan.py`. The scanner
prints locations and an 8-character SHA-256 fingerprint, never a value.

## Working tree

`python tools/secret_scan.py --tracked` → **0 findings.** Two guards keep it
that way:

* `datamind_console/tests/test_no_inline_db_passwords.py`: fails on any
  literal DB password in a tracked file;
* `tools/tests/test_secret_scan.py`: covers the scanner itself.

No `.env`, key, certificate or credentials file is tracked.

## Pre-push guard (installed)

`tools/git-hooks/pre-push` scans exactly the commits a push would send
(`secret_scan.py --push <local sha> <remote sha>`) and refuses the push if
any of them adds a secret. It prints fingerprints, never values. It is
installed in this clone at `.git/hooks/pre-push`; for a new clone:

```bash
cp tools/git-hooks/pre-push .git/hooks/pre-push
```

Remove that file to uninstall it. `tools/tests/test_secret_scan.py` checks it
end to end against a throwaway bare remote: a clean push goes through, a push
with a planted password is refused, and the remote never receives it.

**It refuses every branch of this history that has local commits.** `main` and this
branch both carry the 52 unpushed occurrences, so nothing can be pushed
until the owner picks one of the options below. `git push --no-verify`
bypasses the guard; use it only as an explicit decision.

## Autopilot signing key (decided 2026-10-09: warn and let each installation set its own)

The autopilot signs operator decisions with HMAC-SHA256. The key comes from
`HADES_OPERATOR_SIGNING_SECRET` (or the legacy `DATAMIND_OPERATOR_SIGNING_SECRET`);
`datamind_console/orchestrator/signing_key.py` resolves it.

* With no key set it keeps working with the public development key, logs a
  warning once, and **Settings → Autopilot signing key** shows the warning.
  The value is never shown or logged.
* Each installation creates its own key:
  `python -m datamind_console.orchestrator.signing_key --write .env`
  (adds the line, sets the file to 0600, refuses to replace an existing key
  without `--force`). `.env.example` lists the variable, empty.
* Changing the key only affects new signatures; recorded decisions keep theirs.
