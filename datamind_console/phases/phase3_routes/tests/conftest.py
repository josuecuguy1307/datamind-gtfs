"""Phase 3 unit tests exercise client logic with fake connections.

Catalog verification (``Phase3Client._require_schema``) is covered in
``datamind_console/tests/test_schema_guard.py``. Here it is replaced by a no-op
so the fakes don't need to emulate ``pg_catalog``. It used to *execute
migration SQL*, which made this suite depend on, and write to, a live
database.
"""
from __future__ import annotations

import pytest

from datamind_console.phases.phase3_routes.client import Phase3Client


@pytest.fixture(autouse=True)
def _skip_schema_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Phase3Client, "_require_schema", staticmethod(lambda unit: None))
