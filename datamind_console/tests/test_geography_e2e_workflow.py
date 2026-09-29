"""
End-to-end workflow tests for the geography-first extractor pipeline.

These tests verify the full chain:
  UI place input
  → SharedGeographyResolver
  → bbox interpretation / validation
  → spatial interpretation (Phase 1 / Phase 3)
  → geographic_context in interpreter snapshot
  → geography quality attribution
  → patch routing decisions
  → patch-task envelope contents
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datamind_console.api_chatgpt.services.snapshot_builder import (
    _enrich_geographic_context,
    build_interpreter_snapshot,
)
from datamind_console.common.extractor_input_contracts import derive_phase3_route_hint_contract
from datamind_console.common.geography_input_resolver import SharedGeographyResolver
from datamind_console.orchestrator.pipeline_autopilot import (
    GEOGRAPHY_QUALITY_THRESHOLDS,
    _classify_geography_quality_attribution,
)


def _build_geographic_context_from_spatial(
    spatial_ctx: Dict[str, Any],
    *,
    completion_quality_score: Optional[float] = None,
    efficiency_score: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Simulates how pipeline_autopilot builds geographic_context from spatial_context.
    Mirrors the real code path at pipeline_autopilot.py lines ~9010-9147.
    """
    geo = spatial_ctx
    extractor_scores = {
        "completion_quality_score": completion_quality_score,
        "extractor_efficiency_health_score": efficiency_score,
    }

    def _to_float(v: Any) -> Optional[float]:
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            return None

    def _to_bool(v: Any) -> Optional[bool]:
        if v is None:
            return None
        return bool(v)

    geographic_context = {
        "original_geographic_input": geo.get("original_geographic_input"),
        "normalized_geographic_input": geo.get("normalized_geographic_input"),
        "geographic_input_type": geo.get("geographic_input_type"),
        "interpreted_place_meaning": geo.get("interpreted_place_meaning"),
        "geographic_interpretation_source": (
            geo.get("geographic_interpretation_source")
            or geo.get("spatial_interpretation_source")
        ),
        "geographic_interpretation_status": (
            geo.get("geographic_interpretation_status")
            or geo.get("spatial_interpretation_status")
        ),
        "bbox_candidate": geo.get("bbox_candidate"),
        "bbox_candidate_confidence": _to_float(geo.get("bbox_candidate_confidence")),
        "interpretation_confidence": _to_float(
            geo.get("interpretation_confidence")
            if geo.get("interpretation_confidence") is not None
            else geo.get("bbox_candidate_confidence")
        ),
        "bbox_validation_status": geo.get("bbox_validation_status"),
        "effective_bbox_used": geo.get("effective_bbox_used") or geo.get("runtime_bbox_used"),
        "effective_bbox_fingerprint": geo.get("effective_bbox_fingerprint"),
        "fallback_used": _to_bool(geo.get("fallback_used")),
        "fallback_reason": geo.get("fallback_reason"),
        "runtime_spatial_strategy_used": geo.get("runtime_spatial_strategy_used"),
        "geography_priority_enforced": (
            _to_bool(geo.get("geography_priority_enforced"))
            if geo.get("geography_priority_enforced") is not None
            else True
        ),
        "route_hints_influenced_bbox": _to_bool(geo.get("route_hints_influenced_bbox")),
        "route_hint_provenance": geo.get("route_context"),
        "route_hints_present": (
            _to_bool(geo.get("route_hints_present"))
            if geo.get("route_hints_present") is not None
            else False
        ),
        "route_hints_used_as_secondary_signal": (
            _to_bool(geo.get("route_hints_used_as_secondary_signal"))
            if geo.get("route_hints_used_as_secondary_signal") is not None
            else False
        ),
        "route_hints_overconstrained_geography": (
            _to_bool(geo.get("route_hints_overconstrained_geography"))
            if geo.get("route_hints_overconstrained_geography") is not None
            else False
        ),
        "route_hint_effect_reason": geo.get("route_hint_effect_reason"),
        "extractor_quality_summary": {
            "extractor_efficiency_health_score": _to_float(extractor_scores.get("extractor_efficiency_health_score")),
            "completion_quality_score": _to_float(extractor_scores.get("completion_quality_score")),
        },
        "geography_quality_attribution": _classify_geography_quality_attribution(
            interpretation_confidence=_to_float(
                geo.get("interpretation_confidence")
                if geo.get("interpretation_confidence") is not None
                else geo.get("bbox_candidate_confidence")
            ),
            fallback_used=_to_bool(geo.get("fallback_used")),
            fallback_reason=geo.get("fallback_reason"),
            bbox_validation_status=geo.get("bbox_validation_status"),
            completion_quality_score=_to_float(extractor_scores.get("completion_quality_score")),
            efficiency_score=_to_float(extractor_scores.get("extractor_efficiency_health_score")),
            route_hints_present=_to_bool(geo.get("route_hints_present")),
            route_hints_influenced_bbox=_to_bool(geo.get("route_hints_influenced_bbox")),
            route_hints_overconstrained_geography=_to_bool(geo.get("route_hints_overconstrained_geography")),
        ),
    }
    return geographic_context


# ─── Phase 1 E2E ───────────────────────────────────────────────────────────

class Phase1E2ETests(unittest.TestCase):
    """End-to-end tests for Phase 1 extractor workflow."""

    def test_place_input_resolves_into_bbox_and_reaches_snapshot(self) -> None:
        """Full chain: place → resolver → spatial → geographic_context → snapshot."""
        resolver = SharedGeographyResolver()
        geo = resolver.resolve(
            phase="phase1",
            place_input="Sangolqui Core",
            allow_ai_assist=False,
        )
        # 1. Place resolves
        self.assertEqual(geo["interpretation_status"], "ok")
        self.assertIsNotNone(geo["bbox_candidate"])
        self.assertEqual(geo["bbox_validation_status"], "valid")

        # 2. Simulate spatial interpretation output (what _build_phase1_spatial_interpretation produces)
        spatial = {
            "original_geographic_input": "Sangolqui Core",
            "normalized_geographic_input": geo["normalized_geographic_input"],
            "interpreted_place_meaning": geo["interpreted_place_meaning"],
            "geographic_interpretation_source": geo["interpretation_source"],
            "geographic_interpretation_status": geo["interpretation_status"],
            "interpretation_confidence": geo["interpretation_confidence"],
            "bbox_candidate": geo["bbox_candidate"],
            "bbox_candidate_confidence": geo["bbox_candidate_confidence"],
            "bbox_validation_status": geo["bbox_validation_status"],
            "effective_bbox_used": geo["bbox_candidate"],
            "runtime_bbox_used": geo["bbox_candidate"],
            "effective_bbox_fingerprint": geo["effective_bbox_fingerprint"],
            "fallback_used": geo["fallback_used"],
            "fallback_reason": geo["fallback_reason"],
            "runtime_spatial_strategy_used": "bbox_catalog_bbox",
            "geography_priority_enforced": geo["geography_priority_enforced"],
            "route_hints_present": geo["route_hints_present"],
            "route_hints_influenced_bbox": geo["route_hints_influenced_bbox"],
            "route_hints_used_as_secondary_signal": geo.get("route_hints_used_as_secondary_signal", False),
            "route_hints_overconstrained_geography": geo.get("route_hints_overconstrained_geography", False),
            "route_hint_effect_reason": geo.get("route_hint_effect_reason"),
            "geographic_input_type": "place_input",
        }

        # 3. Build geographic_context
        gc = _build_geographic_context_from_spatial(
            spatial, completion_quality_score=0.80, efficiency_score=0.70,
        )

        # 4. Geography context reaches snapshot correctly
        self.assertEqual(gc["original_geographic_input"], "Sangolqui Core")
        self.assertIsNotNone(gc["bbox_candidate"])
        self.assertEqual(gc["bbox_validation_status"], "valid")
        self.assertFalse(gc["fallback_used"])
        self.assertEqual(gc["effective_bbox_used"], geo["bbox_candidate"])

        # 5. Quality attribution is healthy
        attr = gc["geography_quality_attribution"]
        self.assertFalse(attr["geography_weak"])
        self.assertFalse(attr["quality_low"])
        self.assertEqual(attr["likely_cause"], "none")

        # 6. Enriched context has correct failure mode
        enriched = _enrich_geographic_context(gc)
        self.assertIsNone(enriched["geography_failure_mode"])

    def test_weak_geography_weak_quality_triggers_geography_cause(self) -> None:
        """Weak geography + weak extractor quality → geography_interpretation cause."""
        resolver = SharedGeographyResolver()
        geo = resolver.resolve(
            phase="phase1",
            place_input="nonexistent place xyzzy 12345",
            allow_ai_assist=False,
        )

        spatial = {
            "original_geographic_input": "nonexistent place xyzzy 12345",
            "interpretation_confidence": 0.30,
            "bbox_candidate": None,
            "bbox_candidate_confidence": None,
            "bbox_validation_status": "default_applied",
            "fallback_used": True,
            "fallback_reason": "place_input_unresolved",
            "runtime_bbox_used": {"south": -0.35, "west": -78.6, "north": -0.1, "east": -78.3},
            "effective_bbox_used": {"south": -0.35, "west": -78.6, "north": -0.1, "east": -78.3},
            "runtime_spatial_strategy_used": "default_bbox_fallback",
            "geography_priority_enforced": True,
            "route_hints_present": False,
            "route_hints_influenced_bbox": False,
            "route_hints_used_as_secondary_signal": False,
            "route_hints_overconstrained_geography": False,
            "route_hint_effect_reason": None,
        }

        gc = _build_geographic_context_from_spatial(
            spatial, completion_quality_score=0.20, efficiency_score=0.10,
        )

        attr = gc["geography_quality_attribution"]
        self.assertTrue(attr["geography_weak"])
        self.assertTrue(attr["quality_low"])
        self.assertEqual(attr["likely_cause"], "geography_interpretation")
        self.assertEqual(attr["patch_target_hint"], "patch_geography_interpretation")

        enriched = _enrich_geographic_context(gc)
        self.assertEqual(enriched["geography_failure_mode"], "geography_interpretation")

    def test_explicit_bbox_override_wins(self) -> None:
        """Explicit bbox always wins over place resolution."""
        resolver = SharedGeographyResolver()
        explicit = {"south": -0.35, "west": -78.50, "north": -0.20, "east": -78.35}
        geo = resolver.resolve(
            phase="phase1",
            place_input="Sangolqui Core",
            explicit_bbox=explicit,
            allow_ai_assist=False,
        )
        self.assertEqual(geo["bbox_candidate"], explicit)
        self.assertEqual(geo["interpretation_source"], "explicit_bbox")
        self.assertAlmostEqual(geo["interpretation_confidence"], 0.99)

    def test_good_geography_poor_extractor_gives_extractor_cause(self) -> None:
        """Strong geography + poor extraction → extractor_logic, not geography."""
        spatial = {
            "interpretation_confidence": 0.90,
            "bbox_candidate": {"south": -0.3, "west": -78.5, "north": -0.2, "east": -78.4},
            "bbox_validation_status": "valid",
            "fallback_used": False,
            "fallback_reason": None,
            "route_hints_present": False,
            "route_hints_influenced_bbox": False,
            "route_hints_overconstrained_geography": False,
        }
        gc = _build_geographic_context_from_spatial(
            spatial, completion_quality_score=0.15, efficiency_score=0.10,
        )
        attr = gc["geography_quality_attribution"]
        self.assertFalse(attr["geography_weak"])
        self.assertTrue(attr["quality_low"])
        self.assertEqual(attr["likely_cause"], "extractor_logic")
        self.assertEqual(attr["patch_target_hint"], "patch_extractor")

        enriched = _enrich_geographic_context(gc)
        self.assertEqual(enriched["geography_failure_mode"], "geography_healthy_extractor_weak")


# ─── Phase 3 E2E ───────────────────────────────────────────────────────────

class Phase3E2ETests(unittest.TestCase):
    """End-to-end tests for Phase 3 route extractor workflow."""

    def test_place_resolves_and_route_hints_stay_secondary(self) -> None:
        """Place input resolves into bbox; route hints do NOT override geography."""
        resolver = SharedGeographyResolver()
        hint_contract = derive_phase3_route_hint_contract("E1, Ecovia")

        geo_no_hints = resolver.resolve(
            phase="phase3",
            place_input="Sangolqui Core",
            supporting_hints={},
            allow_ai_assist=False,
        )
        geo_with_hints = resolver.resolve(
            phase="phase3",
            place_input="Sangolqui Core",
            supporting_hints={
                "phase3_operator_hint": "Metro Quito",
                "phase3_route_hint": hint_contract["route_hint"],
                "phase3_refs_hint": hint_contract["refs"],
            },
            allow_ai_assist=False,
        )

        # Same bbox regardless of hints
        self.assertEqual(geo_no_hints["bbox_candidate"], geo_with_hints["bbox_candidate"])
        # But hints ARE present
        self.assertTrue(geo_with_hints["route_hints_present"])
        # And geography priority is enforced
        self.assertTrue(geo_with_hints["geography_priority_enforced"])
        # Hints did NOT influence bbox (bbox came from catalog, not sector with route tokens)
        self.assertFalse(geo_with_hints["route_hints_influenced_bbox"])

    def test_route_hint_provenance_in_snapshot(self) -> None:
        """Route hint provenance fields flow through to geographic_context."""
        resolver = SharedGeographyResolver()
        hint_contract = derive_phase3_route_hint_contract("E1")

        geo = resolver.resolve(
            phase="phase3",
            place_input="Sangolqui Core",
            supporting_hints={
                "phase3_refs_hint": hint_contract["refs"],
            },
            allow_ai_assist=False,
        )

        spatial = {
            "original_geographic_input": "Sangolqui Core",
            "interpreted_place_meaning": geo["interpreted_place_meaning"],
            "geographic_interpretation_source": geo["interpretation_source"],
            "geographic_interpretation_status": geo["interpretation_status"],
            "interpretation_confidence": geo["interpretation_confidence"],
            "bbox_candidate": geo["bbox_candidate"],
            "bbox_candidate_confidence": geo["bbox_candidate_confidence"],
            "bbox_validation_status": geo["bbox_validation_status"],
            "effective_bbox_used": geo["bbox_candidate"],
            "runtime_bbox_used": geo["bbox_candidate"],
            "fallback_used": geo["fallback_used"],
            "fallback_reason": geo["fallback_reason"],
            "runtime_spatial_strategy_used": "bbox_catalog_bbox",
            "geography_priority_enforced": geo["geography_priority_enforced"],
            "route_hints_present": geo["route_hints_present"],
            "route_hints_influenced_bbox": geo["route_hints_influenced_bbox"],
            "route_hints_used_as_secondary_signal": geo.get("route_hints_used_as_secondary_signal", False),
            "route_hints_overconstrained_geography": geo.get("route_hints_overconstrained_geography", False),
            "route_hint_effect_reason": geo.get("route_hint_effect_reason"),
            "route_context": {
                "refs": hint_contract["refs"],
                "operator": None,
                "name": None,
                "hint_strength": hint_contract["hint_strength"],
            },
        }

        gc = _build_geographic_context_from_spatial(
            spatial, completion_quality_score=0.80, efficiency_score=0.70,
        )

        # Route hint provenance is visible in geographic_context
        self.assertTrue(gc["route_hints_present"])
        self.assertFalse(gc["route_hints_influenced_bbox"])
        self.assertIsNotNone(gc["route_hint_provenance"])
        self.assertEqual(gc["route_hint_provenance"]["refs"], ["E1"])
        self.assertEqual(gc["route_hint_provenance"]["hint_strength"], "strong")

        # Geography quality attribution carries route hint fields
        attr = gc["geography_quality_attribution"]
        self.assertTrue(attr["route_hints_present"])
        self.assertFalse(attr["route_hints_influenced_bbox"])

    def test_route_hint_overreach_detected_in_e2e_flow(self) -> None:
        """Route hints influenced bbox + overconstrained + poor quality → overreach."""
        spatial = {
            "original_geographic_input": "sector test",
            "interpretation_confidence": 0.52,
            "bbox_candidate": {"south": -0.3, "west": -78.5, "north": -0.2, "east": -78.4},
            "bbox_validation_status": "valid",
            "fallback_used": False,
            "fallback_reason": None,
            "route_hints_present": True,
            "route_hints_influenced_bbox": True,
            "route_hints_used_as_secondary_signal": True,
            "route_hints_overconstrained_geography": True,
            "route_hint_effect_reason": "route_tokens_influenced_sector_match",
            "route_context": {"refs": ["X1"], "hint_strength": "strong"},
        }

        gc = _build_geographic_context_from_spatial(
            spatial, completion_quality_score=0.25, efficiency_score=0.15,
        )

        attr = gc["geography_quality_attribution"]
        self.assertTrue(attr["route_hint_overreach"])
        self.assertEqual(attr["likely_cause"], "route_hint_overreach")
        self.assertEqual(attr["patch_target_hint"], "patch_geography_interpretation")

        enriched = _enrich_geographic_context(gc)
        self.assertEqual(enriched["geography_failure_mode"], "route_hint_overreach")

    def test_geography_degradation_detected_in_e2e_flow(self) -> None:
        """Marginal confidence (not hard-broken) + poor quality → degradation."""
        spatial = {
            "original_geographic_input": "ambiguous zone",
            "interpretation_confidence": 0.52,
            "bbox_candidate": {"south": -0.3, "west": -78.5, "north": -0.2, "east": -78.4},
            "bbox_validation_status": "valid",
            "fallback_used": False,
            "fallback_reason": None,
            "route_hints_present": False,
            "route_hints_influenced_bbox": False,
            "route_hints_overconstrained_geography": False,
        }

        gc = _build_geographic_context_from_spatial(
            spatial, completion_quality_score=0.30, efficiency_score=0.20,
        )

        attr = gc["geography_quality_attribution"]
        self.assertTrue(attr["geography_degradation"])
        self.assertEqual(attr["likely_cause"], "geography_degradation")
        self.assertEqual(attr["patch_target_hint"], "patch_geography_interpretation")

        enriched = _enrich_geographic_context(gc)
        self.assertEqual(enriched["geography_failure_mode"], "geography_degradation")

    def test_fallback_bbox_poor_quality_visible_and_attributable(self) -> None:
        """Fallback/default bbox + poor quality is explicitly visible."""
        spatial = {
            "original_geographic_input": "unknown place",
            "interpretation_confidence": None,
            "bbox_candidate": None,
            "bbox_validation_status": "default_applied",
            "fallback_used": True,
            "fallback_reason": "phase3_bbox_missing_from_scope",
            "runtime_bbox_used": {"south": -0.35, "west": -78.6, "north": -0.1, "east": -78.3},
            "effective_bbox_used": {"south": -0.35, "west": -78.6, "north": -0.1, "east": -78.3},
            "runtime_spatial_strategy_used": "default_bbox_fallback",
            "route_hints_present": False,
            "route_hints_influenced_bbox": False,
            "route_hints_overconstrained_geography": False,
        }

        gc = _build_geographic_context_from_spatial(
            spatial, completion_quality_score=0.15, efficiency_score=0.05,
        )

        # Fallback is visible
        self.assertTrue(gc["fallback_used"])
        self.assertEqual(gc["fallback_reason"], "phase3_bbox_missing_from_scope")
        self.assertEqual(gc["bbox_validation_status"], "default_applied")

        # Quality attribution blames geography
        attr = gc["geography_quality_attribution"]
        self.assertTrue(attr["geography_weak"])
        self.assertTrue(attr["quality_low"])
        self.assertEqual(attr["likely_cause"], "geography_interpretation")
        self.assertTrue(attr["fallback_used"])


# ─── Patch Envelope E2E ─────────────────────────────────────────────────────

class PatchEnvelopeE2ETests(unittest.TestCase):
    """Verify patch task envelope includes required geography evidence."""

    def test_patch_envelope_includes_geography_evidence(self) -> None:
        """The interpreter snapshot (used as patch envelope base) includes geography."""
        spatial = {
            "original_geographic_input": "Conocoto corridor",
            "interpreted_place_meaning": "Conocoto terminal corridor",
            "interpretation_confidence": 0.85,
            "bbox_candidate": {"south": -0.33, "west": -78.48, "north": -0.22, "east": -78.38},
            "bbox_validation_status": "valid",
            "fallback_used": False,
            "fallback_reason": None,
            "route_hints_present": True,
            "route_hints_influenced_bbox": False,
            "route_hints_used_as_secondary_signal": False,
            "route_hints_overconstrained_geography": False,
            "route_hint_effect_reason": None,
            "route_context": {"refs": ["E1"], "hint_strength": "strong"},
        }
        gc = _build_geographic_context_from_spatial(
            spatial, completion_quality_score=0.30, efficiency_score=0.20,
        )

        # Build interpreter snapshot (the base for patch envelope)
        snapshot = build_interpreter_snapshot(
            snapshot_id="test-snap-1",
            run_id="run-1",
            trace_id="trace-1",
            phase="phase3",
            step_id="P3.1_EXTRACT",
            trigger="test",
            stage_normalization=None,
            validator={"status": "blocked", "gate_passed": False},
            executor_summary={"quality_score": 0.30},
            spatial_context=spatial,
            geographic_context=gc,
            promote_context=None,
            phase3_route_extractor_packet={"scope": {"bbox": spatial["bbox_candidate"], "refs": ["E1"]}},
            ai_scores={"quality_score": 0.30},
            ai_metrics={},
            ai_warnings=[],
            ai_anomaly_flags=[],
            regression_flags=[],
            compare={},
            reorder={},
            extractor_status={},
            extractor_help_needed={},
            adaptive_retry_plan=None,
            shadow_learned_retry_plan=None,
            shadow_retry_plan_comparison=None,
            patch_history_summary=None,
            recent_history_summary=None,
            history=None,
            policy_profile=None,
            insufficient_data_flags=[],
            consistency_candidates=[],
        )

        # Verify geography evidence is in the snapshot
        self.assertIsNotNone(snapshot.get("geographic_context"))
        snap_gc = snapshot["geographic_context"]
        self.assertEqual(snap_gc["original_geographic_input"], "Conocoto corridor")
        self.assertEqual(snap_gc["interpreted_place_meaning"], "Conocoto terminal corridor")
        self.assertIsNotNone(snap_gc["bbox_candidate"])
        self.assertIsNotNone(snap_gc["geography_quality_attribution"])
        self.assertIsNotNone(snap_gc.get("geography_failure_mode"))

        # Verify route hint provenance
        self.assertTrue(snap_gc["route_hints_present"])
        self.assertFalse(snap_gc["route_hints_influenced_bbox"])
        self.assertIsNotNone(snap_gc["route_hint_provenance"])

        # Verify Phase 3 packet is present
        self.assertIsNotNone(snapshot.get("phase3_route_extractor_packet"))
        self.assertIsNotNone(snapshot["phase3_route_extractor_packet"]["scope"]["bbox"])
        self.assertEqual(snapshot["phase3_route_extractor_packet"]["scope"]["refs"], ["E1"])


# ─── Snapshot E2E ────────────────────────────────────────────────────────────

class SnapshotE2ETests(unittest.TestCase):
    """Verify normalized snapshot preserves all geography truth."""

    def test_snapshot_preserves_geography_failure_mode(self) -> None:
        gc = {
            "original_geographic_input": "test",
            "geography_quality_attribution": {
                "likely_cause": "geography_degradation",
                "geography_degradation": True,
                "route_hint_overreach": False,
                "route_hints_present": True,
            },
            "route_hints_present": True,
            "route_hints_influenced_bbox": False,
        }
        enriched = _enrich_geographic_context(gc)
        self.assertEqual(enriched["geography_failure_mode"], "geography_degradation")

    def test_snapshot_propagates_route_hint_fields_from_attribution(self) -> None:
        """If route_hint fields are only in attribution, they still surface."""
        gc = {
            "original_geographic_input": "test",
            "geography_quality_attribution": {
                "likely_cause": "none",
                "route_hints_present": True,
                "route_hints_influenced_bbox": True,
                "route_hints_overconstrained_geography": False,
                "route_hint_effect_reason": "test_reason",
            },
        }
        enriched = _enrich_geographic_context(gc)
        self.assertTrue(enriched["route_hints_present"])
        self.assertTrue(enriched["route_hints_influenced_bbox"])
        self.assertFalse(enriched["route_hints_overconstrained_geography"])
        self.assertEqual(enriched["route_hint_effect_reason"], "test_reason")

    def test_snapshot_does_not_overwrite_existing_route_hint_fields(self) -> None:
        """If route_hint fields exist at top level, enrichment doesn't overwrite."""
        gc = {
            "original_geographic_input": "test",
            "route_hints_present": False,
            "geography_quality_attribution": {
                "route_hints_present": True,  # different from top-level
            },
        }
        enriched = _enrich_geographic_context(gc)
        # Top-level value preserved
        self.assertFalse(enriched["route_hints_present"])

    def test_centralized_thresholds_used_consistently(self) -> None:
        """Geography quality thresholds are centralized and testable."""
        self.assertEqual(GEOGRAPHY_QUALITY_THRESHOLDS["interpretation_confidence_weak"], 0.50)
        self.assertEqual(GEOGRAPHY_QUALITY_THRESHOLDS["completion_quality_low"], 0.40)
        self.assertEqual(GEOGRAPHY_QUALITY_THRESHOLDS["efficiency_low"], 0.30)
        self.assertEqual(GEOGRAPHY_QUALITY_THRESHOLDS["geography_degradation_confidence_floor"], 0.55)
        self.assertEqual(GEOGRAPHY_QUALITY_THRESHOLDS["geography_degradation_quality_ceiling"], 0.45)

        # Verify they're actually used by the function
        out_at_boundary = _classify_geography_quality_attribution(
            interpretation_confidence=0.50,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.40,
            efficiency_score=0.30,
        )
        # At exact thresholds, should NOT be weak/low
        self.assertFalse(out_at_boundary["geography_weak"])
        self.assertFalse(out_at_boundary["quality_low"])

        # Just below thresholds
        out_below = _classify_geography_quality_attribution(
            interpretation_confidence=0.49,
            fallback_used=False,
            fallback_reason=None,
            bbox_validation_status="valid",
            completion_quality_score=0.39,
            efficiency_score=0.29,
        )
        self.assertTrue(out_below["geography_weak"])
        self.assertTrue(out_below["quality_low"])


    def test_overconstraint_thresholds_present(self) -> None:
        """New overconstraint thresholds are centralized."""
        self.assertEqual(GEOGRAPHY_QUALITY_THRESHOLDS["overconstraint_sector_score_floor"], 0.80)
        self.assertEqual(GEOGRAPHY_QUALITY_THRESHOLDS["overconstraint_ai_confidence_floor"], 0.60)


# ─── Overconstraint E2E ─────────────────────────────────────────────────────

class OverconstraintE2ETests(unittest.TestCase):
    """E2E tests verifying overconstraint detection flows through the full pipeline."""

    def _make_spatial(
        self,
        *,
        confidence: float,
        route_hints_influenced: bool,
        overconstrained: bool,
        fallback_used: bool = False,
        bbox_status: str = "valid",
        effect_reason: str | None = None,
    ) -> Dict[str, Any]:
        return {
            "original_geographic_input": "test route corridor",
            "normalized_geographic_input": "test route corridor",
            "interpreted_place_meaning": "Test corridor",
            "geographic_interpretation_source": "sector_catalog",
            "geographic_interpretation_status": "ok",
            "geographic_input_type": "place_input",
            "interpretation_confidence": confidence,
            "bbox_candidate": {"south": -0.3, "west": -78.5, "north": -0.2, "east": -78.4},
            "bbox_candidate_confidence": confidence,
            "bbox_validation_status": bbox_status,
            "effective_bbox_used": {"south": -0.3, "west": -78.5, "north": -0.2, "east": -78.4},
            "effective_bbox_fingerprint": "abc123",
            "fallback_used": fallback_used,
            "fallback_reason": "no_match" if fallback_used else None,
            "runtime_spatial_strategy_used": "sector_catalog_bbox",
            "geography_priority_enforced": True,
            "route_hints_present": True,
            "route_hints_influenced_bbox": route_hints_influenced,
            "route_hints_used_as_secondary_signal": route_hints_influenced,
            "route_hints_overconstrained_geography": overconstrained,
            "route_hint_effect_reason": effect_reason,
            "route_context": {"refs": ["E1"], "hint_strength": "strong"},
        }

    def test_overconstrained_signal_reaches_snapshot_as_route_hint_overreach(self) -> None:
        """Overconstrained=True + poor quality → route_hint_overreach in snapshot."""
        spatial = self._make_spatial(
            confidence=0.52,
            route_hints_influenced=True,
            overconstrained=True,
            effect_reason="route_tokens_overconstrained_sector_match",
        )
        gc = _build_geographic_context_from_spatial(
            spatial, completion_quality_score=0.25, efficiency_score=0.15,
        )
        attr = gc["geography_quality_attribution"]
        self.assertTrue(attr["route_hint_overreach"])
        self.assertEqual(attr["likely_cause"], "route_hint_overreach")

        enriched = _enrich_geographic_context(gc)
        self.assertEqual(enriched["geography_failure_mode"], "route_hint_overreach")
        self.assertTrue(enriched["route_hints_overconstrained_geography"])

    def test_overconstrained_false_with_marginal_confidence_gives_degradation(self) -> None:
        """Overconstrained=False + marginal confidence + poor quality → geography_degradation."""
        spatial = self._make_spatial(
            confidence=0.52,
            route_hints_influenced=True,
            overconstrained=False,
            effect_reason="route_tokens_influenced_sector_match",
        )
        gc = _build_geographic_context_from_spatial(
            spatial, completion_quality_score=0.30, efficiency_score=0.20,
        )
        attr = gc["geography_quality_attribution"]
        self.assertFalse(attr["route_hint_overreach"])
        self.assertTrue(attr["geography_degradation"])
        self.assertEqual(attr["likely_cause"], "geography_degradation")

    def test_all_four_causes_in_snapshot_context(self) -> None:
        """All 4 cause distinctions produce correct geography_failure_mode in enriched context."""
        cases = [
            # (confidence, influenced, overconstrained, fallback_used, bbox_status, cqs, eff, expected_cause)
            (0.30, False, False, True, "default_applied", 0.20, 0.10, "geography_interpretation"),
            (0.85, False, False, False, "valid", 0.20, 0.10, "extractor_logic"),
            (0.52, True, False, False, "valid", 0.30, 0.20, "geography_degradation"),
            (0.52, True, True, False, "valid", 0.25, 0.15, "route_hint_overreach"),
        ]
        for conf, infl, overc, fb, bstat, cqs, eff, expected in cases:
            spatial = self._make_spatial(
                confidence=conf,
                route_hints_influenced=infl,
                overconstrained=overc,
                fallback_used=fb,
                bbox_status=bstat,
            )
            gc = _build_geographic_context_from_spatial(
                spatial, completion_quality_score=cqs, efficiency_score=eff,
            )
            attr = gc["geography_quality_attribution"]
            self.assertEqual(
                attr["likely_cause"], expected,
                f"Expected {expected} for conf={conf}, infl={infl}, overc={overc}, fb={fb}, cqs={cqs}",
            )
            enriched = _enrich_geographic_context(gc)
            if expected == "extractor_logic":
                self.assertEqual(enriched["geography_failure_mode"], "geography_healthy_extractor_weak")
            else:
                self.assertEqual(enriched["geography_failure_mode"], expected)

    def test_no_overconstraint_when_quality_ok(self) -> None:
        """Even with overconstrained=True, if quality is fine, no overreach fires."""
        spatial = self._make_spatial(
            confidence=0.52,
            route_hints_influenced=True,
            overconstrained=True,
        )
        gc = _build_geographic_context_from_spatial(
            spatial, completion_quality_score=0.80, efficiency_score=0.70,
        )
        attr = gc["geography_quality_attribution"]
        self.assertFalse(attr["route_hint_overreach"])
        self.assertEqual(attr["likely_cause"], "none")


if __name__ == "__main__":
    unittest.main()
