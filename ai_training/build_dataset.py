from __future__ import annotations

import argparse
import json
from typing import Any, Optional

from ai_training.common import LABEL_MAP_DEFAULT, label_distribution, list_label_join_rows
from datamind_console.db.ai_repo import replace_ai_training_dataset


def _to_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _map_label(human_label: str, *, map_dismiss_to_reject: bool) -> Optional[str]:
    key = str(human_label or "").strip().lower()
    if map_dismiss_to_reject and key == "dismiss":
        return "reject"
    return LABEL_MAP_DEFAULT.get(key)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build ai.ai_training_dataset from suggestions + human label events.")
    ap.add_argument("--phase", default=None, help="Optional phase filter (text).")
    ap.add_argument("--latest-only", action="store_true", help="Use only the latest label event per suggestion.")
    ap.add_argument(
        "--map-dismiss-to-reject",
        action="store_true",
        help="Map dismiss labels into reject class.",
    )
    args = ap.parse_args()

    phase = str(args.phase).strip() if args.phase else None
    raw_rows = list_label_join_rows(phase=phase, latest_only=bool(args.latest_only))
    out_rows: list[tuple] = []
    labels: list[str] = []
    skipped = 0
    for row in raw_rows:
        mapped = _map_label(str(row.get("human_label") or ""), map_dismiss_to_reject=bool(args.map_dismiss_to_reject))
        if not mapped:
            skipped += 1
            continue
        out_rows.append(
            (
                row.get("label_created_at"),
                str(row.get("phase") or ""),
                str(row.get("entity_type") or ""),
                str(row.get("entity_id") or ""),
                _to_json(row.get("features") or {}),
                mapped,
                row.get("suggestion_id"),
            )
        )
        labels.append(mapped)

    inserted = replace_ai_training_dataset(rows=out_rows, phase=phase)
    print(
        {
            "ok": True,
            "phase": phase,
            "source_rows": len(raw_rows),
            "inserted_rows": int(inserted),
            "skipped_rows": int(skipped),
            "label_distribution": label_distribution(labels),
        }
    )


if __name__ == "__main__":
    main()
