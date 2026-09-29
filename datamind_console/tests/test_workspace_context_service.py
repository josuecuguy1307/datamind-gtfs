from __future__ import annotations

import unittest

from datamind_console.services.workspace_context_service import (
    SAMPLE_REGION_RESOURCE,
    active_work,
    context_summary,
    create_work,
    start_new_session,
)


class WorkspaceContextServiceTests(unittest.TestCase):
    def test_session_starts_neutral(self) -> None:
        state: dict[str, object] = {}
        self.assertIsNone(active_work(state))
        self.assertEqual(context_summary(state)["work"], "No active work selected")

    def test_work_context_is_isolated_and_resource_is_explicit(self) -> None:
        state: dict[str, object] = {}
        first = create_work(state, name="feed A", input_description="a.zip")
        second = create_work(
            state,
            name="feed B",
            input_description="b.zip",
            resource_id=SAMPLE_REGION_RESOURCE["id"],
        )
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(active_work(state)["id"], second["id"])
        self.assertEqual(context_summary(state)["resource"], SAMPLE_REGION_RESOURCE["name"])

    def test_new_session_clears_only_ui_context_references(self) -> None:
        state: dict[str, object] = {
            "ops.gtfs.last_file_path.work-a": "/tmp/a.zip",
            "ops.gtfs.upload.work-a": object(),
            "other.running.task": "untouched",
        }
        create_work(state, name="feed A")
        start_new_session(state)
        self.assertIsNone(active_work(state))
        self.assertNotIn("ops.gtfs.last_file_path.work-a", state)
        self.assertNotIn("ops.gtfs.upload.work-a", state)
        self.assertEqual(state["other.running.task"], "untouched")
