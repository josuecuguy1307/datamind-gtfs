"""Safety net for the DB-backed stop_quality suites.

Several of these modules insert fixture rows and COMMIT. They used to fall
back to the live operational database when ``DB_DSN`` was unset, so a plain
``pytest tests/`` could write to it. They now fall back to an unreachable
placeholder DSN. Modules with their own reachability guard (tests/enforcers)
skip by themselves; these suites connect in fixtures, so this hook skips
them unless ``DB_DSN`` names a database explicitly. Point ``DB_DSN`` at a
disposable copy, never at the live DB.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

PLACEHOLDER_DSN = "postgresql://unset@127.0.0.1:1/set_DB_DSN_to_a_disposable_database"
_HERE = Path(__file__).parent
_SKIP = pytest.mark.skip(reason="DB-backed test: set DB_DSN to a disposable database to run it")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("DB_DSN", PLACEHOLDER_DSN) != PLACEHOLDER_DSN:
        return
    cache: dict[str, bool] = {}
    for item in items:
        # Collection hooks see every item in the session, not just this folder's.
        if _HERE not in Path(str(item.fspath)).parents:
            continue
        path = str(item.fspath)
        if path not in cache:
            try:
                source = Path(path).read_text(encoding="utf-8")
                cache[path] = PLACEHOLDER_DSN in source
            except OSError:
                cache[path] = False
        if cache[path]:
            item.add_marker(_SKIP)
