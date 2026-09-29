#!/usr/bin/env python3
"""Full 39-route benchmark: OR-Tools baseline vs CP-SAT + preprocessing.

Runs Constructor V2 against all 39 Valle routes using:
1. OR-Tools solver (current baseline)
2. CP-SAT solver with side-of-road preprocessing (upgrade)

Produces benchmark_39_route_report.json and benchmark_39_route_report.md
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Add the route_constructor package to path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "phase3_routes" / "services" / "route_constructor"))

from src.constructor_v2.clients.valhalla_client import ValhallaClient
from src.constructor_v2.constants import DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL, DEFAULT_CACHE_DIR
from src.constructor_v2.evaluation.metrics import compare_stop_orders, confidence_summary
from src.constructor_v2.pipeline import ConstructorV2Pipeline
from src.constructor_v2.schemas.route_input import NormalizedStop, RouteInput
from src.constructor_v2.schemas.route_output import OrderProposal
from src.constructor_v2.topology import canonical_topology, is_special_case_topology


VALHALLA_URL = DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL
CACHE_DIR = REPO_ROOT / DEFAULT_CACHE_DIR / "benchmark_39"
OUTPUT_DIR = REPO_ROOT / "constructor_artifacts" / "benchmark_39_cpsat"

REFERENCE_FILE = REPO_ROOT / "CONSTRUCTOR" / "valle_v5_4_COHERENT_v17 (1).json"
SEQUENCES_FILE = REPO_ROOT / "CONSTRUCTOR" / "valle_v5_4_sequences_final (2).json"
OVERRIDES_FILE = REPO_ROOT / "CONSTRUCTOR" / "sequence_overrides_v1.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def build_benchmark_input(current_row: dict, reference_row: dict) -> RouteInput:
    """Build input from current stops with reference termini."""
    current_stops = list(current_row["ordered_stops"])
    reference_start = dict(reference_row["ordered_stops"][0])
    reference_end = dict(reference_row["ordered_stops"][-1])
    remaining = [
        dict(s) for s in current_stops
        if s["stop_id"] not in {reference_start["stop_id"], reference_end["stop_id"]}
    ]
    start = next(
        (dict(s) for s in current_stops if s["stop_id"] == reference_start["stop_id"]),
        reference_start,
    )
    end = next(
        (dict(s) for s in current_stops if s["stop_id"] == reference_end["stop_id"]),
        reference_end,
    )
    start["is_known_anchor"] = True
    end["is_known_anchor"] = True
    ordered_stops = [start] + remaining + [end]
    for idx, s in enumerate(ordered_stops, start=1):
        s["seq"] = idx
        s["path_fraction"] = (idx - 1) / max(len(ordered_stops) - 1, 1)
    merged = dict(current_row)
    merged["route_type"] = reference_row.get("route_type") or current_row.get("route_type") or "lineal"
    merged["ordered_stops"] = ordered_stops
    return RouteInput.from_artifact_route(merged)


def evaluate_current_sequence(pipeline: ConstructorV2Pipeline, route_input: RouteInput):
    """Evaluate the current artifact sequence (as-is) for baseline comparison."""
    raw_stops = [
        NormalizedStop(
            order_hint=s.seq,
            stop_id=s.stop_id,
            stop_name=s.stop_name,
            lat=s.lat,
            lon=s.lon,
            source_stop_ids=(s.stop_id,),
            source_indices=(s.seq,),
            source_names=(s.stop_name,),
            representative_score=float(s.on_route_score or 0.0),
            weak_candidate=False,
            optional_penalty=None,
            is_fixed_start=s.seq == route_input.start_stop.seq,
            is_fixed_end=s.seq == route_input.end_stop.seq,
            is_known_anchor=s.is_known_anchor or s.seq in {route_input.start_stop.seq, route_input.end_stop.seq},
            is_known_intermediate=s.is_known_intermediate,
            metadata={"route_name": route_input.route, "stop_source": s.stop_source},
        )
        for s in route_input.stops
    ]
    proposal = OrderProposal(
        method="current_artifact_sequence",
        ordered_stop_ids=tuple(s.stop_id for s in raw_stops),
        objective_value=0.0,
        objective_unit=pipeline.objective,
    )
    return pipeline.evaluate_order(route_input, raw_stops, proposal)


def run_benchmark():
    print("=" * 60)
    print("Constructor V2 — Full 39-Route Benchmark")
    print("CP-SAT + Preprocessing vs OR-Tools Baseline")
    print("=" * 60)

    # Load artifacts
    reference_data = load_json(REFERENCE_FILE)
    sequences_data = load_json(SEQUENCES_FILE)
    overrides_data = load_json(OVERRIDES_FILE)

    reference_by_route = {r["route"]: r for r in reference_data["routes"]}
    override_routes = {item["route"] for item in overrides_data.get("overrides", [])}
    all_route_names = [r["route"] for r in reference_data["routes"]]

    print(f"Reference routes: {len(all_route_names)}")
    print(f"Valhalla: {VALHALLA_URL}")
    print(f"Cache: {CACHE_DIR}")
    print()

    # Create pipelines
    client = ValhallaClient(base_url=VALHALLA_URL)
    client.refresh_capabilities(force=True)
    print(f"Valhalla capabilities: {client.endpoint_support}")

    # OR-Tools pipeline (baseline)
    ortools_pipeline = ConstructorV2Pipeline(
        client=client,
        cache_dir=CACHE_DIR,
        objective="duration",
        solver_mode="ortools",
        enable_preprocessing=False,
    )

    # CP-SAT pipeline (with preprocessing)
    cpsat_pipeline = ConstructorV2Pipeline(
        client=client,
        cache_dir=CACHE_DIR,
        objective="duration",
        solver_mode="cpsat",
        enable_preprocessing=True,
    )

    route_results = []
    total_preprocess_stats = {
        "opposite_direction": 0,
        "duplicates": 0,
        "outliers": 0,
        "flagged_total": 0,
    }

    for idx, route_name in enumerate(all_route_names):
        print(f"\n[{idx+1}/{len(all_route_names)}] {route_name}...", end=" ", flush=True)

        current_row = next((r for r in sequences_data["routes"] if r["route"] == route_name), None)
        reference_row = reference_by_route.get(route_name)
        if current_row is None or reference_row is None:
            print("SKIP (missing data)")
            continue

        try:
            route_input = build_benchmark_input(current_row, reference_row)
            topology = canonical_topology(route_input.route_type)
            reference_ids = [s["stop_id"] for s in reference_row["ordered_stops"]]

            # Current sequence baseline
            t0 = time.monotonic()
            current_output = evaluate_current_sequence(ortools_pipeline, route_input)
            t_current = time.monotonic() - t0

            # OR-Tools V2
            t0 = time.monotonic()
            ortools_output = ortools_pipeline.run(route_input)
            t_ortools = time.monotonic() - t0

            # CP-SAT V2 + preprocessing
            t0 = time.monotonic()
            cpsat_output = cpsat_pipeline.run(route_input)
            t_cpsat = time.monotonic() - t0

            # Compare against reference
            current_ids = [s.stop_id for s in current_output.final_stops]
            ortools_ids = [s.stop_id for s in ortools_output.final_stops]
            cpsat_ids = [s.stop_id for s in cpsat_output.final_stops]

            current_vs_ref = compare_stop_orders(reference_ids, current_ids)
            ortools_vs_ref = compare_stop_orders(reference_ids, ortools_ids)
            cpsat_vs_ref = compare_stop_orders(reference_ids, cpsat_ids)

            # Preprocessing stats
            preprocess_diag = cpsat_output.diagnostics.get("preprocessing", {})
            preprocess_stats = preprocess_diag.get("stats", {})
            for key in total_preprocess_stats:
                total_preprocess_stats[key] += preprocess_stats.get(key, 0)

            # CP-SAT skip info
            cpsat_diag = cpsat_output.diagnostics.get("cpsat", {})
            cpsat_skipped = cpsat_diag.get("skipped_count", 0)
            cpsat_status = cpsat_diag.get("solver_status", "N/A")

            # Determine winner
            cpsat_better = (
                cpsat_vs_ref["order_agreement"] > ortools_vs_ref["order_agreement"]
                or cpsat_output.confidence.score > ortools_output.confidence.score + 5.0
            )
            ortools_better = (
                ortools_vs_ref["order_agreement"] > cpsat_vs_ref["order_agreement"]
                or ortools_output.confidence.score > cpsat_output.confidence.score + 5.0
            )

            result = {
                "route": route_name,
                "topology": topology,
                "route_type": route_input.route_type,
                "special_case": is_special_case_topology(route_input.route_type),
                "override_present": route_name in override_routes,
                "stops_in": len(route_input.stops),
                "stops_after_preprocess": len(cpsat_output.final_stops),
                "current_confidence": confidence_summary(current_output),
                "ortools_confidence": confidence_summary(ortools_output),
                "cpsat_confidence": confidence_summary(cpsat_output),
                "current_vs_ref": current_vs_ref,
                "ortools_vs_ref": ortools_vs_ref,
                "cpsat_vs_ref": cpsat_vs_ref,
                "cpsat_skipped": cpsat_skipped,
                "cpsat_solver_status": cpsat_status,
                "cpsat_better": cpsat_better,
                "ortools_better": ortools_better,
                "equal": not cpsat_better and not ortools_better,
                "preprocess_stats": preprocess_stats,
                "timing": {
                    "current_s": round(t_current, 2),
                    "ortools_s": round(t_ortools, 2),
                    "cpsat_s": round(t_cpsat, 2),
                },
                "ortools_method": ortools_output.selected_method,
                "cpsat_method": cpsat_output.selected_method,
                "ortools_stop_count": len(ortools_output.final_stops),
                "cpsat_stop_count": len(cpsat_output.final_stops),
                "ortools_distance_m": ortools_output.metrics.get("geometry_distance_m", 0),
                "cpsat_distance_m": cpsat_output.metrics.get("geometry_distance_m", 0),
            }
            route_results.append(result)

            status = "CP-SAT+" if cpsat_better else ("OR-Tools+" if ortools_better else "EQUAL")
            print(
                f"{status} | ORT={ortools_vs_ref['order_agreement']:.2f}/{ortools_output.confidence.score:.0f} "
                f"CPSAT={cpsat_vs_ref['order_agreement']:.2f}/{cpsat_output.confidence.score:.0f} "
                f"skip={cpsat_skipped} [{t_ortools:.1f}s/{t_cpsat:.1f}s]"
            )

        except Exception as exc:
            print(f"ERROR: {exc}")
            traceback.print_exc()
            route_results.append({
                "route": route_name,
                "error": str(exc),
                "topology": canonical_topology(current_row.get("route_type", "lineal")),
            })

    # --- Generate report ---
    report = build_report(route_results, total_preprocess_stats, all_route_names)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / "benchmark_39_route_report.json"
    md_path = OUTPUT_DIR / "benchmark_39_route_report.md"

    json_path.write_text(json.dumps(report, indent=2, default=str))
    md_path.write_text(render_markdown(report))

    print(f"\n{'=' * 60}")
    print(f"Report written to:")
    print(f"  JSON: {json_path}")
    print(f"  MD:   {md_path}")
    print(f"{'=' * 60}")

    # Print summary
    summary = report["summary"]
    print(f"\n--- SUMMARY ---")
    print(f"Routes processed: {summary['routes_processed']} / {summary['routes_total']}")
    print(f"CP-SAT improved:  {summary['cpsat_improved']}")
    print(f"OR-Tools better:  {summary['ortools_better']}")
    print(f"Equal:            {summary['equal']}")
    print(f"Errors:           {summary['errors']}")
    print(f"Avg OR-Tools order agreement: {summary['avg_ortools_agreement']:.3f}")
    print(f"Avg CP-SAT order agreement:   {summary['avg_cpsat_agreement']:.3f}")
    print(f"Avg OR-Tools confidence:       {summary['avg_ortools_confidence']:.1f}")
    print(f"Avg CP-SAT confidence:         {summary['avg_cpsat_confidence']:.1f}")
    print(f"Total stops skipped by CP-SAT: {summary['total_cpsat_skipped']}")
    print(f"Preprocessing: opposite={total_preprocess_stats['opposite_direction']} "
          f"dup={total_preprocess_stats['duplicates']} outlier={total_preprocess_stats['outliers']}")


def build_report(route_results, preprocess_stats, all_route_names):
    valid = [r for r in route_results if "error" not in r]
    errors = [r for r in route_results if "error" in r]

    routes_processed = len(valid)
    cpsat_improved = sum(1 for r in valid if r.get("cpsat_better"))
    ortools_better = sum(1 for r in valid if r.get("ortools_better"))
    equal = sum(1 for r in valid if r.get("equal"))

    avg_ortools_agreement = (
        sum(r["ortools_vs_ref"]["order_agreement"] for r in valid) / routes_processed
        if routes_processed else 0.0
    )
    avg_cpsat_agreement = (
        sum(r["cpsat_vs_ref"]["order_agreement"] for r in valid) / routes_processed
        if routes_processed else 0.0
    )
    avg_ortools_conf = (
        sum(r["ortools_confidence"]["score"] for r in valid) / routes_processed
        if routes_processed else 0.0
    )
    avg_cpsat_conf = (
        sum(r["cpsat_confidence"]["score"] for r in valid) / routes_processed
        if routes_processed else 0.0
    )
    total_skipped = sum(r.get("cpsat_skipped", 0) for r in valid)

    # Topology breakdown
    topo_groups = {}
    for r in valid:
        t = r["topology"]
        topo_groups.setdefault(t, []).append(r)

    topology_summary = {}
    for topo, rows in topo_groups.items():
        n = len(rows)
        topology_summary[topo] = {
            "count": n,
            "cpsat_improved": sum(1 for r in rows if r.get("cpsat_better")),
            "ortools_better": sum(1 for r in rows if r.get("ortools_better")),
            "equal": sum(1 for r in rows if r.get("equal")),
            "avg_ortools_agreement": sum(r["ortools_vs_ref"]["order_agreement"] for r in rows) / n,
            "avg_cpsat_agreement": sum(r["cpsat_vs_ref"]["order_agreement"] for r in rows) / n,
            "avg_ortools_confidence": sum(r["ortools_confidence"]["score"] for r in rows) / n,
            "avg_cpsat_confidence": sum(r["cpsat_confidence"]["score"] for r in rows) / n,
        }

    # Classify routes by confidence
    strong = [r for r in valid if r["cpsat_confidence"]["score"] >= 85]
    acceptable = [r for r in valid if 70 <= r["cpsat_confidence"]["score"] < 85]
    ambiguous = [r for r in valid if 50 <= r["cpsat_confidence"]["score"] < 70]
    manual = [r for r in valid if r["cpsat_confidence"]["score"] < 50]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "routes_total": len(all_route_names),
            "routes_processed": routes_processed,
            "errors": len(errors),
            "cpsat_improved": cpsat_improved,
            "ortools_better": ortools_better,
            "equal": equal,
            "avg_ortools_agreement": avg_ortools_agreement,
            "avg_cpsat_agreement": avg_cpsat_agreement,
            "avg_ortools_confidence": avg_ortools_conf,
            "avg_cpsat_confidence": avg_cpsat_conf,
            "total_cpsat_skipped": total_skipped,
            "preprocessing": preprocess_stats,
            "topology": topology_summary,
        },
        "classification": {
            "strong": [r["route"] for r in strong],
            "acceptable": [r["route"] for r in acceptable],
            "ambiguous": [r["route"] for r in ambiguous],
            "needs_manual_review": [r["route"] for r in manual],
        },
        "routes": route_results,
        "errors": errors,
    }


def render_markdown(report):
    s = report["summary"]
    lines = [
        "# Constructor V2 — Full 39-Route Benchmark",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## Summary",
        "",
        f"- Routes processed: {s['routes_processed']} / {s['routes_total']}",
        f"- CP-SAT improved: {s['cpsat_improved']}",
        f"- OR-Tools better: {s['ortools_better']}",
        f"- Equal: {s['equal']}",
        f"- Errors: {s['errors']}",
        "",
        "### Metrics",
        "",
        f"| Metric | OR-Tools | CP-SAT |",
        f"| --- | ---: | ---: |",
        f"| Avg order agreement | {s['avg_ortools_agreement']:.3f} | {s['avg_cpsat_agreement']:.3f} |",
        f"| Avg confidence | {s['avg_ortools_confidence']:.1f} | {s['avg_cpsat_confidence']:.1f} |",
        f"| Total stops skipped | 0 | {s['total_cpsat_skipped']} |",
        "",
        "### Preprocessing Impact",
        "",
        f"- Opposite-direction flagged: {s['preprocessing']['opposite_direction']}",
        f"- Duplicates flagged: {s['preprocessing']['duplicates']}",
        f"- Outliers flagged: {s['preprocessing']['outliers']}",
        f"- Total flagged: {s['preprocessing']['flagged_total']}",
        "",
        "### Topology Breakdown",
        "",
        "| Topology | Count | CP-SAT+ | OR-Tools+ | Equal | Avg ORT agree | Avg CPSAT agree | Avg ORT conf | Avg CPSAT conf |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for topo, ts in sorted(s.get("topology", {}).items()):
        lines.append(
            f"| {topo} | {ts['count']} | {ts['cpsat_improved']} | {ts['ortools_better']} | "
            f"{ts['equal']} | {ts['avg_ortools_agreement']:.3f} | {ts['avg_cpsat_agreement']:.3f} | "
            f"{ts['avg_ortools_confidence']:.1f} | {ts['avg_cpsat_confidence']:.1f} |"
        )

    lines.extend([
        "",
        "## Per-Route Results",
        "",
        "| Route | Topology | Stops In | Stops After | ORT Agreement | CPSAT Agreement | CPSAT Skipped | ORT Conf | CPSAT Conf | Winner |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ])

    valid = [r for r in report["routes"] if "error" not in r]
    for r in valid:
        winner = "CP-SAT" if r.get("cpsat_better") else ("OR-Tools" if r.get("ortools_better") else "Equal")
        lines.append(
            f"| {r['route']} | {r['topology']} | {r['stops_in']} | {r.get('cpsat_stop_count', '?')} | "
            f"{r['ortools_vs_ref']['order_agreement']:.3f} | {r['cpsat_vs_ref']['order_agreement']:.3f} | "
            f"{r.get('cpsat_skipped', 0)} | {r['ortools_confidence']['score']:.0f} | "
            f"{r['cpsat_confidence']['score']:.0f} | {winner} |"
        )

    # Classification
    cl = report.get("classification", {})
    lines.extend([
        "",
        "## Route Classification (by CP-SAT confidence)",
        "",
        f"### Strong (>= 85): {len(cl.get('strong', []))}",
        "",
    ])
    for name in cl.get("strong", []):
        lines.append(f"- {name}")

    lines.extend([
        "",
        f"### Acceptable (70-84): {len(cl.get('acceptable', []))}",
        "",
    ])
    for name in cl.get("acceptable", []):
        lines.append(f"- {name}")

    lines.extend([
        "",
        f"### Ambiguous (50-69): {len(cl.get('ambiguous', []))}",
        "",
    ])
    for name in cl.get("ambiguous", []):
        lines.append(f"- {name}")

    lines.extend([
        "",
        f"### Needs Manual Review (< 50): {len(cl.get('needs_manual_review', []))}",
        "",
    ])
    for name in cl.get("needs_manual_review", []):
        lines.append(f"- {name}")

    # Errors
    errors = report.get("errors", [])
    if errors:
        lines.extend(["", "## Errors", ""])
        for e in errors:
            lines.append(f"- {e['route']}: {e.get('error', 'unknown')}")

    # Regressions
    regressions = [r for r in valid if r.get("ortools_better")]
    if regressions:
        lines.extend(["", "## Regressions (OR-Tools better than CP-SAT)", ""])
        for r in regressions:
            lines.append(
                f"- {r['route']}: ORT={r['ortools_vs_ref']['order_agreement']:.3f}/{r['ortools_confidence']['score']:.0f} "
                f"vs CPSAT={r['cpsat_vs_ref']['order_agreement']:.3f}/{r['cpsat_confidence']['score']:.0f}"
            )

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    run_benchmark()
