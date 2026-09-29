from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datamind_console.orchestrator.pipeline_autopilot import (
    GEOGRAPHY_QUALITY_THRESHOLDS,
    _classify_geography_quality_attribution,
)


class GeographyQualityAttributionTests(unittest.TestCase):
    """Test _classify_geography_quality_attribution logic for patch routing."""

    def test_bad_geography_low_quality_gives_geography_cause(self) -> None:
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.30,
            fallback_used=True,
            fallback_reason="no_catalog_match",
            bbox_validation_status="default_applied",
            completion_quality_score=0.20,
            efficiency_score=0.10,
        )
        self.assertTrue(out["geography_weak"])
        self.assertTrue(out["quality_low"])
        self.assertEqual(out["likely_cause"], "geography_interpretation")
        self.assertEqual(out["patch_target_hint"], "patch_geography_interpretation")

    def test_good_geography_low_quality_gives_extractor_cause(self) -> None:
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.85,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.20,
            efficiency_score=0.10,
        )
        self.assertFalse(out["geography_weak"])
        self.assertTrue(out["quality_low"])
        self.assertEqual(out["likely_cause"], "extractor_logic")
        self.assertEqual(out["patch_target_hint"], "patch_extractor")

    def test_bad_geography_ok_quality_is_marginal(self) -> None:
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.30,
            fallback_used=True,
            fallback_reason="no_catalog_match",
            bbox_validation_status="default_applied",
            completion_quality_score=0.80,
            efficiency_score=0.70,
        )
        self.assertTrue(out["geography_weak"])
        self.assertFalse(out["quality_low"])
        self.assertEqual(out["likely_cause"], "geography_marginal_but_quality_ok")
        self.assertIsNone(out["patch_target_hint"])

    def test_good_geography_good_quality_gives_none(self) -> None:
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.90,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.85,
            efficiency_score=0.75,
        )
        self.assertFalse(out["geography_weak"])
        self.assertFalse(out["quality_low"])
        self.assertEqual(out["likely_cause"], "none")
        self.assertIsNone(out["patch_target_hint"])

    def test_fallback_used_makes_geography_weak(self) -> None:
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.90,
            fallback_used=True,
            fallback_reason="ambiguous_place",
            bbox_validation_status="valid",
            completion_quality_score=0.80,
            efficiency_score=0.70,
        )
        self.assertTrue(out["geography_weak"])

    def test_invalid_bbox_makes_geography_weak(self) -> None:
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.90,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="invalid",
            completion_quality_score=0.80,
            efficiency_score=0.70,
        )
        self.assertTrue(out["geography_weak"])

    def test_none_scores_do_not_trigger_quality_low(self) -> None:
        out = _classify_geography_quality_attribution(
            interpretation_confidence=None,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status=None,
            completion_quality_score=None,
            efficiency_score=None,
        )
        self.assertFalse(out["geography_weak"])
        self.assertFalse(out["quality_low"])
        self.assertEqual(out["likely_cause"], "none")

    # --- new: geography degradation (not hard-broken but marginal + poor quality) ---

    def test_geography_degradation_marginal_confidence_poor_quality(self) -> None:
        """Confidence slightly above weak threshold but below degradation floor, quality low."""
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.52,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.30,
            efficiency_score=0.20,
        )
        self.assertFalse(out["geography_weak"])
        self.assertTrue(out["quality_low"])
        self.assertTrue(out["geography_degradation"])
        self.assertEqual(out["likely_cause"], "geography_degradation")
        self.assertEqual(out["patch_target_hint"], "patch_geography_interpretation")

    def test_no_degradation_when_confidence_high(self) -> None:
        """High confidence should not trigger degradation even with low quality."""
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.80,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.20,
            efficiency_score=0.10,
        )
        self.assertFalse(out["geography_degradation"])
        self.assertEqual(out["likely_cause"], "extractor_logic")

    # --- new: route hint overreach ---

    def test_route_hint_overreach_with_influenced_bbox_and_poor_quality(self) -> None:
        """Overconstrained route hints + poor quality but confidence above weak threshold."""
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.52,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.25,
            efficiency_score=0.15,
            route_hints_present=True,
            route_hints_influenced_bbox=True,
            route_hints_overconstrained_geography=True,
        )
        self.assertTrue(out["route_hint_overreach"])
        self.assertEqual(out["likely_cause"], "route_hint_overreach")
        self.assertEqual(out["patch_target_hint"], "patch_geography_interpretation")

    def test_route_hint_overreach_without_overconstrained_but_weak_confidence(self) -> None:
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.40,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.30,
            efficiency_score=0.20,
            route_hints_present=True,
            route_hints_influenced_bbox=True,
            route_hints_overconstrained_geography=False,
        )
        # geo_weak is True because confidence < 0.50, so this is geography_interpretation
        self.assertTrue(out["geography_weak"])
        self.assertEqual(out["likely_cause"], "geography_interpretation")

    def test_no_route_hint_overreach_when_not_influenced(self) -> None:
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.52,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.30,
            efficiency_score=0.20,
            route_hints_present=True,
            route_hints_influenced_bbox=False,
            route_hints_overconstrained_geography=False,
        )
        self.assertFalse(out["route_hint_overreach"])

    def test_route_hints_present_but_geography_wins(self) -> None:
        """Route hints present but geography resolves strongly — no overreach."""
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.90,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.85,
            efficiency_score=0.70,
            route_hints_present=True,
            route_hints_influenced_bbox=False,
        )
        self.assertFalse(out["route_hint_overreach"])
        self.assertFalse(out["geography_degradation"])
        self.assertEqual(out["likely_cause"], "none")
        self.assertTrue(out["route_hints_present"])

    # --- new: centralized thresholds ---

    def test_thresholds_are_centralized_constants(self) -> None:
        self.assertIn("interpretation_confidence_weak", GEOGRAPHY_QUALITY_THRESHOLDS)
        self.assertIn("completion_quality_low", GEOGRAPHY_QUALITY_THRESHOLDS)
        self.assertIn("efficiency_low", GEOGRAPHY_QUALITY_THRESHOLDS)
        self.assertIn("geography_degradation_confidence_floor", GEOGRAPHY_QUALITY_THRESHOLDS)
        self.assertIn("geography_degradation_quality_ceiling", GEOGRAPHY_QUALITY_THRESHOLDS)
        self.assertIn("overconstraint_sector_score_floor", GEOGRAPHY_QUALITY_THRESHOLDS)
        self.assertIn("overconstraint_ai_confidence_floor", GEOGRAPHY_QUALITY_THRESHOLDS)

    # --- new: output contract includes new fields ---

    def test_output_includes_new_attribution_fields(self) -> None:
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.60,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.60,
            efficiency_score=0.50,
            route_hints_present=True,
            route_hints_influenced_bbox=True,
        )
        self.assertIn("geography_degradation", out)
        self.assertIn("route_hint_overreach", out)
        self.assertIn("route_hints_present", out)
        self.assertIn("route_hints_influenced_bbox", out)
        self.assertIn("route_hints_overconstrained_geography", out)

    # --- new: fallback bbox + poor quality is attributable ---

    def test_fallback_bbox_poor_quality_attributable(self) -> None:
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.60,
            fallback_used=True,
            fallback_reason="no_catalog_match",
            bbox_validation_status="default_applied",
            completion_quality_score=0.25,
            efficiency_score=0.15,
        )
        self.assertTrue(out["geography_weak"])
        self.assertTrue(out["quality_low"])
        self.assertEqual(out["likely_cause"], "geography_interpretation")
        self.assertTrue(out["fallback_used"])
        self.assertEqual(out["bbox_validation_status"], "default_applied")


    # --- new: real overconstraint signal via rh_overconstrained ---

    def test_overconstrained_true_triggers_overreach_even_above_weak_threshold(self) -> None:
        """When resolver sets overconstrained=True, overreach fires even if confidence > weak."""
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.52,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.25,
            efficiency_score=0.15,
            route_hints_present=True,
            route_hints_influenced_bbox=True,
            route_hints_overconstrained_geography=True,
        )
        self.assertTrue(out["route_hint_overreach"])
        self.assertEqual(out["likely_cause"], "route_hint_overreach")
        self.assertTrue(out["route_hints_overconstrained_geography"])

    def test_overconstrained_false_no_overreach_when_confidence_ok(self) -> None:
        """Without overconstrained signal, overreach doesn't fire if confidence above weak."""
        out = _classify_geography_quality_attribution(
            interpretation_confidence=0.52,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.25,
            efficiency_score=0.15,
            route_hints_present=True,
            route_hints_influenced_bbox=True,
            route_hints_overconstrained_geography=False,
        )
        # Without overconstrained and conf > weak, this is geography_degradation not overreach
        self.assertFalse(out["route_hint_overreach"])
        self.assertEqual(out["likely_cause"], "geography_degradation")

    def test_all_four_cause_distinctions_reachable(self) -> None:
        """Verify all 4 patch-routing causes are reachable with realistic inputs."""
        # 1: geography_interpretation (weak geo + poor quality)
        out1 = _classify_geography_quality_attribution(
            interpretation_confidence=0.30, fallback_used=True, fallback_reason="no_match",
            bbox_validation_status="default_applied", completion_quality_score=0.20, efficiency_score=0.10,
        )
        self.assertEqual(out1["likely_cause"], "geography_interpretation")

        # 2: extractor_logic (strong geo + poor quality)
        out2 = _classify_geography_quality_attribution(
            interpretation_confidence=0.85, fallback_used=False, fallback_reason=None,
            bbox_validation_status="valid", completion_quality_score=0.20, efficiency_score=0.10,
        )
        self.assertEqual(out2["likely_cause"], "extractor_logic")

        # 3: geography_degradation (marginal geo + poor quality, no overconstraint)
        out3 = _classify_geography_quality_attribution(
            interpretation_confidence=0.52, fallback_used=False, fallback_reason=None,
            bbox_validation_status="valid", completion_quality_score=0.30, efficiency_score=0.20,
        )
        self.assertEqual(out3["likely_cause"], "geography_degradation")

        # 4: route_hint_overreach (hints overconstrained + poor quality)
        out4 = _classify_geography_quality_attribution(
            interpretation_confidence=0.52, fallback_used=False, fallback_reason=None,
            bbox_validation_status="valid", completion_quality_score=0.25, efficiency_score=0.15,
            route_hints_present=True, route_hints_influenced_bbox=True,
            route_hints_overconstrained_geography=True,
        )
        self.assertEqual(out4["likely_cause"], "route_hint_overreach")


if __name__ == "__main__":
    unittest.main()
