from __future__ import annotations

import unittest

from datamind_console.api_chatgpt.services.policy_guard import PolicyGuard


class PolicyGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = PolicyGuard()

    def test_rejects_unsafe_execution_framing_and_unknown_evidence(self) -> None:
        snapshot = {
            "evidence_refs": [
                {"evidence_id": "ev_a"},
                {"evidence_id": "ev_b"},
            ]
        }
        response = {
            "mode": "advisory_only",
            "task": "analyze_latest_run",
            "summary": "We executed cleanup and committed reorder.",
            "operator_confirmation_required": False,
            "evidence_used": ["ev_a", "ev_unknown"],
            "top_issues": [],
            "recommended_actions": [
                {
                    "action_id": "a1",
                    "priority": "p0",
                    "description": "Merge directions now",
                    "high_impact": True,
                    "requires_operator_confirmation": False,
                    "evidence_refs": ["ev_unknown"],
                }
            ],
        }

        errs = self.guard.enforce_post_response(
            task="analyze_latest_run",
            response=response,
            snapshot=snapshot,
        )
        self.assertTrue(any("execution happened" in e for e in errs))
        self.assertTrue(any("unknown evidence IDs" in e for e in errs))
        self.assertTrue(any("operator_confirmation_required=true" in e for e in errs))

    def test_hades_pipeline_interpreter_requires_operator_action_for_high_impact(self) -> None:
        snapshot = {"evidence_refs": []}
        response = {
            "summary": "Recommend patch branch for Step20 gate behavior.",
            "dominant_cause_class": "detector_thresholds",
            "confidence": 0.8,
            "secondary_causes": [],
            "evidence_consistency_checks": {"contradictions_found": True, "contradictions": []},
            "recommended_branch": "patch_detector_scoring",
            "recommended_next_actions": ["Create detector/scoring patch task and rerun Step20."],
            "patch_task_recommendation": {
                "should_create_patch_task": True,
                "patch_type": "detector_scoring",
                "justification": "Detector thresholds appear noisy.",
                "suggested_target": "either",
            },
            "operator_action_required": False,
            "approval_type_if_needed": None,
            "risk_notes": [],
        }
        errs = self.guard.enforce_post_response(
            task="hades_pipeline_interpreter",
            response=response,
            snapshot=snapshot,
        )
        self.assertTrue(any("operator_action_required=true" in e for e in errs))

    def test_hades_pipeline_interpreter_patch_diagnostics_requires_patch_recommendation(self) -> None:
        snapshot = {"evidence_refs": []}
        response = {
            "summary": "Diagnostics patch recommended for partial evidence case.",
            "dominant_cause_class": "diagnostics_visibility",
            "confidence": 0.7,
            "secondary_causes": [],
            "evidence_consistency_checks": {"contradictions_found": False, "contradictions": []},
            "recommended_branch": "patch_diagnostics",
            "recommended_next_actions": ["Patch diagnostics and rerun."],
            "patch_task_recommendation": {
                "should_create_patch_task": False,
                "patch_type": None,
                "justification": None,
                "suggested_target": None,
            },
            "operator_action_required": True,
            "approval_type_if_needed": None,
            "risk_notes": [],
        }
        errs = self.guard.enforce_post_response(
            task="hades_pipeline_interpreter",
            response=response,
            snapshot=snapshot,
        )
        self.assertTrue(any("patch_* requires patch_task_recommendation.should_create_patch_task=true" in e for e in errs))

    def test_hades_consistency_checker_allows_minimal_schema(self) -> None:
        snapshot = {"evidence_refs": []}
        response = {
            "contradictions_found": False,
            "contradictions": [],
            "consistency_summary": "No contradictions detected.",
        }
        errs = self.guard.enforce_post_response(
            task="hades_evidence_consistency_checker",
            response=response,
            snapshot=snapshot,
        )
        self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main()
