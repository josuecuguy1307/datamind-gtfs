from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from datamind_console.services.cli_launch_service import (
    LOCAL_CLI_LAUNCH_ENV,
    REPO_ROOT,
    inspect_cli,
    launch_interactive_cli,
)


class CliLaunchServiceTests(unittest.TestCase):
    def test_inspection_requires_explicit_opt_in(self) -> None:
        status = inspect_cli(
            "codex",
            environ={},
            which=lambda _: "/usr/local/bin/codex",
            platform_name="darwin",
        )
        self.assertFalse(status.ready)
        self.assertTrue(status.executable_path)

    def test_launch_uses_separate_osascript_arguments(self) -> None:
        received: dict[str, object] = {}

        def fake_runner(command, **kwargs):
            received["command"] = command
            received["kwargs"] = kwargs
            return subprocess.CompletedProcess(command, 0, "", "")

        with TemporaryDirectory() as temp_dir:
            context = Path(temp_dir) / "context.json"
            context.write_text("{}", encoding="utf-8")
            result = launch_interactive_cli(
                "claude",
                context_path=context,
                scope_request="Read-only review",
                environ={LOCAL_CLI_LAUNCH_ENV: "true"},
                which=lambda _: "/usr/local/bin/claude",
                platform_name="darwin",
                runner=fake_runner,
            )

        self.assertTrue(result.requested)
        command = received["command"]
        self.assertEqual(command[0], "/usr/bin/osascript")
        self.assertEqual(command[-3], str(REPO_ROOT.resolve()))
        self.assertEqual(command[-2], "/usr/local/bin/claude")
        self.assertIn("Read-only review", command[-1])
        self.assertNotIn("shell", received["kwargs"])

    def test_launch_rejects_a_directory_outside_the_project(self) -> None:
        with TemporaryDirectory() as temp_dir:
            context = Path(temp_dir) / "context.json"
            context.write_text("{}", encoding="utf-8")
            result = launch_interactive_cli(
                "codex",
                context_path=context,
                scope_request="Read-only review",
                working_directory=REPO_ROOT.parent,
                environ={LOCAL_CLI_LAUNCH_ENV: "true"},
                which=lambda _: "/usr/local/bin/codex",
                platform_name="darwin",
            )
        self.assertFalse(result.requested)
        self.assertIn("project root", result.message)
