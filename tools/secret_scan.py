"""Find secrets in tracked files or in commits that have not been pushed.

Never prints a secret: findings show where, what kind, and an 8-character
SHA-256 fingerprint, so the same value can be recognised across reports.

    python tools/secret_scan.py --tracked            # working tree (tracked files)
    python tools/secret_scan.py --unpushed           # commits on local branches not on any remote
    python tools/secret_scan.py --range origin/main..HEAD
    python tools/secret_scan.py --push <local sha> <remote sha>  # what a push would send

Exit code 1 when anything is found. Suitable as a pre-push check (see
docs/SECURITY_HYGIENE.md); it is not installed as a hook automatically.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]

PLACEHOLDERS = {"pass", "password", "pwd", "secret", "changeme", "xxx", "***", "user", "example", "postgres"}  # "postgres": the stock local default, not a secret
SKIP_PREFIXES = ("workspace/_backups/",)
SKIP_SUFFIXES = (".ipynb", ".zip", ".png", ".jpg", ".pdf", ".pbf", ".parquet", ".obj")
SKIP_FILES = {"tools/secret_scan.py", "tools/tests/test_secret_scan.py"}  # the scanner and its fake fixtures

# (kind, compiled pattern, group holding the secret value)
PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("db_url_password", re.compile(r"postgres(?:ql)?://[A-Za-z0-9_.-]+:([^@/\s\"'`]+)@"), 1),
    ("pgpassword", re.compile(r"PGPASSWORD=([^\s\"'$<{`]+)"), 1),
    ("aws_access_key", re.compile(r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b"), 1),
    ("openai_anthropic_key", re.compile(r"\b(sk-(?:ant-|proj-|live-)?[A-Za-z0-9_-]{24,})"), 1),
    ("github_token", re.compile(r"\b(ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,})"), 1),
    ("slack_token", re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})"), 1),
    ("google_api_key", re.compile(r"\b(AIza[0-9A-Za-z_-]{35})"), 1),
    ("huggingface_token", re.compile(r"\b(hf_[A-Za-z0-9]{30,})"), 1),
    ("private_key", re.compile(r"(-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----)"), 1),
)


@dataclass(frozen=True)
class Finding:
    where: str
    kind: str
    fingerprint: str

    def __str__(self) -> str:
        return f"{self.where}  {self.kind}  sha256:{self.fingerprint}"


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def is_placeholder(value: str) -> bool:
    bare = value.strip("<>[]{}()")
    return bare.lower() in PLACEHOLDERS or bare.isupper() or bare.startswith("$")


def scan_line(line: str) -> Iterator[tuple[str, str]]:
    for kind, pattern, group in PATTERNS:
        for match in pattern.finditer(line):
            value = match.group(group)
            if kind in ("db_url_password", "pgpassword") and is_placeholder(value):
                continue
            yield kind, fingerprint(value)


def _skip(path: str) -> bool:
    return path.startswith(SKIP_PREFIXES) or path.endswith(SKIP_SUFFIXES) or path in SKIP_FILES


def scan_tracked(root: Path = ROOT) -> list[Finding]:
    files = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout.decode()
    findings: list[Finding] = []
    for rel in filter(None, files.split("\0")):
        if _skip(rel) or rel.endswith("test_no_inline_db_passwords.py"):
            continue
        path = root / rel
        try:
            if not path.is_file() or path.stat().st_size > 5_000_000:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for kind, fp in scan_line(line):
                findings.append(Finding(f"{rel}:{lineno}", kind, fp))
    return findings


def scan_log(rev_args: Iterable[str], root: Path = ROOT) -> list[Finding]:
    """Scan lines *added* by the commits selected with ``git log`` arguments."""
    out = subprocess.run(
        ["git", "-C", str(root), "log", "-p", "--no-color", "--format=@@COMMIT %h", *rev_args],
        capture_output=True, check=True,
    ).stdout.decode("utf-8", "replace")
    findings: list[Finding] = []
    commit = path = ""
    for line in out.splitlines():
        if line.startswith("@@COMMIT "):
            commit = line.split()[1]
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and not _skip(path):
            if path.endswith("test_no_inline_db_passwords.py"):
                continue
            for kind, fp in scan_line(line[1:]):
                findings.append(Finding(f"{commit} {path}", kind, fp))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tracked", action="store_true", help="scan tracked files in the working tree")
    mode.add_argument("--unpushed", action="store_true", help="scan commits on local branches that no remote has")
    mode.add_argument("--range", help="scan commits in a git revision range, e.g. origin/main..HEAD")
    mode.add_argument("--push", nargs=2, metavar=("LOCAL_SHA", "REMOTE_SHA"),
                      help="scan what pushing LOCAL_SHA would send (REMOTE_SHA all zeros for a new branch); used by the pre-push hook")
    args = parser.parse_args(argv)

    if args.tracked:
        findings = scan_tracked()
    elif args.unpushed:
        findings = scan_log(["--branches", "--not", "--remotes"])
    elif args.push:
        local_sha, remote_sha = args.push
        new_branch = set(remote_sha) == {"0"}
        findings = scan_log([local_sha, "--not", "--remotes"] if new_branch else [f"{remote_sha}..{local_sha}"])
    else:
        findings = scan_log([args.range])

    unique = sorted({(f.where, f.kind, f.fingerprint) for f in findings})
    for where, kind, fp in unique:
        print(f"{where}  {kind}  sha256:{fp}")
    values = sorted({fp for _, _, fp in unique})
    print(f"{len(unique)} finding(s), {len(values)} distinct value(s): {', '.join(values) or '-'}", file=sys.stderr)
    return 1 if unique else 0


if __name__ == "__main__":
    sys.exit(main())
