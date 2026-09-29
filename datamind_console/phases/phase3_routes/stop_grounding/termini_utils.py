"""Shared helpers for deriving route-scoped inputs from a TypedRouteSeed.

Kept separate from a2_synthesis_bridge.py so the bridge stays a consumer,
not a producer, of termini.
"""
from __future__ import annotations

from typing import Tuple

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    TypedRouteSeed,
    TypedSeedToken,
)


def _is_origin(token: TypedSeedToken) -> bool:
    role = (token.role or "").lower()
    return role == "origin" or role.startswith("origin_") or role.endswith("_origin")


def _is_destination(token: TypedSeedToken) -> bool:
    role = (token.role or "").lower()
    return (
        role == "destination"
        or role.startswith("destination_")
        or role.endswith("_destination")
    )


def extract_termini(typed_seed: TypedRouteSeed) -> Tuple[Tuple[float, float], ...]:
    """Return (origin_coord, destination_coord) for a TypedRouteSeed.

    Picks the first token with origin-like role + anchor coords, and the last
    token with destination-like role + anchor coords. Falls back to the
    first and last coord-bearing tokens if explicit roles aren't present.

    Returns an empty tuple if fewer than two coord-bearing tokens exist.
    """
    tokens = list(typed_seed.sequence_tokens or [])
    if not tokens:
        return ()

    origin = next(
        (t for t in tokens if _is_origin(t) and t.has_anchor_coords),
        None,
    )
    destination = next(
        (t for t in reversed(tokens) if _is_destination(t) and t.has_anchor_coords),
        None,
    )

    if origin is None or destination is None:
        coord_tokens = [t for t in tokens if t.has_anchor_coords]
        if len(coord_tokens) < 2:
            return ()
        origin = origin or coord_tokens[0]
        destination = destination or coord_tokens[-1]

    if origin is destination:
        return ()

    return (
        (float(origin.anchor_lat), float(origin.anchor_lon)),
        (float(destination.anchor_lat), float(destination.anchor_lon)),
    )
