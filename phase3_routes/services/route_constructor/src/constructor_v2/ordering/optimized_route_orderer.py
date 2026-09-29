from __future__ import annotations

from typing import Sequence

from src.constructor_v2.clients.valhalla_client import ValhallaClient
from src.constructor_v2.schemas.route_input import NormalizedStop
from src.constructor_v2.schemas.route_output import OrderProposal


class OptimizedRouteOrderer:
    def __init__(self, client: ValhallaClient, *, objective: str = "duration") -> None:
        self.client = client
        self.objective = objective

    def order(self, stops: Sequence[NormalizedStop]) -> OrderProposal:
        if len(stops) < 2:
            raise ValueError("Orderer requires at least start and end stops")

        optimized = self.client.try_optimized_route([(stop.lon, stop.lat) for stop in stops])
        if optimized is not None and optimized.ordered_indices:
            ordered_ids = tuple(stops[index].stop_id for index in optimized.ordered_indices)
            return OrderProposal(
                method="valhalla_optimized_route",
                ordered_stop_ids=ordered_ids,
                objective_value=0.0,
                objective_unit=self.objective,
                diagnostics=optimized.diagnostics,
            )

        ordered_indices = [0]
        remaining = list(range(1, len(stops) - 1))
        current_index = 0
        objective_value = 0.0
        end_index = len(stops) - 1

        while remaining:
            best_index = None
            best_score = float("inf")
            for candidate_index in remaining:
                pair = self.client.pairwise_cost(
                    (stops[current_index].stop_id, (stops[current_index].lon, stops[current_index].lat)),
                    (stops[candidate_index].stop_id, (stops[candidate_index].lon, stops[candidate_index].lat)),
                )
                end_pair = self.client.pairwise_cost(
                    (stops[candidate_index].stop_id, (stops[candidate_index].lon, stops[candidate_index].lat)),
                    (stops[end_index].stop_id, (stops[end_index].lon, stops[end_index].lat)),
                )
                current_to_end = self.client.pairwise_cost(
                    (stops[current_index].stop_id, (stops[current_index].lon, stops[current_index].lat)),
                    (stops[end_index].stop_id, (stops[end_index].lon, stops[end_index].lat)),
                )
                primary = pair.duration_s if self.objective == "duration" else pair.distance_m
                end_bias = end_pair.duration_s if self.objective == "duration" else end_pair.distance_m
                current_end = current_to_end.duration_s if self.objective == "duration" else current_to_end.distance_m
                score = primary + max(0.0, end_bias - current_end) * 0.35
                if score < best_score:
                    best_score = score
                    best_index = candidate_index
            if best_index is None:
                break
            ordered_indices.append(best_index)
            remaining.remove(best_index)
            objective_value += best_score
            current_index = best_index

        final_pair = self.client.pairwise_cost(
            (stops[current_index].stop_id, (stops[current_index].lon, stops[current_index].lat)),
            (stops[end_index].stop_id, (stops[end_index].lon, stops[end_index].lat)),
        )
        objective_value += final_pair.duration_s if self.objective == "duration" else final_pair.distance_m
        ordered_indices.append(end_index)
        return OrderProposal(
            method="road_network_greedy_fallback",
            ordered_stop_ids=tuple(stops[index].stop_id for index in ordered_indices),
            objective_value=objective_value,
            objective_unit=self.objective,
            diagnostics={
                "optimized_route_available": self.client.supports("/optimized_route"),
                "fallback_reason": "optimized_route_unavailable_or_failed",
                "pair_cache_entries": self.client.debug_snapshot()["pair_cache_entries"],
            },
        )
