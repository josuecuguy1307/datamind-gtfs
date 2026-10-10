"""The quarantine blocks known-bad GTFS files by content, in every publish path."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from datamind_console.ops import gtfs_quarantine as q


def _registry(tmp_path: Path, *shas: str) -> Path:
    path = tmp_path / "quarantine.json"
    path.write_text(json.dumps({"entries": [{"sha256": s, "label": "bad feed", "reason": "122,789 errors"} for s in shas]}))
    return path


def test_blocks_by_content_not_name(tmp_path: Path) -> None:
    bad = tmp_path / "gtfs.zip"
    bad.write_bytes(b"PK-bad")
    renamed = tmp_path / "renamed_copy.zip"
    renamed.write_bytes(b"PK-bad")
    good = tmp_path / "good.zip"
    good.write_bytes(b"PK-good")
    reg = _registry(tmp_path, q.sha256_of(bad))
    assert "122,789 errors" in q.refusal(bad, registry_path=reg)
    assert q.refusal(renamed, registry_path=reg)
    assert q.refusal(good, registry_path=reg) is None


def test_missing_registry_quarantines_nothing(tmp_path: Path) -> None:
    f = tmp_path / "f.zip"
    f.write_bytes(b"x")
    assert q.refusal(f, registry_path=tmp_path / "absent.json") is None


def test_unreadable_registry_fails_closed(tmp_path: Path) -> None:
    f = tmp_path / "f.zip"
    f.write_bytes(b"x")
    reg = tmp_path / "quarantine.json"
    reg.write_text("{not json")
    assert "unreadable" in q.refusal(f, registry_path=reg)


def test_local_publish_refuses_before_doing_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datamind_console.ops import local_publish

    bad = tmp_path / "gtfs.zip"
    bad.write_bytes(b"PK-bad")
    reg = _registry(tmp_path, q.sha256_of(bad))
    monkeypatch.setattr(q, "REGISTRY_PATH", reg)
    monkeypatch.setattr(local_publish, "validate_gtfs_zip", lambda p: pytest.fail("must not get past quarantine"))
    out = local_publish.run_local_publish(cfg=None, gtfs_zip_path=bad)  # type: ignore[arg-type]
    assert out["ok"] is False and out["steps"]["quarantine"]["ok"] is False


def test_aws_full_deploy_refuses_before_any_remote_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datamind_console.widgets import gtfs_ops_widget as widget

    bad = tmp_path / "latest_upload_gtfs.zip"
    bad.write_bytes(b"PK-bad")
    monkeypatch.setattr(q, "REGISTRY_PATH", _registry(tmp_path, q.sha256_of(bad)))
    monkeypatch.setattr(widget, "run_aws_backend_push", lambda *a, **k: pytest.fail("must not deploy"))
    out = widget._aws_full_deploy_action(cfg=None, gtfs_zip=bad, log=lambda m: None, progress=lambda f, m: None)  # type: ignore[arg-type]
    assert out["ok"] is False and out["steps"]["quarantine"]["ok"] is False
