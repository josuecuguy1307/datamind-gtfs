"""Pipeline-level test for the grounded_stop_coords auto-fill behavior.

The discovery_pipeline populates `a2_inputs.grounded_stop_coords` from
the just-computed typed_grounding result if the caller passed an empty
sequence. Callers don't have this data at construction time, so the
pipeline is the honest place to produce it.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from datamind_console.phases.phase3_routes.stop_grounding import (
    a2_synthesis_bridge as bridge,
    discovery_pipeline as dp,
)
from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    StopMatch,
    TypedGroundingResult,
    TypedRouteSeed,
    TypedSeedToken,
    TypedTokenGrounding,
)


def _tok(label: str, *, role: str = "intermediate_anchor") -> TypedSeedToken:
    return TypedSeedToken(
        label=label,
        kind="stop_candidate",
        role=role,
        resolution_policy="resolve_to_best_matching_stop_node",
        confidence="high",
        position=0,
    )


def _grounding_with_one_resolved() -> TypedGroundingResult:
    resolved = StopMatch(
        stop_id="stop-A",
        stop_name="A",
        lat=-0.18,
        lon=-78.47,
        composite_score=1.0,
    )
    token_groundings = {
        "A": TypedTokenGrounding(
            token=_tok("A"),
            candidates=[resolved],
            best_candidate=resolved,
            resolution_method="db_stop",
            is_resolved=True,
        ),
        "B": TypedTokenGrounding(
            token=_tok("B"),
            candidates=[],
            best_candidate=None,
            resolution_method="unresolved",
            is_resolved=False,
        ),
    }
    return TypedGroundingResult(
        token_groundings=token_groundings,
        corridor_constraints=[],
        overall_confidence=0.5,
    )


def _seed() -> TypedRouteSeed:
    return TypedRouteSeed(
        route_name="TEST",
        cooperative=None,
        sequence_tokens=[_tok("A"), _tok("B")],
        corridor_constraints=[],
        localities=[],
        province="sample_region",
    )


def _a2_inputs(tmp_path: Path, *, grounded_stop_coords) -> bridge.A2RunInputs:
    return bridge.A2RunInputs(
        route_id="r1",
        route_code="TEST",
        unit="unit1",
        province="sample_region",
        termini=((-0.18, -78.47), (-0.19, -78.48)),
        osm_relation_id=None,
        grounded_stop_coords=grounded_stop_coords,
        review_root=tmp_path / "review",
        research_queue_root=tmp_path / "rq",
    )


def _run_until_a2(tmp_path, monkeypatch, a2_in, *, grounding):
    """Invoke run_discovery_pipeline just far enough to hit the A2 block.
    Captures the inputs that run_a2_for_unresolved_anchors receives."""
    monkeypatch.setattr(dp, "ground_typed_seed", lambda *a, **kw: grounding)
    monkeypatch.setattr(dp, "token_dispatch_log", lambda *a, **kw: {})
    captured: dict = {}

    def fake_run_a2(*, typed_grounding, typed_seed, inputs, conn, **kw):
        captured["inputs"] = inputs
        return []

    monkeypatch.setattr(dp, "run_a2_for_unresolved_anchors", fake_run_a2)
    # Force resolve_coherent_hint_set to return (None, []) so the pipeline
    # short-circuits right after A2 and we don't drag in corridor build.
    monkeypatch.setattr(dp, "resolve_coherent_hint_set", lambda *a, **kw: (None, []))

    fake_conn = MagicMock()
    dp.run_discovery_pipeline(
        _seed(),
        artifact_dir=str(tmp_path / "artifacts"),
        conn=fake_conn,
        enable_a2=True,
        a2_inputs=a2_in,
    )
    return captured


def test_empty_grounded_stop_coords_populated_from_typed_grounding(tmp_path, monkeypatch):
    a2_in = _a2_inputs(tmp_path, grounded_stop_coords=[])
    captured = _run_until_a2(
        tmp_path, monkeypatch, a2_in,
        grounding=_grounding_with_one_resolved(),
    )
    assert "inputs" in captured, "A2 was not invoked"
    coords = captured["inputs"].grounded_stop_coords
    assert coords == ((-0.18, -78.47),), f"got {coords!r}"


def test_nonempty_grounded_stop_coords_respected_as_override(tmp_path, monkeypatch):
    override = ((-0.99, -79.99),)
    a2_in = _a2_inputs(tmp_path, grounded_stop_coords=override)
    captured = _run_until_a2(
        tmp_path, monkeypatch, a2_in,
        grounding=_grounding_with_one_resolved(),
    )
    assert captured["inputs"].grounded_stop_coords == override
