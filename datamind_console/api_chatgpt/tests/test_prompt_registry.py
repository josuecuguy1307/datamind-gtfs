from __future__ import annotations

import unittest

from datamind_console.api_chatgpt.services.prompt_registry import PromptRegistry


class PromptRegistryTests(unittest.TestCase):
    def test_legacy_tasks_keep_default_system_prompt(self) -> None:
        reg = PromptRegistry()
        system_prompt, _ = reg.render(
            task="analyze_latest_run",
            snapshot={"snapshot_id": "s1"},
            operator_context={},
        )
        self.assertIn("DataMind AI Analyst Copilot", system_prompt)

    def test_hades_pipeline_template_replaces_all_placeholders(self) -> None:
        reg = PromptRegistry()
        snapshot = {
            "phase": "phase3",
            "step_id": "P3.2_SEQUENCE_STEP20",
            "validator": {"status": "blocked"},
            "recent_history_summary": [{"step_id": "P3.1_ROUTE_EXTRACT", "status": "completed"}],
            "policy_profile": {"name": "balanced"},
        }
        system_prompt, user_prompt = reg.render(
            task="hades_pipeline_interpreter",
            snapshot=snapshot,
            operator_context={"operator_id": "op-1"},
        )
        self.assertTrue(system_prompt.strip())
        self.assertIn("HADES Pipeline Interpreter", system_prompt)
        self.assertNotIn("{{EVENT_SNAPSHOT_JSON}}", user_prompt)
        self.assertNotIn("{{RECENT_HISTORY_SUMMARY_JSON_OR_NULL}}", user_prompt)
        self.assertNotIn("{{POLICY_PROFILE_JSON_OR_NULL}}", user_prompt)
        self.assertIn("P3.2_SEQUENCE_STEP20", user_prompt)
        self.assertIn('"name": "balanced"', user_prompt)

    def test_hades_pipeline_template_renders_null_optionals(self) -> None:
        reg = PromptRegistry()
        snapshot = {
            "phase": "phase3",
            "step_id": "P3.2_SEQUENCE_STEP20",
        }
        _, user_prompt = reg.render(
            task="hades_pipeline_interpreter",
            snapshot=snapshot,
            operator_context={},
        )
        self.assertNotIn("{{EVENT_SNAPSHOT_JSON}}", user_prompt)
        self.assertIn("null", user_prompt)

    def test_hades_consistency_checker_replaces_event_placeholder(self) -> None:
        reg = PromptRegistry()
        snapshot = {
            "phase": "phase1",
            "step_id": "P1.2_NORMALIZE_FEATURES_CLUSTER_RESOLVE",
            "validator": {"status": "blocked"},
        }
        system_prompt, user_prompt = reg.render(
            task="hades_evidence_consistency_checker",
            snapshot=snapshot,
            operator_context={},
        )
        self.assertIn("pipeline evidence consistency checker", system_prompt.lower())
        self.assertNotIn("{{EVENT_SNAPSHOT_JSON}}", user_prompt)
        self.assertIn("P1.2_NORMALIZE_FEATURES_CLUSTER_RESOLVE", user_prompt)

    def test_hades_patch_task_generator_template_renders_snapshot(self) -> None:
        reg = PromptRegistry()
        snapshot = {
            "phase": "phase3",
            "step_id": "P3.2_SEQUENCE_STEP20",
            "run_id": "run-77",
            "evidence_refs": [{"evidence_id": "ev_1"}],
        }
        system_prompt, user_prompt = reg.render(
            task="hades_patch_task_generator",
            snapshot=snapshot,
            operator_context={"operator_id": "op-7"},
        )
        self.assertIn("HADES Patch Task Generator", system_prompt)
        self.assertNotIn("{{snapshot_json}}", user_prompt)
        self.assertIn("run-77", user_prompt)

    def test_hades_retest_comparator_template_renders_snapshot(self) -> None:
        reg = PromptRegistry()
        snapshot = {
            "change_type": "patch_detector_scoring",
            "phase": "phase3",
            "step_focus": "P3.2_SEQUENCE_STEP20",
            "baseline_run_snapshot": {"run_id": "run-base-1"},
            "retest_run_snapshot": {"run_id": "run-retest-2"},
            "extra_context": {"threshold_config_changed": False},
        }
        system_prompt, user_prompt = reg.render(
            task="hades_retest_comparator",
            snapshot=snapshot,
            operator_context={"operator_id": "op-9"},
        )
        self.assertIn("HADES Retest Comparator", system_prompt)
        self.assertNotIn("{{snapshot_json}}", user_prompt)
        self.assertIn("run-base-1", user_prompt)
        self.assertIn("run-retest-2", user_prompt)


if __name__ == "__main__":
    unittest.main()
