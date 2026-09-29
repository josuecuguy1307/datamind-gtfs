from __future__ import annotations

from typing import Sequence

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from src.constructor_v2.constants import OBJECTIVE_SCALE, SOLVER_TIME_LIMIT_MS
from src.constructor_v2.ordering.matrix_builder import MatrixResult
from src.constructor_v2.schemas.route_input import NormalizedStop
from src.constructor_v2.schemas.route_output import OrderProposal


class ORToolsFixedEndSolver:
    def __init__(
        self,
        *,
        objective: str = "duration",
        time_limit_ms: int = SOLVER_TIME_LIMIT_MS,
    ) -> None:
        self.objective = objective
        self.time_limit_ms = time_limit_ms

    def solve(self, stops: Sequence[NormalizedStop], matrix_result: MatrixResult) -> OrderProposal:
        objective_matrix = matrix_result.objective_matrix(self.objective)
        manager = pywrapcp.RoutingIndexManager(len(stops), 1, [0], [len(stops) - 1])
        routing = pywrapcp.RoutingModel(manager)

        def cost_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            raw_cost = objective_matrix[from_node][to_node]
            if raw_cost == float("inf"):
                return 10**9
            return max(0, int(round(raw_cost * OBJECTIVE_SCALE)))

        transit_index = routing.RegisterTransitCallback(cost_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_index)

        for node_index in range(1, len(stops) - 1):
            penalty = stops[node_index].optional_penalty
            if penalty is not None:
                routing.AddDisjunction([manager.NodeToIndex(node_index)], int(penalty * OBJECTIVE_SCALE))

        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        params.time_limit.FromMilliseconds(self.time_limit_ms)
        solution = routing.SolveWithParameters(params)
        if solution is None:
            raise RuntimeError("OR-Tools failed to produce a route")

        index = routing.Start(0)
        ordered_ids: list[str] = []
        visited_nodes: set[int] = set()
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            visited_nodes.add(node)
            ordered_ids.append(stops[node].stop_id)
            index = solution.Value(routing.NextVar(index))
        end_node = manager.IndexToNode(index)
        visited_nodes.add(end_node)
        ordered_ids.append(stops[end_node].stop_id)

        skipped = tuple(
            stops[node_index].stop_id
            for node_index in range(1, len(stops) - 1)
            if node_index not in visited_nodes
        )
        return OrderProposal(
            method="ortools_fixed_end",
            ordered_stop_ids=tuple(ordered_ids),
            objective_value=float(solution.ObjectiveValue()) / OBJECTIVE_SCALE,
            objective_unit=self.objective,
            skipped_stop_ids=skipped,
            diagnostics={
                "time_limit_ms": self.time_limit_ms,
                "matrix_engine": matrix_result.engine,
            },
        )
