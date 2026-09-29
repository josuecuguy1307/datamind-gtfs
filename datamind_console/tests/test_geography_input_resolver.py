from __future__ import annotations

import unittest
from unittest.mock import patch

from datamind_console.common.geography_input_resolver import (
    SharedGeographyResolver,
    _build_geocoder_queries,
    build_geographic_text_variants,
    normalize_group_hint_key,
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

    def test_valle_corridor_group_normalizes_and_biases_geocoder_queries(self) -> None:
        self.assertEqual(normalize_group_hint_key("rumipamba_rural_corridor"), "valle de los chillos")
        queries = _build_geocoder_queries(
            "Rumipamba",
            supporting_hints={"phase3_target_group": "rumipamba_rural_corridor"},
        )
        self.assertTrue(queries)
        self.assertEqual(queries[0], "Rumipamba, Ruminahui, Sample Region, Ecuador")

    def test_quito_sur_group_normalizes_and_biases_geocoder_queries(self) -> None:
        self.assertEqual(normalize_group_hint_key("quito_sur_terminal_core"), "quito sur")
        queries = _build_geocoder_queries(
            "Chillogallo",
            supporting_hints={"phase3_target_group": "quito_sur_chillogallo_corridor"},
        )
        self.assertTrue(queries)
        self.assertEqual(queries[0], "Chillogallo, Quitumbe, Quito, Sample Region, Ecuador")


class GeographyPriorityTests(unittest.TestCase):
    """Ensure geography_priority_enforced and route hint flags are correct."""

    def test_geography_priority_enforced_always_true(self) -> None:
        resolver = SharedGeographyResolver()
        out = resolver.resolve(
            phase="phase3",
            place_input="Sangolqui Core",
            supporting_hints={"phase3_refs_hint": ["E1"]},
            allow_ai_assist=False,
        )
        self.assertTrue(out.get("geography_priority_enforced"))

    def test_route_hints_present_when_refs_given(self) -> None:
        resolver = SharedGeographyResolver()
        out = resolver.resolve(
            phase="phase3",
            place_input="Sangolqui Core",
            supporting_hints={"phase3_refs_hint": ["E1"]},
            allow_ai_assist=False,
        )
        self.assertTrue(out.get("route_hints_present"))

    def test_route_hints_not_present_when_no_hints(self) -> None:
        resolver = SharedGeographyResolver()
        out = resolver.resolve(
            phase="phase1",
            place_input="Sangolqui Core",
            supporting_hints={},
            allow_ai_assist=False,
        )
        self.assertFalse(out.get("route_hints_present"))

    def test_route_hints_do_not_override_place_bbox(self) -> None:
        resolver = SharedGeographyResolver()
        out_no_hints = resolver.resolve(
            phase="phase3",
            place_input="Sangolqui Core",
            supporting_hints={},
            allow_ai_assist=False,
        )
        out_with_hints = resolver.resolve(
            phase="phase3",
            place_input="Sangolqui Core",
            supporting_hints={"phase3_refs_hint": ["E1"], "operator": "Metro"},
            allow_ai_assist=False,
        )
        bbox_no = dict(out_no_hints.get("bbox_candidate") or {})
        bbox_with = dict(out_with_hints.get("bbox_candidate") or {})
        # Route hints must not change the bbox when place resolves deterministically
        if bbox_no and bbox_with:
            self.assertEqual(bbox_no, bbox_with)

    def test_explicit_bbox_takes_precedence_over_place(self) -> None:
        resolver = SharedGeographyResolver()
        explicit = {"south": -0.35, "west": -78.50, "north": -0.20, "east": -78.35}
        out = resolver.resolve(
            phase="phase1",
            place_input="Sangolqui Core",
            explicit_bbox=explicit,
            supporting_hints={},
            allow_ai_assist=False,
        )
        self.assertEqual(dict(out.get("bbox_candidate") or {}), explicit)
        self.assertEqual(str(out.get("geographic_interpretation_source") or ""), "explicit_bbox")

    def test_fallback_bbox_is_flagged_explicitly(self) -> None:
        resolver = SharedGeographyResolver()
        out = resolver.resolve(
            phase="phase1",
            place_input="nonexistent place xyzzy 12345",
            supporting_hints={},
            allow_ai_assist=False,
        )
        status = str(out.get("interpretation_status") or "")
        # Should NOT silently succeed — must be unresolved/ambiguous or show fallback
        if out.get("bbox_candidate"):
            self.assertTrue(out.get("fallback_used"), "bbox_candidate present but fallback_used not flagged")

    # --- new: route hint influence attribution fields ---

    def test_route_hint_influence_fields_present_in_output(self) -> None:
        resolver = SharedGeographyResolver()
        out = resolver.resolve(
            phase="phase3",
            place_input="Sangolqui Core",
            supporting_hints={"phase3_refs_hint": ["E1"]},
            allow_ai_assist=False,
        )
        self.assertIn("route_hints_used_as_secondary_signal", out)
        self.assertIn("route_hints_overconstrained_geography", out)
        self.assertIn("route_hint_effect_reason", out)

    def test_route_hints_not_used_as_secondary_when_catalog_resolves(self) -> None:
        """When place resolves deterministically, route hints are not used as secondary signal."""
        resolver = SharedGeographyResolver()
        out = resolver.resolve(
            phase="phase3",
            place_input="Sangolqui Core",
            supporting_hints={"phase3_refs_hint": ["E1"]},
            allow_ai_assist=False,
        )
        self.assertFalse(out.get("route_hints_used_as_secondary_signal"))
        self.assertFalse(out.get("route_hints_overconstrained_geography"))

    def test_ai_assisted_marks_route_hints_as_secondary_signal(self) -> None:
        class _StubAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                return {
                    "response": {
                        "interpreted_place_meaning": "Test zone",
                        "interpretation_source": "hades_geography_interpreter",
                        "interpretation_status": "ok",
                        "interpretation_confidence": 0.75,
                        "bbox_candidate": {"south": -0.3, "west": -78.5, "north": -0.2, "east": -78.4},
                    },
                    "meta": {"requested_mode": "real_advisory", "model": "gpt-5.2", "source": "openai"},
                }

        resolver = SharedGeographyResolver(
            advisory_mode="real_advisory",
            advisory_service=_StubAdvisoryService(),
        )
        out = resolver.resolve(
            phase="phase3",
            place_input="mystery place xyz",
            supporting_hints={"refs": ["E1"], "operator": "Metro"},
            allow_ai_assist=True,
        )
        self.assertTrue(out.get("route_hints_used_as_secondary_signal"))
        self.assertEqual(out.get("route_hint_effect_reason"), "ai_advisory_used_route_hints_as_context")

    def test_explicit_bbox_does_not_set_route_hint_influence(self) -> None:
        resolver = SharedGeographyResolver()
        out = resolver.resolve(
            phase="phase1",
            place_input=None,
            explicit_bbox={"south": -0.31, "west": -78.57, "north": -0.11, "east": -78.34},
            supporting_hints={"refs": ["E1"]},
            allow_ai_assist=False,
        )
        self.assertFalse(out.get("route_hints_influenced_bbox"))
        self.assertFalse(out.get("route_hints_used_as_secondary_signal"))


    # --- new: overconstraint detection in resolver ---

    def test_ai_assisted_low_confidence_sets_overconstrained(self) -> None:
        """AI-assisted path with route hints and low confidence should flag overconstrained."""
        class _StubAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                return {
                    "response": {
                        "interpreted_place_meaning": "Weak zone",
                        "interpretation_source": "hades_geography_interpreter",
                        "interpretation_status": "ok",
                        "interpretation_confidence": 0.45,
                        "bbox_candidate": {"south": -0.3, "west": -78.5, "north": -0.2, "east": -78.4},
                    },
                    "meta": {"requested_mode": "real_advisory", "model": "gpt-5.2", "source": "openai"},
                }

        resolver = SharedGeographyResolver(
            advisory_mode="real_advisory",
            advisory_service=_StubAdvisoryService(),
        )
        out = resolver.resolve(
            phase="phase3",
            place_input="mystery place xyz",
            supporting_hints={"refs": ["E1"], "operator": "Metro"},
            allow_ai_assist=True,
        )
        self.assertTrue(out.get("route_hints_overconstrained_geography"))
        self.assertEqual(
            out.get("route_hint_effect_reason"),
            "ai_advisory_route_hints_overconstrained_geography",
        )

    def test_ai_assisted_high_confidence_not_overconstrained(self) -> None:
        """AI-assisted path with route hints but high confidence is NOT overconstrained."""
        class _StubAdvisoryService:
            def run_task_detailed(self, *, endpoint_task, envelope):
                return {
                    "response": {
                        "interpreted_place_meaning": "Strong zone",
                        "interpretation_source": "hades_geography_interpreter",
                        "interpretation_status": "ok",
                        "interpretation_confidence": 0.82,
                        "bbox_candidate": {"south": -0.3, "west": -78.5, "north": -0.2, "east": -78.4},
                    },
                    "meta": {"requested_mode": "real_advisory", "model": "gpt-5.2", "source": "openai"},
                }

        resolver = SharedGeographyResolver(
            advisory_mode="real_advisory",
            advisory_service=_StubAdvisoryService(),
        )
        out = resolver.resolve(
            phase="phase3",
            place_input="mystery place xyz",
            supporting_hints={"refs": ["E1"], "operator": "Metro"},
            allow_ai_assist=True,
        )
        self.assertFalse(out.get("route_hints_overconstrained_geography"))
        self.assertEqual(
            out.get("route_hint_effect_reason"),
            "ai_advisory_used_route_hints_as_context",
        )

    def test_sector_match_with_route_tokens_marginal_score_is_overconstrained(self) -> None:
        """Sector match influenced by route tokens with score < 0.80 should flag overconstrained."""
        resolver = SharedGeographyResolver()
        with patch(
            "datamind_console.common.geography_input_resolver._bbox_catalog_matches",
            return_value=[],
        ), patch(
            "datamind_console.common.geography_input_resolver._sector_catalog_match",
            return_value={
                "kind": "sector_catalog",
                "score": 0.70,
                "label": "Marginal Zone",
                "description": "marginal",
                "bbox": {"south": -0.3, "west": -78.5, "north": -0.2, "east": -78.4},
                "area_group_hint": "marginal",
                "sector_hint": "Marginal",
                "corridor_hint": None,
            },
        ):
            out = resolver.resolve(
                phase="phase3",
                place_input="some place",
                supporting_hints={"refs": ["E1"], "operator": "Metro", "route_tokens": ["E1"]},
                allow_ai_assist=False,
            )
        self.assertTrue(out.get("route_hints_overconstrained_geography"))
        self.assertEqual(
            out.get("route_hint_effect_reason"),
            "route_tokens_overconstrained_sector_match",
        )

    def test_sector_match_with_route_tokens_high_score_not_overconstrained(self) -> None:
        """Sector match influenced by route tokens with high score should NOT flag overconstrained."""
        resolver = SharedGeographyResolver()
        with patch(
            "datamind_console.common.geography_input_resolver._bbox_catalog_matches",
            return_value=[],
        ), patch(
            "datamind_console.common.geography_input_resolver._sector_catalog_match",
            return_value={
                "kind": "sector_catalog",
                "score": 0.90,
                "label": "Strong Zone",
                "description": "strong",
                "bbox": {"south": -0.3, "west": -78.5, "north": -0.2, "east": -78.4},
                "area_group_hint": "strong",
                "sector_hint": "Strong",
                "corridor_hint": None,
            },
        ):
            out = resolver.resolve(
                phase="phase3",
                place_input="some place",
                supporting_hints={"refs": ["E1"], "operator": "Metro", "route_tokens": ["E1"]},
                allow_ai_assist=False,
            )
        self.assertFalse(out.get("route_hints_overconstrained_geography"))
        self.assertEqual(
            out.get("route_hint_effect_reason"),
            "route_tokens_influenced_sector_match",
        )


if __name__ == "__main__":
    unittest.main()
