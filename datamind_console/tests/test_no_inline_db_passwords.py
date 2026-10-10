"""No tracked file may carry a literal database password.

On 2026-10-08 a full DSN with its password ended up in the Postgres log, and
the same password was committed in a migration runbook. Local Postgres uses
``trust``; DSNs need no password. Documentation placeholders are allowed.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
URL_PASSWORD = re.compile(r"postgres(?:ql)?://[A-Za-z0-9_.-]+:([^@/\s]+)@")
ENV_PASSWORD = re.compile(r"PGPASSWORD=([^\s\"'$<{]+)")
_SCANNER_FIXTURES = {"tools/secret_scan.py", "tools/tests/test_secret_scan.py"}
PLACEHOLDERS = {"pass", "password", "PASSWORD", "PASS", "pwd", "secret", "changeme", "xxx", "***"}


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    files = []
    for rel in out.decode().split("\0"):
        if not rel or rel.startswith("workspace/_backups/") or rel.endswith((".ipynb", ".zip", ".png", ".pdf", ".pbf", ".parquet")):
            continue
        path = ROOT / rel
        if path.resolve() == Path(__file__).resolve() or rel in _SCANNER_FIXTURES:
            continue  # pattern definitions and fake fixture values, not secrets
        if path.is_file() and path.stat().st_size < 2_000_000:
            files.append(path)
    return files


def test_no_literal_database_passwords_in_tracked_files() -> None:
    offenders = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern in (URL_PASSWORD, ENV_PASSWORD):
                for match in pattern.finditer(line):
                    value = match.group(1).strip("<>")
                    if value not in PLACEHOLDERS and not value.isupper():
                        offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, f"literal DB password in tracked files (values not shown): {sorted(set(offenders))}"
