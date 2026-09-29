from __future__ import annotations

from contextlib import contextmanager
import subprocess
import unittest
import uuid
from unittest.mock import patch

from datamind_console.phases.phase3_routes.client import Phase3Client
from phase3_routes.services.route_constructor.src.sequence import candidates as sequence_candidates


class _FakeCursor:
    def __init__(self, *, fetchall_rows=None, fetchone_rows=None) -> None:
        self.fetchall_rows = list(fetchall_rows or [])
        self.fetchone_rows = list(fetchone_rows or [])
        self.executed = []

    def execute(self, sql, params=None) -> None:
        self.executed.append((sql, params))

    def fetchall(self):
        if self.fetchall_rows:
            return self.fetchall_rows.pop(0)
        return []

    def fetchone(self):
        if self.fetchone_rows:
            return self.fetchone_rows.pop(0)
        return {}


def _fake_db_context(cursor: _FakeCursor):
    @contextmanager
    def _db_conn():
        yield object()

    @contextmanager
    def _db_cursor(_conn):
        yield cursor

    return _db_conn, _db_cursor


class ManualSequenceClientTests(unittest.TestCase):
    def _client(self) -> Phase3Client:
        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            c = Phase3Client()
        c._manual_sequence_schema_ready = True
        return c

    def test_load_approved_prod_stops(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        cursor = _FakeCursor(
            fetchall_rows=[
                [
                    {
                        "stop_id": sid,
                        "place_id": str(uuid.uuid4()),
                        "name": "Av. Central",
                        "ref": "S12",
                        "operator": "DATAMIND",
                        "mapping_source": "phase2_auto",
                        "confidence": 0.98,
                        "lat": -0.201,
                        "lon": -78.49,
                        "place_type": "STOP",
                        "place_status": "active",
                    }
                ]
            ]
        )
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)
        with patch("datamind_console.phases.phase3_routes.client.db_conn", fake_db_conn), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor", fake_db_cursor
        ):
            rows = client.list_manual_builder_approved_stops(search="central", limit=20)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stop_id"], sid)
        self.assertEqual(rows[0]["source"] if "source" in rows[0] else "manual_builder", "manual_builder")

    def test_export_rejects_empty_sequence(self) -> None:
        client = self._client()
        with patch.object(client, "_ensure_manual_sequence_schema", lambda: None):
            with self.assertRaises(ValueError):
                client.export_manual_sequence_to_phase3(
                    {
                        "route_job_id": str(uuid.uuid4()),
                        "ordered_stop_ids": [],
                        "is_loop": False,
                        "source": "manual_builder",
                    }
                )

    def test_export_rejects_unknown_stop_id(self) -> None:
        client = self._client()
        sid1 = str(uuid.uuid4())
        sid2 = str(uuid.uuid4())
        with patch.object(client, "_ensure_manual_sequence_schema", lambda: None), patch.object(
            client,
            "_resolve_manual_builder_stop_rows",
            return_value=[{"stop_id": sid1, "lat": -0.1, "lon": -78.4}],
        ):
            with self.assertRaises(ValueError):
                client.export_manual_sequence_to_phase3(
                    {
                        "route_job_id": str(uuid.uuid4()),
                        "ordered_stop_ids": [sid1, sid2],
                        "is_loop": False,
                        "source": "manual_builder",
                    }
                )

    def test_export_valid_payload_sets_manual_source(self) -> None:
        client = self._client()
        route_id = str(uuid.uuid4())
        stop_a = str(uuid.uuid4())
        stop_b = str(uuid.uuid4())
        set_id = uuid.uuid4()
        candidate_id = uuid.uuid4()
        export_id = str(uuid.uuid4())

        cursor = _FakeCursor(fetchone_rows=[{"export_id": export_id, "created_at": "2026-03-01T00:00:00Z"}])
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)

        with patch.object(client, "_ensure_manual_sequence_schema", lambda: None), patch.object(
            client,
            "_resolve_manual_builder_stop_rows",
            return_value=[
                {"stop_id": stop_a, "lat": -0.1, "lon": -78.4},
                {"stop_id": stop_b, "lat": -0.12, "lon": -78.45},
            ],
        ), patch.object(
            client,
            "get_route_job",
            return_value={"route_id": route_id},
        ), patch.object(
            client,
            "replace_relation_stop_prior",
            return_value=None,
        ) as replace_prior, patch.object(
            client,
            "mark_direction_progress_by_route",
            return_value=None,
        ), patch(
            "datamind_console.phases.phase3_routes.client.create_stop_sequence_set",
            return_value=set_id,
        ), patch(
            "datamind_console.phases.phase3_routes.client.insert_stop_sequence_candidate",
            return_value=candidate_id,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ):
            out = client.export_manual_sequence_to_phase3(
                {
                    "route_job_id": route_id,
                    "ordered_stop_ids": [stop_a, stop_b],
                    "ordered_node_ids": [stop_a, stop_b],
                    "is_loop": False,
                    "source": "manual_builder",
                }
            )

        self.assertEqual(out["source"], "manual_builder")
        self.assertEqual(out["stop_sequence_set_id"], str(set_id))
        self.assertEqual(out["stop_sequence_candidate_id"], str(candidate_id))
        self.assertEqual(out["export_id"], export_id)
        replace_prior.assert_called_once()

    def test_export_links_coverage_gap_when_present(self) -> None:
        client = self._client()
        route_id = str(uuid.uuid4())
        stop_a = str(uuid.uuid4())
        stop_b = str(uuid.uuid4())
        gap_id = str(uuid.uuid4())
        set_id = uuid.uuid4()
        candidate_id = uuid.uuid4()
        export_id = str(uuid.uuid4())

        cursor = _FakeCursor(fetchone_rows=[{"export_id": export_id, "created_at": "2026-03-01T00:00:00Z"}])
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)

        with patch.object(client, "_ensure_manual_sequence_schema", lambda: None), patch.object(
            client,
            "_resolve_manual_builder_stop_rows",
            return_value=[
                {"stop_id": stop_a, "lat": -0.1, "lon": -78.4},
                {"stop_id": stop_b, "lat": -0.12, "lon": -78.45},
            ],
        ), patch.object(
            client,
            "get_route_job",
            return_value={"route_id": route_id},
        ), patch.object(
            client,
            "replace_relation_stop_prior",
            return_value=None,
        ), patch.object(
            client,
            "mark_direction_progress_by_route",
            return_value=None,
        ), patch.object(
            client,
            "link_phase3_coverage_gap_to_route",
            return_value={"gap_id": gap_id, "resolution_status": "in_progress"},
        ) as link_gap, patch(
            "datamind_console.phases.phase3_routes.client.create_stop_sequence_set",
            return_value=set_id,
        ), patch(
            "datamind_console.phases.phase3_routes.client.insert_stop_sequence_candidate",
            return_value=candidate_id,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ):
            out = client.export_manual_sequence_to_phase3(
                {
                    "route_job_id": route_id,
                    "coverage_gap_id": gap_id,
                    "ordered_stop_ids": [stop_a, stop_b],
                    "ordered_node_ids": [stop_a, stop_b],
                    "is_loop": False,
                    "source": "manual_builder",
                }
            )

        self.assertEqual(out["coverage_gap_id"], gap_id)
        link_gap.assert_called_once()

    def test_link_gap_to_route_applies_route_job_gap_metadata(self) -> None:
        client = self._client()
        gap_id = str(uuid.uuid4())
        route_id = str(uuid.uuid4())
        cursor = _FakeCursor()
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)

        with patch.object(client, "_ensure_route_review_schema", lambda: None), patch.object(
            client,
            "get_phase3_coverage_gap",
            side_effect=[
                {
                    "gap_id": gap_id,
                    "sector_key": "pintag_corridor",
                    "sector_label": "Pintag Corridor",
                    "route_family_hint": "Pintag-La Marin",
                    "start_hint": "Pintag",
                    "end_hint": "La Marin",
                    "source_catalog": "valle_de_los_chillos_phase3_catalog_v1",
                    "heuristic_notes": {"operator_hints": ["General Pintag"]},
                },
                {"gap_id": gap_id, "resolution_status": "in_progress"},
            ],
        ), patch.object(
            client,
            "_persist_route_job_extractor_review",
            return_value={},
        ) as persist_review, patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ):
            out = client.link_phase3_coverage_gap_to_route(
                gap_id=gap_id,
                route_id=route_id,
                reviewed_by="codex",
            )

        self.assertEqual(out["resolution_status"], "in_progress")
        persist_review.assert_called_once()
        _, kwargs = persist_review.call_args
        review_patch = kwargs["review_patch"]
        self.assertEqual(review_patch["geography"]["sector_hint"], "pintag_corridor")
        self.assertEqual(review_patch["hints"]["route_hint_raw"], "Pintag-La Marin")
        self.assertEqual(review_patch["hints"]["cooperative_hint"], "General Pintag")
        self.assertTrue(
            any("UPDATE route_raw.route_jobs" in sql for sql, _params in cursor.executed),
            msg="expected route_raw.route_jobs metadata hydration update",
        )

    def test_downstream_handoff_reads_exported_sequence(self) -> None:
        client = self._client()
        route_uuid = uuid.uuid4()
        export_uuid = uuid.uuid4()
        set_uuid = uuid.uuid4()
        candidate_uuid = uuid.uuid4()
        stop_a = str(uuid.uuid4())
        stop_b = str(uuid.uuid4())

        cursor = _FakeCursor(
            fetchone_rows=[
                {"export_id": str(export_uuid)},
                {
                    "export_id": str(export_uuid),
                    "route_job_id": str(route_uuid),
                    "service_route_id": None,
                    "direction_id": 0,
                    "coverage_gap_id": str(uuid.uuid4()),
                    "source": "manual_builder",
                    "ordered_stop_ids": [stop_a, stop_b],
                    "ordered_node_ids": [stop_a, stop_b],
                    "ordered_coords": [[-78.4, -0.1], [-78.45, -0.12]],
                    "is_loop": False,
                    "name_hint": None,
                    "operator_hint": None,
                    "variant_hint": None,
                    "created_by": "tester",
                    "created_at": "2026-03-01T00:00:00Z",
                    "stop_sequence_set_id": str(set_uuid),
                    "stop_sequence_candidate_id": str(candidate_uuid),
                    "draft_id": None,
                },
            ]
        )
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)

        with patch.object(client, "_ensure_manual_sequence_schema", lambda: None), patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ), patch.object(
            client,
            "get_sequence_candidates",
            return_value={
                "route_id": str(route_uuid),
                "set_id": str(set_uuid),
                "candidates": [{"candidate_id": str(candidate_uuid)}],
            },
        ):
            handoff = client.get_manual_sequence_handoff(route_uuid)

        self.assertEqual(handoff["manual_export"]["source"], "manual_builder")
        self.assertTrue(str(handoff.get("coverage_gap_id") or "").strip())
        self.assertEqual(handoff["sequence"]["set_id"], str(set_uuid))
        self.assertEqual(len(handoff["sequence"]["candidates"]), 1)

    def test_happy_path_integration_like_flow(self) -> None:
        client = self._client()
        route_id = str(uuid.uuid4())
        stop_a = str(uuid.uuid4())
        stop_b = str(uuid.uuid4())
        set_id = uuid.uuid4()
        candidate_id = uuid.uuid4()
        export_id = str(uuid.uuid4())

        export_cursor = _FakeCursor(fetchone_rows=[{"export_id": export_id, "created_at": "2026-03-01T00:00:00Z"}])
        fake_db_conn, fake_db_cursor = _fake_db_context(export_cursor)

        with patch.object(client, "_ensure_manual_sequence_schema", lambda: None), patch.object(
            client,
            "_resolve_manual_builder_stop_rows",
            return_value=[
                {"stop_id": stop_a, "lat": -0.1, "lon": -78.4},
                {"stop_id": stop_b, "lat": -0.11, "lon": -78.42},
            ],
        ), patch.object(client, "get_route_job", return_value={"route_id": route_id}), patch.object(
            client,
            "replace_relation_stop_prior",
            return_value=None,
        ), patch.object(
            client,
            "mark_direction_progress_by_route",
            return_value=None,
        ), patch(
            "datamind_console.phases.phase3_routes.client.create_stop_sequence_set",
            return_value=set_id,
        ), patch(
            "datamind_console.phases.phase3_routes.client.insert_stop_sequence_candidate",
            return_value=candidate_id,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ):
            out = client.export_manual_sequence_to_phase3(
                {
                    "route_job_id": route_id,
                    "ordered_stop_ids": [stop_a, stop_b],
                    "is_loop": True,
                    "source": "manual_builder",
                }
            )

        self.assertEqual(out["route_job_id"], route_id)
        self.assertEqual(out["source"], "manual_builder")
        self.assertEqual(out["stop_sequence_set_id"], str(set_id))
        self.assertEqual(out["stop_sequence_candidate_id"], str(candidate_id))

    def test_get_step30_gate_blocks_until_canonical_sequence_is_approved(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()

        with patch.object(
            client,
            "get_sequence_resolution_state",
            return_value={
                "approved_stop_sequence_candidate_id": None,
                "sequence_stabilized": False,
                "approval_status": None,
                "variant_pressure_detected": False,
                "variant_pressure_reasons": [],
                "direction_stable": True,
                "direction_reasons": [],
            },
        ), patch.object(
            client,
            "get_prior_match_report",
            return_value={"all_matched": True, "total": 2, "matched": 2, "unmatched": 0, "ambiguous": 0},
        ):
            gate = client.get_step30_gate(route_id)

        self.assertFalse(gate["can_run_step30"])
        self.assertIn("sequence_not_approved", list(gate.get("blocking_reasons") or []))

    def test_get_step30_gate_rejects_noncanonical_candidate(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        approved_candidate_id = uuid.uuid4()
        requested_candidate_id = uuid.uuid4()

        with patch.object(
            client,
            "get_sequence_resolution_state",
            return_value={
                "approved_stop_sequence_candidate_id": str(approved_candidate_id),
                "sequence_stabilized": True,
                "approval_status": "approved",
                "variant_pressure_detected": False,
                "variant_pressure_reasons": [],
                "direction_stable": True,
                "direction_reasons": [],
            },
        ), patch.object(
            client,
            "get_prior_match_report",
            return_value={"all_matched": True, "total": 2, "matched": 2, "unmatched": 0, "ambiguous": 0},
        ):
            gate = client.get_step30_gate(route_id, stop_sequence_candidate_id=requested_candidate_id)

        self.assertFalse(gate["can_run_step30"])
        self.assertIn("requested_sequence_not_canonical", list(gate.get("blocking_reasons") or []))

    def test_ensure_direction_ready_for_step20_refreshes_stale_persisted_gate(self) -> None:
        client = self._client()
        route_id = str(uuid.uuid4())
        service_route_id = str(uuid.uuid4())

        with patch.object(
            client,
            "get_step20_direction_gate",
            side_effect=[
                {
                    "gate_passed": False,
                    "gate_code": "direction_not_ready",
                    "service_route_id": service_route_id,
                    "direction_id": 1,
                    "route_id": route_id,
                    "blocker_codes": ["inverse_state_missing"],
                    "blocker_messages": ["Persisted inverse-completion state is missing."],
                },
                {
                    "gate_passed": True,
                    "gate_code": "direction_ready",
                    "service_route_id": service_route_id,
                    "direction_id": 1,
                    "route_id": route_id,
                    "blocker_codes": [],
                    "blocker_messages": [],
                },
            ],
        ) as get_gate, patch.object(
            client,
            "refresh_direction_readiness",
            return_value={"persisted_row_count": 2},
        ) as refresh:
            gate = client.ensure_direction_ready_for_step20(route_id=route_id)

        self.assertTrue(gate["gate_passed"])
        self.assertTrue(gate["direction_gate_refresh_attempted"])
        self.assertEqual(gate["direction_gate_pre_refresh_code"], "direction_not_ready")
        refresh.assert_called_once_with(
            service_route_id=service_route_id,
            route_id=route_id,
            include_ready=True,
        )
        self.assertEqual(get_gate.call_count, 2)

    def test_ensure_direction_ready_for_step20_keeps_strict_block_after_refresh(self) -> None:
        client = self._client()
        route_id = str(uuid.uuid4())
        service_route_id = str(uuid.uuid4())

        blocked_gate = {
            "gate_passed": False,
            "gate_code": "direction_not_ready",
            "service_route_id": service_route_id,
            "direction_id": 0,
            "route_id": route_id,
            "blocker_codes": ["inverse_missing_candidate"],
            "blocker_messages": ["Missing opposite direction candidate."],
        }

        with patch.object(
            client,
            "get_step20_direction_gate",
            side_effect=[blocked_gate, blocked_gate],
        ) as get_gate, patch.object(
            client,
            "refresh_direction_readiness",
            return_value={"persisted_row_count": 2},
        ) as refresh:
            gate = client.ensure_direction_ready_for_step20(route_id=route_id)

        self.assertFalse(gate["gate_passed"])
        self.assertEqual(gate["gate_code"], "direction_not_ready")
        self.assertTrue(gate["direction_gate_refresh_attempted"])
        refresh.assert_called_once_with(
            service_route_id=service_route_id,
            route_id=route_id,
            include_ready=True,
        )
        self.assertEqual(get_gate.call_count, 2)

    def test_get_sequence_resolution_state_groups_same_direction_variants(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        set_id = uuid.uuid4()
        candidate_a = uuid.uuid4()
        candidate_b = uuid.uuid4()
        candidate_inverse = uuid.uuid4()

        with patch.object(client, "_ensure_sequence_resolution_schema", lambda: None), patch.object(
            client,
            "list_stop_sequence_sets",
            return_value=[{"set_id": str(set_id)}],
        ), patch.object(
            client,
            "get_sequence_candidates",
            return_value={
                "route_id": str(route_id),
                "set_id": str(set_id),
                "candidates": [
                    {
                        "candidate_id": str(candidate_a),
                        "rank": 1,
                        "stop_prior_seqs": [1, 2, 3, 4, 5, 6],
                        "stop_node_ids": [],
                        "metrics": {
                            "sequence_score": 94.0,
                            "variant_group_key": "forward:main",
                            "variant_group_label": "forward | main",
                            "sequence_orientation": "forward",
                        },
                    },
                    {
                        "candidate_id": str(candidate_b),
                        "rank": 2,
                        "stop_prior_seqs": [1, 2, 3, 6, 5, 4],
                        "stop_node_ids": [],
                        "metrics": {
                            "sequence_score": 91.5,
                            "variant_group_key": "forward:branch",
                            "variant_group_label": "forward | branch",
                            "sequence_orientation": "forward",
                        },
                    },
                    {
                        "candidate_id": str(candidate_inverse),
                        "rank": 3,
                        "stop_prior_seqs": [6, 5, 4, 3, 2, 1],
                        "stop_node_ids": [],
                        "metrics": {
                            "sequence_score": 88.0,
                            "variant_group_key": "inverse:check",
                            "variant_group_label": "inverse | check",
                            "sequence_orientation": "inverse",
                        },
                    },
                ],
            },
        ), patch.object(
            client,
            "get_sequence_approval",
            return_value={},
        ), patch.object(
            client,
            "get_relation_stop_prior",
            return_value=[{"seq": idx} for idx in range(1, 7)],
        ), patch.object(
            client,
            "_get_direction_stability",
            return_value={
                "service_route_id": str(uuid.uuid4()),
                "direction_id": 0,
                "direction_stable": True,
                "direction_reasons": [],
            },
        ):
            state = client.get_sequence_resolution_state(route_id)

        self.assertEqual(state["variant_state"], "unresolved_multi_variant")
        self.assertTrue(state["variant_pressure_detected"])
        self.assertEqual(state["recommended_variant_group_key"], "forward:main")
        self.assertEqual(len(list(state.get("variant_groups") or [])), 3)

    def test_get_step30_gate_only_blocks_variant_pressure_when_unresolved_multi_variant(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()

        with patch.object(
            client,
            "get_sequence_resolution_state",
            return_value={
                "approved_stop_sequence_candidate_id": None,
                "sequence_stabilized": False,
                "approval_status": None,
                "variant_state": "mild_variant_pressure_dominant_group",
                "variant_pressure_detected": True,
                "variant_pressure_reasons": ["divergent_tails_with_shared_corridor"],
                "variant_groups": [],
                "direction_stable": True,
                "direction_reasons": [],
            },
        ), patch.object(
            client,
            "get_prior_match_report",
            return_value={"all_matched": True, "total": 2, "matched": 2, "unmatched": 0, "ambiguous": 0},
        ):
            mild_gate = client.get_step30_gate(route_id)

        self.assertNotIn("variant_pressure_blocking", list(mild_gate.get("blocking_reasons") or []))

        with patch.object(
            client,
            "get_sequence_resolution_state",
            return_value={
                "approved_stop_sequence_candidate_id": None,
                "sequence_stabilized": False,
                "approval_status": None,
                "variant_state": "unresolved_multi_variant",
                "variant_pressure_detected": True,
                "variant_pressure_reasons": ["divergent_tails_with_shared_corridor"],
                "variant_groups": [],
                "direction_stable": True,
                "direction_reasons": [],
            },
        ), patch.object(
            client,
            "get_prior_match_report",
            return_value={"all_matched": True, "total": 2, "matched": 2, "unmatched": 0, "ambiguous": 0},
        ):
            unresolved_gate = client.get_step30_gate(route_id)

        self.assertIn("variant_pressure_blocking", list(unresolved_gate.get("blocking_reasons") or []))

    def test_variant_resolution_preserves_unresolved_state_when_structural_gap_is_small(self) -> None:
        client = self._client()
        candidates = [
            {
                "candidate_id": str(uuid.uuid4()),
                "rank": 1,
                "stop_prior_seqs": [1, 2, 3, 4],
                "metrics": {
                    "sequence_score": 96.0,
                    "structural_sequence_score": 95.8,
                    "variant_group_key": "forward:main",
                    "variant_group_label": "forward | main",
                    "sequence_orientation": "forward",
                },
            },
            {
                "candidate_id": str(uuid.uuid4()),
                "rank": 2,
                "stop_prior_seqs": [1, 2, 6, 7],
                "metrics": {
                    "sequence_score": 84.0,
                    "structural_sequence_score": 90.2,
                    "variant_group_key": "forward:branch",
                    "variant_group_label": "forward | branch",
                    "sequence_orientation": "forward",
                },
            },
            {
                "candidate_id": str(uuid.uuid4()),
                "rank": 3,
                "stop_prior_seqs": [1, 2, 6, 7],
                "metrics": {
                    "sequence_score": 80.0,
                    "structural_sequence_score": 88.6,
                    "variant_group_key": "forward:branch",
                    "variant_group_label": "forward | branch",
                    "sequence_orientation": "forward",
                },
            },
        ]

        summary = client._assess_variant_resolution(candidates)

        self.assertEqual(summary.get("variant_state"), "unresolved_multi_variant")

    def test_get_sequence_approval_hides_variant_group_when_invalidated(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        cursor = _FakeCursor(
            fetchone_rows=[
                {
                    "sequence_approval_id": str(uuid.uuid4()),
                    "route_id": str(route_id),
                    "stop_sequence_set_id": str(uuid.uuid4()),
                    "chosen_stop_sequence_candidate_id": str(uuid.uuid4()),
                    "approval_status": "invalidated",
                    "approved_at": None,
                    "approved_by": None,
                    "notes": None,
                    "invalidated_at": "2026-03-08T00:00:00Z",
                    "invalidated_reason": "canonical_sequence_changed",
                    "created_at": "2026-03-08T00:00:00Z",
                    "updated_at": "2026-03-08T00:00:00Z",
                    "candidate_rank": 1,
                    "candidate_metrics": {
                        "variant_group_key": "forward:main",
                        "variant_group_label": "forward | main",
                    },
                }
            ]
        )
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)

        with patch("datamind_console.phases.phase3_routes.client.db_conn", fake_db_conn), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ):
            out = client.get_sequence_approval(route_id)

        self.assertIsNone(out.get("approved_variant_group_key"))
        self.assertIsNone(out.get("approved_variant_group_label"))

    def test_sequence_shortlist_row_exposes_valhalla_evidence(self) -> None:
        client = self._client()
        candidate_id = uuid.uuid4()

        row = client._sequence_shortlist_row(
            {
                "candidate_id": str(candidate_id),
                "rank": 1,
                "stop_prior_seqs": [1, 2, 3],
                "metrics": {
                    "family": "current_relation_order",
                    "label": "Current relation order",
                    "sequence_score": 91.2,
                    "structural_sequence_score": 94.0,
                    "valhalla_evidence_status": "available",
                    "valhalla_traversability_score": 78.4,
                    "valhalla_segment_success_rate": 0.92,
                    "valhalla_failed_segment_count": 1,
                    "valhalla_detour_penalty": 7.5,
                    "valhalla_backtrack_penalty": 0.0,
                    "valhalla_path_vs_geodesic_ratio": 1.88,
                    "network_risk_indicators": ["extreme_detour_pressure"],
                    "valhalla_evidence_summary": ["segment_success_rate=0.92"],
                    "variant_group_key": "forward:main",
                    "variant_group_label": "forward | main",
                    "sequence_orientation": "forward",
                },
            }
        )

        self.assertEqual(row["valhalla_evidence_status"], "available")
        self.assertAlmostEqual(float(row["structural_sequence_score"] or 0.0), 94.0, places=3)
        self.assertAlmostEqual(float(row["traversability_score"] or 0.0), 78.4, places=3)
        self.assertAlmostEqual(float(row["segment_success_rate"] or 0.0), 0.92, places=3)
        self.assertEqual(int(row["failed_segment_count"] or 0), 1)
        self.assertIn("extreme_detour_pressure", list(row.get("network_risk_indicators") or []))

    def test_build_sequence_candidates_adds_valhalla_evidence_when_available(self) -> None:
        set_id = uuid.uuid4()
        inserted_metrics = []

        def _capture_insert(conn, *, set_id, rank, stop_node_ids, stop_prior_seqs=None, metrics=None):
            del conn, set_id, rank, stop_node_ids, stop_prior_seqs
            inserted_metrics.append(dict(metrics or {}))
            return uuid.uuid4()

        prior_rows = [
            {
                "seq": idx + 1,
                "lat": -0.10 - (idx * 0.005),
                "lon": -78.40 - (idx * 0.01),
                "matched_stop_node_id": uuid.uuid4(),
                "match_dist_m": 0.5,
            }
            for idx in range(4)
        ]

        with patch.object(sequence_candidates, "replace_stop_prior", return_value=None), patch.object(
            sequence_candidates,
            "create_stop_sequence_set",
            return_value=set_id,
        ), patch.object(
            sequence_candidates,
            "insert_stop_sequence_candidate",
            side_effect=_capture_insert,
        ), patch.object(
            sequence_candidates,
            "valhalla_route",
            side_effect=lambda locations, costing_options=None, timeout_s=45: list(locations),
        ):
            out = sequence_candidates.build_sequence_candidates(object(), uuid.uuid4(), prior_rows)

        self.assertEqual(out, set_id)
        self.assertTrue(inserted_metrics)
        top_metrics = inserted_metrics[0]
        self.assertEqual(top_metrics.get("valhalla_evidence_status"), "available")
        self.assertIsNotNone(top_metrics.get("valhalla_traversability_score"))
        self.assertIn("structural_sequence_score", top_metrics)
        self.assertIn("combined_sequence_score", top_metrics)
        self.assertIn("valhalla_score_blend_weight", top_metrics)
        self.assertGreaterEqual(float(top_metrics.get("valhalla_segment_success_rate") or 0.0), 1.0)

    def test_build_sequence_candidates_falls_back_when_valhalla_unavailable(self) -> None:
        set_id = uuid.uuid4()
        inserted_metrics = []

        def _capture_insert(conn, *, set_id, rank, stop_node_ids, stop_prior_seqs=None, metrics=None):
            del conn, set_id, rank, stop_node_ids, stop_prior_seqs
            inserted_metrics.append(dict(metrics or {}))
            return uuid.uuid4()

        prior_rows = [
            {
                "seq": idx + 1,
                "lat": -0.10 - (idx * 0.005),
                "lon": -78.40 - (idx * 0.01),
                "matched_stop_node_id": uuid.uuid4(),
                "match_dist_m": 0.5,
            }
            for idx in range(4)
        ]

        with patch.object(sequence_candidates, "replace_stop_prior", return_value=None), patch.object(
            sequence_candidates,
            "create_stop_sequence_set",
            return_value=set_id,
        ), patch.object(
            sequence_candidates,
            "insert_stop_sequence_candidate",
            side_effect=_capture_insert,
        ), patch.object(
            sequence_candidates,
            "valhalla_route",
            side_effect=RuntimeError("valhalla down"),
        ):
            out = sequence_candidates.build_sequence_candidates(object(), uuid.uuid4(), prior_rows)

        self.assertEqual(out, set_id)
        self.assertTrue(inserted_metrics)
        top_metrics = inserted_metrics[0]
        self.assertEqual(top_metrics.get("valhalla_evidence_status"), "unavailable")
        self.assertEqual(
            float(top_metrics.get("sequence_score") or 0.0),
            float(top_metrics.get("structural_sequence_score") or 0.0),
        )
        self.assertIn(
            "valhalla_evidence_unavailable",
            list(top_metrics.get("network_risk_indicators") or []),
        )

    def test_get_ranked_candidates_preserves_stop_sequence_candidate_id(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        geometry_set_id = uuid.uuid4()
        stop_sequence_candidate_id = uuid.uuid4()
        cursor = _FakeCursor(
            fetchall_rows=[
                [
                    {
                        "set_id": str(geometry_set_id),
                        "geometry_candidate_id": str(uuid.uuid4()),
                        "stop_sequence_candidate_id": str(stop_sequence_candidate_id),
                        "label": "top_choice",
                        "ml_rank": 1,
                        "score": 0.82,
                        "length_m": 1234.5,
                        "avg_stop_dist_m": 3.2,
                        "max_stop_dist_m": 7.8,
                        "metrics": {"ml_rank": 1},
                        "created_at": "2026-03-07T00:00:00Z",
                    }
                ]
            ]
        )
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)

        with patch("datamind_console.phases.phase3_routes.client.db_conn", fake_db_conn), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ):
            out = client.get_ranked_candidates(route_id, geometry_set_id=geometry_set_id)

        self.assertEqual(len(out["candidates"]), 1)
        self.assertEqual(
            str(out["candidates"][0].get("stop_sequence_candidate_id") or ""),
            str(stop_sequence_candidate_id),
        )

    def test_run_step_32_stop_recovery_parses_summary(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        geometry_set_id = uuid.uuid4()
        stdout = (
            f"geometry_stop_recovery_set_id: {geometry_set_id}\n"
            "geometry_candidates_processed: 3\n"
            "recovered_total: 2\n"
            "ambiguous_total: 1\n"
            "rejected_total: 4\n"
        )

        with patch.object(client, "_ensure_geometry_stop_recovery_schema", lambda: None), patch(
            "datamind_console.phases.phase3_routes.client._run_script",
            return_value=subprocess.CompletedProcess(args=["step32"], returncode=0, stdout=stdout, stderr=""),
        ), patch.object(
            client,
            "mark_direction_progress_by_route",
            return_value=None,
        ):
            out = client.run_step_32_stop_recovery(route_id=route_id, geometry_set_id=geometry_set_id)

        self.assertEqual(out["geometry_set_id"], str(geometry_set_id))
        self.assertEqual(int(out["geometry_candidate_count"]), 3)
        self.assertEqual(int(out["recovered_total"]), 2)
        self.assertEqual(int(out["ambiguous_total"]), 1)
        self.assertEqual(int(out["rejected_total"]), 4)

    def test_approve_geometry_persists_canonical_sequence_lineage(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        geometry_candidate_id = uuid.uuid4()
        approved_sequence_candidate_id = uuid.uuid4()
        service_route_id = uuid.uuid4()
        stop_a = str(uuid.uuid4())
        stop_b = str(uuid.uuid4())
        cursor = _FakeCursor(
            fetchone_rows=[
                {
                    "geometry_candidate_id": str(geometry_candidate_id),
                    "geom": "LINESTRING(-78.4 -0.1,-78.45 -0.12)",
                    "stop_sequence_candidate_id": str(approved_sequence_candidate_id),
                    "route_id": str(route_id),
                },
                {
                    "chosen_stop_sequence_candidate_id": str(approved_sequence_candidate_id),
                    "approval_status": "approved",
                    "sequence_approved_at": "2026-03-07T12:00:00Z",
                    "sequence_approved_by": "sequence-op",
                    "service_route_id": str(service_route_id),
                    "direction_id": 1,
                },
                {"stop_node_ids": [stop_a, stop_b]},
            ]
        )
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)

        with patch.object(client, "_ensure_sequence_resolution_schema", lambda: None), patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ), patch.object(
            client,
            "mark_direction_approved_by_route",
            return_value=None,
        ), patch.object(
            client,
            "_resolve_linked_coverage_gaps_for_route",
            return_value=None,
        ):
            client.approve_geometry(
                route_id=route_id,
                geometry_candidate_id=geometry_candidate_id,
                approved_by="tester",
            )

        prod_upserts = [
            (sql, params)
            for sql, params in cursor.executed
            if "INSERT INTO route_prod.routes" in str(sql)
        ]
        self.assertEqual(len(prod_upserts), 1)
        prod_sql, prod_params = prod_upserts[0]
        self.assertIn("chosen_stop_sequence_candidate_id", str(prod_sql))
        self.assertIn("canonical_sequence_ready", str(prod_sql))
        self.assertEqual(str(prod_params[2]), str(approved_sequence_candidate_id))
        self.assertEqual(str(prod_params[3]), "2026-03-07T12:00:00Z")
        self.assertEqual(str(prod_params[4]), "sequence-op")
        self.assertEqual(str(prod_params[5]), str(service_route_id))
        self.assertEqual(int(prod_params[6]), 1)

    def test_approve_geometry_rejects_noncanonical_geometry_candidate(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        geometry_candidate_id = uuid.uuid4()
        geometry_sequence_candidate_id = uuid.uuid4()
        approved_sequence_candidate_id = uuid.uuid4()
        cursor = _FakeCursor(
            fetchone_rows=[
                {
                    "geometry_candidate_id": str(geometry_candidate_id),
                    "geom": "LINESTRING(-78.4 -0.1,-78.45 -0.12)",
                    "stop_sequence_candidate_id": str(geometry_sequence_candidate_id),
                    "route_id": str(route_id),
                },
                {
                    "chosen_stop_sequence_candidate_id": str(approved_sequence_candidate_id),
                    "approval_status": "approved",
                    "service_route_id": str(uuid.uuid4()),
                    "direction_id": 0,
                },
            ]
        )
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)

        with patch.object(client, "_ensure_sequence_resolution_schema", lambda: None), patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ):
            with self.assertRaises(RuntimeError):
                client.approve_geometry(
                    route_id=route_id,
                    geometry_candidate_id=geometry_candidate_id,
                    approved_by="tester",
                )


if __name__ == "__main__":
    unittest.main()
