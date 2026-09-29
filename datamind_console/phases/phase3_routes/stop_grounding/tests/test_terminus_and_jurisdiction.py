"""
Tests for MEJORA 1 (terminus protection) and MEJORA 2 (jurisdiction filtering).
"""
import sys
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "datamind_console").exists())
sys.path.insert(0, str(ROOT))

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    CorridorStopCandidate,
    TypedSeedToken,
)
from datamind_console.phases.phase3_routes.stop_grounding.on_route_classifier import (
    UNPRUNEABLE_ROLES,
    heuristic_on_route_score,
    infer_terminus_type,
    is_terminus_protected,
    score_candidates,
)
from datamind_console.phases.phase3_routes.stop_grounding.corridor_stop_intersector import (
    infer_stop_source,
)


# ============================================================================
# MEJORA 1 — Terminus protection tests
# ============================================================================


def _make_candidate(
    stop_id: str = "stop_1",
    in_expected_geography: bool = False,
    in_forbidden_area: bool = False,
    is_known_anchor: bool = False,
    distance_to_corridor_m: float = 30.0,
    path_fraction: float = 0.95,
    **kwargs,
) -> CorridorStopCandidate:
    return CorridorStopCandidate(
        stop_id=stop_id,
        stop_name=kwargs.get("stop_name", "Test Stop"),
        lat=kwargs.get("lat", -0.33),
        lon=kwargs.get("lon", -78.45),
        distance_to_corridor_m=distance_to_corridor_m,
        path_fraction=path_fraction,
        discovery_buffer_m=50,
        in_expected_geography=in_expected_geography,
        in_forbidden_area=in_forbidden_area,
        is_known_anchor=is_known_anchor,
        operator_match=kwargs.get("operator_match", False),
        locality_match=kwargs.get("locality_match", False),
        bearing_alignment_deg=kwargs.get("bearing_alignment_deg", 45.0),
        locality_consistency_score=kwargs.get("locality_consistency_score", 0.5),
        stop_usage_frequency=kwargs.get("stop_usage_frequency", 2.0),
        distance_to_envelope_m=kwargs.get("distance_to_envelope_m", 300.0),
    )


def test_endpoint_not_pruned_when_is_conflicting():
    """
    Test 1: A waypoint with role="end" (destination_stop_candidate) is NOT
    pruned even when it's outside expected geography (is_conflicting=True
    equivalent in this codebase).
    """
    # Candidate outside expected geography — would normally be rejected
    candidate = _make_candidate(
        stop_id="terminus_loreto",
        stop_name="Parada de bus Loreto",
        in_expected_geography=False,
        in_forbidden_area=True,
        path_fraction=0.99,
    )

    # Score WITHOUT terminus protection → severely penalized
    score_unprotected = heuristic_on_route_score(candidate, terminus_protected=False)

    # Score WITH terminus protection → mild penalty only
    score_protected = heuristic_on_route_score(candidate, terminus_protected=True)

    # Protected score should be significantly higher
    assert score_protected > score_unprotected * 3, (
        f"Protected score ({score_protected:.4f}) should be much higher "
        f"than unprotected ({score_unprotected:.4f})"
    )

    # Now test through score_candidates: terminus-protected stops go to probable
    candidates = [
        candidate,
        _make_candidate(stop_id="normal_1", in_expected_geography=True, path_fraction=0.5, lat=-0.28, lon=-78.50),
    ]
    probable, marginal, rejected = score_candidates(
        candidates,
        scoring_mode="heuristic",
        terminus_protected_ids={"terminus_loreto"},
    )

    probable_ids = {s.stop_id for s in probable}
    rejected_ids = {s.stop_id for s in rejected}

    assert "terminus_loreto" in probable_ids, (
        f"Terminus stop should be in probable, but found in: "
        f"probable={probable_ids}, rejected={rejected_ids}"
    )
    assert "terminus_loreto" not in rejected_ids


def test_non_terminus_still_pruned_outside_geography():
    """
    Regular (non-terminus) stops outside expected geography should still be
    rejected as before — no regression.
    """
    candidate = _make_candidate(
        stop_id="random_stop",
        in_expected_geography=False,
        path_fraction=0.5,
    )
    candidates = [candidate]

    probable, marginal, rejected = score_candidates(
        candidates,
        scoring_mode="heuristic",
        terminus_protected_ids=set(),
    )

    assert len(rejected) == 1
    assert rejected[0].stop_id == "random_stop"


def test_infer_terminus_type_formal_terminal():
    token = TypedSeedToken(
        label="Terminal Calsig Express",
        kind="terminal_confirmed",
        role="origin_stop_candidate",
        resolution_policy="resolve_to_best_matching_terminal_node",
        confidence="high",
    )
    assert infer_terminus_type(token) == "formal_terminal"


def test_infer_terminus_type_neighborhood_endpoint():
    token = TypedSeedToken(
        label="Barrio Central",
        kind="stop_candidate",
        role="destination_stop_candidate",
        resolution_policy="resolve_to_best_matching_stop_node",
        confidence="medium",
    )
    assert infer_terminus_type(token) == "neighborhood_endpoint"


def test_infer_terminus_type_street_terminus():
    token = TypedSeedToken(
        label="Miranda",
        kind="stop_candidate",
        role="destination_stop_candidate",
        resolution_policy="resolve_to_best_matching_stop_node",
        confidence="medium",
    )
    assert infer_terminus_type(token) == "street_terminus"


def test_infer_terminus_type_explicit_override():
    token = TypedSeedToken(
        label="Test",
        kind="stop_candidate",
        role="origin_stop_candidate",
        resolution_policy="resolve_to_best_matching_stop_node",
        confidence="medium",
        terminus_type="neighborhood_endpoint",
    )
    assert infer_terminus_type(token) == "neighborhood_endpoint"


def test_is_terminus_protected_origin():
    token = TypedSeedToken(
        label="Loreto",
        kind="stop_candidate",
        role="origin_stop_candidate",
        resolution_policy="resolve_to_best_matching_stop_node",
        confidence="high",
    )
    assert is_terminus_protected(token) is True


def test_is_terminus_protected_intermediate_with_generic_policy_not_protected():
    """An intermediate with a non-unpruneable policy is NOT protected."""
    token = TypedSeedToken(
        label="El Choclo",
        kind="stop_candidate",
        role="intermediate_anchor",
        resolution_policy="resolve_to_nearest_road_node",
        confidence="medium",
    )
    assert is_terminus_protected(token) is False


# ============================================================================
# MEJORA 2 — Jurisdiction filtering tests
# ============================================================================


def test_infer_stop_source_dmq_by_ref():
    assert infer_stop_source(lat=-0.30, lon=-78.50, ref="Q-1234", operator="") == "DMQ"


def test_infer_stop_source_dmq_by_operator():
    assert infer_stop_source(lat=-0.30, lon=-78.50, ref="", operator="EPMMOP Quito") == "DMQ"


def test_infer_stop_source_ant_by_location():
    """Stop inside Rumiñahui canton → ANT."""
    assert infer_stop_source(lat=-0.33, lon=-78.44, ref="", operator="") == "ANT"


def test_infer_stop_source_unknown():
    """Stop outside Rumiñahui with no DMQ indicators → None."""
    assert infer_stop_source(lat=-0.28, lon=-78.52, ref="X-999", operator="Private") is None


def test_ant_route_excludes_dmq_stops():
    """
    Test 2: A route with jurisdiction="ANT" should NOT receive DMQ stops
    as candidates. We test via infer_stop_source + manual filtering logic.
    """
    # Simulate candidates
    stops = [
        {"stop_id": "ant_1", "lat": -0.33, "lon": -78.44, "ref": "", "operator": ""},  # ANT (in Rumiñahui)
        {"stop_id": "dmq_1", "lat": -0.30, "lon": -78.50, "ref": "Q-100", "operator": ""},  # DMQ (Q-ref)
        {"stop_id": "unk_1", "lat": -0.28, "lon": -78.50, "ref": "X-1", "operator": "Private"},  # Unknown
    ]

    jurisdiction = "ANT"
    accepted = []
    excluded = []
    for s in stops:
        source = infer_stop_source(
            lat=s["lat"], lon=s["lon"], ref=s["ref"], operator=s["operator"]
        )
        if jurisdiction != "both" and source is not None and source != jurisdiction:
            excluded.append(s["stop_id"])
        else:
            accepted.append(s["stop_id"])

    assert "ant_1" in accepted, "ANT stop should be accepted for ANT route"
    assert "dmq_1" in excluded, "DMQ stop should be excluded from ANT route"
    assert "unk_1" in accepted, "Unknown source stops pass through (not excluded)"


def test_both_jurisdiction_receives_all_stops():
    """
    Test 3: A route with jurisdiction="both" should receive stops from
    both DMQ and ANT sources.
    """
    stops = [
        {"stop_id": "ant_1", "lat": -0.33, "lon": -78.44, "ref": "", "operator": ""},
        {"stop_id": "dmq_1", "lat": -0.30, "lon": -78.50, "ref": "Q-100", "operator": ""},
        {"stop_id": "unk_1", "lat": -0.28, "lon": -78.50, "ref": "", "operator": ""},
    ]

    jurisdiction = "both"
    accepted = []
    for s in stops:
        source = infer_stop_source(
            lat=s["lat"], lon=s["lon"], ref=s["ref"], operator=s["operator"]
        )
        if jurisdiction != "both" and source is not None and source != jurisdiction:
            continue
        accepted.append(s["stop_id"])

    assert set(accepted) == {"ant_1", "dmq_1", "unk_1"}, (
        f"All stops should be accepted for 'both' jurisdiction, got {accepted}"
    )


def test_dmq_route_excludes_ant_stops():
    """DMQ route should exclude ANT-only stops."""
    stops = [
        {"stop_id": "ant_1", "lat": -0.33, "lon": -78.44, "ref": "", "operator": ""},
        {"stop_id": "dmq_1", "lat": -0.30, "lon": -78.50, "ref": "Q-200", "operator": ""},
    ]

    jurisdiction = "DMQ"
    accepted = []
    for s in stops:
        source = infer_stop_source(
            lat=s["lat"], lon=s["lon"], ref=s["ref"], operator=s["operator"]
        )
        if jurisdiction != "both" and source is not None and source != jurisdiction:
            continue
        accepted.append(s["stop_id"])

    assert "dmq_1" in accepted
    assert "ant_1" not in accepted
