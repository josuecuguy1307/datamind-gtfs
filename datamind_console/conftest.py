"""Console-wide test isolation.

``PipelineAutopilot`` persists every run as JSON. Without an explicit
directory it used ``datamind_console/orchestrator_logs/autopilot_runs`` inside
the repository, so each test run left dozens of files there and loaded the
thousands already present. Tests now get a fresh temporary directory.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_autopilot_runs(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAMIND_AUTOPILOT_RUNS_DIR", str(tmp_path_factory.mktemp("autopilot_runs")))
