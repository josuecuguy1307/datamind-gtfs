from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from datamind_console.ai_insights.service import AIInsightsService
from datamind_console.labels import operator_labels
from datamind_console.scripts.ai_bot_golive_check import _build_operator_label_soft_gate


class OperatorLabelsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        self._orig_dir = operator_labels.OPERATOR_LABELS_DIR
        self._orig_session = operator_labels.SESSION_LABELS_PATH
        self._orig_run = operator_labels.RUN_LABELS_PATH

        operator_labels.OPERATOR_LABELS_DIR = self.root / "operator"
        operator_labels.SESSION_LABELS_PATH = operator_labels.OPERATOR_LABELS_DIR / "operator_session_labels.jsonl"
        operator_labels.RUN_LABELS_PATH = operator_labels.OPERATOR_LABELS_DIR / "operator_run_labels.jsonl"

    def tearDown(self) -> None:
        operator_labels.OPERATOR_LABELS_DIR = self._orig_dir
        operator_labels.SESSION_LABELS_PATH = self._orig_session
        operator_labels.RUN_LABELS_PATH = self._orig_run
        self.tmp.cleanup()

    def test_label_write_round_trip(self) -> None:
        s_row = operator_labels.append_session_label(
            {
                "session_id": "orch_001",
                "operator_grade": "good",
                "sequence_warning_correct": True,
                "reorder_action_taken": False,
                "reorder_helpful": None,
                "final_run_disposition": "passed_clean",
                "notes": "ok",
                "label_version": "1",
            }
        )
        r_row = operator_labels.append_run_label(
            {
                "route_id": "R1",
                "run_ref": "run_123",
                "operator_sequence_label": "good",
                "sequence_warning_correct": True,
                "final_run_disposition": "passed_clean",
                "operator_grade": "acceptable",
                "notes": "looks good",
                "label_version": "1",
            }
        )
        self.assertTrue(s_row.get("label_id"))
        self.assertTrue(r_row.get("label_id"))
        self.assertEqual(len(operator_labels.load_session_labels(days=30)), 1)
        self.assertEqual(len(operator_labels.load_run_labels(days=30)), 1)

    def test_ai_bot_scoring_reads_label_metrics(self) -> None:
        operator_labels.append_session_label(
            {
                "session_id": "orch_1",
                "operator_grade": "good",
                "sequence_warning_correct": True,
                "reorder_action_taken": True,
                "reorder_helpful": True,
                "final_run_disposition": "passed_clean",
                "notes": "",
                "label_version": "1",
            }
        )
        operator_labels.append_session_label(
            {
                "session_id": "orch_2",
                "operator_grade": "poor",
                "sequence_warning_correct": False,
                "reorder_action_taken": False,
                "reorder_helpful": False,
                "final_run_disposition": "failed",
                "notes": "",
                "label_version": "1",
            }
        )
        operator_labels.append_run_label(
            {
                "route_id": "R2",
                "run_ref": "run_9",
                "operator_sequence_label": "acceptable",
                "sequence_warning_correct": True,
                "final_run_disposition": "passed_with_warnings",
                "operator_grade": "good",
                "notes": "",
                "label_version": "1",
            }
        )

        svc = AIInsightsService()
        metrics = svc.operator_label_metrics(days=30)
        self.assertEqual(int(metrics.get("operator_label_count_30d") or 0), 3)
        self.assertAlmostEqual(float(metrics.get("operator_good_rate_30d") or 0), 0.6667, places=3)
        self.assertAlmostEqual(float(metrics.get("sequence_warning_correct_rate") or 0), 0.6667, places=3)
        self.assertAlmostEqual(float(metrics.get("reorder_helpful_rate") or 0), 0.5, places=3)

    def test_empty_label_files_return_zero_counts(self) -> None:
        svc = AIInsightsService()
        metrics = svc.operator_label_metrics(days=30)
        self.assertEqual(int(metrics.get("operator_label_count_30d") or 0), 0)
        self.assertEqual(float(metrics.get("operator_good_rate_30d") or 0), 0.0)
        self.assertEqual(float(metrics.get("sequence_warning_correct_rate") or 0), 0.0)
        self.assertEqual(float(metrics.get("reorder_helpful_rate") or 0), 0.0)

    def test_v2_label_contract_and_metrics(self) -> None:
        s_row = operator_labels.append_session_label(
            {
                "session_id": "orch_v2_1",
                "phase": "phase3",
                "stage": "step40_approve",
                "run_id": "run_v2_1",
                "route_id": "route_v2_1",
                "operator_sequence_label": "needs_minor_fix",
                "operator_grade": 5,
                "sequence_warning_correct": "partial",
                "reorder_action_taken": "not_applicable",
                "reorder_helpful": "partial",
                "final_run_disposition": "blocked_merge_review",
                "notes": "v2 session label",
                "label_version": "2",
            }
        )
        r_row = operator_labels.append_run_label(
            {
                "route_id": "route_v2_2",
                "run_ref": "run_v2_2",
                "run_id": "run_v2_2",
                "phase": "phase3",
                "stage": "step35_rank",
                "operator_sequence_label": "needs_major_fix",
                "sequence_warning_correct": "yes",
                "reorder_action_taken": "yes",
                "reorder_helpful": "no",
                "final_run_disposition": "passed_after_manual_fix",
                "operator_grade": "4",
                "notes": "v2 run label",
                "label_version": "2",
            }
        )
        self.assertEqual(str(s_row.get("operator_grade")), "5")
        self.assertEqual(str(r_row.get("operator_grade")), "4")

        metrics = operator_labels.compute_label_metrics_30d()
        self.assertEqual(int(metrics.get("operator_label_count_30d") or 0), 2)
        self.assertEqual(int(metrics.get("operator_label_v2_count_30d") or 0), 2)
        self.assertAlmostEqual(float(metrics.get("operator_good_rate_30d") or 0.0), 1.0, places=3)
        self.assertAlmostEqual(float(metrics.get("sequence_warning_correct_rate") or 0.0), 0.75, places=3)
        self.assertAlmostEqual(float(metrics.get("reorder_helpful_rate") or 0.0), 0.25, places=3)

    def test_go_live_soft_gate_threshold(self) -> None:
        gate_fail = _build_operator_label_soft_gate(operator_label_count_30d=4, operator_label_min=5)
        gate_pass = _build_operator_label_soft_gate(operator_label_count_30d=5, operator_label_min=5)
        self.assertFalse(bool(gate_fail.get("pass")))
        self.assertTrue(bool(gate_pass.get("pass")))


if __name__ == "__main__":
    unittest.main()
