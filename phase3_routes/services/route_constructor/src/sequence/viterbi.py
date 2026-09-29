"""
Viterbi / CRF-style decoding for stop sequence (Phase 3).

We model an ordered list of observations p_i (stop-prior points from OSM relation)
and decode the best hidden stop sequence s_i (canonical stop_node_ids).

- Emissions: E_i(s) = cost of matching observation p_i to stop candidate s
  (e.g., based on distance between p_i and stop, role consistency, etc.)

- Transitions: T(s_prev, s_next) = travel cost between consecutive stops
  (typically from Valhalla matrix: time/distance; plus penalties for duplicates/jumps)

We decode:
- best path (Viterbi)
- top-K paths (k-best Viterbi DP)

This module is intentionally "pluggable":
- candidate generation is done elsewhere (e.g., src/sequence/candidates.py)
- Valhalla client is used elsewhere to build transition matrices
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import uuid


# ----------------------------
# Types
# ----------------------------

Cost = float


@dataclass(frozen=True)
class StopCandidate:
    """
    A candidate canonical stop for one observation p_i.

    Required:
      stop_id: canonical stop_nodes_prod.stop_node_id
      emission_cost: lower is better (e.g., distance in meters, or normalized cost)

    Optional:
      lat/lon if you want to build Valhalla matrices outside and attach coords here.
      meta: extra data (match_dist_m, role, tags, etc.)
    """
    stop_id: uuid.UUID
    emission_cost: Cost
    lat: Optional[float] = None
    lon: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DecodedPath:
    stop_ids: List[uuid.UUID]
    total_cost: Cost
    # Optional diagnostic breakdown
    emission_cost_sum: Cost = 0.0
    transition_cost_sum: Cost = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


# Transition matrix between two consecutive steps:
# matrix[j][k] = cost from prev_candidates[j] -> curr_candidates[k]
TransitionMatrix = List[List[Cost]]


class ViterbiError(RuntimeError):
    pass


class NoCandidatesError(ViterbiError):
    pass


# ----------------------------
# Helpers
# ----------------------------

def _validate_steps(steps: Sequence[Sequence[StopCandidate]]) -> None:
    if not steps:
        raise NoCandidatesError("No steps provided to Viterbi decoder.")
    for i, cand_list in enumerate(steps):
        if not cand_list:
            raise NoCandidatesError(
                f"Step {i} has 0 candidates. Either drop this observation p_i, "
                f"or add a dummy 'unmatched' candidate with a large emission cost."
            )


def _validate_transition_matrices(
    steps: Sequence[Sequence[StopCandidate]],
    transition_matrices: Sequence[TransitionMatrix],
) -> None:
    n = len(steps)
    if len(transition_matrices) != max(0, n - 1):
        raise ViterbiError(
            f"Expected {max(0, n-1)} transition matrices, got {len(transition_matrices)}."
        )

    for i in range(n - 1):
        prev_n = len(steps[i])
        curr_n = len(steps[i + 1])
        mat = transition_matrices[i]

        if len(mat) != prev_n:
            raise ViterbiError(
                f"Transition matrix {i} has {len(mat)} rows, expected {prev_n}."
            )
        for r in mat:
            if len(r) != curr_n:
                raise ViterbiError(
                    f"Transition matrix {i} row has {len(r)} cols, expected {curr_n}."
                )


def _default_transition_penalty(
    prev: StopCandidate,
    curr: StopCandidate,
    *,
    duplicate_penalty: Cost = 5000.0,
) -> Cost:
    """
    Optional penalty that can be added on top of Valhalla costs.
    Keeps things sane:
      - discourage s_prev == s_curr duplicates
    """
    if prev.stop_id == curr.stop_id:
        return duplicate_penalty
    return 0.0


def summarize_decoded_path(
    steps: Sequence[Sequence[StopCandidate]],
    path_state_indices: List[int],
    transition_matrices: Optional[Sequence[TransitionMatrix]] = None,
) -> Tuple[Cost, Cost, Cost]:
    """
    Returns (total_cost, emission_sum, transition_sum) for a decoded path.
    """
    emission_sum = 0.0
    transition_sum = 0.0

    for i, j in enumerate(path_state_indices):
        emission_sum += steps[i][j].emission_cost

    if transition_matrices is not None and len(steps) > 1:
        for i in range(len(steps) - 1):
            j_prev = path_state_indices[i]
            j_curr = path_state_indices[i + 1]
            transition_sum += transition_matrices[i][j_prev][j_curr]

    return emission_sum + transition_sum, emission_sum, transition_sum


# ----------------------------
# Best-path Viterbi
# ----------------------------

def viterbi_best(
    steps: Sequence[Sequence[StopCandidate]],
    *,
    transition_matrices: Optional[Sequence[TransitionMatrix]] = None,
    transition_cost_fn: Optional[Callable[[StopCandidate, StopCandidate], Cost]] = None,
    transition_penalty_fn: Optional[Callable[[StopCandidate, StopCandidate], Cost]] = None,
) -> DecodedPath:
    """
    Decode the single best path.

    You can provide either:
      - transition_matrices (recommended: built from Valhalla matrix calls), OR
      - transition_cost_fn(prev, curr) returning a cost

    transition_penalty_fn is optional and is ADDED to transition cost.
    """
    _validate_steps(steps)

    n = len(steps)
    if n == 1:
        # trivial: choose min emission
        j_best = min(range(len(steps[0])), key=lambda j: steps[0][j].emission_cost)
        cand = steps[0][j_best]
        return DecodedPath(
            stop_ids=[cand.stop_id],
            total_cost=cand.emission_cost,
            emission_cost_sum=cand.emission_cost,
            transition_cost_sum=0.0,
            meta={"mode": "viterbi_best", "n_steps": 1},
        )

    if transition_matrices is not None:
        _validate_transition_matrices(steps, transition_matrices)

    if transition_penalty_fn is None:
        transition_penalty_fn = _default_transition_penalty

    # dp_cost[i][k] = best cost ending at state k in step i
    # backptr[i][k] = argmin prev_state index
    dp_cost: List[List[Cost]] = []
    backptr: List[List[int]] = []

    # init
    dp0 = [c.emission_cost for c in steps[0]]
    dp_cost.append(dp0)
    backptr.append([-1] * len(steps[0]))

    # recurrence
    for i in range(1, n):
        prev_cands = steps[i - 1]
        curr_cands = steps[i]
        curr_dp: List[Cost] = [float("inf")] * len(curr_cands)
        curr_bp: List[int] = [-1] * len(curr_cands)

        for k, curr in enumerate(curr_cands):
            best_val = float("inf")
            best_j = -1

            for j, prev in enumerate(prev_cands):
                base = dp_cost[i - 1][j]

                if transition_matrices is not None:
                    t_cost = transition_matrices[i - 1][j][k]
                elif transition_cost_fn is not None:
                    t_cost = transition_cost_fn(prev, curr)
                else:
                    t_cost = 0.0  # no transitions (degenerate)

                t_cost += transition_penalty_fn(prev, curr)

                val = base + t_cost + curr.emission_cost
                if val < best_val:
                    best_val = val
                    best_j = j

            curr_dp[k] = best_val
            curr_bp[k] = best_j

        dp_cost.append(curr_dp)
        backptr.append(curr_bp)

    # choose best ending state
    last = dp_cost[-1]
    k_best = min(range(len(last)), key=lambda k: last[k])

    # backtrack state indices
    path_idx = [0] * n
    path_idx[-1] = k_best
    for i in range(n - 1, 0, -1):
        path_idx[i - 1] = backptr[i][path_idx[i]]

    # build stop_id path
    stop_ids = [steps[i][path_idx[i]].stop_id for i in range(n)]
    total, e_sum, t_sum = summarize_decoded_path(steps, path_idx, transition_matrices)

    return DecodedPath(
        stop_ids=stop_ids,
        total_cost=total,
        emission_cost_sum=e_sum,
        transition_cost_sum=t_sum,
        meta={"mode": "viterbi_best", "n_steps": n},
    )


# ----------------------------
# Top-K Viterbi (k-best DP)
# ----------------------------

@dataclass(frozen=True)
class _KNode:
    cost: Cost
    prev_state: int
    prev_rank: int  # which of the K paths in previous state we came from


def viterbi_topk(
    steps: Sequence[Sequence[StopCandidate]],
    *,
    k: int = 5,
    transition_matrices: Optional[Sequence[TransitionMatrix]] = None,
    transition_cost_fn: Optional[Callable[[StopCandidate, StopCandidate], Cost]] = None,
    transition_penalty_fn: Optional[Callable[[StopCandidate, StopCandidate], Cost]] = None,
) -> List[DecodedPath]:
    """
    Decode top-K paths using k-best Viterbi DP.

    Keeps up to k best partial paths per state at each step,
    then returns the overall best k complete paths across end states.
    """
    if k <= 0:
        return []

    _validate_steps(steps)
    n = len(steps)

    if n == 1:
        # return up to k best by emission
        ranked = sorted(steps[0], key=lambda c: c.emission_cost)[:k]
        out: List[DecodedPath] = []
        for c in ranked:
            out.append(
                DecodedPath(
                    stop_ids=[c.stop_id],
                    total_cost=c.emission_cost,
                    emission_cost_sum=c.emission_cost,
                    transition_cost_sum=0.0,
                    meta={"mode": "viterbi_topk", "n_steps": 1},
                )
            )
        return out

    if transition_matrices is not None:
        _validate_transition_matrices(steps, transition_matrices)

    if transition_penalty_fn is None:
        transition_penalty_fn = _default_transition_penalty

    # dp[i][state] = list of up to k _KNode entries for paths ending at 'state'
    # For i=0 we use prev_state=-1 prev_rank=-1 and cost=emission.
    dp: List[List[List[_KNode]]] = []

    dp0: List[List[_KNode]] = []
    for c in steps[0]:
        dp0.append([_KNode(cost=c.emission_cost, prev_state=-1, prev_rank=-1)])
    dp.append(dp0)

    # recurrence
    for i in range(1, n):
        prev_cands = steps[i - 1]
        curr_cands = steps[i]
        layer: List[List[_KNode]] = [[] for _ in range(len(curr_cands))]

        for k_state, curr in enumerate(curr_cands):
            candidates_nodes: List[_KNode] = []

            for j_state, prev in enumerate(prev_cands):
                prev_list = dp[i - 1][j_state]
                if not prev_list:
                    continue

                if transition_matrices is not None:
                    base_t = transition_matrices[i - 1][j_state][k_state]
                elif transition_cost_fn is not None:
                    base_t = transition_cost_fn(prev, curr)
                else:
                    base_t = 0.0

                base_t += transition_penalty_fn(prev, curr)

                for prev_rank, prev_node in enumerate(prev_list):
                    new_cost = prev_node.cost + base_t + curr.emission_cost
                    candidates_nodes.append(_KNode(cost=new_cost, prev_state=j_state, prev_rank=prev_rank))

            # keep k smallest
            candidates_nodes.sort(key=lambda x: x.cost)
            layer[k_state] = candidates_nodes[:k]

        dp.append(layer)

    # collect all complete paths across end states
    end_nodes: List[Tuple[Cost, int, int]] = []  # (cost, end_state, end_rank)
    for end_state, nodes in enumerate(dp[-1]):
        for end_rank, node in enumerate(nodes):
            end_nodes.append((node.cost, end_state, end_rank))

    end_nodes.sort(key=lambda x: x[0])
    end_nodes = end_nodes[:k]

    # backtrack each solution
    out_paths: List[DecodedPath] = []
    for _, end_state, end_rank in end_nodes:
        state_indices = [0] * n
        # store which rank we picked at each state (needed for dp backtracking)
        ranks = [0] * n

        state_indices[-1] = end_state
        ranks[-1] = end_rank

        for i in range(n - 1, 0, -1):
            node = dp[i][state_indices[i]][ranks[i]]
            state_indices[i - 1] = node.prev_state
            ranks[i - 1] = node.prev_rank

        stop_ids = [steps[i][state_indices[i]].stop_id for i in range(n)]
        total, e_sum, t_sum = summarize_decoded_path(steps, state_indices, transition_matrices)

        out_paths.append(
            DecodedPath(
                stop_ids=stop_ids,
                total_cost=total,
                emission_cost_sum=e_sum,
                transition_cost_sum=t_sum,
                meta={"mode": "viterbi_topk", "n_steps": n, "k": k},
            )
        )

    return out_paths
