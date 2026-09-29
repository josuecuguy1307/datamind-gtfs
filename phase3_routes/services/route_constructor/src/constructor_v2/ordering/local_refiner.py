from __future__ import annotations

from itertools import permutations
from typing import Sequence

from src.constructor_v2.common import haversine_m
from src.constructor_v2.constants import MAX_ACCEPTABLE_DETOUR_RATIO
from src.constructor_v2.ordering.matrix_builder import MatrixResult
from src.constructor_v2.schemas.route_input import NormalizedStop
from src.constructor_v2.schemas.route_output import OrderProposal


class LocalRefiner:
    def __init__(self, *, objective: str = "duration") -> None:
        self.objective = objective

    def refine(
        self,
        stops_by_id: dict[str, NormalizedStop],
        proposal: OrderProposal,
        matrix_result: MatrixResult,
    ) -> OrderProposal:
        ordered_ids = list(proposal.ordered_stop_ids)
        if len(ordered_ids) <= 3:
            return proposal

        best_ids = list(ordered_ids)
        best_score = self._score_order(best_ids, stops_by_id, matrix_result)
        accepted_moves: list[dict[str, object]] = []

        improved = True
        while improved:
            improved = False
            for candidate_ids, move in self._candidate_orders(best_ids):
                score = self._score_order(candidate_ids, stops_by_id, matrix_result)
                if score + 1e-6 < best_score:
                    best_score = score
                    best_ids = candidate_ids
                    accepted_moves.append(move)
                    improved = True
                    break

        if tuple(best_ids) == proposal.ordered_stop_ids:
            return proposal
        return OrderProposal(
            method=f"{proposal.method}+local_refine",
            ordered_stop_ids=tuple(best_ids),
            objective_value=best_score,
            objective_unit=self.objective,
            skipped_stop_ids=proposal.skipped_stop_ids,
            diagnostics={**proposal.diagnostics, "accepted_moves": accepted_moves},
        )

    def _candidate_orders(self, ordered_ids: Sequence[str]):
        for index in range(1, len(ordered_ids) - 2):
            swapped = list(ordered_ids)
            swapped[index], swapped[index + 1] = swapped[index + 1], swapped[index]
            yield swapped, {"type": "adjacent_swap", "index": index}
        for start in range(1, len(ordered_ids) - 3):
            window = ordered_ids[start : start + 3]
            for perm in permutations(window):
                if list(perm) == list(window):
                    continue
                candidate = list(ordered_ids[:start]) + list(perm) + list(ordered_ids[start + 3 :])
                yield candidate, {"type": "window_permutation", "start": start, "window": list(window)}

    def _score_order(
        self,
        ordered_ids: Sequence[str],
        stops_by_id: dict[str, NormalizedStop],
        matrix_result: MatrixResult,
    ) -> float:
        index_by_id = {stop_id: idx for idx, stop_id in enumerate(matrix_result.stop_ids)}
        objective_matrix = matrix_result.objective_matrix(self.objective)
        score = 0.0
        for left_id, right_id in zip(ordered_ids, ordered_ids[1:]):
            left_index = index_by_id[left_id]
            right_index = index_by_id[right_id]
            score += objective_matrix[left_index][right_index]
            left = stops_by_id[left_id]
            right = stops_by_id[right_id]
            straight = max(20.0, haversine_m(left.lat, left.lon, right.lat, right.lon))
            road = matrix_result.distance_matrix_m[left_index][right_index]
            detour_ratio = road / straight if road and road != float("inf") else MAX_ACCEPTABLE_DETOUR_RATIO * 2.0
            if detour_ratio > MAX_ACCEPTABLE_DETOUR_RATIO:
                score += (detour_ratio - MAX_ACCEPTABLE_DETOUR_RATIO) * 500.0
        return score
