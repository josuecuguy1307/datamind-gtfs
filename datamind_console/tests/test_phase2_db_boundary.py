from __future__ import annotations

from unittest.mock import patch

import pytest

from phase2_semantics.src.db import conn


def test_local_only_phase2_never_uses_server_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAMIND_LOCAL_ONLY_MODE", "true")
    monkeypatch.delenv("LOCAL_DB_DSN", raising=False)
    monkeypatch.delenv("DATAMIND_LOCAL_DB_DSN", raising=False)
    monkeypatch.delenv("DB_DSN_LOCAL", raising=False)
    monkeypatch.setenv("DB_DSN", "postgresql://server.example/project")

    assert conn._resolve_db_dsn() is None
    with patch.object(conn.psycopg2, "connect") as connect:
        with pytest.raises(RuntimeError, match="LOCAL_DB_DSN"):
            conn._get_connection()
    connect.assert_not_called()
