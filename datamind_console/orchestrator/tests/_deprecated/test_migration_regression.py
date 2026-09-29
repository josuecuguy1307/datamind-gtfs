# QUARANTINED 2026-04-20.
#
# This test asserts the presence of datamind_console/views/operator_orchestrator_view.py
# and an `elif page == "Operator Orchestrator":` routing block in app.py. Neither
# exists: the view was never committed to git and app.py has no such route. The
# ORCHESTRATOR_V2 migration referenced here did not land in the expected shape.
#
# The real active runtime for orchestration is pipeline_autopilot.py. Tests for
# that module live in tests/test_pipeline_autopilot.py. If/when an
# operator_orchestrator_view.py is actually wired, resurrect (or rewrite) these
# tests from this file.
#
# File is under tests/_deprecated/ so pytest does not collect it by default.
from __future__ import annotations

import unittest
from pathlib import Path


class MigrationRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[3]
        self.view_path = self.repo_root / "datamind_console" / "views" / "operator_orchestrator_view.py"
        self.app_path = self.repo_root / "datamind_console" / "app.py"
        self.migration_doc = self.repo_root / "docs" / "orchestrator_replan_migration.md"

    def test_operator_orchestrator_view_is_v2_gated(self) -> None:
        text = self.view_path.read_text(encoding="utf-8")
        self.assertIn("ORCHESTRATOR_V2_ROLE_MODEL", text)
        self.assertIn('os.getenv("ORCHESTRATOR_V2_ROLE_MODEL", "true")', text)
        self.assertIn("Legacy orchestration model has been retired", text)

    def test_legacy_use_db_backend_toggle_not_present_in_active_view(self) -> None:
        text = self.view_path.read_text(encoding="utf-8")
        self.assertNotIn("Use DB Backend", text)

    def test_app_routes_operator_orchestrator_to_v2_view(self) -> None:
        text = self.app_path.read_text(encoding="utf-8")
        self.assertIn("from datamind_console.views.operator_orchestrator_view import render_operator_orchestrator_view", text)
        self.assertIn("elif page == \"Operator Orchestrator\":", text)
        self.assertIn("render_operator_orchestrator_view(analytics=analytics, audit=audit)", text)

    def test_migration_doc_present_and_mentions_flag(self) -> None:
        text = self.migration_doc.read_text(encoding="utf-8")
        self.assertIn("ORCHESTRATOR_V2_ROLE_MODEL=true", text)
        self.assertIn("What Was Removed/Disabled", text)
        self.assertIn("What Was Replaced", text)


if __name__ == "__main__":
    unittest.main()
