from __future__ import annotations

from unittest.mock import patch

import pytest

from datamind_console.phases.phase5_gtfs.client import Phase5Client


def test_local_only_phase5_never_uses_server_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAMIND_LOCAL_ONLY_MODE", "true")
    monkeypatch.delenv("LOCAL_DB_DSN", raising=False)
    monkeypatch.delenv("DATAMIND_LOCAL_DB_DSN", raising=False)
    monkeypatch.delenv("DB_DSN_LOCAL", raising=False)
    monkeypatch.setenv("DB_DSN", "postgresql://server.example/project")
    client = Phase5Client()

    with pytest.raises(RuntimeError, match="LOCAL_DB_DSN"):
        client._dsn()
    with patch("datamind_console.phases.phase5_gtfs.client.psycopg2.connect") as connect:
        with pytest.raises(RuntimeError, match="LOCAL_DB_DSN"):
            with client._conn():
                pass
    connect.assert_not_called()
