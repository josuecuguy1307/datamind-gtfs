from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from local_runner.scripts.runner_core import RunnerError, load_runner_config, run_prompt_file


class RunnerCoreTests(unittest.TestCase):
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
        working_directory: Path | None = None,
    ) -> None:
        codex_cmd = codex_cmd or [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.read(); print('CODEX:' + data.strip())",
        ]
        claude_cmd = claude_cmd or [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.read(); print('CLAUDE:' + data.strip())",
        ]
        cfg = {
            "runner": {
                "timeout_seconds": timeout_seconds,
                "capture_stderr_separately": True,
                "infer_target_from_filename": True,
                "default_target": None,
                "log_filename": "runs.jsonl",
                "working_directory": str(working_directory or self.root),
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

    def _write_prompt(self, name: str, text: str) -> Path:
        path = self.inbox / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_config_loading(self) -> None:
        cfg = load_runner_config(self.config_path)
        self.assertEqual(cfg.timeout_seconds, 3)
        self.assertIn("codex", cfg.targets)
        self.assertEqual(cfg.paths.inbox, self.inbox.resolve())

    def test_missing_file_handling(self) -> None:
        missing = self.inbox / "nope.txt"
        with self.assertRaises(RunnerError):
            run_prompt_file(file_path=missing, target="codex", config_path=self.config_path)

    def test_unknown_target_handling(self) -> None:
        prompt = self._write_prompt("codex_unknown_target.txt", "hello")
        with self.assertRaises(RunnerError):
            run_prompt_file(file_path=prompt, target="unknown", config_path=self.config_path)

    def test_success_run_creates_output_log_and_moves_prompt(self) -> None:
        prompt = self._write_prompt("codex_success_prompt.txt", "hello world")
        result = run_prompt_file(file_path=prompt, target="codex", config_path=self.config_path)

        self.assertEqual(result.status, "success")
        self.assertTrue(Path(result.output_file).exists())
        self.assertTrue(Path(result.prompt_file_final).exists())
        self.assertTrue(str(Path(result.prompt_file_final).resolve()).startswith(str(self.done.resolve())))

        out_text = Path(result.output_file).read_text(encoding="utf-8")
        self.assertIn("CODEX:hello world", out_text)

        log_path = Path(result.log_file)
        self.assertTrue(log_path.exists())
        log_lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertGreaterEqual(len(log_lines), 1)
        self.assertEqual(log_lines[-1]["status"], "success")

    def test_run_uses_configured_working_directory(self) -> None:
        work_dir = self.root / "project"
        work_dir.mkdir()
        cwd_cmd = [sys.executable, "-c", "import os; print(os.getcwd())"]
        self._write_config(timeout_seconds=3, codex_cmd=cwd_cmd, working_directory=work_dir)
        prompt = self._write_prompt("codex_cwd_prompt.txt", "ignored")

        result = run_prompt_file(file_path=prompt, target="codex", config_path=self.config_path)

        self.assertEqual(result.status, "success")
        self.assertEqual(Path(result.output_file).read_text(encoding="utf-8").strip(), str(work_dir.resolve()))

    def test_infer_target_from_filename_prefix(self) -> None:
        prompt = self._write_prompt("claude_auto_target.md", "review this")
        result = run_prompt_file(file_path=prompt, target=None, config_path=self.config_path)
        self.assertEqual(result.target, "claude")
        out_text = Path(result.output_file).read_text(encoding="utf-8")
        self.assertIn("CLAUDE:review this", out_text)

    def test_timeout_moves_prompt_to_failed(self) -> None:
        timeout_cmd = [sys.executable, "-c", "import time; time.sleep(2); print('late')"]
        self._write_config(timeout_seconds=1, codex_cmd=timeout_cmd)

        prompt = self._write_prompt("codex_timeout_prompt.txt", "slow")
        result = run_prompt_file(file_path=prompt, target="codex", config_path=self.config_path)

        self.assertEqual(result.status, "timeout")
        self.assertTrue(Path(result.prompt_file_final).exists())
        self.assertTrue(str(Path(result.prompt_file_final).resolve()).startswith(str(self.failed.resolve())))
        self.assertIsNotNone(result.error_summary)

        log_path = Path(result.log_file)
        line = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(line["status"], "timeout")

    def test_cancel_event_marks_run_cancelled(self) -> None:
        slow_cmd = [sys.executable, "-c", "import time,sys; time.sleep(2); print(sys.stdin.read())"]
        self._write_config(timeout_seconds=5, codex_cmd=slow_cmd)
        prompt = self._write_prompt("codex_cancel_prompt.txt", "cancel me")
        cancel_event = threading.Event()
        cancel_event.set()
        result = run_prompt_file(
            file_path=prompt,
            target="codex",
            config_path=self.config_path,
            cancel_event=cancel_event,
        )
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(Path(result.prompt_file_final).exists())
        self.assertTrue(str(Path(result.prompt_file_final).resolve()).startswith(str(self.failed.resolve())))


if __name__ == "__main__":
    unittest.main()
