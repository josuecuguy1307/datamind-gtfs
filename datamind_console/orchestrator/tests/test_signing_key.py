"""The autopilot signing key: own key from env, warned fallback, and the --write helper."""
from __future__ import annotations

import logging

import pytest

from datamind_console.orchestrator import signing_key


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in signing_key.ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(signing_key, "_warned", False)


def test_env_var_wins_and_source_never_contains_the_value(monkeypatch) -> None:
    monkeypatch.setenv("HADES_OPERATOR_SIGNING_SECRET", "own-key-123")
    key, source = signing_key.resolve()
    assert key == "own-key-123" and source == "HADES_OPERATOR_SIGNING_SECRET"
    assert "own-key-123" not in signing_key.source()
    assert not signing_key.is_development_key()


def test_legacy_env_var_still_works(monkeypatch) -> None:
    monkeypatch.setenv("DATAMIND_OPERATOR_SIGNING_SECRET", "legacy")
    assert signing_key.resolve() == ("legacy", "DATAMIND_OPERATOR_SIGNING_SECRET")


def test_explicit_argument_wins(monkeypatch) -> None:
    monkeypatch.setenv("HADES_OPERATOR_SIGNING_SECRET", "env")
    assert signing_key.resolve("arg") == ("arg", "argument")


def test_fallback_keeps_working_but_warns_once(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=signing_key.__name__):
        first = signing_key.resolve()
        signing_key.resolve()
    assert first == (signing_key.DEVELOPMENT_KEY, signing_key.DEVELOPMENT_SOURCE)
    assert signing_key.is_development_key()
    warnings = [r for r in caplog.records if "development key" in r.getMessage()]
    assert len(warnings) == 1


def test_generate_gives_long_distinct_keys() -> None:
    a, b = signing_key.generate(), signing_key.generate()
    assert a != b and len(a) >= 60


def test_write_adds_then_refuses_to_overwrite_without_force(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("DB_DSN=postgresql://u@127.0.0.1/db\nHADES_OPERATOR_SIGNING_SECRET=\n")
    assert signing_key.write_to_env_file(env, "k1") == "replaced"  # empty placeholder gets filled
    assert "HADES_OPERATOR_SIGNING_SECRET=k1" in env.read_text()
    assert "DB_DSN=" in env.read_text()
    with pytest.raises(FileExistsError):
        signing_key.write_to_env_file(env, "k2")
    assert signing_key.write_to_env_file(env, "k2", force=True) == "replaced"
    assert env.read_text().count("HADES_OPERATOR_SIGNING_SECRET=") == 1
    assert oct(env.stat().st_mode)[-3:] == "600"


def test_cli_write_never_prints_the_key(tmp_path, capsys) -> None:
    env = tmp_path / ".env"
    assert signing_key.main(["--write", str(env)]) == 0
    key = env.read_text().split("=", 1)[1].strip()
    out = capsys.readouterr()
    assert key and key not in out.out + out.err
