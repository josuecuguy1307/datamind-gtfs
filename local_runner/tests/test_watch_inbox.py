from __future__ import annotations

import json
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from local_runner.scripts.watch_inbox import main as watcher_main


class WatchInboxTests(unittest.TestCase):
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
        self.config_path = self.root / "runner_config.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_config(self, *, codex_cmd: list[str], default_target: str | None = "codex") -> None:
        cfg = {
            "runner": {
                "timeout_seconds": 5,
                "capture_stderr_separately": True,
                "infer_target_from_filename": True,
                "default_target": default_target,
                "log_filename": "runs.jsonl",
            },
            "watcher": {
                "enabled": True,
                "poll_interval_seconds": 1,
                "max_concurrent_per_target": 1,
                "watch_dir": str(self.inbox),
                "auto_infer_target": True,
                "default_target": default_target,
                "ignored_prefixes": ["_", "."],
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
                "codex": {"command": codex_cmd, "input_mode": "stdin"},
                "claude": {"command": [sys.executable, "-c", "import sys; print(sys.stdin.read())"], "input_mode": "stdin"},
            },
        }
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")

    def test_watcher_picks_file_and_dispatches(self) -> None:
        self._write_config(codex_cmd=[sys.executable, "-c", "import sys; print('WATCH:' + sys.stdin.read().strip())"])
        prompt = self.inbox / "codex_job.md"
        prompt.write_text("hello watcher", encoding="utf-8")

        rc = watcher_main(["--config", str(self.config_path), "--max-cycles", "1"])
        self.assertEqual(rc, 0)
        done_files = list(self.done.glob("*.md"))
        self.assertEqual(len(done_files), 1)
        out_files = list(self.results.glob("*.out.txt"))
        self.assertTrue(out_files)
        self.assertIn("WATCH:hello watcher", out_files[0].read_text(encoding="utf-8"))

    def test_watcher_skips_hidden_ignored_and_lock_files(self) -> None:
        self._write_config(codex_cmd=[sys.executable, "-c", "import sys; print(sys.stdin.read())"])
        (self.inbox / ".hidden.md").write_text("x", encoding="utf-8")
        (self.inbox / "_ignored.md").write_text("x", encoding="utf-8")
        (self.inbox / "busy.lock").write_text("x", encoding="utf-8")

        rc = watcher_main(["--config", str(self.config_path), "--max-cycles", "1"])
        self.assertEqual(rc, 0)
        self.assertFalse(list(self.done.iterdir()))
        self.assertFalse(list(self.failed.iterdir()))
        self.assertTrue((self.inbox / ".hidden.md").exists())
        self.assertTrue((self.inbox / "_ignored.md").exists())
        self.assertTrue((self.inbox / "busy.lock").exists())

    def test_unknown_target_moves_to_failed(self) -> None:
        self._write_config(
            codex_cmd=[sys.executable, "-c", "import sys; print(sys.stdin.read())"],
            default_target=None,
        )
        prompt = self.inbox / "no_target.md"
        prompt.write_text("hello", encoding="utf-8")

        rc = watcher_main(["--config", str(self.config_path), "--max-cycles", "1"])
        self.assertEqual(rc, 0)
        failed_files = list(self.failed.glob("*.md"))
        self.assertEqual(len(failed_files), 1)
        self.assertFalse(prompt.exists())

    def test_sigint_waits_for_current_dispatch_then_exits(self) -> None:
        self._write_config(codex_cmd=[sys.executable, "-c", "import time,sys; data=sys.stdin.read(); time.sleep(0.6); print('DONE:'+data.strip())"])
        prompt = self.inbox / "codex_slow.md"
        prompt.write_text("slow", encoding="utf-8")

        script_path = Path(__file__).resolve().parents[1] / "scripts" / "watch_inbox.py"
        proc = subprocess.Popen(
            [sys.executable, str(script_path), "--config", str(self.config_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(0.15)
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
        proc.communicate(timeout=2)

        self.assertEqual(proc.returncode, 0)
        done_files = list(self.done.glob("*.md"))
        self.assertEqual(len(done_files), 1)
        out_files = list(self.results.glob("*.out.txt"))
        self.assertTrue(out_files)
        self.assertIn("DONE:slow", out_files[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
