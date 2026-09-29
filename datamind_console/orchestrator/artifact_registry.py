from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4


ARTIFACT_TYPES = {
    "snapshot_json",
    "ai_bot_health_report",
    "chatgpt_analysis",
    "chatgpt_ai_bot_review",
    "patch_task_json",
    "patch_prompt_text",
    "runner_stdout",
    "runner_stderr",
    "runner_dispatch_meta",
    "operator_review_notes",
    "operator_label",
    "test_results",
    "labels_jsonl_ref",
    "gtfs_gold_manifest",
    "gtfs_gold_manifest_ref",
    "gtfs_gold_jsonl_ref",
    "combined_labels_metadata",
    "model_improvement_hooks",
    "session_summary",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArtifactRegistry:
    def __init__(self, *, base_dir: Path) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def register(
        self,
        *,
        session_id: str,
        artifact_type: str,
        content: Any | None = None,
        source_path: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        a_type = str(artifact_type or "").strip()
        if a_type not in ARTIFACT_TYPES:
            raise ValueError(f"Unsupported artifact type: {a_type}")

        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required")

        out_dir = self.base_dir / sid
        out_dir.mkdir(parents=True, exist_ok=True)

        artifact_id = str(uuid4())
        payload = {
            "artifact_id": artifact_id,
            "session_id": sid,
            "artifact_type": a_type,
            "created_at": _utc_now_iso(),
            "source_path": (str(source_path).strip() if source_path else None),
            "metadata": dict(metadata or {}),
            "storage_path": None,
        }

        if content is not None:
            if isinstance(content, str):
                ext = "txt"
                body = content
            else:
                ext = "json"
                body = json.dumps(content, ensure_ascii=True, indent=2)
            target = out_dir / f"{artifact_id}.{ext}"
            target.write_text(str(body), encoding="utf-8")
            payload["storage_path"] = str(target)

        index_path = out_dir / "artifacts_index.jsonl"
        with index_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")

        return payload

    def list_artifacts(self, *, session_id: str, limit: int = 500) -> list[dict]:
        sid = str(session_id or "").strip()
        if not sid:
            return []
        index_path = self.base_dir / sid / "artifacts_index.jsonl"
        if not index_path.exists():
            return []
        out: list[dict] = []
        with index_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                txt = str(line or "").strip()
                if not txt:
                    continue
                try:
                    row = json.loads(txt)
                except Exception:
                    continue
                if isinstance(row, dict):
                    out.append(row)
        lim = max(1, int(limit))
        return out[-lim:]
