from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from src.constructor_v2.clients.valhalla_client import ValhallaClient
from src.constructor_v2.constants import (
    BENCHMARK_ARTIFACT_DIR,
    BENCHMARK_COMPARISON_ARTIFACT_DIR,
    BENCHMARK_ROUTE_BUCKETS,
    DEFAULT_CACHE_DIR,
    DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL,
    LEGACY_CONSTRUCTOR_V2_VALHALLA_URL,
)
from src.constructor_v2.evaluation.metrics import compare_stop_orders, confidence_summary
from src.constructor_v2.evaluation.reports import write_json, write_markdown, write_text
from src.constructor_v2.pipeline import ConstructorV2Pipeline
from src.constructor_v2.schemas.route_input import NormalizedStop, RouteInput
from src.constructor_v2.schemas.route_output import OrderProposal
from src.constructor_v2.topology import canonical_topology, is_special_case_topology


class BenchmarkRunner:
    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        output_dir: Path = BENCHMARK_ARTIFACT_DIR,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        objective: str = "duration",
        valhalla_base_url: str | None = None,
        scenario_name: str = "constructor_v2_dedicated",
    ) -> None:
        self.repo_root = repo_root or Path.cwd()
        self.scenario_name = scenario_name
        self.valhalla_base_url = valhalla_base_url or DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL
        self.output_dir = self.repo_root / output_dir
        self.cache_dir = self.repo_root / cache_dir / self.scenario_name
        self.pipeline = ConstructorV2Pipeline(
            client=ValhallaClient(base_url=self.valhalla_base_url),
            cache_dir=self.cache_dir,
            objective=objective,
        )
        self.current_data = self._load_json("CONSTRUCTOR/valle_v5_4_sequences_final*.json")
        self.reference_data = self._load_json("CONSTRUCTOR/valle_v5_4_COHERENT_v17*.json")
        self.override_data = self._load_json("CONSTRUCTOR/sequence_overrides_v1.json")
        self.reference_by_route = {row["route"]: row for row in self.reference_data["routes"]}
        self.override_routes = {item["route"] for item in self.override_data.get("overrides", [])}

    def _load_json(self, pattern: str) -> dict[str, Any]:
        matches = sorted(self.repo_root.glob(pattern))
        if not matches:
            raise FileNotFoundError(f"No files matched {pattern}")
        return json.loads(matches[0].read_text())

    def run(self, route_names: Iterable[str] | None = None) -> dict[str, Any]:
        selected_routes = list(route_names or self._default_route_order())
        route_results: list[dict[str, Any]] = []

        for route_name in selected_routes:
            current_row = next((row for row in self.current_data["routes"] if row["route"] == route_name), None)
            reference_row = self.reference_by_route.get(route_name)
            if current_row is None or reference_row is None:
                continue
            route_input = self._build_benchmark_input(current_row, reference_row)
            topology = canonical_topology(route_input.route_type)
            current_output = self._evaluate_current_sequence(route_input)
            v2_output = self.pipeline.run(route_input)

            reference_ids = [stop["stop_id"] for stop in reference_row["ordered_stops"]]
            current_ids = [stop.stop_id for stop in current_output.final_stops]
            v2_ids = [stop.stop_id for stop in v2_output.final_stops]
            current_vs_reference = compare_stop_orders(reference_ids, current_ids)
            v2_vs_reference = compare_stop_orders(reference_ids, v2_ids)

            reference_length_m = float(reference_row.get("geometry_km") or 0.0) * 1000.0
            current_length_m = float(current_output.metrics.get("geometry_distance_m") or 0.0)
            v2_length_m = float(v2_output.metrics.get("geometry_distance_m") or 0.0)

            improved = (
                v2_vs_reference["order_agreement"] > current_vs_reference["order_agreement"]
                or v2_output.confidence.score > current_output.confidence.score + 5.0
            )
            route_results.append(
                {
                    "route": route_name,
                    "bucket": self._route_bucket(route_name),
                    "topology": topology,
                    "route_type": route_input.route_type,
                    "topology_special_case": is_special_case_topology(route_input.route_type),
                    "override_present": route_name in self.override_routes,
                    "selected_method": v2_output.selected_method,
                    "current_confidence": confidence_summary(current_output),
                    "v2_confidence": confidence_summary(v2_output),
                    "current_vs_reference": current_vs_reference,
                    "v2_vs_reference": v2_vs_reference,
                    "current_length_delta_ratio": _length_delta_ratio(current_length_m, reference_length_m),
                    "v2_length_delta_ratio": _length_delta_ratio(v2_length_m, reference_length_m),
                    "improved": improved,
                    "still_ambiguous": v2_output.confidence.label == "ambiguous",
                    "needs_manual_review": v2_output.confidence.label == "needs_manual_review",
                    "route_input": route_input.to_dict(),
                    "current_output": current_output.to_dict(),
                    "v2_output": v2_output.to_dict(),
                }
            )

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scenario": {
                "name": self.scenario_name,
                "valhalla_base_url": self.valhalla_base_url,
                "client": self.pipeline.client.debug_snapshot(),
            },
            "artifacts": {
                "current_sequence_source": "CONSTRUCTOR/valle_v5_4_sequences_final*.json",
                "reference_source": "CONSTRUCTOR/valle_v5_4_COHERENT_v17*.json",
                "override_source": "CONSTRUCTOR/sequence_overrides_v1.json",
            },
            "summary": self._summarize(route_results),
            "routes": route_results,
        }
        write_json(self.output_dir / "benchmark_report.json", report)
        write_markdown(self.output_dir / "benchmark_report.md", report)
        write_json(
            self.output_dir / "route_level_metrics_summary.json",
            [
                {
                    "route": route["route"],
                    "bucket": route["bucket"],
                    "topology": route["topology"],
                    "route_type": route["route_type"],
                    "selected_method": route["selected_method"],
                    "current_vs_reference": route["current_vs_reference"],
                    "v2_vs_reference": route["v2_vs_reference"],
                    "current_confidence": route["current_confidence"],
                    "v2_confidence": route["v2_confidence"],
                    "improved": route["improved"],
                }
                for route in route_results
            ],
        )
        self._write_examples(route_results)
        return report

    def _evaluate_current_sequence(self, route_input: RouteInput):
        raw_stops = [
            NormalizedStop(
                order_hint=stop.seq,
                stop_id=stop.stop_id,
                stop_name=stop.stop_name,
                lat=stop.lat,
                lon=stop.lon,
                source_stop_ids=(stop.stop_id,),
                source_indices=(stop.seq,),
                source_names=(stop.stop_name,),
                representative_score=float(stop.on_route_score or 0.0),
                weak_candidate=False,
                optional_penalty=None,
                is_fixed_start=stop.seq == route_input.start_stop.seq,
                is_fixed_end=stop.seq == route_input.end_stop.seq,
                is_known_anchor=stop.is_known_anchor or stop.seq in {route_input.start_stop.seq, route_input.end_stop.seq},
                is_known_intermediate=stop.is_known_intermediate,
                metadata={
                    "route_name": route_input.route,
                    "cooperative": route_input.cooperative,
                    "route_type": route_input.route_type,
                    "stop_source": stop.stop_source,
                },
            )
            for stop in route_input.stops
        ]
        proposal = OrderProposal(
            method="anchored_current_sequence_artifact",
            ordered_stop_ids=tuple(stop.stop_id for stop in raw_stops),
            objective_value=0.0,
            objective_unit=self.pipeline.objective,
        )
        return self.pipeline.evaluate_order(route_input, raw_stops, proposal)

    def _build_benchmark_input(self, current_row: dict[str, Any], reference_row: dict[str, Any]) -> RouteInput:
        current_stops = list(current_row["ordered_stops"])
        reference_start = dict(reference_row["ordered_stops"][0])
        reference_end = dict(reference_row["ordered_stops"][-1])
        remaining = [
            dict(stop)
            for stop in current_stops
            if stop["stop_id"] not in {reference_start["stop_id"], reference_end["stop_id"]}
        ]
        start = next(
            (dict(stop) for stop in current_stops if stop["stop_id"] == reference_start["stop_id"]),
            reference_start,
        )
        end = next(
            (dict(stop) for stop in current_stops if stop["stop_id"] == reference_end["stop_id"]),
            reference_end,
        )
        start["is_known_anchor"] = True
        end["is_known_anchor"] = True
        ordered_stops = [start] + remaining + [end]
        for index, stop in enumerate(ordered_stops, start=1):
            stop["seq"] = index
            stop["path_fraction"] = (index - 1) / max(len(ordered_stops) - 1, 1)
        merged_row = dict(current_row)
        merged_row["route_type"] = reference_row.get("route_type") or current_row.get("route_type") or "lineal"
        merged_row["ordered_stops"] = ordered_stops
        return RouteInput.from_artifact_route(merged_row)

    def _route_bucket(self, route_name: str) -> str:
        for bucket, names in BENCHMARK_ROUTE_BUCKETS.items():
            if route_name in names:
                return bucket
        return "unclassified"

    def _default_route_order(self) -> list[str]:
        ordered: list[str] = []
        for bucket in ("easy", "medium", "hard"):
            ordered.extend(BENCHMARK_ROUTE_BUCKETS[bucket])
        return ordered

    def all_route_order(self) -> list[str]:
        return [row["route"] for row in self.reference_data["routes"]]

    def _summarize(self, route_results: list[dict[str, Any]]) -> dict[str, Any]:
        route_count = len(route_results)
        if route_count == 0:
            return {
                "route_count": 0,
                "improved_routes": 0,
                "ambiguous_routes": 0,
                "avg_current_order_agreement": 0.0,
                "avg_v2_order_agreement": 0.0,
                "avg_current_confidence": 0.0,
                "avg_v2_confidence": 0.0,
                "topology_summary": {},
            }
        return {
            "route_count": route_count,
            "improved_routes": sum(1 for route in route_results if route["improved"]),
            "ambiguous_routes": sum(1 for route in route_results if route["still_ambiguous"]),
            "manual_review_routes": sum(1 for route in route_results if route["needs_manual_review"]),
            "avg_current_order_agreement": sum(route["current_vs_reference"]["order_agreement"] for route in route_results) / route_count,
            "avg_v2_order_agreement": sum(route["v2_vs_reference"]["order_agreement"] for route in route_results) / route_count,
            "avg_current_confidence": sum(route["current_confidence"]["score"] for route in route_results) / route_count,
            "avg_v2_confidence": sum(route["v2_confidence"]["score"] for route in route_results) / route_count,
            "topology_summary": self._topology_summary(route_results),
        }

    def _topology_summary(self, route_results: list[dict[str, Any]]) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for route in route_results:
            grouped.setdefault(route["topology"], []).append(route)

        summary: dict[str, Any] = {}
        for topology, rows in grouped.items():
            count = len(rows)
            summary[topology] = {
                "route_count": count,
                "improved_routes": sum(1 for row in rows if row["improved"]),
                "ambiguous_routes": sum(1 for row in rows if row["still_ambiguous"]),
                "manual_review_routes": sum(1 for row in rows if row["needs_manual_review"]),
                "avg_current_order_agreement": sum(row["current_vs_reference"]["order_agreement"] for row in rows) / count,
                "avg_v2_order_agreement": sum(row["v2_vs_reference"]["order_agreement"] for row in rows) / count,
                "avg_current_confidence": sum(row["current_confidence"]["score"] for row in rows) / count,
                "avg_v2_confidence": sum(row["v2_confidence"]["score"] for row in rows) / count,
                "uses_main_backbone": topology == "linear",
                "recommendation": self._topology_recommendation(topology),
            }
        return summary

    def _topology_recommendation(self, topology: str) -> str:
        if topology == "linear":
            return "use_constructor_v2_backbone"
        return "keep_manual_or_special_case_pending_dedicated_loop_handling"

    def _write_examples(self, route_results: list[dict[str, Any]]) -> None:
        if not route_results:
            return
        hard_result = next((route for route in route_results if route["bucket"] == "hard"), route_results[0])
        write_json(self.output_dir / "example_input.json", hard_result["route_input"])
        write_json(self.output_dir / "example_output.json", hard_result["v2_output"])


def _length_delta_ratio(observed_m: float, reference_m: float) -> float | None:
    if reference_m <= 0.0:
        return None
    return abs(observed_m - reference_m) / reference_m


class ComparisonBenchmarkRunner:
    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        output_dir: Path = BENCHMARK_COMPARISON_ARTIFACT_DIR,
        objective: str = "duration",
    ) -> None:
        self.repo_root = repo_root or Path.cwd()
        self.output_dir = self.repo_root / output_dir
        self.objective = objective

    def run(self, route_names: Iterable[str] | None = None) -> dict[str, Any]:
        selected_routes = list(route_names or self._default_route_order())
        legacy_runner = BenchmarkRunner(
            repo_root=self.repo_root,
            output_dir=self.output_dir / "legacy_8002",
            cache_dir=self.output_dir / "cache",
            objective=self.objective,
            valhalla_base_url=LEGACY_CONSTRUCTOR_V2_VALHALLA_URL,
            scenario_name="legacy_8002",
        )
        dedicated_runner = BenchmarkRunner(
            repo_root=self.repo_root,
            output_dir=self.output_dir / "dedicated_8003",
            cache_dir=self.output_dir / "cache",
            objective=self.objective,
            valhalla_base_url=DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL,
            scenario_name="dedicated_8003",
        )
        legacy_report = legacy_runner.run(selected_routes)
        dedicated_report = dedicated_runner.run(selected_routes)

        legacy_by_route = {route["route"]: route for route in legacy_report["routes"]}
        dedicated_by_route = {route["route"]: route for route in dedicated_report["routes"]}
        route_comparisons: list[dict[str, Any]] = []
        for route_name in selected_routes:
            legacy = legacy_by_route.get(route_name)
            dedicated = dedicated_by_route.get(route_name)
            if legacy is None or dedicated is None:
                continue
            order_delta = dedicated["v2_vs_reference"]["order_agreement"] - legacy["v2_vs_reference"]["order_agreement"]
            confidence_delta = dedicated["v2_confidence"]["score"] - legacy["v2_confidence"]["score"]
            route_comparisons.append(
                {
                    "route": route_name,
                    "bucket": dedicated["bucket"],
                    "topology": dedicated["topology"],
                    "legacy": {
                        "selected_method": legacy["selected_method"],
                        "order_agreement": legacy["v2_vs_reference"]["order_agreement"],
                        "confidence": legacy["v2_confidence"],
                        "needs_manual_review": legacy["needs_manual_review"],
                    },
                    "dedicated": {
                        "selected_method": dedicated["selected_method"],
                        "order_agreement": dedicated["v2_vs_reference"]["order_agreement"],
                        "confidence": dedicated["v2_confidence"],
                        "needs_manual_review": dedicated["needs_manual_review"],
                    },
                    "order_agreement_delta": order_delta,
                    "confidence_delta": confidence_delta,
                    "improved": order_delta > 0.01 or confidence_delta > 3.0,
                    "still_ambiguous": dedicated["still_ambiguous"],
                }
            )

        comparison_report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "routes": route_comparisons,
            "legacy_summary": legacy_report["summary"],
            "dedicated_summary": dedicated_report["summary"],
            "comparison_summary": self._comparison_summary(route_comparisons, legacy_report, dedicated_report),
            "topology_comparison": self._topology_comparison(route_comparisons),
            "artifacts": {
                "legacy_report": str((self.output_dir / "legacy_8002" / "benchmark_report.json").relative_to(self.repo_root)),
                "dedicated_report": str((self.output_dir / "dedicated_8003" / "benchmark_report.json").relative_to(self.repo_root)),
            },
        }
        write_json(self.output_dir / "comparison_report.json", comparison_report)
        write_text(self.output_dir / "comparison_report.md", self._render_comparison_markdown(comparison_report))
        return comparison_report

    def _default_route_order(self) -> list[str]:
        ordered: list[str] = []
        for bucket in ("easy", "medium", "hard"):
            ordered.extend(BENCHMARK_ROUTE_BUCKETS[bucket])
        return ordered

    def all_route_order(self) -> list[str]:
        runner = BenchmarkRunner(repo_root=self.repo_root)
        return runner.all_route_order()

    def _comparison_summary(
        self,
        route_comparisons: list[dict[str, Any]],
        legacy_report: dict[str, Any],
        dedicated_report: dict[str, Any],
    ) -> dict[str, Any]:
        route_count = len(route_comparisons)
        if route_count == 0:
            return {
                "route_count": 0,
                "improved_routes": 0,
                "ambiguous_routes": 0,
                "avg_order_agreement_delta": 0.0,
                "avg_confidence_delta": 0.0,
                "materially_stronger": False,
            }
        avg_order_delta = sum(route["order_agreement_delta"] for route in route_comparisons) / route_count
        avg_confidence_delta = sum(route["confidence_delta"] for route in route_comparisons) / route_count
        return {
            "route_count": route_count,
            "improved_routes": sum(1 for route in route_comparisons if route["improved"]),
            "ambiguous_routes": sum(1 for route in route_comparisons if route["still_ambiguous"]),
            "avg_order_agreement_delta": avg_order_delta,
            "avg_confidence_delta": avg_confidence_delta,
            "legacy_avg_order_agreement": legacy_report["summary"]["avg_v2_order_agreement"],
            "dedicated_avg_order_agreement": dedicated_report["summary"]["avg_v2_order_agreement"],
            "legacy_avg_confidence": legacy_report["summary"]["avg_v2_confidence"],
            "dedicated_avg_confidence": dedicated_report["summary"]["avg_v2_confidence"],
            "materially_stronger": avg_confidence_delta > 5.0 or avg_order_delta > 0.03,
        }

    def _topology_comparison(self, route_comparisons: list[dict[str, Any]]) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for route in route_comparisons:
            grouped.setdefault(route["topology"], []).append(route)

        summary: dict[str, Any] = {}
        for topology, rows in grouped.items():
            count = len(rows)
            avg_order_delta = sum(route["order_agreement_delta"] for route in rows) / count
            avg_conf_delta = sum(route["confidence_delta"] for route in rows) / count
            summary[topology] = {
                "route_count": count,
                "improved_routes": sum(1 for route in rows if route["improved"]),
                "ambiguous_routes": sum(1 for route in rows if route["still_ambiguous"]),
                "manual_review_routes": sum(1 for route in rows if route["dedicated"]["needs_manual_review"]),
                "legacy_avg_order_agreement": sum(route["legacy"]["order_agreement"] for route in rows) / count,
                "dedicated_avg_order_agreement": sum(route["dedicated"]["order_agreement"] for route in rows) / count,
                "legacy_avg_confidence": sum(route["legacy"]["confidence"]["score"] for route in rows) / count,
                "dedicated_avg_confidence": sum(route["dedicated"]["confidence"]["score"] for route in rows) / count,
                "avg_order_agreement_delta": avg_order_delta,
                "avg_confidence_delta": avg_conf_delta,
                "recommendation": (
                    "use_constructor_v2_backbone"
                    if topology == "linear"
                    else "keep_manual_or_special_case_pending_dedicated_loop_handling"
                ),
            }
        return summary

    def _render_comparison_markdown(self, report: dict[str, Any]) -> str:
        summary = report["comparison_summary"]
        lines = [
            "# Constructor V2 Legacy vs Dedicated Valhalla",
            "",
            f"- Generated: {report['generated_at']}",
            f"- Routes tested: {summary['route_count']}",
            f"- Improved with dedicated 8003: {summary['improved_routes']}",
            f"- Ambiguous on dedicated 8003: {summary['ambiguous_routes']}",
            f"- Legacy avg order agreement: {summary['legacy_avg_order_agreement']:.3f}",
            f"- Dedicated avg order agreement: {summary['dedicated_avg_order_agreement']:.3f}",
            f"- Legacy avg confidence: {summary['legacy_avg_confidence']:.1f}",
            f"- Dedicated avg confidence: {summary['dedicated_avg_confidence']:.1f}",
            f"- Materially stronger: {'yes' if summary['materially_stronger'] else 'no'}",
            "",
            "## Topology",
            "",
        ]
        for topology, topo_summary in sorted(report.get("topology_comparison", {}).items()):
            lines.extend(
                [
                    f"- {topology}: routes={topo_summary['route_count']} improved={topo_summary['improved_routes']} "
                    f"legacy_agree={topo_summary['legacy_avg_order_agreement']:.3f} "
                    f"dedicated_agree={topo_summary['dedicated_avg_order_agreement']:.3f} "
                    f"legacy_conf={topo_summary['legacy_avg_confidence']:.1f} "
                    f"dedicated_conf={topo_summary['dedicated_avg_confidence']:.1f} "
                    f"recommendation={topo_summary['recommendation']}",
                ]
            )
        lines.extend(
            [
                "",
                "| Route | Bucket | Topology | Legacy agree | Dedicated agree | Delta | Legacy conf | Dedicated conf | Delta | Improved | Dedicated method |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
            ]
        )
        for route in report["routes"]:
            lines.append(
                "| {route} | {bucket} | {topology} | {legacy_agree:.3f} | {dedicated_agree:.3f} | {order_delta:.3f} | {legacy_conf:.1f} | {dedicated_conf:.1f} | {confidence_delta:.1f} | {improved} | {method} |".format(
                    route=route["route"],
                    bucket=route["bucket"],
                    topology=route["topology"],
                    legacy_agree=route["legacy"]["order_agreement"],
                    dedicated_agree=route["dedicated"]["order_agreement"],
                    order_delta=route["order_agreement_delta"],
                    legacy_conf=route["legacy"]["confidence"]["score"],
                    dedicated_conf=route["dedicated"]["confidence"]["score"],
                    confidence_delta=route["confidence_delta"],
                    improved="yes" if route["improved"] else "no",
                    method=route["dedicated"]["selected_method"],
                )
            )
        return "\n".join(lines) + "\n"
