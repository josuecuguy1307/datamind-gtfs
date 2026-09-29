"""
Unit tests for the A2 synthesis bridge
(``datamind_console.phases.phase3_routes.stop_grounding.a2_synthesis_bridge``).

The bridge sits between Stage A (typed grounding) and the corridor build
path. These tests target the bridge's three public responsibilities:

1. Tokens that already resolved in Stage A are skipped — synthesis is never
   invoked for them.
2. Unresolved anchor tokens are funneled into
   ``synthesis.core.path_aware_synthesis``; when that returns success, the
   bridge wraps the result as a ``synthesized`` decision.
3. When synthesis returns a cap-hit rejection, the bridge surfaces it as
   ``dropped_cap_route`` (or ``dropped_cap_unit_week``) and the pipeline
   can degrade gracefully without raising.

Full-pipeline canary tests live in the Rumiñahui canary run, not here —
these tests isolate the bridge by injecting ``synthesis_fn``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List
from unittest.mock import MagicMock

import pytest

from datamind_console.phases.phase3_routes.stop_grounding import (
    a2_synthesis_bridge as bridge,
)
from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    TypedGroundingResult,
    TypedRouteSeed,
    TypedSeedToken,
    TypedTokenGrounding,
    StopMatch,
)
from datamind_console.phases.phase3_routes.synthesis import core as synth_core
from datamind_console.phases.phase3_routes.synthesis import stages as st


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _token(label: str, *, anchor_role: str = "waypoint",
           kind: str = "stop_candidate") -> TypedSeedToken:
    return TypedSeedToken(
        label=label,
        kind=kind,
        role="intermediate",
        resolution_policy="resolve_to_best_matching_stop_node",
        confidence="medium",
        position=0,
        anchor_role=anchor_role,
    )


def _stopmatch(stop_id: str, stop_name: str) -> StopMatch:
    return StopMatch(
        stop_id=stop_id,
        stop_name=stop_name,
        lat=-0.18,
        lon=-78.47,
        name_similarity=1.0,
        composite_score=1.0,
    )


def _seed_with_tokens(tokens: List[TypedSeedToken]) -> TypedRouteSeed:
    return TypedRouteSeed(
        route_name="CAL-03",
        cooperative="CALSIG",
        sequence_tokens=tokens,
        corridor_constraints=[],
        localities=["ruminahui"],
        province="sample_region",
    )


def _grounding(resolved: Dict[str, TypedSeedToken],
               unresolved: Dict[str, TypedSeedToken]) -> TypedGroundingResult:
    token_groundings: Dict[str, TypedTokenGrounding] = {}
    for label, token in resolved.items():
        token_groundings[label] = TypedTokenGrounding(
            token=token,
            candidates=[_stopmatch(f"stop-{label}", label)],
            best_candidate=_stopmatch(f"stop-{label}", label),
            resolution_method="db_stop",
            is_resolved=True,
        )
    for label, token in unresolved.items():
        token_groundings[label] = TypedTokenGrounding(
            token=token,
            candidates=[],
            best_candidate=None,
            resolution_method="unresolved",
            is_resolved=False,
        )
    return TypedGroundingResult(
        token_groundings=token_groundings,
        corridor_constraints=[],
        overall_confidence=0.5,
    )


def _inputs(tmp_path: Path) -> bridge.A2RunInputs:
    review_root = tmp_path / "synthetic_review"
    (review_root / "pending").mkdir(parents=True, exist_ok=True)
    research_root = tmp_path / "research_queue"
    (research_root / "pending").mkdir(parents=True, exist_ok=True)
    return bridge.A2RunInputs(
        route_id="route-uuid-1",
        route_code="CAL-03",
        unit="ruminahui",
        province="sample_region",
        termini=((-0.180, -78.475), (-0.180, -78.465)),
        osm_relation_id=None,
        grounded_stop_coords=[],
        review_root=review_root,
        research_queue_root=research_root,
        anchor_payloads={},
    )


def _spy_synth(return_value) -> Callable:
    spy = MagicMock(return_value=return_value)
    return spy


# ---------------------------------------------------------------------------
# Scenario 1 — resolved anchor skips A2 (synthesis is never called)
# ---------------------------------------------------------------------------


def test_resolved_anchor_skips_synthesis(tmp_path: Path):
    resolved_token = _token("Capelo")
    typed_seed = _seed_with_tokens([resolved_token])
    grounding = _grounding(resolved={"Capelo": resolved_token}, unresolved={})

    spy = _spy_synth(return_value=None)

    decisions = bridge.run_a2_for_unresolved_anchors(
        typed_grounding=grounding,
        typed_seed=typed_seed,
        inputs=_inputs(tmp_path),
        conn=MagicMock(),
        synthesis_fn=spy,
    )

    assert spy.call_count == 0, (
        "path_aware_synthesis must not be invoked when the token already "
        "has a best_candidate in token_groundings"
    )
    assert len(decisions) == 1
    assert decisions[0].anchor_name == "Capelo"
    assert decisions[0].outcome == "skipped_already_grounded"


# ---------------------------------------------------------------------------
# Scenario 2 — unresolved anchor fires synthesis, success wraps as
# ``synthesized``
# ---------------------------------------------------------------------------


def test_unresolved_anchor_fires_synthesis_and_wraps_success(tmp_path: Path):
    unresolved_token = _token("Y de Calsig", anchor_role="waypoint")
    typed_seed = _seed_with_tokens([unresolved_token])
    grounding = _grounding(resolved={}, unresolved={"Y de Calsig": unresolved_token})

    success_result = synth_core.SynthesisResult(
        success=True,
        stage=st.STAGE_3B,
        node_id="node-uuid-42",
        osm_id=-1_000_042,
        final_coords=(-0.181, -78.470),
        source="path_corridor_projected:CAL-03:Y-de-Calsig",
        source_type="path_corridor_projected",
        synthetic_confidence="medium",
        semantic_spatial_conflict=False,
        review_path=Path(tmp_path / "synthetic_review" / "pending" / "node-42.md"),
    )
    spy = _spy_synth(return_value=success_result)

    inputs = _inputs(tmp_path)
    inputs.anchor_payloads["Y de Calsig"] = {
        "research_coords": (-0.1812, -78.4705),
        "road_tokens": ("Autopista General Rumiñahui",),
        "research_output_file": "01_stop_grounding_detail_ruminahui_CAL-03_abc.json",
    }

    decisions = bridge.run_a2_for_unresolved_anchors(
        typed_grounding=grounding,
        typed_seed=typed_seed,
        inputs=inputs,
        conn=MagicMock(),
        synthesis_fn=spy,
    )

    assert spy.call_count == 1
    call_kwargs = spy.call_args.kwargs
    passed_anchor = spy.call_args.args[0]
    passed_route = spy.call_args.args[1]
    assert passed_anchor.name == "Y de Calsig"
    assert passed_anchor.research_coords == (-0.1812, -78.4705)
    assert passed_anchor.road_tokens == ("Autopista General Rumiñahui",)
    assert passed_route.route_id == "route-uuid-1"
    assert passed_route.route_code == "CAL-03"
    assert passed_route.unit == "ruminahui"
    assert call_kwargs["research_queue_root"] is not None

    assert len(decisions) == 1
    d = decisions[0]
    assert d.outcome == "synthesized"
    assert d.stage == st.STAGE_3B
    assert d.node_id == "node-uuid-42"
    assert d.osm_id == -1_000_042
    assert d.osm_id < 0, "synthetic node must carry a negative osm_id"
    assert d.synthetic_confidence == "medium"
    assert d.semantic_spatial_conflict is False
    assert d.review_path is not None
    assert d.rejected_reason is None


# ---------------------------------------------------------------------------
# Scenario 3 — cap-hit synthesis result degrades to dropped_cap_route
# ---------------------------------------------------------------------------


def test_cap_hit_synthesis_result_maps_to_dropped_cap_route(tmp_path: Path):
    unresolved_token = _token("San Pedro de Amaguaña", anchor_role="waypoint")
    typed_seed = _seed_with_tokens([unresolved_token])
    grounding = _grounding(
        resolved={}, unresolved={"San Pedro de Amaguaña": unresolved_token},
    )

    cap_hit_result = synth_core.SynthesisResult(
        success=False,
        stage=st.STAGE_4,
        rejected_reason="route_synthesis_cap_hit",
    )
    spy = _spy_synth(return_value=cap_hit_result)

    decisions = bridge.run_a2_for_unresolved_anchors(
        typed_grounding=grounding,
        typed_seed=typed_seed,
        inputs=_inputs(tmp_path),
        conn=MagicMock(),
        synthesis_fn=spy,
    )

    assert spy.call_count == 1
    assert len(decisions) == 1
    d = decisions[0]
    assert d.outcome == "dropped_cap_route"
    assert d.rejected_reason == "route_synthesis_cap_hit"
    assert d.stage == st.STAGE_4
    assert d.node_id is None
    assert d.osm_id is None


def test_unit_weekly_cap_hit_maps_to_dropped_cap_unit_week(tmp_path: Path):
    unresolved_token = _token("Anchor X", anchor_role="waypoint")
    typed_seed = _seed_with_tokens([unresolved_token])
    grounding = _grounding(resolved={}, unresolved={"Anchor X": unresolved_token})

    spy = _spy_synth(return_value=synth_core.SynthesisResult(
        success=False, stage=st.STAGE_3D,
        rejected_reason="unit_weekly_cap_hit",
    ))

    decisions = bridge.run_a2_for_unresolved_anchors(
        typed_grounding=grounding,
        typed_seed=typed_seed,
        inputs=_inputs(tmp_path),
        conn=MagicMock(),
        synthesis_fn=spy,
    )

    assert len(decisions) == 1
    assert decisions[0].outcome == "dropped_cap_unit_week"
    assert decisions[0].rejected_reason == "unit_weekly_cap_hit"


def test_low_precision_research_coords_maps_to_dropped_precision(tmp_path: Path):
    unresolved_token = _token("Anchor Y", anchor_role="waypoint")
    typed_seed = _seed_with_tokens([unresolved_token])
    grounding = _grounding(resolved={}, unresolved={"Anchor Y": unresolved_token})

    spy = _spy_synth(return_value=synth_core.SynthesisResult(
        success=False,
        rejected_reason="research_coords_low_precision",
    ))

    decisions = bridge.run_a2_for_unresolved_anchors(
        typed_grounding=grounding,
        typed_seed=typed_seed,
        inputs=_inputs(tmp_path),
        conn=MagicMock(),
        synthesis_fn=spy,
    )

    assert len(decisions) == 1
    assert decisions[0].outcome == "dropped_precision"


def test_synthesis_raising_is_caught_and_reported_as_error(tmp_path: Path):
    unresolved_token = _token("Explodes", anchor_role="waypoint")
    typed_seed = _seed_with_tokens([unresolved_token])
    grounding = _grounding(resolved={}, unresolved={"Explodes": unresolved_token})

    def _boom(*a, **kw):
        raise RuntimeError("valhalla timeout")

    decisions = bridge.run_a2_for_unresolved_anchors(
        typed_grounding=grounding,
        typed_seed=typed_seed,
        inputs=_inputs(tmp_path),
        conn=MagicMock(),
        synthesis_fn=_boom,
    )

    assert len(decisions) == 1
    d = decisions[0]
    assert d.outcome == "error"
    assert "valhalla timeout" in (d.error or "")


# ---------------------------------------------------------------------------
# corridor-only constraint tokens are skipped with skipped_not_anchor
# ---------------------------------------------------------------------------


def test_corridor_only_token_is_skipped_not_anchor(tmp_path: Path):
    corridor_token = TypedSeedToken(
        label="Av. Ilaló",
        kind="road",
        role="corridor",
        resolution_policy="use_as_corridor_constraint_only",
        confidence="medium",
        position=0,
        anchor_role="waypoint",
    )
    typed_seed = _seed_with_tokens([corridor_token])
    grounding = _grounding(resolved={}, unresolved={})

    spy = _spy_synth(return_value=None)
    decisions = bridge.run_a2_for_unresolved_anchors(
        typed_grounding=grounding,
        typed_seed=typed_seed,
        inputs=_inputs(tmp_path),
        conn=MagicMock(),
        synthesis_fn=spy,
    )

    assert spy.call_count == 0
    assert len(decisions) == 1
    assert decisions[0].outcome == "skipped_not_anchor"
