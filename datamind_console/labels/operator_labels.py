from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATOR_LABELS_DIR = REPO_ROOT / "labels" / "operator"
SESSION_LABELS_PATH = OPERATOR_LABELS_DIR / "operator_session_labels.jsonl"
RUN_LABELS_PATH = OPERATOR_LABELS_DIR / "operator_run_labels.jsonl"

SESSION_GRADE_TEXT = {"good", "acceptable", "poor", "invalid"}
RUN_GRADE_TEXT = {"good", "acceptable", "poor"}
SESSION_DISPOSITIONS_V1 = {"passed_clean", "passed_with_warnings", "failed", "aborted"}
RUN_DISPOSITIONS_V1 = {"passed_clean", "passed_with_warnings", "failed"}
RUN_DISPOSITIONS_V2 = {
    "passed_clean",
    "passed_after_manual_fix",
    "blocked_unresolved_stops",
    "blocked_sequence_quality",
    "blocked_merge_review",
    "abandoned",
}
SEQUENCE_LABELS_V1 = {"good", "acceptable", "bad"}
SEQUENCE_LABELS_V2 = {"good", "needs_minor_fix", "needs_major_fix", "bad_sequence"}

SIGNAL_YPN = {"yes", "partial", "no"}
SIGNAL_YPN_NA = {"yes", "partial", "no", "not_applicable"}
SIGNAL_YN_NA = {"yes", "no", "not_applicable"}

LABEL_VERSIONS = {"1", "2"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_ts(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            text = str(line or "").strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except Exception:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def _ratio(num: int, den: int) -> float:
    if den <= 0:
        return 0.0
    return float(num) / float(den)


def _as_clean_str(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_grade(value: Any, *, for_session: bool) -> str:
    text = _as_clean_str(value)
    if not text:
        raise ValueError("operator_grade is required")

    numeric: Optional[int]
    try:
        numeric = int(float(text))
    except Exception:
        numeric = None

    if numeric is not None and 1 <= numeric <= 5:
        return str(numeric)

    allowed_text = SESSION_GRADE_TEXT if for_session else RUN_GRADE_TEXT
    if text in allowed_text:
        return text

    if for_session:
        raise ValueError("operator_grade must be 1..5 or one of: acceptable/good/poor/invalid")
    raise ValueError("operator_grade must be 1..5 or one of: acceptable/good/poor")


def _normalize_final_disposition(value: Any, *, for_session: bool) -> str:
    text = _as_clean_str(value)
    if not text:
        raise ValueError("final_run_disposition is required")

    allowed = set(RUN_DISPOSITIONS_V2)
    allowed.update(SESSION_DISPOSITIONS_V1 if for_session else RUN_DISPOSITIONS_V1)
    if text not in allowed:
        raise ValueError(f"final_run_disposition must be one of: {sorted(allowed)}")
    return text


def _normalize_sequence_label(value: Any) -> str:
    text = _as_clean_str(value)
    if not text:
        raise ValueError("operator_sequence_label is required")
    allowed = set(SEQUENCE_LABELS_V2)
    allowed.update(SEQUENCE_LABELS_V1)
    if text not in allowed:
        raise ValueError(f"operator_sequence_label must be one of: {sorted(allowed)}")
    return text


def _normalize_signal(value: Any, *, allowed: set[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"

    text = _as_clean_str(value)
    if not text:
        return None
    if text not in allowed:
        raise ValueError(f"value must be one of: {sorted(allowed)}")
    return text


def _normalize_label_version(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if text not in LABEL_VERSIONS:
        raise ValueError(f"label_version must be one of: {sorted(LABEL_VERSIONS)}")
    return text


def _is_v2_record(record: Dict[str, Any]) -> bool:
    if str(record.get("label_version") or "").strip() == "2":
        return True
    if _as_clean_str(record.get("operator_sequence_label")) in SEQUENCE_LABELS_V2:
        return True
    if _as_clean_str(record.get("final_run_disposition")) in RUN_DISPOSITIONS_V2:
        return True
    if _as_clean_str(record.get("reorder_action_taken")) == "not_applicable":
        return True
    if _as_clean_str(record.get("reorder_helpful")) == "not_applicable":
        return True
    grade = _as_clean_str(record.get("operator_grade"))
    try:
        numeric = int(float(grade))
        if 1 <= numeric <= 5:
            return True
    except Exception:
        pass
    return False


def _signal_score(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    text = _as_clean_str(value)
    if not text:
        return None
    if text in {"yes", "good", "true"}:
        return 1.0
    if text == "partial":
        return 0.5
    if text in {"no", "bad", "false"}:
        return 0.0
    return None


def _grade_score(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = _as_clean_str(value)
    if not text:
        return None
    try:
        n = float(text)
        if 1.0 <= n <= 5.0:
            return n
    except Exception:
        pass

    grade_map = {
        "good": 5.0,
        "acceptable": 3.0,
        "poor": 1.0,
        "invalid": 0.0,
    }
    return grade_map.get(text)


def append_session_label(payload: Dict[str, Any]) -> Dict[str, Any]:
    sequence_warning = _normalize_signal(payload.get("sequence_warning_correct"), allowed=SIGNAL_YPN)
    reorder_taken = _normalize_signal(payload.get("reorder_action_taken"), allowed=SIGNAL_YN_NA)
    reorder_helpful = _normalize_signal(payload.get("reorder_helpful"), allowed=SIGNAL_YPN_NA)
    normalized_grade = _normalize_grade(payload.get("operator_grade"), for_session=True)
    normalized_disposition = _normalize_final_disposition(payload.get("final_run_disposition"), for_session=True)

    inferred_v2 = (
        _is_v2_record(
            {
                "operator_grade": normalized_grade,
                "final_run_disposition": normalized_disposition,
                "sequence_warning_correct": sequence_warning,
                "reorder_action_taken": reorder_taken,
                "reorder_helpful": reorder_helpful,
            }
        )
    )

    row = {
        "label_id": str(payload.get("label_id") or f"slbl_{uuid4().hex[:16]}"),
        "session_id": str(payload.get("session_id") or "").strip(),
        "captured_at": str(payload.get("captured_at") or utc_now_iso()),
        "phase": _as_clean_str(payload.get("phase")) or None,
        "stage": _as_clean_str(payload.get("stage")) or None,
        "run_id": str(payload.get("run_id") or "").strip() or None,
        "route_id": str(payload.get("route_id") or "").strip() or None,
        "operator_sequence_label": (
            _normalize_sequence_label(payload.get("operator_sequence_label"))
            if str(payload.get("operator_sequence_label") or "").strip()
            else None
        ),
        "operator_grade": normalized_grade,
        "sequence_warning_correct": sequence_warning,
        "reorder_action_taken": reorder_taken,
        "reorder_helpful": reorder_helpful,
        "final_run_disposition": normalized_disposition,
        "notes": str(payload.get("notes") or payload.get("operator_notes") or "").strip(),
        "label_version": _normalize_label_version(payload.get("label_version"), fallback=("2" if inferred_v2 else "1")),
    }
    if not row["session_id"]:
        raise ValueError("session_id is required")
    _append_jsonl(SESSION_LABELS_PATH, row)
    return row


def append_run_label(payload: Dict[str, Any]) -> Dict[str, Any]:
    sequence_warning = _normalize_signal(payload.get("sequence_warning_correct"), allowed=SIGNAL_YPN)
    reorder_taken = _normalize_signal(payload.get("reorder_action_taken"), allowed=SIGNAL_YN_NA)
    reorder_helpful = _normalize_signal(payload.get("reorder_helpful"), allowed=SIGNAL_YPN_NA)
    normalized_grade = _normalize_grade(payload.get("operator_grade"), for_session=False)
    normalized_disposition = _normalize_final_disposition(payload.get("final_run_disposition"), for_session=False)
    sequence_label = _normalize_sequence_label(payload.get("operator_sequence_label"))

    inferred_v2 = _is_v2_record(
        {
            "operator_sequence_label": sequence_label,
            "operator_grade": normalized_grade,
            "final_run_disposition": normalized_disposition,
            "sequence_warning_correct": sequence_warning,
            "reorder_action_taken": reorder_taken,
            "reorder_helpful": reorder_helpful,
        }
    )

    row = {
        "label_id": str(payload.get("label_id") or f"rlbl_{uuid4().hex[:16]}"),
        "route_id": str(payload.get("route_id") or "").strip(),
        "run_ref": str(payload.get("run_ref") or payload.get("run_id") or "").strip(),
        "run_id": str(payload.get("run_id") or payload.get("run_ref") or "").strip() or None,
        "phase": _as_clean_str(payload.get("phase")) or None,
        "stage": _as_clean_str(payload.get("stage")) or None,
        "captured_at": str(payload.get("captured_at") or utc_now_iso()),
        "operator_sequence_label": sequence_label,
        "sequence_warning_correct": sequence_warning,
        "reorder_action_taken": reorder_taken,
        "reorder_helpful": reorder_helpful,
        "final_run_disposition": normalized_disposition,
        "operator_grade": normalized_grade,
        "notes": str(payload.get("notes") or payload.get("operator_notes") or "").strip(),
        "label_version": _normalize_label_version(payload.get("label_version"), fallback=("2" if inferred_v2 else "1")),
    }
    if not row["route_id"]:
        raise ValueError("route_id is required")
    if not row["run_ref"]:
        raise ValueError("run_ref is required")
    _append_jsonl(RUN_LABELS_PATH, row)
    return row


def load_session_labels(*, days: int | None = None) -> List[Dict[str, Any]]:
    rows = _read_jsonl(SESSION_LABELS_PATH)
    if days is None:
        return rows
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))
    return [r for r in rows if _parse_iso_ts(r.get("captured_at")) >= cutoff]


def load_run_labels(*, days: int | None = None) -> List[Dict[str, Any]]:
    rows = _read_jsonl(RUN_LABELS_PATH)
    if days is None:
        return rows
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))
    return [r for r in rows if _parse_iso_ts(r.get("captured_at")) >= cutoff]


def compute_label_metrics_30d() -> Dict[str, Any]:
    session_rows = load_session_labels(days=30)
    run_rows = load_run_labels(days=30)
    merged: List[Dict[str, Any]] = [*session_rows, *run_rows]

    total = len(merged)
    good_count = 0
    grade_scores: List[float] = []
    for row in merged:
        g = _grade_score(row.get("operator_grade"))
        if g is None:
            continue
        grade_scores.append(g)
        if g >= 4.0:
            good_count += 1

    seq_values: List[float] = []
    for row in merged:
        score = _signal_score(row.get("sequence_warning_correct"))
        if score is not None:
            seq_values.append(score)

    reorder_values: List[float] = []
    for row in session_rows:
        score = _signal_score(row.get("reorder_helpful"))
        if score is not None:
            reorder_values.append(score)
    for row in run_rows:
        score = _signal_score(row.get("reorder_helpful"))
        if score is not None:
            reorder_values.append(score)

    v2_count = sum(1 for r in merged if _is_v2_record(r))
    v1_count = max(0, total - v2_count)

    return {
        "operator_label_count_30d": int(total),
        "operator_good_rate_30d": round(_ratio(good_count, total), 4),
        "operator_grade_avg_30d": (round(sum(grade_scores) / float(len(grade_scores)), 4) if grade_scores else 0.0),
        "sequence_warning_correct_rate": (round(sum(seq_values) / float(len(seq_values)), 4) if seq_values else 0.0),
        "reorder_helpful_rate": (round(sum(reorder_values) / float(len(reorder_values)), 4) if reorder_values else 0.0),
        "session_labels_30d": len(session_rows),
        "run_labels_30d": len(run_rows),
        "operator_label_v1_count_30d": int(v1_count),
        "operator_label_v2_count_30d": int(v2_count),
    }


def iter_all_operator_labels() -> Iterable[Dict[str, Any]]:
    for row in _read_jsonl(SESSION_LABELS_PATH):
        yield row
    for row in _read_jsonl(RUN_LABELS_PATH):
        yield row
