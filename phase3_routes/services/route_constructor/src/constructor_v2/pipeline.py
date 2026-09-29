from __future__ import annotations

from dataclasses import dataclass, field
from collections import defaultdict
from pathlib import Path
from typing import Sequence

from src.constructor_v2.clients.valhalla_client import ValhallaClient
from src.constructor_v2.geometry.final_router import FinalRouter, RoutedGeometry
from src.constructor_v2.input_normalizer import NormalizationResult, normalize_route_input
from src.constructor_v2.ordering.local_refiner import LocalRefiner
from src.constructor_v2.ordering.matrix_builder import MatrixBuilder, MatrixResult
from src.constructor_v2.ordering.optimized_route_orderer import OptimizedRouteOrderer
from src.constructor_v2.ordering.ortools_solver import ORToolsFixedEndSolver
from src.constructor_v2.ordering.cpsat_solver import CPSATCircuitSolver, CPSATSolverConfig
from src.constructor_v2.preprocessing.side_of_road import SideOfRoadPreprocessor, PreprocessorConfig, PreprocessingResult
from src.constructor_v2.references.corridor_constraints import CorridorConstraintLoader, CorridorConstraintResult
from src.constructor_v2.schemas.route_input import NormalizedStop, RouteInput
from src.constructor_v2.schemas.route_output import OrderProposal, RouteOutput
from src.constructor_v2.topology import (
    apply_topology_confidence_policy,
    canonical_topology,
    is_special_case_topology,
    topology_strategy,
)
from src.constructor_v2.validation import (
    score_confidence,
    validate_anchors,
    validate_corridor_consistency,
    validate_detour_ratios,
    validate_duplicates,
    validate_monotonic_progress,
    validate_repeated_segments,
)


@dataclass(slots=True)
class ConstructorV2Pipeline:
    client: ValhallaClient
    cache_dir: Path | None = None
    objective: str = "duration"
    solver_mode: str = "ortools"  # "ortools", "cpsat", or "both"
    enable_preprocessing: bool = False
    enable_corridor_constraints: bool = False
    orderer: OptimizedRouteOrderer = field(init=False)
    matrix_builder: MatrixBuilder = field(init=False)
    solver: ORToolsFixedEndSolver = field(init=False)
    cpsat_solver: CPSATCircuitSolver = field(init=False)
    preprocessor: SideOfRoadPreprocessor = field(init=False)
    corridor_loader: CorridorConstraintLoader = field(init=False)
    refiner: LocalRefiner = field(init=False)
    final_router: FinalRouter = field(init=False)

    def __post_init__(self) -> None:
        self.orderer = OptimizedRouteOrderer(self.client, objective=self.objective)
        self.matrix_builder = MatrixBuilder(self.client, cache_dir=self.cache_dir)
        self.solver = ORToolsFixedEndSolver(objective=self.objective)
        self.cpsat_solver = CPSATCircuitSolver(CPSATSolverConfig(objective=self.objective))
        self.preprocessor = SideOfRoadPreprocessor(self.client)
        self.corridor_loader = CorridorConstraintLoader()
        self.refiner = LocalRefiner(objective=self.objective)
        self.final_router = FinalRouter(self.client)

    @classmethod
    def default(
        cls,
        *,
        cache_dir: Path | None = None,
        objective: str = "duration",
        solver_mode: str = "ortools",
        enable_preprocessing: bool = False,
        enable_corridor_constraints: bool = False,
    ) -> "ConstructorV2Pipeline":
        return cls(
            client=ValhallaClient(),
            cache_dir=cache_dir,
            objective=objective,
            solver_mode=solver_mode,
            enable_preprocessing=enable_preprocessing,
            enable_corridor_constraints=enable_corridor_constraints,
        )

    def run(self, route_input: RouteInput) -> RouteOutput:
        normalization = normalize_route_input(route_input)
        normalized_stops = normalization.normalized_stops
        topology = canonical_topology(route_input.route_type)

        if is_special_case_topology(route_input.route_type):
            proposal = OrderProposal(
                method=topology_strategy(route_input.route_type),
                ordered_stop_ids=tuple(stop.stop_id for stop in normalized_stops),
                objective_value=0.0,
                objective_unit=self.objective,
                diagnostics={
                    "topology": topology,
                    "reason": "loop_or_circular_routes_are_kept_in_special_case_mode",
                },
            )
            selected = self.evaluate_order(route_input, normalized_stops, proposal)
            selected.baseline_order = proposal
            selected.diagnostics["normalization"] = normalization.diagnostics
            selected.diagnostics["client"] = self.client.debug_snapshot()
            selected.diagnostics["topology"] = self._topology_diagnostics(route_input)
            return selected

        # --- Preprocessing ---
        preprocess_result: PreprocessingResult | None = None
        if self.enable_preprocessing:
            preprocess_result = self.preprocessor.preprocess(normalized_stops)
            normalized_stops = preprocess_result.cleaned_stops

        baseline = self.orderer.order(normalized_stops)
        baseline_output = self.evaluate_order(route_input, normalized_stops, baseline)

        matrix_result: MatrixResult | None = None
        matrix_order: OrderProposal | None = None
        refined_order: OrderProposal | None = None
        cpsat_order: OrderProposal | None = None
        selected = baseline_output
        solution_divergence = False

        use_ortools = self.solver_mode in ("ortools", "both")
        use_cpsat = self.solver_mode in ("cpsat", "both")

        # --- Corridor constraints ---
        corridor_result: CorridorConstraintResult | None = None
        corridor_ordering_pairs: list[tuple[int, int]] = []
        if self.enable_corridor_constraints:
            corridor_result = self.corridor_loader.get_constraints(
                route_input.route, normalized_stops
            )
            for cs in corridor_result.constraint_sets:
                corridor_ordering_pairs.extend(cs.ordering_pairs)

        needs_solver = baseline_output.confidence.label not in {"strong", "acceptable"} or use_cpsat

        if needs_solver:
            matrix_result = self.matrix_builder.build(normalized_stops)

            # OR-Tools path
            if use_ortools:
                matrix_order = self.solver.solve(normalized_stops, matrix_result)
                stops_by_id = {stop.stop_id: stop for stop in normalized_stops}
                refined_order = self.refiner.refine(stops_by_id, matrix_order, matrix_result)
                refined_output = self.evaluate_order(route_input, normalized_stops, refined_order)
                solution_divergence = baseline.ordered_stop_ids != refined_order.ordered_stop_ids
                if refined_output.confidence.score > baseline_output.confidence.score + 0.5:
                    selected = refined_output

            # CP-SAT path (with corridor constraints)
            if use_cpsat:
                cpsat_order = self.cpsat_solver.solve(
                    normalized_stops, matrix_result, route_type=route_input.route_type,
                    corridor_ordering_pairs=corridor_ordering_pairs if corridor_ordering_pairs else None,
                )
                cpsat_output = self.evaluate_order(route_input, normalized_stops, cpsat_order)

                # CP-SAT wins if it beats the current best
                if cpsat_output.confidence.score > selected.confidence.score + 0.5:
                    selected = cpsat_output
                    solution_divergence = True

        selected.confidence = self._score_route_confidence(
            route_type=route_input.route_type,
            validations=selected.validation,
            diagnostics={"solution_divergence": solution_divergence},
        )
        selected.baseline_order = baseline
        selected.matrix_order = matrix_order
        selected.refined_order = refined_order
        selected.diagnostics["normalization"] = normalization.diagnostics
        selected.diagnostics["client"] = self.client.debug_snapshot()
        selected.diagnostics["topology"] = self._topology_diagnostics(route_input)
        if preprocess_result is not None:
            selected.diagnostics["preprocessing"] = {
                "flagged_reasons": preprocess_result.flagged_reasons,
                "stats": preprocess_result.stats,
            }
        if corridor_result is not None and corridor_result.total_ordering_pairs > 0:
            selected.diagnostics["corridor_constraints"] = {
                "corridors_matched": [cs.corridor_name for cs in corridor_result.constraint_sets],
                "total_constrained_stops": corridor_result.total_constrained_stops,
                "total_ordering_pairs": corridor_result.total_ordering_pairs,
                "confirmed_route": corridor_result.diagnostics.get("confirmed_route_code"),
            }
        if cpsat_order is not None:
            selected.diagnostics["cpsat"] = cpsat_order.diagnostics
        if matrix_result is not None:
            selected.diagnostics["matrix"] = matrix_result.to_dict()
        return selected

    def _score_route_confidence(
        self,
        *,
        route_type: str,
        validations: dict[str, object],
        diagnostics: dict | None = None,
    ):
        confidence = score_confidence(
            validations,
            diagnostics=diagnostics,
        )
        return apply_topology_confidence_policy(confidence, route_type)

    def _ordered_stops(self, all_stops: Sequence[NormalizedStop], proposal: OrderProposal) -> list[NormalizedStop]:
        skip_ids = set(proposal.skipped_stop_ids)
        by_id: dict[str, list[NormalizedStop]] = defaultdict(list)
        for stop in all_stops:
            by_id[stop.stop_id].append(stop)
        ordered: list[NormalizedStop] = []
        for stop_id in proposal.ordered_stop_ids:
            if stop_id in skip_ids or stop_id not in by_id or not by_id[stop_id]:
                continue
            ordered.append(by_id[stop_id].pop(0))
        return ordered

    def evaluate_order(
        self,
        route_input: RouteInput,
        all_stops: Sequence[NormalizedStop],
        proposal: OrderProposal,
    ) -> RouteOutput:
        final_stops = self._ordered_stops(all_stops, proposal)
        routed = self.final_router.route(final_stops)
        locate_summary = self._locate_summary(final_stops)
        validation = self._validate(all_stops, final_stops, routed, route_type=route_input.route_type)
        confidence = self._score_route_confidence(
            route_type=route_input.route_type,
            validations=validation,
        )
        return RouteOutput(
            route=route_input.route,
            cooperative=route_input.cooperative,
            route_type=route_input.route_type,
            normalized_stops=list(all_stops),
            selected_method=proposal.method,
            baseline_order=None,
            matrix_order=None,
            refined_order=None,
            final_stops=final_stops,
            dropped_stop_ids=list(proposal.skipped_stop_ids),
            legs=routed.legs,
            geometry_geojson=routed.geometry_geojson,
            validation=validation,
            confidence=confidence,
            diagnostics={
                "proposal": proposal.diagnostics,
                "routing": routed.diagnostics,
                "locate": locate_summary,
                "topology": self._topology_diagnostics(route_input),
            },
            metrics={
                "stop_count": len(final_stops),
                "geometry_distance_m": routed.diagnostics["distance_m"],
                "geometry_duration_s": routed.diagnostics["duration_s"],
                "locate_matched_stop_count": locate_summary["matched_stop_count"],
                "locate_supported": locate_summary["supported"],
                "topology": canonical_topology(route_input.route_type),
            },
        )

    def _validate(
        self,
        original_stops: Sequence[NormalizedStop],
        final_stops: Sequence[NormalizedStop],
        routed: RoutedGeometry,
        *,
        route_type: str,
    ) -> dict[str, object]:
        return {
            "anchor_respect": validate_anchors(original_stops, final_stops),
            "detour_ratio": validate_detour_ratios(routed.legs),
            "monotonic_progress": validate_monotonic_progress(final_stops, routed.geometry_coords),
            "duplicate_stops": validate_duplicates(final_stops),
            "repeated_segments": validate_repeated_segments(routed.geometry_coords, route_type=route_type),
            "corridor_consistency": validate_corridor_consistency(final_stops, route_type=route_type),
        }

    def _locate_summary(self, stops: Sequence[NormalizedStop]) -> dict[str, object]:
        if not stops:
            return {"supported": self.client.supports("/locate"), "matched_stop_count": 0, "results": []}
        try:
            located = self.client.locate([(stop.lon, stop.lat) for stop in stops])
        except Exception as exc:
            return {
                "supported": self.client.supports("/locate"),
                "matched_stop_count": 0,
                "results": [],
                "error": str(exc),
            }

        summaries = []
        matched = 0
        for stop, item in zip(stops, located):
            way_ids = [edge.get("way_id") for edge in item.get("edges") or [] if edge.get("way_id") is not None]
            if way_ids:
                matched += 1
            summaries.append(
                {
                    "stop_id": stop.stop_id,
                    "stop_name": stop.stop_name,
                    "way_ids": way_ids[:3],
                    "edge_count": len(item.get("edges") or []),
                }
            )
        return {
            "supported": True,
            "matched_stop_count": matched,
            "results": summaries,
        }

    def _topology_diagnostics(self, route_input: RouteInput) -> dict[str, object]:
        topology = canonical_topology(route_input.route_type)
        return {
            "route_type": route_input.route_type,
            "canonical_topology": topology,
            "special_case": topology != "linear",
            "strategy": topology_strategy(route_input.route_type),
        }
