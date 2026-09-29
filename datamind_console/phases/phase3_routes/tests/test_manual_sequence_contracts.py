from __future__ import annotations

import unittest
import uuid

from datamind_console.phases.phase3_routes.manual_sequence_contracts import (
    normalize_manual_sequence_export_request,
    reversed_sequence,
    validate_manual_sequence_rows,
)


def _u() -> str:
    return str(uuid.uuid4())


class ManualSequenceContractsTests(unittest.TestCase):
    def test_single_stop_sequence_is_rejected(self) -> None:
        sid = _u()
        out = validate_manual_sequence_rows(
            ordered_stop_ids=[sid],
            resolved_rows=[{"stop_id": sid, "lat": -0.1, "lon": -78.4}],
            is_loop=False,
            min_stops=2,
            jump_warn_m=3500.0,
        )
        self.assertTrue(any("minimum is 2 stops" in e for e in out.errors))

    def test_duplicate_consecutive_stops_is_rejected(self) -> None:
        sid = _u()
        out = validate_manual_sequence_rows(
            ordered_stop_ids=[sid, sid],
            resolved_rows=[{"stop_id": sid, "lat": -0.1, "lon": -78.4}],
            is_loop=False,
            min_stops=2,
            jump_warn_m=3500.0,
        )
        self.assertTrue(any("Duplicate consecutive stops" in e for e in out.errors))

    def test_missing_coordinates_is_rejected(self) -> None:
        sid1 = _u()
        sid2 = _u()
        out = validate_manual_sequence_rows(
            ordered_stop_ids=[sid1, sid2],
            resolved_rows=[
                {"stop_id": sid1, "lat": -0.1, "lon": -78.4},
                {"stop_id": sid2, "lat": None, "lon": -78.45},
            ],
            is_loop=False,
            min_stops=2,
            jump_warn_m=3500.0,
        )
        self.assertTrue(any("missing coordinates" in e for e in out.errors))

    def test_loop_route_allows_repeated_stops_warning_suppressed(self) -> None:
        sid1 = _u()
        sid2 = _u()
        out = validate_manual_sequence_rows(
            ordered_stop_ids=[sid1, sid2, sid1],
            resolved_rows=[
                {"stop_id": sid1, "lat": -0.1, "lon": -78.4},
                {"stop_id": sid2, "lat": -0.12, "lon": -78.42},
            ],
            is_loop=True,
            min_stops=2,
            jump_warn_m=3500.0,
        )
        self.assertFalse(any("Repeated stops detected" in w for w in out.warnings))

    def test_excessive_jump_emits_warning(self) -> None:
        sid1 = _u()
        sid2 = _u()
        out = validate_manual_sequence_rows(
            ordered_stop_ids=[sid1, sid2],
            resolved_rows=[
                {"stop_id": sid1, "lat": 0.0, "lon": 0.0},
                {"stop_id": sid2, "lat": 1.0, "lon": 1.0},
            ],
            is_loop=False,
            min_stops=2,
            jump_warn_m=10.0,
        )
        self.assertTrue(any("Excessive jump" in w for w in out.warnings))

    def test_reverse_sequence_action(self) -> None:
        items = [_u(), _u(), _u()]
        self.assertEqual(reversed_sequence(items), list(reversed(items)))

    def test_normalize_accepts_coverage_gap_id(self) -> None:
        gap_id = _u()
        stop_a = _u()
        stop_b = _u()
        out = normalize_manual_sequence_export_request(
            {
                "coverage_gap_id": gap_id,
                "ordered_stop_ids": [stop_a, stop_b],
                "ordered_node_ids": [stop_a, stop_b],
                "source": "manual_builder",
            }
        )
        self.assertEqual(out.coverage_gap_id, gap_id)


if __name__ == "__main__":
    unittest.main()
