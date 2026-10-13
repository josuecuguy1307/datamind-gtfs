"""MobilityData validator wrapper: command, parsing and the "never fake a pass" rules."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from datamind_console.ops import gtfs_canonical_validator as v


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    jar = tmp_path / "gtfs-validator.jar"
    jar.write_bytes(b"jar")
    monkeypatch.setenv("GTFS_VALIDATOR_JAR", str(jar))
    monkeypatch.setattr(v, "resolve_java", lambda: "/usr/bin/java")
    feed = tmp_path / "feed.zip"
    feed.write_bytes(b"zip")
    return feed


def _runner(report: dict | None, *, returncode: int = 0, system_errors: dict | None = None):
    if isinstance(report, dict) and isinstance(report.get("notices"), list) and "summary" not in report:
        report = {"summary": {"validatorVersion": "7.1.0"}, **report}
    if system_errors is None:
        system_errors = {"notices": []}
    calls: list[list[str]] = []

    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(cmd)
        out = Path(cmd[cmd.index("-o") + 1])
        if report is not None:
            (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
        if system_errors is not None:
            (out / "system_errors.json").write_text(json.dumps(system_errors), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="")

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


def test_command_skips_update_check_and_adds_country(ready: Path, tmp_path: Path) -> None:
    cmd = v.build_command("/usr/bin/java", Path("/x/jar.jar"), ready, tmp_path, country_code="EC")
    assert "-svu" in cmd and cmd[cmd.index("-c") + 1] == "ec"
    assert cmd[cmd.index("-i") + 1] == str(ready)


def test_pass_requires_zero_errors(ready: Path, tmp_path: Path) -> None:
    report = {"summary": {"validatorVersion": "7.1.0"}, "notices": [{"code": "expired_calendar", "severity": "WARNING", "totalNotices": 3}]}
    res = v.run_validator(ready, output_dir=tmp_path / "out", runner=_runner(report))
    assert res.status == "pass" and res.ok
    assert res.counts["WARNING"] == 3 and res.counts["ERROR"] == 0
    assert res.validator_version == "7.1.0"


def test_errors_fail_and_are_sorted_first(ready: Path, tmp_path: Path) -> None:
    report = {"notices": [
        {"code": "expired_calendar", "severity": "WARNING", "totalNotices": 50},
        {"code": "foreign_key_violation", "severity": "ERROR", "totalNotices": 7},
    ]}
    res = v.run_validator(ready, output_dir=tmp_path / "out", runner=_runner(report))
    assert res.status == "fail" and not res.ok
    assert res.notices[0]["code"] == "foreign_key_violation"


def test_missing_input_is_an_error_not_a_pass(ready: Path, tmp_path: Path) -> None:
    runner = _runner({"notices": []})
    res = v.run_validator(tmp_path / "does_not_exist.zip", output_dir=tmp_path / "out", runner=runner)
    assert res.status == "error" and not res.ok
    assert runner.calls == []  # type: ignore[attr-defined]


def test_nonzero_exit_or_system_errors_never_pass(ready: Path, tmp_path: Path) -> None:
    # The real validator writes an empty report.json plus system_errors.json
    # when it cannot read the feed; that must not read as "0 errors".
    res = v.run_validator(
        ready, output_dir=tmp_path / "out",
        runner=_runner({"notices": []}, returncode=255, system_errors={"notices": [{"code": "i_o_error", "totalNotices": 1}]}),
    )
    assert res.status == "error" and not res.ok
    assert any(n["code"] == "i_o_error" for n in res.notices)


def test_missing_report_is_an_error(ready: Path, tmp_path: Path) -> None:
    res = v.run_validator(ready, output_dir=tmp_path / "out", runner=_runner(None, returncode=1))
    assert res.status == "error"


def test_unavailable_without_java_or_jar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GTFS_VALIDATOR_JAR", str(tmp_path / "missing.jar"))
    monkeypatch.setattr(v, "resolve_java", lambda: None)
    avail = v.availability()
    assert not avail.ready and set(avail.missing) == {"jar", "java"}
    res = v.run_validator(tmp_path / "x.zip", output_dir=tmp_path / "out")
    assert res.status == "error"


@pytest.mark.parametrize("report", [None, {}, [], {"notices": None}, {"notices": {}},
    {"notices": [None]}, {"notices": [{"code": "bad", "severity": "UNKNOWN", "totalNotices": 1}]},
    {"notices": [{"code": "bad", "severity": "ERROR", "totalNotices": -1}]},
    {"notices": [{"code": "bad", "severity": "ERROR", "totalNotices": "1"}]},
    {"notices": [{"code": "bad", "severity": "ERROR", "totalNotices": True}]},
    {"notices": [], "summary": []}])
def test_malformed_report_never_passes(ready, tmp_path, report):
    result = v.run_validator(ready, output_dir=tmp_path / "out", runner=_runner(report))
    assert not result.ok and result.status == "error"


@pytest.mark.parametrize("filename,content", [("report.json", "{broken"),
    ("system_errors.json", "{broken"), ("system_errors.json", "[]"),
    ("system_errors.json", '{}'), ("system_errors.json", '{"notices":[null]}')])
def test_unreadable_json_never_passes(ready, tmp_path, filename, content):
    def runner(cmd, **kwargs):
        out = Path(cmd[cmd.index("-o") + 1])
        (out / "report.json").write_text('{"summary":{"validatorVersion":"7.1.0"},"notices":[]}')
        (out / filename).write_text(content)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    result = v.run_validator(ready, output_dir=tmp_path / "out", runner=runner)
    assert not result.ok and result.status == "error"


def test_stale_report_refused(ready, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "report.json").write_text('{"summary":{"validatorVersion":"7.1.0"},"notices":[]}')
    runner = _runner(None)
    result = v.run_validator(ready, output_dir=out, runner=runner)
    assert result.status == "error" and not result.ok
    assert not runner.calls


@pytest.mark.parametrize("exception", [FileNotFoundError("java missing"), PermissionError("denied"),
                                      subprocess.TimeoutExpired("java", 1)])
def test_process_exception_never_passes(ready, tmp_path, exception):
    def runner(*args, **kwargs):
        raise exception
    result = v.run_validator(ready, output_dir=tmp_path / "out", runner=runner)
    assert result.status == "error" and not result.ok


def test_system_error_with_zero_exit_never_passes(ready, tmp_path):
    result = v.run_validator(ready, output_dir=tmp_path / "out",
        runner=_runner({"notices": []}, system_errors={"notices": [{"code": "system_failure"}]}))
    assert result.status == "error" and not result.ok


def test_failed_process_with_empty_report_never_passes(ready, tmp_path):
    result = v.run_validator(ready, output_dir=tmp_path / "out", runner=_runner({"notices": []}, returncode=1))
    assert result.status == "error" and not result.ok


@pytest.mark.parametrize("report", [{"notices": []}, {"summary": {}, "notices": []},
    {"summary": {"validatorVersion": None}, "notices": []}])
def test_missing_version_is_an_error(ready, tmp_path, report):
    def runner(cmd, **kwargs):
        out = Path(cmd[cmd.index("-o") + 1])
        (out / "report.json").write_text(json.dumps(report))
        (out / "system_errors.json").write_text('{"notices":[]}')
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    result = v.run_validator(ready, output_dir=tmp_path / "out", runner=runner)
    assert result.status == "error" and not result.ok


def test_missing_system_errors_report_is_an_error(ready, tmp_path):
    def runner(cmd, **kwargs):
        out = Path(cmd[cmd.index("-o") + 1])
        (out / "report.json").write_text('{"summary":{"validatorVersion":"7.1.0"},"notices":[]}')
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    result = v.run_validator(ready, output_dir=tmp_path / "out", runner=runner)
    assert result.status == "error" and not result.ok
