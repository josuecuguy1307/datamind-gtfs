from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import config, storage
from .ml_adapter import predict
from .scoring import score_phase1_quality, score_phase3_quality
from .sequence_quality import evaluate_sequence_quality


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v or "").strip()]
    s = str(value).strip()
    return [s] if s else []


def _dedup_text_list(values: Any) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for item in _norm_list(values):
        key = str(item).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _parse_ts(s: Any) -> datetime:
    raw = str(s or "").strip()
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def _count_reruns(*, phase: str, stage: str, key_name: str, key_value: str) -> int:
    rows = storage.load_run_logs()
    return storage.count_runs_for_key(
        rows,
        phase=phase,
        stage=stage,
        key_name=key_name,
        key_value=key_value,
    )


def _count_recent_sequence_edits(route_id: str, *, hours: int = 48) -> int:
    rows = storage.load_run_logs()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(hours))
    total = 0
    for r in rows:
        if str(r.get("phase") or "") != "phase3":
            continue
        if str(r.get("event_type") or "") != "sequence_edit":
            continue
        if str(r.get("route_id") or "") != str(route_id):
            continue
        ts = _parse_ts(r.get("timestamp"))
        if ts >= cutoff:
            total += 1
    return total


def _phase1_feature_slice(row: Dict[str, Any]) -> Dict[str, Any]:
    keys = {
        "raw_count",
        "candidate_count",
        "stop_count",
        "poi_count",
        "stop_signal_count",
        "poi_signal_count",
        "stop_ratio",
        "poi_ratio",
        "name_coverage",
        "tag_coverage_public_transport",
        "tag_coverage_highway_bus_stop",
        "tag_coverage_amenity_bus_station",
        "tag_coverage_platform",
        "tag_coverage",
        "cluster_count",
        "n_clusters",
        "singleton_count",
        "singletons",
        "noise_count",
        "resolved_count",
        "approved_count",
        "ambiguity_proxy_count",
        "spatial_spread_indicator",
        "area_group",
        "sector",
        "extraction_action",
    }
    return {k: row.get(k) for k in keys}


def _phase3_feature_slice(row: Dict[str, Any]) -> Dict[str, Any]:
    keys = {
        "prior_stop_count",
        "matched_count",
        "unmatched_count",
        "ambiguous_count",
        "sequence_quality_score",
        "rerun_count",
        "sequence_edit_count",
    }
    return {k: row.get(k) for k in keys}


def log_phase1_run(event: Dict[str, Any]) -> Dict[str, Any]:
    if not config.ENABLE_AI_INSIGHTS:
        return dict(event)

    row = dict(event or {})
    row["phase"] = "phase1"
    row.setdefault("event_type", "run")
    row.setdefault("status", "success")
    row.setdefault("timestamp", _now_iso())
    row["warnings"] = _dedup_text_list(row.get("warnings"))
    row["notes"] = _dedup_text_list(row.get("notes"))
    row["stage"] = str(row.get("stage") or "phase1_step").strip()
    if row.get("node_set_id") is not None:
        row["node_set_id"] = str(row.get("node_set_id"))
    if row.get("run_id") is not None:
        row["run_id"] = str(row.get("run_id"))

    score_out = score_phase1_quality(row)
    row["quality_score"] = score_out.get("score")
    if score_out.get("score_ready") is not None:
        row["quality_score_ready"] = bool(score_out.get("score_ready"))
    if score_out.get("score_provisional") is not None:
        row["quality_score_provisional"] = score_out.get("score_provisional")
    diagnostics = dict(score_out.get("diagnostics") or {})
    if diagnostics:
        row["quality_diagnostics"] = diagnostics
    row["quality_breakdown"] = score_out.get("breakdown") or []
    row["quality_components"] = score_out.get("components_raw") or {}
    row["warnings"] = _dedup_text_list(list(row["warnings"]) + list(score_out.get("warnings") or []))
    row["warning_count"] = int(len(row.get("warnings") or []))

    ml_out = predict("phase1_quality_score", features=_phase1_feature_slice(row))
    row["ml_recommendation"] = ml_out

    storage.append_run_log(row)
    return row


def log_phase3_run(event: Dict[str, Any]) -> Dict[str, Any]:
    if not config.ENABLE_AI_INSIGHTS:
        return dict(event)

    row = dict(event or {})
    row["phase"] = "phase3"
    row.setdefault("event_type", "run")
    row.setdefault("status", "success")
    row.setdefault("timestamp", _now_iso())
    row["warnings"] = _dedup_text_list(row.get("warnings"))
    row["notes"] = _dedup_text_list(row.get("notes"))
    row["stage"] = str(row.get("stage") or "phase3_step").strip()
    route_id = str(row.get("route_id") or "").strip()
    if route_id:
        row["route_id"] = route_id

    if route_id and row.get("rerun_count") is None:
        row["rerun_count"] = _count_reruns(
            phase="phase3",
            stage=row["stage"],
            key_name="route_id",
            key_value=route_id,
        )
    if route_id and row.get("sequence_edit_count") is None:
        row["sequence_edit_count"] = _count_recent_sequence_edits(route_id)

    prior_rows = row.get("prior_rows")
    if isinstance(prior_rows, list) and prior_rows:
        seq_out = evaluate_sequence_quality(
            prior_rows=prior_rows,
            matched_count=(int(row["matched_count"]) if row.get("matched_count") is not None else None),
            unmatched_count=(int(row["unmatched_count"]) if row.get("unmatched_count") is not None else None),
            ambiguous_count=(int(row["ambiguous_count"]) if row.get("ambiguous_count") is not None else None),
            sequence_edit_count=(int(row["sequence_edit_count"]) if row.get("sequence_edit_count") is not None else None),
        )
        row["sequence_quality_score"] = seq_out.get("sequence_quality_score")
        row["sequence_warnings"] = seq_out.get("warnings") or []
        row["warning_tags"] = list(seq_out.get("warning_tags") or [])
        row["warning_subtypes"] = dict(seq_out.get("warning_subtypes") or {})
        row["reorder_recommended"] = bool(seq_out.get("reorder_recommended"))
        row["reorder_confidence"] = _to_float(seq_out.get("reorder_confidence"))
        row["sequence_rationale"] = seq_out.get("rationale")
        row["warnings"] = _dedup_text_list(list(row["warnings"]) + list(seq_out.get("warnings") or []))
        row["prior_rows_count"] = len(prior_rows)
        row["prior_rows_sample"] = prior_rows[:10]
        row.pop("prior_rows", None)
    else:
        row.setdefault("sequence_quality_score", None)
        row.setdefault("warning_tags", [])
        row.setdefault("warning_subtypes", {})
        row.setdefault("reorder_recommended", None)
        row.setdefault("reorder_confidence", None)
        row.setdefault("sequence_rationale", None)

    score_out = score_phase3_quality(row)
    row["quality_score"] = score_out.get("score")
    row["quality_breakdown"] = score_out.get("breakdown") or []
    row["quality_components"] = score_out.get("components_raw") or {}
    row["warnings"] = _dedup_text_list(list(row["warnings"]) + list(score_out.get("warnings") or []))
    row["warning_count"] = int(len(row.get("warnings") or []))

    ml_out = predict("phase3_sequence_risk", features=_phase3_feature_slice(row))
    row["ml_recommendation"] = ml_out

    storage.append_run_log(row)
    return row


def log_phase3_sequence_edit(event: Dict[str, Any]) -> Dict[str, Any]:
    if not config.ENABLE_AI_INSIGHTS:
        return dict(event)
    row = dict(event or {})
    row["phase"] = "phase3"
    row["event_type"] = "sequence_edit"
    row.setdefault("status", "success")
    row.setdefault("timestamp", _now_iso())
    row["route_id"] = str(row.get("route_id") or "")
    row["edit_type"] = str(row.get("edit_type") or "replace").strip().lower()
    row["warnings"] = _dedup_text_list(row.get("warnings"))
    row["notes"] = _dedup_text_list(row.get("notes"))
    storage.append_run_log(row)
    return row
