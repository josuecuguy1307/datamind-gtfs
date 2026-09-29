from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional, Tuple

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None  # type: ignore

from datamind_console.labels import operator_labels

from . import config, ml_adapter, readiness, storage

try:
    from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.extraction_policy import (
        load_sector_recommendations,
        quality_threshold_for_area,
    )
except Exception:  # pragma: no cover
    load_sector_recommendations = None
    quality_threshold_for_area = None


def _parse_ts(v: Any) -> datetime:
    s = str(v or "").strip()
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def _to_df(rows: List[Dict[str, Any]]):
    if pd is None:
        return rows
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def _norm_phase(v: Any) -> str:
    p = str(v or "").strip().lower()
    return p if p in {"phase1", "phase2", "phase3", "phase4"} else ""


def _feedback_value(v: Any) -> Optional[float]:
    s = str(v or "").strip().lower()
    if s == "yes":
        return 1.0
    if s == "partial":
        return 0.5
    if s == "no":
        return 0.0
    return None


def _mean(xs: List[float]) -> Optional[float]:
    vals = [float(x) for x in xs if x is not None]
    return float(mean(vals)) if vals else None


def _safe_div(a: float, b: float) -> float:
    if b <= 0:
        return 0.0
    return float(a) / float(b)


def _warning_count(row: Dict[str, Any]) -> int:
    n = _to_int(row.get("warning_count"))
    if n is not None:
        return max(0, n)
    return len(list(row.get("warnings") or []))


def _quality_breakdown_missing(row: Dict[str, Any]) -> bool:
    if row.get("quality_score") is None:
        return False
    br = row.get("quality_breakdown")
    return not isinstance(br, list) or len(br) == 0


def _row_context_key(row: Dict[str, Any]) -> str:
    parts = [
        str(row.get("timestamp") or ""),
        str(row.get("phase") or ""),
        str(row.get("stage") or ""),
        str(row.get("run_id") or ""),
        str(row.get("node_set_id") or ""),
        str(row.get("route_id") or ""),
        str(row.get("service_route_id") or ""),
        str(row.get("direction_id") if row.get("direction_id") is not None else ""),
    ]
    return "|".join(parts)


def _mode_text(values: List[str]) -> Optional[str]:
    vals = [str(v).strip() for v in values if str(v).strip()]
    if not vals:
        return None
    counter: Counter[str] = Counter(vals)
    return counter.most_common(1)[0][0]


def _template_for_row(row: Dict[str, Any]) -> str:
    for k in ["extraction_action", "template_id", "template", "source_template"]:
        v = str(row.get(k) or "").strip()
        if v:
            return v
    payload = dict(row.get("payload") or {})
    for k in ["extraction_action", "template_id", "template", "source_template"]:
        v = str(payload.get(k) or "").strip()
        if v:
            return v
    return ""


def _flatten_scalar_dict(raw: Any, *, prefix: str = "") -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    stack: List[Tuple[str, Dict[str, Any]]] = [(prefix, raw)]
    while stack:
        parent, obj = stack.pop()
        for k, v in obj.items():
            kk = str(k or "").strip()
            if not kk:
                continue
            key = f"{parent}.{kk}" if parent else kk
            if isinstance(v, dict):
                stack.append((key, v))
                continue
            if isinstance(v, (list, tuple)):
                if len(v) <= 8 and all(not isinstance(x, (dict, list, tuple, set)) for x in v):
                    out[key] = json.dumps(list(v), ensure_ascii=True, sort_keys=True)
                continue
            if isinstance(v, str):
                out[key] = v[:240]
                continue
            if isinstance(v, (int, float, bool)) or v is None:
                out[key] = v
    return out


def _extract_param_snapshot(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    candidate_keys = [
        "normalized_params",
        "parameter_snapshot",
        "params_snapshot",
        "params",
        "extraction_params",
        "source_params",
        "context_params",
    ]
    for k in candidate_keys:
        out.update(_flatten_scalar_dict(row.get(k), prefix=k))

    payload = dict(row.get("payload") or {})
    for k in candidate_keys:
        out.update(_flatten_scalar_dict(payload.get(k), prefix=f"payload.{k}"))

    for k in ["bbox", "area_key", "corridor_key", "corridor_id", "city", "region"]:
        if row.get(k) is not None:
            out[k] = row.get(k)
        elif payload.get(k) is not None:
            out[f"payload.{k}"] = payload.get(k)
    return out


def _warning_subtypes_from_row(row: Dict[str, Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    raw = row.get("warning_subtypes")
    if isinstance(raw, dict):
        for k, v in raw.items():
            kk = str(k or "").strip()
            if not kk:
                continue
            vv = _to_int(v)
            if vv is not None and vv > 0:
                out[kk] = int(vv)
    if out:
        return out

    tags = list(row.get("warning_tags") or [])
    for t in tags:
        tt = str(t or "").strip()
        if tt:
            out[tt] = out.get(tt, 0) + 1
    if out:
        return out

    # Legacy fallback from warning text.
    for w in list(row.get("warnings") or []):
        txt = str(w or "").lower()
        if "repeated matched stop" in txt:
            out["repeated_stops"] = out.get("repeated_stops", 0) + 1
        if "large stop-to-stop jump" in txt:
            out["large_jump"] = out.get("large_jump", 0) + 1
        if "backtracking" in txt:
            out["backtracking"] = out.get("backtracking", 0) + 1
        if "sequence gap" in txt:
            out["gap_anomaly"] = out.get("gap_anomaly", 0) + 1
        if "unmatched" in txt:
            out["unmatched_ratio_high"] = out.get("unmatched_ratio_high", 0) + 1
        if "ambiguous" in txt:
            out["ambiguous_ratio_high"] = out.get("ambiguous_ratio_high", 0) + 1
    return out


def _payload_value(row: Dict[str, Any], key: str) -> Any:
    if key in row:
        return row.get(key)
    payload = dict(row.get("payload") or {})
    return payload.get(key)


class AIInsightsService:
    def __init__(self) -> None:
        storage.ensure_ai_insights_dirs()

    def run_logs(self, *, limit: int = 5000) -> List[Dict[str, Any]]:
        return storage.load_run_logs(limit=limit)

    def model_metrics(self, *, limit: int = 5000) -> List[Dict[str, Any]]:
        return storage.load_model_metrics(limit=limit)

    def train_events(self, *, limit: int = 5000) -> List[Dict[str, Any]]:
        return storage.load_train_events(limit=limit)

    def storage_status(self) -> Dict[str, Any]:
        return storage.get_storage_diagnostics()

    def telemetry_coverage(self, *, days: int = 90) -> Dict[str, Any]:
        rows = self.run_logs(limit=200000)
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        rows = [r for r in rows if _parse_ts(r.get("timestamp")) >= cutoff]

        phase_run_counts: Dict[str, int] = {"phase1": 0, "phase3": 0}
        phase_event_counts: Dict[str, Dict[str, int]] = {"phase1": {}, "phase3": {}}
        stage_counts: Counter[str] = Counter()
        last_timestamp_by_phase: Dict[str, Optional[str]] = {"phase1": None, "phase3": None}
        coverage_flags: List[str] = []

        for r in rows:
            phase = _norm_phase(r.get("phase"))
            if not phase:
                continue
            event_type = str(r.get("event_type") or "").strip().lower() or "unknown"
            stage = str(r.get("stage") or "").strip()
            if event_type == "run":
                phase_run_counts[phase] = int(phase_run_counts.get(phase, 0)) + 1
            per_phase = dict(phase_event_counts.get(phase) or {})
            per_phase[event_type] = int(per_phase.get(event_type, 0)) + 1
            phase_event_counts[phase] = per_phase
            if stage:
                stage_counts[stage] += 1
            last_timestamp_by_phase[phase] = str(r.get("timestamp") or last_timestamp_by_phase.get(phase))

        if int(phase_run_counts.get("phase1") or 0) == 0:
            coverage_flags.append("phase1_run_logs_missing")
        if int(phase_run_counts.get("phase3") or 0) == 0:
            coverage_flags.append("phase3_run_logs_missing")

        return {
            "days": int(days),
            "rows_total": int(len(rows)),
            "phase_run_counts": phase_run_counts,
            "phase_event_counts": phase_event_counts,
            "last_timestamp_by_phase": last_timestamp_by_phase,
            "top_stages": [{"stage": k, "count": int(v)} for k, v in stage_counts.most_common(20)],
            "coverage_flags": coverage_flags,
        }

    def _run_rows(
        self,
        *,
        phase: str,
        days: int = 90,
        event_type: str = "run",
    ) -> List[Dict[str, Any]]:
        rows = self.run_logs(limit=100000)
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        phase_l = str(phase or "").strip().lower()
        out: List[Dict[str, Any]] = []
        for r in rows:
            if str(r.get("phase") or "").strip().lower() != phase_l:
                continue
            if event_type and str(r.get("event_type") or "").strip().lower() != str(event_type).strip().lower():
                continue
            ts = _parse_ts(r.get("timestamp"))
            if ts < cutoff:
                continue
            out.append(dict(r))
        out.sort(key=lambda x: _parse_ts(x.get("timestamp")))
        return out

    def recent_runs_for_feedback(self, *, days: int = 30, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self.run_logs(limit=100000)
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        out: List[Dict[str, Any]] = []
        for r in rows:
            if str(r.get("event_type") or "") != "run":
                continue
            phase = _norm_phase(r.get("phase"))
            if not phase:
                continue
            ts = _parse_ts(r.get("timestamp"))
            if ts < cutoff:
                continue
            rr = dict(r)
            rr["run_context_key"] = _row_context_key(rr)
            rr["warning_count"] = _warning_count(rr)
            rr["label"] = (
                f"{rr.get('timestamp')} | {phase}/{rr.get('stage')} | "
                f"run={rr.get('run_id') or '-'} | node_set={rr.get('node_set_id') or '-'} | "
                f"route={rr.get('route_id') or '-'} | score={rr.get('quality_score')}"
            )
            out.append(rr)
        out.sort(key=lambda x: _parse_ts(x.get("timestamp")), reverse=True)
        return out[: int(max(1, limit))]

    def _feedback_rows_internal(self, *, days: int = 180) -> List[Dict[str, Any]]:
        rows = self.run_logs(limit=100000)
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        out: List[Dict[str, Any]] = []
        for r in rows:
            if str(r.get("event_type") or "").strip().lower() != "system":
                continue
            if str(r.get("stage") or "").strip().lower() != "operator_feedback":
                continue
            ts = _parse_ts(r.get("timestamp"))
            if ts < cutoff:
                continue
            payload = dict(r.get("payload") or {})
            feedback = dict(payload.get("feedback") or {})
            target = dict(payload.get("target_context") or {})
            out.append(
                {
                    "timestamp": r.get("timestamp"),
                    "phase": r.get("phase"),
                    "stage": r.get("stage"),
                    "run_context_key": payload.get("target_context_key"),
                    "operator_grade": feedback.get("operator_grade"),
                    "operator_notes": feedback.get("operator_notes"),
                    "recommendation_useful": feedback.get("recommendation_useful"),
                    "sequence_warning_correct": feedback.get("sequence_warning_correct"),
                    "reorder_action_taken": feedback.get("reorder_action_taken"),
                    "reorder_helpful": feedback.get("reorder_helpful"),
                    "patch_requested": bool(feedback.get("patch_requested")),
                    "patch_area": feedback.get("patch_area"),
                    "target_phase": target.get("phase"),
                    "target_stage": target.get("stage"),
                    "target_timestamp": target.get("timestamp"),
                    "target_run_id": target.get("run_id"),
                    "target_node_set_id": target.get("node_set_id"),
                    "target_route_id": target.get("route_id"),
                }
            )
        out.sort(key=lambda x: _parse_ts(x.get("timestamp")))
        return out

    def operator_feedback(self, *, days: int = 180, limit: int = 2000) -> List[Dict[str, Any]]:
        rows = self._feedback_rows_internal(days=days)
        if int(limit) <= 0:
            return rows
        return rows[-int(limit) :]

    def operator_feedback_summary(self, *, days: int = 180) -> Dict[str, Any]:
        rows = self._feedback_rows_internal(days=days)
        grades = [float(_to_int(r.get("operator_grade"))) for r in rows if _to_int(r.get("operator_grade")) is not None]
        rec_vals = [_feedback_value(r.get("recommendation_useful")) for r in rows]
        rec_vals = [float(x) for x in rec_vals if x is not None]
        seq_vals = [_feedback_value(r.get("sequence_warning_correct")) for r in rows]
        seq_vals = [float(x) for x in seq_vals if x is not None]
        reorder_vals = [_feedback_value(r.get("reorder_helpful")) for r in rows]
        reorder_vals = [float(x) for x in reorder_vals if x is not None]

        patch_counter: Counter[str] = Counter(
            str(r.get("patch_area") or "other").strip() for r in rows if bool(r.get("patch_requested"))
        )
        phase_counter: Counter[str] = Counter(str(r.get("target_phase") or "unknown").strip() for r in rows)

        return {
            "total_feedback_rows": len(rows),
            "avg_operator_grade": (round(_mean(grades), 3) if _mean(grades) is not None else None),
            "recommendation_usefulness_rate": (round(_mean(rec_vals), 4) if _mean(rec_vals) is not None else None),
            "sequence_warning_correct_rate": (round(_mean(seq_vals), 4) if _mean(seq_vals) is not None else None),
            "reorder_helpful_rate": (round(_mean(reorder_vals), 4) if _mean(reorder_vals) is not None else None),
            "patch_requested_count": int(sum(1 for r in rows if bool(r.get("patch_requested")))),
            "patch_area_counts": [{"patch_area": k, "count": int(v)} for k, v in patch_counter.most_common()],
            "feedback_by_phase": [{"phase": k, "count": int(v)} for k, v in phase_counter.items()],
        }

    def operator_label_metrics(self, *, days: int = 30) -> Dict[str, Any]:
        if int(days) != 30:
            session_rows = operator_labels.load_session_labels(days=days)
            run_rows = operator_labels.load_run_labels(days=days)
            merged = [*session_rows, *run_rows]
            total = len(merged)
            good_count = sum(1 for r in merged if str(r.get("operator_grade") or "").strip().lower() == "good")

            seq_vals: List[bool] = []
            for row in merged:
                if row.get("sequence_warning_correct") is None:
                    continue
                seq_vals.append(bool(row.get("sequence_warning_correct")))

            reorder_vals: List[bool] = []
            for row in session_rows:
                if row.get("reorder_helpful") is None:
                    continue
                reorder_vals.append(bool(row.get("reorder_helpful")))

            return {
                "operator_label_count_30d": int(total),
                "operator_good_rate_30d": round(_safe_div(float(good_count), float(max(1, total))), 4),
                "sequence_warning_correct_rate": round(
                    _safe_div(float(sum(1 for x in seq_vals if x)), float(max(1, len(seq_vals)))), 4
                ),
                "reorder_helpful_rate": round(
                    _safe_div(float(sum(1 for x in reorder_vals if x)), float(max(1, len(reorder_vals)))), 4
                ),
                "session_labels_30d": len(session_rows),
                "run_labels_30d": len(run_rows),
            }
        return operator_labels.compute_label_metrics_30d()

    def _find_run_by_context_key(self, run_context_key: str) -> Optional[Dict[str, Any]]:
        key = str(run_context_key or "").strip()
        if not key:
            return None
        rows = self.run_logs(limit=100000)
        for r in reversed(rows):
            if _row_context_key(r) == key and str(r.get("event_type") or "") == "run":
                return dict(r)
        return None

    def submit_operator_feedback(
        self,
        *,
        run_context_key: str,
        operator_grade: Optional[int],
        operator_notes: Optional[str],
        recommendation_useful: Optional[str],
        sequence_warning_correct: Optional[str],
        reorder_action_taken: Optional[str],
        reorder_helpful: Optional[str],
        patch_requested: bool,
        patch_area: Optional[str],
    ) -> Dict[str, Any]:
        target = self._find_run_by_context_key(run_context_key)
        if not target:
            return {"ok": False, "reason": "target_run_not_found", "run_context_key": run_context_key}

        def _norm_enum(v: Any, allowed: List[str], default: str = "") -> str:
            s = str(v or "").strip().lower()
            return s if s in set(allowed) else default

        grade = _to_int(operator_grade)
        if grade is not None:
            grade = max(1, min(5, int(grade)))
        patch_area_norm = _norm_enum(patch_area, config.OPERATOR_PATCH_AREAS, default="other")
        rec_useful_norm = _norm_enum(recommendation_useful, config.OPERATOR_FEEDBACK_USEFULNESS)
        seq_correct_norm = _norm_enum(sequence_warning_correct, config.OPERATOR_FEEDBACK_USEFULNESS)
        reorder_action_norm = _norm_enum(reorder_action_taken, ["yes", "no", "not_applicable"])
        reorder_helpful_norm = _norm_enum(reorder_helpful, config.OPERATOR_FEEDBACK_BOOL_NA)

        target_context = {
            "timestamp": target.get("timestamp"),
            "phase": target.get("phase"),
            "stage": target.get("stage"),
            "event_type": target.get("event_type"),
            "run_id": target.get("run_id"),
            "node_set_id": target.get("node_set_id"),
            "route_id": target.get("route_id"),
            "service_route_id": target.get("service_route_id"),
            "direction_id": target.get("direction_id"),
            "quality_score": target.get("quality_score"),
            "sequence_quality_score": target.get("sequence_quality_score"),
            "score_breakdown": target.get("quality_breakdown"),
            "warning_subtypes": _warning_subtypes_from_row(target),
            "warning_count": _warning_count(target),
            "warnings": list(target.get("warnings") or []),
        }
        feedback_payload = {
            "operator_grade": grade,
            "operator_notes": (str(operator_notes or "").strip() or None),
            "recommendation_useful": (rec_useful_norm or None),
            "sequence_warning_correct": (seq_correct_norm or None),
            "reorder_action_taken": (reorder_action_norm or None),
            "reorder_helpful": (reorder_helpful_norm or None),
            "patch_requested": bool(patch_requested),
            "patch_area": (patch_area_norm if patch_requested else None),
        }
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": _norm_phase(target.get("phase")) or "phase1",
            "event_type": "system",
            "stage": "operator_feedback",
            "status": "success",
            "run_id": target.get("run_id"),
            "node_set_id": target.get("node_set_id"),
            "route_id": target.get("route_id"),
            "service_route_id": target.get("service_route_id"),
            "direction_id": target.get("direction_id"),
            "warnings": [],
            "notes": [str(operator_notes)] if str(operator_notes or "").strip() else [],
            "payload": {
                "feedback_type": "operator_grade",
                "target_context_key": str(run_context_key),
                "target_context": target_context,
                "feedback": feedback_payload,
            },
        }
        storage.append_run_log(record)
        return {
            "ok": True,
            "run_context_key": run_context_key,
            "feedback": feedback_payload,
        }

    def phase1_trends(self, *, days: int = 90) -> Dict[str, Any]:
        rows = self._run_rows(phase="phase1", days=days, event_type="run")
        quality_rows: List[Dict[str, Any]] = []
        counts_rows: List[Dict[str, Any]] = []
        by_template: Dict[str, List[float]] = defaultdict(list)
        for r in rows:
            ts = r.get("timestamp")
            score = r.get("quality_score")
            quality_rows.append(
                {
                    "timestamp": ts,
                    "quality_score": score,
                    "stage": r.get("stage"),
                    "node_set_id": r.get("node_set_id"),
                    "warning_count": _warning_count(r),
                }
            )
            counts_rows.append(
                {
                    "timestamp": ts,
                    "candidate_count": r.get("candidate_count"),
                    "resolved_count": r.get("resolved_count"),
                    "approved_count": r.get("approved_count"),
                }
            )
            tmpl = str(r.get("extraction_action") or r.get("template_id") or "unknown")
            if score is not None:
                try:
                    by_template[tmpl].append(float(score))
                except Exception:
                    pass

        top_templates = []
        for name, vals in by_template.items():
            if not vals:
                continue
            top_templates.append(
                {
                    "template": name,
                    "avg_quality_score": round(sum(vals) / len(vals), 3),
                    "runs": len(vals),
                }
            )
        top_templates.sort(key=lambda x: (x["avg_quality_score"], x["runs"]), reverse=True)
        top_templates = top_templates[:10]
        return {
            "rows_total": len(rows),
            "quality_over_time": _to_df(quality_rows),
            "count_trends": _to_df(counts_rows),
            "top_templates_by_score": _to_df(top_templates),
        }

    def phase1_sector_tuning(self, *, days: int = 90, limit: int = 400) -> Dict[str, Any]:
        rows = self._run_rows(phase="phase1", days=days, event_type="run")
        tuning_rows = [
            r
            for r in rows
            if str(r.get("stage") or "").strip()
            in {"step_build_node_set_tuning_attempt", "step_build_node_set_tuning_recommendation"}
        ]

        attempts: List[Dict[str, Any]] = []
        by_sector: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in tuning_rows:
            stage = str(r.get("stage") or "").strip()
            if stage != "step_build_node_set_tuning_attempt":
                continue
            sector = str(_payload_value(r, "sector") or "").strip() or "unspecified"
            area_group = str(_payload_value(r, "area_group") or "").strip() or "unknown"
            action_id = str(_payload_value(r, "extraction_action") or _template_for_row(r) or "").strip() or "unknown"
            score = _to_float(r.get("quality_score"))
            buffer_pct = _to_float(_payload_value(r, "bbox_buffer_pct"))
            node_set_id = str(r.get("node_set_id") or _payload_value(r, "node_set_id") or "").strip()
            item = {
                "timestamp": r.get("timestamp"),
                "sector": sector,
                "area_group": area_group,
                "action_id": action_id,
                "bbox_buffer_pct": buffer_pct,
                "quality_score": score,
                "node_set_id": node_set_id,
            }
            attempts.append(item)
            by_sector[sector].append(item)

        attempts.sort(key=lambda x: _parse_ts(x.get("timestamp")), reverse=True)
        attempts = attempts[: max(1, int(limit))]

        best_rows: List[Dict[str, Any]] = []
        score_trend_rows: List[Dict[str, Any]] = []
        suggestions_rows: List[Dict[str, Any]] = []

        for sector, items in by_sector.items():
            ordered = sorted(items, key=lambda x: _parse_ts(x.get("timestamp")))
            best = None
            for row in ordered:
                sc = _to_float(row.get("quality_score"))
                if best is None:
                    best = row
                elif sc is not None and (_to_float(best.get("quality_score")) is None or sc > float(best.get("quality_score"))):
                    best = row

            if best is None:
                continue

            recent_scores = [float(x.get("quality_score")) for x in ordered if _to_float(x.get("quality_score")) is not None][-5:]
            trend = "flat"
            if len(recent_scores) >= 2:
                delta = recent_scores[-1] - recent_scores[0]
                if delta > 2.0:
                    trend = "up"
                elif delta < -2.0:
                    trend = "down"

            best_rows.append(
                {
                    "sector": sector,
                    "area_group": best.get("area_group"),
                    "best_action": best.get("action_id"),
                    "best_bbox_buffer_pct": best.get("bbox_buffer_pct"),
                    "best_score": best.get("quality_score"),
                    "last_score": ordered[-1].get("quality_score"),
                    "runs": len(ordered),
                    "score_trend": trend,
                }
            )

            for row in ordered[-10:]:
                score_trend_rows.append(
                    {
                        "timestamp": row.get("timestamp"),
                        "sector": sector,
                        "quality_score": row.get("quality_score"),
                    }
                )

        best_rows.sort(key=lambda x: (_to_float(x.get("best_score")) or -1.0, int(x.get("runs") or 0)), reverse=True)
        for row in best_rows:
            threshold = 58.0
            if callable(quality_threshold_for_area):
                try:
                    threshold = float(quality_threshold_for_area(row.get("area_group")))
                except Exception:
                    threshold = 58.0
            if (_to_float(row.get("last_score")) or 0.0) < threshold:
                suggestions_rows.append(
                    {
                        "sector": row.get("sector"),
                        "suggestion": f"Retry {row.get('best_action')} with +25% or +50% bbox buffer and compare.",
                    }
                )
            else:
                suggestions_rows.append(
                    {
                        "sector": row.get("sector"),
                        "suggestion": f"Use {row.get('best_action')} @ +{row.get('best_bbox_buffer_pct')}% as baseline.",
                    }
                )

        rec_rows: List[Dict[str, Any]] = []
        if callable(load_sector_recommendations):
            try:
                rec_state = load_sector_recommendations()
            except Exception:
                rec_state = {}
            for key, item in dict(rec_state.get("recommendations") or {}).items():
                latest = dict(item.get("latest") or {})
                rec_rows.append(
                    {
                        "sector_key": key,
                        "sector": item.get("sector"),
                        "area_group": item.get("area_group"),
                        "best_action": latest.get("best_action"),
                        "best_bbox_buffer_pct": latest.get("best_bbox_buffer_pct"),
                        "best_score": latest.get("best_score"),
                        "recommended_next_attempt": latest.get("recommended_next_attempt"),
                        "updated_at": item.get("updated_at"),
                    }
                )
            rec_rows.sort(key=lambda x: _parse_ts(x.get("updated_at")), reverse=True)

        return {
            "rows_total": len(tuning_rows),
            "attempts": _to_df(attempts),
            "best_by_sector": _to_df(best_rows),
            "score_trends": _to_df(score_trend_rows),
            "next_attempt_suggestions": _to_df(suggestions_rows),
            "recommended_configs": _to_df(rec_rows),
        }

    def sequence_warning_subtype_frequency(self, *, days: int = 90, stage: Optional[str] = None) -> Any:
        rows = self._run_rows(phase="phase3", days=days, event_type="run")
        if stage:
            rows = [r for r in rows if str(r.get("stage") or "") == str(stage)]
        by_type: Counter[str] = Counter()
        by_type_runs: Counter[str] = Counter()
        n_runs = len(rows)
        for r in rows:
            sub = _warning_subtypes_from_row(r)
            for k, c in sub.items():
                by_type[str(k)] += int(c)
                if int(c) > 0:
                    by_type_runs[str(k)] += 1
        out = []
        for k, c in by_type.most_common():
            out.append(
                {
                    "warning_subtype": k,
                    "count": int(c),
                    "runs_with_subtype": int(by_type_runs.get(k, 0)),
                    "run_share_pct": round(100.0 * _safe_div(float(by_type_runs.get(k, 0)), float(max(1, n_runs))), 2),
                    "avg_count_per_run": round(_safe_div(float(c), float(max(1, n_runs))), 4),
                }
            )
        return _to_df(out)

    def phase3_trends(self, *, days: int = 90) -> Dict[str, Any]:
        rows = self._run_rows(phase="phase3", days=days, event_type="run")
        match_rows: List[Dict[str, Any]] = []
        seq_rows: List[Dict[str, Any]] = []
        gate_rows: List[Dict[str, Any]] = []
        warning_counter: Counter[str] = Counter()
        for r in rows:
            ts = r.get("timestamp")
            match_rows.append(
                {
                    "timestamp": ts,
                    "matched_count": r.get("matched_count"),
                    "unmatched_count": r.get("unmatched_count"),
                    "ambiguous_count": r.get("ambiguous_count"),
                    "route_id": r.get("route_id"),
                    "stage": r.get("stage"),
                }
            )
            seq_rows.append(
                {
                    "timestamp": ts,
                    "sequence_quality_score": r.get("sequence_quality_score"),
                    "quality_score": r.get("quality_score"),
                    "reorder_recommended": r.get("reorder_recommended"),
                    "reorder_confidence": r.get("reorder_confidence"),
                    "route_id": r.get("route_id"),
                    "warning_count": _warning_count(r),
                }
            )
            gate_rows.append(
                {
                    "timestamp": ts,
                    "sequence_gate_pass": bool(r.get("sequence_gate_pass")),
                    "progressed_to_next_step": bool(r.get("progressed_to_next_step")),
                    "stage": r.get("stage"),
                    "route_id": r.get("route_id"),
                }
            )
            for subtype, n in _warning_subtypes_from_row(r).items():
                warning_counter[subtype] += int(n)

        warning_rows = [{"warning_subtype": k, "count": int(v)} for k, v in warning_counter.most_common(25)]
        return {
            "rows_total": len(rows),
            "match_trends": _to_df(match_rows),
            "sequence_quality_over_time": _to_df(seq_rows),
            "gate_trends": _to_df(gate_rows),
            "warning_frequency": _to_df(warning_rows),
            "warning_subtype_frequency": self.sequence_warning_subtype_frequency(days=days),
        }

    def metric_trend(self, *, task: str, days: int = 365) -> Any:
        rows = self.model_metrics(limit=100000)
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        out = []
        for r in rows:
            if str(r.get("task") or "") != str(task):
                continue
            ts = _parse_ts(r.get("timestamp"))
            if ts < cutoff:
                continue
            out.append(
                {
                    "timestamp": r.get("timestamp"),
                    "event_type": r.get("event_type"),
                    "metric_primary_name": r.get("metric_primary_name"),
                    "metric_primary_value": r.get("metric_primary_value"),
                    "metric_higher_better": bool(r.get("metric_higher_better", True)),
                }
            )
        out.sort(key=lambda x: _parse_ts(x.get("timestamp")))
        return _to_df(out)

    def labeled_growth(self, *, task: str, days: int = 365) -> Any:
        if task == "phase1_quality_score":
            rows = self._run_rows(phase="phase1", days=days, event_type="run")
            relevant = [r for r in rows if r.get("quality_score") is not None]
        else:
            rows = self._run_rows(phase="phase3", days=days, event_type="run")
            relevant = [r for r in rows if r.get("reorder_recommended") is not None]

        by_day: Dict[str, int] = Counter()
        for r in relevant:
            ts = str(r.get("timestamp") or "")
            day = ts[:10] if len(ts) >= 10 else ts
            by_day[day] += 1
        running = 0
        out = []
        for day in sorted(by_day.keys()):
            running += int(by_day[day])
            out.append({"day": day, "new_labeled_rows": int(by_day[day]), "cumulative_labeled_rows": running})
        return _to_df(out)

    def model_readiness(self) -> Dict[str, Any]:
        run_rows = self.run_logs(limit=100000)
        metric_rows = self.model_metrics(limit=100000)
        p1_rows = [r for r in run_rows if str(r.get("phase") or "") == "phase1" and str(r.get("event_type") or "") == "run"]
        p3_rows = [r for r in run_rows if str(r.get("phase") or "") == "phase3" and str(r.get("event_type") or "") == "run"]
        r1 = readiness.build_task_readiness(task="phase1_quality_score", run_rows=p1_rows, metric_rows=metric_rows)
        r3 = readiness.build_task_readiness(task="phase3_sequence_risk", run_rows=p3_rows, metric_rows=metric_rows)
        return {
            "phase1_quality_score": r1,
            "phase3_sequence_risk": r3,
        }

    def compare_latest_vs_recent(
        self,
        *,
        phase: str,
        stage: Optional[str] = None,
        lookback: int = 20,
    ) -> Dict[str, Any]:
        phase_l = _norm_phase(phase)
        if not phase_l:
            return {"ok": False, "reason": "invalid_phase"}
        rows = self._run_rows(phase=phase_l, days=365, event_type="run")
        if not rows:
            return {"ok": False, "reason": "no_rows"}
        latest = rows[-1]
        stage_use = str(stage or latest.get("stage") or "").strip()
        peers = [r for r in rows if str(r.get("stage") or "").strip() == stage_use] if stage_use else list(rows)
        if not peers:
            peers = list(rows)
        latest = peers[-1]
        history = peers[:-1][-int(max(3, lookback)) :]
        if not history:
            return {
                "ok": True,
                "phase": phase_l,
                "stage": stage_use,
                "latest": latest,
                "baseline": {},
                "deltas": {},
                "regression_flags": [],
            }

        metrics = [
            "quality_score",
            "warning_count",
        ]
        if phase_l == "phase1":
            metrics.extend(["candidate_count", "resolved_count", "approved_count"])
        else:
            metrics.extend(
                [
                    "matched_count",
                    "unmatched_count",
                    "ambiguous_count",
                    "sequence_quality_score",
                    "rerun_count",
                    "sequence_edit_count",
                ]
            )

        baseline: Dict[str, float] = {}
        for m in metrics:
            vals: List[float] = []
            for r in history:
                if m == "warning_count":
                    vals.append(float(_warning_count(r)))
                else:
                    v = _to_float(r.get(m))
                    if v is not None:
                        vals.append(v)
            if vals:
                baseline[m] = float(sum(vals) / len(vals))

        latest_vals: Dict[str, Optional[float]] = {}
        for m in metrics:
            if m == "warning_count":
                latest_vals[m] = float(_warning_count(latest))
            else:
                latest_vals[m] = _to_float(latest.get(m))

        deltas: Dict[str, Optional[float]] = {}
        for m in metrics:
            lv = latest_vals.get(m)
            bv = baseline.get(m)
            if lv is None or bv is None:
                deltas[m] = None
            else:
                deltas[m] = round(float(lv - bv), 4)

        # Template/parameter deltas to diagnose extraction drift.
        history_templates = [_template_for_row(r) for r in history]
        baseline_template = _mode_text(history_templates)
        latest_template = _template_for_row(latest) or None

        latest_params = _extract_param_snapshot(latest)
        baseline_param_modes: Dict[str, Any] = {}
        history_param_values: Dict[str, Counter[str]] = defaultdict(Counter)
        for r in history:
            for k, v in _extract_param_snapshot(r).items():
                history_param_values[k][json.dumps(v, ensure_ascii=True, sort_keys=True)] += 1
        for k, cnt in history_param_values.items():
            raw = cnt.most_common(1)[0][0]
            try:
                baseline_param_modes[k] = json.loads(raw)
            except Exception:
                baseline_param_modes[k] = raw

        param_deltas: List[Dict[str, Any]] = []
        for k in sorted(set(list(baseline_param_modes.keys()) + list(latest_params.keys()))):
            if k not in latest_params:
                param_deltas.append(
                    {
                        "key": k,
                        "change": "missing_in_latest",
                        "baseline_value": baseline_param_modes.get(k),
                        "latest_value": None,
                    }
                )
                continue
            if k not in baseline_param_modes:
                param_deltas.append(
                    {
                        "key": k,
                        "change": "new_in_latest",
                        "baseline_value": None,
                        "latest_value": latest_params.get(k),
                    }
                )
                continue
            if baseline_param_modes.get(k) != latest_params.get(k):
                param_deltas.append(
                    {
                        "key": k,
                        "change": "value_changed",
                        "baseline_value": baseline_param_modes.get(k),
                        "latest_value": latest_params.get(k),
                    }
                )
        param_deltas = param_deltas[:60]

        th = dict(config.REGRESSION_THRESHOLDS)
        flags: List[Dict[str, Any]] = []
        qd = deltas.get("quality_score")
        if qd is not None and qd < (-1.0 * float(th["quality_drop_points"])):
            flags.append(
                {
                    "code": "quality_drop",
                    "severity": "high",
                    "message": f"Quality score dropped by {abs(float(qd)):.2f} points vs recent baseline.",
                }
            )

        wc_latest = float(latest_vals.get("warning_count") or 0.0)
        wc_base = float(baseline.get("warning_count") or 0.0)
        explosion_ref = max(wc_base * float(th["warning_explosion_factor"]), wc_base + float(th["warning_explosion_abs"]))
        if wc_latest > explosion_ref and wc_latest >= 3:
            flags.append(
                {
                    "code": "warning_explosion",
                    "severity": "medium",
                    "message": "Warning volume is significantly above recent baseline.",
                }
            )

        if phase_l == "phase1":
            for m, code, ratio_th in [
                ("candidate_count", "candidate_drop", float(th["phase1_candidate_drop_ratio"])),
                ("resolved_count", "resolved_drop", float(th["phase1_resolved_drop_ratio"])),
                ("approved_count", "approved_drop", float(th["phase1_approved_drop_ratio"])),
            ]:
                lv = latest_vals.get(m)
                bv = baseline.get(m)
                if lv is None or bv is None or bv <= 0:
                    continue
                ratio_drop = (bv - lv) / bv
                if ratio_drop >= ratio_th:
                    flags.append(
                        {
                            "code": code,
                            "severity": "medium",
                            "message": f"{m} dropped by {ratio_drop*100:.1f}% vs baseline.",
                        }
                    )
        else:
            for m, code, ratio_th in [
                ("unmatched_count", "unmatched_increase", float(th["phase3_unmatched_increase_ratio"])),
                ("ambiguous_count", "ambiguous_increase", float(th["phase3_ambiguous_increase_ratio"])),
            ]:
                lv = latest_vals.get(m)
                bv = baseline.get(m)
                prior = _to_float(latest.get("prior_stop_count")) or 0.0
                if lv is None or bv is None or prior <= 0:
                    continue
                ratio = (lv - bv) / max(1.0, prior)
                if ratio >= ratio_th:
                    flags.append(
                        {
                            "code": code,
                            "severity": "high" if m == "unmatched_count" else "medium",
                            "message": f"{m} increased materially vs baseline ({ratio*100:.1f}% of prior stops).",
                        }
                    )
            sqd = deltas.get("sequence_quality_score")
            if sqd is not None and sqd < (-1.0 * float(th["phase3_sequence_quality_drop_points"])):
                flags.append(
                    {
                        "code": "sequence_quality_drop",
                        "severity": "high",
                        "message": f"Sequence quality dropped by {abs(float(sqd)):.2f} points.",
                    }
                )
            rc = deltas.get("rerun_count")
            if rc is not None and rc >= float(th.get("phase3_rerun_pressure_abs", 2.0)):
                flags.append(
                    {
                        "code": "rerun_pressure_up",
                        "severity": "medium",
                        "message": f"Rerun pressure increased by {float(rc):.2f} vs baseline.",
                    }
                )
            sc = deltas.get("sequence_edit_count")
            if sc is not None and sc >= float(th.get("phase3_sequence_edit_pressure_abs", 3.0)):
                flags.append(
                    {
                        "code": "sequence_edit_pressure_up",
                        "severity": "medium",
                        "message": f"Sequence edit pressure increased by {float(sc):.2f} vs baseline.",
                    }
                )

        # Repeated failure pattern.
        w = int(th["failure_pattern_window"])
        m = int(th["failure_pattern_min"])
        recent = peers[-w:]
        fail_count = 0
        for r in recent:
            status = str(r.get("status") or "success").strip().lower()
            progressed = r.get("progressed_to_next_step")
            if status in {"failed", "partial"}:
                fail_count += 1
            elif progressed is False:
                fail_count += 1
        if fail_count >= m:
            flags.append(
                {
                    "code": "repeated_failure_pattern",
                    "severity": "high",
                    "message": f"{fail_count} problematic runs detected in last {len(recent)} runs.",
                }
            )

        baseline_reorder_rate = None
        if phase_l == "phase3":
            reorder_hist = [
                1.0 if bool(r.get("reorder_recommended")) else 0.0
                for r in history
                if r.get("reorder_recommended") is not None
            ]
            baseline_reorder_rate = _mean(reorder_hist)

        return {
            "ok": True,
            "phase": phase_l,
            "stage": stage_use,
            "latest": latest,
            "baseline": {k: round(v, 4) for k, v in baseline.items()},
            "latest_metrics": {k: latest_vals.get(k) for k in metrics},
            "deltas": deltas,
            "regression_flags": flags,
            "history_count": len(history),
            "template_delta": {
                "baseline_template": baseline_template,
                "latest_template": latest_template,
                "changed": bool(
                    latest_template
                    and baseline_template
                    and str(latest_template).strip() != str(baseline_template).strip()
                ),
            },
            "param_delta_count": len(param_deltas),
            "param_deltas": param_deltas,
            "baseline_reorder_recommend_rate": baseline_reorder_rate,
            "latest_reorder_recommended": latest.get("reorder_recommended"),
        }

    def telemetry_sanity_checks(self, *, days: int = 90) -> Dict[str, Any]:
        rows = self.run_logs(limit=200000)
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        rows = [r for r in rows if _parse_ts(r.get("timestamp")) >= cutoff]
        issues: List[Dict[str, Any]] = []
        max_issue_rows = int(config.SANITY_LIMITS.get("max_issue_rows") or 300)
        ordering_newest_first = True
        ordering_oldest_first = True

        def _issue(code: str, severity: str, message: str, row: Optional[Dict[str, Any]] = None) -> None:
            if len(issues) >= max_issue_rows:
                return
            issues.append(
                {
                    "code": code,
                    "severity": severity,
                    "message": message,
                    "timestamp": (row or {}).get("timestamp"),
                    "phase": (row or {}).get("phase"),
                    "stage": (row or {}).get("stage"),
                    "run_context_key": (_row_context_key(row or {}) if row else None),
                }
            )

        if rows:
            ts_values = [_parse_ts(r.get("timestamp")) for r in rows]
            ordering_newest_first = all(ts_values[i] >= ts_values[i + 1] for i in range(len(ts_values) - 1))
            ordering_oldest_first = all(ts_values[i] <= ts_values[i + 1] for i in range(len(ts_values) - 1))
            if not ordering_newest_first and not ordering_oldest_first:
                _issue("ordering_sanity", "low", "Telemetry rows are not consistently ordered by timestamp.")

        missing_field_rows = 0
        run_key_counter: Counter[str] = Counter()
        run_id_counter: Counter[str] = Counter()
        fp_counter: Counter[str] = Counter()
        warning_unique: set[str] = set()
        warning_entries_total = 0
        warning_duplicate_entries = 0

        for r in rows:
            ev = str(r.get("event_type") or "").strip().lower()
            phase = _norm_phase(r.get("phase"))
            stage = str(r.get("stage") or "").strip()
            ts = str(r.get("timestamp") or "").strip()

            if not ts or not phase or not stage:
                missing_field_rows += 1
                _issue("missing_required_field", "high", "Missing timestamp/phase/stage in telemetry row.", r)

            if ev == "run":
                k = _row_context_key(r)
                run_key_counter[k] += 1
                run_id = str(r.get("run_id") or "").strip()
                if run_id:
                    run_id_counter[f"{phase}|{stage}|{run_id}"] += 1

                # impossible numeric combinations
                for name in [
                    "raw_count",
                    "candidate_count",
                    "resolved_count",
                    "approved_count",
                    "prior_stop_count",
                    "matched_count",
                    "unmatched_count",
                    "ambiguous_count",
                    "rerun_count",
                    "sequence_edit_count",
                ]:
                    iv = _to_int(r.get(name))
                    if iv is not None and iv < 0:
                        _issue("negative_count", "high", f"Negative metric `{name}` detected.", r)

                prior = _to_int(r.get("prior_stop_count"))
                matched = _to_int(r.get("matched_count"))
                unmatched = _to_int(r.get("unmatched_count"))
                ambiguous = _to_int(r.get("ambiguous_count"))
                if prior is not None:
                    if matched is not None and matched > prior:
                        _issue("impossible_metric", "high", "matched_count > prior_stop_count", r)
                    if unmatched is not None and unmatched > prior:
                        _issue("impossible_metric", "high", "unmatched_count > prior_stop_count", r)
                    if ambiguous is not None and ambiguous > prior:
                        _issue("impossible_metric", "high", "ambiguous_count > prior_stop_count", r)

                if _quality_breakdown_missing(r):
                    _issue("score_breakdown_missing", "medium", "quality_score exists but quality_breakdown is empty.", r)

                if r.get("warnings") is not None and not isinstance(r.get("warnings"), list):
                    _issue("malformed_field", "medium", "warnings field is not a list.", r)
                if r.get("notes") is not None and not isinstance(r.get("notes"), list):
                    _issue("malformed_field", "low", "notes field is not a list.", r)
                if r.get("warning_subtypes") is not None and not isinstance(r.get("warning_subtypes"), dict):
                    _issue("malformed_field", "medium", "warning_subtypes is not a dict.", r)
                if r.get("payload") is not None and not isinstance(r.get("payload"), dict):
                    _issue("malformed_field", "medium", "payload is not a dict.", r)
                if isinstance(r.get("warnings"), list):
                    warnings_clean = [str(w).strip() for w in list(r.get("warnings") or []) if str(w).strip()]
                    warning_entries_total += int(len(warnings_clean))
                    warning_unique.update(warnings_clean)
                    dup_n = int(len(warnings_clean) - len(set(warnings_clean)))
                    if dup_n > 0:
                        warning_duplicate_entries += dup_n
                        _issue("duplicate_warnings", "low", f"Found {dup_n} duplicate warning message(s) in run row.", r)

                fingerprint = json.dumps(
                    {
                        "phase": r.get("phase"),
                        "stage": r.get("stage"),
                        "run_id": r.get("run_id"),
                        "node_set_id": r.get("node_set_id"),
                        "route_id": r.get("route_id"),
                        "quality_score": r.get("quality_score"),
                        "sequence_quality_score": r.get("sequence_quality_score"),
                        "warning_count": _warning_count(r),
                    },
                    sort_keys=True,
                    ensure_ascii=True,
                )
                fp_counter[fingerprint] += 1

        for key, n in run_key_counter.items():
            if n > 1 and key.strip("|"):
                _issue("duplicate_run_context", "medium", f"Duplicate run context key detected ({n} rows).")
        for key, n in run_id_counter.items():
            if n > 1 and key.strip("|"):
                _issue("duplicate_run_id", "medium", f"Duplicate run_id detected in same phase/stage ({n} rows).")

        repeat_threshold = int(config.SANITY_LIMITS.get("repeat_fingerprint_threshold") or 4)
        repeated = sum(1 for _, n in fp_counter.items() if n >= repeat_threshold)
        if repeated > 0:
            _issue(
                "repeated_identical_runs",
                "medium",
                f"Detected {repeated} highly repeated run fingerprints (possible duplicate logging).",
            )

        sev_counts: Counter[str] = Counter(str(i.get("severity") or "low") for i in issues)
        code_counts: Counter[str] = Counter(str(i.get("code") or "unknown") for i in issues)
        missing_rate = _safe_div(float(missing_field_rows), float(max(1, len(rows))))
        if bool(ordering_oldest_first) and not bool(ordering_newest_first):
            detected_order = "chronological_oldest_to_newest"
        elif bool(ordering_newest_first) and not bool(ordering_oldest_first):
            detected_order = "reverse_chronological_newest_to_oldest"
        elif bool(ordering_oldest_first) and bool(ordering_newest_first):
            detected_order = "single_or_equal_timestamps"
        else:
            detected_order = "inconsistent_ordering"
        storage_diag = self.storage_status()
        jsonl_stats = dict(storage_diag.get("jsonl_read_stats") or {})
        return {
            "rows_checked": len(rows),
            "issue_count": len(issues),
            "severity_counts": {k: int(v) for k, v in sev_counts.items()},
            "issue_code_counts": {k: int(v) for k, v in code_counts.items()},
            "missing_field_rows": int(missing_field_rows),
            "missing_field_rate": round(missing_rate, 4),
            "ordering": {
                "newest_first": bool(ordering_newest_first),
                "oldest_first": bool(ordering_oldest_first),
                "detected_order": detected_order,
                "note": "AI analytics normalizes by timestamp before latest-vs-baseline comparisons.",
            },
            "warning_diagnostics": {
                "warning_entries_total": int(warning_entries_total),
                "warning_unique_total": int(len(warning_unique)),
                "warning_duplicate_entries": int(warning_duplicate_entries),
            },
            "jsonl_diagnostics": {
                "files": jsonl_stats,
                "invalid_lines_total": int(storage_diag.get("jsonl_invalid_lines_total") or 0),
            },
            "issues": issues,
        }

    def self_review_metrics(self, *, days: int = 90) -> Dict[str, Any]:
        run_rows = self.run_logs(limit=100000)
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        run_rows = [
            r
            for r in run_rows
            if str(r.get("event_type") or "") == "run"
            and _norm_phase(r.get("phase"))
            and _parse_ts(r.get("timestamp")) >= cutoff
        ]
        fb_rows = self._feedback_rows_internal(days=days)
        sanity = self.telemetry_sanity_checks(days=days)

        # Warning volume trend (daily).
        warn_by_day: Dict[str, int] = Counter()
        for r in run_rows:
            day = str(r.get("timestamp") or "")[:10]
            warn_by_day[day] += _warning_count(r)
        warning_volume_trend = [
            {"day": d, "warning_volume": int(warn_by_day[d])}
            for d in sorted(warn_by_day.keys())
        ]

        # Usefulness trends from feedback.
        useful_by_day: Dict[str, List[float]] = defaultdict(list)
        warning_correct_by_day: Dict[str, List[float]] = defaultdict(list)
        rec_use_vals: List[float] = []
        seq_correct_vals: List[float] = []
        false_warning_count = 0
        false_warning_denom = 0
        for f in fb_rows:
            day = str(f.get("timestamp") or "")[:10]
            u = _feedback_value(f.get("recommendation_useful"))
            if u is not None:
                useful_by_day[day].append(u)
                rec_use_vals.append(u)
            sc = _feedback_value(f.get("sequence_warning_correct"))
            if sc is not None:
                warning_correct_by_day[day].append(sc)
                seq_correct_vals.append(sc)
                false_warning_denom += 1
                if float(sc) <= 0.0:
                    false_warning_count += 1

        usefulness_trend: List[Dict[str, Any]] = []
        for d in sorted(set(list(useful_by_day.keys()) + list(warning_correct_by_day.keys()))):
            usefulness_trend.append(
                {
                    "day": d,
                    "recommendation_usefulness": _mean(useful_by_day.get(d, [])),
                    "sequence_warning_correctness": _mean(warning_correct_by_day.get(d, [])),
                }
            )

        # Score stability
        p1_scores = [float(r.get("quality_score")) for r in run_rows if str(r.get("phase")) == "phase1" and _to_float(r.get("quality_score")) is not None]
        p3_scores = [float(r.get("quality_score")) for r in run_rows if str(r.get("phase")) == "phase3" and _to_float(r.get("quality_score")) is not None]
        p1_stability = float(pstdev(p1_scores)) if len(p1_scores) >= 2 else None
        p3_stability = float(pstdev(p3_scores)) if len(p3_scores) >= 2 else None

        # Noisy warning categories
        subtype_counter: Counter[str] = Counter()
        for r in run_rows:
            if str(r.get("phase")) != "phase3":
                continue
            for k, c in _warning_subtypes_from_row(r).items():
                subtype_counter[str(k)] += int(c)
        noisy = [{"warning_subtype": k, "count": int(v)} for k, v in subtype_counter.most_common(20)]

        # Low-value suggestions proxy
        low_reorder = 0
        low_ml = 0
        total_p3 = 0
        for r in run_rows:
            if str(r.get("phase")) != "phase3":
                continue
            total_p3 += 1
            if bool(r.get("reorder_recommended")) and (_to_float(r.get("reorder_confidence")) or 0.0) < float(
                config.LOW_VALUE_THRESHOLDS["reorder_confidence_low"]
            ):
                low_reorder += 1
            ml = dict(r.get("ml_recommendation") or {})
            conf = _to_float(ml.get("confidence"))
            if conf is not None and conf < float(config.LOW_VALUE_THRESHOLDS["ml_confidence_low"]):
                low_ml += 1

        cards = {
            "runs_analyzed": int(len(run_rows)),
            "feedback_rows": int(len(fb_rows)),
            "avg_warning_per_run": round(_safe_div(float(sum(_warning_count(r) for r in run_rows)), float(max(1, len(run_rows)))), 3),
            "recommendation_useful_rate": (
                round(_mean(rec_use_vals), 4) if _mean(rec_use_vals) is not None else None
            ),
            "sequence_warning_correct_rate": (
                round(_mean(seq_correct_vals), 4) if _mean(seq_correct_vals) is not None else None
            ),
            "false_warning_proxy_rate": round(_safe_div(float(false_warning_count), float(max(1, false_warning_denom))), 4),
            "score_stability_phase1_std": (round(p1_stability, 4) if p1_stability is not None else None),
            "score_stability_phase3_std": (round(p3_stability, 4) if p3_stability is not None else None),
            "missing_field_rate": float(sanity.get("missing_field_rate") or 0.0),
            "low_value_reorder_frequency": round(_safe_div(float(low_reorder), float(max(1, total_p3))), 4),
            "low_confidence_ml_frequency": round(_safe_div(float(low_ml), float(max(1, total_p3))), 4),
        }
        return {
            "cards": cards,
            "warning_volume_trend": _to_df(warning_volume_trend),
            "usefulness_trend": _to_df(usefulness_trend),
            "top_noisy_warning_categories": _to_df(noisy),
            "sanity_summary": sanity,
        }

    def operator_checklist_hints(self, *, target_row: Optional[Dict[str, Any]] = None) -> List[str]:
        hints: List[str] = []
        row = dict(target_row or {})
        phase = _norm_phase(row.get("phase"))
        stage = str(row.get("stage") or "").strip().lower()

        if phase == "phase3":
            unmatched = int(_to_int(row.get("unmatched_count")) or 0)
            ambiguous = int(_to_int(row.get("ambiguous_count")) or 0)
            if unmatched > 0 or ambiguous > 0:
                hints.append("Phase 3: do not pass sequence gate until stop matching is complete.")
                hints.append("Phase 3: unresolved/ambiguous stops should be resolved in Phase 1 New Nodes before proceeding.")
            if "step_20" in stage:
                hints.append("Phase 3 Step 20: verify stop prior ordering and match quality before geometry build.")
        if phase == "phase1":
            hints.append("Phase 1: fallback stop naming should be `Parada` when reliable names are absent.")
        hints.append("Phase 2 cleanup caution: destructive cleanup can affect downstream matching/routes.")
        # de-duplicate while preserving order
        dedup: List[str] = []
        seen = set()
        for h in hints:
            if h not in seen:
                dedup.append(h)
                seen.add(h)
        return dedup

    def generate_codex_patch_context(
        self,
        *,
        run_context_key: str,
    ) -> Dict[str, Any]:
        target = self._find_run_by_context_key(run_context_key)
        if not target:
            return {"ok": False, "reason": "target_run_not_found", "run_context_key": run_context_key}
        fb_rows = self._feedback_rows_internal(days=365)
        related_feedback = [f for f in fb_rows if str(f.get("run_context_key") or "") == str(run_context_key)]
        latest_feedback = related_feedback[-1] if related_feedback else {}

        warning_subtypes = _warning_subtypes_from_row(target)
        warning_count = _warning_count(target)

        suspected_issue = "ui_clarity_or_threshold_calibration"
        patch_area = str(latest_feedback.get("patch_area") or "").strip()
        if patch_area:
            suspected_issue = patch_area
        elif warning_count >= 4 and (_feedback_value(latest_feedback.get("recommendation_useful")) or 0.5) < 0.5:
            suspected_issue = "threshold_too_strict_or_warning_noise"
        elif _quality_breakdown_missing(target):
            suspected_issue = "missing_logs_or_score_breakdown"
        elif str(target.get("phase")) == "phase3" and warning_subtypes.get("unmatched_ratio_high", 0) > 0:
            suspected_issue = "sequence_detector_unmatched_penalty_tuning"

        area_to_scope = {
            "scoring": ["datamind_console/ai_insights/scoring.py", "datamind_console/ai_insights/config.py"],
            "thresholds": ["datamind_console/ai_insights/config.py", "datamind_console/ai_insights/sequence_quality.py"],
            "logging": ["datamind_console/ai_insights/telemetry.py", "datamind_console/ai_insights/storage.py"],
            "ui": ["datamind_console/views/insights_view.py"],
            "sequence_detector": ["datamind_console/ai_insights/sequence_quality.py", "datamind_console/ai_insights/telemetry.py"],
            "readiness": ["datamind_console/ai_insights/readiness.py", "datamind_console/ai_insights/service.py"],
            "other": ["datamind_console/ai_insights/service.py"],
        }
        patch_area_for_scope = patch_area if patch_area in area_to_scope else "other"

        package = {
            "phase": target.get("phase"),
            "stage": target.get("stage"),
            "run_context_key": run_context_key,
            "run_ids": [x for x in [target.get("run_id")] if x],
            "node_set_ids": [x for x in [target.get("node_set_id")] if x],
            "route_ids": [x for x in [target.get("route_id")] if x],
            "timestamps": {
                "target_run_timestamp": target.get("timestamp"),
                "feedback_timestamps": [f.get("timestamp") for f in related_feedback],
            },
            "metrics_snapshot": {
                "quality_score": target.get("quality_score"),
                "sequence_quality_score": target.get("sequence_quality_score"),
                "candidate_count": target.get("candidate_count"),
                "resolved_count": target.get("resolved_count"),
                "approved_count": target.get("approved_count"),
                "prior_stop_count": target.get("prior_stop_count"),
                "matched_count": target.get("matched_count"),
                "unmatched_count": target.get("unmatched_count"),
                "ambiguous_count": target.get("ambiguous_count"),
                "warning_count": warning_count,
                "reorder_recommended": target.get("reorder_recommended"),
                "reorder_confidence": target.get("reorder_confidence"),
            },
            "score_breakdown": target.get("quality_breakdown"),
            "warning_subtypes": warning_subtypes,
            "operator_feedback": related_feedback,
            "suspected_issue_type": suspected_issue,
            "recommended_patch_scope": {
                "goal": "small_scoped_reversible_patch",
                "files": area_to_scope.get(patch_area_for_scope, area_to_scope["other"]),
            },
            "safety_reminders": [
                "Do not bypass phase gates.",
                "Do not add destructive automation.",
                "Do not auto-commit reorder/merge/cleanup actions.",
                "Preserve recommendation-only behavior.",
            ],
        }
        text = json.dumps(package, indent=2, ensure_ascii=True)
        return {"ok": True, "package": package, "text": text}

    def _dataset_for_task(self, task: str) -> Dict[str, Any]:
        if task == "phase1_quality_score":
            rows = self._run_rows(phase="phase1", days=3650, event_type="run")
            feature_names = list(config.PHASE1_REQUIRED_FEATURES) + [
                "cluster_count",
                "singleton_count",
                "ambiguity_proxy_count",
            ]
            label_key = "quality_score"
            data = [r for r in rows if r.get(label_key) is not None]
            return {"rows": data, "feature_names": feature_names, "label_key": label_key}

        rows = self._run_rows(phase="phase3", days=3650, event_type="run")
        feature_names = list(config.PHASE3_REQUIRED_FEATURES) + [
            "rerun_count",
            "sequence_edit_count",
        ]
        # Classification target from heuristic reorder recommendation.
        data = []
        for r in rows:
            rr = dict(r)
            if rr.get("reorder_recommended") is None:
                continue
            rr["risk_label"] = 1 if bool(rr.get("reorder_recommended")) else 0
            data.append(rr)
        return {"rows": data, "feature_names": feature_names, "label_key": "risk_label"}

    def manual_train(self, *, task: str) -> Dict[str, Any]:
        ds = self._dataset_for_task(task)
        out = ml_adapter.train_lightgbm(
            task=task,
            rows=list(ds.get("rows") or []),
            feature_names=list(ds.get("feature_names") or []),
            label_key=str(ds.get("label_key") or ""),
        )
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task": task,
            "event_type": "train",
            "ok": bool(out.get("ok")),
            "status": ("success" if bool(out.get("ok")) else "failed"),
            "summary": out,
            "metric_primary_name": out.get("metric_primary_name"),
            "metric_primary_value": out.get("metric_primary_value"),
            "metric_higher_better": out.get("metric_higher_better"),
        }
        storage.append_train_event(row)
        storage.append_model_metrics(row)
        return out

    def manual_evaluate(self, *, task: str) -> Dict[str, Any]:
        ds = self._dataset_for_task(task)
        out = ml_adapter.evaluate_lightgbm(
            task=task,
            rows=list(ds.get("rows") or []),
            feature_names=list(ds.get("feature_names") or []),
            label_key=str(ds.get("label_key") or ""),
        )
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task": task,
            "event_type": "eval",
            "ok": bool(out.get("ok")),
            "status": ("success" if bool(out.get("ok")) else "failed"),
            "summary": out,
            "metric_primary_name": out.get("metric_primary_name"),
            "metric_primary_value": out.get("metric_primary_value"),
            "metric_higher_better": out.get("metric_higher_better"),
        }
        storage.append_train_event(row)
        storage.append_model_metrics(row)
        return out
