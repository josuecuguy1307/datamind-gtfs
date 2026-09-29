"""Phase 4.5 stage 5 — final state to direction_id mapping.

Maps a terminal state (per the four-state contract) to a concrete
``direction_id`` value. This is the only place that converts states to
GTFS direction values; Phase 5 just reads what is committed.

Per the skill (workspace/skills/direction_construction.md):

- ``paired``           → both routes get 0/1 by geographic convention
                         (north/east = 0, south/west = 1)
- ``synthesized``      → original = 0, synthetic = 1
- ``dr_confirmed_mono``→ direction_id = 0
- ``operator_pending`` → direction_id = NULL (stays out of Phase 5 export)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

PAIRED = "paired"
SYNTHESIZED = "synthesized"
DR_CONFIRMED_MONO = "dr_confirmed_mono"
OPERATOR_PENDING = "operator_pending"

TERMINAL_STATES = frozenset({PAIRED, SYNTHESIZED, DR_CONFIRMED_MONO, OPERATOR_PENDING})


@dataclass(frozen=True)
class DirectionAssignment:
    """Result of mapping a terminal state to a direction_id."""

    state: str
    direction_id: Optional[int]
    rationale: str


def assign_for_pair(
    state: str,
    *,
    route_a_endpoints: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None,
    route_b_endpoints: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None,
) -> Tuple[DirectionAssignment, DirectionAssignment]:
    """Assign direction_id to both halves of a paired route.

    Geographic convention: 0 = the route whose start→end vector points
    more to the north or east; 1 = the other. Endpoints are ``((lat, lon),
    (lat, lon))`` for ``(start, end)``.
    """
    if state != PAIRED:
        raise ValueError(f"assign_for_pair requires state='paired', got {state!r}")
    if route_a_endpoints is None or route_b_endpoints is None:
        raise ValueError("paired assignment requires both route endpoints")

    a_start, a_end = route_a_endpoints
    a_dlat = a_end[0] - a_start[0]
    a_dlon = a_end[1] - a_start[1]
    a_score = a_dlat + a_dlon

    b_start, b_end = route_b_endpoints
    b_dlat = b_end[0] - b_start[0]
    b_dlon = b_end[1] - b_start[1]
    b_score = b_dlat + b_dlon

    a_is_zero = a_score >= b_score
    a = DirectionAssignment(
        state=PAIRED,
        direction_id=0 if a_is_zero else 1,
        rationale=(
            f"paired-geographic-convention: a_score={a_score:.6f}, b_score={b_score:.6f}"
        ),
    )
    b = DirectionAssignment(
        state=PAIRED,
        direction_id=1 if a_is_zero else 0,
        rationale=a.rationale,
    )
    return a, b


def assign_for_synthesized() -> Tuple[DirectionAssignment, DirectionAssignment]:
    original = DirectionAssignment(
        state=SYNTHESIZED,
        direction_id=0,
        rationale="synthesized: original direction = 0",
    )
    synthetic = DirectionAssignment(
        state=SYNTHESIZED,
        direction_id=1,
        rationale="synthesized: synthetic reverse direction = 1",
    )
    return original, synthetic


def assign_for_mono() -> DirectionAssignment:
    return DirectionAssignment(
        state=DR_CONFIRMED_MONO,
        direction_id=0,
        rationale="dr_confirmed_mono: single-direction service, direction_id = 0",
    )


def assign_for_pending() -> DirectionAssignment:
    return DirectionAssignment(
        state=OPERATOR_PENDING,
        direction_id=None,
        rationale="operator_pending: score in [PLAUSIBLE, RELIABLE) — no direction_id written",
    )
