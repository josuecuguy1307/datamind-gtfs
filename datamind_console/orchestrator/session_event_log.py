from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionEventLogger:
    def __init__(self, *, base_dir: Path) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.base_dir / "events_v2.jsonl"

    def log_event(
        self,
        *,
        session_id: str,
        step_key: str,
        event_type: str,
        actor_role: str,
        action: str,
        status: str,
        actor_id: Optional[str] = None,
        payload_summary: Optional[dict] = None,
        artifact_refs: Optional[list[str]] = None,
        policy_check_result: Optional[dict] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> dict:
        row = {
            "event_id": str(uuid4()),
            "session_id": str(session_id or "").strip(),
            "timestamp": _utc_now_iso(),
            "step_key": str(step_key or "").strip(),
            "event_type": str(event_type or "").strip(),
            "actor_role": str(actor_role or "").strip(),
            "actor_id": (str(actor_id).strip() if actor_id else None),
            "action": str(action or "").strip(),
            "status": str(status or "").strip(),
            "payload_summary": dict(payload_summary or {}),
            "artifact_refs": list(artifact_refs or []),
            "policy_check_result": dict(policy_check_result or {}),
            "error_code": (str(error_code).strip() if error_code else None),
            "error_message": (str(error_message).strip() if error_message else None),
        }
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n")
        return row

    def read_tail(self, *, limit: int = 100) -> list[dict]:
        lim = max(1, int(limit))
        if not self.events_path.exists():
            return []
        rows: list[dict] = []
        with self.events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                txt = str(line or "").strip()
                if not txt:
                    continue
                try:
                    item = json.loads(txt)
                except Exception:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
        return rows[-lim:]
