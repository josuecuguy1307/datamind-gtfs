"""Tests for CP-SAT circuit solver."""

from __future__ import annotations

import pytest

from src.constructor_v2.ordering.cpsat_solver import CPSATCircuitSolver, CPSATSolverConfig
from src.constructor_v2.ordering.matrix_builder import MatrixResult
from src.constructor_v2.schemas.route_input import NormalizedStop


def _make_stop(
    stop_id: str,
    name: str,
    lat: float,
    lon: float,
    *,
    is_fixed_start: bool = False,
    is_fixed_end: bool = False,
    is_known_anchor: bool = False,
    weak: bool = False,
    optional_penalty: int | None = None,
    duplicate_group_id: str | None = None,
) -> NormalizedStop:
    return NormalizedStop(
        order_hint=0,
        stop_id=stop_id,
        stop_name=name,
        lat=lat,
        lon=lon,
        source_stop_ids=(stop_id,),
        source_indices=(1,),
        source_names=(name,),
        representative_score=0.8,
        weak_candidate=weak,
        optional_penalty=optional_penalty,
        is_fixed_start=is_fixed_start,
        is_fixed_end=is_fixed_end,
        is_known_anchor=is_known_anchor,
        duplicate_group_id=duplicate_group_id,
    )


def _make_matrix(costs: list[list[float]]) -> MatrixResult:
    """Create a MatrixResult from a cost matrix (used as both distance and duration)."""
    n = len(costs)
    return MatrixResult(
        stop_ids=tuple(f"s{i}" for i in range(n)),
        distance_matrix_m=costs,
        duration_matrix_s=costs,
        engine="test",
        complete=True,
    )


class TestCleanStopSet:
    def test_all_stops_visited_correct_order(self):
        """With a clean linear stop set, all stops should be visited in order."""
        stops = [
            _make_stop("s0", "Start", 0.0, 0.0, is_fixed_start=True, is_known_anchor=True),
            _make_stop("s1", "A", 0.0, 0.01),
            _make_stop("s2", "B", 0.0, 0.02),
            _make_stop("s3", "End", 0.0, 0.03, is_fixed_end=True, is_known_anchor=True),
        ]
        # Costs favor sequential order: s0→s1→s2→s3
        costs = [
            [0, 100, 200, 300],
            [100, 0, 100, 200],
            [200, 100, 0, 100],
            [300, 200, 100, 0],
        ]
        matrix = _make_matrix(costs)
        solver = CPSATCircuitSolver(CPSATSolverConfig(objective="distance"))
        proposal = solver.solve(stops, matrix)
        assert proposal.ordered_stop_ids == ("s0", "s1", "s2", "s3")
        assert len(proposal.skipped_stop_ids) == 0
        assert proposal.diagnostics["solver_status"] in ("OPTIMAL", "FEASIBLE")

    def test_two_stops_terminus_only(self):
        """Just start and end should be returned in order."""
        stops = [
            _make_stop("s0", "Start", 0.0, 0.0, is_fixed_start=True, is_known_anchor=True),
            _make_stop("s1", "End", 0.0, 0.01, is_fixed_end=True, is_known_anchor=True),
        ]
        costs = [[0, 100], [100, 0]]
        matrix = _make_matrix(costs)
        solver = CPSATCircuitSolver(CPSATSolverConfig(objective="distance"))
        proposal = solver.solve(stops, matrix)
        assert proposal.ordered_stop_ids == ("s0", "s1")
        assert len(proposal.skipped_stop_ids) == 0


class TestFalsePositiveSkipping:
    def test_one_false_positive_skipped(self):
        """A weak stop that causes a major detour should be skipped."""
        stops = [
            _make_stop("s0", "Start", 0.0, 0.0, is_fixed_start=True, is_known_anchor=True),
            _make_stop("s1", "A", 0.0, 0.01),
            _make_stop("bad", "Detour", 0.0, 0.50, weak=True, optional_penalty=100),  # far away
            _make_stop("s3", "End", 0.0, 0.02, is_fixed_end=True, is_known_anchor=True),
        ]
        # bad stop is very expensive to reach
        costs = [
            [0, 100, 50000, 200],
            [100, 0, 50000, 100],
            [50000, 50000, 0, 50000],
            [200, 100, 50000, 0],
        ]
        matrix = _make_matrix(costs)
        solver = CPSATCircuitSolver(CPSATSolverConfig(objective="distance"))
        proposal = solver.solve(stops, matrix)
        assert "bad" in proposal.skipped_stop_ids
        assert "s0" in proposal.ordered_stop_ids
        assert "s3" in proposal.ordered_stop_ids

    def test_opposite_direction_stop_skipped(self):
        """Stop with very low penalty (opposite-direction flagged) should be skipped."""
        stops = [
            _make_stop("s0", "Start", 0.0, 0.0, is_fixed_start=True, is_known_anchor=True),
            _make_stop("opp", "Opposite", 0.0, 0.50, weak=True, optional_penalty=100),
            _make_stop("s2", "End", 0.0, 0.02, is_fixed_end=True, is_known_anchor=True),
        ]
        costs = [
            [0, 50000, 200],
            [50000, 0, 50000],
            [200, 50000, 0],
        ]
        matrix = _make_matrix(costs)
        solver = CPSATCircuitSolver(CPSATSolverConfig(objective="distance"))
        proposal = solver.solve(stops, matrix)
        assert "opp" in proposal.skipped_stop_ids


class TestTerminusProtection:
    def test_start_never_skipped(self):
        """Start terminus must never be skipped."""
        stops = [
            _make_stop("s0", "Start", 0.0, 0.0, is_fixed_start=True, is_known_anchor=True),
            _make_stop("s1", "End", 0.0, 0.01, is_fixed_end=True, is_known_anchor=True),
        ]
        costs = [[0, 100], [100, 0]]
        matrix = _make_matrix(costs)
        solver = CPSATCircuitSolver(CPSATSolverConfig(objective="distance"))
        proposal = solver.solve(stops, matrix)
        assert "s0" in proposal.ordered_stop_ids
        assert "s0" not in proposal.skipped_stop_ids

    def test_end_never_skipped(self):
        """End terminus must never be skipped."""
        stops = [
            _make_stop("s0", "Start", 0.0, 0.0, is_fixed_start=True, is_known_anchor=True),
            _make_stop("s1", "End", 0.0, 0.01, is_fixed_end=True, is_known_anchor=True),
        ]
        costs = [[0, 100], [100, 0]]
        matrix = _make_matrix(costs)
        solver = CPSATCircuitSolver(CPSATSolverConfig(objective="distance"))
        proposal = solver.solve(stops, matrix)
        assert "s1" in proposal.ordered_stop_ids
        assert "s1" not in proposal.skipped_stop_ids


class TestDuplicateMutualExclusion:
    def test_duplicate_pair_one_visited(self):
        """For stops in the same duplicate group, at most one should be visited."""
        stops = [
            _make_stop("s0", "Start", 0.0, 0.0, is_fixed_start=True, is_known_anchor=True),
            _make_stop("d1", "Parada", 0.0, 0.01, duplicate_group_id="dup1"),
            _make_stop("d2", "Parada", 0.0, 0.011, duplicate_group_id="dup1"),
            _make_stop("s3", "End", 0.0, 0.02, is_fixed_end=True, is_known_anchor=True),
        ]
        costs = [
            [0, 100, 110, 200],
            [100, 0, 10, 100],
            [110, 10, 0, 90],
            [200, 100, 90, 0],
        ]
        matrix = _make_matrix(costs)
        solver = CPSATCircuitSolver(CPSATSolverConfig(objective="distance"))
        proposal = solver.solve(stops, matrix)
        visited_dups = [sid for sid in ("d1", "d2") if sid in proposal.ordered_stop_ids]
        assert len(visited_dups) == 1


class TestAnchorProtection:
    def test_anchor_rarely_skipped(self):
        """Anchors have very high penalty, so they should almost never be skipped."""
        stops = [
            _make_stop("s0", "Start", 0.0, 0.0, is_fixed_start=True, is_known_anchor=True),
            _make_stop("anchor", "Important", 0.0, 0.01, is_known_anchor=True),
            _make_stop("s2", "End", 0.0, 0.02, is_fixed_end=True, is_known_anchor=True),
        ]
        costs = [
            [0, 500, 200],
            [500, 0, 500],
            [200, 500, 0],
        ]
        matrix = _make_matrix(costs)
        solver = CPSATCircuitSolver(CPSATSolverConfig(objective="distance"))
        proposal = solver.solve(stops, matrix)
        assert "anchor" in proposal.ordered_stop_ids


class TestLargeStopSet:
    def test_30_stops_solves_under_5_seconds(self):
        """30+ stops should solve within the time limit."""
        n = 32
        stops = []
        for i in range(n):
            stops.append(
                _make_stop(
                    f"s{i}",
                    f"Stop {i}",
                    0.0 + i * 0.001,
                    0.0 + i * 0.001,
                    is_fixed_start=(i == 0),
                    is_fixed_end=(i == n - 1),
                    is_known_anchor=(i == 0 or i == n - 1),
                )
            )
        # Simple sequential cost matrix
        costs = []
        for i in range(n):
            row = []
            for j in range(n):
                row.append(abs(i - j) * 100.0)
            costs.append(row)
        matrix = _make_matrix(costs)
        solver = CPSATCircuitSolver(CPSATSolverConfig(objective="distance", time_limit_s=5.0))
        proposal = solver.solve(stops, matrix)
        assert proposal.diagnostics["solver_status"] in ("OPTIMAL", "FEASIBLE")
        assert proposal.diagnostics["solve_time_seconds"] < 5.0
        # Should visit most stops
        assert len(proposal.ordered_stop_ids) >= n - 2


class TestSolverStatus:
    def test_trivial_single_stop(self):
        """Single stop should return trivial result."""
        stops = [
            _make_stop("s0", "Only", 0.0, 0.0, is_fixed_start=True, is_fixed_end=True),
        ]
        costs = [[0]]
        matrix = _make_matrix(costs)
        solver = CPSATCircuitSolver(CPSATSolverConfig(objective="distance"))
        proposal = solver.solve(stops, matrix)
        assert proposal.ordered_stop_ids == ("s0",)
        assert proposal.diagnostics["solver_status"] == "TRIVIAL"
