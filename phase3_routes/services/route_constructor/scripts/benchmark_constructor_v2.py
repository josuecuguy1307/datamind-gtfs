from __future__ import annotations

import argparse
from pathlib import Path

from src.constructor_v2.constants import (
    BENCHMARK_ARTIFACT_DIR,
    BENCHMARK_COMPARISON_ARTIFACT_DIR,
    DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL,
)
from src.constructor_v2.evaluation.benchmark_runner import BenchmarkRunner, ComparisonBenchmarkRunner


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Constructor V2 benchmark against Valle artifacts")
    parser.add_argument("--route", action="append", dest="routes", help="Specific route name to benchmark")
    parser.add_argument(
        "--all-routes",
        action="store_true",
        help="Run the full 39-route benchmark instead of the representative subset",
    )
    parser.add_argument(
        "--output-dir",
        default=str(BENCHMARK_ARTIFACT_DIR),
        help="Relative output directory for benchmark artifacts",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL,
        help="Valhalla base URL for the benchmark scenario",
    )
    parser.add_argument(
        "--scenario-name",
        default="constructor_v2_dedicated",
        help="Scenario label used for cache/report grouping",
    )
    parser.add_argument(
        "--compare-legacy-dedicated",
        action="store_true",
        help="Run side-by-side comparison between legacy 8002 and dedicated 8003 Constructor V2 behavior",
    )
    args = parser.parse_args()

    if args.compare_legacy_dedicated:
        compare_output_dir = (
            Path(BENCHMARK_COMPARISON_ARTIFACT_DIR)
            if args.output_dir == str(BENCHMARK_ARTIFACT_DIR)
            else Path(args.output_dir)
        )
        runner = ComparisonBenchmarkRunner(output_dir=compare_output_dir)
        selected_routes = runner.all_route_order() if args.all_routes else args.routes
        report = runner.run(route_names=selected_routes)
        print(
            f"routes={report['comparison_summary']['route_count']} "
            f"improved={report['comparison_summary']['improved_routes']} "
            f"materially_stronger={report['comparison_summary']['materially_stronger']}"
        )
        return

    runner = BenchmarkRunner(
        output_dir=Path(args.output_dir),
        valhalla_base_url=args.base_url,
        scenario_name=args.scenario_name,
    )
    selected_routes = runner.all_route_order() if args.all_routes else args.routes
    report = runner.run(route_names=selected_routes)
    print(
        f"routes={report['summary']['route_count']} "
        f"improved={report['summary']['improved_routes']} "
        f"ambiguous={report['summary']['ambiguous_routes']}"
    )


if __name__ == "__main__":
    main()
