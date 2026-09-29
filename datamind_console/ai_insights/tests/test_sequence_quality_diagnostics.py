from __future__ import annotations

import unittest

from datamind_console.ai_insights.sequence_quality import evaluate_sequence_quality


class SequenceQualityDiagnosticsTests(unittest.TestCase):
    def test_ambiguous_pressure_classifies_matching_ambiguity(self) -> None:
        rows = [
            {"seq": 1, "lat": -0.1, "lon": -78.4, "match_state": "matched", "matched_stop_node_id": "a"},
            {"seq": 2, "lat": -0.101, "lon": -78.401, "match_state": "ambiguous", "matched_stop_node_id": None},
            {"seq": 3, "lat": -0.102, "lon": -78.402, "match_state": "matched", "matched_stop_node_id": "c"},
        ]
        out = evaluate_sequence_quality(prior_rows=rows, matched_count=2, unmatched_count=0, ambiguous_count=1)
        self.assertEqual(out.get("dominant_cause"), "matching_ambiguity")
        self.assertEqual(out.get("triage_route"), "phase1_new_nodes_resolution")

    def test_unmatched_pressure_classifies_node_db_gap(self) -> None:
        rows = [
            {"seq": 1, "lat": -0.1, "lon": -78.4, "match_state": "unmatched", "matched_stop_node_id": None},
            {"seq": 2, "lat": -0.1005, "lon": -78.401, "match_state": "unmatched", "matched_stop_node_id": None},
            {"seq": 3, "lat": -0.101, "lon": -78.402, "match_state": "matched", "matched_stop_node_id": "c"},
            {"seq": 4, "lat": -0.1015, "lon": -78.403, "match_state": "unmatched", "matched_stop_node_id": None},
        ]
        out = evaluate_sequence_quality(prior_rows=rows, matched_count=1, unmatched_count=3, ambiguous_count=0)
        self.assertEqual(out.get("dominant_cause"), "node_db_gap")
        self.assertIn("unmatched_blocking_pressure", list(out.get("warning_tags") or []))

    def test_threshold_noise_candidate_flag_is_explicit(self) -> None:
        rows = [
            {"seq": 1, "lat": -0.1, "lon": -78.4, "match_state": "matched", "matched_stop_node_id": "a"},
            {"seq": 2, "lat": -0.1001, "lon": -78.4001, "match_state": "matched", "matched_stop_node_id": "a"},
            {"seq": 3, "lat": -0.1002, "lon": -78.4002, "match_state": "matched", "matched_stop_node_id": "a"},
            {"seq": 4, "lat": -0.1003, "lon": -78.4003, "match_state": "matched", "matched_stop_node_id": "a"},
        ]
        out = evaluate_sequence_quality(
            prior_rows=rows,
            matched_count=4,
            unmatched_count=0,
            ambiguous_count=0,
            sequence_edit_count=9,  # forces score pressure without geometry anomalies
        )
        tags = list(out.get("warning_tags") or [])
        self.assertTrue("sequence_quality_warning" in tags or "sequence_quality_critical" in tags)
        self.assertIn("detector_threshold_noise_candidate", list(out.get("warning_tags") or []))
        self.assertEqual(out.get("dominant_cause"), "detector_thresholds")

    def test_threshold_profile_is_versioned_and_present(self) -> None:
        rows = [
            {"seq": 1, "lat": -0.1, "lon": -78.4, "match_state": "matched", "matched_stop_node_id": "a"},
            {"seq": 2, "lat": -0.101, "lon": -78.401, "match_state": "matched", "matched_stop_node_id": "b"},
        ]
        out = evaluate_sequence_quality(prior_rows=rows, matched_count=2, unmatched_count=0, ambiguous_count=0)
        profile = dict(out.get("threshold_profile") or {})
        self.assertTrue(str(profile.get("version") or "").strip())
        thresholds = dict(profile.get("thresholds") or {})
        self.assertIn("sequence_quality_warning_score", thresholds)
        self.assertIn("unmatched_ratio_blocking", thresholds)


if __name__ == "__main__":
    unittest.main()
