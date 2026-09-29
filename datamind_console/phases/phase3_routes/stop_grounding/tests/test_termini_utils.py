"""Unit tests for termini_utils.extract_termini."""
from __future__ import annotations

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    TypedRouteSeed,
    TypedSeedToken,
)
from datamind_console.phases.phase3_routes.stop_grounding.termini_utils import (
    extract_termini,
)


def _tok(label: str, *, role: str, lat: float | None, lon: float | None,
         kind: str = "stop_candidate", position: int = 0) -> TypedSeedToken:
    return TypedSeedToken(
        label=label,
        kind=kind,
        role=role,
        resolution_policy="resolve_to_best_matching_stop_node",
        confidence="high",
        position=position,
        anchor_lat=lat,
        anchor_lon=lon,
    )


def _seed(tokens: list[TypedSeedToken]) -> TypedRouteSeed:
    return TypedRouteSeed(
        route_name="TEST",
        cooperative=None,
        sequence_tokens=tokens,
        corridor_constraints=[],
        localities=[],
    )


def test_happy_path_explicit_roles():
    seed = _seed([
        _tok("Origin", role="origin", lat=-0.10, lon=-78.40, kind="terminal_candidate", position=1),
        _tok("Middle", role="intermediate_anchor", lat=-0.15, lon=-78.45, position=2),
        _tok("Dest", role="destination", lat=-0.20, lon=-78.50, kind="terminal_candidate", position=3),
    ])
    termini = extract_termini(seed)
    assert termini == ((-0.10, -78.40), (-0.20, -78.50))


def test_positional_fallback_when_roles_generic():
    # Roles are the generic intake defaults (origin_stop_candidate / destination_stop_candidate)
    seed = _seed([
        _tok("A", role="origin_stop_candidate", lat=-0.11, lon=-78.41, position=1),
        _tok("B", role="intermediate_anchor", lat=-0.12, lon=-78.42, position=2),
        _tok("C", role="destination_stop_candidate", lat=-0.13, lon=-78.43, position=3),
    ])
    termini = extract_termini(seed)
    assert termini == ((-0.11, -78.41), (-0.13, -78.43))


def test_missing_origin_coords_uses_first_coord_token():
    seed = _seed([
        _tok("NoCoords", role="origin", lat=None, lon=None, position=1),
        _tok("Middle", role="intermediate_anchor", lat=-0.15, lon=-78.45, position=2),
        _tok("Dest", role="destination", lat=-0.20, lon=-78.50, position=3),
    ])
    termini = extract_termini(seed)
    # Falls back: origin = first coord-bearing token (Middle), destination = Dest (still matches role)
    assert termini == ((-0.15, -78.45), (-0.20, -78.50))


def test_fewer_than_two_coord_tokens_returns_empty():
    seed = _seed([
        _tok("OnlyOne", role="origin", lat=-0.10, lon=-78.40, position=1),
        _tok("NoCoords", role="destination", lat=None, lon=None, position=2),
    ])
    assert extract_termini(seed) == ()


def test_empty_seed_returns_empty():
    assert extract_termini(_seed([])) == ()
