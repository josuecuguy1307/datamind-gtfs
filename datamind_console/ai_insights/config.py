from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from hades.geometry.canonical import PLAUSIBLE_PAIR_SCORE


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "y", "on"}


REPO_ROOT = Path(__file__).resolve().parents[2]
AI_INSIGHTS_DIR = Path(
    os.getenv("DATAMIND_AI_INSIGHTS_DIR", str(REPO_ROOT / "data" / "ai_insights"))
).expanduser()

RUN_LOG_FILE = AI_INSIGHTS_DIR / "run_logs.jsonl"
MODEL_METRICS_FILE = AI_INSIGHTS_DIR / "model_metrics.jsonl"
TRAIN_EVENTS_FILE = AI_INSIGHTS_DIR / "train_events.jsonl"
MODELS_DIR = AI_INSIGHTS_DIR / "models"

ENABLE_AI_INSIGHTS = _env_bool("DATAMIND_AI_INSIGHTS_ENABLED", True)
ENABLE_ML = _env_bool("DATAMIND_AI_ML_ENABLED", True)
ENABLE_AI_INSIGHTS_DB = _env_bool("DATAMIND_AI_INSIGHTS_DB_ENABLED", True)

# Composite-score weights are centralized here to avoid scattered constants.
PHASE1_SCORE_WEIGHTS: Dict[str, float] = {
    "candidate_yield": 0.18,
    "resolve_rate": 0.28,
    "approval_rate": 0.24,
    "stop_signal_quality": 0.12,
    "cluster_resolution": 0.08,
    "ambiguity_inverse": 0.10,
}

PHASE3_SCORE_WEIGHTS: Dict[str, float] = {
    "match_rate": 0.35,
    "unmatched_inverse": 0.18,
    "ambiguous_inverse": 0.12,
    "sequence_quality": 0.20,
    "sequence_gate": 0.10,
    "rerun_inverse": 0.05,
}

MERGE_ASSIST_WEIGHTS: Dict[str, Dict[str, float]] = {
    "same_route_family": {
        "exact_stop_overlap_ratio": 0.06,
        "paired_stop_alignment_score": 0.18,
        "shared_middle_corridor_alignment_score": 0.07,
        "shared_corridor_overlap_score": 0.14,
        "path_similarity_score": 0.11,
        "length_ratio_score": 0.06,
        "ref_match_score": 0.07,
        "operator_match_score": 0.05,
        "network_match_score": 0.04,
        "overpass_name_similarity_score": 0.05,
        "relation_tag_consistency_score": 0.05,
        "normalized_route_name_similarity": 0.07,
        "alias_match_score": 0.04,
        "name_family_match_score": 0.11,
    },
    "opposite_direction": {
        "reverse_exact_sequence_similarity": 0.06,
        "paired_stop_alignment_score": 0.12,
        "reverse_order_of_paired_stops_score": 0.22,
        "endpoint_region_swap_score": 0.15,
        "shared_corridor_overlap_score": 0.08,
        "reverse_corridor_progression_score": 0.20,
        "shape_direction_opposition_score": 0.10,
        "from_to_swapped_match_score": 0.07,
        "endpoint_name_swap_similarity": 0.10,
    },
    "penalties": {
        "sequence_quality_penalty_a": 0.18,
        "sequence_quality_penalty_b": 0.18,
        "unmatched_penalty_a": 0.10,
        "unmatched_penalty_b": 0.10,
        "ambiguous_penalty_a": 0.06,
        "ambiguous_penalty_b": 0.06,
        "loop_or_branch_suspicion_penalty": 0.20,
        "low_evidence_coverage_penalty": 0.12,
    },
    "readiness_blend": {
        "same_route_family": 0.42,
        "opposite_direction": 0.38,
        "penalty_inverse": 0.20,
    },
}

MERGE_ASSIST_THRESHOLDS: Dict[str, float] = {
    "pair_distance_m": 180.0,
    "low_evidence_coverage_penalty_flag": 0.42,
    "loop_or_branch_suspicion_penalty_flag": 0.35,
    "sequence_quality_penalty_flag": 0.35,
    "direction_word_conflict_penalty": 0.08,
    "minimum_family_for_merge": PLAUSIBLE_PAIR_SCORE,
    "minimum_opposite_for_merge": PLAUSIBLE_PAIR_SCORE,
}

SEQUENCE_HEURISTICS: Dict[str, Any] = {
    "large_jump_m": 1800.0,
    "very_large_jump_m": 3000.0,
    "gap_seq_allowed": 1,
    "backtrack_angle_deg": 150.0,
    "backtrack_min_segment_m": 120.0,
    "duplicate_stop_penalty": 14.0,
    "large_jump_penalty": 9.0,
    "backtrack_penalty": 8.0,
    "gap_penalty": 8.0,
    "unmatched_ratio_penalty": 30.0,
    "ambiguous_ratio_penalty": 18.0,
    "edit_noise_penalty": 4.0,
    "reorder_recommend_below": 70.0,
}

# Versioned diagnostics profile for Step20 detector/scoring interpretation.
# Keep this explicit so threshold changes are auditable in run artifacts/logs.
SEQUENCE_DIAGNOSTIC_PROFILE_VERSION = "step20_diag_v2026_03_04_1"
SEQUENCE_DIAGNOSTIC_THRESHOLDS: Dict[str, Any] = {
    "unmatched_ratio_warning": 0.08,
    "unmatched_ratio_blocking": 0.20,
    "ambiguous_ratio_warning": 0.06,
    "ambiguous_ratio_blocking": 0.15,
    "sequence_quality_warning_score": 70.0,
    "sequence_quality_critical_score": 55.0,
    "reorder_pressure_confidence": 0.62,
}

MODEL_TASKS: Dict[str, Dict[str, Any]] = {
    "phase1_quality_score": {
        "kind": "regression",
        "artifact": MODELS_DIR / "phase1_quality_score_lgbm.txt",
    },
    "phase3_sequence_risk": {
        "kind": "classification",
        "artifact": MODELS_DIR / "phase3_sequence_risk_lgbm.txt",
    },
}

READINESS_GATES: Dict[str, Dict[str, Any]] = {
    "phase1_quality_score": {
        "min_logged_runs": 40,
        "min_labeled_rows": 30,
        "min_feature_completeness": 0.75,
        "min_retrains": 2,
        "plateau_window": 3,
        "plateau_delta": 0.015,
        "xgb_ready_rows": 120,
        "mlp_ready_rows": 260,
    },
    "phase3_sequence_risk": {
        "min_logged_runs": 40,
        "min_labeled_rows": 30,
        "min_feature_completeness": 0.75,
        "min_retrains": 2,
        "plateau_window": 3,
        "plateau_delta": 0.02,
        "xgb_ready_rows": 140,
        "mlp_ready_rows": 320,
        "min_minority_ratio": 0.10,
    },
}

PHASE1_REQUIRED_FEATURES = [
    "raw_count",
    "candidate_count",
    "stop_signal_count",
    "resolved_count",
    "approved_count",
]

PHASE3_REQUIRED_FEATURES = [
    "prior_stop_count",
    "matched_count",
    "unmatched_count",
    "ambiguous_count",
    "sequence_quality_score",
]

OPERATOR_FEEDBACK_USEFULNESS = ["yes", "partial", "no"]
OPERATOR_FEEDBACK_BOOL_NA = ["yes", "partial", "no", "not_applicable"]
OPERATOR_PATCH_AREAS = [
    "scoring",
    "thresholds",
    "logging",
    "ui",
    "sequence_detector",
    "readiness",
    "other",
]

REGRESSION_THRESHOLDS: Dict[str, Any] = {
    "quality_drop_points": 8.0,
    "warning_explosion_factor": 1.8,
    "warning_explosion_abs": 4.0,
    "phase1_candidate_drop_ratio": 0.30,
    "phase1_resolved_drop_ratio": 0.25,
    "phase1_approved_drop_ratio": 0.25,
    "phase3_unmatched_increase_ratio": 0.12,
    "phase3_ambiguous_increase_ratio": 0.08,
    "phase3_sequence_quality_drop_points": 10.0,
    "phase3_rerun_pressure_abs": 2.0,
    "phase3_sequence_edit_pressure_abs": 3.0,
    "failure_pattern_window": 8,
    "failure_pattern_min": 3,
}

LOW_VALUE_THRESHOLDS: Dict[str, Any] = {
    "reorder_confidence_low": 0.55,
    "ml_confidence_low": 0.35,
}

SANITY_LIMITS: Dict[str, Any] = {
    "max_issue_rows": 300,
    "repeat_fingerprint_threshold": 4,
}
