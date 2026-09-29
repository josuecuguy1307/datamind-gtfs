from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from datamind_console.ai_agent.feature_utils import flatten_numeric_features
from datamind_console.db.ai_repo import register_ai_model
from datamind_console.db.db import db_conn, fetch_all

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = REPO_ROOT / "models"

LABEL_MAP_DEFAULT = {
    "approve": "approve",
    "reject": "reject",
    "promote": "promote",
    "dismiss": "dismiss",
    "reviewed": "reviewed",
}


def _json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _phase_clause(phase: Optional[str], *, column: str = "phase") -> tuple[str, tuple]:
    if phase is None or str(phase).strip().lower() == "all":
        return "1=1", tuple()
    return f"{column} = %s", (str(phase).strip(),)


def list_label_join_rows(*, phase: Optional[str] = None, latest_only: bool = True) -> list[dict]:
    where, params = _phase_clause(phase, column="s.phase")
    with db_conn(readonly=True) as conn:
        if latest_only:
            return fetch_all(
                conn,
                f"""
                SELECT DISTINCT ON (s.suggestion_id)
                  le.created_at AS label_created_at,
                  s.phase,
                  s.entity_type,
                  s.entity_id,
                  s.features,
                  le.human_label,
                  s.suggestion_id::text AS suggestion_id
                FROM ai.ai_suggestions s
                JOIN ai.ai_label_events le
                  ON le.suggestion_id = s.suggestion_id
                WHERE {where}
                ORDER BY s.suggestion_id, le.created_at DESC
                """,
                params,
            )
        return fetch_all(
            conn,
            f"""
            SELECT
              le.created_at AS label_created_at,
              s.phase,
              s.entity_type,
              s.entity_id,
              s.features,
              le.human_label,
              s.suggestion_id::text AS suggestion_id
            FROM ai.ai_suggestions s
            JOIN ai.ai_label_events le
              ON le.suggestion_id = s.suggestion_id
            WHERE {where}
            ORDER BY le.created_at DESC
            """,
            params,
        )


def load_training_dataset_rows(*, phase: Optional[str] = None) -> list[dict]:
    where, params = _phase_clause(phase)
    with db_conn(readonly=True) as conn:
        return fetch_all(
            conn,
            f"""
            SELECT
              row_id,
              created_at,
              phase,
              entity_type,
              entity_id,
              features,
              label,
              source_suggestion_id::text AS source_suggestion_id
            FROM ai.ai_training_dataset
            WHERE {where}
            ORDER BY created_at ASC, row_id ASC
            """,
            params,
        )


def to_learning_rows(rows: Iterable[dict]) -> tuple[list[dict[str, float]], list[str], list[datetime]]:
    feature_rows: list[dict[str, float]] = []
    labels: list[str] = []
    created_at: list[datetime] = []
    for row in rows:
        feats = flatten_numeric_features(row.get("features") or {})
        if not feats:
            continue
        label = str(row.get("label") or "").strip().lower()
        if not label:
            continue
        feature_rows.append(feats)
        labels.append(label)
        ts = row.get("created_at")
        if isinstance(ts, datetime):
            created_at.append(ts)
        else:
            created_at.append(datetime.utcnow())
    return feature_rows, labels, created_at


def label_distribution(labels: Iterable[str]) -> dict[str, int]:
    return {k: int(v) for k, v in Counter(labels).items()}


def temporal_split_indices(n: int, *, val_ratio: float = 0.2) -> tuple[list[int], list[int]]:
    if n <= 1:
        return list(range(n)), []
    cut = int(round(n * (1.0 - max(0.05, min(0.5, val_ratio)))))
    cut = max(1, min(n - 1, cut))
    return list(range(0, cut)), list(range(cut, n))


def ensure_models_dir() -> Path:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return MODELS_DIR


def now_version(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{ts}"


def register_model(
    *,
    phase: Optional[str],
    model_name: str,
    model_version: str,
    artifact_path: Path,
    metrics: dict[str, Any],
    trained_on: dict[str, Any],
    activate: bool,
) -> dict:
    return register_ai_model(
        phase=phase,
        model_name=model_name,
        model_version=model_version,
        artifact_path=str(artifact_path),
        metrics=metrics,
        trained_on=trained_on,
        activate=activate,
    )
