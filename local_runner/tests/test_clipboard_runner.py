from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from local_runner.scripts.runner_core import RunnerError, load_runner_config, run_clipboard_prompt


class ClipboardRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        self.inbox = self.root / "inbox"
        self.processing = self.root / "processing"
        self.done = self.root / "done"
        self.failed = self.root / "failed"
        self.results = self.root / "results"
        self.logs = self.root / "logs"

        for d in [self.inbox, self.processing, self.done, self.failed, self.results, self.logs]:
            d.mkdir(parents=True, exist_ok=True)

        self.config_path = self.root / "runner_config.yaml"
        self._write_config(timeout_seconds=3)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_config(
        self,
        *,
        timeout_seconds: int,
        codex_cmd: list[str] | None = None,
        claude_cmd: list[str] | None = None,
        require_target_explicit: bool = False,
    ) -> None:
        codex_cmd = codex_cmd or [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.read(); print('CODEX_CLIP:' + data.strip())",
        ]
        claude_cmd = claude_cmd or [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.read(); print('CLAUDE_CLIP:' + data.strip())",
        ]

        cfg = {
            "runner": {
                "timeout_seconds": timeout_seconds,
                "capture_stderr_separately": True,
                "infer_target_from_filename": True,
                "default_target": "codex",
                "log_filename": "runs.jsonl",
            },
            "clipboard": {
                "clipboard_enabled": True,
                "clipboard_prompt_filename_prefix": "clip_prompt",
                "clipboard_max_chars": 10000,
                "persist_clipboard_prompt": True,
                "require_target_explicit_for_clipboard": require_target_explicit,
                "clipboard_persist_destination": "inbox",
            },
            "paths": {
                "inbox": str(self.inbox),
                "processing": str(self.processing),
                "done": str(self.done),
                "failed": str(self.failed),
                "results": str(self.results),
                "logs": str(self.logs),
            },
            "targets": {
                "codex": {
                    "command": codex_cmd,
                    "input_mode": "stdin",
                },
                "claude": {
                    "command": claude_cmd,
                    "input_mode": "stdin",
                },
            },
        }
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")

    def test_clipboard_config_loading(self) -> None:
        cfg = load_runner_config(self.config_path)
        self.assertTrue(cfg.clipboard.enabled)
        self.assertEqual(cfg.clipboard.prompt_filename_prefix, "clip_prompt")
        self.assertEqual(cfg.clipboard.persist_destination, "inbox")

    def test_empty_clipboard_handling(self) -> None:
        with self.assertRaises(RunnerError):
            run_clipboard_prompt(
                target="codex",
                config_path=self.config_path,
                clipboard_getter=lambda: "   ",
            )

    def test_clipboard_read_failure_handling(self) -> None:
        def broken_clip() -> str:
            raise RuntimeError("clipboard backend exploded")

        with self.assertRaises(RunnerError):
            run_clipboard_prompt(
                target="codex",
                config_path=self.config_path,
                clipboard_getter=broken_clip,
            )

    def test_unknown_target_handling(self) -> None:
        with self.assertRaises(RunnerError):
            run_clipboard_prompt(
                target="unknown",
                config_path=self.config_path,
                clipboard_getter=lambda: "hello",
            )

    def test_clipboard_success_persist_lifecycle_and_logs(self) -> None:
        result = run_clipboard_prompt(
            target="codex",
            config_path=self.config_path,
            clipboard_getter=lambda: "phase2 clipboard prompt",
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.prompt_source, "clipboard")
        self.assertTrue(Path(result.output_file).exists())
        self.assertTrue(str(Path(result.prompt_file_final).resolve()).startswith(str(self.done.resolve())))

        out_text = Path(result.output_file).read_text(encoding="utf-8")
        self.assertIn("CODEX_CLIP:phase2 clipboard prompt", out_text)

        log_path = Path(result.log_file)
        line = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(line["prompt_source"], "clipboard")
        self.assertEqual(line["status"], "success")
        self.assertEqual(line["clipboard_char_count"], len("phase2 clipboard prompt"))

    def test_clipboard_timeout_handling(self) -> None:
        timeout_cmd = [sys.executable, "-c", "import time; time.sleep(2); print('late')"]
        self._write_config(timeout_seconds=1, codex_cmd=timeout_cmd)

        result = run_clipboard_prompt(
            target="codex",
            config_path=self.config_path,
            clipboard_getter=lambda: "timeout me",
        )

        self.assertEqual(result.status, "timeout")
        self.assertTrue(str(Path(result.prompt_file_final).resolve()).startswith(str(self.failed.resolve())))
        self.assertIsNotNone(result.error_summary)

    def test_clipboard_no_persist_prompt(self) -> None:
        result = run_clipboard_prompt(
            target="codex",
            config_path=self.config_path,
            clipboard_getter=lambda: "ephemeral",
            persist_prompt=False,
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.prompt_file, "<clipboard:memory>")
        self.assertEqual(result.prompt_file_final, "<clipboard:memory>")


if __name__ == "__main__":
    unittest.main()
