from __future__ import annotations

from contextlib import contextmanager
import unittest
import uuid
from unittest.mock import patch

from datamind_console.phases.phase3_routes.client import Phase3Client
from pipeline.phase_4_5.pair_detection.inverse_completion.src.adapters.merge_proposal_adapter import (
    analyze_inverse_proposals_for_results,
)
from pipeline.phase_4_5.pair_detection.inverse_completion.src.core.models import (
    DirectionReadinessResult,
    InverseProposalSnapshot,
    PersistedDirectionReadinessRow,
    TargetedInverseSearchResult,
)
from pipeline.phase_4_5.pair_detection.inverse_completion.src.dispatch.targeted_inverse_dispatch import (
    build_targeted_inverse_search_request,
    dispatch_targeted_inverse_search,
)
from pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion import (
    dispatch_targeted_inverse_search_for_slot,
)


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


def _inventory_row(
    *,
    service_route_id: str,
    direction_id: int | None,
    route_id: str | None = None,
    route_ref: str = "E1",
    route_name: str = "Central Corridor",
    operator_name: str = "DATAMIND",
    route_job_service_route_id: str | None = None,
    route_job_direction_id: int | None = None,
    route_prod_service_route_id: str | None = None,
    route_prod_direction_id: int | None = None,
) -> dict:
    return {
        "service_route_id": service_route_id,
        "route_ref": route_ref,
        "route_name": route_name,
        "operator_name": operator_name,
        "direction_id": direction_id,
        "route_id": route_id,
        "phase3_progress_step": 0,
        "direction_approval_status": "pending",
        "geom_source": "unknown",
        "route_job_service_route_id": route_job_service_route_id,
        "route_job_direction_id": route_job_direction_id,
        "chosen_osm_relation_id": 123 if route_id else None,
        "route_prod_service_route_id": route_prod_service_route_id,
        "route_prod_direction_id": route_prod_direction_id,
        "has_route_prod": bool(route_prod_service_route_id and route_id),
    }


def _persisted_row(
    *,
    service_route_id: str,
    direction_id: int,
    bound_route_id: str | None = None,
    anchor_route_id: str | None = None,
    route_ref: str = "E1",
    route_name: str = "Central Corridor",
    operator_name: str = "DATAMIND",
    inverse_status: str = "unknown",
    search_status: str = "not_started",
    direction_ready: bool = False,
    blocker_codes: list[str] | None = None,
    blocker_messages: list[str] | None = None,
    top_candidate_route_id: str | None = None,
    top_candidate_scores: dict | None = None,
    proposal_payload: dict | None = None,
    proposal_source: str | None = None,
    proposal_evaluated_at: str | None = None,
    search_request_payload: dict | None = None,
    search_result_payload: dict | None = None,
    dispatched_route_ids: list[str] | None = None,
    materialized_route_ids: list[str] | None = None,
    search_started_at: str | None = None,
    search_finished_at: str | None = None,
    search_error: str | None = None,
    logical_route_id: str | None = None,
) -> dict:
    return {
        "service_route_id": service_route_id,
        "route_short_name": route_ref,
        "route_label": f"{route_ref} | {route_name}",
        "route_name": route_name,
        "operator_name": operator_name,
        "direction_id": direction_id,
        "logical_route_id": logical_route_id if logical_route_id is not None else bound_route_id,
        "bound_route_id": bound_route_id,
        "anchor_route_id": anchor_route_id,
        "top_candidate_route_id": top_candidate_route_id,
        "top_candidate_scores": dict(top_candidate_scores or {}),
        "proposal_payload": dict(proposal_payload or {}),
        "proposal_source": proposal_source,
        "proposal_evaluated_at": proposal_evaluated_at,
        "phase3_progress_step": 0,
        "direction_approval_status": "pending",
        "geom_source": "unknown",
        "inverse_status": inverse_status,
        "search_status": search_status,
        "search_request_payload": dict(search_request_payload or {}),
        "search_result_payload": dict(search_result_payload or {}),
        "dispatched_route_ids": list(dispatched_route_ids or []),
        "materialized_route_ids": list(materialized_route_ids or []),
        "search_started_at": search_started_at,
        "search_finished_at": search_finished_at,
        "search_error": search_error,
        "manual_required": False,
        "direction_ready": direction_ready,
        "blocker_codes": list(blocker_codes or []),
        "blocker_messages": list(blocker_messages or []),
        "evidence_summary": {},
        "analysis_version": "phase3_inverse_completion_readonly_v1",
        "last_evaluated_at": "2026-03-07T00:00:00+00:00",
        "created_at": "2026-03-07T00:00:00+00:00",
        "updated_at": "2026-03-07T00:00:00+00:00",
    }


def _persisted_dataclass(**kwargs) -> PersistedDirectionReadinessRow:
    payload = _persisted_row(**kwargs)
    return PersistedDirectionReadinessRow(**payload)


class InverseCompletionReadinessTests(unittest.TestCase):
    def _client(self) -> Phase3Client:
        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            client = Phase3Client()
        client._ensure_direction_schema = lambda: None
        client._ensure_sequence_resolution_schema = lambda: None
        client._ensure_inverse_completion_schema = lambda: None
        return client

    def test_adapter_classifies_reliable_opposite_candidate(self) -> None:
        service_route_id = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        candidate = str(uuid.uuid4())
        result = DirectionReadinessResult(
            service_route_id=service_route_id,
            route_id_0=route0,
            route_id_1=None,
            missing_direction_ids=[1],
            is_direction_ready=False,
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )

        class _FakeExtractor:
            def load_route_profile(self, route_id):
                return {"route_id": route_id}

            def extract_route_pair_evidence_from_profiles(self, profile_a, profile_b):
                return {"route_a_id": profile_a["route_id"], "route_b_id": profile_b["route_id"]}

        def _score(_evidence):
            return {
                "route_a_id": route0,
                "route_b_id": candidate,
                "same_route_family_score": 0.82,
                "opposite_direction_score": 0.79,
                "merge_readiness_score": 0.86,
                "review_flags": [],
                "gate_state": "paired",
                "model_version": "merge_assist_v1",
            }

        with patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.adapters.merge_proposal_adapter.RoutePairEvidenceExtractor",
            _FakeExtractor,
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.adapters.merge_proposal_adapter.score_route_pair_evidence",
            _score,
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.adapters.merge_proposal_adapter.list_inventory_bound_route_ids",
            lambda **_: [candidate],
        ):
            out = analyze_inverse_proposals_for_results([result])

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].proposal_status, "reliable_opposite_candidate")
        self.assertEqual(out[0].top_candidate_route_id, candidate)

    def test_adapter_classifies_plausible_opposite_candidate(self) -> None:
        service_route_id = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        candidate = str(uuid.uuid4())
        result = DirectionReadinessResult(
            service_route_id=service_route_id,
            route_id_0=route0,
            route_id_1=None,
            missing_direction_ids=[1],
            is_direction_ready=False,
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )

        class _FakeExtractor:
            def load_route_profile(self, route_id):
                return {"route_id": route_id}

            def extract_route_pair_evidence_from_profiles(self, profile_a, profile_b):
                return {"route_a_id": profile_a["route_id"], "route_b_id": profile_b["route_id"]}

        def _score(_evidence):
            return {
                "route_a_id": route0,
                "route_b_id": candidate,
                "same_route_family_score": 0.51,
                "opposite_direction_score": 0.53,
                "merge_readiness_score": 0.58,
                "review_flags": ["weak opposite-direction signal"],
                "gate_state": "operator_pending",
                "model_version": "merge_assist_v1",
            }

        with patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.adapters.merge_proposal_adapter.RoutePairEvidenceExtractor",
            _FakeExtractor,
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.adapters.merge_proposal_adapter.score_route_pair_evidence",
            _score,
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.adapters.merge_proposal_adapter.list_inventory_bound_route_ids",
            lambda **_: [candidate],
        ):
            out = analyze_inverse_proposals_for_results([result])

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].proposal_status, "plausible_opposite_candidate")
        self.assertEqual(out[0].top_candidate_route_id, candidate)

    def test_adapter_classifies_no_candidate_found(self) -> None:
        service_route_id = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        candidate = str(uuid.uuid4())
        result = DirectionReadinessResult(
            service_route_id=service_route_id,
            route_id_0=route0,
            route_id_1=None,
            missing_direction_ids=[1],
            is_direction_ready=False,
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )

        class _FakeExtractor:
            def load_route_profile(self, route_id):
                return {"route_id": route_id}

            def extract_route_pair_evidence_from_profiles(self, profile_a, profile_b):
                return {"route_a_id": profile_a["route_id"], "route_b_id": profile_b["route_id"]}

        def _score(_evidence):
            return {
                "route_a_id": route0,
                "route_b_id": candidate,
                "same_route_family_score": 0.22,
                "opposite_direction_score": 0.31,
                "merge_readiness_score": 0.33,
                "review_flags": ["weak opposite-direction signal"],
                "gate_state": "synthesis_candidate",
                "model_version": "merge_assist_v1",
            }

        with patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.adapters.merge_proposal_adapter.RoutePairEvidenceExtractor",
            _FakeExtractor,
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.adapters.merge_proposal_adapter.score_route_pair_evidence",
            _score,
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.adapters.merge_proposal_adapter.list_inventory_bound_route_ids",
            lambda **_: [candidate],
        ):
            out = analyze_inverse_proposals_for_results([result])

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].proposal_status, "no_candidate_found")
        self.assertIsNone(out[0].top_candidate_route_id)

    def test_list_direction_readiness_ready_with_both_slots_bound(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        route1 = str(uuid.uuid4())
        cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _inventory_row(
                        service_route_id=sid,
                        direction_id=0,
                        route_id=route0,
                        route_job_service_route_id=sid,
                        route_job_direction_id=0,
                        route_prod_service_route_id=sid,
                        route_prod_direction_id=0,
                    ),
                    _inventory_row(
                        service_route_id=sid,
                        direction_id=1,
                        route_id=route1,
                        route_job_service_route_id=sid,
                        route_job_direction_id=1,
                        route_prod_service_route_id=sid,
                        route_prod_direction_id=1,
                    ),
                ]
            ]
        )
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", fake_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", fake_db_cursor
        ):
            out = client.list_direction_readiness(limit=20)

        self.assertEqual(out["counts"]["service_routes_total"], 1)
        self.assertEqual(out["counts"]["direction_ready"], 1)
        self.assertEqual(len(out["results"]), 1)
        self.assertTrue(out["results"][0]["is_direction_ready"])
        self.assertEqual(out["results"][0]["blocker_codes"], [])

    def test_list_direction_readiness_flags_missing_direction_row(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _inventory_row(
                        service_route_id=sid,
                        direction_id=0,
                        route_id=route0,
                        route_job_service_route_id=sid,
                        route_job_direction_id=0,
                    )
                ]
            ]
        )
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", fake_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", fake_db_cursor
        ):
            out = client.list_direction_readiness(limit=20)

        result = out["results"][0]
        self.assertFalse(result["is_direction_ready"])
        self.assertEqual(result["missing_direction_ids"], [1])
        self.assertIn("one_direction_missing", result["blocker_codes"])
        self.assertIn("only_one_bound_route", result["blocker_codes"])

    def test_list_direction_readiness_flags_no_bound_routes(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _inventory_row(service_route_id=sid, direction_id=0, route_id=None),
                    _inventory_row(service_route_id=sid, direction_id=1, route_id=None),
                ]
            ]
        )
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", fake_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", fake_db_cursor
        ):
            out = client.list_direction_readiness(limit=20)

        result = out["results"][0]
        self.assertFalse(result["is_direction_ready"])
        self.assertIn("no_bound_routes", result["blocker_codes"])
        self.assertIn("incomplete_logical_route_binding", result["blocker_codes"])

    def test_list_direction_readiness_flags_legacy_context_mismatch(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        route1 = str(uuid.uuid4())
        cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _inventory_row(
                        service_route_id=sid,
                        direction_id=0,
                        route_id=route0,
                        route_job_service_route_id=str(uuid.uuid4()),
                        route_job_direction_id=1,
                    ),
                    _inventory_row(
                        service_route_id=sid,
                        direction_id=1,
                        route_id=route1,
                        route_job_service_route_id=sid,
                        route_job_direction_id=1,
                    ),
                ]
            ]
        )
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", fake_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", fake_db_cursor
        ):
            out = client.list_direction_readiness(limit=20)

        result = out["results"][0]
        self.assertFalse(result["is_direction_ready"])
        self.assertIn("legacy_direction_context_untrusted", result["blocker_codes"])

    def test_get_direction_readiness_blocks_without_service_route_context(self) -> None:
        client = self._client()
        route_id = str(uuid.uuid4())
        cursor = _FakeCursor(fetchone_rows=[{"route_id": route_id, "service_route_id": None, "direction_id": None}])
        fake_db_conn, fake_db_cursor = _fake_db_context(cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", fake_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", fake_db_cursor
        ):
            out = client.get_direction_readiness(route_id=route_id)

        self.assertFalse(out["is_direction_ready"])
        self.assertIn("service_route_context_missing", out["blocker_codes"])
        self.assertEqual(out["focus_route_id"], route_id)

    def test_refresh_direction_readiness_persists_structural_ready_rows(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        route1 = str(uuid.uuid4())
        inventory_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _inventory_row(
                        service_route_id=sid,
                        direction_id=0,
                        route_id=route0,
                        route_job_service_route_id=sid,
                        route_job_direction_id=0,
                        route_prod_service_route_id=sid,
                        route_prod_direction_id=0,
                    ),
                    _inventory_row(
                        service_route_id=sid,
                        direction_id=1,
                        route_id=route1,
                        route_job_service_route_id=sid,
                        route_job_direction_id=1,
                        route_prod_service_route_id=sid,
                        route_prod_direction_id=1,
                    ),
                ]
            ]
        )
        status_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=0,
                        bound_route_id=route0,
                        anchor_route_id=route0,
                        inverse_status="structurally_ready",
                        search_status="not_applicable",
                        direction_ready=True,
                    ),
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=1,
                        bound_route_id=route1,
                        anchor_route_id=route1,
                        inverse_status="structurally_ready",
                        search_status="not_applicable",
                        direction_ready=True,
                    ),
                ]
            ]
        )
        inv_db_conn, inv_db_cursor = _fake_db_context(inventory_cursor)
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", inv_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", inv_db_cursor
        ), patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            out = client.refresh_direction_readiness(service_route_id=sid)

        self.assertEqual(out["persisted_row_count"], 2)
        self.assertEqual(out["persisted"]["counts"]["ready_rows"], 2)
        self.assertTrue(all(row["direction_ready"] for row in out["persisted"]["results"]))
        insert_sql = [
            sql for sql, _params in list(status_cursor.executed or []) if "inverse_direction_status" in str(sql)
        ]
        self.assertGreaterEqual(len(insert_sql), 2)

    def test_refresh_direction_readiness_persists_one_direction_missing_as_blocked(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        blocker_codes = ["one_direction_missing", "only_one_bound_route"]
        inventory_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _inventory_row(
                        service_route_id=sid,
                        direction_id=0,
                        route_id=route0,
                        route_job_service_route_id=sid,
                        route_job_direction_id=0,
                    )
                ]
            ]
        )
        status_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=0,
                        bound_route_id=route0,
                        anchor_route_id=route0,
                        inverse_status="structurally_blocked",
                        search_status="not_started",
                        blocker_codes=blocker_codes,
                    ),
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=1,
                        bound_route_id=None,
                        anchor_route_id=route0,
                        inverse_status="structurally_blocked",
                        search_status="not_started",
                        blocker_codes=blocker_codes,
                    ),
                ]
            ]
        )
        inv_db_conn, inv_db_cursor = _fake_db_context(inventory_cursor)
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", inv_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", inv_db_cursor
        ), patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            out = client.refresh_direction_readiness(service_route_id=sid)

        self.assertEqual(out["persisted_row_count"], 2)
        self.assertEqual(out["persisted"]["counts"]["blocked_rows"], 2)
        self.assertIn("one_direction_missing", out["persisted"]["results"][0]["blocker_codes"])

    def test_refresh_direction_readiness_persists_no_bound_routes_as_blocked(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        blocker_codes = ["no_bound_routes", "incomplete_logical_route_binding"]
        inventory_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _inventory_row(service_route_id=sid, direction_id=0, route_id=None),
                    _inventory_row(service_route_id=sid, direction_id=1, route_id=None),
                ]
            ]
        )
        status_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=0,
                        bound_route_id=None,
                        anchor_route_id=None,
                        inverse_status="structurally_blocked",
                        blocker_codes=blocker_codes,
                    ),
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=1,
                        bound_route_id=None,
                        anchor_route_id=None,
                        inverse_status="structurally_blocked",
                        blocker_codes=blocker_codes,
                    ),
                ]
            ]
        )
        inv_db_conn, inv_db_cursor = _fake_db_context(inventory_cursor)
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", inv_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", inv_db_cursor
        ), patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            out = client.refresh_direction_readiness(service_route_id=sid)

        self.assertEqual(out["persisted"]["counts"]["blocked_rows"], 2)
        self.assertTrue(all(not row["direction_ready"] for row in out["persisted"]["results"]))
        self.assertIn("no_bound_routes", out["persisted"]["results"][0]["blocker_codes"])

    def test_list_persisted_direction_readiness_reads_view_rows(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        route1 = str(uuid.uuid4())
        status_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=0,
                        bound_route_id=route0,
                        anchor_route_id=route0,
                        inverse_status="structurally_ready",
                        search_status="not_applicable",
                        direction_ready=True,
                    ),
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=1,
                        bound_route_id=route1,
                        anchor_route_id=route1,
                        inverse_status="structurally_ready",
                        search_status="not_applicable",
                        direction_ready=True,
                    ),
                ]
            ]
        )
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            out = client.list_persisted_direction_readiness(service_route_id=sid)

        self.assertEqual(out["counts"]["rows_total"], 2)
        self.assertEqual(out["results"][0]["route_label"], "E1 | Central Corridor")
        self.assertEqual(out["results"][0]["inverse_status"], "structurally_ready")

    def test_refresh_direction_readiness_is_idempotent_for_same_snapshot(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        route1 = str(uuid.uuid4())
        inventory_rows = [
            _inventory_row(
                service_route_id=sid,
                direction_id=0,
                route_id=route0,
                route_job_service_route_id=sid,
                route_job_direction_id=0,
            ),
            _inventory_row(
                service_route_id=sid,
                direction_id=1,
                route_id=route1,
                route_job_service_route_id=sid,
                route_job_direction_id=1,
            ),
        ]
        persisted_rows = [
            _persisted_row(
                service_route_id=sid,
                direction_id=0,
                bound_route_id=route0,
                anchor_route_id=route0,
                inverse_status="structurally_ready",
                search_status="not_applicable",
                direction_ready=True,
            ),
            _persisted_row(
                service_route_id=sid,
                direction_id=1,
                bound_route_id=route1,
                anchor_route_id=route1,
                inverse_status="structurally_ready",
                search_status="not_applicable",
                direction_ready=True,
            ),
        ]
        inventory_cursor = _FakeCursor(fetchall_rows=[inventory_rows, inventory_rows])
        status_cursor = _FakeCursor(fetchall_rows=[persisted_rows, persisted_rows])
        inv_db_conn, inv_db_cursor = _fake_db_context(inventory_cursor)
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", inv_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", inv_db_cursor
        ), patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            first = client.refresh_direction_readiness(service_route_id=sid)
            second = client.refresh_direction_readiness(service_route_id=sid)

        self.assertEqual(first["persisted_row_count"], 2)
        self.assertEqual(second["persisted_row_count"], 2)
        self.assertEqual(first["persisted"]["counts"], second["persisted"]["counts"])
        self.assertEqual(first["persisted"]["results"][0]["inverse_status"], second["persisted"]["results"][0]["inverse_status"])

    def test_refresh_inverse_proposals_persists_reliable_candidate(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        candidate = str(uuid.uuid4())
        inventory_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _inventory_row(
                        service_route_id=sid,
                        direction_id=0,
                        route_id=route0,
                        route_job_service_route_id=sid,
                        route_job_direction_id=0,
                    )
                ]
            ]
        )
        status_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=0,
                        bound_route_id=route0,
                        anchor_route_id=route0,
                        inverse_status="structurally_blocked",
                    ),
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=1,
                        bound_route_id=None,
                        anchor_route_id=route0,
                        top_candidate_route_id=candidate,
                        top_candidate_scores={"merge_readiness_score": 0.88},
                        proposal_payload={"top_proposal": {"route_b_id": candidate}},
                        proposal_source="merge_assist_v1",
                        proposal_evaluated_at="2026-03-07T00:00:00+00:00",
                        inverse_status="reliable_opposite_candidate",
                        blocker_codes=["one_direction_missing"],
                    ),
                ]
            ]
        )
        proposal_rows = [
            InverseProposalSnapshot(
                service_route_id=sid,
                direction_id=1,
                anchor_route_id=route0,
                top_candidate_route_id=candidate,
                proposal_status="reliable_opposite_candidate",
                top_candidate_scores={"merge_readiness_score": 0.88},
                proposal_payload={"top_proposal": {"route_b_id": candidate}},
                proposal_source="merge_assist_v1",
                proposal_evaluated_at="2026-03-07T00:00:00+00:00",
                blocker_codes=["one_direction_missing"],
                blocker_messages=["missing direction 1"],
            )
        ]
        inv_db_conn, inv_db_cursor = _fake_db_context(inventory_cursor)
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", inv_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", inv_db_cursor
        ), patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.analyze_inverse_proposals_for_results",
            lambda *_args, **_kwargs: proposal_rows,
        ):
            out = client.refresh_inverse_proposals(service_route_id=sid)

        self.assertEqual(out["persisted_row_count"], 2)
        self.assertEqual(out["persisted"]["results"][1]["inverse_status"], "reliable_opposite_candidate")
        self.assertEqual(out["persisted"]["results"][1]["top_candidate_route_id"], candidate)

    def test_refresh_inverse_proposals_keeps_structurally_ready_rows(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        route1 = str(uuid.uuid4())
        inventory_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _inventory_row(
                        service_route_id=sid,
                        direction_id=0,
                        route_id=route0,
                        route_job_service_route_id=sid,
                        route_job_direction_id=0,
                    ),
                    _inventory_row(
                        service_route_id=sid,
                        direction_id=1,
                        route_id=route1,
                        route_job_service_route_id=sid,
                        route_job_direction_id=1,
                    ),
                ]
            ]
        )
        status_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=0,
                        bound_route_id=route0,
                        anchor_route_id=route0,
                        inverse_status="structurally_ready",
                        search_status="not_applicable",
                        direction_ready=True,
                    ),
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=1,
                        bound_route_id=route1,
                        anchor_route_id=route1,
                        inverse_status="structurally_ready",
                        search_status="not_applicable",
                        direction_ready=True,
                    ),
                ]
            ]
        )
        inv_db_conn, inv_db_cursor = _fake_db_context(inventory_cursor)
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", inv_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", inv_db_cursor
        ), patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.analyze_inverse_proposals_for_results",
            lambda *_args, **_kwargs: [],
        ):
            out = client.refresh_inverse_proposals(service_route_id=sid)

        self.assertTrue(all(row["inverse_status"] == "structurally_ready" for row in out["persisted"]["results"]))

    def test_refresh_inverse_proposals_is_idempotent(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        candidate = str(uuid.uuid4())
        inventory_rows = [
            _inventory_row(
                service_route_id=sid,
                direction_id=0,
                route_id=route0,
                route_job_service_route_id=sid,
                route_job_direction_id=0,
            )
        ]
        persisted_rows = [
            _persisted_row(
                service_route_id=sid,
                direction_id=0,
                bound_route_id=route0,
                anchor_route_id=route0,
                inverse_status="structurally_blocked",
            ),
            _persisted_row(
                service_route_id=sid,
                direction_id=1,
                bound_route_id=None,
                anchor_route_id=route0,
                top_candidate_route_id=candidate,
                top_candidate_scores={"merge_readiness_score": 0.61},
                proposal_payload={"top_proposal": {"route_b_id": candidate}},
                proposal_source="merge_assist_v1",
                proposal_evaluated_at="2026-03-07T00:00:00+00:00",
                inverse_status="plausible_opposite_candidate",
            ),
        ]
        proposal_rows = [
            InverseProposalSnapshot(
                service_route_id=sid,
                direction_id=1,
                anchor_route_id=route0,
                top_candidate_route_id=candidate,
                proposal_status="plausible_opposite_candidate",
                top_candidate_scores={"merge_readiness_score": 0.61},
                proposal_payload={"top_proposal": {"route_b_id": candidate}},
                proposal_source="merge_assist_v1",
                proposal_evaluated_at="2026-03-07T00:00:00+00:00",
            )
        ]
        inventory_cursor = _FakeCursor(fetchall_rows=[inventory_rows, inventory_rows])
        status_cursor = _FakeCursor(fetchall_rows=[persisted_rows, persisted_rows])
        inv_db_conn, inv_db_cursor = _fake_db_context(inventory_cursor)
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", inv_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", inv_db_cursor
        ), patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.analyze_inverse_proposals_for_results",
            lambda *_args, **_kwargs: proposal_rows,
        ):
            first = client.refresh_inverse_proposals(service_route_id=sid)
            second = client.refresh_inverse_proposals(service_route_id=sid)

        self.assertEqual(first["persisted"]["counts"], second["persisted"]["counts"])
        self.assertEqual(first["persisted"]["results"][1]["inverse_status"], second["persisted"]["results"][1]["inverse_status"])

    def test_refresh_inverse_proposals_persists_no_candidate_found(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        inventory_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _inventory_row(
                        service_route_id=sid,
                        direction_id=0,
                        route_id=route0,
                        route_job_service_route_id=sid,
                        route_job_direction_id=0,
                    )
                ]
            ]
        )
        status_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=0,
                        bound_route_id=route0,
                        anchor_route_id=route0,
                        inverse_status="structurally_blocked",
                    ),
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=1,
                        bound_route_id=None,
                        anchor_route_id=route0,
                        inverse_status="no_candidate_found",
                        proposal_payload={"reason": "no_candidate"},
                        proposal_source="merge_assist_v1",
                        proposal_evaluated_at="2026-03-07T00:00:00+00:00",
                    ),
                ]
            ]
        )
        proposal_rows = [
            InverseProposalSnapshot(
                service_route_id=sid,
                direction_id=1,
                anchor_route_id=route0,
                top_candidate_route_id=None,
                proposal_status="no_candidate_found",
                proposal_payload={"reason": "no_candidate"},
                proposal_source="merge_assist_v1",
                proposal_evaluated_at="2026-03-07T00:00:00+00:00",
            )
        ]
        inv_db_conn, inv_db_cursor = _fake_db_context(inventory_cursor)
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_conn", inv_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.inventory_repo.db_cursor", inv_db_cursor
        ), patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.analyze_inverse_proposals_for_results",
            lambda *_args, **_kwargs: proposal_rows,
        ):
            out = client.refresh_inverse_proposals(service_route_id=sid)

        self.assertEqual(out["persisted"]["results"][1]["inverse_status"], "no_candidate_found")

    def test_list_inverse_proposal_rows_keeps_stable_two_slot_shape(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        status_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=0,
                        logical_route_id=route0,
                        bound_route_id=route0,
                        anchor_route_id=route0,
                        inverse_status="structurally_blocked",
                    ),
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=1,
                        logical_route_id=None,
                        bound_route_id=None,
                        anchor_route_id=route0,
                        inverse_status="no_candidate_found",
                    ),
                ]
            ]
        )
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            out = client.list_inverse_proposal_rows(service_route_id=sid)

        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(sorted(row["direction_id"] for row in out["results"]), [0, 1])

    def test_build_targeted_inverse_search_request_uses_anchor_route_evidence(self) -> None:
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        structural = DirectionReadinessResult(
            service_route_id=sid,
            route_id_0=route0,
            route_id_1=None,
            present_direction_ids=[0],
            missing_direction_ids=[1],
            is_direction_ready=False,
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )
        persisted = _persisted_dataclass(
            service_route_id=sid,
            direction_id=1,
            anchor_route_id=route0,
            inverse_status="no_candidate_found",
        )

        class _FakePhase3Client:
            def get_route_job(self, route_id):
                self.last_route_id = route_id
                return {
                    "bbox": {"south": -1.0, "west": -2.0, "north": 1.0, "east": 2.0},
                    "known_ref": "E1",
                }

            def build_phase3_extract_bbox(self, *, bbox, group_hint=None, priority=None, extra_expand_pct=0.0):
                self.last_bbox_args = {
                    "bbox": dict(bbox or {}),
                    "group_hint": group_hint,
                    "priority": priority,
                    "extra_expand_pct": extra_expand_pct,
                }
                return dict(bbox or {})

        class _FakeExtractor:
            def load_route_profile(self, route_id):
                return {
                    "route_id": route_id,
                    "route_ref": "E1",
                    "relation_ref": "E1",
                    "operator_name": "DATAMIND",
                    "route_name": "North Terminal - South Terminal",
                    "relation_name": "North Terminal - South Terminal",
                    "relation_from": "North Terminal",
                    "relation_to": "South Terminal",
                    "prior_rows": [],
                    "geometry_points": [],
                }

        fake_client = _FakePhase3Client()
        with patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.dispatch.targeted_inverse_dispatch.RoutePairEvidenceExtractor",
            _FakeExtractor,
        ):
            request = build_targeted_inverse_search_request(
                phase3_client=fake_client,
                structural=structural,
                persisted_row=persisted,
                service_route_id=sid,
                direction_id=1,
            )

        self.assertEqual(request.anchor_route_id, route0)
        self.assertEqual(request.refs, ["E1"])
        self.assertEqual(request.operator, "DATAMIND")
        self.assertEqual(request.name, "South Terminal - North Terminal")
        self.assertEqual(request.target_group, "inverse_completion")
        self.assertEqual(request.target_seed_origin, "inverse_search")
        self.assertEqual(request.target_attempt_type, "targeted_inverse")

    def test_dispatch_targeted_inverse_search_materializes_without_auto_binding(self) -> None:
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        discovered = str(uuid.uuid4())
        structural = DirectionReadinessResult(
            service_route_id=sid,
            route_id_0=route0,
            route_id_1=None,
            present_direction_ids=[0],
            missing_direction_ids=[1],
            is_direction_ready=False,
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )
        persisted = _persisted_dataclass(
            service_route_id=sid,
            direction_id=1,
            anchor_route_id=route0,
            inverse_status="no_candidate_found",
            search_status="not_started",
        )
        captured = {}

        class _FakePhase3Client:
            def get_route_job(self, route_id):
                return {
                    "bbox": {"south": -1.0, "west": -2.0, "north": 1.0, "east": 2.0},
                    "known_ref": "E1",
                }

            def build_phase3_extract_bbox(self, *, bbox, group_hint=None, priority=None, extra_expand_pct=0.0):
                return dict(bbox or {})

            def run_step_05_discover(self, **kwargs):
                captured["step05"] = dict(kwargs)
                return {"route_id": discovered, "chosen_osm_relation_id": 12345}

            def run_step_10_fetch(self, **kwargs):
                captured["step10"] = dict(kwargs)
                return {"stored": True}

            def bind_route_to_direction(self, *args, **kwargs):
                raise AssertionError("targeted inverse search must not auto-bind routes")

        class _FakeExtractor:
            def load_route_profile(self, route_id):
                return {
                    "route_id": route_id,
                    "route_ref": "E1",
                    "relation_ref": "E1",
                    "operator_name": "DATAMIND",
                    "route_name": "North Terminal - South Terminal",
                    "relation_name": "North Terminal - South Terminal",
                    "relation_from": "North Terminal",
                    "relation_to": "South Terminal",
                    "prior_rows": [],
                    "geometry_points": [],
                }

        with patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.dispatch.targeted_inverse_dispatch.RoutePairEvidenceExtractor",
            _FakeExtractor,
        ):
            result = dispatch_targeted_inverse_search(
                phase3_client=_FakePhase3Client(),
                structural=structural,
                persisted_row=persisted,
                service_route_id=sid,
                direction_id=1,
            )

        self.assertTrue(result.eligible)
        self.assertTrue(result.launched)
        self.assertEqual(result.search_status, "materialized")
        self.assertEqual(result.dispatched_route_ids, [discovered])
        self.assertEqual(result.materialized_route_ids, [discovered])
        self.assertIsNone(captured["step05"].get("service_route_id"))
        self.assertIsNone(captured["step05"].get("direction_id"))
        self.assertIsNone(captured["step05"].get("route_id"))
        self.assertEqual(captured["step10"]["route_id"], discovered)
        self.assertEqual(captured["step10"]["osm_relation_id"], 12345)

    def test_dispatch_targeted_inverse_search_skips_structurally_ready_slot(self) -> None:
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        route1 = str(uuid.uuid4())
        structural = DirectionReadinessResult(
            service_route_id=sid,
            route_id_0=route0,
            route_id_1=route1,
            present_direction_ids=[0, 1],
            missing_direction_ids=[],
            is_direction_ready=True,
            blocker_codes=[],
            blocker_messages=[],
        )
        persisted = _persisted_dataclass(
            service_route_id=sid,
            direction_id=1,
            anchor_route_id=route1,
            bound_route_id=route1,
            inverse_status="structurally_ready",
            search_status="not_applicable",
            direction_ready=True,
        )

        class _FailIfCalledClient:
            def get_route_job(self, route_id):
                raise AssertionError("ready slots should not launch targeted search")

        result = dispatch_targeted_inverse_search(
            phase3_client=_FailIfCalledClient(),
            structural=structural,
            persisted_row=persisted,
            service_route_id=sid,
            direction_id=1,
        )

        self.assertFalse(result.eligible)
        self.assertFalse(result.launched)
        self.assertEqual(result.search_status, "not_applicable")

    def test_dispatch_targeted_inverse_search_for_slot_persists_materialized_result(self) -> None:
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        candidate = str(uuid.uuid4())
        discovered = str(uuid.uuid4())
        structural = DirectionReadinessResult(
            service_route_id=sid,
            route_id_0=route0,
            route_id_1=None,
            present_direction_ids=[0],
            missing_direction_ids=[1],
            is_direction_ready=False,
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )
        persisted_before = _persisted_dataclass(
            service_route_id=sid,
            direction_id=1,
            anchor_route_id=route0,
            top_candidate_route_id=candidate,
            top_candidate_scores={"merge_readiness_score": 0.71},
            proposal_payload={"top_proposal": {"route_b_id": candidate}},
            proposal_source="merge_assist_v1",
            proposal_evaluated_at="2026-03-07T00:00:00+00:00",
            inverse_status="plausible_opposite_candidate",
            search_status="not_started",
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )
        persisted_after = _persisted_dataclass(
            service_route_id=sid,
            direction_id=1,
            anchor_route_id=route0,
            top_candidate_route_id=candidate,
            top_candidate_scores={"merge_readiness_score": 0.71},
            proposal_payload={"top_proposal": {"route_b_id": candidate}},
            proposal_source="merge_assist_v1",
            proposal_evaluated_at="2026-03-07T00:00:00+00:00",
            inverse_status="plausible_opposite_candidate",
            search_status="materialized",
            search_request_payload={"anchor_route_id": route0},
            search_result_payload={"step05": {"route_id": discovered}},
            dispatched_route_ids=[discovered],
            materialized_route_ids=[discovered],
            search_started_at="2026-03-07T00:10:00+00:00",
            search_finished_at="2026-03-07T00:11:00+00:00",
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )
        captured = {}

        def _upsert(rows):
            captured["rows"] = list(rows)
            return len(rows)

        search_result = TargetedInverseSearchResult(
            service_route_id=sid,
            direction_id=1,
            eligible=True,
            launched=True,
            direction_ready=False,
            inverse_status="plausible_opposite_candidate",
            search_status="materialized",
            anchor_route_id=route0,
            request_payload={"anchor_route_id": route0},
            result_payload={"step05": {"route_id": discovered}},
            dispatched_route_ids=[discovered],
            materialized_route_ids=[discovered],
            search_started_at="2026-03-07T00:10:00+00:00",
            search_finished_at="2026-03-07T00:11:00+00:00",
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )

        with patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.get_direction_readiness",
            lambda **_kwargs: structural,
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.get_persisted_direction_readiness_row",
            side_effect=[persisted_before, persisted_after],
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.upsert_inverse_direction_status_rows",
            _upsert,
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.dispatch_targeted_inverse_search",
            lambda **_kwargs: search_result,
        ):
            out = dispatch_targeted_inverse_search_for_slot(
                service_route_id=sid,
                direction_id=1,
                phase3_client=object(),
            )

        self.assertEqual(out.search_status, "materialized")
        self.assertEqual(len(captured["rows"]), 1)
        snapshot = captured["rows"][0]
        self.assertEqual(snapshot.top_candidate_route_id, candidate)
        self.assertEqual(snapshot.inverse_status, "plausible_opposite_candidate")
        self.assertEqual(snapshot.search_status, "materialized")
        self.assertEqual(snapshot.dispatched_route_ids, [discovered])
        self.assertEqual(snapshot.materialized_route_ids, [discovered])
        self.assertIsNone(snapshot.bound_route_id)

    def test_dispatch_targeted_inverse_search_for_slot_persists_no_results(self) -> None:
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        structural = DirectionReadinessResult(
            service_route_id=sid,
            route_id_0=route0,
            route_id_1=None,
            present_direction_ids=[0],
            missing_direction_ids=[1],
            is_direction_ready=False,
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )
        persisted_before = _persisted_dataclass(
            service_route_id=sid,
            direction_id=1,
            anchor_route_id=route0,
            inverse_status="no_candidate_found",
            search_status="not_started",
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )
        persisted_after = _persisted_dataclass(
            service_route_id=sid,
            direction_id=1,
            anchor_route_id=route0,
            inverse_status="no_candidate_found",
            search_status="no_results",
            search_request_payload={"anchor_route_id": route0},
            search_error="No route relations found for that bbox/filters.",
            search_started_at="2026-03-07T00:10:00+00:00",
            search_finished_at="2026-03-07T00:10:30+00:00",
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )
        captured = {}

        def _upsert(rows):
            captured["rows"] = list(rows)
            return len(rows)

        search_result = TargetedInverseSearchResult(
            service_route_id=sid,
            direction_id=1,
            eligible=True,
            launched=False,
            direction_ready=False,
            inverse_status="no_candidate_found",
            search_status="no_results",
            anchor_route_id=route0,
            request_payload={"anchor_route_id": route0},
            result_payload={},
            dispatched_route_ids=[],
            materialized_route_ids=[],
            search_error="No route relations found for that bbox/filters.",
            search_started_at="2026-03-07T00:10:00+00:00",
            search_finished_at="2026-03-07T00:10:30+00:00",
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )

        with patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.get_direction_readiness",
            lambda **_kwargs: structural,
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.get_persisted_direction_readiness_row",
            side_effect=[persisted_before, persisted_after],
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.upsert_inverse_direction_status_rows",
            _upsert,
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.dispatch_targeted_inverse_search",
            lambda **_kwargs: search_result,
        ):
            out = dispatch_targeted_inverse_search_for_slot(
                service_route_id=sid,
                direction_id=1,
                phase3_client=object(),
            )

        self.assertEqual(out.search_status, "no_results")
        snapshot = captured["rows"][0]
        self.assertEqual(snapshot.inverse_status, "no_candidate_found")
        self.assertEqual(snapshot.search_status, "no_results")
        self.assertIsNone(snapshot.bound_route_id)
        self.assertEqual(snapshot.search_error, "No route relations found for that bbox/filters.")

    def test_dispatch_targeted_inverse_search_for_slot_persists_failed_result(self) -> None:
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        structural = DirectionReadinessResult(
            service_route_id=sid,
            route_id_0=route0,
            route_id_1=None,
            present_direction_ids=[0],
            missing_direction_ids=[1],
            is_direction_ready=False,
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )
        persisted_before = _persisted_dataclass(
            service_route_id=sid,
            direction_id=1,
            anchor_route_id=route0,
            inverse_status="structurally_blocked",
            search_status="not_started",
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )
        persisted_after = _persisted_dataclass(
            service_route_id=sid,
            direction_id=1,
            anchor_route_id=route0,
            inverse_status="structurally_blocked",
            search_status="failed",
            search_request_payload={"anchor_route_id": route0},
            search_result_payload={"step05": {"route_id": "candidate"}},
            dispatched_route_ids=["candidate"],
            search_error="step10 failed",
            search_started_at="2026-03-07T00:10:00+00:00",
            search_finished_at="2026-03-07T00:10:45+00:00",
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )
        captured = {}

        def _upsert(rows):
            captured["rows"] = list(rows)
            return len(rows)

        search_result = TargetedInverseSearchResult(
            service_route_id=sid,
            direction_id=1,
            eligible=True,
            launched=True,
            direction_ready=False,
            inverse_status="structurally_blocked",
            search_status="failed",
            anchor_route_id=route0,
            request_payload={"anchor_route_id": route0},
            result_payload={"step05": {"route_id": "candidate"}},
            dispatched_route_ids=["candidate"],
            materialized_route_ids=[],
            search_error="step10 failed",
            search_started_at="2026-03-07T00:10:00+00:00",
            search_finished_at="2026-03-07T00:10:45+00:00",
            blocker_codes=["one_direction_missing"],
            blocker_messages=["missing direction 1"],
        )

        with patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.get_direction_readiness",
            lambda **_kwargs: structural,
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.get_persisted_direction_readiness_row",
            side_effect=[persisted_before, persisted_after],
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.upsert_inverse_direction_status_rows",
            _upsert,
        ), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion.dispatch_targeted_inverse_search",
            lambda **_kwargs: search_result,
        ):
            out = dispatch_targeted_inverse_search_for_slot(
                service_route_id=sid,
                direction_id=1,
                phase3_client=object(),
            )

        self.assertEqual(out.search_status, "failed")
        snapshot = captured["rows"][0]
        self.assertEqual(snapshot.search_status, "failed")
        self.assertEqual(snapshot.search_error, "step10 failed")
        self.assertEqual(snapshot.dispatched_route_ids, ["candidate"])

    def test_list_targeted_inverse_search_results_keeps_stable_two_slot_shape(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        route1 = str(uuid.uuid4())
        status_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=0,
                        logical_route_id=route0,
                        bound_route_id=route0,
                        anchor_route_id=route0,
                        inverse_status="structurally_ready",
                        search_status="not_applicable",
                        direction_ready=True,
                    ),
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=1,
                        logical_route_id=None,
                        bound_route_id=None,
                        anchor_route_id=route0,
                        inverse_status="no_candidate_found",
                        search_status="no_results",
                        search_error="No route relations found for that bbox/filters.",
                    ),
                ]
            ]
        )
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            out = client.list_targeted_inverse_search_results(service_route_id=sid)

        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(sorted(row["direction_id"] for row in out["results"]), [0, 1])

    def test_list_inverse_completion_rows_combines_structural_proposal_and_search_state(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        candidate = str(uuid.uuid4())
        discovered = str(uuid.uuid4())
        status_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=0,
                        logical_route_id=route0,
                        bound_route_id=route0,
                        anchor_route_id=route0,
                        inverse_status="structurally_ready",
                        search_status="not_applicable",
                        direction_ready=True,
                    ),
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=1,
                        logical_route_id=None,
                        bound_route_id=None,
                        anchor_route_id=route0,
                        top_candidate_route_id=candidate,
                        top_candidate_scores={"merge_readiness_score": 0.76, "opposite_direction_score": 0.73},
                        proposal_payload={"top_proposal": {"route_b_id": candidate}},
                        proposal_source="merge_assist_v1",
                        proposal_evaluated_at="2026-03-07T00:00:00+00:00",
                        inverse_status="plausible_opposite_candidate",
                        search_status="materialized",
                        search_request_payload={"anchor_route_id": route0},
                        search_result_payload={"step05": {"route_id": discovered}},
                        dispatched_route_ids=[discovered],
                        materialized_route_ids=[discovered],
                        blocker_codes=["one_direction_missing"],
                        blocker_messages=["missing direction 1"],
                    ),
                ]
            ]
        )
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            out = client.list_inverse_completion_rows(service_route_id=sid)

        self.assertEqual(len(out["rows"]), 2)
        unresolved = next(row for row in out["rows"] if int(row["direction_id"]) == 1)
        self.assertEqual(unresolved["inverse_status"], "plausible_opposite_candidate")
        self.assertEqual(unresolved["proposal_strength"], "plausible")
        self.assertEqual(unresolved["search_status"], "materialized")
        self.assertEqual(unresolved["materialized_route_ids"], [discovered])
        self.assertTrue(str(unresolved["proposal_summary"]).startswith("Top candidate:"))

    def test_inverse_completion_summary_counts_unresolved_and_ready_rows(self) -> None:
        client = self._client()
        sid_a = str(uuid.uuid4())
        sid_b = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        route1 = str(uuid.uuid4())
        persisted_rows = [
            _persisted_row(
                service_route_id=sid_a,
                direction_id=0,
                bound_route_id=route0,
                anchor_route_id=route0,
                inverse_status="structurally_ready",
                search_status="not_applicable",
                direction_ready=True,
            ),
            _persisted_row(
                service_route_id=sid_a,
                direction_id=1,
                bound_route_id=route1,
                anchor_route_id=route1,
                inverse_status="structurally_ready",
                search_status="not_applicable",
                direction_ready=True,
            ),
            _persisted_row(
                service_route_id=sid_b,
                direction_id=0,
                bound_route_id=str(uuid.uuid4()),
                anchor_route_id=str(uuid.uuid4()),
                top_candidate_route_id=str(uuid.uuid4()),
                top_candidate_scores={"merge_readiness_score": 0.83},
                inverse_status="reliable_opposite_candidate",
                search_status="discovered",
                blocker_codes=["one_direction_missing"],
                blocker_messages=["missing direction 1"],
            ),
            _persisted_row(
                service_route_id=sid_b,
                direction_id=1,
                bound_route_id=None,
                anchor_route_id=str(uuid.uuid4()),
                inverse_status="no_candidate_found",
                search_status="no_results",
                blocker_codes=["one_direction_missing"],
                blocker_messages=["missing direction 1"],
            ),
        ]
        status_cursor = _FakeCursor(fetchall_rows=[persisted_rows, persisted_rows])
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            out = client.list_inverse_completion_rows(include_ready=True)
            summary = client.get_inverse_completion_summary(include_ready=True)

        self.assertEqual(out["summary"]["service_routes_total"], 2)
        self.assertEqual(out["summary"]["ready_rows"], 2)
        self.assertEqual(out["summary"]["unresolved_rows"], 2)
        self.assertEqual(out["summary"]["reliable_candidate_rows"], 1)
        self.assertEqual(out["summary"]["no_candidate_rows"], 1)
        self.assertEqual(summary["service_routes_total"], 2)
        self.assertEqual(summary["search_discovered_rows"], 1)
        self.assertEqual(summary["search_no_results_rows"], 1)

    def test_inverse_completion_rows_expose_manual_handoff_for_unresolved_slot(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        status_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=0,
                        bound_route_id=route0,
                        anchor_route_id=route0,
                        inverse_status="structurally_blocked",
                        search_status="not_started",
                        blocker_codes=["one_direction_missing"],
                        blocker_messages=["missing direction 1"],
                    ),
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=1,
                        bound_route_id=None,
                        anchor_route_id=route0,
                        inverse_status="no_candidate_found",
                        search_status="no_results",
                        blocker_codes=["one_direction_missing"],
                        blocker_messages=["missing direction 1"],
                    ),
                ]
            ]
        )
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            out = client.list_inverse_completion_rows(service_route_id=sid)

        unresolved = next(row for row in out["unresolved_rows"] if int(row["direction_id"]) == 1)
        self.assertTrue(unresolved["manual_handoff_recommended"])
        self.assertTrue(str(unresolved["manual_handoff_reason"] or "").strip())
        self.assertEqual(unresolved["next_action"], "Open manual builder.")

    def test_inverse_completion_rows_keep_ready_and_unresolved_lists_separate(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route0 = str(uuid.uuid4())
        status_cursor = _FakeCursor(
            fetchall_rows=[
                [
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=0,
                        bound_route_id=route0,
                        anchor_route_id=route0,
                        inverse_status="structurally_ready",
                        search_status="not_applicable",
                        direction_ready=True,
                    ),
                    _persisted_row(
                        service_route_id=sid,
                        direction_id=1,
                        bound_route_id=None,
                        anchor_route_id=route0,
                        inverse_status="no_candidate_found",
                        search_status="no_results",
                        blocker_codes=["one_direction_missing"],
                        blocker_messages=["missing direction 1"],
                    ),
                ]
            ]
        )
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            out = client.list_inverse_completion_rows(service_route_id=sid)

        self.assertEqual(len(out["ready_rows"]), 1)
        self.assertEqual(len(out["unresolved_rows"]), 1)
        self.assertTrue(out["ready_rows"][0]["direction_ready"])
        self.assertFalse(out["unresolved_rows"][0]["direction_ready"])

    def test_step20_direction_gate_blocks_when_direction_ready_is_false(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        status_cursor = _FakeCursor(
            fetchone_rows=[
                _persisted_row(
                    service_route_id=sid,
                    direction_id=1,
                    inverse_status="structurally_blocked",
                    search_status="not_started",
                    direction_ready=False,
                    blocker_codes=["one_direction_missing"],
                    blocker_messages=["missing direction 1"],
                )
            ]
        )
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            gate = client.get_step20_direction_gate(service_route_id=sid, direction_id=1)

        self.assertFalse(gate["gate_passed"])
        self.assertEqual(gate["gate_code"], "direction_not_ready")
        self.assertEqual(gate["blocker_codes"], ["one_direction_missing"])
        self.assertEqual(gate["blocker_messages"], ["missing direction 1"])

    def test_step20_direction_gate_blocks_reliable_candidate_when_not_ready(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        status_cursor = _FakeCursor(
            fetchone_rows=[
                _persisted_row(
                    service_route_id=sid,
                    direction_id=1,
                    inverse_status="reliable_opposite_candidate",
                    search_status="discovered",
                    direction_ready=False,
                    blocker_codes=["one_direction_missing"],
                    blocker_messages=["missing direction 1"],
                )
            ]
        )
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            gate = client.get_step20_direction_gate(service_route_id=sid, direction_id=1)

        self.assertFalse(gate["gate_passed"])
        self.assertEqual(gate["inverse_status"], "reliable_opposite_candidate")
        self.assertEqual(gate["suggested_next_action"], "review_reliable_candidate")

    def test_step20_direction_gate_passes_when_direction_ready_is_true(self) -> None:
        client = self._client()
        sid = str(uuid.uuid4())
        route1 = str(uuid.uuid4())
        status_cursor = _FakeCursor(
            fetchone_rows=[
                _persisted_row(
                    service_route_id=sid,
                    direction_id=1,
                    bound_route_id=route1,
                    anchor_route_id=route1,
                    inverse_status="structurally_ready",
                    search_status="not_applicable",
                    direction_ready=True,
                )
            ]
        )
        status_db_conn, status_db_cursor = _fake_db_context(status_cursor)
        with patch("pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_conn", status_db_conn), patch(
            "pipeline.phase_4_5.pair_detection.inverse_completion.src.db.status_repo.db_cursor", status_db_cursor
        ):
            gate = client.get_step20_direction_gate(service_route_id=sid, direction_id=1, route_id=route1)

        self.assertTrue(gate["gate_passed"])
        self.assertEqual(gate["gate_code"], "direction_ready")
        self.assertEqual(gate["suggested_next_action"], "proceed_step20")


if __name__ == "__main__":
    unittest.main()
