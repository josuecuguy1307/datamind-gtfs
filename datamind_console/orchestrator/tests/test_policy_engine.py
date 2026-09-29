from __future__ import annotations

import unittest

from datamind_console.orchestrator.policy_engine import (
    get_effective_policy,
    should_auto_advance,
    validate_file_scope,
)


class PolicyEngineTests(unittest.TestCase):
    def test_profile_aliases_supported(self) -> None:
        self.assertEqual(get_effective_policy("operator_dispatch_checkpoint", "conservative"), "gate")
        self.assertEqual(get_effective_policy("generate_codex_patch_task", "aggressive_supervised"), "auto")

    def test_should_auto_advance_blocks_high_risk_flags(self) -> None:
        self.assertFalse(
            should_auto_advance(
                "generate_codex_patch_task",
                profile="aggressive_supervised",
                risk_flags=["high_impact"],
            )
        )

    def test_should_auto_advance_blocks_forbidden_file_scope(self) -> None:
        self.assertFalse(
            should_auto_advance(
                "generate_codex_patch_task",
                profile="aggressive_supervised",
                files=["phase3_routes/services/route_constructor/src/pipeline/step20_build_sequences.py"],
            )
        )

    def test_validate_file_scope_enforces_forbidden_paths(self) -> None:
        self.assertFalse(validate_file_scope("phase1_nodes/some_file.py", operation="write"))
        self.assertFalse(validate_file_scope("phase2_semantics/sql/migrations/001.sql", operation="write"))
        self.assertTrue(validate_file_scope("datamind_console/orchestrator_logs/v2/events.jsonl", operation="write"))


if __name__ == "__main__":
    unittest.main()
