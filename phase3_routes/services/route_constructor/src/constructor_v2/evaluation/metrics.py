from __future__ import annotations

from typing import Any, Sequence

from src.constructor_v2.common import order_agreement_ratio


def dedupe_preserve_order(stop_ids: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for stop_id in stop_ids:
        if stop_id in seen:
            continue
        seen.add(stop_id)
        out.append(stop_id)
    return out


def compare_stop_orders(reference_ids: Sequence[str], candidate_ids: Sequence[str]) -> dict[str, Any]:
    reference = dedupe_preserve_order(reference_ids)
    candidate = dedupe_preserve_order(candidate_ids)
    reference_set = set(reference)
    candidate_set = set(candidate)
    common = [stop_id for stop_id in candidate if stop_id in reference_set]
    return {
        "reference_count": len(reference),
        "candidate_count": len(candidate),
        "common_count": len(common),
        "order_agreement": order_agreement_ratio(reference, candidate),
        "coverage_recall": len(common) / len(reference) if reference else 1.0,
        "coverage_precision": len(common) / len(candidate) if candidate else 1.0,
        "missing_reference_count": len(reference_set - candidate_set),
        "extra_candidate_count": len(candidate_set - reference_set),
    }


def confidence_summary(output: Any) -> dict[str, Any]:
    return {
        "label": output.confidence.label,
        "score": output.confidence.score,
        "auto_accept": output.confidence.auto_accept,
    }
