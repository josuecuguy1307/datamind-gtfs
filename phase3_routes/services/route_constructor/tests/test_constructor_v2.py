from __future__ import annotations

from unittest.mock import Mock, patch

from src.constructor_v2.clients.valhalla_client import ValhallaClient
from src.constructor_v2.constants import DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL
from src.constructor_v2.input_normalizer import normalize_route_input
from src.constructor_v2.ordering.local_refiner import LocalRefiner
from src.constructor_v2.ordering.matrix_builder import MatrixResult
from src.constructor_v2.ordering.ortools_solver import ORToolsFixedEndSolver
from src.constructor_v2.schemas.route_input import NormalizedStop, RouteInput
from src.constructor_v2.schemas.route_output import ConfidenceResult, OrderProposal, ValidationResult
from src.constructor_v2.topology import apply_topology_confidence_policy, canonical_topology
from src.constructor_v2.validation.confidence_scorer import score_confidence
from src.constructor_v2.validation.corridor_validator import validate_corridor_consistency
from src.constructor_v2.validation.monotonicity_validator import validate_monotonic_progress
from src.constructor_v2.validation.repeated_segment_validator import validate_repeated_segments


def test_normalizer_clusters_duplicate_stop_ids() -> None:
    route_input = RouteInput.from_stop_rows(
        route="Test Route",
        cooperative="Test Coop",
        route_type="lineal",
        stop_rows=[
            {"seq": 1, "stop_id": "start", "stop_name": "Start", "lat": 0.0, "lon": 0.0, "on_route_score": 0.9},
            {"seq": 2, "stop_id": "dup", "stop_name": "Midpoint", "lat": 0.0, "lon": 0.001, "on_route_score": 0.5},
            {"seq": 3, "stop_id": "dup", "stop_name": "Midpoint", "lat": 0.0, "lon": 0.00101, "on_route_score": 0.4},
            {"seq": 4, "stop_id": "end", "stop_name": "End", "lat": 0.0, "lon": 0.002, "on_route_score": 0.9},
        ],
    )

    result = normalize_route_input(route_input)

    assert len(result.normalized_stops) == 3
    assert result.normalized_stops[1].source_stop_ids == ("dup", "dup")
    assert len(result.diagnostics["collapsed_duplicate_groups"]) == 1


def test_ortools_solver_can_drop_weak_optional_stop() -> None:
    stops = [
        NormalizedStop(1, "start", "Start", 0.0, 0.0, ("start",), (1,), ("Start",), 1.0, False, None, is_fixed_start=True),
        NormalizedStop(2, "weak", "Weak", 1.0, 1.0, ("weak",), (2,), ("Weak",), 0.1, True, 20),
        NormalizedStop(3, "mid", "Mid", 0.0, 0.001, ("mid",), (3,), ("Mid",), 0.8, False, None),
        NormalizedStop(4, "end", "End", 0.0, 0.002, ("end",), (4,), ("End",), 1.0, False, None, is_fixed_end=True),
    ]
    matrix = MatrixResult(
        stop_ids=tuple(stop.stop_id for stop in stops),
        distance_matrix_m=[
            [0.0, 1000.0, 100.0, 200.0],
            [1000.0, 0.0, 1000.0, 1000.0],
            [100.0, 1000.0, 0.0, 100.0],
            [200.0, 1000.0, 100.0, 0.0],
        ],
        duration_matrix_s=[
            [0.0, 1000.0, 100.0, 200.0],
            [1000.0, 0.0, 1000.0, 1000.0],
            [100.0, 1000.0, 0.0, 100.0],
            [200.0, 1000.0, 100.0, 0.0],
        ],
        engine="test",
        complete=True,
    )

    proposal = ORToolsFixedEndSolver().solve(stops, matrix)

    assert proposal.ordered_stop_ids == ("start", "mid", "end")
    assert proposal.skipped_stop_ids == ("weak",)


def test_local_refiner_repairs_simple_bad_swap() -> None:
    stops_by_id = {
        stop.stop_id: stop
        for stop in [
            NormalizedStop(1, "start", "Start", 0.0, 0.0, ("start",), (1,), ("Start",), 1.0, False, None, is_fixed_start=True),
            NormalizedStop(2, "a", "A", 0.0, 0.001, ("a",), (2,), ("A",), 0.8, False, None),
            NormalizedStop(3, "b", "B", 0.0, 0.002, ("b",), (3,), ("B",), 0.8, False, None),
            NormalizedStop(4, "end", "End", 0.0, 0.003, ("end",), (4,), ("End",), 1.0, False, None, is_fixed_end=True),
        ]
    }
    matrix = MatrixResult(
        stop_ids=("start", "a", "b", "end"),
        distance_matrix_m=[
            [0.0, 100.0, 300.0, 500.0],
            [100.0, 0.0, 100.0, 300.0],
            [300.0, 100.0, 0.0, 100.0],
            [500.0, 300.0, 100.0, 0.0],
        ],
        duration_matrix_s=[
            [0.0, 100.0, 300.0, 500.0],
            [100.0, 0.0, 100.0, 300.0],
            [300.0, 100.0, 0.0, 100.0],
            [500.0, 300.0, 100.0, 0.0],
        ],
        engine="test",
        complete=True,
    )
    proposal = OrderProposal(
        method="seed",
        ordered_stop_ids=("start", "b", "a", "end"),
        objective_value=700.0,
        objective_unit="duration",
    )

    refined = LocalRefiner().refine(stops_by_id, proposal, matrix)

    assert refined.ordered_stop_ids == ("start", "a", "b", "end")


def test_monotonicity_validator_flags_regression() -> None:
    stops = [
        NormalizedStop(1, "start", "Start", 0.0, 0.0, ("start",), (1,), ("Start",), 1.0, False, None, is_fixed_start=True),
        NormalizedStop(2, "mid", "Mid", 0.0, 0.002, ("mid",), (2,), ("Mid",), 0.8, False, None),
        NormalizedStop(3, "back", "Back", 0.0, 0.001, ("back",), (3,), ("Back",), 0.8, False, None),
        NormalizedStop(4, "end", "End", 0.0, 0.003, ("end",), (4,), ("End",), 1.0, False, None, is_fixed_end=True),
    ]

    result = validate_monotonic_progress(stops, [(0.0, 0.0), (0.003, 0.0)])

    assert result.status == "fail"
    assert result.metrics["regression_count"] == 1


def test_valhalla_client_defaults_to_dedicated_constructor_v2_url() -> None:
    client = ValhallaClient()
    assert client.base_url == DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL


def test_valhalla_client_parses_available_actions_from_status_error_hint() -> None:
    response = Mock()
    response.status_code = 404
    response.json.return_value = {"error": "Try any of: '/route' '/optimized_route' '/sources_to_targets' '/locate' "}

    with patch("src.constructor_v2.clients.valhalla_client.requests.Session.get", return_value=response):
        client = ValhallaClient(base_url="http://127.0.0.1:9999")
        capabilities = client.refresh_capabilities(force=True)

    assert capabilities["/route"] is True
    assert capabilities["/optimized_route"] is True
    assert capabilities["/sources_to_targets"] is True
    assert capabilities["/locate"] is True


def test_confidence_scorer_keeps_proxy_failures_out_of_manual_review_without_core_backtracking() -> None:
    validations = {
        "anchor_respect": ValidationResult("anchor_respect", "pass", 1.0),
        "detour_ratio": ValidationResult("detour_ratio", "fail", 0.86, issues=["detour too high"]),
        "monotonic_progress": ValidationResult("monotonic_progress", "pass", 1.0),
        "duplicate_stops": ValidationResult("duplicate_stops", "pass", 1.0),
        "corridor_consistency": ValidationResult("corridor_consistency", "fail", 0.45, issues=["corridor switched"]),
        "repeated_segments": ValidationResult("repeated_segments", "fail", 0.40, issues=["segment overlap"]),
    }

    result = score_confidence(validations)

    assert result.label == "weak"
    assert result.auto_accept is False
    assert result.metrics["core_fail_count"] == 1
    assert result.metrics["proxy_fail_count"] == 2


def test_confidence_scorer_requires_manual_review_for_monotonic_failure() -> None:
    validations = {
        "anchor_respect": ValidationResult("anchor_respect", "pass", 1.0),
        "detour_ratio": ValidationResult("detour_ratio", "warn", 0.80, issues=["detour elevated"]),
        "monotonic_progress": ValidationResult("monotonic_progress", "fail", 0.0, issues=["backtracked"]),
        "duplicate_stops": ValidationResult("duplicate_stops", "pass", 1.0),
        "corridor_consistency": ValidationResult("corridor_consistency", "warn", 0.5, issues=["switches"]),
        "repeated_segments": ValidationResult("repeated_segments", "warn", 0.6, issues=["overlap"]),
    }

    result = score_confidence(validations)

    assert result.label == "needs_manual_review"
    assert result.auto_accept is False


def test_corridor_validator_warns_without_zeroing_score_for_borderline_switches() -> None:
    stops = [
        NormalizedStop(1, "s1", "S1", 0.0, 0.0, ("s1",), (1,), ("S1",), 1.0, False, None, is_fixed_start=True),
        NormalizedStop(2, "s2", "S2", 0.0, 0.001, ("s2",), (2,), ("S2",), 0.8, False, None),
        NormalizedStop(3, "s3", "S3", 0.0, 0.002, ("s3",), (3,), ("S3",), 0.8, False, None),
        NormalizedStop(4, "s4", "S4", 0.001, 0.002, ("s4",), (4,), ("S4",), 0.8, False, None),
        NormalizedStop(5, "s5", "S5", 0.001, 0.003, ("s5",), (5,), ("S5",), 0.8, False, None),
        NormalizedStop(6, "s6", "S6", 0.001, 0.004, ("s6",), (6,), ("S6",), 0.8, False, None),
        NormalizedStop(7, "s7", "S7", 0.002, 0.004, ("s7",), (7,), ("S7",), 0.8, False, None),
        NormalizedStop(8, "s8", "S8", 0.002, 0.005, ("s8",), (8,), ("S8",), 0.8, False, None),
        NormalizedStop(9, "s9", "S9", 0.002, 0.006, ("s9",), (9,), ("S9",), 1.0, False, None, is_fixed_end=True),
    ]

    result = validate_corridor_consistency(stops, route_type="lineal")

    assert result.status == "warn"
    assert result.score > 0.0


def test_repeated_segment_validator_treats_moderate_overlap_as_warning() -> None:
    geometry = [
        (0.0, 0.0),
        (0.001, 0.0),
        (0.002, 0.0),
        (0.003, 0.0),
        (0.002, 0.0),
        (0.004, 0.0),
        (0.005, 0.0),
        (0.006, 0.0),
        (0.007, 0.0),
        (0.001, 0.0),
    ]

    result = validate_repeated_segments(geometry, route_type="lineal")

    assert result.status == "warn"
    assert 0.0 < result.score < 0.8


def test_canonical_topology_normalizes_internal_linear_to_linear() -> None:
    assert canonical_topology("lineal") == "linear"
    assert canonical_topology("internal_linear") == "linear"
    assert canonical_topology("internal_loop") == "internal_loop"
    assert canonical_topology("possible_circular") == "possible_circular"


def test_topology_policy_keeps_edge_cases_out_of_auto_accept() -> None:
    confidence = ConfidenceResult(
        label="strong",
        score=94.0,
        auto_accept=True,
        reasons=["all validators passed"],
        metrics={},
    )

    adjusted = apply_topology_confidence_policy(confidence, "possible_circular")

    assert adjusted.label == "ambiguous"
    assert adjusted.auto_accept is False
    assert adjusted.metrics["topology_special_case"] is True
