from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from local_runner.scripts.runner_core import load_runner_config, run_prompt_file
    _HAS_LOCAL_RUNNER = True
except Exception:  # pragma: no cover
    load_runner_config = None  # type: ignore[assignment]
    run_prompt_file = None  # type: ignore[assignment]
    _HAS_LOCAL_RUNNER = False


def dispatch_to_assistant(
    session_id: str,
    step: dict,
    *,
    target: str = "codex",
    prompt_text: Optional[str] = None,
    runner_config_path: Optional[str] = None,
) -> dict:
    if not _HAS_LOCAL_RUNNER:
        return {
            "ok": False,
            "message": f"local_runner is unavailable; dispatch to '{target}' was not attempted.",
        }

    try:
        config_path = Path(runner_config_path).expanduser().resolve() if runner_config_path else _REPO_ROOT / "local_runner/config/runner_config.yaml"
        config = load_runner_config(config_path)
        safe_session = re.sub(r"[^A-Za-z0-9._-]+", "_", str(session_id or "session")).strip("_")[:80] or "session"
        safe_target = re.sub(r"[^A-Za-z0-9._-]+", "_", str(target or "target")).strip("_")[:40] or "target"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        prompt_path = config.paths.inbox / f"{stamp}_{safe_target}_orchestrator_{safe_session}.md"
        if prompt_text is None:
            prompt_text = json.dumps(step or {}, ensure_ascii=False, indent=2)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(str(prompt_text), encoding="utf-8")
        result = run_prompt_file(
            file_path=prompt_path,
            target=target,
            config_path=config_path,
        )
        return {"ok": result.status == "success", "result": result.__dict__}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def make_dispatch_executor(target: str = "codex") -> Callable:
    def _executor(session_id: str, step: dict) -> dict:
        return dispatch_to_assistant(
            session_id, step,
            target=target,
        )
    return _executor
