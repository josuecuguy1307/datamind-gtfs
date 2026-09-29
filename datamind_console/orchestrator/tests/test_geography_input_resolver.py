from __future__ import annotations

import unittest
from unittest.mock import patch

from datamind_console.common.geography_input_resolver import (
    SharedGeographyResolver,
    build_geographic_text_variants,
)


class GeographyInputResolverTests(unittest.TestCase):
    def test_geographic_text_variants_expand_terminal_and_quito_forms(self) -> None:
        terminal_variants = build_geographic_text_variants("Terminal Rio Coca")
        historic_variants = build_geographic_text_variants("Centro Historico de Quito")
        self.assertIn("rio coca", [v.lower() for v in terminal_variants])
        self.assertIn("centro historico", [v.lower() for v in historic_variants])
        self.assertIn("quito centro", [v.lower() for v in historic_variants])

    def test_explicit_bbox_passthrough(self) -> None:
        resolver = SharedGeographyResolver()
        out = resolver.resolve(
            phase="phase1",
            place_input=None,
            explicit_bbox={"south": -0.31, "west": -78.57, "north": -0.11, "east": -78.34},
            allow_ai_assist=False,
        )
        self.assertEqual(str(out.get("interpretation_source") or ""), "explicit_bbox")
        self.assertEqual(str(out.get("interpretation_status") or ""), "ok")
        self.assertEqual(str(out.get("bbox_validation_status") or ""), "valid")
        self.assertEqual(
            dict(out.get("bbox_candidate") or {}),
            {"south": -0.31, "west": -78.57, "north": -0.11, "east": -78.34},
        )

    @unittest.skip("depends on the original region's bbox catalog; adapt it to your own region's catalogs (see README, Tests)")
    def test_catalog_resolution_returns_trusted_bbox(self) -> None:
        resolver = SharedGeographyResolver()
        out = resolver.resolve(
            phase="phase1",
            place_input="Sangolqui Core",
            allow_ai_assist=False,
        )
        self.assertEqual(str(out.get("interpretation_source") or ""), "bbox_catalog")
        self.assertEqual(str(out.get("interpretation_status") or ""), "ok")
        self.assertEqual(str(out.get("bbox_validation_status") or ""), "valid")
        self.assertEqual(
            dict(out.get("bbox_candidate") or {}),
            {"south": -0.36, "west": -78.48, "north": -0.28, "east": -78.42},
        )

    def test_invalid_explicit_bbox_text_is_visible(self) -> None:
        resolver = SharedGeographyResolver()
        out = resolver.resolve(
            phase="phase3",
            place_input="-0.2,-78.4,-0.3,-78.5",
            allow_ai_assist=False,
        )
        self.assertEqual(str(out.get("interpretation_status") or ""), "invalid_explicit_bbox")
        self.assertEqual(str(out.get("bbox_validation_status") or ""), "invalid")
        self.assertTrue(bool(out.get("fallback_used")))
        self.assertEqual(str(out.get("fallback_reason") or ""), "invalid_bbox_text")
        self.assertIsNone(out.get("bbox_candidate"))

    def test_ambiguous_deterministic_resolution_is_explicit(self) -> None:
        resolver = SharedGeographyResolver()
        with patch(
            "datamind_console.common.geography_input_resolver._bbox_catalog_matches",
            return_value=[
                {
                    "kind": "bbox_catalog",
                    "score": 0.9,
                    "bbox_id": "A",
                    "label": "Alpha",
                    "description": "alpha",
                    "bbox": {"south": -0.3, "west": -78.5, "north": -0.2, "east": -78.4},
                    "area_group_hint": None,
                    "sector_hint": None,
                    "corridor_hint": None,
                },
                {
                    "kind": "bbox_catalog",
                    "score": 0.9,
                    "bbox_id": "B",
                    "label": "Beta",
                    "description": "beta",
                    "bbox": {"south": -0.25, "west": -78.45, "north": -0.15, "east": -78.35},
                    "area_group_hint": None,
                    "sector_hint": None,
                    "corridor_hint": None,
                },
            ],
        ), patch(
            "datamind_console.common.geography_input_resolver._sector_catalog_match",
            return_value=None,
        ):
            out = resolver.resolve(
                phase="phase1",
                place_input="ambiguous zone",
                allow_ai_assist=False,
            )
        self.assertEqual(str(out.get("interpretation_status") or ""), "ambiguous")
        self.assertEqual(str(out.get("fallback_reason") or ""), "ambiguous_place_input")
        self.assertIsNone(out.get("bbox_candidate"))

    def test_geocoder_fallback_is_visible_when_catalogs_miss(self) -> None:
        resolver = SharedGeographyResolver()
        with patch(
            "datamind_console.common.geography_input_resolver._bbox_catalog_matches",
            return_value=[],
        ), patch(
            "datamind_console.common.geography_input_resolver._sector_catalog_match",
            return_value=None,
        ), patch(
            "datamind_console.common.geography_input_resolver._resolve_place_with_geocoder",
            return_value={
                "bbox": {"south": -0.25, "west": -78.51, "north": -0.21, "east": -78.47},
                "display_name": "La Marin, Quito, Sample Region, Ecuador",
                "query": "La Marin, Quito, Sample Region, Ecuador",
                "confidence": 0.73,
                "importance": 0.18,
            },
        ):
            out = resolver.resolve(
                phase="phase3",
                place_input="La Marin",
                supporting_hints={"phase3_target_group": "Quito Urbano"},
                allow_ai_assist=False,
            )
        self.assertEqual(str(out.get("interpretation_source") or ""), "nominatim_geocoder")
        self.assertEqual(str(out.get("interpretation_status") or ""), "ok")
        self.assertTrue(bool(out.get("fallback_used")))
        self.assertEqual(str(out.get("fallback_reason") or ""), "place_input_geocoded")
        self.assertEqual(
            dict(out.get("bbox_candidate") or {}),
            {"south": -0.25, "west": -78.51, "north": -0.21, "east": -78.47},
        )

    def test_ai_assisted_interpretation_is_visible_and_traceable(self) -> None:
        class _StubAdvisoryService:
            def __init__(self) -> None:
                self.calls = []

            def run_task_detailed(self, *, endpoint_task, envelope):
                self.calls.append({"task": endpoint_task, "envelope": dict(envelope or {})})
                return {
                    "response": {
                        "interpreted_place_meaning": "Conocoto terminal corridor",
                        "interpretation_source": "hades_geography_interpreter",
                        "interpretation_status": "ok",
                        "interpretation_confidence": 0.82,
                        "bbox_candidate": {
                            "south": -0.33,
                            "west": -78.48,
                            "north": -0.22,
                            "east": -78.38,
                        },
                        "area_group_hint": "conocoto_corridor",
                        "sector_hint": "Conocoto",
                        "corridor_hint": "Conocoto",
                        "fallback_reason": None,
                    },
                    "meta": {
                        "requested_mode": "real_advisory",
                        "model": "gpt-5.2",
                        "source": "openai",
                        "schema_name": "hades_geography_interpreter_response.json",
                        "fallback_used": False,
                    },
                }

        advisory = _StubAdvisoryService()
        resolver = SharedGeographyResolver(
            advisory_mode="real_advisory",
            advisory_service=advisory,
        )
        out = resolver.resolve(
            phase="phase3",
            place_input="misterio central",
            supporting_hints={"operator": "Metro", "refs": ["E1"]},
            allow_ai_assist=True,
        )
        self.assertEqual(str(out.get("interpretation_source") or ""), "hades_geography_interpreter")
        self.assertEqual(str(out.get("interpretation_status") or ""), "ok")
        self.assertEqual(float(out.get("bbox_candidate_confidence") or 0.0), 0.82)
        self.assertEqual(
            dict(out.get("bbox_candidate") or {}),
            {"south": -0.33, "west": -78.48, "north": -0.22, "east": -78.38},
        )
        trace = dict(out.get("advisory_trace") or {})
        self.assertEqual(str(trace.get("task") or ""), "hades_geography_interpreter")
        self.assertEqual(str(trace.get("configured_model_name") or ""), "gpt-5.2")
        self.assertEqual(str(trace.get("configured_provider_name") or ""), "openai")
        self.assertFalse(bool(trace.get("fallback_used")))
        self.assertTrue(advisory.calls)
