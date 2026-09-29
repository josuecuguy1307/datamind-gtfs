from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
import uuid
import subprocess
from pathlib import Path
from unittest.mock import patch

from datamind_console.phases.phase3_routes.client import (
    Phase3Client,
    _classify_discover_attempt_error,
    _phase3_overpass_urls,
    _classify_step20_blocker_origin,
    _extractor_review_context_count,
    _extractor_job_matches_source,
    _scope_extractor_job_to_source,
    _summarize_discover_candidates,
)


class Phase3ExtractorDiagnosticsTests(unittest.TestCase):
    def _client(self) -> Phase3Client:
        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            client = Phase3Client()
        client._ensure_direction_schema = lambda: None
        client._ensure_sequence_resolution_schema = lambda: None
        client._ensure_inverse_completion_schema = lambda: None
        return client

    @staticmethod
    def _overpass_module():
        import sys

        pkg_root = os.path.join(
            str(Path(__file__).resolve().parents[4]),
            "phase3_routes",
            "services",
            "route_constructor",
        )
        if pkg_root not in sys.path:
            sys.path.insert(0, pkg_root)
        return importlib.import_module("src.evidence.overpass")

    def test_summarize_discover_candidates_marks_low_signal(self) -> None:
        out = _summarize_discover_candidates(
            [{"osm_relation_id": 99, "score": 101.0, "stop_prior_count": 1}],
            chosen_relation_id=99,
        )
        self.assertEqual(out["signal_strength"], "low")
        self.assertIn("discover_top_candidate_weak_stop_prior_signal", out["quality_flags"])

    def test_classify_step20_origin_node_db_gap(self) -> None:
        out = _classify_step20_blocker_origin(
            prior_stop_count=24,
            unmatched_count=5,
            ambiguous_count=0,
            extractor_diagnostics={
                "quality_flags": [],
                "top_stop_prior_count": 12,
                "signal_strength": "high",
            },
        )
        self.assertEqual(out["dominant_cause"], "node_db_gap")
        self.assertEqual(out["confidence"], "high")

    def test_classify_step20_origin_matching_ambiguity(self) -> None:
        out = _classify_step20_blocker_origin(
            prior_stop_count=18,
            unmatched_count=0,
            ambiguous_count=3,
            extractor_diagnostics={"quality_flags": []},
        )
        self.assertEqual(out["dominant_cause"], "matching_ambiguity")

    def test_classify_step20_origin_extractor_config(self) -> None:
        out = _classify_step20_blocker_origin(
            prior_stop_count=2,
            unmatched_count=2,
            ambiguous_count=0,
            extractor_diagnostics={"quality_flags": ["discover_candidates_empty"]},
        )
        self.assertEqual(out["dominant_cause"], "extractor_config")

    def test_classify_step20_origin_defaults_to_node_db_gap_when_discover_missing(self) -> None:
        out = _classify_step20_blocker_origin(
            prior_stop_count=12,
            unmatched_count=4,
            ambiguous_count=0,
            extractor_diagnostics={"candidate_count": 0, "quality_flags": ["discover_candidates_empty"]},
        )
        self.assertEqual(out["dominant_cause"], "node_db_gap")

    def test_run_step20_emits_blocker_origin_hint(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        fake_rows = [
            {"seq": 1, "lat": -0.10, "lon": -78.40, "matched_stop_node_id": None},
            {"seq": 2, "lat": -0.11, "lon": -78.41, "matched_stop_node_id": None},
            {"seq": 3, "lat": -0.12, "lon": -78.42, "matched_stop_node_id": None},
        ]

        with patch.object(client, "get_relation_stop_prior", return_value=fake_rows), patch.object(
            client,
            "ensure_direction_ready_for_step20",
            return_value={"gate_passed": True, "gate_code": "direction_ready"},
        ), patch.object(
            client,
            "get_prior_match_report",
            return_value={
                "matched": 0,
                "unmatched": 3,
                "ambiguous": 0,
                "total": 3,
                "all_matched": False,
                "rows": fake_rows,
            },
        ), patch.object(
            client,
            "get_route_job",
            return_value={"route_id": str(route_id), "osm_relation_id": 12345},
        ), patch.object(
            client,
            "list_relation_candidates",
            return_value=[
                {"osm_relation_id": 12345, "score": 140.0, "stop_prior_count": 7},
                {"osm_relation_id": 88888, "score": 120.0, "stop_prior_count": 6},
            ],
        ), patch.object(
            client,
            "build_sequence_candidates_strict_relaxed",
            return_value=uuid.uuid4(),
        ), patch.object(
            client,
            "invalidate_route_resolution",
            return_value={},
        ), patch.object(
            client,
            "get_sequence_candidates",
            return_value={"route_id": str(route_id), "set_id": "set-1", "candidates": []},
        ), patch.object(
            client,
            "get_sequence_resolution_state",
            return_value={},
        ), patch.object(
            client,
            "mark_direction_progress_by_route",
            return_value={},
        ), patch.object(
            client,
            "_safe_ai_log_phase3",
            return_value=None,
        ):
            out = client.run_step_20_sequences(route_id=route_id, match_radius_m=3.0)

        self.assertEqual(out.get("blocker_origin_hint"), "node_db_gap")
        self.assertIn("extractor_diagnostics", out)
        self.assertIn("sequence_warning_subtypes", out)
        self.assertIn("step20_diagnostics_payload", out)
        self.assertTrue(str(out.get("sequence_diagnostic_profile_version") or "").strip())

    def test_run_step20_returns_direction_gate_payload_when_blocked(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()

        with patch.object(
            client,
            "ensure_direction_ready_for_step20",
            return_value={
                "gate_passed": False,
                "gate_code": "direction_not_ready",
                "service_route_id": str(uuid.uuid4()),
                "direction_id": 1,
                "route_id": str(route_id),
                "direction_ready": False,
                "inverse_status": "plausible_opposite_candidate",
                "search_status": "not_started",
                "blocker_codes": ["one_direction_missing"],
                "blocker_messages": ["missing direction 1"],
                "suggested_next_action": "run_targeted_inverse_search",
            },
        ), patch.object(
            client,
            "get_relation_stop_prior",
            side_effect=AssertionError("Step 20 should stop before stop-level work when direction gate fails."),
        ):
            out = client.run_step_20_sequences(route_id=route_id, match_radius_m=3.0)

        self.assertFalse(out.get("gate_passed"))
        self.assertTrue(out.get("step20_blocked"))
        self.assertEqual(out.get("gate_code"), "direction_not_ready")
        self.assertEqual(out.get("blocker_codes"), ["one_direction_missing"])

    def test_discover_attempt_error_classification(self) -> None:
        self.assertEqual(_classify_discover_attempt_error("HTTP 429 from upstream"), "rate_limited")
        self.assertEqual(_classify_discover_attempt_error("Gateway timeout 504"), "timeout")
        self.assertEqual(_classify_discover_attempt_error("Overpass returned non-JSON"), "non_json_response")

    def test_phase3_overpass_urls_prefers_client_url_and_keeps_fallbacks(self) -> None:
        urls = _phase3_overpass_urls("https://preferred.example/api/interpreter")
        self.assertEqual(urls[0], "https://preferred.example/api/interpreter")
        self.assertIn("https://overpass-api.de/api/interpreter", urls)
        self.assertIn("https://maps.mail.ru/osm/tools/overpass/api/interpreter", urls)
        self.assertIn("https://overpass.kumi.systems/api/interpreter", urls)

    def test_search_route_relations_bbox_first_broad_omits_metadata_filters(self) -> None:
        overpass_mod = self._overpass_module()

        class _Resp:
            status_code = 200
            text = '{"elements": [{"type": "relation", "id": 123, "tags": {"ref": "E1"}}]}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"elements": [{"type": "relation", "id": 123, "tags": {"ref": "E1"}}]}

        with patch.object(overpass_mod.requests, "post", return_value=_Resp()) as post:
            rows = overpass_mod.search_route_relations(
                (-0.3, -78.5, -0.2, -78.4),
                refs=["E1", "E2"],
                operator_contains="Metro",
                name_contains="Ecovia",
                query_strategy="bbox_first_broad",
                timeout_s=45,
                limit=50,
            )

        self.assertEqual(len(rows), 1)
        query = str(post.call_args.kwargs.get("data", b"").decode("utf-8"))
        self.assertIn('["type"="route"]', query)
        self.assertIn('["type"="route_master"]', query)
        self.assertIn('["route_master"~"bus|minibus|trolleybus|share_taxi",i]', query)
        self.assertNotIn('["operator"~', query)
        self.assertNotIn('["name"~', query)
        self.assertNotIn('["ref"~', query)

    def test_search_route_relations_metadata_filtered_applies_metadata_filters(self) -> None:
        overpass_mod = self._overpass_module()

        class _Resp:
            status_code = 200
            text = '{"elements": [{"type": "relation", "id": 123, "tags": {"ref": "E1"}}]}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"elements": [{"type": "relation", "id": 123, "tags": {"ref": "E1"}}]}

        with patch.object(overpass_mod.requests, "post", return_value=_Resp()) as post:
            rows = overpass_mod.search_route_relations(
                (-0.3, -78.5, -0.2, -78.4),
                refs=["E1", "E2"],
                operator_contains="Metro",
                name_contains="Ecovia",
                query_strategy="metadata_filtered",
                timeout_s=45,
                limit=50,
            )

        self.assertEqual(len(rows), 1)
        query = str(post.call_args.kwargs.get("data", b"").decode("utf-8"))
        self.assertIn('["route_master"~"bus|minibus|trolleybus|share_taxi",i]', query)
        self.assertIn('["operator"~"Metro",i]', query)
        self.assertIn('["name"~"Ecovia",i]', query)
        self.assertIn('["ref"~"(E1|E2)",i]', query)

    def test_run_step05_discover_retries_alternate_overpass_on_non_json(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        success_stdout = "\n".join(
            [
                f"route_id: {route_id}",
                "osm_relation_id: 12345",
                'top_candidate: {"osm_relation_id": 12345, "score": 17.0}',
            ]
        )
        with patch(
            "datamind_console.phases.phase3_routes.client._run_script",
            side_effect=[
                subprocess.CompletedProcess(
                    args=["05_discover_relation.py"],
                    returncode=1,
                    stdout="",
                    stderr="RuntimeError: Overpass returned non-JSON response. HTTP 200. Body: <?xml version='1.0'?>",
                ),
                subprocess.CompletedProcess(
                    args=["05_discover_relation.py"],
                    returncode=0,
                    stdout=success_stdout,
                    stderr="",
                ),
            ],
        ) as run_script, patch.object(
            client,
            "list_relation_candidates",
            return_value=[{"osm_relation_id": 12345, "score": 17.0, "stop_prior_count": 3, "is_chosen": True}],
        ), patch.object(
            client,
            "_safe_ai_log_phase3",
            return_value=None,
        ):
            out = client.run_step_05_discover(
                route_id=route_id,
                bbox=(-0.3, -78.5, -0.2, -78.4),
                refs=["E1"],
                max_candidates=10,
                timeout_s=60,
            )
        self.assertEqual(str(out.get("route_id") or ""), str(route_id))
        self.assertEqual(int(out.get("chosen_osm_relation_id") or 0), 12345)
        self.assertEqual(run_script.call_count, 2)


    def test_build_phase3_target_attempts_supports_cumbaya_tumbaco_catalog_schema(self) -> None:
        catalog = {
            "region": "Cumbaya / Tumbaco / corredor oriental de Quito",
            "priority_places": [
                {"name": "Cumbaya", "kind": "core_place", "confidence": "confirmed_in_user_research"},
                {"name": "Tumbaco Parque Central", "kind": "landmark_anchor", "confidence": "confirmed_in_user_research"},
            ],
            "critical_nodes": ["Terminal Rio Coca", "USFQ"],
            "cooperative_or_operator_hints": [
                {"name": "Cooperativa Flor del Valle", "confidence": "confirmed_in_user_research"},
            ],
            "route_hint_strings": ["Rio Coca - Pifo", "Cumbaya - Arenal"],
            "transport_corridors": [
                {"name": "Interoceanica", "confidence": "confirmed_in_user_research"},
            ],
            "relation_search_hints": [
                {"key": "ref", "value": "RIO COCA - PIFO"},
                {"key": "name", "value": "Cumbaya - Arenal"},
            ],
            "seed_extraction_combos": [
                {"place": "Terminal Rio Coca", "route_hint": "Rio Coca - Pifo"},
                {"place": "Cumbaya", "route_hint": "Cumbaya - Arenal"},
            ],
        }

        attempts = Phase3Client._build_phase3_target_attempts(catalog)

        self.assertGreaterEqual(len(attempts), 8)
        attempt_types = {str(row.get("attempt_type") or "") for row in attempts}
        self.assertIn("catalog_seed_combination", attempt_types)
        self.assertIn("catalog_route_bundle", attempt_types)
        self.assertIn("place_only", attempt_types)

        cumbaya_attempts = [row for row in attempts if str(row.get("place") or "").strip() == "Cumbaya"]
        self.assertTrue(any(dict(row.get("bbox_hint") or {}) for row in cumbaya_attempts))
        self.assertTrue(any(str(row.get("place_bundle") or "").strip() == "tumbaco_core_bundle" for row in cumbaya_attempts))
        self.assertTrue(any(str(row.get("route_hint") or "").strip() == "Cumbaya - Arenal" for row in cumbaya_attempts))

        rio_coca_attempts = [row for row in attempts if "Rio Coca" in str(row.get("place") or "")]
        self.assertTrue(any(str(row.get("group") or "").strip() == "Tumbaco-Cumbaya" for row in rio_coca_attempts))
        self.assertTrue(any(str(row.get("place_bundle") or "").strip() == "tumbaco_gateway_anchors" for row in rio_coca_attempts))

    def test_extractor_review_context_count_uses_attempt_history(self) -> None:
        rows = [
            {"extractor_review_ready": True, "attempt_history_count": 3},
            {"extractor_review_ready": True, "attempt_history_count": 0},
            {"extractor_review_ready": False, "attempt_history_count": 99},
        ]
        self.assertEqual(_extractor_review_context_count(rows), 4)

    def test_extractor_job_matches_source_when_attempt_history_mentions_source_document(self) -> None:
        job = {
            "extractor_source": "other_catalog.json",
            "extractor_review": {
                "source_document": "/tmp/other_catalog.json",
                "attempt_history": [
                    {"source_document": "/tmp/valle_de_los_chillos_phase3_catalog_codex_explicit.json"},
                ],
            },
        }
        self.assertTrue(
            _extractor_job_matches_source(job, "valle_de_los_chillos_phase3_catalog_codex_explicit.json")
        )

    def test_scope_extractor_job_to_source_filters_attempt_history(self) -> None:
        job = {
            "target_place": "Legacy Place",
            "route_hint": "legacy route",
            "cooperative_hint": "legacy coop",
            "attempt_history_count": 2,
            "selected_osm_relation_id": 123,
            "extractor_review": {
                "target": {"place": "Legacy Place"},
                "hints": {"route_hint_raw": "legacy route", "cooperative_hint": "legacy coop"},
                "geography": {"bbox_used": {"south": 0, "west": 0, "north": 1, "east": 1}},
                "dedupe": {"novelty_status": "duplicate_reused_existing_route"},
                "attempt_history": [
                    {"source_document": "/tmp/other.json", "place": "Other"},
                    {
                        "source_document": "/tmp/catalogo_cumbaya_tumbaco_phase3.json",
                        "place": "Cumbaya",
                        "group": "Tumbaco-Cumbaya",
                        "priority": "high",
                        "place_bundle": "tumbaco_core_bundle",
                        "seed_origin": "seed_extraction_combos",
                        "attempt_type": "catalog_seed_combination@expand_0",
                        "route_hint_raw": "Cumbaya - Arenal",
                        "cooperative_hint": "Flor del Valle",
                        "bbox_used": {"south": -0.24, "west": -78.48, "north": -0.16, "east": -78.38},
                        "novelty_status": "novel_alternative_selected",
                        "selection_confidence": 0.81,
                        "chosen_osm_relation_id": 456,
                        "fetch_relation_stored": True,
                    },
                ],
            },
        }

        scoped = _scope_extractor_job_to_source(job, "catalogo_cumbaya_tumbaco_phase3.json")

        self.assertEqual(scoped["target_place"], "Cumbaya")
        self.assertEqual(scoped["attempt_history_count"], 1)
        self.assertEqual(scoped["target_place_bundle"], "tumbaco_core_bundle")
        self.assertEqual(scoped["route_hint"], "Cumbaya - Arenal")
        self.assertEqual(scoped["cooperative_hint"], "Flor del Valle")
        self.assertEqual(scoped["selected_osm_relation_id"], 456)
        self.assertEqual(
            len(list((scoped.get("extractor_review") or {}).get("attempt_history") or [])),
            1,
        )

    def test_run_phase3_extractor_harvest_fetches_canonical_route_when_duplicate_reused(self) -> None:
        client = self._client()
        transient_route_id = uuid.uuid4()
        canonical_route_id = uuid.uuid4()
        target_doc = {
            "document_name": "phase3_test_catalog",
            "target_places": [{"name": "Conocoto", "group": "Valle de Los Chillos", "priority": "high"}],
        }

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(target_doc, fh)
            temp_path = fh.name

        try:
            with patch.object(
                client,
                "dedupe_extractor_review_jobs",
                side_effect=[
                    {"duplicate_relation_groups": 0, "merged_route_count": 0, "canonical_route_count": 0},
                    {"duplicate_relation_groups": 1, "merged_route_count": 1, "canonical_route_count": 1},
                ],
            ), patch.object(
                client,
                "list_extractor_review_jobs",
                side_effect=[
                    [],
                    [{"extractor_review_ready": True}],
                ],
            ), patch(
                "datamind_console.phases.phase3_routes.client.SharedGeographyResolver.resolve",
                return_value={
                    "bbox_candidate": {"south": -0.33, "west": -78.48, "north": -0.22, "east": -78.38},
                    "interpretation_source": "sector_catalog",
                    "interpretation_status": "ok",
                },
            ), patch.object(
                client,
                "create_route_job",
                return_value=transient_route_id,
            ), patch.object(
                client,
                "run_step_05_discover",
                return_value={
                    "route_id": str(canonical_route_id),
                    "chosen_osm_relation_id": 6258190,
                    "selection_summary": {"selection_confidence": 0.81},
                    "candidate_universe_summary": {"candidate_universe_count": 8, "top_stop_prior_count": 42},
                    "extractor_diagnostics": {"candidate_count": 8, "signal_strength": "high", "top_stop_prior_count": 42},
                    "novelty_status": "duplicate_reused_existing_route",
                    "reused_existing_route_id": str(canonical_route_id),
                    "existing_relation_usage_count": 3,
                },
            ), patch.object(
                client,
                "run_step_10_fetch",
                return_value={
                    "stored": True,
                    "fetch_relation_stored": True,
                    "fetch_status": "already_stored",
                },
            ) as run_fetch, patch.object(
                client,
                "_write_phase3_harvest_output",
                return_value="/tmp/phase3_extract_test.json",
            ):
                out = client.run_phase3_extractor_harvest(
                    targets_path=temp_path,
                    minimum_goal=1,
                    fetch_selected_relation=True,
                    allow_ai_assist=False,
                )
        finally:
            os.unlink(temp_path)

        self.assertEqual(str(run_fetch.call_args.kwargs.get("route_id") or ""), str(canonical_route_id))
        self.assertEqual(int(out.get("reused_existing_relation_count") or 0), 1)
        self.assertEqual(int(out.get("extracted_context_count") or 0), 1)

    def test_resolve_extractor_candidate_novelty_prefers_novel_alternative(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        annotated_rows = [
            {
                "osm_relation_id": 111,
                "score": 1447.0,
                "stop_prior_count": 42,
                "selection_rank": 1,
                "selection_confidence": 0.76,
                "existing_relation_usage_count": 3,
                "existing_relation_route_ids": ["a", "b", "c"],
                "matched_soft_signals": [],
                "is_chosen": True,
            },
            {
                "osm_relation_id": 222,
                "score": 1437.0,
                "stop_prior_count": 41,
                "selection_rank": 2,
                "selection_confidence": 0.55,
                "existing_relation_usage_count": 0,
                "existing_relation_route_ids": [],
                "matched_soft_signals": [],
                "is_chosen": False,
            },
        ]
        with patch.object(
            client,
            "_annotate_relation_candidate_novelty",
            return_value=annotated_rows,
        ), patch.object(client, "set_chosen_relation", return_value=None) as set_chosen:
            out = client._resolve_extractor_candidate_novelty(
                route_id=route_id,
                candidate_rows=annotated_rows,
                chosen_relation_id=111,
            )

        self.assertEqual(str(out.get("novelty_status") or ""), "novel_alternative_selected")
        self.assertEqual(int(out.get("chosen_relation_id") or 0), 222)
        self.assertEqual(int((out.get("selection_override") or {}).get("replacement_relation_id") or 0), 222)
        set_chosen.assert_called_once()

    def test_extractor_review_summary_surfaces_dedupe_counts(self) -> None:
        out = Phase3Client._extractor_review_summary(
            {
                "route_id": "route-1",
                "osm_relation_id": 999,
                "extractor_review": {
                    "target": {"place": "Pintag"},
                    "discover": {
                        "relation_extraction_success": True,
                        "selection_summary": {"selection_confidence": 0.81},
                    },
                    "dedupe": {
                        "novelty_status": "duplicate_reused_existing_route",
                        "reused_existing_route_id": "route-canonical",
                        "existing_relation_usage_count": 4,
                        "duplicate_attempt_count": 3,
                        "duplicate_attempts": [{"attempt_key": "a"}, {"attempt_key": "b"}, {"attempt_key": "c"}],
                    },
                    "attempt_history": [{"attempt_key": "root"}, {"attempt_key": "dup"}],
                },
            }
        )
        self.assertEqual(str(out.get("extractor_novelty_status") or ""), "duplicate_reused_existing_route")
        self.assertEqual(str(out.get("reused_existing_route_id") or ""), "route-canonical")
        self.assertEqual(int(out.get("existing_relation_usage_count") or 0), 4)
        self.assertEqual(int(out.get("duplicate_attempt_count") or 0), 3)
        self.assertEqual(int(out.get("attempt_history_count") or 0), 2)

    def test_extractor_review_summary_marks_suppressed_duplicate_status(self) -> None:
        out = Phase3Client._extractor_review_summary(
            {
                "route_id": "route-dup",
                "dedupe_group_id": "group-1",
                "membership_role": "duplicate",
                "membership_status": "suppressed",
                "review_status": "proposed",
                "group_status": "proposed",
                "dedupe_reviewable": True,
                "extractor_review": {
                    "target": {"place": "Conocoto"},
                    "discover": {"relation_extraction_success": True},
                    "dedupe": {
                        "canonical_route_id": "route-canonical",
                        "dedupe_membership_role": "duplicate",
                        "dedupe_membership_status": "suppressed",
                    },
                },
            }
        )

        self.assertEqual(str(out.get("canonical_route_id") or ""), "route-canonical")
        self.assertEqual(str(out.get("dedupe_group_id") or ""), "group-1")
        self.assertEqual(str(out.get("dedupe_membership_status") or ""), "suppressed")
        self.assertTrue(bool(out.get("is_suppressed_duplicate")))
        self.assertEqual(str(out.get("canonicalization_status") or ""), "suppressed_duplicate")

    def test_merge_duplicate_extractor_attempt_keeps_duplicate_route_job(self) -> None:
        client = self._client()
        canonical_route_id = uuid.uuid4()
        duplicate_route_id = uuid.uuid4()
        canonical_review = {
            "target": {"place": "Conocoto"},
            "discover": {
                "chosen_osm_relation_id": 6258190,
                "selection_summary": {"selection_confidence": 0.88},
            },
            "attempt_history": [{"attempt_key": "canonical-root"}],
            "dedupe": {},
        }
        duplicate_review = {
            "target": {"place": "Conocoto"},
            "discover": {
                "chosen_osm_relation_id": 6258190,
                "selection_summary": {"selection_confidence": 0.67},
            },
            "attempt_history": [{"attempt_key": "duplicate-root"}],
            "dedupe": {},
        }
        persisted: list[dict] = []

        def _capture_persist(**kwargs):
            persisted.append(dict(kwargs))
            return dict(kwargs.get("review_patch") or {})

        with patch.object(client, "_ensure_route_review_schema", return_value=None), patch.object(
            client,
            "get_route_job",
            side_effect=[
                {"route_id": str(canonical_route_id), "osm_relation_id": 6258190, "extractor_review": canonical_review},
                {"route_id": str(duplicate_route_id), "osm_relation_id": 6258190, "extractor_review": duplicate_review},
            ],
        ), patch.object(
            client,
            "_upsert_route_job_dedupe_cluster",
            return_value={
                "dedupe_group_id": "group-1",
                "canonical_route_id": str(canonical_route_id),
                "duplicate_route_id": str(duplicate_route_id),
            },
        ), patch.object(
            client,
            "_persist_route_job_extractor_review",
            side_effect=_capture_persist,
        ), patch.object(
            client,
            "delete_route_job",
            side_effect=AssertionError("destructive delete must not run"),
        ):
            out = client._merge_duplicate_extractor_attempt(
                canonical_route_id=canonical_route_id,
                duplicate_route_id=duplicate_route_id,
                duplicate_review=duplicate_review,
            )

        self.assertEqual(len(persisted), 2)
        self.assertEqual(str(out.get("dedupe", {}).get("dedupe_group_id") or ""), "group-1")
        self.assertFalse(bool(out.get("dedupe", {}).get("destructive_cleanup")))
        duplicate_patch = next(
            row for row in persisted if str(row.get("route_id") or "") == str(duplicate_route_id)
        )
        self.assertEqual(
            str(duplicate_patch.get("review_patch", {}).get("dedupe", {}).get("dedupe_membership_status") or ""),
            "suppressed",
        )
        self.assertEqual(
            str(duplicate_patch.get("review_patch", {}).get("dedupe", {}).get("canonical_route_id") or ""),
            str(canonical_route_id),
        )

    def test_run_step05_discover_passes_bbox_and_query_strategy_to_script(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        stdout = "\n".join(
            [
                f"route_id: {route_id}",
                "osm_relation_id: 12345",
                'top_candidate: {"osm_relation_id": 12345, "score": 17.0}',
            ]
        )
        with patch(
            "datamind_console.phases.phase3_routes.client._run_script",
            return_value=subprocess.CompletedProcess(
                args=["05_discover_relation.py"],
                returncode=0,
                stdout=stdout,
                stderr="",
            ),
        ) as run_script, patch.object(
            client,
            "list_relation_candidates",
            return_value=[{"osm_relation_id": 12345, "score": 17.0, "stop_prior_count": 3, "is_chosen": True}],
        ), patch.object(
            client,
            "_safe_ai_log_phase3",
            return_value=None,
        ):
            out = client.run_step_05_discover(
                route_id=route_id,
                bbox=(-0.3, -78.5, -0.2, -78.4),
                refs=["E1", "T1"],
                operator="Metro",
                name="Ecovia",
                max_candidates=50,
                timeout_s=60,
                query_strategy="bbox_first_broad",
            )
        self.assertEqual(str(out.get("route_id") or ""), str(route_id))
        cmd = list(run_script.call_args.args[0] or [])
        self.assertIn("--bbox=-0.3,-78.5,-0.2,-78.4", cmd)
        self.assertIn("--query-strategy", cmd)
        self.assertEqual(cmd[cmd.index("--query-strategy") + 1], "bbox_first_broad")
        self.assertIn("--refs", cmd)
        self.assertEqual(cmd[cmd.index("--refs") + 1], "E1,T1")
        self.assertIn("--operator", cmd)
        self.assertEqual(cmd[cmd.index("--operator") + 1], "Metro")
        self.assertIn("--name", cmd)
        self.assertEqual(cmd[cmd.index("--name") + 1], "Ecovia")

    def test_run_step05_discover_failure_lists_attempted_overpass_urls(self) -> None:
        client = self._client()
        with patch(
            "datamind_console.phases.phase3_routes.client._run_script",
            side_effect=[
                subprocess.CompletedProcess(
                    args=["05_discover_relation.py"],
                    returncode=1,
                    stdout="",
                    stderr="RuntimeError: Overpass returned non-JSON response. HTTP 200. Body: <?xml version='1.0'?>",
                ),
                subprocess.CompletedProcess(
                    args=["05_discover_relation.py"],
                    returncode=1,
                    stdout="",
                    stderr="Gateway timeout 504",
                ),
                subprocess.CompletedProcess(
                    args=["05_discover_relation.py"],
                    returncode=1,
                    stdout="",
                    stderr="Gateway timeout 504",
                ),
            ],
        ):
            with self.assertRaises(RuntimeError) as ctx:
                client.run_step_05_discover(
                    route_id=None,
                    bbox=(-0.3, -78.5, -0.2, -78.4),
                    max_candidates=10,
                    timeout_s=60,
                )
        msg = str(ctx.exception)
        self.assertIn("Attempted Overpass URLs:", msg)
        self.assertIn("https://overpass-api.de/api/interpreter", msg)
        self.assertIn("https://maps.mail.ru/osm/tools/overpass/api/interpreter", msg)

    def test_run_step05_discover_returns_candidate_universe_and_selection_summary(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        stdout = "\n".join(
            [
                f"route_id: {route_id}",
                "osm_relation_id: 12345",
                'top_candidate: {"osm_relation_id": 12345, "score": 17.0, "selection_confidence": 0.72}',
                "candidate_universe: "
                + json.dumps(
                    {
                        "candidate_universe_count": 12,
                        "candidate_scored_count": 7,
                        "candidate_fetch_evaluated_count": 4,
                        "query_strategy": "bbox_first_broad",
                        "hard_filters_applied": [],
                        "soft_signals_used": ["refs", "operator", "name"],
                    }
                ),
                "selection_summary: "
                + json.dumps(
                    {
                        "selection_status": "provisional_selected",
                        "selected_osm_relation_id": 12345,
                        "selected_rank": 1,
                        "selected_score": 17.0,
                        "selection_confidence": 0.72,
                        "score_gap_top2": 2.4,
                        "selected_relation_stop_prior_count": 5,
                        "selection_reason_codes": ["soft_ref_match", "soft_name_match"],
                    }
                ),
                "candidate_preview: "
                + json.dumps(
                    [
                        {
                            "osm_relation_id": 12345,
                            "selection_rank": 1,
                            "selection_confidence": 0.72,
                            "score": 17.0,
                            "stop_prior_count": 5,
                        }
                    ]
                ),
            ]
        )
        with patch(
            "datamind_console.phases.phase3_routes.client._run_script",
            return_value=subprocess.CompletedProcess(
                args=["05_discover_relation.py"],
                returncode=0,
                stdout=stdout,
                stderr="",
            ),
        ), patch.object(
            client,
            "list_relation_candidates",
            return_value=[
                {
                    "osm_relation_id": 12345,
                    "score": 17.0,
                    "stop_prior_count": 5,
                    "is_chosen": True,
                    "selection_rank": 1,
                    "selection_confidence": 0.72,
                    "matched_soft_signals": ["refs", "name"],
                    "selection_reason_codes": ["soft_ref_match", "soft_name_match"],
                    "hard_filters_applied": [],
                    "soft_signals_used": ["refs", "operator", "name"],
                    "query_strategy": "bbox_first_broad",
                    "ref": "E1",
                    "name": "Ecovia",
                    "operator": "Metro",
                    "route_mode": "bus",
                    "rel_type": "route",
                }
            ],
        ), patch.object(client, "_safe_ai_log_phase3", return_value=None):
            out = client.run_step_05_discover(
                route_id=route_id,
                bbox=(-0.3, -78.5, -0.2, -78.4),
                refs=["E1"],
                operator="Metro",
                name="Ecovia",
                max_candidates=50,
                timeout_s=60,
                query_strategy="bbox_first_broad",
            )

        self.assertEqual(str(out.get("query_strategy") or ""), "bbox_first_broad")
        universe = dict(out.get("candidate_universe_summary") or {})
        selection = dict(out.get("selection_summary") or {})
        self.assertEqual(int(universe.get("candidate_universe_count") or 0), 12)
        self.assertEqual(int(universe.get("candidate_scored_count") or 0), 7)
        self.assertEqual(float(selection.get("selection_confidence") or 0.0), 0.72)
        self.assertEqual(int(selection.get("selected_relation_stop_prior_count") or 0), 5)
        self.assertTrue(list(out.get("candidate_preview") or []))

    def test_run_step10_fetch_returns_selected_relation_summary(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        with patch(
            "datamind_console.phases.phase3_routes.client._run_script",
            return_value=subprocess.CompletedProcess(
                args=["10_fetch_relation.py"],
                returncode=0,
                stdout='P3_JSON: {"route_id":"%s","osm_relation_id":12345,"stored":true}' % route_id,
                stderr="",
            ),
        ), patch.object(
            client,
            "list_relation_candidates",
            return_value=[
                {
                    "osm_relation_id": 12345,
                    "score": 17.0,
                    "stop_prior_count": 5,
                    "is_chosen": True,
                    "selection_rank": 1,
                    "selection_confidence": 0.72,
                    "matched_soft_signals": ["refs", "name"],
                    "selection_reason_codes": ["soft_ref_match", "soft_name_match"],
                    "hard_filters_applied": [],
                    "soft_signals_used": ["refs", "operator", "name"],
                    "query_strategy": "bbox_first_broad",
                },
                {
                    "osm_relation_id": 99999,
                    "score": 11.0,
                    "stop_prior_count": 3,
                    "is_chosen": False,
                    "selection_rank": 2,
                    "selection_confidence": 0.41,
                },
            ],
        ), patch.object(client, "mark_direction_progress_by_route", return_value={}), patch.object(
            client,
            "_safe_ai_log_phase3",
            return_value=None,
        ):
            out = client.run_step_10_fetch(route_id=route_id, osm_relation_id=12345)

        self.assertEqual(int(out.get("candidate_universe_count") or 0), 2)
        selected = dict(out.get("selected_relation_summary") or {})
        self.assertEqual(int(selected.get("selection_rank") or 0), 1)
        self.assertEqual(float(selected.get("selection_confidence") or 0.0), 0.72)
        self.assertEqual(int(selected.get("stop_prior_count") or 0), 5)


if __name__ == "__main__":
    unittest.main()
