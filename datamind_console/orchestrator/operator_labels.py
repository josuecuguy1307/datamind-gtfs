from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


REQUIRED_OPERATOR_LABEL_FIELDS = (
    "sequence_quality_label",
    "warning_correctness_label",
    "merge_decision_quality_label",
)


@dataclass(frozen=True)
class OperatorLabelValidation:
    valid: bool
    message: str
    normalized: Dict[str, str]


def normalize_operator_labels(payload: dict) -> dict:
    src = dict(payload or {})
    normalized: dict = {
        "sequence_quality_label": str(src.get("sequence_quality_label") or "").strip().lower(),
        "warning_correctness_label": str(src.get("warning_correctness_label") or "").strip().lower(),
        "merge_decision_quality_label": str(src.get("merge_decision_quality_label") or "").strip().lower(),
        "notes": str(src.get("notes") or "").strip(),
    }
    return normalized


def validate_operator_labels(payload: dict) -> OperatorLabelValidation:
    normalized = normalize_operator_labels(payload)
    missing = [k for k in REQUIRED_OPERATOR_LABEL_FIELDS if not str(normalized.get(k) or "").strip()]
    if missing:
        return OperatorLabelValidation(
            valid=False,
            message=f"Missing required operator label fields: {', '.join(missing)}",
            normalized=normalized,
        )
    return OperatorLabelValidation(
        valid=True,
        message="ok",
        normalized=normalized,
    )
