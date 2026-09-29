from __future__ import annotations

import json
import unittest
from pathlib import Path
from contextlib import contextmanager
from tempfile import TemporaryDirectory
import uuid
from unittest.mock import patch

from datamind_console.phases.phase3_routes.client import (
    Phase3Client,
    _normalized_phase3_extract_bbox,
)


class Phase3ExtractorCatalogTests(unittest.TestCase):
    @staticmethod
    def _fake_db_context(cursor):
        @contextmanager
        def _db_conn():
            yield object()

        @contextmanager
        def _db_cursor(_conn):
            yield cursor

        return _db_conn, _db_cursor

    @staticmethod
    def _catalog_path() -> Path:
        return (
            Path(__file__).resolve().parents[4]
            / "phase3_routes"
            / "catalogs"
            / "valle_de_los_chillos_phase3_catalog.json"
        )

    @staticmethod
    def _quito_sur_catalog_path() -> Path:
        return (
            Path(__file__).resolve().parents[4]
            / "phase3_routes"
            / "catalogs"
            / "quito_sur_phase3_catalog.json"
        )

    def _catalog_doc(self) -> dict:
        return json.loads(self._catalog_path().read_text(encoding="utf-8"))

    def test_valle_catalog_builds_broad_attempt_universe(self) -> None:
        doc = self._catalog_doc()
        attempts = Phase3Client._build_phase3_target_attempts(doc)

        self.assertGreaterEqual(len(attempts), 50)
        self.assertGreaterEqual(
            len(
                {
                    str(row.get("attempt_type") or "").strip()
                    for row in attempts
                    if str(row.get("attempt_type") or "").strip()
                }
            ),
            5,
        )
        self.assertTrue(any(str(row.get("place_bundle") or "").strip() for row in attempts))
        self.assertTrue(any(str(row.get("route_hint") or "").strip() for row in attempts))
        self.assertTrue(any(str(row.get("cooperative_hint") or "").strip() for row in attempts))
        self.assertTrue(
            any(isinstance(row.get("bbox_hint"), dict) and bool(row.get("bbox_hint")) for row in attempts)
        )

    def test_valle_catalog_includes_bundle_route_and_place_only_attempts(self) -> None:
        doc = self._catalog_doc()
        attempts = Phase3Client._build_phase3_target_attempts(doc)
        by_type = {}
        for row in attempts:
            by_type.setdefault(str(row.get("attempt_type") or "").strip(), []).append(row)

        self.assertIn("catalog_seed_combination", by_type)
        self.assertIn("catalog_place_bundle", by_type)
        self.assertIn("catalog_bundle_route", by_type)
        self.assertIn("catalog_bundle_operator", by_type)

        place_only = [
            row
            for row in by_type["catalog_place_bundle"]
            if not row.get("route_hint") and not row.get("cooperative_hint")
        ]
        self.assertTrue(place_only)

        route_bundle = [
            row
            for row in by_type["catalog_bundle_route"]
            if str(row.get("seed_origin") or "").strip() == "place_bundles"
            and str(row.get("place_bundle") or "").strip()
        ]
        self.assertTrue(route_bundle)

    def test_valle_catalog_keeps_secondary_hints_geographically_aligned(self) -> None:
        doc = self._catalog_doc()
        attempts = Phase3Client._build_phase3_target_attempts(doc)
        tambillo_rows = [row for row in attempts if str(row.get("place") or "").strip() == "Tambillo"]

        self.assertTrue(
            any(
                any(
                    token in str(row.get("route_hint") or "").strip()
                    for token in ("Tambillo", "Amaguana", "Conocoto")
                )
                for row in tambillo_rows
                if str(row.get("route_hint") or "").strip()
            )
        )
        self.assertTrue(
            any(
                str(row.get("cooperative_hint") or "").strip() == "San Pedro de Amaguana"
                for row in tambillo_rows
            )
        )
        self.assertFalse(
            any(str(row.get("route_hint") or "").strip() == "Pintag-La Marin" for row in tambillo_rows)
        )
        self.assertFalse(
            any(str(row.get("cooperative_hint") or "").strip() == "CALSIG Express" for row in tambillo_rows)
        )

    def test_corridor_bbox_expansion_is_broader_than_core_for_same_seed_bbox(self) -> None:
        seed_bbox = {"south": -0.33, "west": -78.48, "north": -0.22, "east": -78.38}
        corridor_bbox = _normalized_phase3_extract_bbox(
            seed_bbox,
            group_hint="amaguana_corridor",
            priority="high",
        )
        core_bbox = _normalized_phase3_extract_bbox(
            seed_bbox,
            group_hint="ruminahui_urbano_core",
            priority="high",
        )

        corridor_lat_span = float(corridor_bbox["north"]) - float(corridor_bbox["south"])
        corridor_lon_span = float(corridor_bbox["east"]) - float(corridor_bbox["west"])
        core_lat_span = float(core_bbox["north"]) - float(core_bbox["south"])
        core_lon_span = float(core_bbox["east"]) - float(core_bbox["west"])

        self.assertGreater(corridor_lat_span, core_lat_span)
        self.assertGreater(corridor_lon_span, core_lon_span)

    def test_harvest_uses_existing_reviewable_rows_to_reduce_remaining_goal(self) -> None:
        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            client = Phase3Client()

        ready_rows = [{"extractor_review_ready": True} for _ in range(55)]
        with patch.object(client, "dedupe_extractor_review_jobs", return_value={}), patch.object(
            client,
            "list_extractor_review_jobs",
            side_effect=[ready_rows, ready_rows],
        ), patch.object(
            client,
            "create_route_job",
            side_effect=AssertionError("create_route_job should not run when the goal is already met"),
        ), patch.object(
            client,
            "_write_phase3_harvest_output",
            return_value="/tmp/fake_phase3_harvest.json",
        ):
            out = client.run_phase3_extractor_harvest(
                targets_path=str(self._catalog_path()),
                minimum_goal=50,
                max_attempts=5,
                fetch_selected_relation=False,
                allow_ai_assist=False,
            )

        self.assertTrue(out["goal_met"])
        self.assertTrue(out["goal_already_met_before_run"])
        self.assertEqual(out["preexisting_reviewable_count"], 55)
        self.assertEqual(out["remaining_goal_at_start"], 0)
        self.assertEqual(out["reviewable_count_after_run"], 55)
        self.assertEqual(out["extracted_context_count"], 0)

    def test_quito_sur_catalog_builds_attempts_and_carries_bbox_hints(self) -> None:
        doc = json.loads(self._quito_sur_catalog_path().read_text(encoding="utf-8"))
        attempts = Phase3Client._build_phase3_target_attempts(doc)

        self.assertGreaterEqual(len(attempts), 50)
        quitumbe_rows = [row for row in attempts if str(row.get("place") or "").strip() == "Quitumbe"]
        self.assertTrue(quitumbe_rows)
        self.assertTrue(any(str(row.get("route_hint") or "").strip() == "Quitumbe - Chillogallo" for row in quitumbe_rows))
        self.assertTrue(
            any(str(row.get("cooperative_hint") or "").strip() == "Cooperativa Transur 7 de Mayo" for row in quitumbe_rows)
        )
        self.assertTrue(
            any(
                dict(row.get("bbox_hint") or {}) == {"south": -0.34, "west": -78.57, "north": -0.30, "east": -78.50}
                for row in quitumbe_rows
            )
        )

    def test_catalog_expected_routes_produce_sector_scoped_gap_candidates(self) -> None:
        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            client = Phase3Client()

        rows = client._collect_phase3_catalog_expected_routes(
            doc=self._catalog_doc(),
            source_catalog=str(self._catalog_path()),
        )

        self.assertTrue(rows)
        self.assertTrue(any(str(row.get("sector_key") or "").strip() == "pintag_corridor" for row in rows))
        self.assertTrue(any(str(row.get("route_family_hint") or "").strip() == "Pintag-La Marin" for row in rows))
        self.assertTrue(
            any(
                "Expreso Antisana" in list(row.get("operator_hints") or [])
                for row in rows
                if str(row.get("route_family_hint") or "").strip() == "Pintag-La Marin"
            )
        )

    def test_global_catalog_search_includes_prod_route_name_filter(self) -> None:
        class _FakeCursor:
            def __init__(self) -> None:
                self.executed = []

            def execute(self, sql, params=None) -> None:
                self.executed.append((sql, params))

            def fetchall(self):
                return []

        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            client = Phase3Client()
        cursor = _FakeCursor()
        fake_db_conn, fake_db_cursor = self._fake_db_context(cursor)

        with patch.object(client, "_ensure_route_review_schema", lambda: None), patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ):
            client.list_phase3_global_catalog(route_family_search="marin", limit=25)

        sql = str(cursor.executed[0][0])
        self.assertIn("COALESCE(prod_route_name, '') ILIKE %s", sql)

    def test_get_phase3_coverage_gap_casts_view_gap_id_back_to_uuid(self) -> None:
        class _FakeCursor:
            def __init__(self) -> None:
                self.executed = []

            def execute(self, sql, params=None) -> None:
                self.executed.append((sql, params))

            def fetchone(self):
                return {}

        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            client = Phase3Client()
        cursor = _FakeCursor()
        fake_db_conn, fake_db_cursor = self._fake_db_context(cursor)

        with patch.object(client, "_ensure_route_review_schema", lambda: None), patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ):
            client.get_phase3_coverage_gap("6470b3f1-030d-48bb-bcbc-8b39772fd52b")

        sql = str(cursor.executed[0][0])
        self.assertIn("WHERE gap_id::uuid = %s::uuid", sql)

    def test_global_catalog_can_filter_by_route_job_ids(self) -> None:
        class _FakeCursor:
            def __init__(self) -> None:
                self.executed = []

            def execute(self, sql, params=None) -> None:
                self.executed.append((sql, params))

            def fetchall(self):
                return []

        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            client = Phase3Client()
        cursor = _FakeCursor()
        fake_db_conn, fake_db_cursor = self._fake_db_context(cursor)
        route_job_id = "6470b3f1-030d-48bb-bcbc-8b39772fd52b"

        with patch.object(client, "_ensure_route_review_schema", lambda: None), patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ):
            client.list_phase3_global_catalog(route_job_ids=[route_job_id], limit=25)

        sql = str(cursor.executed[0][0])
        params = cursor.executed[0][1]
        self.assertIn("route_job_id::uuid = ANY(%s::uuid[])", sql)
        self.assertIn(route_job_id, list(params[0]))

    def test_get_phase3_gap_manual_context_prefers_related_route_ids_and_dedupes_stop_candidates(self) -> None:
        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            client = Phase3Client()

        gap_id = str(uuid.uuid4())
        route_a = str(uuid.uuid4())
        route_b = str(uuid.uuid4())
        stop_a = str(uuid.uuid4())
        stop_b = str(uuid.uuid4())

        with patch.object(
            client,
            "get_phase3_coverage_gap",
            return_value={
                "gap_id": gap_id,
                "sector_key": "pintag_corridor",
                "route_family_hint": "Pintag-La Marin",
                "start_hint": "Pintag",
                "end_hint": "La Marin",
                "heuristic_notes": {"operator_hints": ["General Pintag"]},
                "related_route_ids": [route_a, route_b],
            },
        ), patch.object(
            client,
            "list_manual_builder_approved_stops",
            side_effect=[
                [{"stop_id": stop_a, "name": "Parque de Pintag"}],
                [
                    {"stop_id": stop_a, "name": "Parque de Pintag"},
                    {"stop_id": stop_b, "name": "La Marin"},
                ],
            ],
        ), patch.object(
            client,
            "list_phase3_global_catalog",
            return_value=[{"route_job_id": route_a}, {"route_job_id": route_b}],
        ) as list_catalog:
            out = client.get_phase3_gap_manual_context(gap_id)

        self.assertEqual(out["name_hint"], "Pintag-La Marin")
        self.assertEqual(out["operator_hint"], "General Pintag")
        self.assertEqual(out["recommended_stop_ids"], [stop_a, stop_b])
        self.assertEqual(
            list_catalog.call_args.kwargs.get("route_job_ids"),
            [route_a, route_b],
        )

    def test_sync_phase3_coverage_gaps_marks_prod_match_resolved(self) -> None:
        class _FakeCursor:
            def __init__(self) -> None:
                self.executed = []

            def execute(self, sql, params=None) -> None:
                self.executed.append((sql, params))

            def fetchone(self):
                return {"gap_id": str(uuid.uuid4())}

        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            client = Phase3Client()

        cursor = _FakeCursor()
        fake_db_conn, fake_db_cursor = self._fake_db_context(cursor)
        observed_route_id = str(uuid.uuid4())
        synced_gap_id = str(uuid.uuid4())

        with patch.object(client, "_ensure_route_review_schema", lambda: None), patch.object(
            client,
            "list_phase3_catalog_documents",
            return_value=[{"path": "/tmp/catalog.json"}],
        ), patch.object(
            client,
            "_load_phase3_catalog_document",
            return_value={"catalog_id": "valle_de_los_chillos_phase3_catalog_v1"},
        ), patch.object(
            client,
            "_collect_phase3_catalog_expected_routes",
            return_value=[
                {
                    "dedupe_key": "catalog|pintag_corridor|pintag-la-marin|-",
                    "sector_key": "pintag_corridor",
                    "sector_label": "Pintag Corridor",
                    "route_family_hint": "Pintag-La Marin",
                    "known_aliases": ["Pintag-La Marin"],
                    "start_hint": "Pintag",
                    "end_hint": "La Marin",
                    "direction_hint": None,
                    "operator_hints": ["Expreso Antisana"],
                    "place_hints": ["Pintag", "La Marin"],
                    "evidence_sources": ["catalog"],
                }
            ],
        ), patch.object(
            client,
            "list_phase3_global_catalog",
            return_value=[
                {
                    "route_job_id": observed_route_id,
                    "route_family_label": "Pintag-La Marin",
                    "service_route_name": "Pintag-La Marin",
                    "route_hint": "Pintag-La Marin",
                    "target_place": "La Marin",
                    "known_ref": "Pintag-La Marin",
                    "service_route_ref": "Pintag-La Marin",
                    "sector_key": "pintag_corridor",
                    "sector_label": "Pintag Corridor",
                    "target_group": "pintag_corridor",
                    "target_place_bundle": "pintag_corridor",
                    "area_key": "pintag_corridor",
                    "prod_status": "in_prod",
                    "approval_status": "prod",
                    "step05_state": "complete",
                    "geometry_status": "prod",
                    "manual_origin": True,
                }
            ],
        ), patch.object(
            client,
            "get_phase3_coverage_gap",
            return_value={"gap_id": synced_gap_id, "resolution_status": "resolved"},
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ):
            out = client.sync_phase3_coverage_gaps()

        self.assertEqual(out["resolved_gap_count"], 1)
        self.assertEqual(out["open_gap_count"], 0)
        sql, params = cursor.executed[0]
        self.assertIn("INSERT INTO route_review.coverage_gaps", str(sql))
        self.assertEqual(params[11], "still_extractable")
        self.assertEqual(params[14], "already_resolved")
        self.assertEqual(params[16], "resolved")
        self.assertEqual(params[17], observed_route_id)
        self.assertEqual(params[18], observed_route_id)

    def test_export_phase3_missing_route_catalogs_skips_resolved_by_default(self) -> None:
        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            client = Phase3Client()

        with TemporaryDirectory() as tmpdir, patch.object(
            client,
            "_ensure_route_review_schema",
            lambda: None,
        ), patch.object(
            client,
            "list_phase3_coverage_gaps",
            return_value=[
                {
                    "gap_id": "gap-open",
                    "sector_key": "pintag_corridor",
                    "sector_label": "Pintag Corridor",
                    "route_family_hint": "Pintag-La Marin",
                    "known_aliases": ["Pintag-La Marin"],
                    "start_hint": "Pintag",
                    "end_hint": "La Marin",
                    "direction_hint": None,
                    "effective_classification": "still_extractable",
                    "classification_status": "still_extractable",
                    "classification_confidence": 0.74,
                    "evidence_summary": {"observed_route_count": 1},
                    "related_route_ids": [str(uuid.uuid4())],
                    "recommended_next_action": "retry_extraction_or_patch_matching",
                    "heuristic_notes": {"operator_hints": ["Expreso Antisana"]},
                    "resolution_status": "open",
                    "resolved_route_id": None,
                    "resolved_prod_route_id": None,
                },
                {
                    "gap_id": "gap-resolved",
                    "sector_key": "pintag_corridor",
                    "sector_label": "Pintag Corridor",
                    "route_family_hint": "Pintag-La Marin",
                    "known_aliases": ["Pintag-La Marin"],
                    "start_hint": "Pintag",
                    "end_hint": "La Marin",
                    "direction_hint": None,
                    "effective_classification": "still_extractable",
                    "classification_status": "still_extractable",
                    "classification_confidence": 0.98,
                    "evidence_summary": {"observed_prod_count": 1},
                    "related_route_ids": [str(uuid.uuid4())],
                    "recommended_next_action": "already_resolved",
                    "heuristic_notes": {},
                    "resolution_status": "resolved",
                    "resolved_route_id": str(uuid.uuid4()),
                    "resolved_prod_route_id": str(uuid.uuid4()),
                },
            ],
        ):
            out = client.export_phase3_missing_route_catalogs(output_dir=tmpdir)

            self.assertEqual(out["file_count"], 1)
            payload = json.loads((Path(tmpdir) / "pintag_corridor_missing_routes.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["gap_count"], 1)
            self.assertEqual(payload["items"][0]["gap_id"], "gap-open")

            out_with_resolved = client.export_phase3_missing_route_catalogs(
                output_dir=tmpdir,
                include_resolved=True,
            )

            self.assertEqual(out_with_resolved["file_count"], 1)
            payload_all = json.loads((Path(tmpdir) / "pintag_corridor_missing_routes.json").read_text(encoding="utf-8"))
            self.assertEqual(payload_all["gap_count"], 2)

    def test_sync_phase3_coverage_gaps_sql_preserves_dismissed_operator_state(self) -> None:
        class _FakeCursor:
            def __init__(self) -> None:
                self.executed = []

            def execute(self, sql, params=None) -> None:
                self.executed.append((sql, params))

            def fetchone(self):
                return {"gap_id": str(uuid.uuid4())}

        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            client = Phase3Client()

        cursor = _FakeCursor()
        fake_db_conn, fake_db_cursor = self._fake_db_context(cursor)

        with patch.object(client, "_ensure_route_review_schema", lambda: None), patch.object(
            client,
            "list_phase3_catalog_documents",
            return_value=[{"path": "/tmp/catalog.json"}],
        ), patch.object(
            client,
            "_load_phase3_catalog_document",
            return_value={"catalog_id": "valle_de_los_chillos_phase3_catalog_v1"},
        ), patch.object(
            client,
            "_collect_phase3_catalog_expected_routes",
            return_value=[
                {
                    "dedupe_key": "catalog|pintag_corridor|pintag-la-marin|-",
                    "sector_key": "pintag_corridor",
                    "sector_label": "Pintag Corridor",
                    "route_family_hint": "Pintag-La Marin",
                    "known_aliases": ["Pintag-La Marin"],
                    "start_hint": "Pintag",
                    "end_hint": "La Marin",
                    "direction_hint": None,
                    "operator_hints": [],
                    "place_hints": ["Pintag", "La Marin"],
                    "evidence_sources": ["catalog"],
                }
            ],
        ), patch.object(
            client,
            "list_phase3_global_catalog",
            return_value=[],
        ), patch.object(
            client,
            "get_phase3_coverage_gap",
            return_value={"gap_id": str(uuid.uuid4()), "resolution_status": "open"},
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ):
            client.sync_phase3_coverage_gaps()

        sql = str(cursor.executed[0][0])
        self.assertIn("route_review.coverage_gaps.resolution_status = 'dismissed'", sql)
