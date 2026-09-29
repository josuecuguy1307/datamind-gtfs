from __future__ import annotations

from unittest.mock import patch

import pytest

from phase4_naming.phase4_semantics.common import db


def test_local_only_phase4_never_uses_server_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAMIND_LOCAL_ONLY_MODE", "true")
    monkeypatch.delenv("LOCAL_DB_DSN", raising=False)
    monkeypatch.delenv("DATAMIND_LOCAL_DB_DSN", raising=False)
    monkeypatch.delenv("DB_DSN_LOCAL", raising=False)
    monkeypatch.setenv("DB_DSN", "postgresql://server.example/project")

    with pytest.raises(RuntimeError, match="LOCAL_DB_DSN"):
        db.get_db_dsn()
    with patch.object(db.psycopg2, "connect") as connect:
        with pytest.raises(RuntimeError, match="LOCAL_DB_DSN"):
            with db.get_conn():
                pass
    connect.assert_not_called()
