from __future__ import annotations

import json
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


_SERVICE_PATH = Path(__file__).resolve().parents[1] / "local_runner_service.py"
_SPEC = importlib.util.spec_from_file_location("local_runner_service_test", _SERVICE_PATH)
assert _SPEC and _SPEC.loader
_SERVICE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SERVICE)
dispatch_to_assistant = _SERVICE.dispatch_to_assistant


class LocalRunnerServiceTests(unittest.TestCase):
    def test_dispatch_creates_an_inbox_artifact_and_uses_runner_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = {name: root / name for name in ("inbox", "processing", "done", "failed", "results", "logs")}
            for path in paths.values():
                path.mkdir()
            config_path = root / "runner_config.yaml"
            config_path.write_text(
                json.dumps(
                    {
                        "runner": {"timeout_seconds": 3, "working_directory": str(root)},
                        "paths": {name: str(path) for name, path in paths.items()},
                        "targets": {
                            "codex": {
                                "command": [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
                                "input_mode": "stdin",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            response = dispatch_to_assistant(
                "route/42",
                {"phase": 5},
                target="codex",
                prompt_text="validate this feed",
                runner_config_path=str(config_path),
            )

            self.assertTrue(response["ok"], response)
            result = response["result"]
            self.assertEqual(result["status"], "success")
            self.assertTrue(Path(result["prompt_file_final"]).is_file())
            self.assertIn("route_42", result["prompt_file"])


if __name__ == "__main__":
    unittest.main()
