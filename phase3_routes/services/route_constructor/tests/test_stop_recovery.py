from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

ROUTE_CONSTRUCTOR_ROOT = Path(__file__).resolve().parents[1]
if str(ROUTE_CONSTRUCTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(ROUTE_CONSTRUCTOR_ROOT))

from src.geometry.stop_recovery import build_geometry_stop_recovery_result
from src.learning.rank_geometry import rank_geometry_set


def _base_context() -> dict:
    return {
        "geometry_candidate_id": str(uuid.uuid4()),
        "set_id": str(uuid.uuid4()),
        "route_id": str(uuid.uuid4()),
        "stop_sequence_candidate_id": str(uuid.uuid4()),
        "length_m": 220.0,
        "stop_prior_seqs": [1, 2],
    }


def _original_stops(stop_a: str, stop_c: str) -> list[dict]:
    return [
        {
            "stop_id": stop_a,
            "name": "Terminal A",
            "lat": 0.0,
            "lon": 0.0,
            "progress": 0.1,
            "order_index": 1,
        },
        {
            "stop_id": stop_c,
            "name": "Terminal C",
            "lat": 0.0,
            "lon": 0.002,
            "progress": 0.9,
            "order_index": 2,
        },
    ]


class GeometryStopRecoveryTests(unittest.TestCase):
    def test_recovers_missing_stop_near_geometry(self) -> None:
        stop_a = str(uuid.uuid4())
        stop_b = str(uuid.uuid4())
        stop_c = str(uuid.uuid4())
        result = build_geometry_stop_recovery_result(
            _base_context(),
            original_stops=_original_stops(stop_a, stop_c),
            nearby_stops=[
                {
                    "stop_id": stop_b,
                    "name": "Recovered Midpoint",
                    "lat": 0.0,
                    "lon": 0.001,
                    "progress": 0.5,
                    "dist_to_geometry_m": 3.0,
                }
            ],
        )

        self.assertEqual(result["recovered_stop_ids"], [stop_b])
        self.assertEqual(result["ambiguous_nearby_stop_ids"], [])
        self.assertEqual(result["rejected_nearby_stop_ids"], [])
        self.assertEqual(result["enriched_stop_ids"], [stop_a, stop_b, stop_c])
        self.assertEqual(int(result["summary_metrics"]["recovered_count"]), 1)

    def test_rejects_parallel_road_false_positive_outside_tight_corridor(self) -> None:
        stop_a = str(uuid.uuid4())
        stop_d = str(uuid.uuid4())
        stop_c = str(uuid.uuid4())
        result = build_geometry_stop_recovery_result(
            _base_context(),
            original_stops=_original_stops(stop_a, stop_c),
            nearby_stops=[
                {
                    "stop_id": stop_d,
                    "name": "Parallel Road Stop",
                    "lat": 0.00026,
                    "lon": 0.001,
                    "progress": 0.5,
                    "dist_to_geometry_m": 28.0,
                }
            ],
        )

        self.assertEqual(result["recovered_stop_ids"], [])
        self.assertEqual(result["ambiguous_nearby_stop_ids"], [])
        self.assertEqual(result["rejected_nearby_stop_ids"], [stop_d])
        rejected = result["provenance"]["rejected_candidates"][0]
        self.assertEqual(rejected["decision_reason"], "corridor_or_progress_mismatch")

    def test_keeps_competing_nearby_candidates_ambiguous(self) -> None:
        stop_a = str(uuid.uuid4())
        stop_b1 = str(uuid.uuid4())
        stop_b2 = str(uuid.uuid4())
        stop_c = str(uuid.uuid4())
        result = build_geometry_stop_recovery_result(
            _base_context(),
            original_stops=_original_stops(stop_a, stop_c),
            nearby_stops=[
                {
                    "stop_id": stop_b1,
                    "name": "Candidate One",
                    "lat": 0.0,
                    "lon": 0.00092,
                    "progress": 0.46,
                    "dist_to_geometry_m": 3.0,
                },
                {
                    "stop_id": stop_b2,
                    "name": "Candidate Two",
                    "lat": 0.0,
                    "lon": 0.00108,
                    "progress": 0.54,
                    "dist_to_geometry_m": 3.5,
                },
            ],
        )

        self.assertEqual(result["recovered_stop_ids"], [])
        self.assertEqual(result["ambiguous_nearby_stop_ids"], [stop_b1, stop_b2])
        self.assertEqual(int(result["summary_metrics"]["ambiguous_count"]), 2)

    def test_is_idempotent_for_same_inputs(self) -> None:
        stop_a = str(uuid.uuid4())
        stop_b = str(uuid.uuid4())
        stop_c = str(uuid.uuid4())
        context = _base_context()
        original = _original_stops(stop_a, stop_c)
        nearby = [
            {
                "stop_id": stop_b,
                "name": "Recovered Midpoint",
                "lat": 0.0,
                "lon": 0.001,
                "progress": 0.5,
                "dist_to_geometry_m": 3.0,
            }
        ]

        left = build_geometry_stop_recovery_result(context, original_stops=original, nearby_stops=nearby)
        right = build_geometry_stop_recovery_result(context, original_stops=original, nearby_stops=nearby)

        self.assertEqual(left, right)

    def test_noop_when_no_valid_nearby_stops_exist(self) -> None:
        stop_a = str(uuid.uuid4())
        stop_c = str(uuid.uuid4())
        result = build_geometry_stop_recovery_result(
            _base_context(),
            original_stops=_original_stops(stop_a, stop_c),
            nearby_stops=[],
        )

        self.assertEqual(result["recovered_stop_ids"], [])
        self.assertEqual(result["ambiguous_nearby_stop_ids"], [])
        self.assertEqual(result["rejected_nearby_stop_ids"], [])
        self.assertEqual(result["enriched_stop_ids"], [stop_a, stop_c])
        self.assertEqual(int(result["summary_metrics"]["nearby_stops_scanned"]), 0)


class _FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None) -> None:
        self.executed.append((str(sql), params))

    def fetchall(self):
        return list(self.rows)


def _fake_db_cursor(cursor: _FakeCursor):
    @contextmanager
    def _ctx(_conn):
        yield cursor

    return _ctx


class RankGeometryRecoveryTests(unittest.TestCase):
    def test_ranker_uses_step32_recovery_signal(self) -> None:
        candidate_a = str(uuid.uuid4())
        candidate_b = str(uuid.uuid4())
        cursor = _FakeCursor(
            [
                {
                    "geometry_candidate_id": candidate_a,
                    "score": 5.0,
                    "metrics": {},
                    "stop_sequence_candidate_id": str(uuid.uuid4()),
                    "recovery_summary": {
                        "original_stop_count": 2,
                        "recovered_count": 0,
                        "ambiguous_count": 0,
                        "rejected_count": 0,
                    },
                    "recovered_stop_ids": [],
                    "ambiguous_nearby_stop_ids": [],
                    "rejected_nearby_stop_ids": [],
                    "enriched_stop_ids": [],
                },
                {
                    "geometry_candidate_id": candidate_b,
                    "score": 4.0,
                    "metrics": {},
                    "stop_sequence_candidate_id": str(uuid.uuid4()),
                    "recovery_summary": {
                        "original_stop_count": 2,
                        "recovered_count": 1,
                        "ambiguous_count": 0,
                        "rejected_count": 0,
                    },
                    "recovered_stop_ids": [str(uuid.uuid4())],
                    "ambiguous_nearby_stop_ids": [],
                    "rejected_nearby_stop_ids": [],
                    "enriched_stop_ids": [str(uuid.uuid4())],
                },
            ]
        )

        with patch("src.learning.rank_geometry.ensure_geometry_stop_recovery_schema", lambda conn: None), patch(
            "src.learning.rank_geometry.db_cursor",
            _fake_db_cursor(cursor),
        ):
            rank_geometry_set(object(), route_id=uuid.uuid4(), geometry_set_id=uuid.uuid4())

        updates = [
            (sql, params)
            for sql, params in cursor.executed
            if "UPDATE route_work.geometry_candidates" in sql
        ]
        self.assertEqual(len(updates), 2)

        first_metrics = json.loads(updates[0][1][0])
        second_metrics = json.loads(updates[1][1][0])

        self.assertEqual(int(first_metrics["ml_rank"]), 1)
        self.assertEqual(int(second_metrics["ml_rank"]), 2)
        self.assertEqual(len(first_metrics["stop_recovery_recovered_stop_ids"]), 1)
        self.assertGreater(float(first_metrics["ml_score"]), float(second_metrics["ml_score"]))


if __name__ == "__main__":
    unittest.main()
