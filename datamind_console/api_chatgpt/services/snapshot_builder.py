from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from datamind_console.ai_insights.service import AIInsightsService
from datamind_console.ai_insights.scoring import (
    normalize_score_01,
    normalize_score_100,
    pick_quality_score_01,
    pick_sequence_quality_score_100,
    to_bool,
    validate_warnings,
)


TASK_TO_FIXTURE = {
    "analyze_latest_run": "phase3_low_evidence_run_snapshot.json",
    "review_ai_bot_quality": "ai_bot_quality_low_labels_snapshot.json",
    "hades_patch_task_generator": "codex_patch_issue_snapshot.json",
    "hades_retest_comparator": "hades_retest_comparator_snapshot.json",
    "generate_codex_patch_task": "codex_patch_issue_snapshot.json",
    "hades_geography_interpreter": "phase3_low_evidence_run_snapshot.json",
    "hades_evidence_consistency_checker": "phase3_low_evidence_run_snapshot.json",
    "hades_pipeline_interpreter": "phase3_low_evidence_run_snapshot.json",
    "interpret_pipeline_blocker": "phase3_low_evidence_run_snapshot.json",
    "prioritize_pipeline_resolution": "phase3_low_evidence_run_snapshot.json",
    "explain_cleanup_risk": "phase3_low_evidence_run_snapshot.json",
    "interpret_merge_evidence": "phase3_low_evidence_run_snapshot.json",
}

SENSITIVE_KEYWORDS = {
    "api_key",
    "token",
    "password",
    "secret",
    "authorization",
    "access_key",
}


def _sanitize_value(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for key, value in obj.items():
            text = str(key)
            lowered = text.lower()
            if any(token in lowered for token in SENSITIVE_KEYWORDS):
                continue
            out[text] = _sanitize_value(value)
        return out
    if isinstance(obj, list):
        return [_sanitize_value(value) for value in obj]
    if isinstance(obj, str):
        value = obj.strip()
        return value[:8000] if len(value) > 8000 else value
    return obj


def _dedupe_text(values: Any) -> List[str]:
    out: List[str] = []
    for raw in list(values or []):
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def build_ai_bot_snapshot(
    *,
    scores: Dict[str, Any] | None,
    metrics: Dict[str, Any] | None,
    warnings: List[Any] | None,
    anomaly_flags: List[Any] | None,
    proposals: Dict[str, Any] | None,
    extractor_status: Dict[str, Any] | None,
    extractor_help_needed: Dict[str, Any] | None,
) -> Dict[str, Any]:
    extractor_status = dict(extractor_status or {})
    extractor_scores = dict(extractor_status.get("scores") or {})
    metrics = dict(metrics or {})
    scores = dict(scores or {})
    proposals = dict(proposals or {})
    extractor_help_needed = dict(extractor_help_needed or {})

    quality_score = pick_quality_score_01(
        [
            scores.get("quality_score"),
            scores.get("quality"),
            metrics.get("quality_score"),
        ],
        fallback_validator_status=str(metrics.get("validator_status") or ""),
    )
    sequence_quality_score = pick_sequence_quality_score_100(
        [
            scores.get("sequence_quality_score"),
            metrics.get("sequence_quality_score"),
        ]
    )

    evidence = {
        "phase": (
            str(extractor_status.get("phase") or "").strip().lower()
            or str(metrics.get("phase") or "").strip().lower()
        ),
        "step_id": str(extractor_status.get("step_id") or metrics.get("step_id") or "").strip(),
        "attempt_count_total": (
            dict(extractor_status.get("efficiency_metrics") or {}).get("attempt_count_total")
        ),
        "same_config_repeat_count": (
            dict(extractor_status.get("efficiency_metrics") or {}).get("same_config_repeat_count")
        ),
        "non_empty_attempt_count": (
            dict(extractor_status.get("efficiency_metrics") or {}).get("non_empty_attempt_count")
        ),
        "target_option_received": (
            dict(extractor_status.get("spatial_metrics") or {}).get("target_option_received")
        ),
        "failed_stage": (
            dict(extractor_status.get("completion_metrics") or {}).get("failed_stage")
            or metrics.get("failed_stage")
        ),
    }
    merged_warnings = validate_warnings(
        _dedupe_text(list(warnings or []) + list(extractor_status.get("warnings") or [])),
        evidence,
    )

    metrics["quality_score"] = quality_score
    scores["quality_score"] = quality_score
    scores["quality"] = quality_score
    if sequence_quality_score is not None:
        metrics["sequence_quality_score"] = sequence_quality_score
        scores["sequence_quality_score"] = sequence_quality_score
    if extractor_status:
        metrics["extractor_status"] = extractor_status
        metrics["extractor_efficiency_health_score"] = extractor_status.get("extractor_efficiency_health_score")
        metrics["completion_quality_score"] = extractor_scores.get("completion_quality_score")
        metrics["order_completion_quality_score"] = extractor_scores.get("order_completion_quality_score")
    if extractor_help_needed:
        metrics["extractor_help_needed"] = bool(to_bool(extractor_help_needed.get("needed")))
        metrics["extractor_help_severity"] = extractor_help_needed.get("severity")
        metrics["extractor_help_payload"] = extractor_help_needed  # Deprecated alias retained for compatibility.

    metrics["warning_count"] = len(merged_warnings)
    return {
        "scores": _sanitize_value(scores),
        "metrics": _sanitize_value(metrics),
        "warnings": merged_warnings,
        "anomaly_flags": _dedupe_text(anomaly_flags),
        "proposals": _sanitize_value(proposals),
        "extractor_status": _sanitize_value(extractor_status),
        "extractor_help_needed": _sanitize_value(extractor_help_needed),
    }


def _enrich_geographic_context(geo_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure interpreter-usable geography attribution fields are present."""
    if not geo_ctx:
        return geo_ctx
    out = dict(geo_ctx)
    attr = dict(out.get("geography_quality_attribution") or {})
    # Surface key geography failure mode classifications for machine use
    out.setdefault("geography_failure_mode", None)
    likely_cause = str(attr.get("likely_cause") or "").strip()
    if likely_cause in {"geography_interpretation", "geography_degradation", "route_hint_overreach"}:
        out["geography_failure_mode"] = likely_cause
    elif likely_cause == "extractor_logic":
        out["geography_failure_mode"] = "geography_healthy_extractor_weak"
    # Ensure new route-hint attribution fields propagate
    for field in (
        "route_hints_present",
        "route_hints_influenced_bbox",
        "route_hints_used_as_secondary_signal",
        "route_hints_overconstrained_geography",
        "route_hint_effect_reason",
    ):
        if field not in out:
            out[field] = attr.get(field)
    return out


def build_interpreter_snapshot(
    *,
    snapshot_id: str,
    run_id: str,
    trace_id: str,
    phase: str,
    step_id: str,
    trigger: str,
    stage_normalization: Dict[str, Any] | None,
    validator: Dict[str, Any] | None,
    executor_summary: Dict[str, Any] | None,
    spatial_context: Dict[str, Any] | None,
    geographic_context: Dict[str, Any] | None,
    promote_context: Dict[str, Any] | None,
    phase3_route_extractor_packet: Dict[str, Any] | None,
    ai_scores: Dict[str, Any] | None,
    ai_metrics: Dict[str, Any] | None,
    ai_warnings: List[Any] | None,
    ai_anomaly_flags: List[Any] | None,
    regression_flags: List[Any] | None,
    compare: Dict[str, Any] | None,
    reorder: Dict[str, Any] | None,
    extractor_status: Dict[str, Any] | None,
    extractor_help_needed: Dict[str, Any] | None,
    adaptive_retry_plan: Dict[str, Any] | None,
    shadow_learned_retry_plan: Dict[str, Any] | None,
    shadow_retry_plan_comparison: Dict[str, Any] | None,
    patch_history_summary: Dict[str, Any] | None,
    recent_history_summary: List[Dict[str, Any]] | None,
    history: List[Dict[str, Any]] | None,
    policy_profile: Dict[str, Any] | None,
    insufficient_data_flags: List[Any] | None,
    consistency_candidates: List[Dict[str, Any]] | None,
) -> Dict[str, Any]:
    validator = dict(validator or {})
    executor_summary = dict(executor_summary or {})
    compare = dict(compare or {})
    stage_normalization = dict(stage_normalization or {})
    reorder = dict(reorder or {})

    ai_bot = build_ai_bot_snapshot(
        scores=dict(ai_scores or {}),
        metrics=dict(ai_metrics or {}),
        warnings=list(ai_warnings or []),
        anomaly_flags=list(ai_anomaly_flags or []) + list(regression_flags or []),
        proposals={
            "reorder_proposal": bool(reorder.get("recommended")) if reorder.get("recommended") is not None else False,
            "reorder": reorder,
            "comparison": compare,
            "regression_flags": _dedupe_text(regression_flags),
        },
        extractor_status=dict(extractor_status or {}),
        extractor_help_needed=dict(extractor_help_needed or {}),
    )
    ai_scores_norm = dict(ai_bot.get("scores") or {})
    ai_metrics_norm = dict(ai_bot.get("metrics") or {})
    compare_latest = dict(compare.get("latest") or {})
    compare_latest_metrics = dict(compare.get("latest_metrics") or {})
    quality_score = pick_quality_score_01(
        [
            ai_scores_norm.get("quality_score"),
            compare_latest.get("quality_score"),
            compare_latest_metrics.get("quality_score"),
            executor_summary.get("quality_score"),
            dict(validator.get("evidence") or {}).get("quality_score"),
        ],
        fallback_validator_status=str(validator.get("status") or ""),
    )
    sequence_quality_score = pick_sequence_quality_score_100(
        [
            ai_scores_norm.get("sequence_quality_score"),
            ai_metrics_norm.get("sequence_quality_score"),
            compare_latest.get("sequence_quality_score"),
            compare_latest_metrics.get("sequence_quality_score"),
            executor_summary.get("sequence_quality_score"),
            dict(validator.get("evidence") or {}).get("sequence_quality_score"),
        ]
    )

    return _sanitize_value(
        {
            "snapshot_id": snapshot_id,
            "run_id": run_id,
            "trace_id": trace_id,
            "phase": phase,
            "step_id": step_id,
            "trigger": trigger,
            "stage_normalization": stage_normalization,
            "validator": validator,
            "executor_summary": executor_summary,
            "executor_result_summary": executor_summary,  # Deprecated alias retained for compatibility.
            "spatial_context": (dict(spatial_context or {}) or None),
            "geographic_context": _enrich_geographic_context(dict(geographic_context or {})) if geographic_context else None,
            "promote_context": (dict(promote_context or {}) or None),
            "phase3_route_extractor_packet": (dict(phase3_route_extractor_packet or {}) or None),
            "ai_bot": {
                "quality_score": quality_score,
                "warnings": list(ai_bot.get("warnings") or []),
                "anomaly_flags": list(ai_bot.get("anomaly_flags") or []),
                "regression_flags": _dedupe_text(regression_flags),
                "comparison": {
                    "stage_raw": compare.get("stage"),
                    "canonical_step_id": step_id,
                    "baseline": dict(compare.get("baseline") or {}),
                    "latest": compare_latest,
                    "latest_metrics": compare_latest_metrics,
                    "deltas": dict(compare.get("deltas") or {}),
                    "regression_flags": _dedupe_text(compare.get("regression_flags")),
                    "template_delta": dict(compare.get("template_delta") or {}),
                    "param_delta_count": int(compare.get("param_delta_count") or 0),
                    "param_deltas": list(compare.get("param_deltas") or [])[:60],
                    "history_count": int(compare.get("history_count") or 0),
                },
                "proposals": {
                    "reorder": {
                        "recommended": reorder.get("recommended"),
                        "confidence": reorder.get("confidence"),
                    }
                },
                "metrics": {
                    **ai_metrics_norm,
                    "sequence_quality_score": sequence_quality_score,
                    "warning_count": len(list(ai_bot.get("warnings") or [])),
                },
                "scores": ai_scores_norm,
                "extractor_status": dict(ai_bot.get("extractor_status") or {}),
                "extractor_help_needed": dict(ai_bot.get("extractor_help_needed") or {}),
                "adaptive_retry_plan": (dict(adaptive_retry_plan or {}) or None),
                "shadow_learned_retry_plan": (dict(shadow_learned_retry_plan or {}) or None),
                "shadow_retry_plan_comparison": (dict(shadow_retry_plan_comparison or {}) or None),
                "patch_history_summary": (dict(patch_history_summary or {}) or None),
            },
            "ai_bot_snapshot": ai_bot,
            "recent_history_summary": (list(recent_history_summary or []) or None),
            "history": (list(history or []) or None),
            "policy_profile": (dict(policy_profile or {}) or None),
            "insufficient_data_flags": _dedupe_text(insufficient_data_flags),
            "consistency_candidates": list(consistency_candidates or []),
            "patch_history_summary": (dict(patch_history_summary or {}) or None),
            "extractor_help_needed": (dict(ai_bot.get("extractor_help_needed") or {}) or None),
            "extractor_help_reason_class": dict(ai_bot.get("extractor_help_needed") or {}).get("reason_class"),
            "extractor_help_severity": dict(ai_bot.get("extractor_help_needed") or {}).get("severity"),
            "adaptive_retry_plan": (dict(adaptive_retry_plan or {}) or None),
            "adaptive_retry_reordered": (
                to_bool(dict(adaptive_retry_plan or {}).get("reordered"))
                if adaptive_retry_plan
                else None
            ),
            "adaptive_retry_escalation_bias": (
                dict(adaptive_retry_plan or {}).get("escalation_bias")
                if adaptive_retry_plan
                else None
            ),
            "shadow_learned_retry_plan": (dict(shadow_learned_retry_plan or {}) or None),
            "shadow_learned_retry_available": (
                to_bool(dict(shadow_learned_retry_plan or {}).get("available"))
                if shadow_learned_retry_plan
                else None
            ),
            "shadow_retry_plan_comparison": (dict(shadow_retry_plan_comparison or {}) or None),
            "shadow_retry_plan_comparison_status": (
                dict(shadow_retry_plan_comparison or {}).get("comparison_status")
                if shadow_retry_plan_comparison
                else None
            ),
            "shadow_retry_top1_match": (
                to_bool(dict(shadow_retry_plan_comparison or {}).get("top1_match"))
                if shadow_retry_plan_comparison
                else None
            ),
        }
    )


class SnapshotBuilder:
    def __init__(self, base_dir: Path | None = None, snapshot_mode: str | None = None) -> None:
        self.base_dir = base_dir or Path(__file__).resolve().parents[1]
        self.fixtures_dir = self.base_dir / "tests" / "fixtures" / "snapshots"
        env_mode = os.getenv("DATAMIND_CHATGPT_SNAPSHOT_MODE", "fixture")
        self.snapshot_mode = str(snapshot_mode or env_mode or "fixture").strip().lower()
        if self.snapshot_mode not in {"fixture", "live_try", "live_required"}:
            self.snapshot_mode = "fixture"

    def build(
        self,
        *,
        task: str,
        supplied_snapshot: Dict[str, Any] | None,
        operator_context: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        cleaned_supplied = self._sanitize_payload(supplied_snapshot or {})
        if cleaned_supplied:
            snapshot = cleaned_supplied
        elif self.snapshot_mode == "fixture":
            snapshot = self._load_fixture(task)
        elif self.snapshot_mode == "live_required":
            snapshot = self._build_live_snapshot(task=task, operator_context=operator_context)
        else:
            try:
                snapshot = self._build_live_snapshot(task=task, operator_context=operator_context)
            except Exception:
                snapshot = self._load_fixture(task)

        return self._ensure_normalized_snapshot(task=task, snapshot=snapshot)

    def _load_fixture(self, task: str) -> Dict[str, Any]:
        fixture_name = TASK_TO_FIXTURE.get(str(task or "").strip())
        if not fixture_name:
            raise ValueError(f"Unsupported task for snapshot fixture: {task}")
        path = self.fixtures_dir / fixture_name
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            raise ValueError(f"Invalid fixture snapshot at {path}")
        return obj

    def _build_live_snapshot(self, *, task: str, operator_context: Dict[str, Any] | None) -> Dict[str, Any]:
        svc = AIInsightsService()

        task_name = str(task or "").strip()
        if task_name == "analyze_latest_run":
            return self._build_live_analyze_snapshot(svc)
        if task_name == "review_ai_bot_quality":
            return self._build_live_quality_snapshot(svc)
        if task_name in {"generate_codex_patch_task", "hades_patch_task_generator"}:
            return self._build_live_patch_snapshot(
                svc,
                operator_context=operator_context or {},
                task_name=task_name,
            )
        if task_name == "hades_retest_comparator":
            return self._build_live_retest_snapshot(svc, operator_context=operator_context or {})
        if task_name in {
            "hades_evidence_consistency_checker",
            "hades_pipeline_interpreter",
            "interpret_pipeline_blocker",
            "prioritize_pipeline_resolution",
            "interpret_merge_evidence",
        }:
            snap = self._build_live_analyze_snapshot(svc)
            snap["task"] = task_name
            return snap
        if task_name == "explain_cleanup_risk":
            snap = self._build_live_quality_snapshot(svc)
            snap["task"] = task_name
            return snap

        raise ValueError(f"Unsupported task: {task}")

    def _build_live_analyze_snapshot(self, svc: AIInsightsService) -> Dict[str, Any]:
        recent_runs = svc.recent_runs_for_feedback(days=30, limit=250)
        latest = dict(recent_runs[0]) if recent_runs else {}

        phase = str(latest.get("phase") or "phase3")
        stage = str(latest.get("stage") or "") or None
        compare = svc.compare_latest_vs_recent(phase=phase, stage=stage, lookback=20)
        coverage = svc.telemetry_coverage(days=7)
        label_metrics = svc.operator_label_metrics(days=30)

        phase_run_counts = dict(coverage.get("phase_run_counts") or {})
        phase3_runs_7d = int(phase_run_counts.get("phase3") or 0)
        phase1_runs_7d = int(phase_run_counts.get("phase1") or 0)
        label_count_30d = int(label_metrics.get("operator_label_count_30d") or 0)

        insufficient: List[str] = []
        if phase3_runs_7d <= 1:
            insufficient.append("phase3_live_evidence_low")
        if label_count_30d <= 0:
            insufficient.append("operator_labels_low")

        return {
            "snapshot_id": f"snap_run_live_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            "task": "analyze_latest_run",
            "snapshot_source": "live",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "context": {
                "phase": phase,
                "stage": stage,
                "run_context_key": latest.get("run_context_key"),
            },
            "latest_run": {
                "timestamp": latest.get("timestamp"),
                "run_id": latest.get("run_id"),
                "route_id": latest.get("route_id"),
                "quality_score": latest.get("quality_score"),
                "sequence_quality_score": latest.get("sequence_quality_score"),
                "warning_count": latest.get("warning_count"),
                "warnings": list(latest.get("warnings") or []),
                "reorder_recommended": latest.get("reorder_recommended"),
                "reorder_confidence": latest.get("reorder_confidence"),
            },
            "baseline": {
                "history_count": compare.get("history_count"),
                "quality_score_baseline": (compare.get("baseline") or {}).get("quality_score"),
                "warning_count_baseline": (compare.get("baseline") or {}).get("warning_count"),
                "regression_flags": compare.get("regression_flags") or [],
            },
            "coverage": {
                "phase3_run_logs_7d": phase3_runs_7d,
                "phase1_run_logs_7d": phase1_runs_7d,
                "operator_label_count_30d": label_count_30d,
                "operator_good_rate_30d": label_metrics.get("operator_good_rate_30d"),
                "sequence_warning_correct_rate": label_metrics.get("sequence_warning_correct_rate"),
                "reorder_helpful_rate": label_metrics.get("reorder_helpful_rate"),
            },
            "insufficient_data_flags": insufficient,
            "evidence_refs": [
                {
                    "evidence_id": "ev_run_latest",
                    "kind": "run_log",
                    "description": "Latest available run row",
                },
                {
                    "evidence_id": "ev_compare_latest",
                    "kind": "comparison",
                    "description": "Latest vs recent baseline comparison",
                },
                {
                    "evidence_id": "ev_cov_phase3_7d",
                    "kind": "coverage_counter",
                    "description": "Phase3 run logs in 7 day window",
                },
                {
                    "evidence_id": "ev_cov_operator_feedback_30d",
                    "kind": "coverage_counter",
                    "description": "Operator feedback rows in 30 day window",
                },
            ],
        }

    def _build_live_quality_snapshot(self, svc: AIInsightsService) -> Dict[str, Any]:
        self_review = svc.self_review_metrics(days=90)
        coverage = svc.telemetry_coverage(days=7)
        readiness = svc.model_readiness()
        label_metrics = svc.operator_label_metrics(days=30)

        cards = dict(self_review.get("cards") or {})
        phase_run_counts = dict(coverage.get("phase_run_counts") or {})

        phase3_runs_7d = int(phase_run_counts.get("phase3") or 0)
        label_count_30d = int(label_metrics.get("operator_label_count_30d") or 0)

        insufficient: List[str] = []
        if phase3_runs_7d <= 1:
            insufficient.append("phase3_live_evidence_low")
        if label_count_30d <= 0:
            insufficient.append("operator_labels_low")
        if cards.get("recommendation_useful_rate") is None:
            insufficient.append("feedback_signal_missing")

        return {
            "snapshot_id": f"snap_quality_live_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            "task": "review_ai_bot_quality",
            "snapshot_source": "live",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "quality_inputs": {
                "runs_analyzed": cards.get("runs_analyzed"),
                "feedback_rows": cards.get("feedback_rows"),
                "avg_warning_per_run": cards.get("avg_warning_per_run"),
                "missing_field_rate": cards.get("missing_field_rate"),
                "false_warning_proxy_rate": cards.get("false_warning_proxy_rate"),
                "recommendation_useful_rate": cards.get("recommendation_useful_rate"),
                "sequence_warning_correct_rate": cards.get("sequence_warning_correct_rate"),
            },
            "coverage": {
                "phase1_run_logs_7d": int(phase_run_counts.get("phase1") or 0),
                "phase3_run_logs_7d": phase3_runs_7d,
                "operator_label_count_30d": label_count_30d,
                "operator_good_rate_30d": label_metrics.get("operator_good_rate_30d"),
                "sequence_warning_correct_rate": label_metrics.get("sequence_warning_correct_rate"),
                "reorder_helpful_rate": label_metrics.get("reorder_helpful_rate"),
            },
            "readiness": readiness,
            "sanity_summary": {
                "issue_count": ((self_review.get("sanity_summary") or {}).get("issue_count")),
                "severity_counts": ((self_review.get("sanity_summary") or {}).get("severity_counts")),
            },
            "insufficient_data_flags": insufficient,
            "evidence_refs": [
                {
                    "evidence_id": "ev_quality_cards",
                    "kind": "self_review_cards",
                    "description": "Self review summary cards",
                },
                {
                    "evidence_id": "ev_cov_phase3_7d",
                    "kind": "coverage_counter",
                    "description": "Phase3 run logs in 7 day window",
                },
                {
                    "evidence_id": "ev_feedback_rows_30d",
                    "kind": "coverage_counter",
                    "description": "Operator feedback rows in 30 day window",
                },
            ],
        }

    def _build_live_patch_snapshot(
        self,
        svc: AIInsightsService,
        operator_context: Dict[str, Any],
        *,
        task_name: str = "generate_codex_patch_task",
    ) -> Dict[str, Any]:
        run_context_key = str(operator_context.get("run_context_key") or "").strip()
        if not run_context_key:
            recent_runs = svc.recent_runs_for_feedback(days=30, limit=50)
            if recent_runs:
                run_context_key = str(recent_runs[0].get("run_context_key") or "").strip()

        patch_ctx = (
            svc.generate_codex_patch_context(run_context_key=run_context_key)
            if run_context_key
            else {"ok": False, "reason": "run_context_key_missing"}
        )

        package = dict(patch_ctx.get("package") or {})
        insufficient: List[str] = []
        if not package:
            insufficient.append("patch_context_missing")

        return {
            "snapshot_id": f"snap_patch_live_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            "task": str(task_name or "generate_codex_patch_task"),
            "snapshot_source": "live",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "issue_context": {
                "suspected_issue_type": package.get("suspected_issue_type") or "other",
                "phase": package.get("phase"),
                "stage": package.get("stage"),
                "warning_subtypes": package.get("warning_subtypes") or {},
                "warning_count": (package.get("metrics_snapshot") or {}).get("warning_count"),
                "run_context_key": package.get("run_context_key") or run_context_key,
            },
            "recommended_patch_scope": package.get("recommended_patch_scope")
            or {"goal": "small_scoped_reversible_patch", "files": ["datamind_console/ai_insights/service.py"]},
            "safety_reminders": package.get("safety_reminders")
            or [
                "Do not bypass phase gates.",
                "Do not add destructive automation.",
                "Do not auto-commit reorder/merge/cleanup actions.",
                "Preserve recommendation-only behavior.",
            ],
            "insufficient_data_flags": insufficient,
            "evidence_refs": [
                {
                    "evidence_id": "ev_patch_context",
                    "kind": "generated_patch_context",
                    "description": "Patch context generated by AI Insights service",
                },
            ],
        }

    def _build_live_retest_snapshot(
        self,
        svc: AIInsightsService,
        *,
        operator_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        rows = list(svc.recent_runs_for_feedback(days=30, limit=60) or [])
        latest = dict(rows[0]) if rows else {}
        baseline = dict(rows[1]) if len(rows) > 1 else {}

        def _to_float(v: Any) -> float | None:
            try:
                return float(v)
            except Exception:
                return None

        def _to_int(v: Any) -> int | None:
            try:
                return int(v)
            except Exception:
                try:
                    return int(float(v))
                except Exception:
                    return None

        def _run_snapshot(row: Dict[str, Any], *, label: str) -> Dict[str, Any]:
            warning_count = _to_int(row.get("warning_count"))
            warnings = list(row.get("warnings") or [])
            if warning_count is None:
                warning_count = len(warnings)
            out = {
                "label": label,
                "run_id": row.get("run_id"),
                "timestamp": row.get("timestamp"),
                "phase": row.get("phase"),
                "step_id": row.get("stage"),
                "route_id": row.get("route_id"),
                "service_route_id": row.get("service_route_id"),
                "metrics": {
                    "quality_score": _to_float(row.get("quality_score")),
                    "sequence_quality_score": _to_float(row.get("sequence_quality_score")),
                    "warning_count": warning_count,
                    "unmatched_count": _to_int(row.get("unmatched_count")),
                    "ambiguous_count": _to_int(row.get("ambiguous_count")),
                },
                "warnings": warnings,
                "regression_flags": list(row.get("regression_flags") or []),
            }
            return out

        phase = str(latest.get("phase") or baseline.get("phase") or "phase3")
        step_focus = str(latest.get("stage") or baseline.get("stage") or "P3.2_SEQUENCE_STEP20")
        change_type = str(operator_context.get("change_type") or "tuning")

        insufficient: List[str] = []
        if not latest:
            insufficient.append("retest_snapshot_missing")
        if not baseline:
            insufficient.append("baseline_snapshot_missing")

        extra_context = {
            "policy_profile_before": operator_context.get("policy_profile_before"),
            "policy_profile_after": operator_context.get("policy_profile_after"),
            "threshold_config_changed": bool(operator_context.get("threshold_config_changed", False)),
            "manual_changes_applied": bool(operator_context.get("manual_changes_applied", False)),
            "phase2_partial_rerun": bool(operator_context.get("phase2_partial_rerun", False)),
            "notes": operator_context.get("notes"),
        }

        return {
            "snapshot_id": f"snap_retest_live_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            "task": "hades_retest_comparator",
            "snapshot_source": "live",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "change_type": change_type,
            "phase": phase,
            "step_focus": step_focus,
            "baseline_run_snapshot": _run_snapshot(baseline, label="baseline"),
            "retest_run_snapshot": _run_snapshot(latest, label="retest"),
            "extra_context": extra_context,
            "insufficient_data_flags": insufficient,
            "evidence_refs": [
                {
                    "evidence_id": "ev_retest_latest",
                    "kind": "run_log",
                    "description": "Latest run used as retest snapshot",
                },
                {
                    "evidence_id": "ev_retest_baseline",
                    "kind": "run_log",
                    "description": "Previous run used as baseline snapshot",
                },
            ],
        }

    @staticmethod
    def _normalize_retest_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(snapshot or {})
        out["change_type"] = str(out.get("change_type") or "tuning")
        out["phase"] = str(out.get("phase") or "phase3")
        out["step_focus"] = str(out.get("step_focus") or "P3.2_SEQUENCE_STEP20")

        baseline = out.get("baseline_run_snapshot")
        if not isinstance(baseline, dict):
            baseline = dict(out.get("baseline") or {})
        retest = out.get("retest_run_snapshot")
        if not isinstance(retest, dict):
            retest = dict(out.get("retest") or {})

        out["baseline_run_snapshot"] = dict(baseline or {})
        out["retest_run_snapshot"] = dict(retest or {})
        extra = out.get("extra_context")
        out["extra_context"] = dict(extra or {}) if isinstance(extra, dict) else {}
        return out

    def _ensure_normalized_snapshot(self, *, task: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        out = self._sanitize_payload(snapshot)
        task_name = str(task or "").strip()
        if task_name == "hades_retest_comparator":
            out = self._normalize_retest_snapshot(out)

        out.setdefault("snapshot_id", f"snap_{task_name}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
        if str(out.get("task") or "").strip() != task_name:
            out["task"] = task_name
        else:
            out.setdefault("task", task)
        out.setdefault("snapshot_source", "fixture")
        out.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
        out.setdefault("insufficient_data_flags", [])

        refs = out.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            out["evidence_refs"] = [
                {
                    "evidence_id": "ev_default",
                    "kind": "snapshot",
                    "description": "Fallback evidence reference",
                }
            ]

        return out

    def _sanitize_payload(self, obj: Any) -> Any:
        return _sanitize_value(obj)
