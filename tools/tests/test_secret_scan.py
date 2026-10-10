"""The secret scanner finds credentials without ever printing them."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("secret_scan", Path(__file__).resolve().parents[1] / "secret_scan.py")
secret_scan = importlib.util.module_from_spec(_spec)
sys.modules["secret_scan"] = secret_scan  # dataclasses resolve their module by name
_spec.loader.exec_module(secret_scan)  # type: ignore[union-attr]


def _kinds(line: str) -> list[str]:
    return [kind for kind, _ in secret_scan.scan_line(line)]


def test_detects_literal_db_passwords_and_keys() -> None:
    assert _kinds('DSN = "postgresql://app_user:s3cr3tValue@127.0.0.1:5432/db"') == ["db_url_password"]
    assert _kinds("PGPASSWORD=s3cr3tValue pg_dump -d db") == ["pgpassword"]
    assert _kinds("key = 'AKIAABCDEFGHIJKLMNOP'") == ["aws_access_key"]
    assert _kinds("-----BEGIN OPENSSH PRIVATE KEY-----") == ["private_key"]


def test_ignores_placeholders_and_variables() -> None:
    assert _kinds('export DB_DSN="postgresql://user:pass@host:5432/dbname"') == []
    assert _kinds("postgresql://USER:PASSWORD@HOST:PORT/DB_NAME") == []
    assert _kinds("postgresql://postgres:postgres@localhost:5432/postgres") == []
    assert _kinds('PGPASSWORD="$PGPASSWORD" psql') == []
    assert _kinds("postgresql://app_user@127.0.0.1:5432/datamind_ml") == []


def test_findings_carry_a_fingerprint_never_the_value() -> None:
    (kind, fp), = secret_scan.scan_line("postgresql://u:s3cr3tValue@h/db")
    assert kind == "db_url_password" and len(fp) == 8 and "s3cr3t" not in fp


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True,
    ).stdout.strip()


def test_pre_push_hook_blocks_a_secret_and_lets_clean_pushes_through(tmp_path: Path) -> None:
    import shutil
    import subprocess

    tools = Path(__file__).resolve().parents[1]
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", str(work))
    (work / "tools").mkdir()
    shutil.copy(tools / "secret_scan.py", work / "tools" / "secret_scan.py")
    shutil.copy(tools / "git-hooks" / "pre-push", work / ".git" / "hooks" / "pre-push")
    (work / ".git" / "hooks" / "pre-push").chmod(0o755)
    _git(work, "remote", "add", "origin", str(remote))

    (work / "app.py").write_text('DSN = "postgresql://app@127.0.0.1:5432/db"\n')
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "clean")
    clean = subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, capture_output=True, text=True)
    assert clean.returncode == 0, clean.stderr

    (work / "app.py").write_text('DSN = "postgresql://app:s3cr3tValue@127.0.0.1:5432/db"\n')
    _git(work, "commit", "-am", "leak")
    blocked = subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, capture_output=True, text=True)
    assert blocked.returncode != 0
    assert "refusing to push" in blocked.stderr and "s3cr3tValue" not in blocked.stderr + blocked.stdout
    assert _git(remote, "log", "--format=%s", "main") == "clean"  # the leak never reached the remote
