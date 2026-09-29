from __future__ import annotations

import unittest
from unittest.mock import patch

from datamind_console.phases.phase1_nodes.client import Phase1Client


class _DummyConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False


class Phase1EvidenceClientTests(unittest.TestCase):
    @patch("datamind_console.phases.phase1_nodes.client.fetchall")
    @patch("datamind_console.phases.phase1_nodes.client.db_conn")
    def test_get_nodes_for_node_set_returns_required_keys_for_map_plotting(self, db_conn_mock, fetchall_mock) -> None:
        db_conn_mock.return_value = _DummyConn()
        fetchall_mock.return_value = [
            {
                "point_id": "node-1",
                "node_set_id": "ns-1",
                "lat": -0.1001,
                "lon": -78.50,
                "name": "Stop A",
                "tags": {"name": "Stop A", "highway": "bus_stop"},
                "confidence": 0.88,
                "approval_status": "approved",
                "cluster_id": None,
                "created_at": "2026-03-05T00:00:00+00:00",
            }
        ]
        client = Phase1Client()
        resolved = client.get_nodes_for_node_set("ns-1", mode="resolved", limit=100, offset=0)
        self.assertTrue(resolved)
        for key in (
            "point_id",
            "node_set_id",
            "mode",
            "lat",
            "lon",
            "name",
            "tags",
            "tags_summary",
            "confidence",
            "approval_status",
            "cluster_id",
        ):
            self.assertIn(key, resolved[0])

        fetchall_mock.return_value = [
            {
                "point_id": "cand-1",
                "node_set_id": "ns-1",
                "lat": -0.1002,
                "lon": -78.5002,
                "name": "Candidate B",
                "tags": {"name": "Candidate B", "public_transport": "platform"},
                "tag_kind": "platform",
                "confidence": 0.42,
                "approval_status": None,
                "cluster_id": None,
                "created_at": "2026-03-05T00:01:00+00:00",
            }
        ]
        candidates = client.get_nodes_for_node_set("ns-1", mode="candidates", limit=100, offset=0)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].get("mode"), "candidates")
        self.assertIn("tag_kind", candidates[0])


if __name__ == "__main__":
    unittest.main()
