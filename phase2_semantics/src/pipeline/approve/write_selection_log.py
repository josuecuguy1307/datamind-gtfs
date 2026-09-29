from __future__ import annotations

import json
from datetime import datetime
from typing import List, Dict, Any
from pathlib import Path


LOG_DIR = Path("logs/approvals")
LOG_DIR.mkdir(parents=True, exist_ok=True)


def write_selection_log(
    *,
    query: str,
    chosen_place_id: str,
    candidates: List[Dict[str, Any]],
    pipeline_version: str,
) -> None:
    """
    Persist a human-approved place selection.
    """

    record = {
        "query": query,
        "chosen_place_id": chosen_place_id,
        "candidates": candidates,
        "pipeline_version": pipeline_version,
        "timestamp": datetime.utcnow().isoformat(),
    }

    fname = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{chosen_place_id}.json"
    path = LOG_DIR / fname

    with path.open("w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
