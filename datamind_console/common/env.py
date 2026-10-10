"""Project root and ``.env`` loading, shared by every entrypoint.

The console (``app.py``), the MCP server and the admin CLIs load the same
``.env`` before ``datamind_console.common.config`` builds ``CFG``, which reads
the environment at import time. Existing process variables always win, so a
caller can point a run at another database (for example a read-only DSN)
without editing ``.env``.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export "):]
    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return (key, value) if key else None


def load_project_env(path: Path | None = None) -> Path | None:
    """Load ``<project>/.env`` without overriding variables already set.

    Returns the file that was read, or ``None`` when it does not exist.
    """
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.exists():
        return None
    try:
        from dotenv import load_dotenv
    except Exception:  # pragma: no cover - python-dotenv is in requirements
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(env_path, override=False)
        return env_path
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(raw)
        if parsed and parsed[0] not in os.environ:
            os.environ[parsed[0]] = parsed[1]
    return env_path
