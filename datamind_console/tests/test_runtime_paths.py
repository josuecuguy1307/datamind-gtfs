"""Runtime paths come from configuration, never from one developer's home."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import subprocess

import pytest

from datamind_console.common import paths
from datamind_console.ops.pre_deploy_enforcer import resolve_local_otp_dirs
from datamind_console.phases.phase3_routes.constructor_runner import catalog_mirror_dir


def _cfg(root=None, extract=None):
    return SimpleNamespace(transportapp_root=root, local_gtfs_extract_dir=extract)


def test_otp_dirs_derive_from_transportapp_root(tmp_path: Path) -> None:
    extract, root = resolve_local_otp_dirs(_cfg(root=tmp_path))
    assert root == tmp_path
    assert extract == tmp_path / "otp" / "otp-data" / "routers" / "default"


def test_explicit_extract_dir_wins(tmp_path: Path) -> None:
    extract, _ = resolve_local_otp_dirs(_cfg(root=tmp_path, extract=tmp_path / "x"))
    assert extract == tmp_path / "x"


def test_unconfigured_otp_dirs_are_none() -> None:
    assert resolve_local_otp_dirs(_cfg()) == (None, None)


def test_catalog_mirror_dir_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PHASE3_CATALOG_MIRROR_DIR", str(tmp_path))
    assert catalog_mirror_dir() == tmp_path
    monkeypatch.delenv("PHASE3_CATALOG_MIRROR_DIR")
    assert catalog_mirror_dir() == Path.home() / "phase3_route_catalog"


def test_console_runtime_has_no_user_home_literals() -> None:
    offenders = []
    for path in Path("datamind_console").rglob("*.py"):
        text = str(path)
        if "/tests/" in text or path.name.startswith("test_") or "/services/" in text:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/Users/"):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, offenders


def test_no_tracked_python_file_hardcodes_a_user_home() -> None:
    root = paths.PROJECT_ROOT
    # A real user's Desktop/Downloads/Documents; generic examples such as
    # "/Users/foo/bar" in parser tests are fine. Built by concatenation so
    # this line does not match itself.
    pattern = "/Us" + "ers/[A-Za-z0-9._-]+/(Desktop|Downloads|Documents)"
    out = subprocess.run(["git", "-C", str(root), "grep", "-nE", pattern, "--", "*.py"], capture_output=True, text=True)
    hits = [line for line in out.stdout.splitlines() if not line.startswith("workspace/_backups/")]
    assert not hits, hits


def test_resolve_input_prefers_env_then_candidates_then_fails_clearly(monkeypatch, tmp_path: Path) -> None:
    present = tmp_path / "in.json"
    present.write_text("{}")
    monkeypatch.setenv("DM_TEST_INPUT", str(present))
    assert paths.resolve_input("DM_TEST_INPUT", tmp_path / "nope.json") == present
    monkeypatch.delenv("DM_TEST_INPUT")
    assert paths.resolve_input("DM_TEST_INPUT", tmp_path / "nope.json", present) == present
    with pytest.raises(paths.PathNotConfigured, match="Set DM_TEST_INPUT="):
        paths.resolve_input("DM_TEST_INPUT", tmp_path / "nope.json", description="demo")
    monkeypatch.setenv("DM_TEST_INPUT", str(tmp_path / "missing.json"))
    with pytest.raises(paths.PathNotConfigured, match="does not exist"):
        paths.resolve_input("DM_TEST_INPUT")


def test_path_not_configured_is_catchable_as_a_missing_file() -> None:
    # Optional inputs inside ``except Exception`` must stay optional.
    assert issubclass(paths.PathNotConfigured, FileNotFoundError)


def test_downloads_and_mirror_are_home_relative(monkeypatch) -> None:
    monkeypatch.delenv("PHASE3_CATALOG_MIRROR_DIR", raising=False)
    assert paths.downloads("x.json") == Path.home() / "Downloads" / "x.json"
    assert paths.phase3_catalog_mirror_dir() == Path.home() / "phase3_route_catalog"
