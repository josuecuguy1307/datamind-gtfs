"""CP-SAT solver with optional nodes (Prize-Collecting TSP).

Uses OR-Tools CP-SAT add_circuit with self-loops to allow skipping
suspected bad stops. This is the core algorithmic upgrade over the
OR-Tools routing solver.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from ortools.sat.python import cp_model

from src.constructor_v2.schemas.route_input import NormalizedStop
from src.constructor_v2.schemas.route_output import OrderProposal
from src.constructor_v2.ordering.matrix_builder import MatrixResult
from src.constructor_v2.topology import canonical_topology

logger = logging.getLogger(__name__)

# --- Skip penalty defaults ---
PENALTY_TERMINUS = 1_000_000_000  # effectively infinite
PENALTY_ANCHOR = 50_000
PENALTY_NORMAL = 10_000
PENALTY_WEAK = 500
PENALTY_OPPOSITE_DIRECTION = 100

# Solver settings
DEFAULT_TIME_LIMIT_S = 5.0
COST_SCALE = 1  # matrix values are already in meters or seconds


@dataclass(slots=True)
class CPSATSolverConfig:
    time_limit_s: float = DEFAULT_TIME_LIMIT_S
    objective: str = "duration"
    enable_monotonic_progress: bool = False
    penalty_terminus: int = PENALTY_TERMINUS
    penalty_anchor: int = PENALTY_ANCHOR
    penalty_normal: int = PENALTY_NORMAL
    penalty_weak: int = PENALTY_WEAK
    penalty_opposite_direction: int = PENALTY_OPPOSITE_DIRECTION


@dataclass(slots=True)
class CPSATResult:
    ordered_stop_ids: list[str]
    skipped_stop_ids: list[str]
    total_road_cost: float
    total_skip_penalty: float
    solver_status: str
    solve_time_seconds: float
    objective_value: float
    stop_diagnostics: dict[str, Any] = field(default_factory=dict)


class CPSATCircuitSolver:
    def __init__(self, config: CPSATSolverConfig | None = None) -> None:
        self.config = config or CPSATSolverConfig()

    def solve(
        self,
        stops: Sequence[NormalizedStop],
        matrix_result: MatrixResult,
        *,
        route_type: str = "lineal",
        corridor_ordering_pairs: list[tuple[int, int]] | None = None,
    ) -> OrderProposal:
        result = self._solve_circuit(
            stops, matrix_result, route_type=route_type,
            corridor_ordering_pairs=corridor_ordering_pairs or [],
        )

        return OrderProposal(
            method="cpsat_circuit",
            ordered_stop_ids=tuple(result.ordered_stop_ids),
            objective_value=result.objective_value,
            objective_unit=self.config.objective,
            skipped_stop_ids=tuple(result.skipped_stop_ids),
            diagnostics={
                "solver_status": result.solver_status,
                "solve_time_seconds": result.solve_time_seconds,
                "total_road_cost": result.total_road_cost,
                "total_skip_penalty": result.total_skip_penalty,
                "skipped_count": len(result.skipped_stop_ids),
                "corridor_constraints": len(corridor_ordering_pairs or []),
                "stop_diagnostics": result.stop_diagnostics,
                "matrix_engine": matrix_result.engine,
            },
        )

    def _solve_circuit(
        self,
        stops: Sequence[NormalizedStop],
        matrix_result: MatrixResult,
        *,
        route_type: str,
        corridor_ordering_pairs: list[tuple[int, int]] | None = None,
    ) -> CPSATResult:
        n = len(stops)
        if n < 2:
            return CPSATResult(
                ordered_stop_ids=[s.stop_id for s in stops],
                skipped_stop_ids=[],
                total_road_cost=0.0,
                total_skip_penalty=0.0,
                solver_status="TRIVIAL",
                solve_time_seconds=0.0,
                objective_value=0.0,
            )

        topology = canonical_topology(route_type)
        is_linear = topology == "linear"
        cost_matrix = matrix_result.objective_matrix(self.config.objective)

        # Identify start/end indices
        start_idx = 0
        end_idx = n - 1
        for i, stop in enumerate(stops):
            if stop.is_fixed_start:
                start_idx = i
            if stop.is_fixed_end:
                end_idx = i

        # Compute skip penalties per stop
        skip_penalties = self._compute_skip_penalties(stops, start_idx, end_idx)

        # Scale costs to integers
        int_costs = self._scale_cost_matrix(cost_matrix, n)

        model = cp_model.CpModel()

        # Create arc literals
        arc_literals: dict[tuple[int, int], cp_model.IntVar] = {}
        arcs: list[tuple[int, int, cp_model.IntVar]] = []

        for i in range(n):
            for j in range(n):
                if i == j:
                    # Self-loop: skip this stop
                    lit = model.new_bool_var(f"skip_{i}")
                    arc_literals[(i, i)] = lit
                    arcs.append((i, j, lit))
                else:
                    lit = model.new_bool_var(f"arc_{i}_{j}")
                    arc_literals[(i, j)] = lit
                    arcs.append((i, j, lit))

        # Circuit constraint
        model.add_circuit(arcs)

        # --- Mandatory constraints ---
        # Start cannot be skipped
        model.add(arc_literals[(start_idx, start_idx)] == 0)
        # End cannot be skipped
        model.add(arc_literals[(end_idx, end_idx)] == 0)

        # For linear routes: force return arc end → start (dummy arc to close circuit)
        if is_linear:
            model.add(arc_literals[(end_idx, start_idx)] == 1)

        # --- Duplicate mutual exclusion ---
        dup_groups: dict[str, list[int]] = {}
        for i, stop in enumerate(stops):
            if stop.duplicate_group_id:
                dup_groups.setdefault(stop.duplicate_group_id, []).append(i)
        for group_id, indices in dup_groups.items():
            if len(indices) >= 2:
                # At most one in the group can be visited (not skipped)
                model.add(
                    sum(arc_literals[(idx, idx)] for idx in indices) >= len(indices) - 1
                )

        # --- Corridor ordering constraints (HARD) ---
        # These come from confirmed GTFS routes and enforce known correct orderings.
        # We use position variables to enforce ordering between non-skipped stops.
        corridor_constraint_count = 0
        if corridor_ordering_pairs:
            # Create position variables for each node
            pos = [model.new_int_var(0, n - 1, f"pos_{i}") for i in range(n)]
            # Position of start is 0
            model.add(pos[start_idx] == 0)
            # Link positions to arcs: if arc[i][j] == 1 and neither is skipped, pos[j] == pos[i] + 1
            for i in range(n):
                for j in range(n):
                    if i == j:
                        continue
                    if is_linear and i == end_idx and j == start_idx:
                        continue  # skip dummy return arc
                    # If arc i→j is active, pos[j] = pos[i] + 1
                    model.add(pos[j] == pos[i] + 1).only_enforce_if(arc_literals[(i, j)])

            # Inject corridor ordering: if A must come before B, pos[A] < pos[B]
            # Only enforce when both stops are NOT skipped
            for idx_a, idx_b in corridor_ordering_pairs:
                if 0 <= idx_a < n and 0 <= idx_b < n:
                    both_visited = model.new_bool_var(f"both_visited_{idx_a}_{idx_b}")
                    a_visited = model.new_bool_var(f"a_vis_{idx_a}_{idx_b}")
                    b_visited = model.new_bool_var(f"b_vis_{idx_a}_{idx_b}")
                    # a_visited iff NOT skipped
                    model.add(arc_literals[(idx_a, idx_a)] == 0).only_enforce_if(a_visited)
                    model.add(arc_literals[(idx_a, idx_a)] == 1).only_enforce_if(a_visited.negated())
                    model.add(arc_literals[(idx_b, idx_b)] == 0).only_enforce_if(b_visited)
                    model.add(arc_literals[(idx_b, idx_b)] == 1).only_enforce_if(b_visited.negated())
                    # both_visited = a_visited AND b_visited
                    model.add_bool_and([a_visited, b_visited]).only_enforce_if(both_visited)
                    model.add_bool_or([a_visited.negated(), b_visited.negated()]).only_enforce_if(both_visited.negated())
                    # Enforce ordering only when both are visited
                    model.add(pos[idx_a] < pos[idx_b]).only_enforce_if(both_visited)
                    corridor_constraint_count += 1

            logger.info("Injected %d corridor ordering constraints", corridor_constraint_count)

        # --- Objective: minimize travel cost + skip penalties ---
        travel_terms: list[cp_model.LinearExpr] = []
        skip_terms: list[cp_model.LinearExpr] = []

        for i in range(n):
            for j in range(n):
                if i == j:
                    # Skip penalty
                    skip_terms.append(int(skip_penalties[i]) * arc_literals[(i, i)])
                else:
                    cost = int_costs[i][j]
                    # For the forced return arc (end→start on linear), use 0 cost
                    if is_linear and i == end_idx and j == start_idx:
                        cost = 0
                    travel_terms.append(cost * arc_literals[(i, j)])

        model.minimize(sum(travel_terms) + sum(skip_terms))

        # --- Solve ---
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.config.time_limit_s
        solver.parameters.log_search_progress = False

        t0 = time.monotonic()
        status_code = solver.solve(model)
        solve_time = time.monotonic() - t0

        status_name = {
            cp_model.OPTIMAL: "OPTIMAL",
            cp_model.FEASIBLE: "FEASIBLE",
            cp_model.INFEASIBLE: "INFEASIBLE",
            cp_model.MODEL_INVALID: "MODEL_INVALID",
            cp_model.UNKNOWN: "UNKNOWN",
        }.get(status_code, f"STATUS_{status_code}")

        if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            logger.warning("CP-SAT solver status: %s", status_name)
            return CPSATResult(
                ordered_stop_ids=[s.stop_id for s in stops],
                skipped_stop_ids=[],
                total_road_cost=0.0,
                total_skip_penalty=0.0,
                solver_status=status_name,
                solve_time_seconds=solve_time,
                objective_value=0.0,
                stop_diagnostics={"error": f"Solver returned {status_name}"},
            )

        # Extract solution: follow the arcs from start_idx
        visited_order: list[int] = []
        skipped_indices: list[int] = []
        stop_diag: dict[str, Any] = {}

        for i in range(n):
            is_skipped = solver.value(arc_literals[(i, i)]) == 1
            if is_skipped:
                skipped_indices.append(i)
                stop_diag[stops[i].stop_id] = {
                    "status": "skipped",
                    "skip_penalty": skip_penalties[i],
                    "reason": "solver_chose_to_skip",
                }
            else:
                stop_diag[stops[i].stop_id] = {
                    "status": "visited",
                    "skip_penalty": skip_penalties[i],
                }

        # Follow circuit from start to extract order
        current = start_idx
        visited_set: set[int] = set()
        for _ in range(n):
            if current in visited_set:
                break
            if current not in [idx for idx in skipped_indices]:
                visited_order.append(current)
            visited_set.add(current)
            # Find next
            for j in range(n):
                if j != current and solver.value(arc_literals[(current, j)]) == 1:
                    current = j
                    break
            else:
                break

        # Compute costs
        total_road_cost = 0.0
        for k in range(len(visited_order) - 1):
            i_idx = visited_order[k]
            j_idx = visited_order[k + 1]
            c = cost_matrix[i_idx][j_idx]
            if c != float("inf"):
                total_road_cost += c

        total_skip_penalty = sum(skip_penalties[i] for i in skipped_indices)

        ordered_ids = [stops[i].stop_id for i in visited_order]
        skipped_ids = [stops[i].stop_id for i in skipped_indices]

        # Add position info to diagnostics
        for pos, idx in enumerate(visited_order):
            stop_diag[stops[idx].stop_id]["position"] = pos

        return CPSATResult(
            ordered_stop_ids=ordered_ids,
            skipped_stop_ids=skipped_ids,
            total_road_cost=total_road_cost,
            total_skip_penalty=total_skip_penalty,
            solver_status=status_name,
            solve_time_seconds=solve_time,
            objective_value=float(solver.objective_value) if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 0.0,
            stop_diagnostics=stop_diag,
        )

    def _compute_skip_penalties(
        self,
        stops: Sequence[NormalizedStop],
        start_idx: int,
        end_idx: int,
    ) -> list[int]:
        """Assign skip penalties based on stop importance."""
        penalties: list[int] = []
        for i, stop in enumerate(stops):
            if i == start_idx or i == end_idx or stop.is_fixed_start or stop.is_fixed_end:
                penalties.append(self.config.penalty_terminus)
            elif stop.is_known_anchor:
                penalties.append(self.config.penalty_anchor)
            elif stop.weak_candidate:
                # Check if it was flagged as opposite-direction (lowest penalty)
                p = stop.optional_penalty
                if p is not None and p <= PENALTY_OPPOSITE_DIRECTION:
                    penalties.append(self.config.penalty_opposite_direction)
                else:
                    penalties.append(self.config.penalty_weak)
            else:
                penalties.append(self.config.penalty_normal)
        return penalties

    def _scale_cost_matrix(
        self,
        cost_matrix: list[list[float]],
        n: int,
    ) -> list[list[int]]:
        """Convert float cost matrix to integer values for CP-SAT."""
        int_matrix: list[list[int]] = []
        for i in range(n):
            row: list[int] = []
            for j in range(n):
                val = cost_matrix[i][j]
                if val == float("inf") or val < 0:
                    row.append(10**9)
                else:
                    row.append(max(0, int(round(val * COST_SCALE))))
            int_matrix.append(row)
        return int_matrix
