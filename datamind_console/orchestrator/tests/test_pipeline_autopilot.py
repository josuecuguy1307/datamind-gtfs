from __future__ import annotations

from dataclasses import replace
import json
import unittest
from uuid import uuid4

from datamind_console.orchestrator.pipeline_autopilot import (
    AIBotSnapshot,
    AdvisoryChatGPTInterpreter,
    BlockReasonCode,
    PolicyProfile,
    PHASE3_CANONICAL_ROUTE_STAGE_ORDER,
    PHASE3_LEGACY_NON_DEFAULT_STAGES,
    RunSessionState,
    STEP_P1_1_EXTRACT,
    STEP_P1_2_CHAIN,
    STEP_P1_3A_WORKSPACE,
    STEP_P1_3B_NEW_NODES,
    STEP_P1_4_PROMOTE,
    STEP_P2_1_SEMANTIC,
    STEP_P2_3_CLEANUP,
    STEP_P3_1_EXTRACT,
    STEP_P3_15_INVERSE,
    STEP_P3_2_STEP20,
    STEP_P3_3_REORDER,
    STEP_P3_4_STEP30,
    STEP_P3_4_STEP32,
    STEP_P3_4_STEP35,
    STEP_P3_4_STEP40,
    STEP_P3_5_MERGE,
    STEP_P3_6_CATALOG,
    ApprovalType,
    ExecutorResult,
    SupervisedPipelineAutopilot,
    ValidatorResult,
    _build_extractor_help_needed,
    _build_extractor_status,
    _handoff_build_consistency_candidates,
    _handoff_confidence_to_numeric,
    _handoff_extract_reorder_signal,
    build_ai_bot_telemetry_hook,
    build_default_pipeline_step_registry,
    build_phase_client_executor_bridge,
    build_phase_client_validator_bridge,
)


class _StubPatchChainInterpreter:
    def __init__(
        self,
        *,
        branch: str = "patch_extractor",
        should_patch: bool = True,
        patch_type: str = "extractor",
        fail_generation: bool = False,
        fail_compare: bool = False,
        comparator_result: str = "improved",
        comparator_recommendation: str = "accept",
    ) -> None:
        self.branch = str(branch)
        self.should_patch = bool(should_patch)
        self.patch_type = str(patch_type)
        self.fail_generation = bool(fail_generation)
        self.fail_compare = bool(fail_compare)
        self.comparator_result = str(comparator_result or "improved")
        self.comparator_recommendation = str(comparator_recommendation or "accept")
        self.generate_calls = []
        self.compare_calls = []

    def __call__(self, state, step, attempt_no, exec_result, validator_result, ai_snapshot, trigger):
        del exec_result, validator_result, ai_snapshot
        priorities = [f"branch:{self.branch}"]
        if self.should_patch:
            priorities.append(f"patch:{self.patch_type}:codex")
        return {
            "summary": f"{step.step_id} attempt {attempt_no} interpreted for stage5 patch chain",
            "priorities": priorities,
            "risk_notes": ["advisory_only_no_gate_override"],
            "task": "hades_pipeline_interpreter",
            "trigger": str(trigger),
            "snapshot_id": f"snap-{step.step_id}-{attempt_no}",
            "schema_name": "hades_pipeline_interpreter_response.json",
            "prompt_package": {
                "snapshot": {
                    "run_id": str(getattr(state, "run_id", "")),
                    "phase": str(getattr(step, "phase", "")),
                    "step_id": str(getattr(step, "step_id", "")),
                },
                "interpreter_structured": {
                    "recommended_branch": self.branch,
                    "patch_task_recommendation": {
                        "should_create_patch_task": bool(self.should_patch),
                        "patch_type": self.patch_type if self.should_patch else None,
                        "justification": "stage5-test",
                        "suggested_target": "codex" if self.should_patch else None,
                    },
                },
            },
        }

    def generate_patch_task(self, **kwargs):
        self.generate_calls.append(dict(kwargs or {}))
        if self.fail_generation:
            raise RuntimeError("schema validation failed for hades_patch_task_generator")
        patch_type = str((dict(kwargs.get("patch_intent") or {})).get("patch_type") or self.patch_type)
        return {
            "response": {
                "mode": "advisory_only",
                "task": "hades_patch_task_generator",
                "summary": "Generated patch task from interpreter recommendation.",
                "confidence": {"band": "medium", "score": 0.67, "reasons": ["stage5_test"]},
                "risk_flags": [{"code": "PATCH_REVIEW_REQUIRED", "severity": "medium", "message": "Operator review required"}],
                "insufficient_data_flags": [],
                "operator_confirmation_required": True,
                "evidence_used": ["stage5_event_snapshot"],
                "patch_task": {
                    "title": f"Stage5 {patch_type} patch",
                    "goal": "Test patch task generation chain.",
                    "scope": ["autopilot patch chain"],
                    "constraints": ["advisory_only", "no gate bypass"],
                    "prompt_text": "Apply a minimal safe patch and preserve validator/gate authority boundaries.",
                    "expected_files": ["datamind_console/orchestrator/pipeline_autopilot.py"],
                    "acceptance_criteria": ["patch task generated", "authority boundaries preserved"],
                    "high_impact_notes": ["dispatch requires approval"],
                },
            },
            "meta": {
                "task": "hades_patch_task_generator",
                "model": "stub-model",
                "latency_ms": 12,
                "token_usage": {"total_tokens": 42},
                "schema_name": "hades_patch_task_generator_response.json",
                "snapshot_id": "snap-patch-generator",
                "source": "stub",
                "prompt_package": {"task": "hades_patch_task_generator"},
            },
        }

    def compare_retest_outcome(self, **kwargs):
        self.compare_calls.append(dict(kwargs or {}))
        if self.fail_compare:
            raise RuntimeError("schema validation failed for hades_retest_comparator")
        return {
            "response": {
                "mode": "advisory_only",
                "task": "hades_retest_comparator",
                "summary": "Retest comparison complete for staged patch.",
                "result": self.comparator_result,
                "confidence": 0.74,
                "comparability_assessment": {
                    "is_comparable": True,
                    "notes": ["same corridor and policy profile"],
                },
                "attribution_assessment": {
                    "likely_attributable": True,
                    "confounders": [],
                },
                "metric_deltas": [
                    {
                        "metric": "quality_score",
                        "before": 0.61,
                        "after": 0.76,
                        "delta": 0.15,
                        "interpretation": "improved",
                    }
                ],
                "recommendation": self.comparator_recommendation,
                "recommended_next_actions": [
                    "Record outcome in patch registry.",
                    "Proceed with operator decision checkpoint.",
                ],
                "risk_notes": [
                    {
                        "code": "COMPARATOR_ADVISORY_ONLY",
                        "severity": "low",
                        "message": "Comparator result is advisory and does not bypass gates.",
                    }
                ],
            },
            "meta": {
                "task": "hades_retest_comparator",
                "model": "stub-model",
                "latency_ms": 14,
                "token_usage": {"total_tokens": 55},
                "schema_name": "hades_retest_comparator_response.json",
                "snapshot_id": "snap-retest-comparator",
                "source": "stub",
                "prompt_package": {"task": "hades_retest_comparator"},
            },
        }


class _FakePhase3SequenceClient:
    def __init__(self, *, variant_state: str = "no_variant_issue") -> None:
        self.route_id = str(uuid4())
        self.sequence_set_id = str(uuid4())
        self.geometry_set_id = str(uuid4())
        self.recommended_candidate_id = str(uuid4())
        self.alternate_candidate_id = str(uuid4())
        self.approved_candidate_id: str | None = None
        self.calls: list[tuple[str, dict]] = []
        self.variant_state = str(variant_state or "no_variant_issue")
        self.recommended_variant_group_key = "forward:main"
        self.alternate_variant_group_key = (
            "forward:branch" if self.variant_state != "no_variant_issue" else self.recommended_variant_group_key
        )

    def _shortlist(self) -> list[dict]:
        return [
            {
                "candidate_id": self.recommended_candidate_id,
                "rank": 1,
                "score": 93.0,
                "sequence_score": 93.0,
                "structural_sequence_score": 95.5,
                "traversability_score": 84.0,
                "segment_success_rate": 1.0,
                "failed_segment_count": 0,
                "detour_penalty": 3.2,
                "network_risk_indicators": [],
                "valhalla_evidence_summary": ["segment_success_rate=1.00", "valhalla_traversability_score=84.00"],
                "variant_group_key": self.recommended_variant_group_key,
                "variant_group_label": "forward | main",
                "sequence_orientation": "forward",
                "metrics": {
                    "confidence_label": "high",
                    "sequence_score": 93.0,
                    "structural_sequence_score": 95.5,
                    "valhalla_evidence_status": "available",
                    "valhalla_traversability_score": 84.0,
                    "valhalla_segment_success_rate": 1.0,
                    "valhalla_failed_segment_count": 0,
                    "valhalla_detour_penalty": 3.2,
                    "network_risk_indicators": [],
                    "valhalla_evidence_summary": ["segment_success_rate=1.00", "valhalla_traversability_score=84.00"],
                    "source_families": ["current_relation_order", "terminal_forward_order"],
                    "sequence_risk_indicators": [],
                    "variant_group_key": self.recommended_variant_group_key,
                    "variant_group_label": "forward | main",
                    "sequence_orientation": "forward",
                },
            },
            {
                "candidate_id": self.alternate_candidate_id,
                "rank": 2,
                "score": 81.0,
                "sequence_score": 81.0,
                "structural_sequence_score": 86.0,
                "traversability_score": 58.0,
                "segment_success_rate": 0.67,
                "failed_segment_count": 1,
                "detour_penalty": 17.5,
                "network_risk_indicators": ["extreme_detour_pressure"],
                "valhalla_evidence_summary": ["segment_success_rate=0.67", "failed_segments=1/3"],
                "variant_group_key": self.alternate_variant_group_key,
                "variant_group_label": (
                    "forward | branch"
                    if self.alternate_variant_group_key != self.recommended_variant_group_key
                    else "forward | main"
                ),
                "sequence_orientation": "forward",
                "metrics": {
                    "confidence_label": "medium",
                    "sequence_score": 81.0,
                    "structural_sequence_score": 86.0,
                    "valhalla_evidence_status": "available",
                    "valhalla_traversability_score": 58.0,
                    "valhalla_segment_success_rate": 0.67,
                    "valhalla_failed_segment_count": 1,
                    "valhalla_detour_penalty": 17.5,
                    "network_risk_indicators": ["extreme_detour_pressure"],
                    "valhalla_evidence_summary": ["segment_success_rate=0.67", "failed_segments=1/3"],
                    "source_families": ["spatial_smooth_forward"],
                    "sequence_risk_indicators": ["long_jump_penalty"],
                    "variant_group_key": self.alternate_variant_group_key,
                    "variant_group_label": (
                        "forward | branch"
                        if self.alternate_variant_group_key != self.recommended_variant_group_key
                        else "forward | main"
                    ),
                    "sequence_orientation": "forward",
                },
            },
        ]

    def _approved_variant_group_key(self) -> str | None:
        if self.approved_candidate_id == self.recommended_candidate_id:
            return self.recommended_variant_group_key
        if self.approved_candidate_id == self.alternate_candidate_id:
            return self.alternate_variant_group_key
        return None

    def _variant_groups(self) -> list[dict]:
        if self.variant_state == "no_variant_issue":
            return [
                {
                    "variant_group_key": self.recommended_variant_group_key,
                    "variant_group_label": "forward | main",
                "sequence_orientation": "forward",
                "candidate_count": 2,
                "top_candidate_id": self.recommended_candidate_id,
                "top_sequence_score": 93.0,
                "top_traversability_score": 84.0,
                "top_segment_success_rate": 1.0,
                "top_failed_segment_count": 0,
                "risk_indicators": [],
            }
        ]
        return [
            {
                "variant_group_key": self.recommended_variant_group_key,
                "variant_group_label": "forward | main",
                "sequence_orientation": "forward",
                "candidate_count": 1,
                "top_candidate_id": self.recommended_candidate_id,
                "top_sequence_score": 93.0,
                "top_traversability_score": 84.0,
                "top_segment_success_rate": 1.0,
                "top_failed_segment_count": 0,
                "risk_indicators": [],
            },
            {
                "variant_group_key": self.alternate_variant_group_key,
                "variant_group_label": "forward | branch",
                "sequence_orientation": "forward",
                "candidate_count": 1,
                "top_candidate_id": self.alternate_candidate_id,
                "top_sequence_score": 81.0,
                "top_traversability_score": 58.0,
                "top_segment_success_rate": 0.67,
                "top_failed_segment_count": 1,
                "risk_indicators": ["long_jump_penalty"],
            },
        ]

    def run_step_20_sequences(self, *, route_id, match_radius_m=None):
        del match_radius_m
        self.calls.append(("step20", {"route_id": str(route_id)}))
        proposal = {
            "requires_approval": True,
            "operator_approval_required": True,
            "needs_reorder": True,
            "apply_recommended": True,
            "blocking": True,
            "blocking_reason": "canonical_sequence_unapproved",
            "risk_level": "high",
            "recommended_candidate_id": self.recommended_candidate_id,
            "approved_candidate_id": self.approved_candidate_id,
            "candidate_shortlist": self._shortlist(),
            "recommendation_reason": "Recommended by Step20 composite sequence scoring.",
            "sequence_stabilized": bool(self.approved_candidate_id),
            "variant_pressure_detected": bool(self.variant_state != "no_variant_issue"),
            "variant_pressure_reasons": (
                ["divergent_tails_with_shared_corridor"] if self.variant_state != "no_variant_issue" else []
            ),
            "variant_state": self.variant_state,
            "variant_groups": self._variant_groups(),
            "recommended_variant_group_key": self.recommended_variant_group_key,
            "approved_variant_group_key": self._approved_variant_group_key(),
            "recommended_candidates_by_variant": [
                {
                    "variant_group_key": group["variant_group_key"],
                    "candidate_id": group["top_candidate_id"],
                    "sequence_orientation": group["sequence_orientation"],
                    "top_sequence_score": group["top_sequence_score"],
                    "candidate_count": group["candidate_count"],
                }
                for group in self._variant_groups()
            ],
            "variant_evidence_summary": (
                ["Top same-direction variant groups disagree."] if self.variant_state != "no_variant_issue" else []
            ),
            "direction_stable": True,
            "direction_reasons": [],
        }
        return {
            "route_id": str(route_id),
            "matched_count": 10,
            "unmatched_count": 0,
            "ambiguous_count": 0,
            "sequence_gate_pass": True,
            "sequence_quality_score": 92.0,
            "sequence_warning_tags": [],
            "sequence_warning_subtypes": {},
            "sequence_diagnostic_profile_version": "v1",
            "blocker_origin_hint": "sequence_resolution",
            "blocker_origin_confidence": "high",
            "blocker_origin_reason": "operator_approval_required",
            "step20_diagnostics_payload": {
                "triage_route": "phase3_sequence_resolution",
                "threshold_profile": {
                    "thresholds": {
                        "sequence_quality_warning_score": 70.0,
                    }
                },
            },
            "candidate_count": 2,
            "candidate_shortlist": self._shortlist(),
            "recommended_stop_sequence_candidate_id": self.recommended_candidate_id,
            "approved_stop_sequence_candidate_id": self.approved_candidate_id,
            "sequence_stabilized": bool(self.approved_candidate_id),
            "sequence_approval_status": ("approved" if self.approved_candidate_id else None),
            "variant_state": self.variant_state,
            "variant_pressure_detected": bool(self.variant_state != "no_variant_issue"),
            "variant_pressure_reasons": (
                ["divergent_tails_with_shared_corridor"] if self.variant_state != "no_variant_issue" else []
            ),
            "variant_groups": self._variant_groups(),
            "recommended_variant_group_key": self.recommended_variant_group_key,
            "approved_variant_group_key": self._approved_variant_group_key(),
            "recommended_candidates_by_variant": [
                {
                    "variant_group_key": group["variant_group_key"],
                    "candidate_id": group["top_candidate_id"],
                    "sequence_orientation": group["sequence_orientation"],
                    "top_sequence_score": group["top_sequence_score"],
                    "candidate_count": group["candidate_count"],
                }
                for group in self._variant_groups()
            ],
            "variant_evidence_summary": (
                ["Top same-direction variant groups disagree."] if self.variant_state != "no_variant_issue" else []
            ),
            "direction_stable": True,
            "direction_reasons": [],
            "reorder_recommended": True,
            "reorder_confidence": 0.88,
            "reorder_proposal": proposal,
            "stop_sequence_set_id": self.sequence_set_id,
        }

    def sync_unresolved_prior_to_phase1_requests(self, route_id, radius_m=3.0, dry_run=False):
        del route_id, radius_m, dry_run
        return {"created_requests": 0, "updated_requests": 0}

    def get_sequence_resolution_state(self, route_id):
        return {
            "route_id": str(route_id),
            "latest_stop_sequence_set_id": self.sequence_set_id,
            "candidate_shortlist": self._shortlist(),
            "recommended_stop_sequence_candidate_id": self.recommended_candidate_id,
            "approved_stop_sequence_candidate_id": self.approved_candidate_id,
            "sequence_stabilized": bool(self.approved_candidate_id),
            "approval_status": ("approved" if self.approved_candidate_id else None),
            "variant_state": self.variant_state,
            "variant_pressure_detected": bool(self.variant_state != "no_variant_issue"),
            "variant_pressure_reasons": (
                ["divergent_tails_with_shared_corridor"] if self.variant_state != "no_variant_issue" else []
            ),
            "variant_groups": self._variant_groups(),
            "recommended_variant_group_key": self.recommended_variant_group_key,
            "approved_variant_group_key": self._approved_variant_group_key(),
            "recommended_candidates_by_variant": [
                {
                    "variant_group_key": group["variant_group_key"],
                    "candidate_id": group["top_candidate_id"],
                    "sequence_orientation": group["sequence_orientation"],
                    "top_sequence_score": group["top_sequence_score"],
                    "candidate_count": group["candidate_count"],
                }
                for group in self._variant_groups()
            ],
            "variant_evidence_summary": (
                ["Top same-direction variant groups disagree."] if self.variant_state != "no_variant_issue" else []
            ),
            "direction_stable": True,
            "direction_reasons": [],
        }

    def approve_stop_sequence_candidate(self, *, route_id, stop_sequence_candidate_id, approved_by=None, notes=None):
        self.approved_candidate_id = str(stop_sequence_candidate_id)
        self.calls.append(
            (
                "approve_sequence",
                {
                    "route_id": str(route_id),
                    "candidate_id": str(stop_sequence_candidate_id),
                    "approved_by": approved_by,
                    "notes": notes,
                },
            )
        )
        return {
            "route_id": str(route_id),
            "sequence_approval_id": str(uuid4()),
            "approved_stop_sequence_candidate_id": self.approved_candidate_id,
            "stop_sequence_set_id": self.sequence_set_id,
            "approved_by": approved_by,
            "reordered_relation_stop_prior": True,
        }

    def get_step30_gate(self, route_id, *, stop_sequence_candidate_id=None, match_radius_m=3.0):
        del route_id, match_radius_m
        requested = str(stop_sequence_candidate_id) if stop_sequence_candidate_id is not None else self.approved_candidate_id
        reasons: list[str] = []
        if not self.approved_candidate_id:
            reasons.append("sequence_not_approved")
        if self.variant_state == "unresolved_multi_variant" and not self.approved_candidate_id:
            reasons.append("variant_pressure_blocking")
        if requested and self.approved_candidate_id and requested != self.approved_candidate_id:
            reasons.append("requested_sequence_not_canonical")
        return {
            "route_id": self.route_id,
            "total_count": 10,
            "matched_count": 10,
            "unmatched_count": 0,
            "ambiguous_count": 0,
            "approved_stop_sequence_candidate_id": self.approved_candidate_id,
            "requested_stop_sequence_candidate_id": requested,
            "sequence_stabilized": bool(self.approved_candidate_id),
            "approval_status": ("approved" if self.approved_candidate_id else None),
            "variant_state": self.variant_state,
            "variant_pressure_detected": bool(self.variant_state != "no_variant_issue"),
            "variant_pressure_reasons": (
                ["divergent_tails_with_shared_corridor"] if self.variant_state != "no_variant_issue" else []
            ),
            "variant_groups": self._variant_groups(),
            "recommended_variant_group_key": self.recommended_variant_group_key,
            "approved_variant_group_key": self._approved_variant_group_key(),
            "direction_stable": True,
            "direction_reasons": [],
            "blocking_reasons": reasons,
            "can_run_step30": len(reasons) == 0,
        }

    def run_step_30_geometry(self, *, route_id, stop_sequence_candidate_id):
        gate = self.get_step30_gate(
            route_id,
            stop_sequence_candidate_id=stop_sequence_candidate_id,
        )
        self.calls.append(
            (
                "step30",
                {
                    "route_id": str(route_id),
                    "candidate_id": str(stop_sequence_candidate_id),
                },
            )
        )
        return {
            "route_id": str(route_id),
            "geometry_set_id": self.geometry_set_id,
            "stop_sequence_candidate_id": str(stop_sequence_candidate_id),
            "n_candidates": 3,
            "geometry_quality": 0.82,
            "step30_gate": gate,
        }

    def run_step_32_stop_recovery(self, *, route_id, geometry_set_id):
        self.calls.append(
            (
                "step32",
                {
                    "route_id": str(route_id),
                    "geometry_set_id": str(geometry_set_id),
                },
            )
        )
        return {
            "route_id": str(route_id),
            "geometry_set_id": str(geometry_set_id),
            "geometry_candidate_count": 3,
            "recovered_total": 2,
            "ambiguous_total": 1,
            "rejected_total": 4,
        }


class PipelineAutopilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_default_pipeline_step_registry()
        self._patch_dispatch_calls = []

    def _new_engine(self, *, profile: str = PolicyProfile.BALANCED.value) -> SupervisedPipelineAutopilot:
        engine = SupervisedPipelineAutopilot(
            step_registry=self.registry,
            enabled=True,
        )
        self._profile = profile
        engine.patch_dispatch_runner = self._stub_patch_dispatch_runner
        return engine

    def _register_phase3_live_bridge(
        self,
        engine: SupervisedPipelineAutopilot,
        *,
        phase3_client: _FakePhase3SequenceClient,
    ) -> None:
        executors = build_phase_client_executor_bridge(phase3_client=phase3_client)
        validators = build_phase_client_validator_bridge()
        for name in (
            "phase3_step20_sequence",
            "phase3_reorder_proposal",
            "phase3_reorder_apply",
            "phase3_step30_geometry",
            "phase3_step32_stop_recovery",
        ):
            engine.register_executor(name, executors[name])
            if name in validators:
                engine.register_validator(name, validators[name])

    @staticmethod
    def _strong_p3_partial_fetch_summary(
        *,
        route_id: str | None = None,
        prior_stop_count: int = 0,
        fetch_status_classification: str = "fetch_partial_after_valid_selection",
    ) -> dict:
        rid = str(route_id or uuid4())
        return {
            "route_id": rid,
            "prior_stop_count": int(prior_stop_count),
            "candidate_universe_summary": {
                "candidate_universe_count": 8,
                "candidate_scored_count": 8,
                "candidate_fetch_evaluated_count": 3,
                "query_strategy": "bbox_first_broad",
                "top_stop_prior_count": 42,
                "hard_filters_applied": [],
                "soft_signals_used": ["refs", "operator", "name"],
            },
            "selection_summary": {
                "selection_status": "provisional_selected",
                "selected_osm_relation_id": 654321,
                "selected_rank": 1,
                "selected_score": 18.7,
                "selection_confidence": 0.7633,
                "score_gap_top2": 2.4,
                "selected_relation_stop_prior_count": 42,
                "selection_reason_codes": ["stop_prior_signal_strong", "soft_ref_match"],
            },
            "selected_relation_summary": {
                "osm_relation_id": 654321,
                "selection_rank": 1,
                "selection_confidence": 0.7633,
                "score": 18.7,
                "stop_prior_count": 42,
                "selection_reason_codes": ["stop_prior_signal_strong", "soft_ref_match"],
            },
            "extractor_diagnostics": {
                "candidate_count": 8,
                "top_stop_prior_count": 42,
                "signal_strength": "high",
                "quality_flags": [],
            },
            "fetch_status_classification": fetch_status_classification,
            "fetch_observability_gap": True,
            "extraction_attempt_history_summary": {
                "attempt_count_total": 1,
                "successful_attempt_count": 1,
                "non_empty_attempt_count": 1,
                "same_config_repeat_count": 0,
                "retry_diversity_count": 1,
                "diversified_attempts": False,
                "attempts": [
                    {
                        "attempt_no": 1,
                        "status": "success",
                        "action_or_template_used": "step_10_fetch",
                        "candidate_count": None,
                        "raw_elements_count": None,
                    }
                ],
            },
            "validator_payload": {
                "route_id": rid,
                "prior_stop_count": int(prior_stop_count),
                "prior_stop_evidence_count": 42,
                "candidate_universe_summary": {
                    "candidate_universe_count": 8,
                    "candidate_scored_count": 8,
                    "candidate_fetch_evaluated_count": 3,
                    "query_strategy": "bbox_first_broad",
                    "top_stop_prior_count": 42,
                    "hard_filters_applied": [],
                    "soft_signals_used": ["refs", "operator", "name"],
                },
                "selection_summary": {
                    "selection_status": "provisional_selected",
                    "selected_osm_relation_id": 654321,
                    "selected_rank": 1,
                    "selected_score": 18.7,
                    "selection_confidence": 0.7633,
                    "score_gap_top2": 2.4,
                    "selected_relation_stop_prior_count": 42,
                    "selection_reason_codes": ["stop_prior_signal_strong", "soft_ref_match"],
                },
                "selected_relation_summary": {
                    "osm_relation_id": 654321,
                    "selection_rank": 1,
                    "selection_confidence": 0.7633,
                    "score": 18.7,
                    "stop_prior_count": 42,
                    "selection_reason_codes": ["stop_prior_signal_strong", "soft_ref_match"],
                },
                "selected_relation_present": True,
                "selected_osm_relation_id": 654321,
                "selected_relation_stop_prior_count": 42,
                "top_stop_prior_count": 42,
                "selection_confidence": 0.7633,
                "selection_reason_codes": ["stop_prior_signal_strong", "soft_ref_match"],
                "strong_bundle_evidence": True,
                "fetch_status_classification": fetch_status_classification,
                "fetch_observability_gap": True,
                "extractor_attempt_history_summary": {
                    "attempt_count_total": 1,
                    "successful_attempt_count": 1,
                    "non_empty_attempt_count": 1,
                    "same_config_repeat_count": 0,
                    "retry_diversity_count": 1,
                    "diversified_attempts": False,
                    "attempts": [
                        {
                            "attempt_no": 1,
                            "status": "success",
                            "action_or_template_used": "step_10_fetch",
                        }
                    ],
                },
            },
        }

    def _stub_patch_dispatch_runner(
        self,
        *,
        patch_task_id: str,
        prompt_text: str,
        target: str,
        prompt_artifact: dict,
        dispatch_context: dict,
    ) -> dict:
        self._patch_dispatch_calls.append(
            {
                "patch_task_id": str(patch_task_id or ""),
                "prompt_text": str(prompt_text or ""),
                "target": str(target or ""),
                "prompt_artifact": dict(prompt_artifact or {}),
                "dispatch_context": dict(dispatch_context or {}),
            }
        )
        return {
            "status": "success",
            "target": str(target or "codex"),
            "configured_provider_name": "codex_cli" if str(target or "").strip().lower() != "claude" else "claude_cli",
            "configured_model_name": "gpt-5.2-codex" if str(target or "").strip().lower() != "claude" else None,
            "command_used": "codex exec --model gpt-5.2-codex --skip-git-repo-check -",
            "duration_ms": 19,
            "output_file": "/tmp/codex.out",
            "stderr_file": "/tmp/codex.err",
            "error_summary": None,
            "return_code": 0,
            "prompt_file": "/tmp/inbox/codex_prompt.md",
            "prompt_file_final": "/tmp/done/codex_prompt.md",
            "log_file": "/tmp/local_runner/runs.jsonl",
        }

    @staticmethod
    def _set_feature_flags(
        engine: SupervisedPipelineAutopilot,
        *,
        adaptive_retry_enabled: bool | None = None,
        shadow_learned_retry_ranking_enabled: bool | None = None,
    ) -> None:
        ff = engine.feature_flags
        engine.feature_flags = ff.__class__(
            live_bridge_enabled=ff.live_bridge_enabled,
            db_persistence_enabled=ff.db_persistence_enabled,
            auto_resume_enabled=ff.auto_resume_enabled,
            policy_profiles_enabled=ff.policy_profiles_enabled,
            worker_enabled=ff.worker_enabled,
            slo_alerting_enabled=ff.slo_alerting_enabled,
            patch_chaining_enabled=ff.patch_chaining_enabled,
            patch_auto_dispatch_enabled=ff.patch_auto_dispatch_enabled,
            adaptive_retry_enabled=(
                ff.adaptive_retry_enabled
                if adaptive_retry_enabled is None
                else bool(adaptive_retry_enabled)
            ),
            shadow_learned_retry_ranking_enabled=(
                ff.shadow_learned_retry_ranking_enabled
                if shadow_learned_retry_ranking_enabled is None
                else bool(shadow_learned_retry_ranking_enabled)
            ),
        )

    @staticmethod
    def _priority_value(priorities, prefix: str) -> str:
        token = f"{prefix}:"
        for row in list(priorities or []):
            txt = str(row or "").strip()
            if txt.startswith(token):
                return txt[len(token) :]
        return ""

    def _run_stage5_patch_context(
        self,
        *,
        branch: str = "patch_extractor",
        patch_type: str = "extractor",
    ):
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        stub = _StubPatchChainInterpreter(branch=branch, should_patch=True, patch_type=patch_type)
        engine.chatgpt_interpreter = stub

        def warning_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="warning", gate_passed=True, passable_warning=True, warnings=["warn"])

        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, warning_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        rec = run.step_execution_records[-1]
        patch_artifacts = [dict(a or {}) for a in list(rec.artifacts or []) if dict(a or {}).get("artifact_type") == "patch_task"]
        self.assertTrue(patch_artifacts)
        patch_task_id = str(patch_artifacts[0].get("patch_task_id") or patch_artifacts[0].get("artifact_id") or "")
        self.assertTrue(patch_task_id)
        return engine, stub, run, patch_task_id

    def test_p1_extraction_retry_then_success(self) -> None:
        engine = self._new_engine()

        v_calls = {"n": 0}

        def p1_extract_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            v_calls["n"] += 1
            if v_calls["n"] == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_LOW_COVERAGE,
                    summary="coverage too low",
                    evidence={"coverage": 0.21},
                )
            return ValidatorResult(status="pass", gate_passed=True, evidence={"coverage": 0.89})

        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, p1_extract_validator)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=self._profile,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id)

        self.assertEqual(run.attempt_counters.get(STEP_P1_1_EXTRACT), 2)
        self.assertTrue(any(e.event_type == "step_retry_scheduled" for e in run.events))
        self.assertEqual(run.current_step_id, STEP_P1_3A_WORKSPACE)
        self.assertEqual(run.status, "paused")

    def test_p1_normalize_chain_failure_produces_structured_block_reason(self) -> None:
        engine = self._new_engine()

        def p1_chain_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(
                status="failed",
                gate_passed=False,
                block_reason_code=BlockReasonCode.NORMALIZE_FAILED,
                summary="normalize failed due null geom",
                evidence={"missing_geom": 17},
            )

        engine.register_validator(self.registry[STEP_P1_2_CHAIN].validator, p1_chain_validator)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=self._profile,
            start_step_id=STEP_P1_2_CHAIN,
        )
        run = engine.advance_run(run.run_id)

        self.assertEqual(run.status, "paused")
        rec = run.step_execution_records[-1]
        self.assertIsNotNone(rec.block_reason)
        block = dict(rec.block_reason or {})
        self.assertEqual(block.get("code"), BlockReasonCode.NORMALIZE_FAILED)
        for key in (
            "severity",
            "summary",
            "validator_evidence",
            "ai_bot_metrics_snapshot",
            "recommended_next_action",
        ):
            self.assertIn(key, block)

    def test_p1_chain_contract_mismatch_blocks_with_specific_reason(self) -> None:
        engine = self._new_engine()
        bridge_validators = build_phase_client_validator_bridge()
        engine.register_validator(self.registry[STEP_P1_2_CHAIN].validator, bridge_validators["phase1_chain"])

        def p1_chain_executor(state, step, attempt_no, params):
            del state, step, attempt_no, params
            return ExecutorResult(
                ok=True,
                summary={
                    "normalize": {"ok": True},
                    "features": {"ok": True},
                    "cluster": {"ok": True, "degenerate": False},
                    "resolve": {"ok": True, "resolved_total": 1652},
                    "validator_payload": {
                        "normalize_ok": True,
                        "features_ok": True,
                        "cluster_ok": True,
                        "cluster_degenerate": False,
                        "resolve_ok": True,
                        "resolved_count": 0,
                    },
                },
                artifacts=[],
            )

        engine.register_executor(self.registry[STEP_P1_2_CHAIN].executor, p1_chain_executor)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=self._profile,
            start_step_id=STEP_P1_2_CHAIN,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(run.status, "paused")
        rec = run.step_execution_records[-1]
        self.assertIsNotNone(rec.block_reason)
        block = dict(rec.block_reason or {})
        self.assertEqual(block.get("code"), BlockReasonCode.VALIDATOR_PAYLOAD_CONTRACT_MISMATCH)
        self.assertNotEqual(block.get("code"), BlockReasonCode.RESOLVE_ZERO_RESULTS)
        evidence = dict(block.get("validator_evidence") or {})
        self.assertEqual(int(evidence.get("resolved_count_executor") or 0), 1652)
        self.assertEqual(int(evidence.get("resolved_count_payload") or 0), 0)

    def test_p3_step20_clean_pass_auto_advances_to_step30(self) -> None:
        engine = self._new_engine()

        def step20_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_validator(self.registry[STEP_P3_2_STEP20].validator, step20_validator)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=self._profile,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(run.current_step_id, STEP_P3_4_STEP30)
        self.assertEqual(run.status, "running")
        self.assertTrue(
            any(e.event_type == "step_completed" and e.step_id == STEP_P3_2_STEP20 for e in run.events)
        )

    def test_p3_step20_warning_passable_obeys_policy_profiles(self) -> None:
        def warning_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(
                status="warning",
                gate_passed=True,
                passable_warning=True,
                warnings=["minor sequence drift"],
                evidence={"sequence_quality_score": 78.4},
            )

        conservative = self._new_engine(profile=PolicyProfile.CONSERVATIVE.value)
        conservative.register_validator(self.registry[STEP_P3_2_STEP20].validator, warning_validator)
        run_c = conservative.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.CONSERVATIVE.value,
            start_step_id=STEP_P3_2_STEP20,
        )
        run_c = conservative.advance_run(run_c.run_id, max_steps=1)
        self.assertEqual(run_c.status, "paused")
        self.assertEqual(run_c.current_step_id, STEP_P3_2_STEP20)
        self.assertTrue(any(e.event_type == "step_warning" for e in run_c.events))

        balanced = self._new_engine(profile=PolicyProfile.BALANCED.value)
        balanced.register_validator(self.registry[STEP_P3_2_STEP20].validator, warning_validator)
        run_b = balanced.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P3_2_STEP20,
        )
        run_b = balanced.advance_run(run_b.run_id, max_steps=1)
        self.assertEqual(run_b.status, "running")
        self.assertEqual(run_b.current_step_id, STEP_P3_4_STEP30)

    def test_step20_bridge_validator_normalizes_0_1_quality_scale(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        bridge_validators = build_phase_client_validator_bridge()
        engine.register_validator(self.registry[STEP_P3_2_STEP20].validator, bridge_validators["phase3_step20_sequence"])

        def step20_executor(state, step, attempt_no, params):
            del state, step, attempt_no, params
            return ExecutorResult(
                ok=True,
                summary={
                    "validator_payload": {
                        "unmatched_count": 0,
                        "ambiguous_count": 0,
                        "sequence_gate_pass": True,
                        "sequence_quality_score": 0.82,  # legacy 0..1 scale
                        "sequence_quality_warning_threshold": 70.0,
                    }
                },
                artifacts=[],
            )

        engine.register_executor(self.registry[STEP_P3_2_STEP20].executor, step20_executor)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(run.status, "running")
        self.assertEqual(run.current_step_id, STEP_P3_4_STEP30)
        rec = run.step_execution_records[-1]
        self.assertEqual(rec.validator_result.get("status"), "pass")
        self.assertAlmostEqual(float(rec.validator_result.get("evidence", {}).get("sequence_quality_score")), 82.0, places=2)

    def test_step20_bridge_validator_exposes_threshold_noise_warning(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.CONSERVATIVE.value)
        bridge_validators = build_phase_client_validator_bridge()
        engine.register_validator(self.registry[STEP_P3_2_STEP20].validator, bridge_validators["phase3_step20_sequence"])

        def step20_executor(state, step, attempt_no, params):
            del state, step, attempt_no, params
            return ExecutorResult(
                ok=True,
                summary={
                    "validator_payload": {
                        "unmatched_count": 0,
                        "ambiguous_count": 0,
                        "sequence_gate_pass": True,
                        "sequence_quality_score": 88.0,
                        "sequence_warning_subtypes": {"detector_threshold_noise_candidate": 1},
                    }
                },
                artifacts=[],
            )

        engine.register_executor(self.registry[STEP_P3_2_STEP20].executor, step20_executor)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.CONSERVATIVE.value,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(run.status, "paused")
        self.assertEqual(run.current_step_id, STEP_P3_2_STEP20)
        rec = run.step_execution_records[-1]
        self.assertEqual(rec.validator_result.get("status"), "warning")
        self.assertIn("sequence_threshold_noise_candidate", list(rec.validator_result.get("warnings") or []))

    def test_live_phase3_step20_executor_emits_canonical_sequence_proposal(self) -> None:
        fake = _FakePhase3SequenceClient()
        bridge = build_phase_client_executor_bridge(phase3_client=fake)
        state = RunSessionState(
            run_id="run-live-p3-step20",
            pipeline_scope={"phases": ["phase3"], "phase3": {"route_id": fake.route_id}},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_2_STEP20,
        )

        out = bridge["phase3_step20_sequence"](
            state,
            self.registry[STEP_P3_2_STEP20],
            1,
            {},
        )

        self.assertTrue(out.ok)
        proposal = dict(out.summary.get("reorder_proposal") or {})
        payload = dict(out.summary.get("validator_payload") or {})
        self.assertTrue(bool(proposal))
        self.assertTrue(bool(payload.get("proposal_ready")))
        self.assertEqual(
            str(proposal.get("recommended_candidate_id") or ""),
            fake.recommended_candidate_id,
        )
        self.assertEqual(
            str(payload.get("recommended_candidate_id") or ""),
            fake.recommended_candidate_id,
        )
        self.assertEqual(len(list(proposal.get("candidate_shortlist") or [])), 2)

    def test_live_phase3_step20_executor_groups_variant_candidates_when_unresolved(self) -> None:
        fake = _FakePhase3SequenceClient(variant_state="unresolved_multi_variant")
        bridge = build_phase_client_executor_bridge(phase3_client=fake)
        state = RunSessionState(
            run_id="run-live-p3-step20-variant",
            pipeline_scope={"phases": ["phase3"], "phase3": {"route_id": fake.route_id}},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_2_STEP20,
        )

        out = bridge["phase3_step20_sequence"](
            state,
            self.registry[STEP_P3_2_STEP20],
            1,
            {},
        )

        proposal = dict(out.summary.get("reorder_proposal") or {})
        self.assertEqual(str(proposal.get("variant_state") or ""), "unresolved_multi_variant")
        self.assertEqual(len(list(proposal.get("variant_groups") or [])), 2)
        self.assertEqual(
            str(proposal.get("recommended_variant_group_key") or ""),
            fake.recommended_variant_group_key,
        )

    def test_live_phase3_reorder_approval_resumes_to_step30_with_selected_candidate(self) -> None:
        engine = self._new_engine()
        fake = _FakePhase3SequenceClient()
        self._register_phase3_live_bridge(engine, phase3_client=fake)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"], "phase3": {"route_id": fake.route_id}},
            policy_profile=self._profile,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id, max_steps=2)

        self.assertEqual(run.status, "waiting_for_approval")
        self.assertEqual(run.current_step_id, STEP_P3_3_REORDER)
        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(len(pending), 1)
        proposal = dict(
            dict(dict(pending[0].evidence_payload or {}).get("validator_result") or {}).get("evidence") or {}
        ).get("proposal")
        self.assertTrue(bool(proposal))

        selected_candidate_id = fake.alternate_candidate_id
        run = engine.resolve_approval(
            run_id=run.run_id,
            approval_id=pending[0].approval_id,
            decision="approved",
            operator_id="op-seq",
            operator_role="admin",
            operator_decision="Approve alternate canonical sequence",
            resolution_payload={"stop_sequence_candidate_id": selected_candidate_id},
            max_steps_after_resume=0,
        )

        self.assertEqual(fake.approved_candidate_id, selected_candidate_id)
        self.assertEqual(run.current_step_id, STEP_P3_4_STEP30)
        self.assertNotEqual(run.current_step_id, STEP_P3_2_STEP20)
        approve_calls = [payload for name, payload in fake.calls if name == "approve_sequence"]
        self.assertEqual(len(approve_calls), 1)
        self.assertEqual(str(approve_calls[0].get("candidate_id") or ""), selected_candidate_id)

    def test_live_phase3_reorder_approval_requires_explicit_candidate_selection(self) -> None:
        engine = self._new_engine()
        fake = _FakePhase3SequenceClient()
        self._register_phase3_live_bridge(engine, phase3_client=fake)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"], "phase3": {"route_id": fake.route_id}},
            policy_profile=self._profile,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id, max_steps=2)
        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(len(pending), 1)

        with self.assertRaises(ValueError):
            engine.resolve_approval(
                run_id=run.run_id,
                approval_id=pending[0].approval_id,
                decision="approved",
                operator_id="op-seq",
                operator_role="admin",
                operator_decision="Approve without selecting candidate",
                resolution_payload={},
                max_steps_after_resume=0,
            )

        self.assertFalse(any(name == "approve_sequence" for name, _ in fake.calls))

    def test_live_phase3_reorder_proposal_includes_valhalla_shortlist_evidence(self) -> None:
        engine = self._new_engine()
        fake = _FakePhase3SequenceClient(variant_state="mild_variant_pressure_dominant_group")
        self._register_phase3_live_bridge(engine, phase3_client=fake)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"], "phase3": {"route_id": fake.route_id}},
            policy_profile=self._profile,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id, max_steps=2)
        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(len(pending), 1)

        proposal = dict(
            dict(dict(pending[0].evidence_payload or {}).get("validator_result") or {}).get("evidence") or {}
        ).get("proposal") or {}
        shortlist = list(proposal.get("candidate_shortlist") or [])
        self.assertTrue(shortlist)
        self.assertAlmostEqual(float(shortlist[0].get("traversability_score") or 0.0), 84.0, places=3)
        self.assertAlmostEqual(float(shortlist[0].get("segment_success_rate") or 0.0), 1.0, places=3)
        self.assertEqual(int(shortlist[1].get("failed_segment_count") or 0), 1)
        self.assertTrue(list(shortlist[1].get("network_risk_indicators") or []))

    def test_live_phase3_step30_blocks_without_sequence_approval(self) -> None:
        engine = self._new_engine()
        fake = _FakePhase3SequenceClient()
        self._register_phase3_live_bridge(engine, phase3_client=fake)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"], "phase3": {"route_id": fake.route_id}},
            policy_profile=self._profile,
            start_step_id=STEP_P3_4_STEP30,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(run.status, "paused")
        self.assertEqual(run.current_step_id, STEP_P3_4_STEP30)
        rec = run.step_execution_records[-1]
        self.assertEqual(rec.block_reason.get("code"), BlockReasonCode.STEP30_SEQUENCE_RESOLUTION_BLOCKING)
        self.assertFalse(any(name == "step30" for name, _ in fake.calls))

    def test_live_phase3_step30_blocks_unresolved_multi_variant_before_approval(self) -> None:
        engine = self._new_engine()
        fake = _FakePhase3SequenceClient(variant_state="unresolved_multi_variant")
        self._register_phase3_live_bridge(engine, phase3_client=fake)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"], "phase3": {"route_id": fake.route_id}},
            policy_profile=self._profile,
            start_step_id=STEP_P3_4_STEP30,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        rec = run.step_execution_records[-1]
        evidence = dict(rec.validator_result.get("evidence") or {})
        self.assertEqual(rec.block_reason.get("code"), BlockReasonCode.STEP30_SEQUENCE_RESOLUTION_BLOCKING)
        self.assertIn("variant_pressure_blocking", list(evidence.get("blocking_reasons") or []))
        self.assertEqual(str(evidence.get("step30_gate", {}).get("variant_state") or ""), "unresolved_multi_variant")

    def test_live_phase3_step30_runs_after_sequence_approval(self) -> None:
        engine = self._new_engine()
        fake = _FakePhase3SequenceClient()
        fake.approved_candidate_id = fake.recommended_candidate_id
        self._register_phase3_live_bridge(engine, phase3_client=fake)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"], "phase3": {"route_id": fake.route_id}},
            policy_profile=self._profile,
            start_step_id=STEP_P3_4_STEP30,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(run.status, "running")
        self.assertEqual(run.current_step_id, STEP_P3_4_STEP32)
        step30_calls = [payload for name, payload in fake.calls if name == "step30"]
        self.assertEqual(len(step30_calls), 1)
        self.assertEqual(
            str(step30_calls[0].get("candidate_id") or ""),
            fake.recommended_candidate_id,
        )

    def test_live_phase3_step32_runs_after_step30(self) -> None:
        engine = self._new_engine()
        fake = _FakePhase3SequenceClient()
        fake.approved_candidate_id = fake.recommended_candidate_id
        self._register_phase3_live_bridge(engine, phase3_client=fake)

        run = engine.start_run(
            pipeline_scope={
                "phases": ["phase3"],
                "phase3": {"route_id": fake.route_id, "geometry_set_id": fake.geometry_set_id},
            },
            policy_profile=self._profile,
            start_step_id=STEP_P3_4_STEP32,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(run.status, "running")
        self.assertEqual(run.current_step_id, STEP_P3_4_STEP35)
        step32_calls = [payload for name, payload in fake.calls if name == "step32"]
        self.assertEqual(len(step32_calls), 1)
        self.assertEqual(str(step32_calls[0].get("geometry_set_id") or ""), fake.geometry_set_id)

    def test_approval_apply_failure_does_not_advance_run(self) -> None:
        engine = self._new_engine()

        def reorder_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="pass", gate_passed=True)

        def failing_apply(state, step, attempt_no, params):
            del state, step, attempt_no, params
            return ExecutorResult(ok=False, summary={"error": "apply_failed"}, artifacts=[])

        engine.register_validator(self.registry[STEP_P3_3_REORDER].validator, reorder_validator)
        engine.register_executor(self.registry[STEP_P3_3_REORDER].approval_apply_executor or "", failing_apply)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=self._profile,
            start_step_id=STEP_P3_3_REORDER,
        )
        run = engine.advance_run(run.run_id)
        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(len(pending), 1)

        run = engine.resolve_approval(
            run_id=run.run_id,
            approval_id=pending[0].approval_id,
            decision="approved",
            operator_id="op-2",
            operator_role="admin",
            operator_decision="approve reorder apply",
            resolution_payload={"stop_sequence_candidate_id": str(uuid4())},
            max_steps_after_resume=0,
        )

        self.assertEqual(run.status, "failed")
        self.assertEqual(run.current_step_id, STEP_P3_3_REORDER)
        self.assertTrue(any(e.event_type == "run_failed" for e in run.events))

    def test_confidence_band_normalization(self) -> None:
        self.assertAlmostEqual(float(_handoff_confidence_to_numeric("high") or 0.0), 0.9, places=3)
        self.assertAlmostEqual(float(_handoff_confidence_to_numeric("medium") or 0.0), 0.65, places=3)
        self.assertAlmostEqual(float(_handoff_confidence_to_numeric("low") or 0.0), 0.35, places=3)

    def test_reorder_signal_normalization_canonicalizes_legacy_fields(self) -> None:
        signal = _handoff_extract_reorder_signal(
            exec_summary={},
            validator_evidence={},
            ai_proposals={"reorder": {"recommended": True, "confidence": "medium"}},
        )
        self.assertTrue(signal.get("recommended"))
        self.assertAlmostEqual(float(signal.get("confidence") or 0.0), 0.65, places=3)

        legacy_signal = _handoff_extract_reorder_signal(
            exec_summary={},
            validator_evidence={},
            ai_proposals={"reorder_proposal": {"needs_reorder": True, "confidence": "high"}},
        )
        self.assertTrue(legacy_signal.get("recommended"))
        self.assertAlmostEqual(float(legacy_signal.get("confidence") or 0.0), 0.9, places=3)

    def test_consistency_candidate_generation_for_resolve_payload_mismatch(self) -> None:
        candidates = _handoff_build_consistency_candidates(
            step_id=STEP_P1_2_CHAIN,
            exec_summary={
                "resolve": {"resolved_total": 1652},
                "validator_payload": {"resolved_count": 0},
            },
            validator_result={
                "block_reason_code": BlockReasonCode.RESOLVE_ZERO_RESULTS.value,
                "evidence": {"resolved_count": 0},
            },
            ai_snapshot={"proposals": {"comparison": {"latest": {"resolved_count": 1652}}}},
        )
        codes = {str(c.get("code") or "") for c in candidates}
        self.assertIn("RESOLVE_COUNT_CONTRACT_MISMATCH", codes)
        self.assertIn("BLOCK_REASON_EVIDENCE_MISMATCH", codes)
        self.assertIn("VALIDATOR_PAYLOAD_STALE_OR_DEFAULTED", codes)

    def test_p3_validator_preserves_strong_bundle_when_fetch_is_partial(self) -> None:
        validators = build_phase_client_validator_bridge()
        state = RunSessionState(
            run_id="run-p3-validator-partial",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
        )
        exec_result = ExecutorResult(
            ok=True,
            summary=self._strong_p3_partial_fetch_summary(),
            artifacts=[],
        )

        out = validators["phase3_route_extract"](
            state,
            self.registry[STEP_P3_1_EXTRACT],
            1,
            exec_result,
        )

        self.assertEqual(out.status, "blocked")
        self.assertEqual(out.block_reason_code, BlockReasonCode.VALIDATOR_PAYLOAD_CONTRACT_MISMATCH)
        self.assertNotEqual(out.block_reason_code, BlockReasonCode.EXTRACTION_EMPTY)
        self.assertIn("Route selected", str(out.summary or ""))
        self.assertNotIn("zero prior stops", str(out.summary or "").lower())
        self.assertNotIn("did not produce route_id", str(out.summary or "").lower())
        self.assertTrue(bool(dict(out.evidence or {}).get("strong_bundle_evidence")))
        self.assertEqual(
            str(dict(out.evidence or {}).get("fetch_status_classification") or ""),
            "fetch_partial_after_valid_selection",
        )

    def test_p3_route_extract_fetch_attempt_not_marked_empty_when_relation_storage_succeeds(self) -> None:
        class _FakePhase3Client:
            def run_step_05_discover(self, **kwargs):
                del kwargs
                return {
                    "route_id": str(uuid4()),
                    "chosen_osm_relation_id": 654321,
                    "candidate_universe_summary": {
                        "candidate_universe_count": 8,
                        "candidate_scored_count": 8,
                        "candidate_fetch_evaluated_count": 3,
                        "query_strategy": "bbox_first_broad",
                        "top_stop_prior_count": 42,
                        "hard_filters_applied": [],
                        "soft_signals_used": ["refs", "operator", "name"],
                    },
                    "selection_summary": {
                        "selection_status": "provisional_selected",
                        "selected_osm_relation_id": 654321,
                        "selected_rank": 1,
                        "selected_score": 18.7,
                        "selection_confidence": 0.7633,
                        "score_gap_top2": 2.4,
                        "selected_relation_stop_prior_count": 42,
                        "selection_reason_codes": ["stop_prior_signal_strong", "soft_ref_match"],
                    },
                    "candidate_preview": [{"osm_relation_id": 654321, "selection_rank": 1}],
                    "extractor_diagnostics": {
                        "candidate_count": 8,
                        "top_stop_prior_count": 42,
                        "signal_strength": "high",
                        "quality_flags": [],
                    },
                    "extractor_attempts": [{"phase": "primary", "returncode": 0, "error_class": "ok", "candidate_count": 8}],
                }

            def run_step_10_fetch(self, **kwargs):
                route_id = str(kwargs.get("route_id") or "")
                return {
                    "route_id": route_id,
                    "osm_relation_id": 654321,
                    "stored": True,
                    "fetch_relation_stored": True,
                    "fetch_status": "success",
                    "fetch_status_classification": "fetch_partial_after_valid_selection",
                    "fetch_observability_gap": True,
                    "selected_relation_summary": {
                        "osm_relation_id": 654321,
                        "selection_rank": 1,
                        "selection_confidence": 0.7633,
                        "score": 18.7,
                        "stop_prior_count": 42,
                        "selection_reason_codes": ["stop_prior_signal_strong", "soft_ref_match"],
                    },
                    "candidate_universe_count": 8,
                }

            def get_relation_stop_prior(self, _route_uuid):
                return []

        bridge = build_phase_client_executor_bridge(phase3_client=_FakePhase3Client())
        state = RunSessionState(
            run_id="run-p3-fetch-status",
            pipeline_scope={
                "phases": ["phase3"],
                "phase3": {
                    "bbox": {"south": -0.32, "west": -78.58, "north": -0.09, "east": -78.31},
                    "refs": ["E1"],
                    "operator": "Metro",
                    "name": "Ecovia",
                },
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-fetch-status",
        )

        result = bridge["phase3_route_extract"](state, self.registry[STEP_P3_1_EXTRACT], 1, {})
        summary = dict(result.summary or {})
        fetch_attempts = [
            dict(row or {})
            for row in list(summary.get("extraction_attempt_records") or [])
            if str(dict(row or {}).get("action_or_template_used") or "") == "step_10_fetch"
        ]

        self.assertTrue(result.ok)
        self.assertEqual(str(summary.get("fetch_status_classification") or ""), "fetch_partial_after_valid_selection")
        self.assertTrue(bool(summary.get("strong_bundle_evidence")))
        self.assertTrue(fetch_attempts)
        self.assertEqual(str(fetch_attempts[-1].get("status") or ""), "success")
        self.assertEqual(
            str(dict(fetch_attempts[-1].get("extractor_diagnostics_summary") or {}).get("fetch_status_classification") or ""),
            "fetch_partial_after_valid_selection",
        )

    def test_p3_consistency_candidates_capture_false_empty_contradictions(self) -> None:
        route_id = str(uuid4())
        exec_summary = self._strong_p3_partial_fetch_summary(route_id=route_id)
        validator_result = {
            "status": "blocked",
            "summary": "Route extraction did not produce route_id.",
            "block_reason_code": BlockReasonCode.EXTRACTION_EMPTY.value,
            "evidence": {
                "route_id": route_id,
                "prior_stop_count": 0,
                "candidate_universe_summary": dict(exec_summary.get("candidate_universe_summary") or {}),
                "selection_summary": dict(exec_summary.get("selection_summary") or {}),
                "selected_relation_summary": dict(exec_summary.get("selected_relation_summary") or {}),
                "selected_relation_stop_prior_count": 42,
                "top_stop_prior_count": 42,
                "strong_bundle_evidence": True,
                "fetch_status_classification": "fetch_partial_after_valid_selection",
            },
        }

        candidates = _handoff_build_consistency_candidates(
            step_id=STEP_P3_1_EXTRACT,
            exec_summary=exec_summary,
            validator_result=validator_result,
            ai_snapshot={},
        )

        by_code = {
            str(dict(row or {}).get("code") or ""): dict(row or {})
            for row in candidates
        }
        self.assertIn("P3_STRONG_BUNDLE_BLOCKED_AS_EXTRACTION_EMPTY", by_code)
        self.assertIn("P3_PRIOR_STOP_COUNT_CONTRADICTION", by_code)
        self.assertIn("P3_ROUTE_ID_SUMMARY_MISMATCH", by_code)
        self.assertIn("P3_SELECTED_RELATION_FETCH_OBSERVABILITY_GAP", by_code)
        self.assertEqual(
            str(dict(by_code["P3_PRIOR_STOP_COUNT_CONTRADICTION"]).get("likely_implication") or ""),
            "validator_scoring_or_fetch_mapping_issue",
        )

    def test_p3_validator_summary_reflects_selected_route_state_when_route_id_exists(self) -> None:
        validators = build_phase_client_validator_bridge()
        state = RunSessionState(
            run_id="run-p3-validator-summary",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
        )
        exec_result = ExecutorResult(
            ok=True,
            summary=self._strong_p3_partial_fetch_summary(route_id="route-p3-summary"),
            artifacts=[],
        )

        out = validators["phase3_route_extract"](
            state,
            self.registry[STEP_P3_1_EXTRACT],
            1,
            exec_result,
        )

        self.assertIn("Route selected", str(out.summary or ""))
        self.assertNotIn("did not produce route_id", str(out.summary or "").lower())

    def test_p3_interpreter_prefers_diagnostics_when_strong_bundle_conflicts_with_empty_block(self) -> None:
        snapshot = {
            "phase": "phase3",
            "step_id": STEP_P3_1_EXTRACT,
            "validator": {
                "block_reason_code": BlockReasonCode.EXTRACTION_EMPTY.value,
                "summary": "Route extraction produced zero prior stops.",
                "evidence": {
                    "route_id": "route-p3-override",
                    "prior_stop_count": 0,
                    "prior_stop_evidence_count": 42,
                    "strong_bundle_evidence": True,
                    "fetch_status_classification": "fetch_partial_after_valid_selection",
                    "candidate_universe_summary": {
                        "candidate_universe_count": 8,
                        "top_stop_prior_count": 42,
                    },
                    "selection_summary": {
                        "selected_osm_relation_id": 654321,
                        "selection_confidence": 0.7633,
                        "selected_relation_stop_prior_count": 42,
                        "selection_reason_codes": ["stop_prior_signal_strong"],
                    },
                    "selected_relation_summary": {
                        "osm_relation_id": 654321,
                        "stop_prior_count": 42,
                    },
                },
            },
            "executor_summary": self._strong_p3_partial_fetch_summary(route_id="route-p3-override"),
            "phase3_route_extractor_packet": {
                "candidate_universe": {
                    "count_total": 8,
                    "selection_confidence": 0.7633,
                    "top_stop_prior_count": 42,
                },
                "selection": {
                    "selected_osm_relation_id": 654321,
                    "selected_relation_stop_prior_count": 42,
                    "selection_reason_codes": ["stop_prior_signal_strong"],
                    "strong_bundle_evidence": True,
                },
                "fetch": {
                    "selected_relation_fetched": True,
                    "raw_relation_available": True,
                    "prior_stop_count": 0,
                    "prior_stop_evidence_count": 42,
                    "fetch_status_classification": "fetch_partial_after_valid_selection",
                    "fetch_observability_gap": True,
                },
            },
            "ai_bot": {
                "extractor_status": {
                    "schema_version": "extractor_status_v1",
                    "phase": "phase3",
                    "completion_metrics": {
                        "candidate_universe_count": 8,
                        "selection_confidence": 0.7633,
                        "query_strategy": "bbox_first_broad",
                        "strong_bundle_evidence": True,
                        "selected_relation_present": True,
                        "selected_relation_stop_prior_count": 42,
                        "top_stop_prior_count": 42,
                        "fetch_status_classification": "fetch_partial_after_valid_selection",
                        "step20_available": False,
                    },
                    "scores": {"order_completion_quality_score": 0.83},
                },
                "extractor_help_needed": {
                    "needed": True,
                    "severity": "low",
                    "confidence": 0.6,
                    "reason_class": "partial_evidence_extractor_struggle",
                    "reasons": [{"code": "partial_evidence_extractor_struggle"}],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.81,
                        "completion_quality_score": 0.86,
                        "order_completion_quality_score": 0.83,
                        "attempt_count_total": 1,
                        "same_config_repeat_count": 0,
                        "step20_available": False,
                    },
                    "partial_evidence": True,
                },
            },
            "consistency_candidates": [
                {"code": "P3_STRONG_BUNDLE_BLOCKED_AS_EXTRACTION_EMPTY", "severity": "high"},
                {"code": "P3_PRIOR_STOP_COUNT_CONTRADICTION", "severity": "high"},
            ],
        }
        response = {
            "summary": "generic extractor route",
            "dominant_cause_class": "extractor_config",
            "confidence": 0.41,
            "secondary_causes": [],
            "evidence_consistency_checks": {"contradictions_found": True, "contradictions": [{"code": "P3_PRIOR_STOP_COUNT_CONTRADICTION"}]},
            "recommended_branch": "patch_extractor",
            "recommended_next_actions": ["patch extractor"],
            "patch_task_recommendation": {
                "should_create_patch_task": True,
                "patch_type": "extractor",
                "justification": "generic",
                "suggested_target": "either",
            },
            "operator_action_required": True,
            "approval_type_if_needed": None,
            "risk_notes": [],
        }

        patched, override_tag = AdvisoryChatGPTInterpreter._extractor_signal_routing_override(
            snapshot=snapshot,
            response=response,
            consistency_precheck={"contradictions_found": True, "contradictions": [{"code": "P3_PRIOR_STOP_COUNT_CONTRADICTION"}]},
        )

        self.assertEqual(override_tag, "extractor_override:p3_bundle_first_fetch_gap")
        self.assertEqual(str(patched.get("recommended_branch") or ""), "patch_diagnostics")
        self.assertEqual(str(patched.get("dominant_cause_class") or ""), "diagnostics_visibility")
        self.assertNotIn(str(patched.get("dominant_cause_class") or ""), {"extractor_config", "extractor_logic"})

    def test_p3_strong_bundle_without_step20_keeps_bundle_first_contract(self) -> None:
        state = RunSessionState(
            run_id="run-p3-bundle-first",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
        )
        summary = self._strong_p3_partial_fetch_summary(route_id="route-p3-bundle-first")
        validator_result = ValidatorResult(
            status="blocked",
            gate_passed=False,
            block_reason_code=BlockReasonCode.VALIDATOR_PAYLOAD_CONTRACT_MISMATCH,
            evidence=dict(summary.get("validator_payload") or {}),
        )

        extractor_status = _build_extractor_status(
            state=state,
            step=self.registry[STEP_P3_1_EXTRACT],
            attempt_no=1,
            exec_summary=summary,
            validator_result=validator_result,
            compare={},
            reorder={},
        )
        extractor_help = _build_extractor_help_needed(
            step=self.registry[STEP_P3_1_EXTRACT],
            extractor_status=extractor_status,
            validator_result=validator_result,
        )

        completion_metrics = dict(extractor_status.get("completion_metrics") or {})
        self.assertFalse(bool(completion_metrics.get("step20_available")))
        self.assertTrue(bool(completion_metrics.get("strong_bundle_evidence")))
        self.assertEqual(int(completion_metrics.get("prior_stop_evidence_count") or 0), 42)
        self.assertEqual(
            str(completion_metrics.get("fetch_status_classification") or ""),
            "fetch_partial_after_valid_selection",
        )
        self.assertNotEqual(str(extractor_help.get("reason_class") or ""), "partial_evidence_extractor_struggle")
        self.assertFalse(bool(extractor_help.get("needed")))

    def test_interpreter_snapshot_includes_normalized_hades_fields(self) -> None:
        class _StubAdvisoryService:
            def __init__(self) -> None:
                self.calls = []

            def run_task_detailed(self, *, endpoint_task, envelope):
                self.calls.append({"endpoint_task": endpoint_task, "envelope": dict(envelope or {})})
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {
                            "contradictions_found": False,
                            "contradictions": [],
                            "consistency_summary": "ok",
                        },
                        "meta": {"task": endpoint_task},
                    }
                return {
                    "response": {
                        "summary": "Step20 warning interpreted with normalized evidence.",
                        "dominant_cause_class": "detector_thresholds",
                        "confidence": 0.74,
                        "secondary_causes": [],
                        "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                        "recommended_branch": "no_patch_continue",
                        "recommended_next_actions": [
                            "Monitor warning trend for this corridor.",
                            "Continue pipeline under current policy profile.",
                        ],
                        "patch_task_recommendation": {
                            "should_create_patch_task": False,
                            "patch_type": None,
                            "justification": None,
                            "suggested_target": None,
                        },
                        "operator_action_required": False,
                        "approval_type_if_needed": None,
                        "risk_notes": [
                            {
                                "code": "WARNING_ONLY",
                                "severity": "low",
                                "message": "No blocker contradiction detected.",
                            }
                        ],
                    },
                    "meta": {
                        "task": endpoint_task,
                        "model": "stub-model",
                        "latency_ms": 11,
                        "token_usage": {"total_tokens": 31},
                        "schema_name": "hades_pipeline_interpreter_response.json",
                        "snapshot_id": "snap-normalized",
                        "source": "stub",
                        "prompt_package": {"task": endpoint_task, "snapshot": {"id": "normalized"}},
                    },
                }

        step = self.registry[STEP_P3_2_STEP20]
        state = RunSessionState(
            run_id="run-normalized",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_2_STEP20,
            trace_id="trace-normalized",
        )
        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_StubAdvisoryService())
        _ = interpreter(
            state,
            step,
            1,
            ExecutorResult(
                ok=True,
                summary={
                    "sequence_quality_score": 81.0,
                    "reorder_proposal": {"recommended": True, "confidence": "medium"},
                },
                artifacts=[],
            ),
            ValidatorResult(
                status="warning",
                gate_passed=True,
                passable_warning=True,
                warnings=["sequence_warning"],
                anomalies=[],
                evidence={"sequence_quality_score": 81.0, "unmatched_count": 0, "ambiguous_count": 0},
            ),
            AIBotSnapshot(
                scores={"quality": 0.81},
                metrics={"sequence_quality_score": 81.0},
                warnings=["ai_sequence_warning"],
                anomaly_flags=["threshold_noise_candidate"],
                proposals={
                    "reorder": {"recommended": True, "confidence": "high"},
                    "comparison": {
                        "ok": True,
                        "phase": "phase3",
                        "stage": "step_20_sequences",
                        "history_count": 4,
                        "baseline": {"quality_score": 0.79},
                        "latest": {"quality_score": 0.82, "sequence_quality_score": 81.0},
                        "latest_metrics": {"quality_score": 0.82, "sequence_quality_score": 81.0},
                        "deltas": {"quality_score": 0.03},
                        "regression_flags": [{"code": "sequence_warning_noise", "severity": "low"}],
                    },
                },
            ),
            "warning",
        )

        service_calls = list(getattr(interpreter._service, "calls", []))
        interp_calls = [c for c in service_calls if str(c.get("endpoint_task")) == "hades_pipeline_interpreter"]
        self.assertTrue(interp_calls)
        snap = dict((interp_calls[-1].get("envelope") or {}).get("snapshot") or {})
        self.assertEqual(snap.get("run_id"), "run-normalized")
        self.assertEqual(snap.get("trace_id"), "trace-normalized")
        self.assertEqual(snap.get("step_id"), STEP_P3_2_STEP20)
        self.assertIn("consistency_candidates", snap)
        self.assertIn("ai_bot", snap)

        ai_bot = dict(snap.get("ai_bot") or {})
        reorder = dict((dict(ai_bot.get("proposals") or {}).get("reorder") or {}))
        self.assertTrue(reorder.get("recommended"))
        self.assertAlmostEqual(float(reorder.get("confidence") or 0.0), 0.9, places=3)
        self.assertIn("comparison", ai_bot)
        self.assertAlmostEqual(float(ai_bot.get("quality_score") or 0.0), 0.81, places=3)
        self.assertTrue(
            bool((dict(snap.get("stage_normalization") or {})).get("telemetry_stage_matched"))
        )

    def test_interpreter_schema_failure_is_safe_and_logged(self) -> None:
        class _FailingAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                del endpoint_task, envelope
                raise RuntimeError("Model response failed schema/policy checks.")

        step = self.registry[STEP_P3_2_STEP20]
        state = RunSessionState(
            run_id="run-schema-fail",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_2_STEP20,
            trace_id="trace-schema-fail",
        )
        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_FailingAdvisoryService())
        snap = interpreter(
            state,
            step,
            1,
            ExecutorResult(ok=True, summary={}, artifacts=[]),
            ValidatorResult(status="warning", gate_passed=True, warnings=["warn"]),
            AIBotSnapshot(),
            "warning",
        )
        self.assertEqual(snap.task, "hades_pipeline_interpreter")
        self.assertIn("ChatGPT interpretation unavailable", str(snap.summary or ""))
        self.assertIn("chatgpt_schema_validation_failed", list(snap.risk_notes or []))
        self.assertIn("snapshot", dict(snap.prompt_package or {}))

    def test_real_advisory_failure_is_explicit_and_not_mock(self) -> None:
        from datamind_console.api_chatgpt.services.advisory_service import AdvisoryError

        class _FailingRealAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                snapshot = dict((dict(envelope or {}).get("snapshot") or {}))
                raise AdvisoryError(
                    "Model call failed.",
                    errors=["REAL_ADVISORY_PROVIDER_UNAVAILABLE"],
                    status_code=502,
                    detail={
                        "task": endpoint_task,
                        "status": "error",
                        "source": "real_advisory_error",
                        "requested_mode": "real_advisory",
                        "model": None,
                        "error_code": "REAL_ADVISORY_PROVIDER_UNAVAILABLE",
                        "error_summary": "OpenAI SDK not installed.",
                        "fallback_used": False,
                        "token_usage": None,
                        "snapshot_id": snapshot.get("snapshot_id"),
                        "schema_name": "hades_pipeline_interpreter_response.json",
                    },
                )

        step = self.registry[STEP_P3_2_STEP20]
        state = RunSessionState(
            run_id="run-real-advisory-fail",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_2_STEP20,
            trace_id="trace-real-advisory-fail",
        )
        interpreter = AdvisoryChatGPTInterpreter(
            advisory_service=_FailingRealAdvisoryService(),
            advisory_mode="real_advisory",
        )
        snap = interpreter(
            state,
            step,
            1,
            ExecutorResult(ok=True, summary={}, artifacts=[]),
            ValidatorResult(status="warning", gate_passed=True, warnings=["warn"]),
            AIBotSnapshot(),
            "warning",
        )
        self.assertEqual(snap.status, "error")
        self.assertEqual(snap.source, "real_advisory_error")
        self.assertEqual(snap.requested_mode, "real_advisory")
        self.assertEqual(snap.error_code, "REAL_ADVISORY_PROVIDER_UNAVAILABLE")
        self.assertEqual(snap.fallback_used, False)
        self.assertNotEqual(snap.source, "mock")
        self.assertNotEqual(snap.model, "mock-fixture-model")

    def test_chatgpt_snapshot_persists_task_model_latency_tokens(self) -> None:
        class _StubAdvisoryService:
            def __init__(self) -> None:
                self.calls = []

            def run_task_detailed(self, *, endpoint_task, envelope):
                self.calls.append({"endpoint_task": endpoint_task, "envelope": dict(envelope or {})})
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {
                            "contradictions_found": True,
                            "contradictions": [
                                {
                                    "code": "RESOLVE_COUNT_MISMATCH",
                                    "severity": "high",
                                    "details": "resolved_total != resolved_count",
                                    "likely_implication": "validator_mapping_bug",
                                }
                            ],
                            "consistency_summary": "Detected contradictions in resolve payload mapping.",
                        },
                        "meta": {
                            "task": endpoint_task,
                            "model": "stub-model",
                            "latency_ms": 3,
                            "token_usage": {"total_tokens": 10},
                            "schema_name": "hades_evidence_consistency_checker_response.json",
                            "snapshot_id": "snap-checker",
                            "source": "stub",
                            "prompt_package": {"task": endpoint_task, "snapshot": {"id": "c"}},
                        },
                    }
                return {
                    "response": {
                        "summary": "interpreted",
                        "prioritized_improvements": [{"action": "fix_blocker_first"}],
                        "risk_flags": ["advisory_only"],
                    },
                    "meta": {
                        "task": endpoint_task,
                        "model": "stub-model",
                        "latency_ms": 7,
                        "token_usage": {"total_tokens": 21},
                        "schema_name": "hades_pipeline_interpreter_response.json",
                        "snapshot_id": "snap-1",
                        "source": "stub",
                        "prompt_package": {"task": endpoint_task, "snapshot": {"id": "x"}},
                    },
                }

        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        stub_service = _StubAdvisoryService()
        engine.chatgpt_interpreter = AdvisoryChatGPTInterpreter(advisory_service=stub_service)

        def warning_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(
                status="warning",
                gate_passed=True,
                passable_warning=True,
                warnings=["sequence_quality_low"],
                anomalies=[],
                summary="warning",
            )

        engine.register_validator(self.registry[STEP_P3_2_STEP20].validator, warning_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        rec = run.step_execution_records[-1]
        snap = dict(rec.chatgpt_snapshot or {})
        self.assertEqual(snap.get("task"), "hades_pipeline_interpreter")
        self.assertEqual(snap.get("model"), "stub-model")
        self.assertEqual(snap.get("latency_ms"), 7)
        self.assertEqual(dict(snap.get("token_usage") or {}).get("total_tokens"), 21)
        self.assertIsInstance(snap.get("prompt_package"), dict)
        self.assertIn("consistency:contradictions_found", list(snap.get("priorities") or []))
        called_tasks = [str(c.get("endpoint_task")) for c in list(stub_service.calls or [])]
        self.assertIn("hades_evidence_consistency_checker", called_tasks)

    def test_engine_records_real_advisory_failure_without_mock_fallback(self) -> None:
        from datamind_console.api_chatgpt.services.advisory_service import AdvisoryError

        class _FailingRealAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                snapshot = dict((dict(envelope or {}).get("snapshot") or {}))
                raise AdvisoryError(
                    "Model call failed.",
                    errors=["REAL_ADVISORY_REQUEST_FAILED"],
                    status_code=502,
                    detail={
                        "task": endpoint_task,
                        "status": "error",
                        "source": "real_advisory_error",
                        "requested_mode": "real_advisory",
                        "model": None,
                        "error_code": "REAL_ADVISORY_REQUEST_FAILED",
                        "error_summary": "network timeout",
                        "fallback_used": False,
                        "token_usage": None,
                        "snapshot_id": snapshot.get("snapshot_id"),
                        "schema_name": "hades_pipeline_interpreter_response.json",
                    },
                )

        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        engine.chatgpt_interpreter = AdvisoryChatGPTInterpreter(
            advisory_service=_FailingRealAdvisoryService(),
            advisory_mode="real_advisory",
        )

        def warning_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(
                status="warning",
                gate_passed=True,
                passable_warning=True,
                warnings=["needs_interpretation"],
                anomalies=[],
                summary="warning",
            )

        engine.register_validator(self.registry[STEP_P3_2_STEP20].validator, warning_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        rec = run.step_execution_records[-1]
        snap = dict(rec.chatgpt_snapshot or {})
        self.assertEqual(snap.get("status"), "error")
        self.assertEqual(snap.get("source"), "real_advisory_error")
        self.assertEqual(snap.get("requested_mode"), "real_advisory")
        self.assertEqual(snap.get("error_code"), "REAL_ADVISORY_REQUEST_FAILED")
        self.assertEqual(snap.get("fallback_used"), False)
        self.assertNotEqual(snap.get("source"), "mock")
        self.assertNotEqual(snap.get("model"), "mock-fixture-model")
        warning_events = [
            e for e in list(run.events or [])
            if str(getattr(e, "event_type", "") or "") == "step_warning"
            and str((dict(getattr(e, "payload", {}) or {}).get("warning_type") or "")) == "chatgpt_advisory_failure"
        ]
        self.assertTrue(warning_events)
        self.assertEqual(
            str(dict(warning_events[-1].payload or {}).get("error_code") or ""),
            "REAL_ADVISORY_REQUEST_FAILED",
        )

    def test_stage4_p1_repeated_empty_high_help_prefers_patch_extractor(self) -> None:
        class _StubAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                del envelope
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {"contradictions_found": False, "contradictions": [], "consistency_summary": "ok"},
                        "meta": {"task": endpoint_task},
                    }
                return {
                    "response": {
                        "summary": "advisory interpretation generated",
                        "dominant_cause_class": "unknown",
                        "confidence": 0.42,
                        "secondary_causes": [],
                        "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                        "recommended_branch": "manual_review_required",
                        "recommended_next_actions": ["collect more evidence"],
                        "patch_task_recommendation": {
                            "should_create_patch_task": False,
                            "patch_type": None,
                            "justification": None,
                            "suggested_target": None,
                        },
                        "operator_action_required": False,
                        "approval_type_if_needed": None,
                        "risk_notes": [],
                    },
                    "meta": {"task": endpoint_task},
                }

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_StubAdvisoryService())
        step = self.registry[STEP_P1_1_EXTRACT]
        state = RunSessionState(
            run_id="run-stage4-p1-empty",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-stage4-p1-empty",
        )
        snap = interpreter(
            state,
            step,
            1,
            ExecutorResult(ok=False, summary={"candidate_count": 0}, artifacts=[]),
            ValidatorResult(status="blocked", gate_passed=False, block_reason_code=BlockReasonCode.EXTRACTION_EMPTY, evidence={"candidate_count": 0}),
            AIBotSnapshot(
                extractor_status={
                    "schema_version": "extractor_status_v1",
                    "phase": "phase1",
                    "completion_metrics": {"candidate_count": 0, "completion_quality_score": 0.22},
                    "scores": {"completion_quality_score": 0.22},
                    "warnings": ["repeated_empty_extraction"],
                },
                extractor_help_needed={
                    "needed": True,
                    "severity": "high",
                    "confidence": 0.86,
                    "reason_class": "repeated_empty_extraction",
                    "reasons": [{"code": "repeated_empty_extraction", "value": 3, "threshold": 2, "message": "Repeated empty extraction attempts detected."}],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.21,
                        "completion_quality_score": 0.22,
                        "order_completion_quality_score": None,
                        "attempt_count_total": 3,
                        "same_config_repeat_count": 2,
                        "retry_diversity_count": 1,
                        "fallback_rescue_success": False,
                        "step20_available": False,
                    },
                    "recommended_escalation": "interpreter_patch_evaluation",
                    "partial_evidence": False,
                    "notes": [],
                },
            ),
            "blocked",
        )
        branch = self._priority_value(snap.priorities, "branch")
        cause = self._priority_value(snap.priorities, "cause")
        self.assertEqual(branch, "patch_extractor")
        self.assertIn(cause, {"extractor_logic", "extractor_config"})
        self.assertIn("extractor_help_needed", str(snap.summary or ""))

    def test_stage4_p1_low_diversity_usable_completion_prefers_tuning_retry(self) -> None:
        class _StubAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                del envelope
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {"contradictions_found": False, "contradictions": [], "consistency_summary": "ok"},
                        "meta": {"task": endpoint_task},
                    }
                return {
                    "response": {
                        "summary": "advisory interpretation generated",
                        "dominant_cause_class": "unknown",
                        "confidence": 0.40,
                        "secondary_causes": [],
                        "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                        "recommended_branch": "manual_review_required",
                        "recommended_next_actions": ["collect more evidence"],
                        "patch_task_recommendation": {
                            "should_create_patch_task": False,
                            "patch_type": None,
                            "justification": None,
                            "suggested_target": None,
                        },
                        "operator_action_required": False,
                        "approval_type_if_needed": None,
                        "risk_notes": [],
                    },
                    "meta": {"task": endpoint_task},
                }

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_StubAdvisoryService())
        step = self.registry[STEP_P1_1_EXTRACT]
        state = RunSessionState(
            run_id="run-stage4-p1-diversity",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-stage4-p1-diversity",
        )
        snap = interpreter(
            state,
            step,
            1,
            ExecutorResult(ok=True, summary={"candidate_count": 11}, artifacts=[]),
            ValidatorResult(status="pass", gate_passed=True, evidence={"candidate_count": 11}),
            AIBotSnapshot(
                extractor_status={
                    "schema_version": "extractor_status_v1",
                    "phase": "phase1",
                    "scores": {"completion_quality_score": 0.71},
                    "warnings": ["retry_not_diversified", "same_config_retry_loop"],
                },
                extractor_help_needed={
                    "needed": True,
                    "severity": "medium",
                    "confidence": 0.69,
                    "reason_class": "retry_not_diversified",
                    "reasons": [{"code": "retry_not_diversified", "value": 3, "threshold": 2, "message": "Retry diversity is low relative to repeated attempts."}],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.49,
                        "completion_quality_score": 0.71,
                        "order_completion_quality_score": None,
                        "attempt_count_total": 4,
                        "same_config_repeat_count": 3,
                        "retry_diversity_count": 1,
                        "fallback_rescue_success": True,
                        "step20_available": False,
                    },
                    "recommended_escalation": "interpreter_patch_evaluation",
                    "partial_evidence": False,
                    "notes": [],
                },
            ),
            "warning",
        )
        self.assertEqual(self._priority_value(snap.priorities, "branch"), "tuning_retry")
        self.assertEqual(self._priority_value(snap.priorities, "cause"), "extractor_config")

    def test_stage4_p1_contradiction_with_material_extractor_degradation_prefers_patch_extractor(self) -> None:
        class _StubAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                del envelope
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {
                            "contradictions_found": True,
                            "contradictions": [{"code": "RESOLVE_COUNT_CONTRACT_MISMATCH", "details": "resolved_total != resolved_count"}],
                            "consistency_summary": "mismatch",
                        },
                        "meta": {"task": endpoint_task},
                    }
                return {
                    "response": {
                        "summary": "advisory interpretation generated",
                        "dominant_cause_class": "extractor_logic",
                        "confidence": 0.70,
                        "secondary_causes": [],
                        "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                        "recommended_branch": "patch_extractor",
                        "recommended_next_actions": ["patch extractor now"],
                        "patch_task_recommendation": {
                            "should_create_patch_task": True,
                            "patch_type": "extractor",
                            "justification": "default",
                            "suggested_target": "either",
                        },
                        "operator_action_required": True,
                        "approval_type_if_needed": None,
                        "risk_notes": [],
                    },
                    "meta": {"task": endpoint_task},
                }

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_StubAdvisoryService())
        step = self.registry[STEP_P1_2_CHAIN]
        state = RunSessionState(
            run_id="run-stage4-p1-contradiction",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_2_CHAIN,
            trace_id="trace-stage4-p1-contradiction",
        )
        snap = interpreter(
            state,
            step,
            1,
            ExecutorResult(ok=False, summary={"resolve": {"resolved_total": 1652}}, artifacts=[]),
            ValidatorResult(status="blocked", gate_passed=False, block_reason_code=BlockReasonCode.RESOLVE_ZERO_RESULTS, evidence={"resolved_count": 0}),
            AIBotSnapshot(
                extractor_status={"schema_version": "extractor_status_v1", "phase": "phase1"},
                extractor_help_needed={
                    "needed": True,
                    "severity": "high",
                    "confidence": 0.82,
                    "reason_class": "repeated_empty_extraction",
                    "reasons": [{"code": "repeated_empty_extraction", "value": 3, "threshold": 2, "message": "Repeated empty extraction attempts detected."}],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.22,
                        "completion_quality_score": 0.20,
                        "order_completion_quality_score": None,
                        "attempt_count_total": 3,
                        "same_config_repeat_count": 2,
                        "retry_diversity_count": 1,
                        "fallback_rescue_success": False,
                        "step20_available": False,
                    },
                    "recommended_escalation": "interpreter_patch_evaluation",
                    "partial_evidence": False,
                    "notes": [],
                },
            ),
            "blocked",
        )
        branch = self._priority_value(snap.priorities, "branch")
        self.assertEqual(branch, "patch_extractor")
        self.assertIn("did not override the extractor patch path", str(snap.summary or ""))

    def test_stage4_p1_contradictions_plus_empty_extraction_prefers_patch_extractor(self) -> None:
        snapshot = {
            "phase": "phase1",
            "step_id": STEP_P1_1_EXTRACT,
            "validator": {
                "block_reason_code": BlockReasonCode.EXTRACTION_EMPTY.value,
                "evidence": {"candidate_count": 0},
            },
            "executor_summary": {"candidate_count": 0},
            "ai_bot": {
                "extractor_status": {
                    "schema_version": "extractor_status_v1",
                    "phase": "phase1",
                    "completion_metrics": {"candidate_count": 0, "non_empty_extraction": False},
                    "scores": {"completion_quality_score": 0.24},
                },
                "extractor_help_needed": {
                    "needed": True,
                    "severity": "high",
                    "confidence": 0.84,
                    "reason_class": "repeated_empty_extraction",
                    "reasons": [
                        {"code": "repeated_empty_extraction", "value": 2, "threshold": 2, "message": "Repeated empty extraction attempts detected."}
                    ],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.32,
                        "completion_quality_score": 0.24,
                        "attempt_count_total": 2,
                    },
                    "partial_evidence": False,
                },
            },
            "consistency_candidates": [{"code": "EXTRACT_CONTRACT_MISMATCH", "severity": "high"}],
        }
        response = {
            "summary": "generic contradiction path",
            "dominant_cause_class": "detector_thresholds",
            "confidence": 0.5,
            "secondary_causes": [],
            "evidence_consistency_checks": {"contradictions_found": True, "contradictions": [{"code": "EXTRACT_CONTRACT_MISMATCH"}]},
            "recommended_branch": "patch_diagnostics",
            "recommended_next_actions": ["inspect payload mismatch"],
            "patch_task_recommendation": {
                "should_create_patch_task": True,
                "patch_type": "diagnostics",
                "justification": "default contradiction route",
                "suggested_target": "either",
            },
            "operator_action_required": True,
            "approval_type_if_needed": None,
            "risk_notes": [],
        }
        patched, override_tag = AdvisoryChatGPTInterpreter._extractor_signal_routing_override(
            snapshot=snapshot,
            response=response,
            consistency_precheck={"contradictions_found": True, "contradictions": [{"code": "EXTRACT_CONTRACT_MISMATCH"}]},
        )
        self.assertEqual(override_tag, "extractor_override:p1_patch_extractor_primary")
        self.assertEqual(patched.get("recommended_branch"), "patch_extractor")
        self.assertEqual(dict(patched.get("patch_task_recommendation") or {}).get("patch_type"), "extractor")

    def test_stage4_p1_contradictions_with_mild_weakness_prefers_patch_diagnostics(self) -> None:
        snapshot = {
            "phase": "phase1",
            "step_id": STEP_P1_1_EXTRACT,
            "validator": {
                "block_reason_code": None,
                "evidence": {"candidate_count": 4},
            },
            "executor_summary": {"candidate_count": 4},
            "ai_bot": {
                "extractor_status": {
                    "schema_version": "extractor_status_v1",
                    "phase": "phase1",
                    "completion_metrics": {"candidate_count": 4, "non_empty_extraction": True},
                    "scores": {"completion_quality_score": 0.58},
                },
                "extractor_help_needed": {
                    "needed": False,
                    "severity": "low",
                    "confidence": 0.48,
                    "reason_class": "none",
                    "reasons": [],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.61,
                        "completion_quality_score": 0.58,
                        "attempt_count_total": 1,
                    },
                    "partial_evidence": True,
                },
            },
            "consistency_candidates": [{"code": "EXTRACT_SCALE_MISMATCH", "severity": "medium"}],
        }
        response = {
            "summary": "generic contradiction path",
            "dominant_cause_class": "unknown",
            "confidence": 0.42,
            "secondary_causes": [],
            "evidence_consistency_checks": {"contradictions_found": True, "contradictions": [{"code": "EXTRACT_SCALE_MISMATCH"}]},
            "recommended_branch": "manual_review_required",
            "recommended_next_actions": ["collect more evidence"],
            "patch_task_recommendation": {
                "should_create_patch_task": False,
                "patch_type": None,
                "justification": None,
                "suggested_target": None,
            },
            "operator_action_required": False,
            "approval_type_if_needed": None,
            "risk_notes": [],
        }
        patched, override_tag = AdvisoryChatGPTInterpreter._extractor_signal_routing_override(
            snapshot=snapshot,
            response=response,
            consistency_precheck={"contradictions_found": True, "contradictions": [{"code": "EXTRACT_SCALE_MISMATCH"}]},
        )
        self.assertEqual(override_tag, "extractor_override:contradictions")
        self.assertEqual(patched.get("recommended_branch"), "patch_diagnostics")
        self.assertEqual(dict(patched.get("patch_task_recommendation") or {}).get("patch_type"), "diagnostics")

    def test_stage4_p1_low_quality_with_persistent_retries_prefers_patch_extractor(self) -> None:
        snapshot = {
            "phase": "phase1",
            "step_id": STEP_P1_1_EXTRACT,
            "validator": {"block_reason_code": None, "evidence": {"candidate_count": 2}},
            "executor_summary": {"candidate_count": 2},
            "ai_bot": {
                "extractor_status": {
                    "schema_version": "extractor_status_v1",
                    "phase": "phase1",
                    "completion_metrics": {"candidate_count": 2, "non_empty_extraction": True},
                    "scores": {"completion_quality_score": 0.48},
                },
                "extractor_help_needed": {
                    "needed": True,
                    "severity": "medium",
                    "confidence": 0.73,
                    "reason_class": "low_completion_quality",
                    "reasons": [{"code": "low_completion_quality", "value": 0.48, "threshold": 0.50, "message": "Completion quality is weak."}],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.58,
                        "completion_quality_score": 0.48,
                        "attempt_count_total": 3,
                        "same_config_repeat_count": 2,
                    },
                    "partial_evidence": False,
                },
            },
            "consistency_candidates": [],
        }
        response = {
            "summary": "generic",
            "dominant_cause_class": "unknown",
            "confidence": 0.40,
            "secondary_causes": [],
            "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
            "recommended_branch": "manual_review_required",
            "recommended_next_actions": ["collect more evidence"],
            "patch_task_recommendation": {
                "should_create_patch_task": False,
                "patch_type": None,
                "justification": None,
                "suggested_target": None,
            },
            "operator_action_required": False,
            "approval_type_if_needed": None,
            "risk_notes": [],
        }
        patched, override_tag = AdvisoryChatGPTInterpreter._extractor_signal_routing_override(
            snapshot=snapshot,
            response=response,
            consistency_precheck={"contradictions_found": False, "contradictions": []},
        )
        self.assertEqual(override_tag, "extractor_override:p1_patch_extractor_primary")
        self.assertEqual(patched.get("recommended_branch"), "patch_extractor")

    def test_stage4_p1_low_quality_without_persistence_prefers_tuning_retry(self) -> None:
        snapshot = {
            "phase": "phase1",
            "step_id": STEP_P1_1_EXTRACT,
            "validator": {"block_reason_code": None, "evidence": {"candidate_count": 3}},
            "executor_summary": {"candidate_count": 3},
            "ai_bot": {
                "extractor_status": {
                    "schema_version": "extractor_status_v1",
                    "phase": "phase1",
                    "completion_metrics": {"candidate_count": 3, "non_empty_extraction": True},
                    "scores": {"completion_quality_score": 0.48},
                },
                "extractor_help_needed": {
                    "needed": True,
                    "severity": "medium",
                    "confidence": 0.58,
                    "reason_class": "low_efficiency_health",
                    "reasons": [{"code": "low_efficiency_health", "value": 0.58, "threshold": 0.60, "message": "Efficiency health is weak."}],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.58,
                        "completion_quality_score": 0.48,
                        "attempt_count_total": 1,
                        "same_config_repeat_count": 0,
                    },
                    "partial_evidence": False,
                },
            },
            "consistency_candidates": [],
        }
        response = {
            "summary": "generic",
            "dominant_cause_class": "unknown",
            "confidence": 0.40,
            "secondary_causes": [],
            "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
            "recommended_branch": "manual_review_required",
            "recommended_next_actions": ["collect more evidence"],
            "patch_task_recommendation": {
                "should_create_patch_task": False,
                "patch_type": None,
                "justification": None,
                "suggested_target": None,
            },
            "operator_action_required": False,
            "approval_type_if_needed": None,
            "risk_notes": [],
        }
        patched, override_tag = AdvisoryChatGPTInterpreter._extractor_signal_routing_override(
            snapshot=snapshot,
            response=response,
            consistency_precheck={"contradictions_found": False, "contradictions": []},
        )
        self.assertEqual(override_tag, "extractor_override:p1_tuning_retry_moderate")
        self.assertEqual(patched.get("recommended_branch"), "tuning_retry")

    def test_stage4_spatial_contradiction_does_not_suppress_patch_extractor(self) -> None:
        snapshot = {
            "phase": "phase1",
            "step_id": STEP_P1_1_EXTRACT,
            "validator": {
                "block_reason_code": BlockReasonCode.EXTRACTION_EMPTY.value,
                "evidence": {"candidate_count": 0},
            },
            "executor_summary": {"candidate_count": 0},
            "ai_bot": {
                "extractor_status": {
                    "schema_version": "extractor_status_v1",
                    "phase": "phase1",
                    "completion_metrics": {"candidate_count": 0, "non_empty_extraction": False},
                    "scores": {
                        "completion_quality_score": 0.22,
                        "extractor_efficiency_health_score": 0.31,
                    },
                    "warnings": [
                        "spatial_interpretation_failed",
                        "spatial_plan_reused_without_change",
                    ],
                    "spatial_metrics": {
                        "target_option_received": True,
                        "target_option_text": "madeup nowhere",
                        "spatial_interpretation_status": "fallback_default_bbox",
                        "bbox_candidate": None,
                        "runtime_bbox_used": {
                            "south": -0.38,
                            "west": -78.6,
                            "north": -0.02,
                            "east": -78.35,
                        },
                        "runtime_spatial_strategy_used": "default_bbox_fallback",
                        "retry_changed_spatial_plan": False,
                        "same_spatial_plan_retry_count": 1,
                        "spatial_interpretation_failure_reason": "target_intent_unresolved",
                        "target_intent_ignored": True,
                    },
                },
                "extractor_help_needed": {
                    "needed": True,
                    "severity": "high",
                    "confidence": 0.84,
                    "reason_class": "spatial_interpretation_failed",
                    "reasons": [
                        {
                            "code": "spatial_interpretation_failed",
                            "value": "fallback_default_bbox",
                            "threshold": 2,
                            "message": "Spatial interpretation failed.",
                        },
                        {
                            "code": "spatial_plan_reused_without_change",
                            "value": 2,
                            "threshold": 2,
                            "message": "Spatial plan was retried without reinterpretation.",
                        },
                    ],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.31,
                        "completion_quality_score": 0.22,
                        "attempt_count_total": 2,
                        "same_config_repeat_count": 1,
                        "retry_diversity_count": 1,
                        "target_option_received": True,
                        "spatial_interpretation_status": "fallback_default_bbox",
                        "same_spatial_plan_retry_count": 1,
                        "retry_changed_spatial_plan": False,
                    },
                    "partial_evidence": False,
                },
            },
            "consistency_candidates": [{"code": "EXTRACT_PAYLOAD_MISMATCH", "severity": "medium"}],
        }
        response = {
            "summary": "generic contradiction path",
            "dominant_cause_class": "detector_thresholds",
            "confidence": 0.5,
            "secondary_causes": [],
            "evidence_consistency_checks": {
                "contradictions_found": True,
                "contradictions": [{"code": "EXTRACT_PAYLOAD_MISMATCH"}],
            },
            "recommended_branch": "patch_diagnostics",
            "recommended_next_actions": ["inspect payload mismatch"],
            "patch_task_recommendation": {
                "should_create_patch_task": True,
                "patch_type": "diagnostics",
                "justification": "default contradiction route",
                "suggested_target": "either",
            },
            "operator_action_required": True,
            "approval_type_if_needed": None,
            "risk_notes": [],
        }
        patched, override_tag = AdvisoryChatGPTInterpreter._extractor_signal_routing_override(
            snapshot=snapshot,
            response=response,
            consistency_precheck={"contradictions_found": True, "contradictions": [{"code": "EXTRACT_PAYLOAD_MISMATCH"}]},
        )
        self.assertEqual(override_tag, "extractor_override:p1_patch_extractor_primary")
        self.assertEqual(patched.get("recommended_branch"), "patch_extractor")
        self.assertEqual(dict(patched.get("patch_task_recommendation") or {}).get("patch_type"), "extractor")
        self.assertIn(
            "target intent was not converted into a usable bbox/sector plan",
            " ".join(list(patched.get("recommended_next_actions") or [])),
        )

    def test_stage4_p3_efficient_but_step20_poor_routes_not_blind_patch_extractor(self) -> None:
        class _StubAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                del envelope
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {"contradictions_found": False, "contradictions": [], "consistency_summary": "ok"},
                        "meta": {"task": endpoint_task},
                    }
                return {
                    "response": {
                        "summary": "advisory interpretation generated",
                        "dominant_cause_class": "unknown",
                        "confidence": 0.40,
                        "secondary_causes": [],
                        "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                        "recommended_branch": "manual_review_required",
                        "recommended_next_actions": ["collect more evidence"],
                        "patch_task_recommendation": {
                            "should_create_patch_task": False,
                            "patch_type": None,
                            "justification": None,
                            "suggested_target": None,
                        },
                        "operator_action_required": False,
                        "approval_type_if_needed": None,
                        "risk_notes": [],
                    },
                    "meta": {"task": endpoint_task},
                }

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_StubAdvisoryService())
        step = self.registry[STEP_P3_1_EXTRACT]
        state = RunSessionState(
            run_id="run-stage4-p3-step20-poor",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-stage4-p3-step20-poor",
        )
        snap = interpreter(
            state,
            step,
            1,
            ExecutorResult(ok=True, summary={"extractor_diagnostics": {"candidate_count": 8}}, artifacts=[]),
            ValidatorResult(status="warning", gate_passed=True, evidence={"unmatched_count": 4, "ambiguous_count": 1}),
            AIBotSnapshot(
                extractor_status={
                    "schema_version": "extractor_status_v1",
                    "phase": "phase3",
                    "completion_metrics": {"step20_available": True, "unmatched_count": 4, "ambiguous_count": 1, "step20_gate_passed": False},
                    "warnings": ["extraction_success_but_step20_poor"],
                    "scores": {"order_completion_quality_score": 0.34},
                },
                extractor_help_needed={
                    "needed": True,
                    "severity": "high",
                    "confidence": 0.80,
                    "reason_class": "low_order_completion_quality",
                    "reasons": [{"code": "extraction_success_but_step20_poor", "value": False, "threshold": False, "message": "Extraction succeeded but Step20 quality is poor."}],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.78,
                        "completion_quality_score": None,
                        "order_completion_quality_score": 0.34,
                        "attempt_count_total": 2,
                        "same_config_repeat_count": 0,
                        "retry_diversity_count": 2,
                        "fallback_rescue_success": True,
                        "step20_available": True,
                        "step20_gate_passed": False,
                    },
                    "recommended_escalation": "interpreter_patch_evaluation",
                    "partial_evidence": False,
                    "notes": [],
                },
            ),
            "warning",
        )
        branch = self._priority_value(snap.priorities, "branch")
        self.assertNotEqual(branch, "patch_extractor")
        self.assertIn(branch, {"phase1_new_nodes", "patch_detector_scoring", "patch_diagnostics"})

    def test_stage4_p3_repeated_weak_and_poor_completion_can_choose_patch_extractor(self) -> None:
        class _StubAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                del envelope
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {"contradictions_found": False, "contradictions": [], "consistency_summary": "ok"},
                        "meta": {"task": endpoint_task},
                    }
                return {
                    "response": {
                        "summary": "advisory interpretation generated",
                        "dominant_cause_class": "unknown",
                        "confidence": 0.40,
                        "secondary_causes": [],
                        "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                        "recommended_branch": "manual_review_required",
                        "recommended_next_actions": ["collect more evidence"],
                        "patch_task_recommendation": {
                            "should_create_patch_task": False,
                            "patch_type": None,
                            "justification": None,
                            "suggested_target": None,
                        },
                        "operator_action_required": False,
                        "approval_type_if_needed": None,
                        "risk_notes": [],
                    },
                    "meta": {"task": endpoint_task},
                }

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_StubAdvisoryService())
        step = self.registry[STEP_P3_1_EXTRACT]
        state = RunSessionState(
            run_id="run-stage4-p3-weak",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-stage4-p3-weak",
        )
        snap = interpreter(
            state,
            step,
            1,
            ExecutorResult(ok=True, summary={"extractor_diagnostics": {"candidate_count": 1}}, artifacts=[]),
            ValidatorResult(status="warning", gate_passed=True, evidence={"unmatched_count": 0, "ambiguous_count": 0}),
            AIBotSnapshot(
                extractor_status={
                    "schema_version": "extractor_status_v1",
                    "phase": "phase3",
                    "completion_metrics": {"step20_available": True, "unmatched_count": 0, "ambiguous_count": 0, "step20_gate_passed": False},
                    "warnings": ["retry_not_diversified", "fallback_rescue_failed"],
                    "scores": {"order_completion_quality_score": 0.31},
                },
                extractor_help_needed={
                    "needed": True,
                    "severity": "high",
                    "confidence": 0.83,
                    "reason_class": "low_order_completion_quality",
                    "reasons": [
                        {"code": "low_order_completion_quality", "value": 0.31, "threshold": 0.40, "message": "Order completion quality is critically low."},
                        {"code": "retry_not_diversified", "value": 3, "threshold": 2, "message": "Retries are not diversified."},
                    ],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.29,
                        "completion_quality_score": None,
                        "order_completion_quality_score": 0.31,
                        "attempt_count_total": 4,
                        "same_config_repeat_count": 3,
                        "retry_diversity_count": 1,
                        "fallback_rescue_success": False,
                        "step20_available": True,
                        "step20_gate_passed": False,
                    },
                    "recommended_escalation": "interpreter_patch_evaluation",
                    "partial_evidence": False,
                    "notes": [],
                },
            ),
            "warning",
        )
        self.assertEqual(self._priority_value(snap.priorities, "branch"), "patch_extractor")
        self.assertTrue(any(str(x).startswith("patch:extractor:") for x in list(snap.priorities or [])))

    def test_stage4_p3_candidate_universe_weak_prefers_patch_extractor(self) -> None:
        class _StubAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                del envelope
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {"contradictions_found": True, "contradictions": [{"code": "P3_SELECTION_MISMATCH"}], "consistency_summary": "mismatch"},
                        "meta": {"task": endpoint_task},
                    }
                return {
                    "response": {
                        "summary": "generic route mismatch interpretation",
                        "dominant_cause_class": "detector_thresholds",
                        "confidence": 0.44,
                        "secondary_causes": [],
                        "evidence_consistency_checks": {"contradictions_found": True, "contradictions": [{"code": "P3_SELECTION_MISMATCH"}]},
                        "recommended_branch": "patch_diagnostics",
                        "recommended_next_actions": ["inspect payload mismatch"],
                        "patch_task_recommendation": {
                            "should_create_patch_task": True,
                            "patch_type": "diagnostics",
                            "justification": "default contradiction route",
                            "suggested_target": "either",
                        },
                        "operator_action_required": False,
                        "approval_type_if_needed": None,
                        "risk_notes": [],
                    },
                    "meta": {"task": endpoint_task},
                }

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_StubAdvisoryService())
        step = self.registry[STEP_P3_1_EXTRACT]
        state = RunSessionState(
            run_id="run-stage4-p3-universe-weak",
            pipeline_scope={
                "phases": ["phase3"],
                "phase3": {
                    "bbox": {"south": -0.3, "west": -78.5, "north": -0.2, "east": -78.4},
                    "refs": ["E1"],
                    "operator": "Metro",
                    "name": "Ecovia",
                    "query_strategy": "metadata_filtered",
                },
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-stage4-p3-universe-weak",
        )
        snap = interpreter(
            state,
            step,
            3,
            ExecutorResult(
                ok=True,
                summary={
                    "route_id": str(uuid4()),
                    "prior_stop_count": 1,
                    "query_strategy": "metadata_filtered",
                    "candidate_universe_summary": {
                        "candidate_universe_count": 3,
                        "candidate_scored_count": 3,
                        "candidate_fetch_evaluated_count": 2,
                        "query_strategy": "metadata_filtered",
                        "hard_filters_applied": ["refs", "operator", "name"],
                        "soft_signals_used": ["refs", "operator", "name"],
                    },
                    "selection_summary": {
                        "selection_status": "provisional_selected",
                        "selected_osm_relation_id": 321,
                        "selected_rank": 1,
                        "selected_score": 12.1,
                        "selection_confidence": 0.31,
                        "score_gap_top2": 0.2,
                        "selected_relation_stop_prior_count": 1,
                        "selection_reason_codes": ["soft_ref_match"],
                    },
                    "selected_relation_summary": {
                        "osm_relation_id": 321,
                        "selection_rank": 1,
                        "selection_confidence": 0.31,
                        "score": 12.1,
                        "stop_prior_count": 1,
                    },
                    "extractor_diagnostics": {
                        "candidate_count": 3,
                        "signal_strength": "low",
                        "quality_flags": ["discover_candidate_pool_single"],
                    },
                },
                artifacts=[],
            ),
            ValidatorResult(status="warning", gate_passed=True, evidence={"prior_stop_count": 1}),
            AIBotSnapshot(
                extractor_status={
                    "schema_version": "extractor_status_v1",
                    "phase": "phase3",
                    "completion_metrics": {
                        "candidate_universe_count": 3,
                        "selection_confidence": 0.31,
                        "query_strategy": "metadata_filtered",
                        "hard_filters_applied": ["refs", "operator", "name"],
                        "soft_signals_used": ["refs", "operator", "name"],
                        "step20_available": False,
                    },
                    "warnings": ["candidate_universe_too_small", "selection_confidence_low", "hard_filter_overreach"],
                    "scores": {
                        "extractor_efficiency_health_score": 0.42,
                        "order_completion_quality_score": 0.33,
                    },
                },
                extractor_help_needed={
                    "needed": True,
                    "severity": "high",
                    "confidence": 0.82,
                    "reason_class": "candidate_universe_too_small",
                    "reasons": [
                        {"code": "candidate_universe_too_small", "value": 3, "threshold": 5, "message": "Candidate universe is too small."},
                        {"code": "hard_filter_overreach", "value": 3, "threshold": 5, "message": "Hard metadata filters collapsed the discovery pool."},
                    ],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.42,
                        "candidate_universe_count": 3,
                        "selection_confidence": 0.31,
                        "step20_available": False,
                        "attempt_count_total": 3,
                        "same_config_repeat_count": 2,
                        "retry_diversity_count": 1,
                    },
                    "recommended_escalation": "interpreter_patch_evaluation",
                    "partial_evidence": False,
                    "notes": [],
                },
            ),
            "warning",
        )
        self.assertEqual(self._priority_value(snap.priorities, "branch"), "patch_extractor")
        self.assertTrue(any(str(x).startswith("patch:extractor:") for x in list(snap.priorities or [])))

    def test_stage4_p3_bad_geography_input_prefers_patch_extractor(self) -> None:
        snapshot = {
            "phase": "phase3",
            "step_id": STEP_P3_1_EXTRACT,
            "validator": {
                "block_reason_code": BlockReasonCode.EXTRACTION_EMPTY.value,
                "evidence": {"candidate_count": 0},
            },
            "executor_summary": {
                "candidate_universe_summary": {"candidate_universe_count": 2, "query_strategy": "bbox_first_broad"},
                "selection_summary": {"selection_confidence": 0.18},
            },
            "phase3_route_extractor_packet": {
                "scope": {
                    "query_strategy": "bbox_first_broad",
                    "original_geographic_input": "madeup corridor near terminal",
                    "geographic_interpretation_status": "fallback_default_bbox",
                    "fallback_used": True,
                    "fallback_reason": "phase3_scope_bbox_missing",
                    "target_option_received": True,
                },
                "candidate_universe": {
                    "count_total": 2,
                    "hard_filters_applied": [],
                    "soft_signals_used": ["name"],
                    "selection_confidence": 0.18,
                },
            },
            "ai_bot": {
                "extractor_status": {
                    "schema_version": "extractor_status_v1",
                    "phase": "phase3",
                    "completion_metrics": {
                        "candidate_universe_count": 2,
                        "selection_confidence": 0.18,
                        "query_strategy": "bbox_first_broad",
                        "attempt_count_total": 3,
                        "same_config_repeat_count": 2,
                        "retry_diversity_count": 1,
                        "step20_available": False,
                    },
                    "scores": {
                        "extractor_efficiency_health_score": 0.24,
                        "order_completion_quality_score": 0.22,
                    },
                    "warnings": [
                        "spatial_interpretation_failed",
                        "target_intent_ignored",
                        "candidate_universe_too_small",
                    ],
                    "spatial_metrics": {
                        "target_option_received": True,
                        "target_option_text": "madeup corridor near terminal",
                        "spatial_interpretation_status": "fallback_default_bbox",
                        "runtime_spatial_strategy_used": "default_bbox_fallback",
                        "retry_changed_spatial_plan": False,
                        "same_spatial_plan_retry_count": 1,
                        "spatial_interpretation_failure_reason": "target_intent_unresolved",
                        "target_intent_ignored": True,
                    },
                },
                "extractor_help_needed": {
                    "needed": True,
                    "severity": "high",
                    "confidence": 0.86,
                    "reason_class": "spatial_interpretation_failed",
                    "reasons": [
                        {"code": "spatial_interpretation_failed", "value": "fallback_default_bbox"},
                        {"code": "candidate_universe_too_small", "value": 2, "threshold": 5},
                    ],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.24,
                        "candidate_universe_count": 2,
                        "selection_confidence": 0.18,
                        "target_option_received": True,
                        "spatial_interpretation_status": "fallback_default_bbox",
                        "same_spatial_plan_retry_count": 1,
                        "retry_changed_spatial_plan": False,
                        "attempt_count_total": 3,
                        "same_config_repeat_count": 2,
                        "retry_diversity_count": 1,
                        "step20_available": False,
                    },
                    "partial_evidence": False,
                },
            },
            "consistency_candidates": [{"code": "P3_GEO_PAYLOAD_MISMATCH", "severity": "medium"}],
        }
        response = {
            "summary": "generic contradiction route",
            "dominant_cause_class": "detector_thresholds",
            "confidence": 0.48,
            "secondary_causes": [],
            "evidence_consistency_checks": {
                "contradictions_found": True,
                "contradictions": [{"code": "P3_GEO_PAYLOAD_MISMATCH"}],
            },
            "recommended_branch": "patch_diagnostics",
            "recommended_next_actions": ["inspect payload mismatch"],
            "patch_task_recommendation": {
                "should_create_patch_task": True,
                "patch_type": "diagnostics",
                "justification": "default contradiction route",
                "suggested_target": "either",
            },
            "operator_action_required": False,
            "approval_type_if_needed": None,
            "risk_notes": [],
        }
        patched, override_tag = AdvisoryChatGPTInterpreter._extractor_signal_routing_override(
            snapshot=snapshot,
            response=response,
            consistency_precheck={"contradictions_found": True, "contradictions": [{"code": "P3_GEO_PAYLOAD_MISMATCH"}]},
        )
        self.assertEqual(override_tag, "extractor_override:p3_pre_step20_patch")
        self.assertEqual(patched.get("recommended_branch"), "patch_extractor")
        self.assertEqual(dict(patched.get("patch_task_recommendation") or {}).get("patch_type"), "extractor")
        next_actions_text = " ".join(list(patched.get("recommended_next_actions") or [])).lower()
        self.assertTrue("geographic" in next_actions_text or "bbox" in next_actions_text)

    def test_stage4_p3_pre_step20_partial_evidence_is_cautious(self) -> None:
        class _StubAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                del envelope
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {"contradictions_found": False, "contradictions": [], "consistency_summary": "ok"},
                        "meta": {"task": endpoint_task},
                    }
                return {
                    "response": {
                        "summary": "advisory interpretation generated",
                        "dominant_cause_class": "unknown",
                        "confidence": 0.40,
                        "secondary_causes": [],
                        "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                        "recommended_branch": "manual_review_required",
                        "recommended_next_actions": ["collect more evidence"],
                        "patch_task_recommendation": {
                            "should_create_patch_task": False,
                            "patch_type": None,
                            "justification": None,
                            "suggested_target": None,
                        },
                        "operator_action_required": False,
                        "approval_type_if_needed": None,
                        "risk_notes": [],
                    },
                    "meta": {"task": endpoint_task},
                }

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_StubAdvisoryService())
        step = self.registry[STEP_P3_1_EXTRACT]
        state = RunSessionState(
            run_id="run-stage4-p3-pre-step20",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-stage4-p3-pre-step20",
        )
        snap = interpreter(
            state,
            step,
            1,
            ExecutorResult(ok=True, summary={"extractor_diagnostics": {"candidate_count": 2}}, artifacts=[]),
            ValidatorResult(status="warning", gate_passed=True, evidence={}),
            AIBotSnapshot(
                extractor_status={"schema_version": "extractor_status_v1", "phase": "phase3", "completion_metrics": {"step20_available": False}},
                extractor_help_needed={
                    "needed": True,
                    "severity": "medium",
                    "confidence": 0.42,
                    "reason_class": "partial_evidence_extractor_struggle",
                    "reasons": [{"code": "partial_evidence_extractor_struggle", "value": 0.59, "threshold": 0.65, "message": "Extractor is weak before Step20 evidence is available."}],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.59,
                        "completion_quality_score": None,
                        "order_completion_quality_score": None,
                        "attempt_count_total": 1,
                        "same_config_repeat_count": 0,
                        "retry_diversity_count": 1,
                        "fallback_rescue_success": None,
                        "step20_available": False,
                    },
                    "recommended_escalation": "interpreter_patch_evaluation",
                    "partial_evidence": True,
                    "notes": ["step20_unavailable_partial_evidence"],
                },
            ),
            "warning",
        )
        branch = self._priority_value(snap.priorities, "branch")
        self.assertIn(branch, {"tuning_retry", "manual_review_required", "patch_diagnostics"})
        self.assertNotEqual(branch, "patch_extractor")

    def test_stage4_non_extractor_flow_remains_compatible(self) -> None:
        class _StubAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                del envelope
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {"contradictions_found": False, "contradictions": [], "consistency_summary": "ok"},
                        "meta": {"task": endpoint_task},
                    }
                return {
                    "response": {
                        "summary": "semantic anomaly is minor",
                        "dominant_cause_class": "detector_thresholds",
                        "confidence": 0.61,
                        "secondary_causes": [],
                        "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                        "recommended_branch": "no_patch_continue",
                        "recommended_next_actions": ["continue and monitor"],
                        "patch_task_recommendation": {
                            "should_create_patch_task": False,
                            "patch_type": None,
                            "justification": None,
                            "suggested_target": None,
                        },
                        "operator_action_required": False,
                        "approval_type_if_needed": None,
                        "risk_notes": [],
                    },
                    "meta": {"task": endpoint_task},
                }

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_StubAdvisoryService())
        step = self.registry[STEP_P2_1_SEMANTIC]
        state = RunSessionState(
            run_id="run-stage4-non-extractor",
            pipeline_scope={"phases": ["phase2"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase2",
            current_step_id=STEP_P2_1_SEMANTIC,
            trace_id="trace-stage4-non-extractor",
        )
        snap = interpreter(
            state,
            step,
            1,
            ExecutorResult(ok=True, summary={"quality_score": 0.9}, artifacts=[]),
            ValidatorResult(status="warning", gate_passed=True, evidence={}),
            AIBotSnapshot(),
            "warning",
        )
        self.assertEqual(self._priority_value(snap.priorities, "branch"), "no_patch_continue")
        self.assertFalse(any(str(x).startswith("extractor_override:") for x in list(snap.priorities or [])))

    def test_stage4_p14_no_approved_nodes_prefers_operator_precondition_over_patch(self) -> None:
        snapshot = {
            "phase": "phase1",
            "step_id": STEP_P1_4_PROMOTE,
            "validator": {
                "block_reason_code": BlockReasonCode.NO_APPROVED_NODES_FOR_PROMOTE.value,
                "recommended_action": "approve_nodes_before_promote",
                "evidence": {
                    "promote_status": "no_approved_nodes",
                    "promote_precondition": "no_approved_nodes",
                    "resolved_total": 1652,
                    "approved_count": 0,
                    "pending_review_count": 1652,
                    "workspace_state_summary": {
                        "node_set_id": "ns-p14-operational",
                        "resolved_total": 1652,
                        "approved_count": 0,
                        "rejected_count": 0,
                        "pending_review_count": 1652,
                    },
                },
            },
            "promote_context": {
                "promote_status": "no_approved_nodes",
                "legacy_status": "not_found",
                "promote_precondition": "no_approved_nodes",
                "n_resolved": 0,
                "approved_count": 0,
                "pending_review_count": 1652,
                "resolved_total": 1652,
                "recommended_action": "approve_nodes_before_promote",
                "diagnostics_hint": "No approved nodes yet in workspace; approve nodes before promote.",
                "workspace_state_summary": {
                    "node_set_id": "ns-p14-operational",
                    "resolved_total": 1652,
                    "approved_count": 0,
                    "rejected_count": 0,
                    "pending_review_count": 1652,
                },
            },
            "ai_bot": {
                "extractor_help_needed": {
                    "needed": True,
                    "severity": "medium",
                    "confidence": 0.70,
                    "reason_class": "partial_evidence_extractor_struggle",
                    "reasons": [],
                    "evidence": {},
                    "recommended_escalation": "interpreter_patch_evaluation",
                    "partial_evidence": False,
                    "notes": [],
                }
            },
            "consistency_candidates": [
                {
                    "code": "P1_PROMOTE_RESOLVED_BUT_EMPTY_DRY_RUN",
                    "severity": "medium",
                    "details": "resolved_total > 0 but dry-run empty",
                }
            ],
        }
        response = {
            "summary": "generic patch-first recommendation",
            "dominant_cause_class": "scoring_logic",
            "confidence": 0.61,
            "secondary_causes": [],
            "evidence_consistency_checks": {"contradictions_found": True, "contradictions": []},
            "recommended_branch": "patch_detector_scoring",
            "recommended_next_actions": ["patch detector scoring"],
            "patch_task_recommendation": {
                "should_create_patch_task": True,
                "patch_type": "detector_scoring",
                "justification": "generic",
                "suggested_target": "either",
            },
            "operator_action_required": False,
            "approval_type_if_needed": None,
            "risk_notes": [],
        }
        patched, override_tag = AdvisoryChatGPTInterpreter._extractor_signal_routing_override(
            snapshot=snapshot,
            response=response,
            consistency_precheck={"contradictions_found": True, "contradictions": []},
        )
        self.assertEqual(override_tag, "extractor_override:p1_promote_no_approved_nodes")
        self.assertEqual(patched.get("recommended_branch"), "manual_review_required")
        self.assertEqual(patched.get("dominant_cause_class"), "promote_precondition_unmet")
        self.assertTrue(bool(patched.get("operator_action_required")))
        self.assertEqual(patched.get("approval_type_if_needed"), ApprovalType.PROMOTE_NODE_BATCH.value)
        self.assertEqual(
            dict(patched.get("patch_task_recommendation") or {}).get("should_create_patch_task"),
            False,
        )
        self.assertIn(
            "Approve nodes in the Phase1 workspace before attempting promote again.",
            list(patched.get("recommended_next_actions") or []),
        )

    def test_stage4_p14_after_approvals_empty_promote_can_route_to_diagnostics_patch(self) -> None:
        snapshot = {
            "phase": "phase1",
            "step_id": STEP_P1_4_PROMOTE,
            "validator": {
                "block_reason_code": BlockReasonCode.PROMOTE_LOOKUP_EMPTY.value,
                "recommended_action": "inspect_promote_lookup",
                "evidence": {
                    "promote_status": "staging_missing",
                    "promote_precondition": "lookup_or_staging_issue",
                    "resolved_total": 1652,
                    "approved_count": 12,
                    "pending_review_count": 1640,
                    "diagnostics_hint": "inspect joins",
                    "workspace_state_summary": {
                        "node_set_id": "ns-p14-diagnostics",
                        "resolved_total": 1652,
                        "approved_count": 12,
                        "rejected_count": 0,
                        "pending_review_count": 1640,
                    },
                },
            },
            "promote_context": {
                "promote_status": "staging_missing",
                "legacy_status": "not_found",
                "promote_precondition": "lookup_or_staging_issue",
                "n_resolved": 0,
                "approved_count": 12,
                "pending_review_count": 1640,
                "resolved_total": 1652,
                "recommended_action": "inspect_promote_lookup",
                "diagnostics_hint": "inspect joins",
                "workspace_state_summary": {
                    "node_set_id": "ns-p14-diagnostics",
                    "resolved_total": 1652,
                    "approved_count": 12,
                    "rejected_count": 0,
                    "pending_review_count": 1640,
                },
            },
            "ai_bot": {
                "extractor_help_needed": {
                    "needed": True,
                    "severity": "medium",
                    "confidence": 0.68,
                    "reason_class": "partial_evidence_extractor_struggle",
                    "reasons": [],
                    "evidence": {},
                    "recommended_escalation": "interpreter_patch_evaluation",
                    "partial_evidence": False,
                    "notes": [],
                }
            },
            "consistency_candidates": [
                {
                    "code": "P1_PROMOTE_RESOLVED_BUT_EMPTY_DRY_RUN",
                    "severity": "high",
                    "details": "approved workspace data exists but dry-run empty",
                }
            ],
        }
        response = {
            "summary": "generic",
            "dominant_cause_class": "unknown",
            "confidence": 0.4,
            "secondary_causes": [],
            "evidence_consistency_checks": {"contradictions_found": True, "contradictions": []},
            "recommended_branch": "manual_review_required",
            "recommended_next_actions": ["collect more evidence"],
            "patch_task_recommendation": {
                "should_create_patch_task": False,
                "patch_type": None,
                "justification": None,
                "suggested_target": None,
            },
            "operator_action_required": False,
            "approval_type_if_needed": None,
            "risk_notes": [],
        }
        patched, override_tag = AdvisoryChatGPTInterpreter._extractor_signal_routing_override(
            snapshot=snapshot,
            response=response,
            consistency_precheck={"contradictions_found": True, "contradictions": []},
        )
        self.assertEqual(override_tag, "extractor_override:p1_promote_staging_missing")
        self.assertEqual(patched.get("recommended_branch"), "patch_detector_scoring")
        self.assertEqual(patched.get("dominant_cause_class"), "scoring_logic")
        self.assertTrue(bool(dict(patched.get("patch_task_recommendation") or {}).get("should_create_patch_task")))
        self.assertEqual(
            dict(patched.get("patch_task_recommendation") or {}).get("patch_type"),
            "detector_scoring",
        )

    def test_stage4_extractor_override_remains_schema_and_policy_safe(self) -> None:
        from datamind_console.api_chatgpt.services.policy_guard import PolicyGuard
        from datamind_console.api_chatgpt.services.response_validator import ResponseValidator

        snapshot = {
            "phase": "phase1",
            "step_id": STEP_P1_1_EXTRACT,
            "validator": {"evidence": {"candidate_count": 0}},
            "ai_bot": {
                "extractor_status": {
                    "schema_version": "extractor_status_v1",
                    "phase": "phase1",
                    "completion_metrics": {"candidate_count": 0},
                    "scores": {"completion_quality_score": 0.2},
                },
                "extractor_help_needed": {
                    "needed": True,
                    "severity": "high",
                    "confidence": 0.81,
                    "reason_class": "repeated_empty_extraction",
                    "reasons": [{"code": "repeated_empty_extraction", "value": 3, "threshold": 2, "message": "Repeated empty extraction attempts detected."}],
                    "evidence": {
                        "extractor_efficiency_health_score": 0.22,
                        "completion_quality_score": 0.2,
                        "order_completion_quality_score": None,
                        "attempt_count_total": 3,
                        "same_config_repeat_count": 2,
                        "retry_diversity_count": 1,
                        "fallback_rescue_success": False,
                        "step20_available": False,
                    },
                    "recommended_escalation": "interpreter_patch_evaluation",
                    "partial_evidence": False,
                    "notes": [],
                },
            },
            "consistency_candidates": [],
        }
        response = {
            "summary": "advisory interpretation generated",
            "dominant_cause_class": "unknown",
            "confidence": 0.4,
            "secondary_causes": [],
            "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
            "recommended_branch": "manual_review_required",
            "recommended_next_actions": ["collect more evidence"],
            "patch_task_recommendation": {
                "should_create_patch_task": False,
                "patch_type": None,
                "justification": None,
                "suggested_target": None,
            },
            "operator_action_required": False,
            "approval_type_if_needed": None,
            "risk_notes": [],
        }
        patched, override_tag = AdvisoryChatGPTInterpreter._extractor_signal_routing_override(
            snapshot=snapshot,
            response=response,
            consistency_precheck={},
        )
        self.assertTrue(bool(override_tag))
        validator = ResponseValidator()
        self.assertEqual(
            validator.validate_task_response(task="hades_pipeline_interpreter", payload=patched),
            [],
        )
        guard = PolicyGuard()
        self.assertEqual(
            guard.enforce_post_response(
                task="hades_pipeline_interpreter",
                response=patched,
                snapshot={"evidence_refs": []},
            ),
            [],
        )

    def test_p3_step20_blocked_by_unmatched_diverts_to_p1_new_nodes(self) -> None:
        engine = self._new_engine()

        def blocked_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(
                status="blocked",
                gate_passed=False,
                block_reason_code=BlockReasonCode.STEP20_UNMATCHED_BLOCKING,
                summary="unmatched stops block gate",
                evidence={"unmatched_count": 5, "ambiguous_count": 0},
            )

        engine.register_validator(self.registry[STEP_P3_2_STEP20].validator, blocked_validator)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=self._profile,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id)

        self.assertEqual(run.status, "waiting_for_approval")
        self.assertEqual(run.current_step_id, STEP_P1_3B_NEW_NODES)
        self.assertTrue(any(e.event_type == "diversion_started" for e in run.events))

        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            pending[0].approval_type,
            ApprovalType.RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS,
        )

    def test_resume_loop_after_human_resolution_reruns_step20_and_continues(self) -> None:
        engine = self._new_engine()
        calls = {"step20": 0}

        def step20_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            calls["step20"] += 1
            if calls["step20"] == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.STEP20_UNMATCHED_BLOCKING,
                    summary="needs new nodes",
                    evidence={"unmatched_count": 6, "ambiguous_count": 1},
                )
            return ValidatorResult(status="pass", gate_passed=True)

        def p2_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_validator(self.registry[STEP_P3_2_STEP20].validator, step20_validator)
        engine.register_validator(self.registry[STEP_P2_1_SEMANTIC].validator, p2_validator)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=self._profile,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id)

        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(len(pending), 1)

        run = engine.resolve_approval(
            run_id=run.run_id,
            approval_id=pending[0].approval_id,
            decision="approved",
            operator_id="op-1",
            operator_role="admin",
            operator_decision="resolved unmatched and promoted",
            resolution_payload={"requires_p2_partial_rerun": True, "promote_confirmed": True},
            max_steps_after_resume=2,
        )

        self.assertEqual(calls["step20"], 2)
        self.assertEqual(run.current_step_id, STEP_P3_4_STEP30)
        self.assertTrue(any(e.event_type == "resume_triggered" for e in run.events))
        self.assertTrue(any(e.event_type == "resume_completed" for e in run.events))
        self.assertTrue(any(e.event_type == "diversion_completed" for e in run.events))
        self.assertTrue(any(e.event_type == "step_started" and e.step_id == STEP_P2_1_SEMANTIC for e in run.events))

    def test_step20_diversion_requires_promote_confirmation_before_resume(self) -> None:
        engine = self._new_engine()

        def blocked_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(
                status="blocked",
                gate_passed=False,
                block_reason_code=BlockReasonCode.STEP20_UNMATCHED_BLOCKING,
                summary="needs promote confirmation",
                evidence={"unmatched_count": 4, "ambiguous_count": 0},
            )

        engine.register_validator(self.registry[STEP_P3_2_STEP20].validator, blocked_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=self._profile,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id)
        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(len(pending), 1)

        with self.assertRaises(ValueError):
            engine.resolve_approval(
                run_id=run.run_id,
                approval_id=pending[0].approval_id,
                decision="approved",
                operator_id="op-1",
                operator_role="admin",
                operator_decision="resolved but forgot promote confirm",
                resolution_payload={"requires_p2_partial_rerun": False},
                max_steps_after_resume=1,
            )

    def test_manual_assisted_step_can_be_confirmed_and_advanced(self) -> None:
        engine = self._new_engine()
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=self._profile,
            start_step_id=STEP_P1_3A_WORKSPACE,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        self.assertEqual(run.status, "paused")
        self.assertEqual(run.current_step_id, STEP_P1_3A_WORKSPACE)

        run = engine.mark_manual_step_resolved(
            run_id=run.run_id,
            step_id=STEP_P1_3A_WORKSPACE,
            operator_id="op-2",
            operator_role="admin",
            operator_notes="workspace review completed",
            max_steps_after_resume=2,
        )
        self.assertEqual(run.current_step_id, "P1.4_APPROVE_PROMOTE")
        self.assertEqual(run.status, "waiting_for_approval")

    def test_reorder_proposal_requires_approval_before_apply(self) -> None:
        engine = self._new_engine()
        apply_calls = {"n": 0}

        def reorder_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="pass", gate_passed=True)

        def apply_executor(state, step, attempt_no, params):
            del state, step, attempt_no, params
            apply_calls["n"] += 1
            return ExecutorResult(ok=True, summary={"applied": True}, artifacts=[])

        engine.register_validator(self.registry[STEP_P3_3_REORDER].validator, reorder_validator)
        engine.register_executor(self.registry[STEP_P3_3_REORDER].approval_apply_executor or "", apply_executor)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=self._profile,
            start_step_id=STEP_P3_3_REORDER,
        )
        run = engine.advance_run(run.run_id)

        self.assertEqual(run.status, "waiting_for_approval")
        self.assertEqual(apply_calls["n"], 0)

        # Re-advance without approval must not apply.
        run = engine.advance_run(run.run_id)
        self.assertEqual(apply_calls["n"], 0)

        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(len(pending), 1)

        run = engine.resolve_approval(
            run_id=run.run_id,
            approval_id=pending[0].approval_id,
            decision="approved",
            operator_id="op-2",
            operator_role="admin",
            operator_decision="approve reorder apply",
            resolution_payload={"stop_sequence_candidate_id": "candidate-1"},
            max_steps_after_resume=0,
        )
        self.assertEqual(apply_calls["n"], 1)

    def test_step40_approval_requires_and_executes_apply(self) -> None:
        engine = self._new_engine()
        apply_calls = {"n": 0}

        def step40_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="pass", gate_passed=True)

        def step40_apply(state, step, attempt_no, params):
            del state, step, attempt_no, params
            apply_calls["n"] += 1
            return ExecutorResult(ok=True, summary={"approved_geometry_candidate_id": "abc"}, artifacts=[])

        engine.register_validator(self.registry[STEP_P3_4_STEP40].validator, step40_validator)
        engine.register_executor(self.registry[STEP_P3_4_STEP40].approval_apply_executor or "", step40_apply)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=self._profile,
            start_step_id=STEP_P3_4_STEP40,
        )
        run = engine.advance_run(run.run_id)

        self.assertEqual(run.status, "waiting_for_approval")
        self.assertEqual(apply_calls["n"], 0)
        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(len(pending), 1)

        run = engine.resolve_approval(
            run_id=run.run_id,
            approval_id=pending[0].approval_id,
            decision="approved",
            operator_id="op-2",
            operator_role="admin",
            operator_decision="approve final route checkpoint",
            max_steps_after_resume=0,
        )
        self.assertIn(run.status, {"running", "paused", "waiting_for_approval", "completed"})
        self.assertEqual(apply_calls["n"], 1)

    def test_merge_bind_cannot_auto_execute(self) -> None:
        engine = self._new_engine()
        apply_calls = {"n": 0}

        def merge_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="pass", gate_passed=True)

        def merge_apply(state, step, attempt_no, params):
            del state, step, attempt_no, params
            apply_calls["n"] += 1
            return ExecutorResult(ok=True, summary={"merged": True}, artifacts=[])

        engine.register_validator(self.registry[STEP_P3_5_MERGE].validator, merge_validator)
        engine.register_executor(self.registry[STEP_P3_5_MERGE].approval_apply_executor or "", merge_apply)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=self._profile,
            start_step_id=STEP_P3_5_MERGE,
        )
        run = engine.advance_run(run.run_id)

        self.assertEqual(run.status, "waiting_for_approval")
        self.assertEqual(apply_calls["n"], 0)
        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(pending[0].approval_type, ApprovalType.APPROVE_FINAL_ROUTE_OR_MERGE_BIND)

    def test_destructive_cleanup_cannot_auto_execute(self) -> None:
        engine = self._new_engine()
        apply_calls = {"n": 0}

        def cleanup_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="pass", gate_passed=True)

        def cleanup_apply(state, step, attempt_no, params):
            del state, step, attempt_no, params
            apply_calls["n"] += 1
            return ExecutorResult(ok=True, summary={"cleanup": "done"}, artifacts=[])

        engine.register_validator(self.registry[STEP_P2_3_CLEANUP].validator, cleanup_validator)
        engine.register_executor(self.registry[STEP_P2_3_CLEANUP].approval_apply_executor or "", cleanup_apply)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase2"]},
            policy_profile=self._profile,
            start_step_id=STEP_P2_3_CLEANUP,
        )
        run = engine.advance_run(run.run_id)

        self.assertEqual(run.status, "waiting_for_approval")
        self.assertEqual(apply_calls["n"], 0)
        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(pending[0].approval_type, ApprovalType.RUN_DESTRUCTIVE_CLEANUP)

    def test_gate_bypass_attempts_are_rejected_and_logged(self) -> None:
        engine = self._new_engine()

        def gate_bypass_executor(state, step, attempt_no, params):
            del state, step, attempt_no, params
            return ExecutorResult(
                ok=True,
                summary={"gate_bypass_attempted": True, "gate_override": "force_pass"},
                artifacts=[],
            )

        def passing_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_executor(self.registry[STEP_P3_2_STEP20].executor, gate_bypass_executor)
        engine.register_validator(self.registry[STEP_P3_2_STEP20].validator, passing_validator)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=self._profile,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id)

        self.assertEqual(run.status, "paused")
        rec = run.step_execution_records[-1]
        self.assertIsNotNone(rec.block_reason)
        self.assertEqual(rec.block_reason.get("code"), BlockReasonCode.GATE_BYPASS_ATTEMPT)

        blocked_events = [e for e in run.events if e.event_type == "step_blocked"]
        self.assertTrue(blocked_events)
        self.assertIn("GATE_BYPASS_ATTEMPT", str(blocked_events[-1].payload))

    def test_operator_identity_required_for_approval_resolution(self) -> None:
        engine = self._new_engine()

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=self._profile,
            start_step_id=STEP_P3_3_REORDER,
        )
        run = engine.advance_run(run.run_id)
        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(len(pending), 1)

        with self.assertRaises(PermissionError):
            engine.resolve_approval(
                run_id=run.run_id,
                approval_id=pending[0].approval_id,
                decision="approved",
                operator_id=None,
                operator_decision="missing operator id",
                max_steps_after_resume=0,
            )

    def test_operator_role_required_for_approval_resolution(self) -> None:
        engine = self._new_engine()

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=self._profile,
            start_step_id=STEP_P3_3_REORDER,
            operator_context={"operator_id": "op-9", "operator_roles": []},
        )
        run = engine.advance_run(run.run_id)
        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(len(pending), 1)

        with self.assertRaises(PermissionError):
            engine.resolve_approval(
                run_id=run.run_id,
                approval_id=pending[0].approval_id,
                decision="approved",
                operator_id="op-9",
                operator_decision="missing operator role",
                max_steps_after_resume=0,
            )

    def test_approval_apply_idempotency_prevents_duplicate_apply(self) -> None:
        engine = self._new_engine()
        apply_calls = {"n": 0}

        def apply_executor(state, step, attempt_no, params):
            del state, step, attempt_no, params
            apply_calls["n"] += 1
            return ExecutorResult(ok=True, summary={"applied": True}, artifacts=[])

        engine.register_executor(self.registry[STEP_P3_3_REORDER].approval_apply_executor or "", apply_executor)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=self._profile,
            start_step_id=STEP_P3_3_REORDER,
        )
        run = engine.advance_run(run.run_id)
        pending = engine.list_pending_approvals(run.run_id)
        self.assertEqual(len(pending), 1)

        idem_key = f"test-idem:{pending[0].approval_id}"
        run = engine.resolve_approval(
            run_id=run.run_id,
            approval_id=pending[0].approval_id,
            decision="approved",
            operator_id="op-2",
            operator_role="admin",
            operator_decision="approve once",
            resolution_payload={"stop_sequence_candidate_id": "candidate-1"},
            max_steps_after_resume=0,
            idempotency_key=idem_key,
        )
        self.assertEqual(apply_calls["n"], 1)

        # Duplicate call with same idempotency key must not apply again.
        run2 = engine.resolve_approval(
            run_id=run.run_id,
            approval_id=pending[0].approval_id,
            decision="approved",
            operator_id="op-2",
            operator_role="admin",
            operator_decision="duplicate replay",
            resolution_payload={"stop_sequence_candidate_id": "candidate-1"},
            max_steps_after_resume=0,
            idempotency_key=idem_key,
        )
        self.assertEqual(run2.run_id, run.run_id)
        self.assertEqual(apply_calls["n"], 1)

    def test_p1_retry_delta_mapping_applies_or_surfaces_unsupported(self) -> None:
        class _FakePhase1Client:
            def __init__(self) -> None:
                self.kwargs = {}

            def run_step_build_node_set_tuned(self, **kwargs):
                self.kwargs = dict(kwargs)
                ordered = list(kwargs.get("candidate_actions") or [])
                best_action = ordered[0] if ordered else "stops_broad_bbox"
                return {
                    "ok": True,
                    "area_group": "valle_core",
                    "sector": "demo",
                    "best": {
                        "node_set_id": "node-set-1",
                        "run_id": "run-raw-1",
                        "action_id": best_action,
                        "quality_score": 0.74,
                        "metrics": {"raw_count": 120, "candidate_count": 42},
                    },
                    "attempts": [
                        {
                            "action_id": best_action,
                            "bbox": dict(kwargs.get("bbox") or {}),
                            "retry_strategy": "bbox_expand",
                            "retry_reason": "quality_below_threshold",
                            "retry_parameter_delta": {"bbox_buffer_ratio_to": 0.25},
                            "node_set_id": "node-set-1",
                            "run_id": "run-raw-1",
                            "area_group": "valle_core",
                            "sector": "demo",
                            "quality_score": 0.74,
                            "metrics": {"raw_count": 120, "candidate_count": 42},
                        }
                    ],
                }

        fake = _FakePhase1Client()
        bridge = build_phase_client_executor_bridge(phase1_client=fake)
        state = RunSessionState(
            run_id="run-p1-map",
            pipeline_scope={
                "phases": ["phase1"],
                "phase1": {
                    "extract": {
                        "bbox": {"south": -0.30, "west": -78.55, "north": -0.10, "east": -78.35},
                        "candidate_actions": ["stops_broad_bbox", "platforms_bbox"],
                        "max_bbox_retries": 2,
                    }
                },
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-map",
        )
        result = bridge["phase1_extract"](
            state,
            self.registry[STEP_P1_1_EXTRACT],
            2,
            {
                "bbox_expand_pct": 25,
                "template_variant": "alternative_2",
                "fallback_config": "fallback_1",
                "unsupported_toggle": True,
            },
        )

        self.assertTrue(result.ok)
        summary = dict(result.summary or {})
        applied = dict(summary.get("retry_parameter_applied") or {})
        self.assertIn("bbox_expand_pct", applied)
        self.assertEqual(applied.get("fallback_config"), "fallback_1")
        self.assertEqual(list(fake.kwargs.get("candidate_actions") or [])[0], "platforms_bbox")
        self.assertIn("unsupported_retry_param:unsupported_toggle", list(summary.get("retry_parameter_warnings") or []))
        self.assertTrue(str(summary.get("effective_config_fingerprint") or "").strip())

    def test_p1_extract_maps_area_to_area_group_hint_and_filters_unsupported_runtime_keys(self) -> None:
        class _StrictPhase1Client:
            def __init__(self) -> None:
                self.calls: List[Dict[str, Any]] = []

            def run_step_build_node_set_tuned(
                self,
                *,
                actions_path: str,
                bbox: Dict[str, float],
                candidate_actions: Optional[List[str]] = None,
                sector_hint: Optional[str] = None,
                area_group_hint: Optional[str] = None,
                route_tokens: Optional[List[str]] = None,
                extra_params: Optional[Dict[str, Any]] = None,
                max_actions: int = 3,
                max_bbox_retries: int = 4,
                eps_m: float = 35.0,
                min_pts: int = 3,
                bandit_key: Optional[str] = None,
            ) -> Dict[str, Any]:
                del actions_path, extra_params, max_actions, max_bbox_retries, eps_m, min_pts, bandit_key
                self.calls.append(
                    {
                        "bbox": dict(bbox or {}),
                        "candidate_actions": list(candidate_actions or []),
                        "sector_hint": sector_hint,
                        "area_group_hint": area_group_hint,
                        "route_tokens": list(route_tokens or []),
                    }
                )
                return {
                    "ok": True,
                    "best": {
                        "node_set_id": "ns-area-map",
                        "action_id": "stops_broad_bbox",
                        "quality_score": 0.61,
                        "metrics": {"raw_count": 20, "candidate_count": 6},
                    },
                    "attempts": [],
                }

        strict = _StrictPhase1Client()
        bridge = build_phase_client_executor_bridge(phase1_client=strict)
        state = RunSessionState(
            run_id="run-p1-area-map",
            pipeline_scope={
                "phases": ["phase1"],
                "phase1": {
                    "extract": {
                        "bbox": {"south": -0.30, "west": -78.55, "north": -0.10, "east": -78.35},
                        "candidate_actions": ["stops_broad_bbox"],
                        "area": "sangolqui",
                        "unexpected_scope_key": "x",
                    }
                },
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-area-map",
        )
        result = bridge["phase1_extract"](
            state,
            self.registry[STEP_P1_1_EXTRACT],
            1,
            {
                "retry_strategy": "bbox_expand",
                "retry_reason": "test",
                "retry_parameter_delta": {"bbox_expand_pct": 10},
            },
        )
        self.assertTrue(result.ok)
        self.assertTrue(strict.calls)
        call = dict(strict.calls[-1] or {})
        self.assertEqual(str(call.get("area_group_hint") or ""), "")
        self.assertEqual(str(call.get("sector_hint") or ""), "sangolqui")
        self.assertEqual(list(call.get("route_tokens") or []), ["sangolqui"])
        spatial = dict((result.summary or {}).get("spatial_interpretation") or {})
        self.assertEqual(str(spatial.get("spatial_interpretation_status") or ""), "ok")
        self.assertEqual(str(spatial.get("runtime_spatial_strategy_used") or ""), "explicit_bbox")
        self.assertEqual(dict((result.summary or {}).get("validator_payload") or {}).get("target_option_received"), True)
        warnings = list(dict(result.summary or {}).get("retry_parameter_warnings") or [])
        self.assertIn("unsupported_runtime_conf_key:retry_strategy", warnings)
        self.assertIn("unsupported_runtime_conf_key:retry_reason", warnings)
        self.assertIn("unsupported_runtime_conf_key:retry_parameter_delta", warnings)
        self.assertIn("unsupported_runtime_conf_key:unexpected_scope_key", warnings)

    @unittest.skip("depends on the original region's bbox catalog; adapt it to your own region's catalogs (see README, Tests)")
    def test_p1_extract_target_option_uses_sector_catalog_bbox_when_available(self) -> None:
        from datamind_console.phases.phase1_nodes.client import Phase1Client

        class _StrictPhase1Client:
            def __init__(self) -> None:
                self.calls: List[Dict[str, Any]] = []
                self._resolver = Phase1Client()

            def _resolve_sector_context(self, **kwargs) -> Dict[str, Any]:
                return dict(self._resolver._resolve_sector_context(**kwargs) or {})

            def run_step_build_node_set_tuned(
                self,
                *,
                actions_path: str,
                bbox: Dict[str, float],
                candidate_actions: Optional[List[str]] = None,
                sector_hint: Optional[str] = None,
                area_group_hint: Optional[str] = None,
                route_tokens: Optional[List[str]] = None,
                extra_params: Optional[Dict[str, Any]] = None,
                max_actions: int = 3,
                max_bbox_retries: int = 4,
                eps_m: float = 35.0,
                min_pts: int = 3,
                bandit_key: Optional[str] = None,
            ) -> Dict[str, Any]:
                del actions_path, extra_params, max_actions, max_bbox_retries, eps_m, min_pts, bandit_key
                self.calls.append(
                    {
                        "bbox": dict(bbox or {}),
                        "candidate_actions": list(candidate_actions or []),
                        "sector_hint": sector_hint,
                        "area_group_hint": area_group_hint,
                        "route_tokens": list(route_tokens or []),
                    }
                )
                return {
                    "ok": True,
                    "best": {
                        "node_set_id": "ns-conocoto",
                        "action_id": "stops_broad_bbox",
                        "quality_score": 0.71,
                        "metrics": {"raw_count": 36, "candidate_count": 11},
                    },
                    "attempts": [],
                }

        strict = _StrictPhase1Client()
        bridge = build_phase_client_executor_bridge(phase1_client=strict)
        state = RunSessionState(
            run_id="run-p1-conocoto",
            pipeline_scope={
                "phases": ["phase1"],
                "phase1": {"extract": {"candidate_actions": ["stops_broad_bbox"], "area": "cnct"}},
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-conocoto",
        )
        result = bridge["phase1_extract"](state, self.registry[STEP_P1_1_EXTRACT], 1, {})
        self.assertTrue(result.ok)
        call = dict(strict.calls[-1] or {})
        self.assertEqual(str(call.get("area_group_hint") or ""), "conocoto_corridor")
        self.assertEqual(str(call.get("sector_hint") or ""), "Conocoto")
        self.assertEqual(list(call.get("route_tokens") or []), ["cnct"])
        self.assertEqual(
            dict(call.get("bbox") or {}),
            {"south": -0.33, "west": -78.48, "north": -0.22, "east": -78.38},
        )
        spatial = dict((result.summary or {}).get("spatial_interpretation") or {})
        self.assertEqual(str(spatial.get("spatial_interpretation_status") or ""), "ok")
        self.assertEqual(str(spatial.get("spatial_interpretation_source") or ""), "sector_catalog_alias")
        self.assertEqual(str(spatial.get("runtime_spatial_strategy_used") or ""), "sector_catalog_bbox")
        self.assertEqual(dict(spatial.get("bbox_candidate") or {}), dict(call.get("bbox") or {}))
        artifacts = [dict(a or {}) for a in list(result.artifacts or []) if dict(a or {}).get("artifact_type") == "phase1_spatial_interpretation"]
        self.assertTrue(artifacts)

    def test_p1_extract_target_option_without_resolver_marks_fallback_default_bbox(self) -> None:
        class _StrictPhase1Client:
            def __init__(self) -> None:
                self.calls: List[Dict[str, Any]] = []

            def run_step_build_node_set_tuned(
                self,
                *,
                actions_path: str,
                bbox: Dict[str, float],
                candidate_actions: Optional[List[str]] = None,
                sector_hint: Optional[str] = None,
                area_group_hint: Optional[str] = None,
                route_tokens: Optional[List[str]] = None,
                extra_params: Optional[Dict[str, Any]] = None,
                max_actions: int = 3,
                max_bbox_retries: int = 4,
                eps_m: float = 35.0,
                min_pts: int = 3,
                bandit_key: Optional[str] = None,
            ) -> Dict[str, Any]:
                del actions_path, candidate_actions, sector_hint, area_group_hint, route_tokens, extra_params, max_actions, max_bbox_retries, eps_m, min_pts, bandit_key
                self.calls.append({"bbox": dict(bbox or {})})
                return {"ok": False, "error": "empty", "attempts": []}

        strict = _StrictPhase1Client()
        bridge = build_phase_client_executor_bridge(phase1_client=strict)
        state = RunSessionState(
            run_id="run-p1-spatial-fallback",
            pipeline_scope={
                "phases": ["phase1"],
                "phase1": {"extract": {"candidate_actions": ["stops_broad_bbox"], "area": "madeup nowhere"}},
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-spatial-fallback",
        )
        result1 = bridge["phase1_extract"](state, self.registry[STEP_P1_1_EXTRACT], 1, {})
        result2 = bridge["phase1_extract"](state, self.registry[STEP_P1_1_EXTRACT], 2, {})
        self.assertFalse(result1.ok)
        spatial1 = dict((result1.summary or {}).get("spatial_interpretation") or {})
        spatial2 = dict((result2.summary or {}).get("spatial_interpretation") or {})
        self.assertEqual(str(spatial1.get("spatial_interpretation_status") or ""), "fallback_default_bbox")
        self.assertIsNone(spatial1.get("bbox_candidate"))
        self.assertEqual(
            dict(spatial1.get("runtime_bbox_used") or {}),
            {"south": -0.38, "west": -78.6, "north": -0.02, "east": -78.35},
        )
        warnings1 = list(dict(result1.summary or {}).get("retry_parameter_warnings") or [])
        self.assertIn("spatial_interpretation_failed", warnings1)
        self.assertIn("target_intent_ignored", warnings1)
        self.assertEqual(
            str(dict((result1.summary or {}).get("validator_payload") or {}).get("recommended_action") or ""),
            "inspect_spatial_interpretation_before_retry",
        )
        self.assertIs(spatial2.get("retry_changed_spatial_plan"), False)
        self.assertEqual(int(spatial2.get("same_spatial_plan_retry_count") or 0), 1)
        warnings2 = list(dict(result2.summary or {}).get("retry_parameter_warnings") or [])
        self.assertIn("spatial_plan_reused_without_change", warnings2)
        self.assertEqual(
            str(dict((result2.summary or {}).get("validator_payload") or {}).get("recommended_action") or ""),
            "pause_and_patch_extractor_spatial_interpretation",
        )

    def test_p1_extract_explicit_bbox_passthrough_preserves_coordinate_contract(self) -> None:
        class _StrictPhase1Client:
            def __init__(self) -> None:
                self.calls: List[Dict[str, Any]] = []

            def run_step_build_node_set_tuned(self, **kwargs) -> Dict[str, Any]:
                self.calls.append(dict(kwargs or {}))
                return {
                    "ok": True,
                    "best": {
                        "node_set_id": "ns-bbox-pass",
                        "action_id": "stops_broad_bbox",
                        "quality_score": 0.66,
                        "metrics": {"raw_count": 24, "candidate_count": 8},
                    },
                    "attempts": [],
                }

        strict = _StrictPhase1Client()
        bridge = build_phase_client_executor_bridge(phase1_client=strict)
        bbox = {"south": -0.31, "west": -78.57, "north": -0.11, "east": -78.34}
        state = RunSessionState(
            run_id="run-p1-bbox-pass",
            pipeline_scope={"phases": ["phase1"], "phase1": {"extract": {"bbox": dict(bbox)}}},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-bbox-pass",
        )
        result = bridge["phase1_extract"](state, self.registry[STEP_P1_1_EXTRACT], 1, {})
        self.assertTrue(result.ok)
        call = dict(strict.calls[-1] or {})
        self.assertEqual(dict(call.get("bbox") or {}), bbox)
        spatial = dict((result.summary or {}).get("spatial_interpretation") or {})
        self.assertEqual(str(spatial.get("spatial_interpretation_status") or ""), "ok")
        self.assertEqual(dict(spatial.get("bbox_candidate") or {}), bbox)
        self.assertEqual(dict(spatial.get("runtime_bbox_used") or {}), bbox)
        self.assertEqual(str(spatial.get("bbox_validation_status") or ""), "valid")

    def test_p1_extract_shared_geography_place_input_overrides_legacy_phase1_area(self) -> None:
        class _StrictPhase1Client:
            def __init__(self) -> None:
                self.calls: List[Dict[str, Any]] = []

            def run_step_build_node_set_tuned(self, **kwargs) -> Dict[str, Any]:
                self.calls.append(dict(kwargs or {}))
                return {
                    "ok": True,
                    "best": {
                        "node_set_id": "ns-shared-geo",
                        "action_id": "stops_broad_bbox",
                        "quality_score": 0.71,
                        "metrics": {"raw_count": 31, "candidate_count": 12},
                    },
                    "attempts": [],
                }

        class _StubResolver:
            def __init__(self) -> None:
                self.calls: List[Dict[str, Any]] = []

            def resolve(self, **kwargs) -> Dict[str, Any]:
                self.calls.append(dict(kwargs or {}))
                return {
                    "original_geographic_input": kwargs.get("place_input"),
                    "normalized_geographic_input": "conocoto terminal",
                    "interpreted_place_meaning": "Conocoto terminal",
                    "interpretation_source": "bbox_catalog",
                    "interpretation_status": "ok",
                    "interpretation_confidence": 0.93,
                    "bbox_candidate_confidence": 0.93,
                    "bbox_candidate": {"south": -0.33, "west": -78.48, "north": -0.22, "east": -78.38},
                    "bbox_validation_status": "valid",
                    "area_group_hint": "conocoto_corridor",
                    "sector_hint": "Conocoto",
                    "corridor_hint": "Conocoto",
                    "fallback_used": False,
                    "fallback_reason": None,
                    "phase_applicability": ["phase1"],
                    "supporting_hints": dict(kwargs.get("supporting_hints") or {}),
                    "advisory_trace": None,
                    "geographic_input_type": "place_input",
                    "geographic_interpretation_source": "bbox_catalog",
                    "geographic_interpretation_status": "ok",
                }

        strict = _StrictPhase1Client()
        resolver = _StubResolver()
        bridge = build_phase_client_executor_bridge(phase1_client=strict, geography_resolver=resolver)
        state = RunSessionState(
            run_id="run-p1-shared-geo",
            pipeline_scope={
                "phases": ["phase1"],
                "geography": {"place_input": "Conocoto terminal", "ai_assist_requested": False},
                "target_entities": "legacy target entities",
                "phase1": {"extract": {"area": "legacy area", "candidate_actions": ["stops_broad_bbox"]}},
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-shared-geo",
        )
        result = bridge["phase1_extract"](state, self.registry[STEP_P1_1_EXTRACT], 1, {})
        self.assertTrue(result.ok)
        self.assertTrue(resolver.calls)
        resolver_call = dict(resolver.calls[-1] or {})
        self.assertEqual(str(resolver_call.get("place_input") or ""), "Conocoto terminal")
        self.assertEqual(
            str(dict(resolver_call.get("supporting_hints") or {}).get("phase1_area_hint") or ""),
            "legacy area",
        )
        call = dict(strict.calls[-1] or {})
        self.assertEqual(
            dict(call.get("bbox") or {}),
            {"south": -0.33, "west": -78.48, "north": -0.22, "east": -78.38},
        )
        self.assertEqual(str(call.get("area_group_hint") or ""), "conocoto_corridor")
        self.assertEqual(str(call.get("sector_hint") or ""), "Conocoto")
        spatial = dict((result.summary or {}).get("spatial_interpretation") or {})
        self.assertEqual(str(spatial.get("original_geographic_input") or ""), "Conocoto terminal")
        self.assertEqual(str(spatial.get("spatial_interpretation_source") or ""), "phase1_catalog")
        self.assertEqual(str(spatial.get("runtime_spatial_strategy_used") or ""), "bbox_catalog_bbox")
        self.assertFalse(bool(spatial.get("fallback_used")))

    def test_p1_normalized_snapshot_includes_geographic_context(self) -> None:
        class _NoopAdvisoryService:
            def run_task_detailed(self, endpoint_task, envelope):
                del endpoint_task, envelope
                return {"response": {}, "meta": {}}

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_NoopAdvisoryService())
        state = RunSessionState(
            run_id="run-p1-geo-packet",
            pipeline_scope={"phases": ["phase1"], "target_entities": "Conocoto terminal"},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-geo-packet",
        )
        snapshot = interpreter._build_normalized_snapshot(  # noqa: SLF001
            state=state,
            step=self.registry[STEP_P1_1_EXTRACT],
            attempt_no=1,
            exec_result=ExecutorResult(
                ok=True,
                summary={
                    "candidate_count": 9,
                    "spatial_interpretation": {
                        "original_geographic_input": "Conocoto terminal",
                        "normalized_geographic_input": "conocoto terminal",
                        "geographic_input_type": "place_or_route_hint",
                        "interpreted_place_meaning": "Conocoto terminal",
                        "geographic_interpretation_source": "phase1_catalog",
                        "spatial_interpretation_status": "ok",
                        "bbox_candidate": {"south": -0.33, "west": -78.48, "north": -0.22, "east": -78.38},
                        "bbox_candidate_confidence": 0.88,
                        "bbox_validation_status": "valid",
                        "runtime_bbox_used": {"south": -0.33, "west": -78.48, "north": -0.22, "east": -78.38},
                        "effective_bbox_fingerprint": "bbox-p1-geo",
                        "fallback_used": False,
                        "fallback_reason": None,
                        "runtime_spatial_strategy_used": "sector_catalog_bbox",
                    },
                },
                artifacts=[],
            ),
            validator_result=ValidatorResult(status="pass", gate_passed=True, evidence={}),
            ai_snapshot=AIBotSnapshot(
                extractor_status={
                    "scores": {
                        "extractor_efficiency_health_score": 0.71,
                        "completion_quality_score": 0.68,
                    }
                },
                extractor_help_needed={},
            ),
            trigger="warning",
        )
        geo = dict(snapshot.get("geographic_context") or {})
        self.assertEqual(str(geo.get("original_geographic_input") or ""), "Conocoto terminal")
        self.assertEqual(str(geo.get("geographic_interpretation_source") or ""), "phase1_catalog")
        self.assertEqual(str(geo.get("geographic_interpretation_status") or ""), "ok")
        self.assertEqual(str(geo.get("runtime_spatial_strategy_used") or ""), "sector_catalog_bbox")
        self.assertEqual(str(geo.get("effective_bbox_fingerprint") or ""), "bbox-p1-geo")
        self.assertEqual(str(geo.get("normalized_geographic_input") or ""), "conocoto terminal")
        self.assertEqual(float(geo.get("bbox_candidate_confidence") or 0.0), 0.88)
        self.assertEqual(bool(geo.get("fallback_used")), False)

    def test_p3_extract_shared_geography_place_input_resolves_bbox_for_discover_scope(self) -> None:
        class _FakePhase3Client:
            def __init__(self) -> None:
                self.discover_calls: List[Dict[str, Any]] = []
                self.fetch_calls: List[Dict[str, Any]] = []
                self.prior_calls: List[str] = []

            def run_step_05_discover(self, **kwargs) -> Dict[str, Any]:
                self.discover_calls.append(dict(kwargs or {}))
                return {
                    "route_id": str(uuid4()),
                    "chosen_osm_relation_id": 12345,
                    "query_strategy": kwargs.get("query_strategy"),
                    "candidate_universe_summary": {
                        "candidate_universe_count": 8,
                        "candidate_scored_count": 8,
                        "candidate_fetch_evaluated_count": 3,
                        "query_strategy": kwargs.get("query_strategy"),
                        "hard_filters_applied": [],
                        "soft_signals_used": ["refs", "operator", "name"],
                    },
                    "selection_summary": {
                        "selection_status": "provisional_selected",
                        "selected_osm_relation_id": 12345,
                        "selected_rank": 1,
                        "selected_score": 13.2,
                        "selection_confidence": 0.64,
                        "score_gap_top2": 0.9,
                        "selected_relation_stop_prior_count": 5,
                        "selection_reason_codes": ["soft_name_match"],
                    },
                    "candidate_preview": [{"osm_relation_id": 12345, "rank": 1}],
                    "extractor_diagnostics": {
                        "candidate_count": 8,
                        "signal_strength": "medium",
                        "quality_flags": [],
                    },
                    "extractor_attempts": [
                        {
                            "phase": "primary",
                            "overpass_url": "https://overpass-api.de/api/interpreter",
                            "returncode": 0,
                            "error_class": "ok",
                            "candidate_count": 8,
                        }
                    ],
                }

            def run_step_10_fetch(self, **kwargs) -> Dict[str, Any]:
                self.fetch_calls.append(dict(kwargs or {}))
                return {
                    "http_status": 200,
                    "raw_count": 18,
                    "candidate_count": 1,
                    "selected_relation_summary": {
                        "osm_relation_id": 12345,
                        "selection_rank": 1,
                        "selection_confidence": 0.64,
                        "score": 13.2,
                        "stop_prior_count": 5,
                    },
                }

            def get_relation_stop_prior(self, route_id) -> List[Dict[str, Any]]:
                self.prior_calls.append(str(route_id))
                return [{"seq": 1}, {"seq": 2}, {"seq": 3}, {"seq": 4}, {"seq": 5}]

        class _StubResolver:
            def __init__(self) -> None:
                self.calls: List[Dict[str, Any]] = []

            def resolve(self, **kwargs) -> Dict[str, Any]:
                self.calls.append(dict(kwargs or {}))
                return {
                    "original_geographic_input": kwargs.get("place_input"),
                    "normalized_geographic_input": "conocoto terminal",
                    "interpreted_place_meaning": "Conocoto terminal",
                    "interpretation_source": "bbox_catalog",
                    "interpretation_status": "ok",
                    "interpretation_confidence": 0.84,
                    "bbox_candidate_confidence": 0.84,
                    "bbox_candidate": {"south": -0.33, "west": -78.48, "north": -0.22, "east": -78.38},
                    "bbox_validation_status": "valid",
                    "area_group_hint": None,
                    "sector_hint": None,
                    "corridor_hint": "Conocoto terminal",
                    "fallback_used": False,
                    "fallback_reason": None,
                    "phase_applicability": ["phase3"],
                    "supporting_hints": dict(kwargs.get("supporting_hints") or {}),
                    "advisory_trace": None,
                    "geographic_input_type": "place_input",
                    "geographic_interpretation_source": "bbox_catalog",
                    "geographic_interpretation_status": "ok",
                }

        fake = _FakePhase3Client()
        resolver = _StubResolver()
        bridge = build_phase_client_executor_bridge(phase3_client=fake, geography_resolver=resolver)
        state = RunSessionState(
            run_id="run-p3-shared-geo",
            pipeline_scope={
                "phases": ["phase3"],
                "geography": {"place_input": "Conocoto terminal", "ai_assist_requested": False},
                "target_entities": "legacy geography",
                "phase3": {
                    "refs": ["E1", "E2"],
                    "operator": "Metro",
                    "name": "Ecovia",
                    "service_route_id": "sr-geo",
                    "direction_id": 1,
                    "query_strategy": "bbox_first_broad",
                },
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-shared-geo",
        )
        result = bridge["phase3_route_extract"](state, self.registry[STEP_P3_1_EXTRACT], 1, {})
        self.assertTrue(result.ok)
        self.assertTrue(resolver.calls)
        resolver_call = dict(resolver.calls[-1] or {})
        self.assertEqual(str(resolver_call.get("place_input") or ""), "Conocoto terminal")
        resolver_hints = dict(resolver_call.get("supporting_hints") or {})
        self.assertEqual(list(resolver_hints.get("refs") or []), ["E1", "E2"])
        self.assertEqual(str(resolver_hints.get("operator") or ""), "Metro")
        self.assertEqual(str(resolver_hints.get("name") or ""), "Ecovia")
        self.assertEqual(str(resolver_hints.get("service_route_id") or ""), "sr-geo")
        self.assertEqual(int(resolver_hints.get("direction_id") or 0), 1)
        discover_call = dict(fake.discover_calls[-1] or {})
        self.assertEqual(discover_call.get("bbox"), (-0.33, -78.48, -0.22, -78.38))
        self.assertEqual(list(discover_call.get("refs") or []), ["E1", "E2"])
        self.assertEqual(str(discover_call.get("operator") or ""), "Metro")
        self.assertEqual(str(discover_call.get("name") or ""), "Ecovia")
        self.assertEqual(str(discover_call.get("query_strategy") or ""), "bbox_first_broad")
        spatial = dict((result.summary or {}).get("spatial_interpretation") or {})
        self.assertEqual(str(spatial.get("original_geographic_input") or ""), "Conocoto terminal")
        self.assertEqual(
            dict(spatial.get("runtime_bbox_used") or {}),
            {"south": -0.33, "west": -78.48, "north": -0.22, "east": -78.38},
        )
        self.assertEqual(str(spatial.get("runtime_spatial_strategy_used") or ""), "bbox_first_broad")
        self.assertFalse(bool(spatial.get("fallback_used")))
        artifacts = [dict(a or {}) for a in list(result.artifacts or [])]
        self.assertTrue(any(str(a.get("artifact_type") or "") == "phase3_geographic_interpretation" for a in artifacts))

    def test_p3_retry_applicability_changes_discover_config_or_marks_warning(self) -> None:
        class _FakePhase3Client:
            def __init__(self) -> None:
                self.discover_calls = []
                self.fetch_calls = []

            def run_step_05_discover(self, **kwargs):
                self.discover_calls.append(dict(kwargs))
                rid = str(uuid4())
                return {
                    "route_id": rid,
                    "chosen_osm_relation_id": 123,
                    "extractor_diagnostics": {
                        "candidate_count": 5,
                        "signal_strength": "medium",
                        "quality_flags": ["discover_candidate_pool_single"],
                    },
                    "extractor_attempts": [
                        {
                            "phase": "primary",
                            "overpass_url": "https://example.org/overpass",
                            "returncode": 0,
                            "error_class": "ok",
                        }
                    ],
                    "extractor_fallback_used": False,
                    "extractor_fallback_profile": None,
                }

            def run_step_10_fetch(self, **kwargs):
                self.fetch_calls.append(dict(kwargs))
                return {"http_status": 200, "raw_count": 90, "candidate_count": 30}

            def get_relation_stop_prior(self, _route_uuid):
                return [{"seq": 1}, {"seq": 2}, {"seq": 3}]

        fake = _FakePhase3Client()
        bridge = build_phase_client_executor_bridge(phase3_client=fake)
        state = RunSessionState(
            run_id="run-p3-map",
            pipeline_scope={
                "phases": ["phase3"],
                "phase3": {
                    "bbox": {"south": -0.32, "west": -78.58, "north": -0.09, "east": -78.31},
                    "refs": ["E1"],
                    "service_route_id": "sr-1",
                    "direction_id": 0,
                },
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-map",
        )
        result = bridge["phase3_route_extract"](
            state,
            self.registry[STEP_P3_1_EXTRACT],
            2,
            {"fallback_config": "fallback_2"},
        )

        self.assertTrue(result.ok)
        self.assertTrue(fake.discover_calls)
        discover_call = dict(fake.discover_calls[0] or {})
        self.assertEqual(int(discover_call.get("max_candidates") or 0), 8)
        self.assertEqual(int(discover_call.get("timeout_s") or 0), 90)
        summary = dict(result.summary or {})
        self.assertEqual(dict(summary.get("retry_parameter_applied") or {}).get("fallback_config"), "fallback_2")
        self.assertTrue(list(summary.get("extraction_attempt_records") or []))

    def test_p3_route_extract_emits_candidate_universe_selection_and_fetch_artifacts(self) -> None:
        class _FakePhase3Client:
            def run_step_05_discover(self, **kwargs):
                return {
                    "route_id": str(uuid4()),
                    "chosen_osm_relation_id": 321,
                    "query_strategy": str(kwargs.get("query_strategy") or "bbox_first_broad"),
                    "candidate_universe_summary": {
                        "candidate_universe_count": 18,
                        "candidate_scored_count": 12,
                        "candidate_fetch_evaluated_count": 6,
                        "query_strategy": str(kwargs.get("query_strategy") or "bbox_first_broad"),
                        "hard_filters_applied": [],
                        "soft_signals_used": ["refs", "operator", "name"],
                    },
                    "selection_summary": {
                        "selection_status": "provisional_selected",
                        "selected_osm_relation_id": 321,
                        "selected_rank": 1,
                        "selected_score": 18.4,
                        "selection_confidence": 0.71,
                        "score_gap_top2": 2.9,
                        "selected_relation_stop_prior_count": 7,
                        "selection_reason_codes": ["soft_ref_match"],
                    },
                    "candidate_preview": [
                        {"osm_relation_id": 321, "selection_rank": 1, "selection_confidence": 0.71, "score": 18.4}
                    ],
                    "extractor_diagnostics": {
                        "candidate_count": 18,
                        "signal_strength": "medium",
                        "quality_flags": [],
                    },
                    "extractor_attempts": [{"phase": "primary", "returncode": 0, "error_class": "ok", "candidate_count": 18}],
                    "extractor_fallback_used": False,
                    "extractor_fallback_profile": None,
                }

            def run_step_10_fetch(self, **kwargs):
                return {
                    "http_status": 200,
                    "raw_count": 90,
                    "candidate_count": 30,
                    "candidate_universe_count": 18,
                    "selected_relation_summary": {
                        "osm_relation_id": 321,
                        "selection_rank": 1,
                        "selection_confidence": 0.71,
                        "score": 18.4,
                        "stop_prior_count": 7,
                        "query_strategy": "bbox_first_broad",
                    },
                }

            def get_relation_stop_prior(self, _route_uuid):
                return [{"seq": 1}, {"seq": 2}, {"seq": 3}, {"seq": 4}, {"seq": 5}, {"seq": 6}, {"seq": 7}]

        bridge = build_phase_client_executor_bridge(phase3_client=_FakePhase3Client())
        state = RunSessionState(
            run_id="run-p3-artifacts",
            pipeline_scope={
                "phases": ["phase3"],
                "target_entities": "Conocoto terminal corridor",
                "phase3": {
                    "bbox": {"south": -0.32, "west": -78.58, "north": -0.09, "east": -78.31},
                    "refs": ["E1"],
                    "operator": "Metro",
                    "name": "Ecovia",
                },
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-artifacts",
        )
        result = bridge["phase3_route_extract"](state, self.registry[STEP_P3_1_EXTRACT], 1, {})
        self.assertTrue(result.ok)
        summary = dict(result.summary or {})
        self.assertEqual(str(summary.get("query_strategy") or ""), "bbox_first_broad")
        self.assertEqual(
            int(dict(summary.get("candidate_universe_summary") or {}).get("candidate_universe_count") or 0),
            18,
        )
        self.assertEqual(
            float(dict(summary.get("selection_summary") or {}).get("selection_confidence") or 0.0),
            0.71,
        )
        artifact_types = [str(dict(a or {}).get("artifact_type") or "") for a in list(result.artifacts or [])]
        self.assertIn("phase3_candidate_universe", artifact_types)
        self.assertIn("phase3_selection_summary", artifact_types)
        self.assertIn("phase3_fetch_selected_relation", artifact_types)
        self.assertIn("phase3_geographic_interpretation", artifact_types)
        self.assertTrue(dict(summary.get("spatial_interpretation") or {}))
        self.assertTrue(dict(dict(summary.get("validator_payload") or {}).get("spatial_interpretation") or {}))

    @unittest.skip("depends on the original region's bbox catalog; adapt it to your own region's catalogs (see README, Tests)")
    def test_p3_route_extract_without_scope_bbox_uses_shared_place_resolution_when_available(self) -> None:
        class _FakePhase3Client:
            def run_step_05_discover(self, **kwargs):
                return {
                    "route_id": str(uuid4()),
                    "chosen_osm_relation_id": 444,
                    "query_strategy": str(kwargs.get("query_strategy") or "bbox_first_broad"),
                    "candidate_universe_summary": {
                        "candidate_universe_count": 3,
                        "candidate_scored_count": 3,
                        "candidate_fetch_evaluated_count": 1,
                        "query_strategy": str(kwargs.get("query_strategy") or "bbox_first_broad"),
                        "hard_filters_applied": [],
                        "soft_signals_used": ["name"],
                    },
                    "selection_summary": {
                        "selection_status": "provisional_selected",
                        "selected_osm_relation_id": 444,
                        "selected_rank": 1,
                        "selected_score": 9.5,
                        "selection_confidence": 0.42,
                        "score_gap_top2": 0.4,
                        "selected_relation_stop_prior_count": 2,
                        "selection_reason_codes": ["soft_name_match"],
                    },
                    "candidate_preview": [{"osm_relation_id": 444}],
                    "extractor_diagnostics": {"candidate_count": 3, "signal_strength": "low", "quality_flags": []},
                    "extractor_attempts": [{"phase": "primary", "returncode": 0, "error_class": "ok", "candidate_count": 3}],
                }

            def run_step_10_fetch(self, **kwargs):
                return {"http_status": 200, "raw_count": 25, "candidate_count": 4}

            def get_relation_stop_prior(self, _route_uuid):
                return [{"seq": 1}]

        bridge = build_phase_client_executor_bridge(phase3_client=_FakePhase3Client())
        state = RunSessionState(
            run_id="run-p3-fallback",
            pipeline_scope={
                "phases": ["phase3"],
                "target_entities": "Conocoto",
                "phase3": {"name": "Conocoto loop"},
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-fallback",
        )
        result = bridge["phase3_route_extract"](state, self.registry[STEP_P3_1_EXTRACT], 1, {})
        self.assertTrue(result.ok)
        spatial = dict(dict(result.summary or {}).get("spatial_interpretation") or {})
        self.assertEqual(str(spatial.get("spatial_interpretation_status") or ""), "ok")
        self.assertFalse(bool(spatial.get("fallback_used")))
        self.assertIn(
            str(spatial.get("spatial_interpretation_source") or ""),
            {"bbox_catalog", "phase1_catalog", "sector_catalog", "sector_catalog_alias", "runtime_phase3_scope", "shared_geography_bbox"},
        )
        self.assertTrue(dict(spatial.get("runtime_bbox_used") or {}))

    def test_extraction_attempt_record_normalization_phase1(self) -> None:
        class _FakePhase1Client:
            def run_step_build_node_set_tuned(self, **kwargs):
                return {
                    "ok": True,
                    "best": {
                        "node_set_id": "node-set-x",
                        "action_id": "stops_broad_bbox",
                        "quality_score": 0.70,
                        "metrics": {"raw_count": 88, "candidate_count": 21},
                    },
                    "attempts": [
                        {
                            "action_id": "stops_broad_bbox",
                            "bbox": dict(kwargs.get("bbox") or {}),
                            "retry_strategy": "bbox_expand",
                            "retry_reason": "quality_below_threshold",
                            "retry_parameter_delta": {"bbox_buffer_ratio_to": 0.25},
                            "node_set_id": "node-set-x",
                            "run_id": "run-x",
                            "area_group": "valle_core",
                            "sector": "demo",
                            "quality_score": 0.70,
                            "metrics": {"raw_count": 88, "candidate_count": 21},
                        }
                    ],
                }

        bridge = build_phase_client_executor_bridge(phase1_client=_FakePhase1Client())
        state = RunSessionState(
            run_id="run-p1-attempt",
            pipeline_scope={"phases": ["phase1"], "phase1": {"extract": {"bbox": {"south": -0.3, "west": -78.5, "north": -0.1, "east": -78.3}}}},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-attempt",
        )
        result = bridge["phase1_extract"](state, self.registry[STEP_P1_1_EXTRACT], 1, {})
        records = list(dict(result.summary or {}).get("extraction_attempt_records") or [])
        self.assertTrue(records)
        first = dict(records[0] or {})
        for key in (
            "phase",
            "step_id",
            "attempt_no",
            "run_id",
            "trace_id",
            "bbox_used",
            "bbox_fingerprint",
            "retry_strategy",
            "retry_reason",
            "retry_parameter_delta",
            "action_or_template_used",
            "fallback_config_used",
            "status",
            "response_size_bytes",
            "raw_elements_count",
            "candidate_count",
            "extractor_diagnostics_summary",
            "effective_config_fingerprint",
            "attempt_changed_from_previous",
        ):
            self.assertIn(key, first)

    def test_extraction_attempt_record_normalization_phase3(self) -> None:
        class _FakePhase3Client:
            def run_step_05_discover(self, **kwargs):
                return {
                    "route_id": str(uuid4()),
                    "chosen_osm_relation_id": 321,
                    "extractor_diagnostics": {"candidate_count": 4, "signal_strength": "low", "quality_flags": []},
                    "extractor_attempts": [
                        {"phase": "primary", "overpass_url": "u1", "returncode": 1, "error_class": "timeout"},
                        {"phase": "timeout_fallback", "overpass_url": "u2", "returncode": 0, "error_class": "ok"},
                    ],
                    "extractor_fallback_used": True,
                    "extractor_fallback_profile": {"trigger": "upstream_timeout_504"},
                }

            def run_step_10_fetch(self, **kwargs):
                return {"http_status": 200, "raw_count": 70, "candidate_count": 18}

            def get_relation_stop_prior(self, _route_uuid):
                return [{"seq": 1}, {"seq": 2}]

        bridge = build_phase_client_executor_bridge(phase3_client=_FakePhase3Client())
        state = RunSessionState(
            run_id="run-p3-attempt",
            pipeline_scope={"phases": ["phase3"], "phase3": {"bbox": {"south": -0.3, "west": -78.6, "north": -0.1, "east": -78.3}}},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-attempt",
        )
        result = bridge["phase3_route_extract"](state, self.registry[STEP_P3_1_EXTRACT], 1, {})
        records = list(dict(result.summary or {}).get("extraction_attempt_records") or [])
        self.assertGreaterEqual(len(records), 2)
        first = dict(records[0] or {})
        for key in (
            "phase",
            "step_id",
            "attempt_no",
            "run_id",
            "trace_id",
            "bbox_fingerprint",
            "route_id",
            "route_context_fingerprint",
            "retry_strategy",
            "retry_parameter_delta",
            "fallback_profile_used",
            "status",
            "timeout_flag",
            "response_size_bytes",
            "extractor_diagnostics_summary",
            "effective_config_fingerprint",
            "attempt_changed_from_previous",
        ):
            self.assertIn(key, first)

    def test_extraction_validator_payload_contract_phase1(self) -> None:
        validators = build_phase_client_validator_bridge()
        exec_result = ExecutorResult(
            ok=True,
            summary={
                "validator_payload": {
                    "candidate_count": 14,
                    "quality_score": 0.72,
                    "extractor_attempts_summary": {"attempt_count": 2, "diversified_attempts": True},
                    "retry_diversified": True,
                    "recommended_action": "continue_or_review",
                    "extraction_outcome_classification": "success",
                },
                "extraction_attempt_records": [{"status": "success"}],
            },
            artifacts=[],
        )
        out = validators["phase1_extract"](
            RunSessionState(
                run_id="r-v1",
                pipeline_scope={"phases": ["phase1"]},
                policy_profile=PolicyProfile.BALANCED.value,
                status="running",
                current_phase="phase1",
                current_step_id=STEP_P1_1_EXTRACT,
            ),
            self.registry[STEP_P1_1_EXTRACT],
            1,
            exec_result,
        )
        self.assertEqual(out.status, "pass")
        evidence = dict(out.evidence or {})
        for key in (
            "candidate_count",
            "quality_score",
            "extractor_attempts_summary",
            "extractor_attempt_history_summary",
            "retry_diversified",
            "recommended_action",
            "extraction_outcome_classification",
        ):
            self.assertIn(key, evidence)

    def test_extraction_validator_payload_contract_phase3(self) -> None:
        validators = build_phase_client_validator_bridge()
        exec_result = ExecutorResult(
            ok=True,
            summary={
                "validator_payload": {
                    "route_id": str(uuid4()),
                    "prior_stop_count": 6,
                    "discover_candidate_count": 5,
                    "discover_signal_strength": "medium",
                    "discover_quality_flags": [],
                    "extractor_attempts_summary": {"attempt_count": 2, "diversified_attempts": True},
                    "retry_diversified": True,
                    "fallback_profile_used": "fallback_2",
                    "recommended_action": "continue_or_review",
                    "extraction_outcome_classification": "success",
                },
                "extraction_attempt_records": [{"status": "success"}],
            },
            artifacts=[],
        )
        out = validators["phase3_route_extract"](
            RunSessionState(
                run_id="r-v3",
                pipeline_scope={"phases": ["phase3"]},
                policy_profile=PolicyProfile.BALANCED.value,
                status="running",
                current_phase="phase3",
                current_step_id=STEP_P3_1_EXTRACT,
            ),
            self.registry[STEP_P3_1_EXTRACT],
            1,
            exec_result,
        )
        self.assertEqual(out.status, "pass")
        evidence = dict(out.evidence or {})
        for key in (
            "route_id",
            "prior_stop_count",
            "discover_candidate_count",
            "discover_signal_strength",
            "extractor_attempts_summary",
            "extractor_attempt_history_summary",
            "retry_diversified",
            "fallback_profile_used",
            "recommended_action",
            "extraction_outcome_classification",
        ):
            self.assertIn(key, evidence)
        self.assertIn("candidate_universe_summary", evidence)
        self.assertIn("selection_summary", evidence)

    def test_p3_normalized_snapshot_includes_route_extractor_packet(self) -> None:
        class _NoopAdvisoryService:
            def run_task_detailed(self, endpoint_task, envelope):
                del endpoint_task, envelope
                return {"response": {}, "meta": {}}

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_NoopAdvisoryService())
        state = RunSessionState(
            run_id="run-p3-packet",
            pipeline_scope={
                "phases": ["phase3"],
                "phase3": {
                    "bbox": {"south": -0.32, "west": -78.58, "north": -0.09, "east": -78.31},
                    "refs": ["E1"],
                    "operator": "Metro",
                    "name": "Ecovia",
                    "service_route_id": "sr-1",
                    "direction_id": 0,
                    "query_strategy": "bbox_first_broad",
                },
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-packet",
            resume_context={
                "latest_step20_summary": {
                    "matched_count": 6,
                    "unmatched_count": 2,
                    "ambiguous_count": 1,
                    "sequence_quality_score": 71.0,
                    "sequence_gate_pass": False,
                    "blocker_origin_hint": "matching_ambiguity",
                }
            },
        )
        snapshot = interpreter._build_normalized_snapshot(  # noqa: SLF001
            state=state,
            step=self.registry[STEP_P3_1_EXTRACT],
            attempt_no=1,
            exec_result=ExecutorResult(
                ok=True,
                summary={
                    "route_id": str(uuid4()),
                    "prior_stop_count": 7,
                    "query_strategy": "bbox_first_broad",
                    "spatial_interpretation": {
                        "original_geographic_input": "Conocoto terminal corridor",
                        "geographic_input_type": "place_or_route_hint",
                        "interpreted_place_meaning": "Conocoto terminal corridor",
                        "geographic_interpretation_source": "runtime_phase3_scope",
                        "spatial_interpretation_status": "ok",
                        "bbox_candidate": {"south": -0.32, "west": -78.58, "north": -0.09, "east": -78.31},
                        "bbox_candidate_confidence": 0.82,
                        "bbox_validation_status": "valid",
                        "runtime_bbox_used": {"south": -0.32, "west": -78.58, "north": -0.09, "east": -78.31},
                        "effective_bbox_fingerprint": "bbox-fp-1",
                        "fallback_used": False,
                        "fallback_reason": None,
                        "runtime_spatial_strategy_used": "bbox_first_broad",
                        "target_option_received": True,
                        "target_option_text": "Conocoto terminal corridor",
                    },
                    "candidate_universe_summary": {
                        "candidate_universe_count": 18,
                        "candidate_scored_count": 12,
                        "candidate_fetch_evaluated_count": 6,
                        "query_strategy": "bbox_first_broad",
                        "hard_filters_applied": [],
                        "soft_signals_used": ["refs", "operator", "name"],
                    },
                    "selection_summary": {
                        "selection_status": "provisional_selected",
                        "selected_osm_relation_id": 321,
                        "selected_rank": 1,
                        "selected_score": 18.4,
                        "selection_confidence": 0.71,
                        "score_gap_top2": 2.9,
                        "selected_relation_stop_prior_count": 7,
                        "selection_reason_codes": ["soft_ref_match"],
                    },
                    "selected_relation_summary": {
                        "osm_relation_id": 321,
                        "selection_rank": 1,
                        "selection_confidence": 0.71,
                        "score": 18.4,
                        "stop_prior_count": 7,
                    },
                    "extractor_diagnostics": {"candidate_count": 18, "signal_strength": "medium", "quality_flags": []},
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 2,
                        "successful_attempt_count": 1,
                        "non_empty_attempt_count": 1,
                        "retry_diversity_count": 2,
                        "same_config_repeat_count": 0,
                        "diversified_attempts": True,
                        "attempts": [
                            {"attempt_no": 1, "status": "error", "effective_config_fingerprint": "fp1"},
                            {"attempt_no": 2, "status": "success", "effective_config_fingerprint": "fp2"},
                        ],
                    },
                },
                artifacts=[],
            ),
            validator_result=ValidatorResult(
                status="warning",
                gate_passed=True,
                evidence={
                    "route_id": "r-1",
                    "prior_stop_count": 7,
                    "discover_candidate_count": 18,
                    "discover_signal_strength": "medium",
                    "candidate_universe_summary": {
                        "candidate_universe_count": 18,
                        "query_strategy": "bbox_first_broad",
                    },
                    "selection_summary": {
                        "selection_confidence": 0.71,
                    },
                },
            ),
            ai_snapshot=AIBotSnapshot(
                extractor_status={},
                extractor_help_needed={},
            ),
            trigger="warning",
        )
        packet = dict(snapshot.get("phase3_route_extractor_packet") or {})
        self.assertTrue(packet)
        self.assertEqual(int(dict(packet.get("candidate_universe") or {}).get("count_total") or 0), 18)
        self.assertEqual(float(dict(packet.get("candidate_universe") or {}).get("selection_confidence") or 0.0), 0.71)
        self.assertEqual(str(dict(packet.get("selection") or {}).get("selected_osm_relation_id") or ""), "321")
        self.assertEqual(str(dict(packet.get("scope") or {}).get("query_strategy") or ""), "bbox_first_broad")
        self.assertEqual(str(dict(packet.get("step20_bridge") or {}).get("blocker_origin_hint") or ""), "matching_ambiguity")
        self.assertEqual(str(dict(packet.get("scope") or {}).get("original_geographic_input") or ""), "Conocoto terminal corridor")
        self.assertEqual(str(dict(packet.get("scope") or {}).get("effective_bbox_fingerprint") or ""), "bbox-fp-1")
        geo = dict(snapshot.get("geographic_context") or {})
        self.assertEqual(str(geo.get("original_geographic_input") or ""), "Conocoto terminal corridor")
        self.assertEqual(str(geo.get("geographic_interpretation_status") or ""), "ok")
        self.assertEqual(str(geo.get("runtime_spatial_strategy_used") or ""), "bbox_first_broad")
        self.assertEqual(bool(geo.get("fallback_used")), False)


    def test_p14_promote_snapshot_includes_workspace_state_summary_keys(self) -> None:
        class _FakePhase1Client:
            def get_promote_dry_run(self, node_set_id):
                return {
                    "node_set_id": str(node_set_id),
                    "n_resolved": 0,
                    "n_approved": 0,
                    "n_rejected": 3,
                    "status": "no_approved_nodes",
                    "legacy_status": "not_found",
                    "promote_precondition": "no_approved_nodes",
                    "workspace_state_summary": {
                        "node_set_id": str(node_set_id),
                        "resolved_total": 1652,
                        "approved_count": 0,
                        "rejected_count": 12,
                        "pending_review_count": 1640,
                    },
                    "recommended_action": "approve_nodes_before_promote",
                    "diagnostics_hint": "No approved nodes yet in workspace; approve nodes before promote.",
                }

        bridge = build_phase_client_executor_bridge(phase1_client=_FakePhase1Client())
        state = RunSessionState(
            run_id="run-p14-snapshot",
            pipeline_scope={"phases": ["phase1"], "phase1": {"node_set_id": "ns-p14"}},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_4_PROMOTE,
            trace_id="trace-p14-snapshot",
        )
        out = bridge["phase1_promote_prepare"](state, self.registry[STEP_P1_4_PROMOTE], 1, {})
        summary = dict(out.summary or {})
        workspace = dict(summary.get("workspace_state_summary") or {})
        self.assertEqual(
            set(workspace.keys()),
            {"node_set_id", "resolved_total", "approved_count", "rejected_count", "pending_review_count"},
        )
        self.assertLess(len(json.dumps(workspace)), 300)
        payload = dict(summary.get("validator_payload") or {})
        self.assertIn("workspace_state_summary", payload)
        self.assertEqual(payload.get("promote_status"), "no_approved_nodes")
        self.assertEqual(payload.get("legacy_status"), "not_found")
        self.assertEqual(payload.get("promote_precondition"), "no_approved_nodes")
        self.assertEqual(payload.get("recommended_action"), "approve_nodes_before_promote")
        self.assertLessEqual(len(payload.keys()), 8)

    def test_p14_validator_uses_approve_nodes_before_promote_when_approved_zero(self) -> None:
        validators = build_phase_client_validator_bridge()
        exec_result = ExecutorResult(
            ok=True,
            summary={
                "promote_dry_run": {
                    "node_set_id": "ns-p14-approve",
                    "n_resolved": 0,
                    "n_approved": 0,
                    "status": "no_approved_nodes",
                    "legacy_status": "not_found",
                    "promote_precondition": "no_approved_nodes",
                    "workspace_state_summary": {
                        "node_set_id": "ns-p14-approve",
                        "resolved_total": 1652,
                        "approved_count": 0,
                        "rejected_count": 8,
                        "pending_review_count": 1644,
                    },
                },
                "validator_payload": {
                    "ready_for_approval": True,
                    "n_resolved": 0,
                    "promote_status": "no_approved_nodes",
                    "legacy_status": "not_found",
                    "promote_precondition": "no_approved_nodes",
                    "workspace_state_summary": {
                        "node_set_id": "ns-p14-approve",
                        "resolved_total": 1652,
                        "approved_count": 0,
                        "rejected_count": 8,
                        "pending_review_count": 1644,
                    },
                },
            },
            artifacts=[],
        )
        out = validators["phase1_promote_prepare"](
            RunSessionState(
                run_id="r-p14-approve",
                pipeline_scope={"phases": ["phase1"]},
                policy_profile=PolicyProfile.BALANCED.value,
                status="running",
                current_phase="phase1",
                current_step_id=STEP_P1_4_PROMOTE,
            ),
            self.registry[STEP_P1_4_PROMOTE],
            1,
            exec_result,
        )
        self.assertEqual(out.status, "blocked")
        self.assertEqual(out.block_reason_code, BlockReasonCode.NO_APPROVED_NODES_FOR_PROMOTE)
        self.assertEqual(out.summary, "Promote cannot proceed because no nodes have been approved yet.")
        self.assertEqual(out.recommended_action, "approve_nodes_before_promote")
        self.assertEqual(dict(out.evidence or {}).get("promote_precondition"), "no_approved_nodes")

    def test_p14_validator_uses_inspect_promote_lookup_when_approved_exists_but_zero(self) -> None:
        validators = build_phase_client_validator_bridge()
        exec_result = ExecutorResult(
            ok=True,
            summary={
                "promote_dry_run": {
                    "node_set_id": "ns-p14-lookup",
                    "n_resolved": 0,
                    "n_approved": 12,
                    "status": "staging_missing",
                    "legacy_status": "not_found",
                    "promote_precondition": "lookup_or_staging_issue",
                    "diagnostics_hint": "inspect joins",
                    "workspace_state_summary": {
                        "node_set_id": "ns-p14-lookup",
                        "resolved_total": 1652,
                        "approved_count": 12,
                        "rejected_count": 4,
                        "pending_review_count": 1636,
                    },
                },
                "validator_payload": {
                    "ready_for_approval": True,
                    "n_resolved": 0,
                    "promote_status": "staging_missing",
                    "legacy_status": "not_found",
                    "promote_precondition": "lookup_or_staging_issue",
                    "diagnostics_hint": "inspect joins",
                    "workspace_state_summary": {
                        "node_set_id": "ns-p14-lookup",
                        "resolved_total": 1652,
                        "approved_count": 12,
                        "rejected_count": 4,
                        "pending_review_count": 1636,
                    },
                },
            },
            artifacts=[],
        )
        out = validators["phase1_promote_prepare"](
            RunSessionState(
                run_id="r-p14-lookup",
                pipeline_scope={"phases": ["phase1"]},
                policy_profile=PolicyProfile.BALANCED.value,
                status="running",
                current_phase="phase1",
                current_step_id=STEP_P1_4_PROMOTE,
            ),
            self.registry[STEP_P1_4_PROMOTE],
            1,
            exec_result,
        )
        self.assertEqual(out.status, "blocked")
        self.assertEqual(out.block_reason_code, BlockReasonCode.PROMOTE_LOOKUP_EMPTY)
        self.assertEqual(out.recommended_action, "inspect_promote_lookup")
        self.assertIn("diagnostics_hint", dict(out.evidence or {}))
        self.assertEqual(out.summary, "Promote dry-run returned zero after approved nodes exist.")

    def test_p14_validator_uses_promote_node_set_missing_when_node_set_lookup_missing(self) -> None:
        validators = build_phase_client_validator_bridge()
        exec_result = ExecutorResult(
            ok=True,
            summary={
                "promote_dry_run": {
                    "node_set_id": "ns-p14-missing-node-set",
                    "n_resolved": 0,
                    "n_approved": 0,
                    "status": "node_set_missing",
                    "legacy_status": "not_found",
                    "promote_precondition": "node_set_missing",
                    "workspace_state_summary": {
                        "node_set_id": "ns-p14-missing-node-set",
                        "resolved_total": 0,
                        "approved_count": 0,
                        "rejected_count": 0,
                        "pending_review_count": 0,
                    },
                },
                "validator_payload": {
                    "ready_for_approval": False,
                    "n_resolved": 0,
                    "promote_status": "node_set_missing",
                    "legacy_status": "not_found",
                    "promote_precondition": "node_set_missing",
                    "workspace_state_summary": {
                        "node_set_id": "ns-p14-missing-node-set",
                        "resolved_total": 0,
                        "approved_count": 0,
                        "rejected_count": 0,
                        "pending_review_count": 0,
                    },
                },
            },
            artifacts=[],
        )
        out = validators["phase1_promote_prepare"](
            RunSessionState(
                run_id="r-p14-node-set-missing",
                pipeline_scope={"phases": ["phase1"]},
                policy_profile=PolicyProfile.BALANCED.value,
                status="running",
                current_phase="phase1",
                current_step_id=STEP_P1_4_PROMOTE,
            ),
            self.registry[STEP_P1_4_PROMOTE],
            1,
            exec_result,
        )
        self.assertEqual(out.status, "blocked")
        self.assertEqual(out.block_reason_code, BlockReasonCode.PROMOTE_NODE_SET_MISSING)
        self.assertEqual(out.recommended_action, "inspect_promote_lookup")
        self.assertEqual(out.summary, "Promote dry-run node set lookup returned missing.")

    def test_p14_promote_missing_workspace_state_summary_sets_consistency_candidate(self) -> None:
        candidates = _handoff_build_consistency_candidates(
            step_id=STEP_P1_4_PROMOTE,
            exec_summary={
                "promote_dry_run": {"node_set_id": "ns-p14-missing", "n_resolved": 0},
                "validator_payload": {"n_resolved": 0},
            },
            validator_result={
                "status": "blocked",
                "block_reason_code": BlockReasonCode.RESOLVE_ZERO_RESULTS.value,
                "evidence": {"n_resolved": 0},
            },
            ai_snapshot={
                "proposals": {
                    "comparison": {
                        "latest": {"resolved_count": 19},
                    }
                }
            },
        )
        codes = {str(dict(row or {}).get("code") or "") for row in candidates}
        self.assertIn("P1_PROMOTE_MISSING_WORKSPACE_STATE_SUMMARY", codes)

    def test_p14_snapshot_marks_missing_workspace_state_summary_insufficient_flag(self) -> None:
        class _StubAdvisoryService:
            def __init__(self) -> None:
                self.calls = []

            def run_task_detailed(self, *, endpoint_task, envelope):
                self.calls.append({"endpoint_task": endpoint_task, "envelope": dict(envelope or {})})
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {
                            "contradictions_found": True,
                            "contradictions": [
                                {
                                    "code": "P1_PROMOTE_MISSING_WORKSPACE_STATE_SUMMARY",
                                    "severity": "high",
                                    "details": "workspace summary missing",
                                    "likely_implication": "validator_mapping_bug",
                                }
                            ],
                            "consistency_summary": "missing workspace summary",
                        },
                        "meta": {"task": endpoint_task},
                    }
                return {
                    "response": {
                        "summary": "generic",
                        "dominant_cause_class": "unknown",
                        "confidence": 0.4,
                        "secondary_causes": [],
                        "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                        "recommended_branch": "manual_review_required",
                        "recommended_next_actions": ["review"],
                        "patch_task_recommendation": {
                            "should_create_patch_task": False,
                            "patch_type": None,
                            "justification": None,
                            "suggested_target": None,
                        },
                        "operator_action_required": False,
                        "approval_type_if_needed": None,
                        "risk_notes": [],
                    },
                    "meta": {"task": endpoint_task},
                }

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_StubAdvisoryService())
        step = self.registry[STEP_P1_4_PROMOTE]
        state = RunSessionState(
            run_id="run-p14-flags",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_4_PROMOTE,
            trace_id="trace-p14-flags",
        )
        _ = interpreter(
            state,
            step,
            1,
            ExecutorResult(
                ok=True,
                summary={
                    "promote_dry_run": {"node_set_id": "ns-p14-flag", "n_resolved": 0},
                    "validator_payload": {"n_resolved": 0},
                },
                artifacts=[],
            ),
            ValidatorResult(
                status="blocked",
                gate_passed=False,
                block_reason_code=BlockReasonCode.RESOLVE_ZERO_RESULTS,
                evidence={"n_resolved": 0, "resolved_total": 12},
            ),
            AIBotSnapshot(scores={"quality": 0.4}, metrics={}),
            "blocked",
        )
        calls = list(getattr(interpreter._service, "calls", []))
        interp_calls = [c for c in calls if str(c.get("endpoint_task")) == "hades_pipeline_interpreter"]
        self.assertTrue(interp_calls)
        snap = dict((interp_calls[-1].get("envelope") or {}).get("snapshot") or {})
        flags = set(str(x or "") for x in list(snap.get("insufficient_data_flags") or []))
        self.assertIn("missing_workspace_state_summary", flags)

    def test_p14_snapshot_exposes_promote_context_fields(self) -> None:
        class _StubAdvisoryService:
            def __init__(self) -> None:
                self.calls = []

            def run_task_detailed(self, *, endpoint_task, envelope):
                self.calls.append({"endpoint_task": endpoint_task, "envelope": dict(envelope or {})})
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {
                            "contradictions_found": False,
                            "contradictions": [],
                            "consistency_summary": "ok",
                        },
                        "meta": {"task": endpoint_task},
                    }
                return {
                    "response": {
                        "summary": "generic",
                        "dominant_cause_class": "unknown",
                        "confidence": 0.4,
                        "secondary_causes": [],
                        "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                        "recommended_branch": "manual_review_required",
                        "recommended_next_actions": ["review"],
                        "patch_task_recommendation": {
                            "should_create_patch_task": False,
                            "patch_type": None,
                            "justification": None,
                            "suggested_target": None,
                        },
                        "operator_action_required": False,
                        "approval_type_if_needed": None,
                        "risk_notes": [],
                    },
                    "meta": {"task": endpoint_task},
                }

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_StubAdvisoryService())
        step = self.registry[STEP_P1_4_PROMOTE]
        state = RunSessionState(
            run_id="run-p14-promote-context",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_4_PROMOTE,
            trace_id="trace-p14-promote-context",
        )
        _ = interpreter(
            state,
            step,
            1,
            ExecutorResult(
                ok=True,
                summary={
                    "promote_dry_run": {
                        "node_set_id": "ns-p14-context",
                        "n_resolved": 0,
                        "n_approved": 0,
                        "status": "no_approved_nodes",
                        "legacy_status": "not_found",
                        "promote_precondition": "no_approved_nodes",
                        "recommended_action": "approve_nodes_before_promote",
                        "diagnostics_hint": "No approved nodes yet in workspace; approve nodes before promote.",
                        "workspace_state_summary": {
                            "node_set_id": "ns-p14-context",
                            "resolved_total": 1652,
                            "approved_count": 0,
                            "rejected_count": 12,
                            "pending_review_count": 1640,
                        },
                    },
                    "validator_payload": {
                        "ready_for_approval": False,
                        "n_resolved": 0,
                        "promote_status": "no_approved_nodes",
                        "legacy_status": "not_found",
                        "promote_precondition": "no_approved_nodes",
                        "recommended_action": "approve_nodes_before_promote",
                        "diagnostics_hint": "No approved nodes yet in workspace; approve nodes before promote.",
                        "workspace_state_summary": {
                            "node_set_id": "ns-p14-context",
                            "resolved_total": 1652,
                            "approved_count": 0,
                            "rejected_count": 12,
                            "pending_review_count": 1640,
                        },
                    },
                },
                artifacts=[],
            ),
            ValidatorResult(
                status="blocked",
                gate_passed=False,
                block_reason_code=BlockReasonCode.NO_APPROVED_NODES_FOR_PROMOTE,
                evidence={
                    "promote_status": "no_approved_nodes",
                    "promote_precondition": "no_approved_nodes",
                    "workspace_state_summary": {
                        "node_set_id": "ns-p14-context",
                        "resolved_total": 1652,
                        "approved_count": 0,
                        "rejected_count": 12,
                        "pending_review_count": 1640,
                    },
                    "resolved_total": 1652,
                    "approved_count": 0,
                    "pending_review_count": 1640,
                    "recommended_action": "approve_nodes_before_promote",
                    "diagnostics_hint": "No approved nodes yet in workspace; approve nodes before promote.",
                },
            ),
            AIBotSnapshot(),
            "blocked",
        )
        calls = list(getattr(interpreter._service, "calls", []))
        interp_calls = [c for c in calls if str(c.get("endpoint_task")) == "hades_pipeline_interpreter"]
        self.assertTrue(interp_calls)
        snap = dict((interp_calls[-1].get("envelope") or {}).get("snapshot") or {})
        promote_context = dict(snap.get("promote_context") or {})
        self.assertEqual(promote_context.get("promote_status"), "no_approved_nodes")
        self.assertEqual(promote_context.get("legacy_status"), "not_found")
        self.assertEqual(promote_context.get("promote_precondition"), "no_approved_nodes")
        self.assertEqual(promote_context.get("approved_count"), 0)
        self.assertEqual(promote_context.get("pending_review_count"), 1640)
        self.assertEqual(promote_context.get("resolved_total"), 1652)
        self.assertEqual(promote_context.get("recommended_action"), "approve_nodes_before_promote")
        self.assertIn("workspace_state_summary", promote_context)

    def test_attempt_history_summary_generated_and_bounded(self) -> None:
        class _FakePhase1Client:
            def run_step_build_node_set_tuned(self, **kwargs):
                attempts = []
                for idx in range(12):
                    attempts.append(
                        {
                            "action_id": "stops_broad_bbox" if idx < 6 else "platforms_bbox",
                            "bbox": dict(kwargs.get("bbox") or {}),
                            "retry_strategy": "bbox_expand" if idx < 6 else "template_alternative",
                            "retry_reason": "quality_below_threshold",
                            "retry_parameter_delta": {"idx": idx},
                            "node_set_id": f"ns-{idx}",
                            "metrics": {"raw_count": 10 + idx, "candidate_count": 1 + (idx % 3), "runtime_ms": 100 + idx},
                        }
                    )
                return {
                    "ok": True,
                    "best": {
                        "node_set_id": "ns-best",
                        "action_id": "stops_broad_bbox",
                        "quality_score": 0.75,
                        "metrics": {"raw_count": 120, "candidate_count": 42},
                    },
                    "attempts": attempts,
                }

        bridge = build_phase_client_executor_bridge(phase1_client=_FakePhase1Client())
        state = RunSessionState(
            run_id="run-p1-bounded",
            pipeline_scope={"phases": ["phase1"], "phase1": {"extract": {"bbox": {"south": -0.3, "west": -78.5, "north": -0.1, "east": -78.3}}}},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-bounded",
        )
        result = bridge["phase1_extract"](state, self.registry[STEP_P1_1_EXTRACT], 1, {})
        hist = dict((dict(result.summary or {})).get("extraction_attempt_history_summary") or {})
        self.assertEqual(int(hist.get("attempt_count_total") or 0), 12)
        self.assertLessEqual(len(list(hist.get("attempts") or [])), 8)
        self.assertLessEqual(len(list(hist.get("effective_config_fingerprints") or [])), 12)
        self.assertTrue(bool(hist.get("attempts_truncated")))

    def test_attempt_history_summary_computes_repeat_count_and_strategies(self) -> None:
        class _FakePhase1Client:
            def run_step_build_node_set_tuned(self, **kwargs):
                bbox = dict(kwargs.get("bbox") or {})
                return {
                    "ok": True,
                    "best": {
                        "node_set_id": "ns-best",
                        "action_id": "stops_broad_bbox",
                        "quality_score": 0.69,
                        "metrics": {"raw_count": 60, "candidate_count": 12},
                    },
                    "attempts": [
                        {
                            "action_id": "stops_broad_bbox",
                            "bbox": bbox,
                            "retry_strategy": "bbox_expand",
                            "retry_parameter_delta": {"pct": 10},
                            "node_set_id": "ns-1",
                            "metrics": {"raw_count": 10, "candidate_count": 2},
                        },
                        {
                            "action_id": "stops_broad_bbox",
                            "bbox": bbox,
                            "retry_strategy": "bbox_expand",
                            "retry_parameter_delta": {"pct": 10},
                            "node_set_id": "ns-2",
                            "metrics": {"raw_count": 11, "candidate_count": 3},
                        },
                        {
                            "action_id": "platforms_bbox",
                            "bbox": bbox,
                            "retry_strategy": "template_alternative",
                            "retry_parameter_delta": {"variant": "alternative_2"},
                            "node_set_id": "ns-3",
                            "metrics": {"raw_count": 12, "candidate_count": 4},
                        },
                    ],
                }

        bridge = build_phase_client_executor_bridge(phase1_client=_FakePhase1Client())
        state = RunSessionState(
            run_id="run-p1-repeat",
            pipeline_scope={"phases": ["phase1"], "phase1": {"extract": {"bbox": {"south": -0.3, "west": -78.5, "north": -0.1, "east": -78.3}}}},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-repeat",
        )
        result = bridge["phase1_extract"](state, self.registry[STEP_P1_1_EXTRACT], 1, {})
        hist = dict((dict(result.summary or {})).get("extraction_attempt_history_summary") or {})
        self.assertEqual(int(hist.get("same_config_repeat_count") or 0), 1)
        strategies = list(hist.get("retry_strategies_attempted") or [])
        self.assertIn("bbox_expand", strategies)
        self.assertIn("template_alternative", strategies)

    def test_executor_summary_phase1_includes_attempt_history_summary(self) -> None:
        class _FakePhase1Client:
            def run_step_build_node_set_tuned(self, **kwargs):
                return {
                    "ok": True,
                    "best": {
                        "node_set_id": "ns-history",
                        "action_id": "stops_broad_bbox",
                        "quality_score": 0.73,
                        "metrics": {"raw_count": 40, "candidate_count": 10},
                    },
                    "attempts": [],
                }

        bridge = build_phase_client_executor_bridge(phase1_client=_FakePhase1Client())
        state = RunSessionState(
            run_id="run-p1-history",
            pipeline_scope={"phases": ["phase1"], "phase1": {"extract": {"bbox": {"south": -0.3, "west": -78.5, "north": -0.1, "east": -78.3}}}},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-history",
        )
        result = bridge["phase1_extract"](state, self.registry[STEP_P1_1_EXTRACT], 1, {})
        summary = dict(result.summary or {})
        self.assertIn("extraction_attempt_history_summary", summary)
        self.assertIn("extraction_attempts_summary", summary)
        hist = dict(summary.get("extraction_attempt_history_summary") or {})
        self.assertEqual(int(hist.get("attempt_count_total") or 0), 1)
        self.assertEqual(len(list(hist.get("attempts") or [])), 1)
        self.assertEqual(int(hist.get("best_attempt_index") or 0), 1)

    def test_executor_summary_phase3_includes_attempt_history_summary(self) -> None:
        class _FakePhase3Client:
            def run_step_05_discover(self, **kwargs):
                return {
                    "route_id": str(uuid4()),
                    "chosen_osm_relation_id": 321,
                    "extractor_diagnostics": {"candidate_count": 4, "signal_strength": "low", "quality_flags": []},
                    "extractor_attempts": [{"phase": "primary", "returncode": 0, "error_class": "ok"}],
                    "extractor_fallback_used": False,
                    "extractor_fallback_profile": None,
                }

            def run_step_10_fetch(self, **kwargs):
                return {"http_status": 200, "raw_count": 70, "candidate_count": 18}

            def get_relation_stop_prior(self, _route_uuid):
                return [{"seq": 1}, {"seq": 2}]

        bridge = build_phase_client_executor_bridge(phase3_client=_FakePhase3Client())
        state = RunSessionState(
            run_id="run-p3-history",
            pipeline_scope={"phases": ["phase3"], "phase3": {"bbox": {"south": -0.3, "west": -78.6, "north": -0.1, "east": -78.3}}},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-history",
        )
        result = bridge["phase3_route_extract"](state, self.registry[STEP_P3_1_EXTRACT], 1, {})
        summary = dict(result.summary or {})
        self.assertIn("extraction_attempt_history_summary", summary)
        self.assertIn("extraction_attempts_summary", summary)

    def test_persistence_compatibility_smoke_with_stage1_instrumentation(self) -> None:
        class _FakePhase1Client:
            def run_step_build_node_set_tuned(self, **kwargs):
                return {
                    "ok": True,
                    "best": {
                        "node_set_id": "ns-serial",
                        "action_id": "stops_broad_bbox",
                        "quality_score": 0.72,
                        "metrics": {"raw_count": 30, "candidate_count": 8},
                    },
                    "attempts": [],
                }

        engine = SupervisedPipelineAutopilot(
            step_registry=self.registry,
            executors=build_phase_client_executor_bridge(phase1_client=_FakePhase1Client()),
            validators=build_phase_client_validator_bridge(),
            enabled=True,
            persist_runs=False,
        )
        run = engine.start_run(
            pipeline_scope={
                "phases": ["phase1"],
                "phase1": {"extract": {"bbox": {"south": -0.3, "west": -78.5, "north": -0.1, "east": -78.3}}},
            },
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        rec = run.step_execution_records[-1]
        summary = dict(rec.executor_result_summary or {})
        self.assertIn("extraction_attempt_history_summary", summary)
        json.dumps(summary, sort_keys=True)

    def test_backward_compatibility_extraction_attempts_summary_aliases_remain(self) -> None:
        class _FakePhase1Client:
            def run_step_build_node_set_tuned(self, **kwargs):
                return {
                    "ok": True,
                    "best": {
                        "node_set_id": "ns-backcompat",
                        "action_id": "stops_broad_bbox",
                        "quality_score": 0.71,
                        "metrics": {"raw_count": 50, "candidate_count": 14},
                    },
                    "attempts": [],
                }

        bridge = build_phase_client_executor_bridge(phase1_client=_FakePhase1Client())
        state = RunSessionState(
            run_id="run-p1-backcompat",
            pipeline_scope={"phases": ["phase1"], "phase1": {"extract": {"bbox": {"south": -0.3, "west": -78.5, "north": -0.1, "east": -78.3}}}},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-backcompat",
        )
        result = bridge["phase1_extract"](state, self.registry[STEP_P1_1_EXTRACT], 1, {})
        summary = dict(result.summary or {})
        legacy = dict(summary.get("extraction_attempts_summary") or {})
        for key in ("attempt_count", "success_count", "diversified_attempts", "unique_config_count"):
            self.assertIn(key, legacy)

    def test_p1_extractor_status_generated_from_attempt_history(self) -> None:
        class _StubInsightsService:
            def compare_latest_vs_recent(self, *, phase, stage, lookback):
                del phase, stage, lookback
                return {"ok": False, "history_count": 0, "deltas": {}, "regression_flags": []}

        hook = build_ai_bot_telemetry_hook(insights_service=_StubInsightsService())
        step = self.registry[STEP_P1_1_EXTRACT]
        state = RunSessionState(
            run_id="run-p1-status",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-status",
        )
        snap = hook(
            state,
            step,
            1,
            ExecutorResult(
                ok=True,
                summary={
                    "candidate_count": 24,
                    "quality_score": 0.80,
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 3,
                        "successful_attempt_count": 2,
                        "non_empty_attempt_count": 2,
                        "fallback_used": True,
                        "retry_strategies_attempted": ["bbox_expand", "template_alternative", "fallback_config"],
                        "effective_config_fingerprints": ["fp1", "fp2", "fp3"],
                        "same_config_repeat_count": 0,
                        "best_attempt_index": 2,
                        "attempts": [
                            {"attempt_no": 1, "status": "error", "retry_strategy": "bbox_expand", "duration_ms": 100, "candidate_count": 0, "effective_config_fingerprint": "fp1"},
                            {"attempt_no": 2, "status": "success", "retry_strategy": "template_alternative", "duration_ms": 120, "candidate_count": 24, "fallback_config_used": "fallback_1", "effective_config_fingerprint": "fp2"},
                            {"attempt_no": 3, "status": "success", "retry_strategy": "fallback_config", "duration_ms": 95, "candidate_count": 22, "effective_config_fingerprint": "fp3"},
                        ],
                    },
                },
                artifacts=[],
            ),
            ValidatorResult(status="pass", gate_passed=True, evidence={"candidate_count": 24, "quality_score": 0.8}),
        )

        status = dict(snap.extractor_status or {})
        self.assertEqual(status.get("schema_version"), "extractor_status_v1")
        self.assertEqual(status.get("phase"), "phase1")
        eff = dict(status.get("efficiency_metrics") or {})
        self.assertEqual(int(eff.get("attempt_count_total") or 0), 3)
        self.assertEqual(int(eff.get("successful_attempt_count") or 0), 2)
        self.assertEqual(int(eff.get("retry_diversity_count") or 0), 3)
        self.assertEqual(int(eff.get("same_config_repeat_count") or 0), 0)
        self.assertTrue(bool(eff.get("fallback_used")))
        self.assertIn("extractor_efficiency_health_score", status)
        help_sig = dict(snap.extractor_help_needed or {})
        self.assertFalse(bool(help_sig.get("needed")))
        self.assertEqual(str(help_sig.get("recommended_escalation") or ""), "interpreter_patch_evaluation")

    def test_p3_extractor_status_generated_with_step20_context(self) -> None:
        class _StubInsightsService:
            def compare_latest_vs_recent(self, *, phase, stage, lookback):
                del phase, stage, lookback
                return {"ok": True, "history_count": 4, "deltas": {"unmatched_count": -2}, "regression_flags": []}

        hook = build_ai_bot_telemetry_hook(insights_service=_StubInsightsService())
        step = self.registry[STEP_P3_1_EXTRACT]
        state = RunSessionState(
            run_id="run-p3-status",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-status",
            resume_context={
                "latest_step20_summary": {
                    "matched_count": 8,
                    "unmatched_count": 1,
                    "ambiguous_count": 1,
                    "sequence_quality_score": 80.0,
                    "sequence_gate_pass": True,
                }
            },
        )
        snap = hook(
            state,
            step,
            1,
            ExecutorResult(
                ok=True,
                summary={
                    "prior_stop_count": 10,
                    "extractor_diagnostics": {"candidate_count": 9},
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 2,
                        "successful_attempt_count": 1,
                        "non_empty_attempt_count": 1,
                        "fallback_used": False,
                        "retry_strategies_attempted": ["fallback_config"],
                        "effective_config_fingerprints": ["x1", "x2"],
                        "same_config_repeat_count": 0,
                        "best_attempt_index": 2,
                        "attempts": [
                            {"attempt_no": 1, "status": "timeout", "duration_ms": 150, "candidate_count": 0, "effective_config_fingerprint": "x1"},
                            {"attempt_no": 2, "status": "success", "duration_ms": 130, "candidate_count": 9, "effective_config_fingerprint": "x2"},
                        ],
                    },
                },
                artifacts=[],
            ),
            ValidatorResult(status="pass", gate_passed=True, evidence={"prior_stop_count": 10}),
        )

        status = dict(snap.extractor_status or {})
        self.assertEqual(status.get("phase"), "phase3")
        completion = dict(status.get("completion_metrics") or {})
        self.assertTrue(bool(completion.get("step20_available")))
        self.assertEqual(int(completion.get("matched_count") or 0), 8)
        self.assertEqual(int(completion.get("unmatched_count") or 0), 1)
        self.assertIsNotNone(completion.get("order_completion_quality_score"))
        help_sig = dict(snap.extractor_help_needed or {})
        self.assertFalse(bool(help_sig.get("needed")))
        self.assertAlmostEqual(float(help_sig.get("confidence") or 0.0), 0.85, places=2)

    def test_phase1_completion_quality_score_deterministic_and_null_safe(self) -> None:
        class _StubInsightsService:
            def compare_latest_vs_recent(self, *, phase, stage, lookback):
                del phase, stage, lookback
                return {"ok": False, "history_count": 0, "deltas": {}, "regression_flags": []}

        hook = build_ai_bot_telemetry_hook(insights_service=_StubInsightsService())
        step = self.registry[STEP_P1_1_EXTRACT]
        state = RunSessionState(
            run_id="run-p1-score",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-score",
        )
        snap = hook(
            state,
            step,
            1,
            ExecutorResult(
                ok=True,
                summary={
                    "candidate_count": 20,
                    "quality_score": 0.80,
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 1,
                        "successful_attempt_count": 1,
                        "non_empty_attempt_count": 1,
                        "effective_config_fingerprints": ["one"],
                        "same_config_repeat_count": 0,
                        "attempts": [{"attempt_no": 1, "status": "success", "candidate_count": 20, "duration_ms": 90, "effective_config_fingerprint": "one"}],
                    },
                },
                artifacts=[],
            ),
            ValidatorResult(status="pass", gate_passed=True, evidence={"candidate_count": 20, "quality_score": 0.8}),
        )
        comp = dict((dict(snap.extractor_status or {})).get("completion_metrics") or {})
        self.assertAlmostEqual(float(comp.get("completion_quality_score") or 0.0), 0.88, places=2)

    def test_phase3_order_completion_score_partial_without_step20(self) -> None:
        class _StubInsightsService:
            def compare_latest_vs_recent(self, *, phase, stage, lookback):
                del phase, stage, lookback
                return {"ok": False, "history_count": 0, "deltas": {}, "regression_flags": []}

        hook = build_ai_bot_telemetry_hook(insights_service=_StubInsightsService())
        step = self.registry[STEP_P3_1_EXTRACT]
        state = RunSessionState(
            run_id="run-p3-partial",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-partial",
        )
        snap = hook(
            state,
            step,
            1,
            ExecutorResult(
                ok=True,
                summary={
                    "prior_stop_count": 6,
                    "extractor_diagnostics": {"candidate_count": 4},
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 1,
                        "successful_attempt_count": 1,
                        "non_empty_attempt_count": 1,
                        "effective_config_fingerprints": ["p3a"],
                        "same_config_repeat_count": 0,
                        "attempts": [{"attempt_no": 1, "status": "success", "candidate_count": 4, "duration_ms": 120, "effective_config_fingerprint": "p3a"}],
                    },
                },
                artifacts=[],
            ),
            ValidatorResult(status="pass", gate_passed=True, evidence={"prior_stop_count": 6}),
        )
        completion = dict((dict(snap.extractor_status or {})).get("completion_metrics") or {})
        self.assertFalse(bool(completion.get("step20_available")))
        self.assertIsNotNone(completion.get("order_completion_quality_score"))
        help_sig = dict(snap.extractor_help_needed or {})
        self.assertTrue(bool(help_sig.get("partial_evidence")))
        self.assertIn(str(help_sig.get("severity") or ""), {"low", "medium"})
        self.assertNotEqual(str(help_sig.get("severity") or ""), "high")
        self.assertAlmostEqual(float(help_sig.get("confidence") or 0.0), 0.4, places=2)

    def test_extractor_specific_warnings_generated_for_failure_patterns(self) -> None:
        class _StubInsightsService:
            def compare_latest_vs_recent(self, *, phase, stage, lookback):
                del phase, stage, lookback
                return {"ok": True, "history_count": 6, "deltas": {}, "regression_flags": [{"code": "quality_drop"}]}

        hook = build_ai_bot_telemetry_hook(insights_service=_StubInsightsService())
        step = self.registry[STEP_P1_1_EXTRACT]
        state = RunSessionState(
            run_id="run-p1-warn",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-warn",
        )
        snap = hook(
            state,
            step,
            2,
            ExecutorResult(
                ok=False,
                summary={
                    "candidate_count": 0,
                    "quality_score": 0.12,
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 3,
                        "successful_attempt_count": 0,
                        "non_empty_attempt_count": 0,
                        "fallback_used": True,
                        "retry_strategies_attempted": ["bbox_expand"],
                        "effective_config_fingerprints": ["z1"],
                        "same_config_repeat_count": 2,
                        "attempts": [
                            {"attempt_no": 1, "status": "error", "candidate_count": 0, "fallback_config_used": "fallback_1", "effective_config_fingerprint": "z1"},
                            {"attempt_no": 2, "status": "error", "candidate_count": 0, "fallback_config_used": "fallback_1", "effective_config_fingerprint": "z1"},
                            {"attempt_no": 3, "status": "error", "candidate_count": 0, "fallback_config_used": "fallback_1", "effective_config_fingerprint": "z1"},
                        ],
                    },
                },
                artifacts=[],
            ),
            ValidatorResult(status="blocked", gate_passed=False, evidence={"candidate_count": 0}),
        )
        warning_set = set(list(snap.warnings or []))
        self.assertIn("repeated_empty_extraction", warning_set)
        self.assertIn("retry_not_diversified", warning_set)
        self.assertIn("fallback_rescue_failed", warning_set)
        self.assertIn("same_config_retry_loop", warning_set)
        help_sig = dict(snap.extractor_help_needed or {})
        self.assertTrue(bool(help_sig.get("needed")))
        self.assertEqual(str(help_sig.get("severity") or ""), "high")
        self.assertEqual(str(help_sig.get("reason_class") or ""), "repeated_empty_extraction")
        reasons = list(help_sig.get("reasons") or [])
        self.assertLessEqual(len(reasons), 8)
        self.assertTrue(reasons)
        for row in reasons:
            rr = dict(row or {})
            self.assertIn("code", rr)
            self.assertIn("value", rr)
            self.assertIn("threshold", rr)
            self.assertIn("message", rr)


    def test_missing_attempt_history_does_not_emit_repeated_empty_and_caps_help_confidence(self) -> None:
        class _StubInsightsService:
            def compare_latest_vs_recent(self, *, phase, stage, lookback):
                del phase, stage, lookback
                return {"ok": False, "history_count": 0, "deltas": {}, "regression_flags": []}

        hook = build_ai_bot_telemetry_hook(insights_service=_StubInsightsService())
        step = self.registry[STEP_P1_1_EXTRACT]
        state = RunSessionState(
            run_id="run-p1-missing-history",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-missing-history",
        )
        snap = hook(
            state,
            step,
            4,
            ExecutorResult(
                ok=False,
                summary={
                    "candidate_count": 0,
                    "quality_score": 0.2,
                    "extraction_attempt_history_summary": {"attempt_count_total": 0, "attempts": []},
                },
                artifacts=[],
            ),
            ValidatorResult(status="blocked", gate_passed=False, evidence={"candidate_count": 0}),
        )
        warning_set = set(list(snap.warnings or []))
        self.assertIn("missing_attempt_history", warning_set)
        self.assertNotIn("repeated_empty_extraction", warning_set)
        help_sig = dict(snap.extractor_help_needed or {})
        self.assertTrue(bool(help_sig.get("partial_evidence")))
        self.assertLessEqual(float(help_sig.get("confidence") or 0.0), 0.35)

    def test_empty_extraction_two_attempts_emits_repeated_empty_evidence_based(self) -> None:
        class _StubInsightsService:
            def compare_latest_vs_recent(self, *, phase, stage, lookback):
                del phase, stage, lookback
                return {"ok": False, "history_count": 0, "deltas": {}, "regression_flags": []}

        hook = build_ai_bot_telemetry_hook(insights_service=_StubInsightsService())
        step = self.registry[STEP_P1_1_EXTRACT]
        state = RunSessionState(
            run_id="run-p1-two-empty",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-two-empty",
        )
        snap = hook(
            state,
            step,
            2,
            ExecutorResult(
                ok=False,
                summary={
                    "candidate_count": 0,
                    "quality_score": 0.1,
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 2,
                        "successful_attempt_count": 0,
                        "non_empty_attempt_count": 0,
                        "same_config_repeat_count": 1,
                        "retry_diversity_count": 1,
                        "attempts": [
                            {"attempt_no": 1, "status": "empty", "candidate_count": 0, "effective_config_fingerprint": "r1"},
                            {"attempt_no": 2, "status": "empty", "candidate_count": 0, "effective_config_fingerprint": "r1"},
                        ],
                    },
                },
                artifacts=[],
            ),
            ValidatorResult(status="blocked", gate_passed=False, evidence={"candidate_count": 0}),
        )
        warning_set = set(list(snap.warnings or []))
        self.assertIn("repeated_empty_extraction", warning_set)
    def test_p1_low_diversity_same_config_repeat_help_signal(self) -> None:
        class _StubInsightsService:
            def compare_latest_vs_recent(self, *, phase, stage, lookback):
                del phase, stage, lookback
                return {"ok": False, "history_count": 0, "deltas": {}, "regression_flags": []}

        hook = build_ai_bot_telemetry_hook(insights_service=_StubInsightsService())
        step = self.registry[STEP_P1_1_EXTRACT]
        state = RunSessionState(
            run_id="run-p1-low-div",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-low-div",
        )
        snap = hook(
            state,
            step,
            1,
            ExecutorResult(
                ok=True,
                summary={
                    "candidate_count": 9,
                    "quality_score": 0.5,
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 4,
                        "successful_attempt_count": 2,
                        "non_empty_attempt_count": 2,
                        "retry_strategies_attempted": ["bbox_expand"],
                        "effective_config_fingerprints": ["same1"],
                        "same_config_repeat_count": 3,
                        "best_attempt_index": 4,
                        "attempts": [
                            {"attempt_no": 1, "status": "error", "candidate_count": 0, "duration_ms": 70, "effective_config_fingerprint": "same1"},
                            {"attempt_no": 2, "status": "error", "candidate_count": 0, "duration_ms": 75, "effective_config_fingerprint": "same1"},
                            {"attempt_no": 3, "status": "success", "candidate_count": 7, "duration_ms": 85, "effective_config_fingerprint": "same1"},
                            {"attempt_no": 4, "status": "success", "candidate_count": 9, "duration_ms": 90, "effective_config_fingerprint": "same1"},
                        ],
                    },
                },
                artifacts=[],
            ),
            ValidatorResult(status="pass", gate_passed=True, evidence={"candidate_count": 9, "quality_score": 0.5}),
        )
        help_sig = dict(snap.extractor_help_needed or {})
        self.assertTrue(bool(help_sig.get("needed")))
        self.assertEqual(str(help_sig.get("severity") or ""), "medium")
        reason_codes = {str(r.get("code") or "") for r in list(help_sig.get("reasons") or [])}
        self.assertTrue(("retry_not_diversified" in reason_codes) or ("same_config_retry_loop" in reason_codes))

    def test_p3_success_but_step20_poor_sets_high_help_signal(self) -> None:
        class _StubInsightsService:
            def compare_latest_vs_recent(self, *, phase, stage, lookback):
                del phase, stage, lookback
                return {"ok": False, "history_count": 0, "deltas": {}, "regression_flags": []}

        hook = build_ai_bot_telemetry_hook(insights_service=_StubInsightsService())
        step = self.registry[STEP_P3_1_EXTRACT]
        state = RunSessionState(
            run_id="run-p3-poor-step20",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-poor-step20",
            resume_context={
                "latest_step20_summary": {
                    "matched_count": 5,
                    "unmatched_count": 3,
                    "ambiguous_count": 2,
                    "sequence_quality_score": 60.0,
                    "sequence_gate_pass": False,
                }
            },
        )
        snap = hook(
            state,
            step,
            1,
            ExecutorResult(
                ok=True,
                summary={
                    "prior_stop_count": 8,
                    "extractor_diagnostics": {"candidate_count": 7},
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 1,
                        "successful_attempt_count": 1,
                        "non_empty_attempt_count": 1,
                        "effective_config_fingerprints": ["p3poor"],
                        "same_config_repeat_count": 0,
                        "best_attempt_index": 1,
                        "attempts": [
                            {"attempt_no": 1, "status": "success", "candidate_count": 7, "duration_ms": 130, "effective_config_fingerprint": "p3poor"},
                        ],
                    },
                },
                artifacts=[],
            ),
            ValidatorResult(status="pass", gate_passed=True, evidence={"prior_stop_count": 8}),
        )
        help_sig = dict(snap.extractor_help_needed or {})
        self.assertTrue(bool(help_sig.get("needed")))
        self.assertEqual(str(help_sig.get("severity") or ""), "high")
        reason_codes = {str(r.get("code") or "") for r in list(help_sig.get("reasons") or [])}
        self.assertIn("extraction_success_but_step20_poor", reason_codes)

    def test_p3_repeated_low_evidence_and_poor_step20_help_signal(self) -> None:
        class _StubInsightsService:
            def compare_latest_vs_recent(self, *, phase, stage, lookback):
                del phase, stage, lookback
                return {"ok": True, "history_count": 7, "deltas": {"candidate_count": -4}, "regression_flags": [{"code": "phase3_unmatched_increase"}]}

        hook = build_ai_bot_telemetry_hook(insights_service=_StubInsightsService())
        step = self.registry[STEP_P3_1_EXTRACT]
        state = RunSessionState(
            run_id="run-p3-repeated-weak",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-repeated-weak",
            resume_context={
                "latest_step20_summary": {
                    "matched_count": 2,
                    "unmatched_count": 4,
                    "ambiguous_count": 1,
                    "sequence_quality_score": 58.0,
                    "sequence_gate_pass": False,
                }
            },
        )
        snap = hook(
            state,
            step,
            1,
            ExecutorResult(
                ok=True,
                summary={
                    "prior_stop_count": 2,
                    "extractor_diagnostics": {"candidate_count": 1},
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 4,
                        "successful_attempt_count": 1,
                        "non_empty_attempt_count": 1,
                        "fallback_used": True,
                        "retry_strategies_attempted": ["fallback_config"],
                        "effective_config_fingerprints": ["low1", "low2"],
                        "same_config_repeat_count": 2,
                        "best_attempt_index": 4,
                        "attempts": [
                            {"attempt_no": 1, "status": "error", "candidate_count": 0, "duration_ms": 100, "effective_config_fingerprint": "low1"},
                            {"attempt_no": 2, "status": "error", "candidate_count": 0, "duration_ms": 100, "effective_config_fingerprint": "low1"},
                            {"attempt_no": 3, "status": "error", "candidate_count": 0, "duration_ms": 100, "fallback_profile_used": "fallback_2", "effective_config_fingerprint": "low2"},
                            {"attempt_no": 4, "status": "success", "candidate_count": 1, "duration_ms": 100, "fallback_profile_used": "fallback_2", "effective_config_fingerprint": "low2"},
                        ],
                    },
                },
                artifacts=[],
            ),
            ValidatorResult(status="warning", gate_passed=True, evidence={"prior_stop_count": 2}),
        )
        help_sig = dict(snap.extractor_help_needed or {})
        self.assertTrue(bool(help_sig.get("needed")))
        reason_codes = {str(r.get("code") or "") for r in list(help_sig.get("reasons") or [])}
        self.assertIn("low_order_completion_quality", reason_codes)
        self.assertIn(str(help_sig.get("severity") or ""), {"high", "medium"})

    def test_ai_snapshot_step_record_includes_extractor_status(self) -> None:
        class _StubInsightsService:
            def compare_latest_vs_recent(self, *, phase, stage, lookback):
                del phase, stage, lookback
                return {"ok": False, "history_count": 0, "deltas": {}, "regression_flags": []}

        engine = self._new_engine()

        def p1_extract_executor(state, step, attempt_no, params):
            del state, step, attempt_no, params
            return ExecutorResult(
                ok=True,
                summary={
                    "candidate_count": 12,
                    "quality_score": 0.68,
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 1,
                        "successful_attempt_count": 1,
                        "non_empty_attempt_count": 1,
                        "effective_config_fingerprints": ["r1"],
                        "same_config_repeat_count": 0,
                        "attempts": [{"attempt_no": 1, "status": "success", "candidate_count": 12, "duration_ms": 80, "effective_config_fingerprint": "r1"}],
                    },
                    "validator_payload": {"candidate_count": 12, "quality_score": 0.68},
                },
                artifacts=[],
            )

        def p1_extract_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="pass", gate_passed=True, evidence={"candidate_count": 12, "quality_score": 0.68})

        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, p1_extract_executor)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, p1_extract_validator)
        engine.register_ai_hook(self.registry[STEP_P1_1_EXTRACT].ai_bot_hooks[0], build_ai_bot_telemetry_hook(insights_service=_StubInsightsService()))

        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        rec = run.step_execution_records[-1]
        ai_snap = dict(rec.ai_bot_snapshot or {})
        self.assertIn("extractor_status", ai_snap)
        self.assertEqual(dict(ai_snap.get("extractor_status") or {}).get("schema_version"), "extractor_status_v1")
        self.assertIn("extractor_help_needed", ai_snap)
        help_sig = dict(ai_snap.get("extractor_help_needed") or {})
        self.assertIn("needed", help_sig)
        self.assertIn("severity", help_sig)

    def test_interpreter_snapshot_includes_extractor_status(self) -> None:
        class _StubAdvisoryService:
            def __init__(self) -> None:
                self.calls = []

            def run_task_detailed(self, *, endpoint_task, envelope):
                self.calls.append({"endpoint_task": endpoint_task, "envelope": dict(envelope or {})})
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {"contradictions_found": False, "contradictions": [], "consistency_summary": "ok"},
                        "meta": {"task": endpoint_task},
                    }
                return {
                    "response": {
                        "summary": "ok",
                        "dominant_cause_class": "extractor_config",
                        "confidence": 0.72,
                        "secondary_causes": [],
                        "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                        "recommended_branch": "tuning_retry",
                        "recommended_next_actions": ["retry with diversified strategy"],
                        "patch_task_recommendation": {
                            "should_create_patch_task": False,
                            "patch_type": None,
                            "justification": None,
                            "suggested_target": None,
                        },
                        "operator_action_required": False,
                        "approval_type_if_needed": None,
                        "risk_notes": [],
                    },
                    "meta": {"task": endpoint_task},
                }

        step = self.registry[STEP_P1_1_EXTRACT]
        state = RunSessionState(
            run_id="run-int-extractor",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-int-extractor",
        )
        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_StubAdvisoryService())
        _ = interpreter(
            state,
            step,
            1,
            ExecutorResult(ok=True, summary={"candidate_count": 10}, artifacts=[]),
            ValidatorResult(status="warning", gate_passed=True, warnings=["warn"], evidence={"candidate_count": 10}),
            AIBotSnapshot(
                scores={"quality": 0.7},
                metrics={"extractor_status": {"schema_version": "extractor_status_v1", "phase": "phase1"}},
                extractor_status={"schema_version": "extractor_status_v1", "phase": "phase1"},
            ),
            "warning",
        )
        calls = list(getattr(interpreter._service, "calls", []))
        interp_calls = [c for c in calls if str(c.get("endpoint_task")) == "hades_pipeline_interpreter"]
        self.assertTrue(interp_calls)
        snap = dict((interp_calls[-1].get("envelope") or {}).get("snapshot") or {})
        ai_bot = dict(snap.get("ai_bot") or {})
        self.assertIn("extractor_status", ai_bot)
        self.assertEqual(dict(ai_bot.get("extractor_status") or {}).get("schema_version"), "extractor_status_v1")
        self.assertIn("extractor_help_needed", ai_bot)

    def test_ai_bot_backward_compatibility_p21_reports_semantic_completion_status(self) -> None:
        class _StubInsightsService:
            def compare_latest_vs_recent(self, *, phase, stage, lookback):
                del phase, stage, lookback
                return {"ok": True, "history_count": 2, "deltas": {}, "regression_flags": []}

        hook = build_ai_bot_telemetry_hook(insights_service=_StubInsightsService())
        step = self.registry[STEP_P2_1_SEMANTIC]
        state = RunSessionState(
            run_id="run-p2-smoke",
            pipeline_scope={"phases": ["phase2"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase2",
            current_step_id=STEP_P2_1_SEMANTIC,
            trace_id="trace-p2-smoke",
        )
        snap = hook(
            state,
            step,
            1,
            ExecutorResult(ok=True, summary={"quality_score": 0.91}, artifacts=[]),
            ValidatorResult(status="pass", gate_passed=True, evidence={}),
        )
        self.assertIn("quality_score", dict(snap.scores or {}))
        self.assertIn("comparison", dict(snap.proposals or {}))
        completion = dict(dict(snap.extractor_status or {}).get("completion_metrics") or {})
        self.assertEqual(completion.get("completed_substeps"), 0)
        self.assertEqual(completion.get("expected_substeps"), 7)
        self.assertTrue(bool(dict(snap.extractor_help_needed or {}).get("needed")))

    def test_p21_semantic_pipeline_scopes_to_current_node_set_and_runtime_context(self) -> None:
        calls: Dict[str, Dict[str, Any]] = {}

        class _FakePhase1Client:
            def get_promote_status(self, node_set_id):
                return {
                    "node_set_id": str(node_set_id),
                    "promote_status": "promoted_to_node_prod",
                    "n_prod": 5,
                    "last_promoted_at": "2026-03-05T10:00:00Z",
                }

        class _FakePhase2Client:
            def run_step_10_extract(self, **kwargs):
                calls["10"] = dict(kwargs or {})
                return {"ok": True}

            def run_step_15_build_geo_context(self, **kwargs):
                calls["15"] = dict(kwargs or {})
                return {"ok": True}

            def run_step_20_build_candidates(self, **kwargs):
                calls["20"] = dict(kwargs or {})
                return {"ok": True, "summary": {"place_set_id": "ps-123", "place_set_id_source": "step20_summary"}}

            def run_step_25_build_name_candidates(self, **kwargs):
                calls["25"] = dict(kwargs or {})
                return {"ok": True, "summary": {"place_set_id": "ps-123"}}

            def list_place_candidates_for_set(self, place_set_id, *, limit=500):
                del limit
                return [{"place_set_id": place_set_id, "place_candidate_id": "pc-1"}]

            def run_step_35_train_name_ranker(self, **kwargs):
                calls["35"] = dict(kwargs or {})
                return {"ok": True, "skipped": True, "skip_reason": "no_feedback_rows"}

            def run_step_40_build_embeddings(self, **kwargs):
                calls["40"] = dict(kwargs or {})
                return {"ok": True, "skipped": True, "skip_reason": "embeddings_up_to_date"}

            def run_step_50_reindex_opensearch(self, **kwargs):
                calls["50"] = dict(kwargs or {})
                return {
                    "ok": True,
                    "skipped": True,
                    "skip_reason": "embeddings_step_skipped:embeddings_up_to_date",
                }

        bridge = build_phase_client_executor_bridge(
            phase1_client=_FakePhase1Client(),
            phase2_client=_FakePhase2Client(),
        )
        state = RunSessionState(
            run_id="run-p2-scope",
            pipeline_scope={"phases": ["phase2"], "phase1": {"node_set_id": "ns-123"}},
            resume_context={"node_set_id": "ns-123"},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase2",
            current_step_id=STEP_P2_1_SEMANTIC,
            trace_id="trace-p2-scope",
        )
        result = bridge["phase2_semantic_pipeline"](state, self.registry[STEP_P2_1_SEMANTIC], 1, {})
        summary = dict(result.summary or {})
        handoff = dict(summary.get("phase1_to_phase2_handoff") or {})
        runtime_context = dict(summary.get("runtime_context") or {})
        self.assertTrue(result.ok)
        self.assertEqual(calls["10"].get("source_node_set_id"), "ns-123")
        ctx = str(calls["10"].get("context_key") or "")
        self.assertTrue(ctx.startswith("autopilot_phase2:run-p2-scope:ns-123"))
        self.assertEqual(calls["15"].get("context_key"), ctx)
        self.assertEqual(calls["20"].get("context_key"), ctx)
        self.assertEqual(calls["25"].get("context_key"), ctx)
        self.assertEqual(calls["25"].get("place_set_id"), "ps-123")
        self.assertEqual(runtime_context.get("source_node_set_id"), "ns-123")
        self.assertEqual(runtime_context.get("place_set_id"), "ps-123")
        self.assertEqual(runtime_context.get("context_key"), ctx)
        self.assertTrue(bool(handoff.get("phase1_promote_completed")))
        self.assertEqual(handoff.get("place_set_id"), "ps-123")
        self.assertEqual(handoff.get("place_set_id_source"), "step20_summary")
        self.assertTrue(bool(handoff.get("ready_for_phase2")))
        self.assertTrue(bool(handoff.get("ready_for_step25")))

    def test_p21_semantic_pipeline_records_skipped_heavy_tail_substeps(self) -> None:
        calls: Dict[str, Dict[str, Any]] = {}

        class _FakePhase2Client:
            def run_step_10_extract(self, **kwargs):
                calls["10"] = dict(kwargs or {})
                return {"ok": True}

            def run_step_15_build_geo_context(self, **kwargs):
                calls["15"] = dict(kwargs or {})
                return {"ok": True}

            def run_step_20_build_candidates(self, **kwargs):
                calls["20"] = dict(kwargs or {})
                return {"ok": True, "summary": {"place_set_id": "ps-tail"}}

            def run_step_25_build_name_candidates(self, **kwargs):
                calls["25"] = dict(kwargs or {})
                return {"ok": True}

            def run_step_35_train_name_ranker(self, **kwargs):
                calls["35"] = dict(kwargs or {})
                return {"ok": True, "skipped": True, "skip_reason": "models_up_to_date"}

            def run_step_40_build_embeddings(self, **kwargs):
                calls["40"] = dict(kwargs or {})
                return {"ok": True, "skipped": True, "skip_reason": "embeddings_up_to_date"}

            def run_step_50_reindex_opensearch(self, **kwargs):
                calls["50"] = dict(kwargs or {})
                if not bool(dict(kwargs.get("embeddings_step_result") or {}).get("skipped")):
                    raise AssertionError("embeddings_step_result should be marked skipped before reindex")
                return {
                    "ok": True,
                    "skipped": True,
                    "skip_reason": "embeddings_step_skipped:embeddings_up_to_date",
                }

        bridge = build_phase_client_executor_bridge(phase2_client=_FakePhase2Client())
        state = RunSessionState(
            run_id="run-p2-tail",
            pipeline_scope={"phases": ["phase2"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase2",
            current_step_id=STEP_P2_1_SEMANTIC,
            trace_id="trace-p2-tail",
        )
        result = bridge["phase2_semantic_pipeline"](state, self.registry[STEP_P2_1_SEMANTIC], 1, {})
        summary = dict(result.summary or {})
        skipped = dict(summary.get("skipped_substeps") or {})
        timings = dict(summary.get("substep_timings_ms") or {})
        self.assertTrue(result.ok)
        self.assertIn("train_ranker", skipped)
        self.assertIn("embeddings", skipped)
        self.assertIn("reindex", skipped)
        self.assertEqual(dict(skipped.get("embeddings") or {}).get("skip_reason"), "embeddings_up_to_date")
        self.assertEqual(
            dict(skipped.get("reindex") or {}).get("skip_reason"),
            "embeddings_step_skipped:embeddings_up_to_date",
        )
        self.assertTrue(all(k in timings for k in ("extract", "geo_context", "candidates", "name_candidates", "train_ranker", "embeddings", "reindex")))
        self.assertIn("50", calls)

    def test_p14_promote_apply_emits_phase1_to_phase2_handoff_artifact(self) -> None:
        class _FakePhase1Client:
            def run_step_promote(self, node_set_id):
                return {"node_set_id": node_set_id, "promoted": 7, "prod_total": 42}

        bridge = build_phase_client_executor_bridge(phase1_client=_FakePhase1Client())
        state = RunSessionState(
            run_id="run-p1-promote",
            pipeline_scope={"phases": ["phase1"], "phase1": {"node_set_id": "ns-promote"}},
            resume_context={"node_set_id": "ns-promote"},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_4_PROMOTE,
            artifacts={
                STEP_P1_4_PROMOTE: [
                    {
                        "artifact_type": "phase1_promote_prepare",
                        "payload": {
                            "promote_dry_run": {"n_resolved": 9, "n_approved": 7},
                            "workspace_state_summary": {
                                "resolved_total": 9,
                                "approved_count": 7,
                                "promote_eligible_count": 7,
                            },
                        },
                    }
                ]
            },
        )

        result = bridge["phase1_promote_apply"](state, self.registry[STEP_P1_4_PROMOTE], 1, {})
        summary = dict(result.summary or {})
        handoff = dict(summary.get("phase1_to_phase2_handoff") or {})
        artifact_types = [str(a.get("artifact_type") or "") for a in list(result.artifacts or [])]

        self.assertTrue(result.ok)
        self.assertIn("phase1_promote", artifact_types)
        self.assertIn("phase1_to_phase2_handoff", artifact_types)
        self.assertEqual(handoff.get("source_node_set_id"), "ns-promote")
        self.assertTrue(bool(handoff.get("phase1_promote_completed")))
        self.assertEqual(handoff.get("promoted_count"), 7)
        self.assertTrue(bool(handoff.get("ready_for_phase2")))
        self.assertFalse(bool(handoff.get("ready_for_step25")))

    def test_p21_semantic_pipeline_blocks_when_phase1_promote_not_completed(self) -> None:
        class _FakePhase1Client:
            def get_promote_status(self, node_set_id):
                return {
                    "node_set_id": str(node_set_id),
                    "promote_status": "ready_to_promote",
                    "n_prod": 0,
                }

        class _FakePhase2Client:
            def run_step_10_extract(self, **kwargs):
                raise AssertionError("Phase 2 should not start before Phase 1 promote is verified")

        bridge = build_phase_client_executor_bridge(
            phase1_client=_FakePhase1Client(),
            phase2_client=_FakePhase2Client(),
        )
        validators = build_phase_client_validator_bridge()
        state = RunSessionState(
            run_id="run-p2-precondition",
            pipeline_scope={"phases": ["phase2"], "phase1": {"node_set_id": "ns-blocked"}},
            resume_context={"node_set_id": "ns-blocked"},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase2",
            current_step_id=STEP_P2_1_SEMANTIC,
        )

        result = bridge["phase2_semantic_pipeline"](state, self.registry[STEP_P2_1_SEMANTIC], 1, {})
        validator = validators["phase2_semantic_pipeline"](state, self.registry[STEP_P2_1_SEMANTIC], 1, result)
        handoff = dict(result.summary.get("phase1_to_phase2_handoff") or {})

        self.assertTrue(result.ok)
        self.assertEqual(validator.status, "blocked")
        self.assertEqual(validator.block_reason_code, BlockReasonCode.PHASE1_PROMOTE_NOT_COMPLETED)
        self.assertEqual(handoff.get("source_node_set_id"), "ns-blocked")
        self.assertFalse(bool(handoff.get("ready_for_phase2")))

    def test_p21_semantic_pipeline_blocks_before_step25_when_place_set_id_missing(self) -> None:
        calls: Dict[str, int] = {"25": 0}

        class _FakePhase1Client:
            def get_promote_status(self, node_set_id):
                return {
                    "node_set_id": str(node_set_id),
                    "promote_status": "promoted_to_node_prod",
                    "n_prod": 3,
                }

        class _FakePhase2Client:
            def run_step_10_extract(self, **kwargs):
                return {"ok": True}

            def run_step_15_build_geo_context(self, **kwargs):
                return {"ok": True}

            def run_step_20_build_candidates(self, **kwargs):
                return {"ok": True, "summary": {}}

            def get_latest_place_set_id(self, *, context_key=None):
                del context_key
                return None

            def run_step_25_build_name_candidates(self, **kwargs):
                calls["25"] += 1
                return {"ok": True}

        bridge = build_phase_client_executor_bridge(
            phase1_client=_FakePhase1Client(),
            phase2_client=_FakePhase2Client(),
        )
        validators = build_phase_client_validator_bridge()
        state = RunSessionState(
            run_id="run-p2-missing-place-set",
            pipeline_scope={"phases": ["phase2"], "phase1": {"node_set_id": "ns-25"}},
            resume_context={"node_set_id": "ns-25"},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase2",
            current_step_id=STEP_P2_1_SEMANTIC,
        )

        result = bridge["phase2_semantic_pipeline"](state, self.registry[STEP_P2_1_SEMANTIC], 1, {})
        validator = validators["phase2_semantic_pipeline"](state, self.registry[STEP_P2_1_SEMANTIC], 1, result)
        payload = dict(result.summary.get("validator_payload") or {})

        self.assertTrue(result.ok)
        self.assertEqual(calls["25"], 0)
        self.assertEqual(payload.get("block_reason_code"), BlockReasonCode.PLACE_SET_ID_NOT_FOUND.value)
        self.assertEqual(payload.get("phase1_lookup_key"), "ns-25")
        self.assertEqual(validator.status, "blocked")
        self.assertEqual(validator.block_reason_code, BlockReasonCode.PLACE_SET_ID_NOT_FOUND)

    def test_no_silent_retry_param_ignore_emits_warning_event(self) -> None:
        engine = self._new_engine()

        def p1_extract_executor(state, step, attempt_no, params):
            del state, step, attempt_no, params
            return ExecutorResult(
                ok=True,
                summary={
                    "candidate_count": 10,
                    "quality_score": 0.65,
                    "effective_config_fingerprint": "abc123",
                    "retry_parameter_warnings": ["unsupported_retry_param:test_key"],
                    "validator_payload": {"candidate_count": 10, "quality_score": 0.65},
                },
                artifacts=[],
            )

        def p1_extract_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, p1_extract_executor)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, p1_extract_validator)

        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        warning_events = [
            e for e in run.events if e.event_type == "step_warning" and e.step_id == STEP_P1_1_EXTRACT
        ]
        self.assertTrue(warning_events)
        self.assertTrue(
            any("unsupported_retry_param:test_key" in list((e.payload or {}).get("warnings") or []) for e in warning_events)
        )

    def test_backward_compatibility_smoke_phase1_extraction_bridge(self) -> None:
        class _FakePhase1Client:
            def run_step_build_node_set_tuned(self, **kwargs):
                return {
                    "ok": True,
                    "best": {
                        "node_set_id": "smoke-node-set",
                        "action_id": "stops_broad_bbox",
                        "quality_score": 0.71,
                        "metrics": {"raw_count": 50, "candidate_count": 14},
                    },
                    "attempts": [],
                }

        engine = SupervisedPipelineAutopilot(
            step_registry=self.registry,
            executors=build_phase_client_executor_bridge(phase1_client=_FakePhase1Client()),
            validators=build_phase_client_validator_bridge(),
            enabled=True,
            persist_runs=False,
        )
        run = engine.start_run(
            pipeline_scope={
                "phases": ["phase1"],
                "phase1": {"extract": {"bbox": {"south": -0.3, "west": -78.5, "north": -0.1, "east": -78.3}}},
            },
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        self.assertIn(run.status, {"running", "paused", "waiting_for_approval", "completed"})
        self.assertTrue(run.step_execution_records)

    def test_stage5_patch_branch_triggers_patch_task_generation(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        stub = _StubPatchChainInterpreter(branch="patch_extractor", should_patch=True, patch_type="extractor")
        engine.chatgpt_interpreter = stub

        def warning_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(
                status="warning",
                gate_passed=True,
                passable_warning=True,
                warnings=["extractor_warning"],
                evidence={"candidate_count": 8},
            )

        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, warning_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(len(stub.generate_calls), 1)
        self.assertTrue(any(e.event_type == "patch_task_generation_started" for e in run.events))
        self.assertTrue(any(e.event_type == "patch_task_generation_completed" for e in run.events))

    def test_stage5_non_patch_branch_does_not_trigger_patch_generation(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        stub = _StubPatchChainInterpreter(branch="tuning_retry", should_patch=False)
        engine.chatgpt_interpreter = stub

        def warning_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(
                status="warning",
                gate_passed=True,
                passable_warning=True,
                warnings=["minor_warning"],
                evidence={"candidate_count": 9},
            )

        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, warning_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(len(stub.generate_calls), 0)
        self.assertFalse(any(e.event_type.startswith("patch_task_generation_") for e in run.events))

    def test_stage5_patch_task_artifact_persisted_with_run_step_context(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        stub = _StubPatchChainInterpreter(branch="patch_diagnostics", should_patch=True, patch_type="diagnostics")
        engine.chatgpt_interpreter = stub

        def warning_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="warning", gate_passed=True, passable_warning=True, warnings=["warn"])

        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, warning_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        rec = run.step_execution_records[-1]
        patch_artifacts = [dict(a or {}) for a in list(rec.artifacts or []) if dict(a or {}).get("artifact_type") == "patch_task"]
        self.assertTrue(patch_artifacts)
        art = patch_artifacts[0]
        self.assertEqual(str(art.get("run_id")), str(run.run_id))
        self.assertEqual(str(art.get("step_id")), STEP_P1_1_EXTRACT)
        self.assertEqual(int(art.get("attempt_no") or 0), 1)
        self.assertTrue(str(art.get("artifact_id") or "").strip())
        self.assertTrue(str(art.get("codex_prompt_artifact_id") or "").strip())
        self.assertEqual(str(dict(art.get("prompt_artifact") or {}).get("artifact_type") or ""), "patch_prompt_text")
        self.assertEqual(str(art.get("configured_model_name") or ""), "gpt-5.2-codex")

    def test_stage5_policy_requires_approval_for_patch_dispatch_in_balanced(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        stub = _StubPatchChainInterpreter(branch="patch_extractor", should_patch=True, patch_type="extractor")
        engine.chatgpt_interpreter = stub

        def warning_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="warning", gate_passed=True, passable_warning=True, warnings=["warn"])

        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, warning_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        pending = engine.list_pending_approvals(run.run_id)
        self.assertTrue(any(item.approval_type == ApprovalType.DISPATCH_PATCH_TASK for item in pending))
        self.assertTrue(any(e.event_type == "patch_dispatch_pending_approval" for e in run.events))
        self.assertTrue(any(e.event_type == "patch_recommendation_detected" for e in run.events))

    def test_stage5_balanced_profile_explicitly_disables_auto_dispatch(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        stub = _StubPatchChainInterpreter(branch="patch_extractor", should_patch=True, patch_type="extractor")
        engine.chatgpt_interpreter = stub

        def warning_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="warning", gate_passed=True, passable_warning=True, warnings=["warn"])

        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, warning_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        eval_events = [e for e in run.events if e.event_type == "patch_dispatch_evaluation"]
        self.assertTrue(eval_events)
        payload = dict(eval_events[-1].payload or {})
        self.assertEqual(payload.get("profile"), PolicyProfile.BALANCED.value)
        self.assertFalse(bool(payload.get("auto_dispatch_requested")))
        self.assertFalse(bool(payload.get("dispatch_allowed")))
        self.assertTrue(bool(payload.get("requires_approval")))
        self.assertEqual(str(payload.get("configured_model_name") or ""), "gpt-5.2-codex")
        self.assertEqual(str(payload.get("configured_provider_name") or ""), "codex_cli")

    def test_stage5_patch_generation_failure_is_safe_and_audited(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        stub = _StubPatchChainInterpreter(branch="patch_extractor", should_patch=True, patch_type="extractor", fail_generation=True)
        engine.chatgpt_interpreter = stub

        def warning_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="warning", gate_passed=True, passable_warning=True, warnings=["warn"])

        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, warning_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertTrue(any(e.event_type == "patch_task_generation_failed" for e in run.events))
        self.assertFalse(any(e.event_type == "patch_dispatch_requested" for e in run.events))
        self.assertEqual(str(run.step_execution_records[-1].validator_result.get("status") or ""), "warning")
        self.assertFalse(
            any(item.approval_type == ApprovalType.DISPATCH_PATCH_TASK for item in engine.list_pending_approvals(run.run_id))
        )

    def test_stage5_patch_dispatch_approval_resolution_executes_dispatch_and_records_status(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        stub = _StubPatchChainInterpreter(branch="patch_extractor", should_patch=True, patch_type="extractor")
        engine.chatgpt_interpreter = stub

        def warning_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="warning", gate_passed=True, passable_warning=True, warnings=["warn"])

        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, warning_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        pending = [item for item in engine.list_pending_approvals(run.run_id) if item.approval_type == ApprovalType.DISPATCH_PATCH_TASK]
        self.assertTrue(pending)

        run = engine.resolve_approval(
            run_id=run.run_id,
            approval_id=pending[0].approval_id,
            decision="approved",
            operator_id="op-patch",
            operator_role="admin",
            operator_decision="approve patch dispatch request",
            max_steps_after_resume=0,
        )

        dispatch_events = [e for e in run.events if e.event_type == "patch_dispatch_requested"]
        self.assertTrue(dispatch_events)
        self.assertEqual(str(dispatch_events[-1].payload.get("approval_id") or ""), str(pending[0].approval_id))
        self.assertTrue(list(run.resume_context.get("patch_dispatch_requests") or []))
        self.assertTrue(any(e.event_type == "patch_dispatch_completed" for e in run.events))
        self.assertEqual(len(self._patch_dispatch_calls), 1)
        latest_record = next((r for r in engine.list_patch_registry_records(run.run_id) if str(r.get("patch_type") or "") == "extractor"), {})
        self.assertIn(str(latest_record.get("status") or ""), {"retest_pending", "dispatched"})
        self.assertEqual(str(dict(latest_record.get("dispatch_metadata") or {}).get("codex_dispatch_status") or ""), "success")

    def test_stage5_patch_dispatch_failure_is_visible_and_not_silent(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        engine.patch_dispatch_runner = lambda **kwargs: {
            "status": "failed",
            "target": str(kwargs.get("target") or ""),
            "configured_provider_name": "codex_cli",
            "configured_model_name": "gpt-5.2-codex",
            "error_summary": "runner_unreachable",
            "return_code": 17,
        }
        stub = _StubPatchChainInterpreter(branch="patch_extractor", should_patch=True, patch_type="extractor")
        engine.chatgpt_interpreter = stub

        def warning_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="warning", gate_passed=True, passable_warning=True, warnings=["warn"])

        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, warning_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        pending = [item for item in engine.list_pending_approvals(run.run_id) if item.approval_type == ApprovalType.DISPATCH_PATCH_TASK]
        self.assertTrue(pending)

        run = engine.resolve_approval(
            run_id=run.run_id,
            approval_id=pending[0].approval_id,
            decision="approved",
            operator_id="op-patch",
            operator_role="admin",
            operator_decision="approve patch dispatch request",
            max_steps_after_resume=0,
        )

        self.assertTrue(any(e.event_type == "patch_dispatch_failed" for e in run.events))
        record = next((r for r in engine.list_patch_registry_records(run.run_id) if str(r.get("patch_type") or "") == "extractor"), {})
        self.assertEqual(str(record.get("status") or ""), "failed")
        self.assertEqual(str(dict(record.get("dispatch_metadata") or {}).get("codex_dispatch_status") or ""), "failed")

    def test_stage5_patch_chain_does_not_bypass_blocked_gate_result(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        stub = _StubPatchChainInterpreter(branch="patch_detector_scoring", should_patch=True, patch_type="detector_scoring")
        engine.chatgpt_interpreter = stub

        def blocked_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(
                status="blocked",
                gate_passed=False,
                block_reason_code=BlockReasonCode.STEP20_UNMATCHED_BLOCKING,
                summary="unmatched stops blocking gate",
                evidence={"unmatched_count": 4, "ambiguous_count": 0},
            )

        engine.register_validator(self.registry[STEP_P3_2_STEP20].validator, blocked_validator)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = engine.advance_run(run.run_id)

        self.assertEqual(run.status, "waiting_for_approval")
        self.assertEqual(run.current_step_id, STEP_P1_3B_NEW_NODES)
        rec = run.step_execution_records[-1]
        self.assertEqual(str(rec.validator_result.get("status") or ""), "blocked")
        block_code = str((rec.block_reason or {}).get("code") or "")
        self.assertIn(block_code, {BlockReasonCode.STEP20_UNMATCHED_BLOCKING.value, str(BlockReasonCode.STEP20_UNMATCHED_BLOCKING)})
        self.assertTrue(any(e.event_type == "step_blocked" for e in run.events))
        self.assertTrue(any(e.event_type.startswith("patch_task_generation_") for e in run.events))

    def test_stage6_patch_registry_record_created_from_stage5_patch_context(self) -> None:
        engine, _stub, run, patch_task_id = self._run_stage5_patch_context(branch="patch_extractor", patch_type="extractor")
        records = engine.list_patch_registry_records(run.run_id)
        self.assertTrue(records)
        row = dict(records[0] or {})
        self.assertEqual(str(row.get("patch_task_id") or ""), patch_task_id)
        self.assertEqual(str(row.get("run_id") or ""), str(run.run_id))
        self.assertEqual(str(row.get("phase") or ""), "phase1")
        self.assertEqual(str(row.get("step_id") or ""), STEP_P1_1_EXTRACT)
        self.assertIn(str(row.get("status") or ""), {"pending_approval", "retest_pending", "dispatched"})
        self.assertEqual(str(row.get("comparator_outcome") or ""), "not_run")
        self.assertTrue(any(e.event_type == "patch_registry_record_created" for e in run.events))
        self.assertTrue(any(e.event_type == "patch_registry_status_updated" for e in run.events))

    def test_stage6_comparator_invocation_uses_bounded_payload_and_schema_path(self) -> None:
        engine, stub, run, patch_task_id = self._run_stage5_patch_context(branch="patch_detector_scoring", patch_type="detector_scoring")
        out = engine.run_patch_retest_comparator(
            run_id=run.run_id,
            patch_task_id=patch_task_id,
            baseline_run_snapshot={
                "run_id": "run-baseline",
                "phase": "phase3",
                "step_id": STEP_P3_2_STEP20,
                "timestamp": "2026-03-04T00:00:00Z",
                "metrics": {"quality_score": 0.60, "warning_count": 5, "unmatched_count": 6, "ambiguous_count": 2},
            },
            retest_run_snapshot={
                "run_id": "run-retest",
                "phase": "phase3",
                "step_id": STEP_P3_2_STEP20,
                "timestamp": "2026-03-04T00:20:00Z",
                "metrics": {"quality_score": 0.75, "warning_count": 2, "unmatched_count": 2, "ambiguous_count": 1},
            },
        )
        self.assertEqual(len(stub.compare_calls), 1)
        payload = dict(stub.compare_calls[0] or {})
        self.assertIn("baseline_run_snapshot", payload)
        self.assertIn("retest_run_snapshot", payload)
        self.assertIn("patch_record", payload)
        self.assertEqual(str(out.get("status") or ""), "compared")
        run = engine.get_run(run.run_id)
        self.assertTrue(any(e.event_type == "retest_comparator_started" for e in run.events))
        self.assertTrue(any(e.event_type == "retest_comparator_completed" for e in run.events))

    def test_stage6_comparator_result_persistence_updates_registry_to_compared(self) -> None:
        engine, _stub, run, patch_task_id = self._run_stage5_patch_context(branch="patch_extractor", patch_type="extractor")
        engine.run_patch_retest_comparator(
            run_id=run.run_id,
            patch_task_id=patch_task_id,
            baseline_run_snapshot={"run_id": "base", "phase": "phase1", "step_id": STEP_P1_1_EXTRACT, "metrics": {"quality_score": 0.55}},
            retest_run_snapshot={"run_id": "retest", "phase": "phase1", "step_id": STEP_P1_1_EXTRACT, "metrics": {"quality_score": 0.70}},
        )
        row = next((r for r in engine.list_patch_registry_records(run.run_id) if str(r.get("patch_task_id") or "") == patch_task_id), {})
        self.assertEqual(str(row.get("status") or ""), "compared")
        self.assertIn(str(row.get("comparator_outcome") or ""), {"improved", "regressed", "inconclusive"})
        self.assertTrue(dict(row.get("comparator_result_ref") or {}).get("schema_name"))

    def test_stage6_comparator_failure_is_safe_and_does_not_auto_accept(self) -> None:
        engine, stub, run, patch_task_id = self._run_stage5_patch_context(branch="patch_diagnostics", patch_type="diagnostics")
        stub.fail_compare = True
        with self.assertRaises(RuntimeError):
            engine.run_patch_retest_comparator(
                run_id=run.run_id,
                patch_task_id=patch_task_id,
                baseline_run_snapshot={"run_id": "base", "phase": "phase1", "step_id": STEP_P1_1_EXTRACT},
                retest_run_snapshot={"run_id": "retest", "phase": "phase1", "step_id": STEP_P1_1_EXTRACT},
            )
        row = next((r for r in engine.list_patch_registry_records(run.run_id) if str(r.get("patch_task_id") or "") == patch_task_id), {})
        self.assertEqual(str(row.get("status") or ""), "failed")
        self.assertNotEqual(str(row.get("operator_outcome_decision") or ""), "accepted")
        run = engine.get_run(run.run_id)
        self.assertTrue(any(e.event_type == "retest_comparator_failed" for e in run.events))

    def test_stage6_operator_decision_recording_updates_patch_registry_state(self) -> None:
        engine, _stub, run, patch_task_id = self._run_stage5_patch_context(branch="patch_extractor", patch_type="extractor")
        engine.run_patch_retest_comparator(
            run_id=run.run_id,
            patch_task_id=patch_task_id,
            baseline_run_snapshot={"run_id": "base", "phase": "phase1", "step_id": STEP_P1_1_EXTRACT},
            retest_run_snapshot={"run_id": "retest", "phase": "phase1", "step_id": STEP_P1_1_EXTRACT},
        )
        out = engine.record_patch_outcome_decision(
            run_id=run.run_id,
            patch_task_id=patch_task_id,
            decision="observe_more",
            operator_id="op-stage6",
            operator_role="admin",
            notes="Need one more controlled rerun.",
        )
        self.assertEqual(str(out.get("status") or ""), "observe_more")
        row = next((r for r in engine.list_patch_registry_records(run.run_id) if str(r.get("patch_task_id") or "") == patch_task_id), {})
        self.assertEqual(str(row.get("operator_outcome_decision") or ""), "observe_more")
        self.assertEqual(str(row.get("status") or ""), "observe_more")
        run = engine.get_run(run.run_id)
        self.assertTrue(any(e.event_type == "patch_outcome_recorded" for e in run.events))

    def test_stage6_patch_history_summary_hook_returns_bounded_fields(self) -> None:
        engine, _stub, run, patch_task_id = self._run_stage5_patch_context(branch="patch_extractor", patch_type="extractor")
        engine.run_patch_retest_comparator(
            run_id=run.run_id,
            patch_task_id=patch_task_id,
            baseline_run_snapshot={"run_id": "base", "phase": "phase1", "step_id": STEP_P1_1_EXTRACT},
            retest_run_snapshot={"run_id": "retest", "phase": "phase1", "step_id": STEP_P1_1_EXTRACT},
        )

        class _NoopAdvisoryService:
            def run_task_detailed(self, endpoint_task, envelope):
                del endpoint_task, envelope
                return {"response": {}, "meta": {}}

        interpreter = AdvisoryChatGPTInterpreter(advisory_service=_NoopAdvisoryService())
        state = engine.get_run(run.run_id)
        step = engine.step_registry[STEP_P1_1_EXTRACT]
        rec = state.step_execution_records[-1]
        snap = interpreter._build_normalized_snapshot(  # noqa: SLF001
            state=state,
            step=step,
            attempt_no=int(rec.attempt_no or 1),
            exec_result=ExecutorResult(ok=True, summary=dict(rec.executor_result_summary or {}), artifacts=[]),
            validator_result=ValidatorResult(status="warning", gate_passed=True, passable_warning=True, evidence={}),
            ai_snapshot=AIBotSnapshot(
                scores={"quality": 0.7},
                metrics={},
                warnings=[],
                anomaly_flags=[],
                proposals={},
                extractor_status={},
                extractor_help_needed={"reason_class": "retry_not_diversified"},
            ),
            trigger="warning",
        )
        summary = dict(snap.get("patch_history_summary") or {})
        self.assertIn("last_patch_task_id", summary)
        self.assertIn("last_comparator_outcome", summary)
        self.assertIn("record_count_total", summary)
        self.assertLessEqual(len(summary.keys()), 16)

    def test_phase1_generate_patch_task_envelope_includes_geographic_context(self) -> None:
        class _RecordingAdvisoryService:
            def __init__(self) -> None:
                self.calls = []

            def run_task_detailed(self, *, endpoint_task, envelope):
                self.calls.append({"task": endpoint_task, "envelope": dict(envelope or {})})
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {"contradictions_found": False, "contradictions": [], "consistency_summary": "ok"},
                        "meta": {"task": endpoint_task},
                    }
                if endpoint_task == "hades_pipeline_interpreter":
                    snapshot = dict(envelope.get("snapshot") or {})
                    return {
                        "response": {
                            "summary": "phase1 geography needs an extractor patch",
                            "dominant_cause_class": "extractor_logic",
                            "confidence": 0.81,
                            "secondary_causes": [],
                            "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                            "recommended_branch": "patch_extractor",
                            "recommended_next_actions": ["repair geography interpretation before more retries"],
                            "patch_task_recommendation": {
                                "should_create_patch_task": True,
                                "patch_type": "extractor",
                                "justification": "spatial interpretation failed repeatedly",
                                "suggested_target": "codex",
                            },
                            "operator_action_required": False,
                            "approval_type_if_needed": None,
                            "risk_notes": [],
                        },
                        "meta": {
                            "task": endpoint_task,
                            "model": "gpt-5.2",
                            "source": "openai",
                            "requested_mode": "real_advisory",
                            "fallback_used": False,
                            "prompt_package": {"snapshot": snapshot},
                        },
                    }
                if endpoint_task == "hades_patch_task_generator":
                    return {
                        "response": {
                            "patch_task": {
                                "title": "Patch Phase1 geography handling",
                                "prompt_text": "Fix spatial interpretation failures without weakening validator authority.",
                            }
                        },
                        "meta": {
                            "task": endpoint_task,
                            "model": "gpt-5.2",
                            "source": "openai",
                            "requested_mode": "real_advisory",
                            "fallback_used": False,
                            "prompt_package": {"task": endpoint_task},
                        },
                    }
                raise AssertionError(f"unexpected task {endpoint_task}")

        service = _RecordingAdvisoryService()
        interpreter = AdvisoryChatGPTInterpreter(advisory_service=service, advisory_mode="real_advisory")
        step = self.registry[STEP_P1_1_EXTRACT]
        state = RunSessionState(
            run_id="run-p1-patch-envelope",
            pipeline_scope={"phases": ["phase1"], "target_entities": "madeup nowhere"},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-p1-patch-envelope",
        )
        exec_result = ExecutorResult(
            ok=False,
            summary={
                "candidate_count": 0,
                "spatial_interpretation": {
                    "original_geographic_input": "madeup nowhere",
                    "geographic_input_type": "place_or_route_hint",
                    "geographic_interpretation_source": "phase1_runtime_scope",
                    "spatial_interpretation_status": "fallback_default_bbox",
                    "bbox_candidate": None,
                    "bbox_candidate_confidence": 0.12,
                    "bbox_validation_status": "missing",
                    "runtime_bbox_used": {"south": -0.38, "west": -78.60, "north": -0.02, "east": -78.35},
                    "effective_bbox_fingerprint": "bbox-p1-default",
                    "fallback_used": True,
                    "fallback_reason": "target_intent_unresolved",
                    "runtime_spatial_strategy_used": "default_bbox_fallback",
                    "target_option_received": True,
                    "target_option_text": "madeup nowhere",
                    "retry_changed_spatial_plan": False,
                    "same_spatial_plan_retry_count": 1,
                },
                "extraction_attempt_history_summary": {
                    "attempt_count_total": 2,
                    "successful_attempt_count": 0,
                    "non_empty_attempt_count": 0,
                    "same_config_repeat_count": 1,
                    "diversified_attempts": False,
                    "attempts": [
                        {"attempt_no": 1, "status": "empty", "effective_config_fingerprint": "fp1"},
                        {"attempt_no": 2, "status": "empty", "effective_config_fingerprint": "fp1"},
                    ],
                },
            },
            artifacts=[],
        )
        validator_result = ValidatorResult(status="blocked", gate_passed=False, evidence={"candidate_count": 0})
        ai_snapshot = AIBotSnapshot(
            extractor_status={},
            extractor_help_needed={
                "needed": True,
                "severity": "high",
                "confidence": 0.88,
                "reason_class": "spatial_interpretation_failed",
                "reasons": [{"code": "spatial_interpretation_failed"}],
            },
        )
        chatgpt_snapshot = interpreter(
            state,
            step,
            2,
            exec_result,
            validator_result,
            ai_snapshot,
            "blocked",
        )
        interpreter.generate_patch_task(
            state=state,
            step=step,
            attempt_no=2,
            trigger="blocked",
            exec_result=exec_result,
            validator_result=validator_result,
            ai_snapshot=ai_snapshot,
            chatgpt_snapshot=chatgpt_snapshot,
            patch_intent={
                "branch": "patch_extractor",
                "patch_type": "extractor",
                "suggested_target": "codex",
                "justification": "spatial interpretation failed repeatedly",
            },
        )
        generator_calls = [dict(x or {}) for x in list(service.calls or []) if str(dict(x or {}).get("task") or "") == "hades_patch_task_generator"]
        self.assertTrue(generator_calls)
        snapshot = dict(generator_calls[-1].get("envelope", {}).get("snapshot") or {})
        geo = dict(snapshot.get("geographic_context") or {})
        self.assertEqual(str(geo.get("original_geographic_input") or ""), "madeup nowhere")
        self.assertEqual(str(geo.get("geographic_interpretation_status") or ""), "fallback_default_bbox")
        self.assertTrue(bool(geo.get("fallback_used")))
        self.assertEqual(str(geo.get("fallback_reason") or ""), "target_intent_unresolved")
        self.assertEqual(str(dict(snapshot.get("patch_intent") or {}).get("patch_type") or ""), "extractor")

    def test_phase3_generate_patch_task_envelope_includes_route_extractor_packet(self) -> None:
        class _RecordingAdvisoryService:
            def __init__(self) -> None:
                self.calls = []

            def run_task_detailed(self, *, endpoint_task, envelope):
                self.calls.append({"task": endpoint_task, "envelope": dict(envelope or {})})
                if endpoint_task == "hades_evidence_consistency_checker":
                    return {
                        "response": {"contradictions_found": False, "contradictions": [], "consistency_summary": "ok"},
                        "meta": {"task": endpoint_task},
                    }
                if endpoint_task == "hades_pipeline_interpreter":
                    snapshot = dict(envelope.get("snapshot") or {})
                    return {
                        "response": {
                            "summary": "phase3 extractor needs a patch",
                            "dominant_cause_class": "extractor_logic",
                            "confidence": 0.78,
                            "secondary_causes": [],
                            "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
                            "recommended_branch": "patch_extractor",
                            "recommended_next_actions": ["broaden bbox-first candidate universe"],
                            "patch_task_recommendation": {
                                "should_create_patch_task": True,
                                "patch_type": "extractor",
                                "justification": "candidate universe too small",
                                "suggested_target": "codex",
                            },
                            "operator_action_required": False,
                            "approval_type_if_needed": None,
                            "risk_notes": [],
                        },
                        "meta": {
                            "task": endpoint_task,
                            "model": "gpt-5.2",
                            "source": "openai",
                            "requested_mode": "real_advisory",
                            "fallback_used": False,
                            "prompt_package": {"snapshot": snapshot},
                        },
                    }
                if endpoint_task == "hades_patch_task_generator":
                    return {
                        "response": {
                            "patch_task": {
                                "title": "Patch Phase3 discover",
                                "prompt_text": "Broaden Phase3 discover and preserve authority boundaries.",
                            }
                        },
                        "meta": {
                            "task": endpoint_task,
                            "model": "gpt-5.2",
                            "source": "openai",
                            "requested_mode": "real_advisory",
                            "fallback_used": False,
                            "prompt_package": {"task": endpoint_task},
                        },
                    }
                raise AssertionError(f"unexpected task {endpoint_task}")

        service = _RecordingAdvisoryService()
        interpreter = AdvisoryChatGPTInterpreter(advisory_service=service, advisory_mode="real_advisory")
        step = self.registry[STEP_P3_1_EXTRACT]
        state = RunSessionState(
            run_id="run-p3-patch-envelope",
            pipeline_scope={
                "phases": ["phase3"],
                "phase3": {
                    "bbox": {"south": -0.32, "west": -78.58, "north": -0.09, "east": -78.31},
                    "refs": ["E1"],
                    "operator": "Metro",
                    "name": "Ecovia",
                    "service_route_id": "sr-1",
                    "direction_id": 0,
                    "query_strategy": "bbox_first_broad",
                },
            },
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
            trace_id="trace-p3-patch-envelope",
        )
        exec_result = ExecutorResult(
            ok=True,
            summary={
                "route_id": str(uuid4()),
                "prior_stop_count": 2,
                "query_strategy": "bbox_first_broad",
                "spatial_interpretation": {
                    "original_geographic_input": "Conocoto terminal corridor",
                    "target_option_text": "Conocoto terminal corridor",
                    "geographic_input_type": "place_or_route_hint",
                    "geographic_interpretation_source": "runtime_phase3_scope",
                    "spatial_interpretation_status": "fallback_default_bbox",
                    "bbox_candidate": None,
                    "bbox_candidate_confidence": 0.22,
                    "bbox_validation_status": "missing",
                    "runtime_bbox_used": {"south": -0.38, "west": -78.60, "north": -0.02, "east": -78.35},
                    "effective_bbox_fingerprint": "bbox-default",
                    "fallback_used": True,
                    "fallback_reason": "phase3_scope_bbox_missing",
                    "runtime_spatial_strategy_used": "default_bbox_fallback",
                    "target_option_received": True,
                },
                "candidate_universe_summary": {
                    "candidate_universe_count": 4,
                    "candidate_scored_count": 4,
                    "candidate_fetch_evaluated_count": 2,
                    "query_strategy": "bbox_first_broad",
                    "hard_filters_applied": [],
                    "soft_signals_used": ["refs", "operator", "name"],
                },
                "selection_summary": {
                    "selection_status": "provisional_selected",
                    "selected_osm_relation_id": 321,
                    "selected_rank": 1,
                    "selected_score": 12.1,
                    "selection_confidence": 0.34,
                    "score_gap_top2": 0.3,
                    "selected_relation_stop_prior_count": 2,
                    "selection_reason_codes": ["soft_ref_match"],
                },
                "selected_relation_summary": {
                    "osm_relation_id": 321,
                    "selection_rank": 1,
                    "selection_confidence": 0.34,
                    "score": 12.1,
                    "stop_prior_count": 2,
                },
                "extractor_diagnostics": {
                    "candidate_count": 4,
                    "signal_strength": "low",
                    "quality_flags": ["discover_candidate_pool_single"],
                },
                "extraction_attempt_history_summary": {
                    "attempt_count_total": 2,
                    "successful_attempt_count": 1,
                    "non_empty_attempt_count": 1,
                    "same_config_repeat_count": 1,
                    "diversified_attempts": False,
                    "attempts": [
                        {"attempt_no": 1, "status": "error", "effective_config_fingerprint": "fp1"},
                        {"attempt_no": 2, "status": "success", "effective_config_fingerprint": "fp1"},
                    ],
                },
            },
            artifacts=[],
        )
        validator_result = ValidatorResult(status="warning", gate_passed=True, evidence={"prior_stop_count": 2})
        ai_snapshot = AIBotSnapshot(
            extractor_status={},
            extractor_help_needed={
                "needed": True,
                "severity": "high",
                "confidence": 0.79,
                "reason_class": "candidate_universe_too_small",
                "reasons": [{"code": "candidate_universe_too_small"}],
            },
        )
        chatgpt_snapshot = interpreter(
            state,
            step,
            1,
            exec_result,
            validator_result,
            ai_snapshot,
            "warning",
        )
        interpreter.generate_patch_task(
            state=state,
            step=step,
            attempt_no=1,
            trigger="warning",
            exec_result=exec_result,
            validator_result=validator_result,
            ai_snapshot=ai_snapshot,
            chatgpt_snapshot=chatgpt_snapshot,
            patch_intent={
                "branch": "patch_extractor",
                "patch_type": "extractor",
                "suggested_target": "codex",
                "justification": "candidate universe too small",
            },
        )
        generator_calls = [dict(x or {}) for x in list(service.calls or []) if str(dict(x or {}).get("task") or "") == "hades_patch_task_generator"]
        self.assertTrue(generator_calls)
        snapshot = dict(generator_calls[-1].get("envelope", {}).get("snapshot") or {})
        packet = dict(snapshot.get("phase3_route_extractor_packet") or {})
        self.assertTrue(packet)
        self.assertEqual(int(dict(packet.get("candidate_universe") or {}).get("count_total") or 0), 4)
        self.assertEqual(float(dict(packet.get("candidate_universe") or {}).get("selection_confidence") or 0.0), 0.34)
        self.assertEqual(str(dict(packet.get("selection") or {}).get("selected_osm_relation_id") or ""), "321")
        self.assertEqual(str(dict(snapshot.get("patch_intent") or {}).get("patch_type") or ""), "extractor")
        geo = dict(snapshot.get("geographic_context") or {})
        self.assertEqual(str(geo.get("geographic_interpretation_status") or ""), "fallback_default_bbox")
        self.assertTrue(bool(geo.get("fallback_used")))
        self.assertEqual(str(geo.get("fallback_reason") or ""), "phase3_scope_bbox_missing")

    def test_stage6_authority_safe_comparator_does_not_mutate_validator_gate_result(self) -> None:
        engine, _stub, run, patch_task_id = self._run_stage5_patch_context(branch="patch_extractor", patch_type="extractor")
        before = dict(run.step_execution_records[-1].validator_result or {})
        engine.run_patch_retest_comparator(
            run_id=run.run_id,
            patch_task_id=patch_task_id,
            baseline_run_snapshot={"run_id": "base", "phase": "phase1", "step_id": STEP_P1_1_EXTRACT},
            retest_run_snapshot={"run_id": "retest", "phase": "phase1", "step_id": STEP_P1_1_EXTRACT},
        )
        after_run = engine.get_run(run.run_id)
        after = dict(after_run.step_execution_records[-1].validator_result or {})
        self.assertEqual(before.get("status"), after.get("status"))
        self.assertEqual(before.get("gate_passed"), after.get("gate_passed"))

    def test_stage6_backward_compatibility_without_patch_registry_data(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_2_CHAIN,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        self.assertEqual(engine.list_patch_registry_records(run.run_id), [])
        self.assertIn(run.status, {"running", "paused", "waiting_for_approval", "completed"})

    def test_stage7_no_history_uses_default_retry_order(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={
                    "candidate_count": 0,
                    "retry_strategy": None,
                    "extraction_attempt_history_summary": {"attempt_count_total": 0, "attempts": []},
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_LOW_COVERAGE,
                    summary="low coverage",
                    evidence={"candidate_count": 0},
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        retry_events = [e for e in run.events if e.event_type == "step_retry_scheduled"]
        self.assertTrue(retry_events)
        self.assertEqual(str(retry_events[0].payload.get("retry_strategy") or ""), "bbox_expand")
        skipped = [e for e in run.events if e.event_type == "adaptive_retry_plan_skipped"]
        self.assertTrue(skipped)
        self.assertIn(str(skipped[-1].payload.get("skip_reason") or ""), {"insufficient_history", "no_adaptive_delta"})
        reason_codes = list(skipped[-1].payload.get("reason_codes") or [])
        self.assertTrue("missing_attempt_history" in reason_codes or "adaptive_low_history_support" in reason_codes)
        self.assertNotIn("same_config_loop_risk", list(skipped[-1].payload.get("reason_codes") or []))

    def test_p11_attempt4_blocked_has_coherent_attempt_history_summary(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=False,
                summary={
                    "candidate_count": 0,
                    "quality_score": 0.11,
                    "retry_strategy": "bbox_expand",
                    "extraction_attempt_history_summary": {"attempt_count_total": 0, "attempts": []},
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            return ValidatorResult(
                status="blocked",
                gate_passed=False,
                block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
                summary="empty extraction",
                warnings=[],
                evidence={"candidate_count": 0},
            )

        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=4)
        self.assertTrue(run.step_execution_records)
        last = run.step_execution_records[-1]
        self.assertEqual(int(last.attempt_no or 0), 4)
        hist = dict((last.executor_result_summary or {}).get("extraction_attempt_history_summary") or {})
        self.assertTrue(hist)
        attempt_count_total = int(hist.get("attempt_count_total") or hist.get("attempt_count") or 0)
        attempts = list(hist.get("attempts") or [])
        self.assertTrue(attempt_count_total >= 1 or not attempts)
        if attempt_count_total > 0:
            self.assertTrue(attempts)
            self.assertLessEqual(len(attempts), 8)
        warnings = set(list((last.ai_bot_snapshot or {}).get("warnings") or []))
        if attempt_count_total <= 0:
            self.assertIn("missing_attempt_history", warnings)
            self.assertNotIn("repeated_empty_extraction", warnings)

    def test_stage7_p1_history_favors_alternate_fallback(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={
                    "candidate_count": 0,
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 3,
                        "same_config_repeat_count": 0,
                        "retry_diversity_count": 2,
                        "attempts": [
                            {"attempt_no": 1, "status": "empty", "retry_strategy": "bbox_expand", "candidate_count": 0},
                            {"attempt_no": 2, "status": "success", "retry_strategy": "template_alternative", "candidate_count": 22},
                            {"attempt_no": 3, "status": "success", "retry_strategy": "template_alternative", "candidate_count": 18},
                        ],
                    },
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_LOW_COVERAGE,
                    summary="retryable low coverage",
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        retry_events = [e for e in run.events if e.event_type == "step_retry_scheduled"]
        self.assertTrue(retry_events)
        self.assertEqual(str(retry_events[0].payload.get("retry_strategy") or ""), "template_alternative")
        applied = [e for e in run.events if e.event_type == "adaptive_retry_plan_applied"]
        self.assertTrue(applied)
        self.assertTrue(bool(applied[-1].payload.get("reordered")))
        self.assertFalse(any(e.event_type == "adaptive_early_escalation_bias_applied" for e in run.events))

    def test_stage7_p1_known_bad_pattern_applies_early_escalation_bias(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={
                    "candidate_count": 0,
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 4,
                        "same_config_repeat_count": 3,
                        "retry_diversity_count": 1,
                        "attempts": [
                            {"attempt_no": 1, "status": "empty", "retry_strategy": "bbox_expand", "candidate_count": 0},
                            {"attempt_no": 2, "status": "empty", "retry_strategy": "bbox_expand", "candidate_count": 0},
                            {"attempt_no": 3, "status": "empty", "retry_strategy": "bbox_expand", "candidate_count": 0},
                            {"attempt_no": 4, "status": "empty", "retry_strategy": "bbox_expand", "candidate_count": 0},
                        ],
                    },
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(
                status="blocked",
                gate_passed=False,
                block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
                summary="empty extraction",
            )

        def ai_stub(state, step, attempt_no, exec_result, validator_result):
            del state, step, attempt_no, exec_result, validator_result
            extractor_status = {
                "phase": "phase1",
                "scores": {"completion_quality_score": 0.22},
                "warnings": ["repeated_empty_extraction", "retry_not_diversified"],
                "efficiency_metrics": {
                    "attempt_count_total": 4,
                    "same_config_repeat_count": 3,
                    "retry_diversity_count": 1,
                    "fallback_rescue_success": False,
                },
                "extractor_efficiency_health_score": 0.21,
            }
            return AIBotSnapshot(
                scores={"quality": 0.2},
                metrics={"extractor_status": extractor_status},
                warnings=["repeated_empty_extraction"],
                anomaly_flags=[],
                proposals={},
                extractor_status=extractor_status,
                extractor_help_needed={
                    "needed": True,
                    "severity": "high",
                    "reason_class": "repeated_empty_extraction",
                    "reasons": [{"code": "repeated_empty_extraction"}],
                    "evidence": {"attempt_count_total": 4},
                },
            )

        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        engine.register_ai_hook(self.registry[STEP_P1_1_EXTRACT].ai_bot_hooks[0], ai_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run.patch_registry["patch-known-bad"] = {
            "patch_task_id": "patch-known-bad",
            "phase": "phase1",
            "step_id": STEP_P1_1_EXTRACT,
            "origin_block_reason_code": BlockReasonCode.EXTRACTION_EMPTY.value,
            "trigger_reason_class": "repeated_empty_extraction",
            "last_patch_type": "extractor",
            "patch_type": "extractor",
            "last_comparator_outcome": "regressed",
            "comparator_outcome": "regressed",
            "updated_at": "2026-03-05T00:00:00+00:00",
            "created_at": "2026-03-05T00:00:00+00:00",
        }
        run = engine.advance_run(run.run_id)

        self.assertEqual(int(run.attempt_counters.get(STEP_P1_1_EXTRACT) or 0), 2)
        self.assertEqual(run.status, "paused")
        self.assertEqual(str((run.step_execution_records[-1].validator_result or {}).get("status") or ""), "blocked")
        applied = [e for e in run.events if e.event_type == "adaptive_retry_plan_applied"]
        self.assertTrue(applied)
        self.assertEqual(str(applied[-1].payload.get("escalation_bias") or ""), "early_escalation")
        self.assertIsNotNone(applied[-1].payload.get("max_attempts_override"))
        early_events = [e for e in run.events if e.event_type == "adaptive_early_escalation_bias_applied"]
        self.assertTrue(early_events)

    def test_stage7_spatial_noop_retry_loop_caps_default_retries(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        step = self.registry[STEP_P1_1_EXTRACT]
        state = RunSessionState(
            run_id="run-stage7-spatial-noop",
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase1",
            current_step_id=STEP_P1_1_EXTRACT,
            trace_id="trace-stage7-spatial-noop",
        )
        exec_result = ExecutorResult(
            ok=False,
            summary={
                "candidate_count": 0,
                "spatial_interpretation": {
                    "target_option_received": True,
                    "target_option_text": "madeup nowhere",
                    "spatial_interpretation_status": "fallback_default_bbox",
                    "bbox_candidate": None,
                    "runtime_bbox_used": {"south": -0.38, "west": -78.6, "north": -0.02, "east": -78.35},
                    "runtime_spatial_strategy_used": "default_bbox_fallback",
                    "retry_changed_spatial_plan": False,
                    "same_spatial_plan_retry_count": 1,
                    "spatial_interpretation_failure_reason": "target_intent_unresolved",
                },
                "extraction_attempt_history_summary": {
                    "attempt_count_total": 2,
                    "same_config_repeat_count": 1,
                    "retry_diversity_count": 1,
                    "attempts": [
                        {
                            "attempt_no": 1,
                            "status": "empty",
                            "retry_strategy": "bbox_expand",
                            "candidate_count": 0,
                            "spatial_interpretation_status": "fallback_default_bbox",
                            "retry_changed_spatial_plan": True,
                            "target_option_received": True,
                        },
                        {
                            "attempt_no": 2,
                            "status": "empty",
                            "retry_strategy": "bbox_expand",
                            "candidate_count": 0,
                            "spatial_interpretation_status": "fallback_default_bbox",
                            "retry_changed_spatial_plan": False,
                            "target_option_received": True,
                        },
                    ],
                },
            },
            artifacts=[],
        )
        validator = ValidatorResult(
            status="blocked",
            gate_passed=False,
            block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
            warnings=["spatial_interpretation_failed", "spatial_plan_reused_without_change"],
        )
        ai_snapshot = AIBotSnapshot(
            extractor_status={
                "phase": "phase1",
                "warnings": ["spatial_interpretation_failed", "spatial_plan_reused_without_change"],
                "spatial_metrics": {
                    "target_option_received": True,
                    "target_option_text": "madeup nowhere",
                    "spatial_interpretation_status": "fallback_default_bbox",
                    "bbox_candidate": None,
                    "runtime_bbox_used": {"south": -0.38, "west": -78.6, "north": -0.02, "east": -78.35},
                    "runtime_spatial_strategy_used": "default_bbox_fallback",
                    "retry_changed_spatial_plan": False,
                    "same_spatial_plan_retry_count": 1,
                    "spatial_interpretation_failure_reason": "target_intent_unresolved",
                    "target_intent_ignored": True,
                },
                "efficiency_metrics": {
                    "attempt_count_total": 2,
                    "same_config_repeat_count": 1,
                    "retry_diversity_count": 1,
                },
                "scores": {
                    "completion_quality_score": 0.21,
                    "extractor_efficiency_health_score": 0.31,
                },
            },
            extractor_help_needed={
                "needed": True,
                "severity": "high",
                "reason_class": "spatial_interpretation_failed",
            },
        )
        plan = engine._build_adaptive_retry_plan(
            state=state,
            step=step,
            attempt_no=2,
            exec_result=exec_result,
            validator_result=validator,
            ai_snapshot=ai_snapshot,
        )
        self.assertEqual(str(plan.get("escalation_bias") or ""), "early_escalation")
        self.assertEqual(int(plan.get("max_attempts_override") or 0), 2)
        self.assertIn(
            "p1_spatial_noop_retry_loop",
            {str(dict(r or {}).get("code") or "") for r in list(plan.get("reasons") or [])},
        )

    def test_stage7_p3_step20_poor_pattern_deprioritizes_extractor_only_strategy(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={
                    "route_id": str(uuid4()),
                    "prior_stop_count": 0,
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 3,
                        "same_config_repeat_count": 1,
                        "retry_diversity_count": 2,
                        "attempts": [
                            {"attempt_no": 1, "status": "success", "retry_strategy": "template_alternative", "candidate_count": 9},
                            {"attempt_no": 2, "status": "success", "retry_strategy": "template_alternative", "candidate_count": 8},
                            {"attempt_no": 3, "status": "error", "retry_strategy": "fallback_config", "candidate_count": 0},
                        ],
                    },
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
                    summary="weak extraction context",
                )
            return ValidatorResult(status="pass", gate_passed=True)

        def ai_stub(state, step, attempt_no, exec_result, validator_result):
            del state, step, attempt_no, exec_result, validator_result
            extractor_status = {
                "phase": "phase3",
                "extractor_efficiency_health_score": 0.84,
                "scores": {"order_completion_quality_score": 0.33},
                "warnings": ["extraction_success_but_step20_poor"],
                "completion_metrics": {
                    "step20_available": True,
                    "step20_gate_passed": False,
                    "matched_count": 12,
                    "unmatched_count": 5,
                    "ambiguous_count": 1,
                },
                "efficiency_metrics": {
                    "attempt_count_total": 3,
                    "same_config_repeat_count": 1,
                    "retry_diversity_count": 2,
                },
            }
            return AIBotSnapshot(
                scores={"quality": 0.44},
                metrics={"extractor_status": extractor_status},
                warnings=["extraction_success_but_step20_poor"],
                anomaly_flags=[],
                proposals={},
                extractor_status=extractor_status,
                extractor_help_needed={"needed": True, "severity": "high", "reason_class": "low_order_completion_quality"},
            )

        engine.register_executor(self.registry[STEP_P3_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P3_1_EXTRACT].validator, validator_stub)
        engine.register_ai_hook(self.registry[STEP_P3_1_EXTRACT].ai_bot_hooks[0], ai_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P3_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        retry_events = [e for e in run.events if e.event_type == "step_retry_scheduled"]
        self.assertTrue(retry_events)
        self.assertEqual(str(retry_events[0].payload.get("retry_strategy") or ""), "fallback_config")
        applied = [e for e in run.events if e.event_type == "adaptive_retry_plan_applied"]
        self.assertTrue(applied)
        self.assertEqual(str(applied[-1].payload.get("escalation_bias") or ""), "prefer_patch_eval")

    def test_stage7_p3_sparse_history_keeps_default_order(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={
                    "route_id": str(uuid4()),
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 1,
                        "same_config_repeat_count": 0,
                        "retry_diversity_count": 1,
                        "attempts": [
                            {"attempt_no": 1, "status": "success", "retry_strategy": "template_alternative", "candidate_count": 4}
                        ],
                    },
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_executor(self.registry[STEP_P3_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P3_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P3_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        retry_events = [e for e in run.events if e.event_type == "step_retry_scheduled"]
        self.assertTrue(retry_events)
        self.assertEqual(str(retry_events[0].payload.get("retry_strategy") or ""), "template_alternative")
        skipped = [e for e in run.events if e.event_type == "adaptive_retry_plan_skipped"]
        self.assertTrue(skipped)
        self.assertNotIn("same_config_loop_risk", list(skipped[-1].payload.get("reason_codes") or []))

    def test_stage7_same_config_loop_prefers_diversification(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={
                    "retry_strategy": "bbox_expand",
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 3,
                        "same_config_repeat_count": 3,
                        "retry_diversity_count": 1,
                        "attempts": [
                            {"attempt_no": 1, "status": "empty", "retry_strategy": "bbox_expand", "candidate_count": 0},
                            {"attempt_no": 2, "status": "empty", "retry_strategy": "bbox_expand", "candidate_count": 0},
                            {"attempt_no": 3, "status": "empty", "retry_strategy": "bbox_expand", "candidate_count": 0},
                        ],
                    },
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_LOW_COVERAGE,
                    warnings=["retry_not_diversified"],
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        retry_events = [e for e in run.events if e.event_type == "step_retry_scheduled"]
        self.assertTrue(retry_events)
        self.assertEqual(str(retry_events[0].payload.get("retry_strategy") or ""), "template_alternative")
        applied = [e for e in run.events if e.event_type == "adaptive_retry_plan_applied"]
        self.assertTrue(applied)
        self.assertEqual(str(applied[-1].payload.get("escalation_bias") or ""), "diversify_retries")
        self.assertIn("same_config_loop_risk", list(applied[-1].payload.get("reason_codes") or []))

    def test_stage71_p3_early_escalation_observability_with_retry_depth_reduction(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={
                    "route_id": str(uuid4()),
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 4,
                        "same_config_repeat_count": 3,
                        "retry_diversity_count": 1,
                        "attempts": [
                            {"attempt_no": 1, "status": "empty", "retry_strategy": "template_alternative", "candidate_count": 0},
                            {"attempt_no": 2, "status": "empty", "retry_strategy": "template_alternative", "candidate_count": 0},
                            {"attempt_no": 3, "status": "empty", "retry_strategy": "template_alternative", "candidate_count": 0},
                            {"attempt_no": 4, "status": "empty", "retry_strategy": "fallback_config", "candidate_count": 0},
                        ],
                    },
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(
                status="blocked",
                gate_passed=False,
                block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
                summary="weak extraction context",
                warnings=["retry_not_diversified"],
            )

        def ai_stub(state, step, attempt_no, exec_result, validator_result):
            del state, step, attempt_no, exec_result, validator_result
            extractor_status = {
                "phase": "phase3",
                "extractor_efficiency_health_score": 0.29,
                "scores": {"order_completion_quality_score": 0.30},
                "warnings": ["repeated_corridor_extractor_failure", "extraction_success_but_step20_poor", "retry_not_diversified"],
                "completion_metrics": {"step20_available": True, "step20_gate_passed": False},
                "efficiency_metrics": {
                    "attempt_count_total": 4,
                    "same_config_repeat_count": 3,
                    "retry_diversity_count": 1,
                },
            }
            return AIBotSnapshot(
                scores={"quality": 0.20},
                metrics={"extractor_status": extractor_status},
                warnings=list(extractor_status["warnings"]),
                anomaly_flags=[],
                proposals={},
                extractor_status=extractor_status,
                extractor_help_needed={"needed": True, "severity": "high", "reason_class": "low_order_completion_quality"},
            )

        engine.register_executor(self.registry[STEP_P3_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P3_1_EXTRACT].validator, validator_stub)
        engine.register_ai_hook(self.registry[STEP_P3_1_EXTRACT].ai_bot_hooks[0], ai_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P3_1_EXTRACT,
        )
        run.patch_registry["patch-weak"] = {
            "patch_task_id": "patch-weak",
            "phase": "phase3",
            "step_id": STEP_P3_1_EXTRACT,
            "origin_block_reason_code": BlockReasonCode.EXTRACTION_EMPTY.value,
            "trigger_reason_class": "low_order_completion_quality",
            "last_patch_type": "extractor",
            "patch_type": "extractor",
            "last_comparator_outcome": "inconclusive",
            "comparator_outcome": "inconclusive",
            "updated_at": "2026-03-05T00:00:00+00:00",
            "created_at": "2026-03-05T00:00:00+00:00",
        }
        run = engine.advance_run(run.run_id, max_steps=1)

        applied = [e for e in run.events if e.event_type == "adaptive_retry_plan_applied"]
        self.assertTrue(applied)
        self.assertEqual(str(applied[-1].payload.get("escalation_bias") or ""), "early_escalation")
        self.assertIsNotNone(applied[-1].payload.get("max_attempts_override"))
        early = [e for e in run.events if e.event_type == "adaptive_early_escalation_bias_applied"]
        self.assertTrue(early)

    def test_stage7_non_extractor_steps_are_not_adaptively_reordered(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)

        def exec_stub(state, step, attempt_no, params):
            del state, step, attempt_no, params
            return ExecutorResult(ok=True, summary={"semantic_ok": False}, artifacts=[])

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.SEMANTIC_PIPELINE_FAILED,
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_executor(self.registry[STEP_P2_1_SEMANTIC].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P2_1_SEMANTIC].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase2"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P2_1_SEMANTIC,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        retry_events = [e for e in run.events if e.event_type == "step_retry_scheduled"]
        self.assertTrue(retry_events)
        self.assertEqual(str(retry_events[0].payload.get("retry_strategy") or ""), "fallback_config")
        adaptive_events = [e for e in run.events if str(e.event_type).startswith("adaptive_retry_plan")]
        self.assertEqual(adaptive_events, [])

    def test_stage7_adaptive_payload_is_bounded_and_visible_in_snapshot(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            attempts = []
            for idx in range(20):
                attempts.append(
                    {
                        "attempt_no": idx + 1,
                        "status": "empty",
                        "retry_strategy": "bbox_expand",
                        "candidate_count": 0,
                    }
                )
            return ExecutorResult(
                ok=True,
                summary={
                    "retry_strategy": "bbox_expand",
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 20,
                        "same_config_repeat_count": 19,
                        "retry_diversity_count": 1,
                        "attempts": attempts,
                    },
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
                    warnings=["retry_not_diversified", "same_config_retry_loop"],
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        rec = run.step_execution_records[0]
        plan = dict((rec.executor_result_summary or {}).get("adaptive_retry_plan") or {})
        self.assertTrue(plan)
        self.assertLessEqual(len(list(plan.get("reasons") or [])), 8)
        eval_events = [e for e in run.events if e.event_type == "adaptive_retry_plan_evaluated"]
        self.assertTrue(eval_events)
        self.assertLessEqual(len(list(eval_events[-1].payload.get("reason_codes") or [])), 8)

    def test_stage7_feature_flag_off_preserves_legacy_retry_behavior(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        engine.feature_flags = engine.feature_flags.__class__(
            live_bridge_enabled=engine.feature_flags.live_bridge_enabled,
            db_persistence_enabled=engine.feature_flags.db_persistence_enabled,
            auto_resume_enabled=engine.feature_flags.auto_resume_enabled,
            policy_profiles_enabled=engine.feature_flags.policy_profiles_enabled,
            worker_enabled=engine.feature_flags.worker_enabled,
            slo_alerting_enabled=engine.feature_flags.slo_alerting_enabled,
            patch_chaining_enabled=engine.feature_flags.patch_chaining_enabled,
            patch_auto_dispatch_enabled=engine.feature_flags.patch_auto_dispatch_enabled,
            adaptive_retry_enabled=False,
        )

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 5,
                        "same_config_repeat_count": 4,
                        "retry_diversity_count": 1,
                        "attempts": [
                            {"attempt_no": 1, "status": "empty", "retry_strategy": "bbox_expand", "candidate_count": 0},
                            {"attempt_no": 2, "status": "success", "retry_strategy": "template_alternative", "candidate_count": 20},
                        ],
                    }
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_LOW_COVERAGE,
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        retry_events = [e for e in run.events if e.event_type == "step_retry_scheduled"]
        self.assertTrue(retry_events)
        self.assertEqual(str(retry_events[0].payload.get("retry_strategy") or ""), "bbox_expand")
        adaptive_events = [e for e in run.events if str(e.event_type).startswith("adaptive_")]
        self.assertEqual(adaptive_events, [])

    def test_stage8_flag_off_does_not_invoke_shadow_ranker(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        ranker_calls = {"n": 0}

        def ranker(_features):
            ranker_calls["n"] += 1
            return {"ranked_order": ["template_alternative", "bbox_expand", "fallback_config"], "confidence": 0.91}

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={
                    "candidate_count": 0,
                    "retry_strategy": None,
                    "extraction_attempt_history_summary": {"attempt_count_total": 0, "attempts": []},
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_LOW_COVERAGE,
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_shadow_learned_retry_ranker(ranker)
        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(ranker_calls["n"], 0)
        retry_events = [e for e in run.events if e.event_type == "step_retry_scheduled"]
        self.assertTrue(retry_events)
        self.assertEqual(str(retry_events[0].payload.get("retry_strategy") or ""), "bbox_expand")
        self.assertEqual([e for e in run.events if str(e.event_type).startswith("shadow_")], [])

    def test_stage8_flag_on_model_unavailable_emits_unavailable_without_runtime_change(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        self._set_feature_flags(engine, shadow_learned_retry_ranking_enabled=True)

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={"candidate_count": 0, "extraction_attempt_history_summary": {"attempt_count_total": 0, "attempts": []}},
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        retry_events = [e for e in run.events if e.event_type == "step_retry_scheduled"]
        self.assertTrue(retry_events)
        self.assertEqual(str(retry_events[0].payload.get("retry_strategy") or ""), "bbox_expand")
        self.assertTrue(any(e.event_type == "shadow_learned_retry_plan_evaluated" for e in run.events))
        self.assertTrue(any(e.event_type == "shadow_learned_retry_plan_unavailable" for e in run.events))
        self.assertTrue(any(e.event_type == "shadow_retry_plan_comparison_recorded" for e in run.events))
        rec = run.step_execution_records[0]
        shadow_plan = dict((rec.executor_result_summary or {}).get("shadow_learned_retry_plan") or {})
        self.assertFalse(bool(shadow_plan.get("available")))
        shadow_cmp = dict((rec.executor_result_summary or {}).get("shadow_retry_plan_comparison") or {})
        self.assertEqual(str(shadow_cmp.get("comparison_status") or ""), "unavailable")
        self.assertEqual(str((rec.validator_result or {}).get("status") or ""), "blocked")
        self.assertFalse(bool((rec.validator_result or {}).get("gate_passed")))

    def test_stage8_flag_on_model_available_logs_comparison_but_retry_uses_deterministic_order(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        self._set_feature_flags(engine, shadow_learned_retry_ranking_enabled=True)
        captured = {"n": 0}

        def ranker(_features):
            captured["n"] += 1
            return {
                "model_type": "shadow_stub",
                "model_name": "retry-ranker",
                "model_version": "v0",
                "ranked_order": ["template_alternative", "bbox_expand", "fallback_config"],
                "score_by_strategy": {
                    "template_alternative": 0.91,
                    "bbox_expand": 0.44,
                    "fallback_config": 0.40,
                },
                "confidence": 0.81,
                "support_level": "medium",
                "reason_codes": ["shadow_prefers_template"],
            }

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={"candidate_count": 0, "extraction_attempt_history_summary": {"attempt_count_total": 0, "attempts": []}},
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_LOW_COVERAGE,
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_shadow_learned_retry_ranker(ranker)
        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(captured["n"], 1)
        retry_events = [e for e in run.events if e.event_type == "step_retry_scheduled"]
        self.assertTrue(retry_events)
        self.assertEqual(str(retry_events[0].payload.get("retry_strategy") or ""), "bbox_expand")
        self.assertFalse(any(e.event_type == "shadow_learned_retry_plan_unavailable" for e in run.events))
        cmp_events = [e for e in run.events if e.event_type == "shadow_retry_plan_comparison_recorded"]
        self.assertTrue(cmp_events)
        self.assertEqual(str(cmp_events[-1].payload.get("comparison_status") or ""), "comparable")
        self.assertEqual(bool(cmp_events[-1].payload.get("top1_match")), False)
        rec = run.step_execution_records[0]
        ai_metrics = dict((rec.ai_bot_snapshot or {}).get("metrics") or {})
        self.assertTrue(bool(dict(ai_metrics.get("shadow_learned_retry_plan") or {}).get("available")))
        self.assertIn("shadow_retry_plan_comparison", ai_metrics)

    def test_stage8_p1_shadow_features_and_eval_row_are_recorded(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        self._set_feature_flags(engine, shadow_learned_retry_ranking_enabled=True)
        captured_features: list[dict] = []

        def ranker(features):
            captured_features.append(dict(features or {}))
            return {
                "ranked_order": ["template_alternative", "bbox_expand", "fallback_config"],
                "score_by_strategy": [{"strategy": "template_alternative", "score": 0.75}],
                "confidence": 0.64,
                "reason_codes": [f"reason_{idx}" for idx in range(20)],
            }

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={
                    "area_group": "urban_core",
                    "sector": "N1",
                    "bbox": {"south": -2.0, "west": -79.0, "north": -1.9, "east": -78.9},
                    "retry_strategy": "bbox_expand",
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 3,
                        "same_config_repeat_count": 3,
                        "retry_diversity_count": 1,
                        "attempts": [
                            {"attempt_no": 1, "status": "empty", "retry_strategy": "bbox_expand", "candidate_count": 0},
                            {"attempt_no": 2, "status": "empty", "retry_strategy": "bbox_expand", "candidate_count": 0},
                        ],
                    },
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
                    warnings=["retry_not_diversified"],
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_shadow_learned_retry_ranker(ranker)
        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertTrue(captured_features)
        context = dict(captured_features[-1].get("context") or {})
        self.assertEqual(str(context.get("phase") or ""), "phase1")
        self.assertEqual(str(context.get("area_group") or ""), "urban_core")
        rec = run.step_execution_records[0]
        eval_row = dict((rec.executor_result_summary or {}).get("shadow_retry_eval_row") or {})
        self.assertTrue(bool(eval_row.get("feature_summary_hash")))
        self.assertLessEqual(len(str(eval_row.get("feature_summary_hash") or "")), 24)
        plan = dict((rec.executor_result_summary or {}).get("shadow_learned_retry_plan") or {})
        self.assertLessEqual(len(list(plan.get("reason_codes") or [])), 8)
        eval_events = [e for e in run.events if e.event_type == "shadow_learned_retry_plan_evaluated"]
        self.assertTrue(eval_events)
        self.assertLessEqual(len(list(eval_events[-1].payload.get("reason_codes") or [])), 8)
        self.assertLessEqual(len(list(eval_events[-1].payload.get("ranked_order") or [])), 6)

    def test_stage8_p3_shadow_features_include_step20_context(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        self._set_feature_flags(engine, shadow_learned_retry_ranking_enabled=True)
        captured_features: list[dict] = []

        def ranker(features):
            captured_features.append(dict(features or {}))
            return {
                "ranked_order": ["template_alternative", "fallback_config", "bbox_expand"],
                "confidence": 0.73,
                "support_level": "medium",
            }

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(
                ok=True,
                summary={
                    "route_id": "route-123",
                    "retry_strategy": "template_alternative",
                    "extraction_attempt_history_summary": {
                        "attempt_count_total": 3,
                        "same_config_repeat_count": 1,
                        "retry_diversity_count": 2,
                        "attempts": [
                            {"attempt_no": 1, "status": "success", "retry_strategy": "template_alternative", "candidate_count": 8},
                            {"attempt_no": 2, "status": "success", "retry_strategy": "template_alternative", "candidate_count": 7},
                            {"attempt_no": 3, "status": "error", "retry_strategy": "fallback_config", "candidate_count": 0},
                        ],
                    },
                },
                artifacts=[],
            )

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_EMPTY,
                )
            return ValidatorResult(status="pass", gate_passed=True)

        def ai_stub(state, step, attempt_no, exec_result, validator_result):
            del state, step, attempt_no, exec_result, validator_result
            extractor_status = {
                "phase": "phase3",
                "extractor_efficiency_health_score": 0.82,
                "scores": {"order_completion_quality_score": 0.34},
                "warnings": ["extraction_success_but_step20_poor"],
                "completion_metrics": {"step20_available": True, "step20_gate_passed": False},
                "efficiency_metrics": {
                    "attempt_count_total": 3,
                    "same_config_repeat_count": 1,
                    "retry_diversity_count": 2,
                },
            }
            return AIBotSnapshot(
                scores={"quality": 0.41},
                metrics={"extractor_status": extractor_status},
                warnings=["extraction_success_but_step20_poor"],
                anomaly_flags=[],
                proposals={},
                extractor_status=extractor_status,
                extractor_help_needed={"needed": True, "severity": "high", "reason_class": "low_order_completion_quality"},
            )

        engine.register_shadow_learned_retry_ranker(ranker)
        engine.register_executor(self.registry[STEP_P3_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P3_1_EXTRACT].validator, validator_stub)
        engine.register_ai_hook(self.registry[STEP_P3_1_EXTRACT].ai_bot_hooks[0], ai_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P3_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertTrue(captured_features)
        metrics = dict(captured_features[-1].get("extractor_metrics") or {})
        self.assertTrue(bool(metrics.get("step20_available")))
        self.assertEqual(str(captured_features[-1].get("context", {}).get("phase") or ""), "phase3")
        retry_events = [e for e in run.events if e.event_type == "step_retry_scheduled"]
        self.assertTrue(retry_events)
        self.assertEqual(str(retry_events[0].payload.get("retry_strategy") or ""), "fallback_config")
        self.assertTrue(any(e.event_type == "shadow_retry_plan_comparison_recorded" for e in run.events))

    def test_stage8_non_extractor_steps_do_not_run_shadow_ranker(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        self._set_feature_flags(engine, shadow_learned_retry_ranking_enabled=True)
        ranker_calls = {"n": 0}

        def ranker(_features):
            ranker_calls["n"] += 1
            return {"ranked_order": ["fallback_config"]}

        def exec_stub(state, step, attempt_no, params):
            del state, step, attempt_no, params
            return ExecutorResult(ok=True, summary={"semantic_ok": False}, artifacts=[])

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.SEMANTIC_PIPELINE_FAILED,
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_shadow_learned_retry_ranker(ranker)
        engine.register_executor(self.registry[STEP_P2_1_SEMANTIC].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P2_1_SEMANTIC].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase2"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P2_1_SEMANTIC,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(ranker_calls["n"], 0)
        self.assertEqual([e for e in run.events if str(e.event_type).startswith("shadow_")], [])

    def test_stage8_shadow_payloads_are_bounded(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        self._set_feature_flags(engine, shadow_learned_retry_ranking_enabled=True)

        def ranker(_features):
            return {
                "ranked_order": [
                    "template_alternative",
                    "bbox_expand",
                    "fallback_config",
                    "template_alternative",
                    "unknown_strategy",
                ],
                "score_by_strategy": {
                    "template_alternative": 0.9,
                    "bbox_expand": 0.8,
                    "fallback_config": 0.7,
                    "unknown_strategy": 0.99,
                },
                "confidence": 0.82,
                "reason_codes": [f"r{i}" for i in range(30)],
            }

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(ok=True, summary={"candidate_count": 0}, artifacts=[])

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(status="blocked", gate_passed=False, block_reason_code=BlockReasonCode.EXTRACTION_EMPTY)
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_shadow_learned_retry_ranker(ranker)
        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)

        rec = run.step_execution_records[0]
        plan = dict((rec.executor_result_summary or {}).get("shadow_learned_retry_plan") or {})
        cmp_payload = dict((rec.executor_result_summary or {}).get("shadow_retry_plan_comparison") or {})
        self.assertLessEqual(len(list(plan.get("ranked_order") or [])), 8)
        self.assertLessEqual(len(list(plan.get("reason_codes") or [])), 8)
        self.assertLessEqual(len(list(plan.get("score_by_strategy") or [])), 8)
        self.assertLessEqual(len(list(cmp_payload.get("deterministic_order") or [])), 8)
        self.assertLessEqual(len(list(cmp_payload.get("learned_order") or [])), 8)

    def test_stage8_shadow_outputs_do_not_change_gate_authority(self) -> None:
        engine = self._new_engine(profile=PolicyProfile.BALANCED.value)
        self._set_feature_flags(engine, shadow_learned_retry_ranking_enabled=True)

        def ranker(_features):
            return {"ranked_order": ["template_alternative", "bbox_expand"], "confidence": 0.77}

        def exec_stub(state, step, attempt_no, params):
            del state, step, params
            return ExecutorResult(ok=True, summary={"candidate_count": 0}, artifacts=[])

        def validator_stub(state, step, attempt_no, exec_result):
            del state, step, exec_result
            if attempt_no == 1:
                return ValidatorResult(
                    status="blocked",
                    gate_passed=False,
                    block_reason_code=BlockReasonCode.EXTRACTION_LOW_COVERAGE,
                )
            return ValidatorResult(status="pass", gate_passed=True)

        engine.register_shadow_learned_retry_ranker(ranker)
        engine.register_executor(self.registry[STEP_P1_1_EXTRACT].executor, exec_stub)
        engine.register_validator(self.registry[STEP_P1_1_EXTRACT].validator, validator_stub)
        run = engine.start_run(
            pipeline_scope={"phases": ["phase1"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P1_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=1)
        rec = run.step_execution_records[0]
        self.assertEqual(str((rec.validator_result or {}).get("status") or ""), "blocked")
        self.assertFalse(bool((rec.validator_result or {}).get("gate_passed")))
        self.assertTrue(any(e.event_type == "shadow_learned_retry_plan_evaluated" for e in run.events))

    def test_phase3_registry_routes_inverse_completion_before_step20(self) -> None:
        self.assertEqual(
            tuple(PHASE3_CANONICAL_ROUTE_STAGE_ORDER[:3]),
            (STEP_P3_1_EXTRACT, STEP_P3_15_INVERSE, STEP_P3_2_STEP20),
        )
        self.assertEqual(
            tuple(PHASE3_CANONICAL_ROUTE_STAGE_ORDER[3:7]),
            (STEP_P3_3_REORDER, STEP_P3_4_STEP30, STEP_P3_4_STEP32, STEP_P3_4_STEP35),
        )
        self.assertNotIn(STEP_P3_5_MERGE, tuple(PHASE3_CANONICAL_ROUTE_STAGE_ORDER))
        self.assertIn(STEP_P3_5_MERGE, tuple(PHASE3_LEGACY_NON_DEFAULT_STAGES))
        self.assertEqual(self.registry[STEP_P3_1_EXTRACT].next_step_on_success, STEP_P3_15_INVERSE)
        self.assertEqual(self.registry[STEP_P3_15_INVERSE].next_step_on_success, STEP_P3_2_STEP20)
        self.assertEqual(self.registry[STEP_P3_4_STEP30].next_step_on_success, STEP_P3_4_STEP32)
        self.assertEqual(self.registry[STEP_P3_4_STEP32].next_step_on_success, STEP_P3_4_STEP35)
        self.assertNotEqual(self.registry[STEP_P3_4_STEP40].next_step_on_success, STEP_P3_5_MERGE)
        self.assertIn("required before Step20", self.registry[STEP_P3_15_INVERSE].name)
        self.assertIn("direction-ready only", self.registry[STEP_P3_2_STEP20].name)
        self.assertIn("Legacy", self.registry[STEP_P3_5_MERGE].name)

    def test_phase3_next_step_hardening_ignores_stale_registry_transitions(self) -> None:
        engine = self._new_engine()
        state = RunSessionState(
            run_id="run-phase3-hardening",
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_1_EXTRACT,
        )
        stale_extract = replace(self.registry[STEP_P3_1_EXTRACT], next_step_on_success=STEP_P3_2_STEP20)
        stale_inverse = replace(self.registry[STEP_P3_15_INVERSE], next_step_on_success=STEP_P3_4_STEP30)
        stale_step30 = replace(self.registry[STEP_P3_4_STEP30], next_step_on_success=STEP_P3_4_STEP35)
        stale_approve = replace(self.registry[STEP_P3_4_STEP40], next_step_on_success=STEP_P3_5_MERGE)

        self.assertEqual(engine._next_step_after_success(state, stale_extract), STEP_P3_15_INVERSE)
        self.assertEqual(engine._next_step_after_success(state, stale_inverse), STEP_P3_2_STEP20)
        self.assertEqual(engine._next_step_after_success(state, stale_step30), STEP_P3_4_STEP32)
        self.assertEqual(engine._next_step_after_success(state, stale_approve), STEP_P3_6_CATALOG)

    def test_live_phase3_inverse_completion_executor_can_run_targeted_search_without_binding(self) -> None:
        class _FakePhase3Client:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []
                self.route_id = str(uuid4())
                self._gate_calls = 0

            def get_step20_direction_gate(self, **kwargs):
                self.calls.append(("gate", dict(kwargs or {})))
                self._gate_calls += 1
                if self._gate_calls >= 3:
                    return {
                        "gate_passed": False,
                        "direction_ready": False,
                        "gate_code": "direction_not_ready",
                        "service_route_id": "svc-1",
                        "direction_id": 1,
                        "route_id": self.route_id,
                        "inverse_status": "no_candidate_found",
                        "search_status": "materialized",
                        "blocker_codes": ["one_direction_missing"],
                        "blocker_messages": ["Opposite direction is still unresolved."],
                        "suggested_next_action": "handoff_manual_builder",
                    }
                return {
                    "gate_passed": False,
                    "direction_ready": False,
                    "gate_code": "direction_not_ready",
                    "service_route_id": "svc-1",
                    "direction_id": 1,
                    "route_id": self.route_id,
                    "inverse_status": "no_candidate_found",
                    "search_status": "not_started",
                    "blocker_codes": ["one_direction_missing"],
                    "blocker_messages": ["Opposite direction is still unresolved."],
                    "suggested_next_action": "handoff_manual_builder",
                }

            def refresh_inverse_proposals(self, **kwargs):
                self.calls.append(("refresh_inverse_proposals", dict(kwargs or {})))
                return {"persisted_row_count": 2}

            def dispatch_targeted_inverse_search(self, **kwargs):
                self.calls.append(("dispatch_targeted_inverse_search", dict(kwargs or {})))
                return {
                    "service_route_id": "svc-1",
                    "direction_id": 1,
                    "eligible": True,
                    "launched": True,
                    "direction_ready": False,
                    "inverse_status": "no_candidate_found",
                    "search_status": "materialized",
                    "materialized_route_ids": ["route-materialized-1"],
                    "dispatched_route_ids": ["route-discovered-1"],
                }

            def get_inverse_completion_summary(self, **kwargs):
                self.calls.append(("get_inverse_completion_summary", dict(kwargs or {})))
                return {"unresolved_rows": 1, "search_materialized_rows": 1}

            def bind_route_to_direction(self, **kwargs):
                self.calls.append(("bind_route_to_direction", dict(kwargs or {})))
                raise AssertionError("inverse completion executor must not bind routes")

        fake = _FakePhase3Client()
        bridge = build_phase_client_executor_bridge(phase3_client=fake)
        state = RunSessionState(
            run_id="run-live-p3-inverse",
            pipeline_scope={"phases": ["phase3"], "phase3": {"route_id": fake.route_id}},
            policy_profile=PolicyProfile.BALANCED.value,
            status="running",
            current_phase="phase3",
            current_step_id=STEP_P3_15_INVERSE,
        )

        out = bridge["phase3_inverse_completion"](state, self.registry[STEP_P3_15_INVERSE], 1, {})

        self.assertTrue(out.ok)
        self.assertFalse(bool(out.summary.get("inverse_completion_passed")))
        self.assertTrue(bool(out.summary.get("targeted_inverse_search_attempted")))
        self.assertTrue(bool(out.summary.get("targeted_inverse_search_ran")))
        self.assertEqual(str(out.summary.get("targeted_inverse_search_status") or ""), "materialized")
        self.assertFalse(any(name == "bind_route_to_direction" for name, _ in fake.calls))

    def test_autopilot_phase3_inverse_stage_blocks_before_step20_when_not_ready(self) -> None:
        engine = self._new_engine()
        bridge_validators = build_phase_client_validator_bridge()
        step20_calls = {"count": 0}

        def extract_executor(state, step, attempt_no, params):
            del state, step, attempt_no, params
            return ExecutorResult(ok=True, summary={"route_id": str(uuid4())}, artifacts=[])

        def extract_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="pass", gate_passed=True)

        def inverse_executor(state, step, attempt_no, params):
            route_id = str(state.resume_context.get("route_id") or "")
            del step, attempt_no, params
            return ExecutorResult(
                ok=True,
                summary={
                    "route_id": route_id,
                    "service_route_id": "svc-blocked",
                    "direction_id": 1,
                    "validator_payload": {
                        "route_id": route_id,
                        "service_route_id": "svc-blocked",
                        "direction_id": 1,
                        "gate_passed": False,
                        "direction_ready": False,
                        "gate_code": "direction_not_ready",
                        "inverse_status": "plausible_opposite_candidate",
                        "search_status": "not_started",
                        "blocker_codes": ["one_direction_missing"],
                        "blocker_messages": ["The opposite direction is still missing."],
                        "suggested_next_action": "run_targeted_inverse_search",
                        "targeted_inverse_search_attempted": False,
                        "targeted_inverse_search_ran": False,
                    },
                },
                artifacts=[],
            )

        def step20_executor(state, step, attempt_no, params):
            del state, step, attempt_no, params
            step20_calls["count"] += 1
            return ExecutorResult(ok=True, summary={"validator_payload": {"sequence_gate_pass": True}}, artifacts=[])

        engine.register_executor(self.registry[STEP_P3_1_EXTRACT].executor, extract_executor)
        engine.register_validator(self.registry[STEP_P3_1_EXTRACT].validator, extract_validator)
        engine.register_executor(self.registry[STEP_P3_15_INVERSE].executor, inverse_executor)
        engine.register_validator(self.registry[STEP_P3_15_INVERSE].validator, bridge_validators["phase3_inverse_completion"])
        engine.register_executor(self.registry[STEP_P3_2_STEP20].executor, step20_executor)
        engine.register_validator(self.registry[STEP_P3_2_STEP20].validator, bridge_validators["phase3_step20_sequence"])

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P3_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=3)

        self.assertEqual(run.status, "paused")
        self.assertEqual(run.current_step_id, STEP_P3_15_INVERSE)
        self.assertEqual(step20_calls["count"], 0)
        self.assertTrue(any(e.event_type == "step_started" and e.step_id == STEP_P3_15_INVERSE for e in run.events))
        self.assertFalse(any(e.event_type == "step_started" and e.step_id == STEP_P3_2_STEP20 for e in run.events))
        rec = run.step_execution_records[-1]
        self.assertEqual(rec.step_id, STEP_P3_15_INVERSE)
        self.assertEqual(rec.validator_result.get("status"), "blocked")
        self.assertEqual(rec.validator_result.get("block_reason_code"), BlockReasonCode.PHASE3_INVERSE_COMPLETION_BLOCKING.value)

    def test_autopilot_phase3_inverse_stage_runs_before_step20_when_ready(self) -> None:
        engine = self._new_engine()
        bridge_validators = build_phase_client_validator_bridge()

        def extract_executor(state, step, attempt_no, params):
            del state, step, attempt_no, params
            return ExecutorResult(ok=True, summary={"route_id": str(uuid4())}, artifacts=[])

        def extract_validator(state, step, attempt_no, exec_result):
            del state, step, attempt_no, exec_result
            return ValidatorResult(status="pass", gate_passed=True)

        def inverse_executor(state, step, attempt_no, params):
            route_id = str(state.resume_context.get("route_id") or "")
            del step, attempt_no, params
            return ExecutorResult(
                ok=True,
                summary={
                    "route_id": route_id,
                    "service_route_id": "svc-ready",
                    "direction_id": 0,
                    "validator_payload": {
                        "route_id": route_id,
                        "service_route_id": "svc-ready",
                        "direction_id": 0,
                        "gate_passed": True,
                        "direction_ready": True,
                        "gate_code": "direction_ready",
                        "inverse_status": "structurally_ready",
                        "search_status": "not_applicable",
                        "blocker_codes": [],
                        "blocker_messages": [],
                        "suggested_next_action": "proceed_step20",
                        "targeted_inverse_search_attempted": False,
                        "targeted_inverse_search_ran": False,
                    },
                },
                artifacts=[],
            )

        def step20_executor(state, step, attempt_no, params):
            del state, step, attempt_no, params
            return ExecutorResult(
                ok=True,
                summary={
                    "validator_payload": {
                        "unmatched_count": 0,
                        "ambiguous_count": 0,
                        "sequence_gate_pass": True,
                    },
                },
                artifacts=[],
            )

        engine.register_executor(self.registry[STEP_P3_1_EXTRACT].executor, extract_executor)
        engine.register_validator(self.registry[STEP_P3_1_EXTRACT].validator, extract_validator)
        engine.register_executor(self.registry[STEP_P3_15_INVERSE].executor, inverse_executor)
        engine.register_validator(self.registry[STEP_P3_15_INVERSE].validator, bridge_validators["phase3_inverse_completion"])
        engine.register_executor(self.registry[STEP_P3_2_STEP20].executor, step20_executor)
        engine.register_validator(self.registry[STEP_P3_2_STEP20].validator, bridge_validators["phase3_step20_sequence"])

        run = engine.start_run(
            pipeline_scope={"phases": ["phase3"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P3_1_EXTRACT,
        )
        run = engine.advance_run(run.run_id, max_steps=3)

        step_started_ids = [e.step_id for e in run.events if e.event_type == "step_started"]
        self.assertIn(STEP_P3_15_INVERSE, step_started_ids)
        self.assertIn(STEP_P3_2_STEP20, step_started_ids)
        self.assertLess(step_started_ids.index(STEP_P3_15_INVERSE), step_started_ids.index(STEP_P3_2_STEP20))
        self.assertTrue(any(e.event_type == "step_completed" and e.step_id == STEP_P3_2_STEP20 for e in run.events))


class P21DiagnosticsFixTests(unittest.TestCase):
    """Tests for Part 1 — P2.1 Diagnostics Patch fixes."""

    def _make_engine(self):
        registry = build_default_pipeline_step_registry()
        return SupervisedPipelineAutopilot(
            step_registry=registry,
            enabled=True,
        )

    # --- Fix 1.1: blocked vs failed state mapping ---

    def test_trigger_failed_when_validator_status_failed(self):
        engine = self._make_engine()
        vr = ValidatorResult(status="failed", block_reason_code=None)
        ai = AIBotSnapshot()
        trigger = engine._interpretation_trigger(vr, ai)
        self.assertEqual(trigger, "failed")

    def test_trigger_blocked_only_with_block_reason_code(self):
        engine = self._make_engine()
        vr = ValidatorResult(status="blocked", block_reason_code=BlockReasonCode.SEMANTIC_PIPELINE_FAILED)
        ai = AIBotSnapshot()
        trigger = engine._interpretation_trigger(vr, ai)
        self.assertEqual(trigger, "blocked")

    def test_trigger_failed_when_blocked_without_reason_code(self):
        engine = self._make_engine()
        vr = ValidatorResult(status="blocked", block_reason_code=None)
        ai = AIBotSnapshot()
        trigger = engine._interpretation_trigger(vr, ai)
        self.assertEqual(trigger, "failed")

    def test_trigger_warning_status(self):
        engine = self._make_engine()
        vr = ValidatorResult(status="warning")
        ai = AIBotSnapshot()
        trigger = engine._interpretation_trigger(vr, ai)
        self.assertEqual(trigger, "warning")

    def test_trigger_anomaly_from_validator(self):
        engine = self._make_engine()
        vr = ValidatorResult(status="pass", anomalies=["some_anomaly"])
        ai = AIBotSnapshot()
        trigger = engine._interpretation_trigger(vr, ai)
        self.assertEqual(trigger, "anomaly")

    def test_trigger_none_for_pass(self):
        engine = self._make_engine()
        vr = ValidatorResult(status="pass")
        ai = AIBotSnapshot()
        trigger = engine._interpretation_trigger(vr, ai)
        self.assertIsNone(trigger)

    # --- Fix 1.2: attempt history gap detection ---

    def test_p21_is_extraction_class_step(self):
        engine = self._make_engine()
        registry = build_default_pipeline_step_registry()
        p21_step = registry[STEP_P2_1_SEMANTIC]
        self.assertTrue(engine._is_extraction_class_step(p21_step))

    def test_p21_not_extractor_retry_step(self):
        engine = self._make_engine()
        registry = build_default_pipeline_step_registry()
        p21_step = registry[STEP_P2_1_SEMANTIC]
        self.assertFalse(engine._is_extractor_retry_step(p21_step))

    def test_attempt_history_gap_flagged_when_missing(self):
        engine = self._make_engine()
        registry = build_default_pipeline_step_registry()
        step = registry[STEP_P2_1_SEMANTIC]
        exec_result = ExecutorResult(ok=True, summary={"candidate_count": 5})
        vr = ValidatorResult(status="warning", warnings=["low_coverage"])
        result = engine._ensure_extractor_attempt_history_summary(
            step=step, attempt_no=3, exec_result=exec_result, validator_result=vr,
        )
        flags = list(result.summary.get("insufficient_data_flags") or [])
        self.assertIn("attempt_history_gap", flags)
        gap = result.summary.get("attempt_history_gap")
        self.assertIsNotNone(gap)
        self.assertEqual(gap["expected_count"], 2)

    # --- Fix 1.3: executor_result_summary in snapshot ---

    def test_snapshot_contains_executor_result_summary(self):
        """Verify snapshot has both executor_summary and executor_result_summary."""
        from datamind_console.orchestrator.pipeline_autopilot import (
            AdvisoryChatGPTInterpreter,
            RunSessionState,
        )
        registry = build_default_pipeline_step_registry()
        step = registry[STEP_P2_1_SEMANTIC]
        state = RunSessionState(
            run_id="test-run",
            pipeline_scope={"phases": ["phase2"]},
            policy_profile="balanced",
            status="running",
            current_phase="phase2",
            current_step_id=STEP_P2_1_SEMANTIC,
        )
        exec_result = ExecutorResult(ok=True, summary={"candidate_count": 10})
        vr = ValidatorResult(status="warning")
        ai = AIBotSnapshot()
        interp = AdvisoryChatGPTInterpreter(advisory_mode="mock_advisory")
        snapshot = interp._build_normalized_snapshot(
            state=state, step=step, attempt_no=1,
            exec_result=exec_result, validator_result=vr,
            ai_snapshot=ai, trigger="warning",
        )
        self.assertIn("executor_summary", snapshot)
        self.assertIn("executor_result_summary", snapshot)
        self.assertEqual(snapshot["executor_summary"], snapshot["executor_result_summary"])


class NormPhaseTests(unittest.TestCase):
    """Test that _norm_phase accepts phase2."""

    def test_phase2_accepted(self):
        from datamind_console.ai_insights.service import _norm_phase
        self.assertEqual(_norm_phase("phase2"), "phase2")

    def test_phase1_still_accepted(self):
        from datamind_console.ai_insights.service import _norm_phase
        self.assertEqual(_norm_phase("phase1"), "phase1")

    def test_phase4_accepted(self):
        from datamind_console.ai_insights.service import _norm_phase
        self.assertEqual(_norm_phase("phase4"), "phase4")

    def test_invalid_phase_rejected(self):
        from datamind_console.ai_insights.service import _norm_phase
        self.assertEqual(_norm_phase("phaseX"), "")


if __name__ == "__main__":
    unittest.main()
