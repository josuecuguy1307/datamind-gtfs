from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


class AuditLogger:
    _lock = threading.Lock()

    def __init__(self, file_path: str | None = None) -> None:
        default_path = (
            Path(__file__).resolve().parents[3]
            / "data"
            / "ai_insights"
            / "chatgpt_advisory_audit.jsonl"
        )
        env_path = os.getenv("DATAMIND_CHATGPT_AUDIT_FILE")
        self.file_path = Path(file_path or env_path or default_path).expanduser().resolve()
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def log_call(self, record: Dict[str, Any]) -> None:
        payload = dict(record)
        payload.setdefault("logged_at", datetime.now(timezone.utc).isoformat())
        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        with self._lock:
            with self.file_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
