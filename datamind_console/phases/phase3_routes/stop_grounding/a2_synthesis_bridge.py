"""
a2_synthesis_bridge — Stage A2 wiring between typed-token grounding and
path-aware synthesis.

The typed grounding stage (A) produces a ``TypedGroundingResult`` keyed by
token label. Tokens that resolved (``best_candidate is not None``) are handed
downstream to corridor construction; tokens that did not resolve currently
fall out of the pipeline silently. Stage A2's job is to pick those
unresolved *anchor* tokens up and attempt synthesis through
``synthesis.core.path_aware_synthesis``.

This module is deliberately thin:

* It does not re-run grounding or alter Stage A's outputs.
* It does not consult any backfill module — if a backfill hook is added
  later, it plugs in here; for now A2 goes straight to synthesis.
* It does not commit anything itself. The DB mutations happen inside
  ``path_aware_synthesis`` on the connection passed through.

The function it exposes to the pipeline is
``run_a2_for_unresolved_anchors``, which returns a list of ``A2Report``
dicts the pipeline stores on ``summary.a2_reports``. For per-anchor unit
tests, ``invoke_a2_for_anchor`` is the single-anchor entry point.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    TypedGroundingResult,
    TypedRouteSeed,
    TypedSeedToken,
)
from datamind_console.phases.phase3_routes.synthesis import core as synth_core

log = logging.getLogger(__name__)


A2_OUTCOMES = {
    "synthesized",              # stage 3a/3b/3c/3d/4 produced a node
    "dropped_cap_route",        # route cap hit (3.0 weighted units)
    "dropped_cap_unit_week",    # unit-weekly cap hit (20 events / 7d)
    "dropped_precision",        # research_coords had < 4 decimal digits
    "dropped_all_failed",       # ladder walked; nothing succeeded
    "skipped_already_grounded", # token had best_candidate — no A2 needed
    "skipped_not_anchor",       # token role/policy disqualifies it
    "skipped_no_polyline",      # corridor had no polyline to snap against
    "error",                    # synthesis raised; recorded, not rethrown
}


@dataclass
class A2Decision:
    """Per-anchor outcome of an A2 attempt. Pure data — safe to serialize."""
    anchor_name: str
    outcome: str
    stage: Optional[str] = None
    node_id: Optional[str] = None
    osm_id: Optional[int] = None
    synthetic_confidence: Optional[str] = None
    semantic_spatial_conflict: bool = False
    rejected_reason: Optional[str] = None
    review_path: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "anchor_name": self.anchor_name,
            "outcome": self.outcome,
            "stage": self.stage,
            "node_id": self.node_id,
            "osm_id": self.osm_id,
            "synthetic_confidence": self.synthetic_confidence,
            "semantic_spatial_conflict": self.semantic_spatial_conflict,
            "rejected_reason": self.rejected_reason,
            "review_path": self.review_path,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# single-anchor entry point
# ---------------------------------------------------------------------------


def invoke_a2_for_anchor(
    *,
    anchor_name: str,
    research_coords: Optional[Tuple[float, float]],
    road_tokens: Sequence[str],
    research_output_file: Optional[str],
    route_id: str,
    route_code: str,
    unit: str,
    province: str,
    termini: Sequence[Tuple[float, float]],
    osm_relation_id: Optional[int],
    grounded_stop_coords: Sequence[Tuple[float, float]],
    conn,
    review_root: Path,
    research_queue_root: Optional[Path],
    synthesis_fn: Optional[Callable] = None,
) -> A2Decision:
    """Run path-aware synthesis for one unresolved anchor and wrap the
    result in an :class:`A2Decision`.

    ``synthesis_fn`` defaults to ``synth_core.path_aware_synthesis`` and is
    only overridden by tests.
    """
    fn = synthesis_fn or synth_core.path_aware_synthesis

    anchor = synth_core.Anchor(
        name=anchor_name,
        research_coords=tuple(research_coords) if research_coords else None,
        road_tokens=tuple(road_tokens or ()),
        research_output_file=research_output_file,
    )
    route = synth_core.Route(
        route_id=route_id,
        route_code=route_code,
        unit=unit,
        province=province,
        termini=tuple((float(a), float(b)) for (a, b) in termini),
        osm_relation_id=osm_relation_id,
        grounded_stop_coords=tuple(
            (float(a), float(b)) for (a, b) in grounded_stop_coords
        ),
    )

    try:
        result = fn(
            anchor, route,
            conn=conn,
            review_root=Path(review_root),
            research_queue_root=Path(research_queue_root)
            if research_queue_root else None,
        )
    except Exception as exc:
        log.error(
            "A2 synthesis raised for anchor=%r route=%s: %s",
            anchor_name, route_code, exc,
        )
        return A2Decision(
            anchor_name=anchor_name,
            outcome="error",
            error=str(exc),
        )

    return _wrap_result(anchor_name, result)


def _wrap_result(anchor_name: str, result) -> A2Decision:
    if result.success:
        return A2Decision(
            anchor_name=anchor_name,
            outcome="synthesized",
            stage=result.stage,
            node_id=result.node_id,
            osm_id=result.osm_id,
            synthetic_confidence=result.synthetic_confidence,
            semantic_spatial_conflict=bool(result.semantic_spatial_conflict),
            review_path=str(result.review_path) if result.review_path else None,
        )

    reason = result.rejected_reason
    outcome_map = {
        "route_synthesis_cap_hit": "dropped_cap_route",
        "unit_weekly_cap_hit": "dropped_cap_unit_week",
        "research_coords_low_precision": "dropped_precision",
        "all_stages_failed": "dropped_all_failed",
    }
    outcome = outcome_map.get(reason or "", "dropped_all_failed")
    return A2Decision(
        anchor_name=anchor_name,
        outcome=outcome,
        stage=result.stage,
        rejected_reason=reason,
    )


# ---------------------------------------------------------------------------
# pipeline-facing: walk the typed grounding and fire A2 where needed
# ---------------------------------------------------------------------------


def _is_a2_candidate(token: TypedSeedToken) -> bool:
    """True iff the token represents an anchor that should attempt A2 when
    unresolved. Corridor-only constraints and non-anchor roles are skipped.
    """
    if token.resolution_policy == "use_as_corridor_constraint_only":
        return False
    return token.kind in {"stop_candidate", "anchor", "terminal"} or bool(
        token.anchor_role in {"terminus", "waypoint"}
    )


@dataclass
class A2RunInputs:
    """Bundle of route-scoped values the pipeline already has on hand.

    ``grounded_stop_coords`` is pipeline-populated: callers pass ``()``
    (or ``[]``) and the discovery pipeline fills it from the just-computed
    ``typed_grounding`` before invoking ``run_a2_for_unresolved_anchors``.
    Non-empty values from callers are respected as overrides (used by
    tests that want to inject a synthetic grounding context without
    running Stage A).
    """
    route_id: str
    route_code: str
    unit: str
    province: str
    termini: Sequence[Tuple[float, float]]
    osm_relation_id: Optional[int]
    grounded_stop_coords: Sequence[Tuple[float, float]]
    review_root: Path
    research_queue_root: Optional[Path]
    # Optional: override anchor-token → (research_coords, road_tokens,
    # research_output_file) for routes whose Deep Research response carries
    # richer anchor metadata than the typed seed. Keyed by token label.
    anchor_payloads: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def run_a2_for_unresolved_anchors(
    *,
    typed_grounding: TypedGroundingResult,
    typed_seed: TypedRouteSeed,
    inputs: A2RunInputs,
    conn,
    synthesis_fn: Optional[Callable] = None,
) -> List[A2Decision]:
    """Scan typed_grounding for unresolved anchor tokens and fire A2 on
    each. Tokens already grounded, corridor-only tokens, and non-anchor
    tokens are skipped with a ``skipped_*`` decision for auditability.

    Returns the full list in token order (skips included) so the pipeline
    can log a complete trace, not just the synthesis attempts.
    """
    decisions: List[A2Decision] = []
    token_groundings = typed_grounding.token_groundings or {}

    for token in typed_seed.sequence_tokens:
        if not _is_a2_candidate(token):
            decisions.append(A2Decision(
                anchor_name=token.label,
                outcome="skipped_not_anchor",
            ))
            continue

        grounding = token_groundings.get(token.label)
        if grounding is not None and grounding.best_candidate is not None:
            decisions.append(A2Decision(
                anchor_name=token.label,
                outcome="skipped_already_grounded",
            ))
            continue

        payload = inputs.anchor_payloads.get(token.label, {})
        research_coords = payload.get("research_coords")
        if research_coords is None and token.has_anchor_coords:
            research_coords = (token.anchor_lat, token.anchor_lon)

        decision = invoke_a2_for_anchor(
            anchor_name=token.label,
            research_coords=research_coords,
            road_tokens=payload.get("road_tokens") or (),
            research_output_file=payload.get("research_output_file"),
            route_id=inputs.route_id,
            route_code=inputs.route_code,
            unit=inputs.unit,
            province=inputs.province,
            termini=inputs.termini,
            osm_relation_id=inputs.osm_relation_id,
            grounded_stop_coords=inputs.grounded_stop_coords,
            conn=conn,
            review_root=inputs.review_root,
            research_queue_root=inputs.research_queue_root,
            synthesis_fn=synthesis_fn,
        )
        decisions.append(decision)

    return decisions
