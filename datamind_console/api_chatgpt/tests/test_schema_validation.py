from __future__ import annotations

import json
import unittest
from pathlib import Path

from datamind_console.api_chatgpt.services.response_validator import ResponseValidator


class SchemaValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = ResponseValidator()
        self.fixtures_dir = Path(__file__).resolve().parent / "fixtures"

    def _load_json(self, rel: str) -> dict:
        with (self.fixtures_dir / rel).open("r", encoding="utf-8") as f:
            return json.load(f)

    def test_request_envelope_schema_valid(self) -> None:
        snapshot = self._load_json("snapshots/phase3_low_evidence_run_snapshot.json")
        envelope = {
            "task": "analyze_latest_run",
            "snapshot": snapshot,
            "operator_context": {"note": "schema test"},
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
        }
        errs = self.validator.validate_request_envelope(envelope)
        self.assertEqual(errs, [])

    def test_analyze_response_schema_valid(self) -> None:
        resp = self._load_json("model_responses/analyze_latest_run_response.json")
        errs = self.validator.validate_task_response(task="analyze_latest_run", payload=resp)
        self.assertEqual(errs, [])

    def test_request_envelope_schema_valid_for_pipeline_blocker_task(self) -> None:
        snapshot = self._load_json("snapshots/phase3_low_evidence_run_snapshot.json")
        envelope = {
            "task": "interpret_pipeline_blocker",
            "snapshot": snapshot,
            "operator_context": {"note": "schema test blocker task"},
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
        }
        errs = self.validator.validate_request_envelope(envelope)
        self.assertEqual(errs, [])

    def test_hades_pipeline_interpreter_response_schema_valid(self) -> None:
        payload = {
            "summary": "Detected Step20 unmatched blocker with no contract contradictions.",
            "dominant_cause_class": "node_db_gap",
            "confidence": 0.88,
            "secondary_causes": [
                {
                    "class": "matching_ambiguity",
                    "confidence": 0.33,
                    "note": "Minor ambiguous pressure exists but unmatched dominates.",
                }
            ],
            "evidence_consistency_checks": {
                "contradictions_found": False,
                "contradictions": [],
            },
            "recommended_branch": "phase1_new_nodes",
            "recommended_next_actions": [
                "Divert unresolved stops to P1 New Nodes.",
                "Require operator confirmation before promote.",
                "Resume Step20 automatically after resolution is persisted.",
            ],
            "patch_task_recommendation": {
                "should_create_patch_task": False,
                "patch_type": None,
                "justification": None,
                "suggested_target": None,
            },
            "operator_action_required": True,
            "approval_type_if_needed": "RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS",
            "risk_notes": [
                {
                    "code": "NODE_GAP_BLOCKING",
                    "severity": "high",
                    "message": "Unmatched stops block Step20 gate until node gap is resolved.",
                }
            ],
        }
        errs = self.validator.validate_task_response(task="hades_pipeline_interpreter", payload=payload)
        self.assertEqual(errs, [])

    def test_hades_pipeline_interpreter_response_schema_accepts_patch_diagnostics_branch(self) -> None:
        payload = {
            "summary": "Partial evidence suggests diagnostics patch before extractor changes.",
            "dominant_cause_class": "diagnostics_visibility",
            "confidence": 0.67,
            "secondary_causes": [],
            "evidence_consistency_checks": {
                "contradictions_found": False,
                "contradictions": [],
            },
            "recommended_branch": "patch_diagnostics",
            "recommended_next_actions": [
                "Improve extractor/validator diagnostic payload quality.",
                "Re-run the same case with richer evidence before extractor patching.",
            ],
            "patch_task_recommendation": {
                "should_create_patch_task": True,
                "patch_type": "diagnostics",
                "justification": "Evidence quality is partial and attribution remains weak.",
                "suggested_target": "either",
            },
            "operator_action_required": True,
            "approval_type_if_needed": None,
            "risk_notes": [
                {
                    "code": "PARTIAL_EVIDENCE_CAUTION",
                    "severity": "medium",
                    "message": "Diagnostics-first routing reduces false root-cause attribution risk.",
                }
            ],
        }
        errs = self.validator.validate_task_response(task="hades_pipeline_interpreter", payload=payload)
        self.assertEqual(errs, [])

    def test_request_envelope_schema_valid_for_hades_pipeline_interpreter_task(self) -> None:
        snapshot = self._load_json("snapshots/phase3_low_evidence_run_snapshot.json")
        envelope = {
            "task": "hades_pipeline_interpreter",
            "snapshot": snapshot,
            "operator_context": {"note": "schema test hades interpreter"},
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
        }
        errs = self.validator.validate_request_envelope(envelope)
        self.assertEqual(errs, [])

    def test_hades_evidence_consistency_checker_response_schema_valid(self) -> None:
        payload = {
            "contradictions_found": True,
            "contradictions": [
                {
                    "code": "RESOLVE_COUNT_MISMATCH",
                    "severity": "high",
                    "details": "executor resolved_total=1652 while validator resolved_count=0",
                    "likely_implication": "validator_mapping_bug",
                }
            ],
            "consistency_summary": "Detected contradiction between executor summary and validator payload.",
        }
        errs = self.validator.validate_task_response(task="hades_evidence_consistency_checker", payload=payload)
        self.assertEqual(errs, [])

    def test_request_envelope_schema_valid_for_hades_evidence_consistency_checker_task(self) -> None:
        snapshot = self._load_json("snapshots/phase3_low_evidence_run_snapshot.json")
        envelope = {
            "task": "hades_evidence_consistency_checker",
            "snapshot": snapshot,
            "operator_context": {"note": "schema test consistency checker"},
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
        }
        errs = self.validator.validate_request_envelope(envelope)
        self.assertEqual(errs, [])

    def test_hades_patch_task_generator_response_schema_valid(self) -> None:
        payload = {
            "mode": "advisory_only",
            "task": "hades_patch_task_generator",
            "summary": "Prepared a scoped patch task for detector/scoring contract alignment.",
            "confidence": {
                "band": "medium",
                "score": 0.67,
                "reasons": ["Evidence is actionable but still partly sparse."],
            },
            "risk_flags": [
                {
                    "code": "EVIDENCE_PARTIAL",
                    "severity": "medium",
                    "message": "Some corridors still have limited historical baseline.",
                }
            ],
            "insufficient_data_flags": [],
            "operator_confirmation_required": True,
            "evidence_used": ["ev_patch_context"],
            "patch_task": {
                "title": "HADES detector/scoring contract patch",
                "goal": "Improve interpreter handoff consistency for Step20 diagnostics.",
                "scope": ["contract normalization", "warning subtype consistency"],
                "constraints": ["advisory_only", "no_gate_bypass"],
                "prompt_text": (
                    "Patch Step20 detector/scoring payload normalization so contradictions are surfaced "
                    "as structured evidence without weakening validator gates."
                ),
                "expected_files": ["datamind_console/orchestrator/pipeline_autopilot.py"],
                "acceptance_criteria": ["Consistency candidates are emitted when mismatches are detected."],
                "high_impact_notes": ["Operator review required before applying generated patch output."],
            },
        }
        errs = self.validator.validate_task_response(task="hades_patch_task_generator", payload=payload)
        self.assertEqual(errs, [])

    def test_request_envelope_schema_valid_for_hades_patch_task_generator_task(self) -> None:
        snapshot = self._load_json("snapshots/codex_patch_issue_snapshot.json")
        envelope = {
            "task": "hades_patch_task_generator",
            "snapshot": snapshot,
            "operator_context": {"note": "schema test hades patch task"},
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
        }
        errs = self.validator.validate_request_envelope(envelope)
        self.assertEqual(errs, [])

    def test_hades_retest_comparator_response_schema_valid(self) -> None:
        payload = {
            "mode": "advisory_only",
            "task": "hades_retest_comparator",
            "summary": "Retest improved sequence quality with matched context and no threshold drift.",
            "result": "improved",
            "confidence": 0.81,
            "comparability_assessment": {
                "is_comparable": True,
                "notes": ["Same route, phase, and step focus between baseline and retest."],
            },
            "attribution_assessment": {
                "likely_attributable": True,
                "confounders": [],
            },
            "metric_deltas": [
                {
                    "metric": "sequence_quality_score",
                    "before": 63.0,
                    "after": 77.0,
                    "delta": 14.0,
                    "interpretation": "improved",
                },
                {
                    "metric": "unmatched_count",
                    "before": 8,
                    "after": 3,
                    "delta": -5,
                    "interpretation": "improved",
                },
            ],
            "recommendation": "accept",
            "recommended_next_actions": [
                "Proceed with supervised rollout and keep gate thresholds unchanged.",
                "Collect two additional retests on the same corridor for stability confirmation.",
            ],
            "risk_notes": [
                {
                    "code": "SHORT_RETEST_WINDOW",
                    "severity": "low",
                    "message": "Improvement is promising but currently based on a short retest window.",
                }
            ],
        }
        errs = self.validator.validate_task_response(task="hades_retest_comparator", payload=payload)
        self.assertEqual(errs, [])

    def test_request_envelope_schema_valid_for_hades_retest_comparator_task(self) -> None:
        snapshot = self._load_json("snapshots/hades_retest_comparator_snapshot.json")
        envelope = {
            "task": "hades_retest_comparator",
            "snapshot": snapshot,
            "operator_context": {"note": "schema test hades retest comparator"},
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
        }
        errs = self.validator.validate_request_envelope(envelope)
        self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main()
