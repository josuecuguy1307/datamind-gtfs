#!/usr/bin/env python3
"""Construct all 39 Valle routes using best-of-both solvers (OR-Tools + CP-SAT).

Produces:
  - constructor_artifacts/valle_v2_CONSTRUCTED_39.json  (master artifact)
  - constructor_artifacts/valle_v2_CONSTRUCTED_39/geojson/<route_slug>.geojson  (per-route)
  - constructor_artifacts/valle_v2_CONSTRUCTED_39/comparison_vs_reference.md
"""

from __future__ import annotations

import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "phase3_routes" / "services" / "route_constructor"))

from src.constructor_v2.clients.valhalla_client import ValhallaClient
from src.constructor_v2.constants import DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL, DEFAULT_CACHE_DIR
from src.constructor_v2.common import order_agreement_ratio, haversine_m
from src.constructor_v2.evaluation.metrics import compare_stop_orders, confidence_summary
from src.constructor_v2.pipeline import ConstructorV2Pipeline
from src.constructor_v2.schemas.route_input import NormalizedStop, RouteInput
from src.constructor_v2.schemas.route_output import OrderProposal, RouteOutput
from src.constructor_v2.topology import canonical_topology, is_special_case_topology

VALHALLA_URL = DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL
CACHE_DIR = REPO_ROOT / DEFAULT_CACHE_DIR / "construct_39"

REFERENCE_FILE = REPO_ROOT / "CONSTRUCTOR" / "valle_v5_4_COHERENT_v17 (1).json"
SEQUENCES_FILE = REPO_ROOT / "CONSTRUCTOR" / "valle_v5_4_sequences_final (2).json"

OUTPUT_DIR = REPO_ROOT / "constructor_artifacts" / "valle_v2_CONSTRUCTED_39"
GEOJSON_DIR = OUTPUT_DIR / "geojson"
MASTER_FILE = REPO_ROOT / "constructor_artifacts" / "valle_v2_CONSTRUCTED_39.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def slug(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")[:80]


def build_input(current_row: dict, reference_row: dict) -> RouteInput:
    current_stops = list(current_row["ordered_stops"])
    ref_start = dict(reference_row["ordered_stops"][0])
    ref_end = dict(reference_row["ordered_stops"][-1])
    remaining = [
        dict(s) for s in current_stops
        if s["stop_id"] not in {ref_start["stop_id"], ref_end["stop_id"]}
    ]
    start = next(
        (dict(s) for s in current_stops if s["stop_id"] == ref_start["stop_id"]),
        ref_start,
    )
    end = next(
        (dict(s) for s in current_stops if s["stop_id"] == ref_end["stop_id"]),
        ref_end,
    )
    start["is_known_anchor"] = True
    end["is_known_anchor"] = True
    ordered = [start] + remaining + [end]
    for idx, s in enumerate(ordered, 1):
        s["seq"] = idx
        s["path_fraction"] = (idx - 1) / max(len(ordered) - 1, 1)
    merged = dict(current_row)
    merged["route_type"] = reference_row.get("route_type") or current_row.get("route_type") or "lineal"
    merged["ordered_stops"] = ordered
    return RouteInput.from_artifact_route(merged)


def classify_confidence(score: float, topology: str) -> str:
    if topology != "linear":
        return "special_case"
    if score >= 85:
        return "strong"
    if score >= 70:
        return "acceptable"
    if score >= 50:
        return "ambiguous"
    return "needs_manual_review"


def route_geojson_feature_collection(route_name: str, output: RouteOutput) -> dict:
    """Build a GeoJSON FeatureCollection with route line + stop points."""
    features = []

    # Route geometry
    if output.geometry_geojson:
        geom = output.geometry_geojson
        if geom.get("type") == "FeatureCollection":
            features.extend(geom.get("features", []))
        elif geom.get("type") == "Feature":
            features.append(geom)
        else:
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {"type": "route_geometry", "route": route_name},
            })

    # Stop points
    for idx, stop in enumerate(output.final_stops):
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [stop.lon, stop.lat]},
            "properties": {
                "type": "stop",
                "seq": idx + 1,
                "stop_id": stop.stop_id,
                "stop_name": stop.stop_name,
                "is_fixed_start": stop.is_fixed_start,
                "is_fixed_end": stop.is_fixed_end,
                "is_known_anchor": stop.is_known_anchor,
                "weak_candidate": stop.weak_candidate,
            },
        })

    # Dropped stop points (skipped by solver)
    dropped_ids = set(output.dropped_stop_ids)
    for stop in output.normalized_stops:
        if stop.stop_id in dropped_ids:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [stop.lon, stop.lat]},
                "properties": {
                    "type": "dropped_stop",
                    "stop_id": stop.stop_id,
                    "stop_name": stop.stop_name,
                    "reason": "skipped_by_solver",
                },
            })

    return {"type": "FeatureCollection", "features": features}


def run_construction():
    print("=" * 70)
    print("Constructor V2 — Full 39-Route CONSTRUCTION Run")
    print("Best-of-both solvers (OR-Tools + CP-SAT) with preprocessing")
    print("=" * 70)

    reference_data = load_json(REFERENCE_FILE)
    sequences_data = load_json(SEQUENCES_FILE)

    reference_by_route = {r["route"]: r for r in reference_data["routes"]}
    all_route_names = [r["route"] for r in reference_data["routes"]]

    print(f"Routes: {len(all_route_names)}")
    print(f"Valhalla: {VALHALLA_URL}")
    print(f"Output: {MASTER_FILE}")
    print()

    client = ValhallaClient(base_url=VALHALLA_URL)
    client.refresh_capabilities(force=True)
    print(f"Valhalla OK: {client.endpoint_support}")
    print()

    # Best-of-both pipeline: runs OR-Tools AND CP-SAT with corridor constraints
    pipeline = ConstructorV2Pipeline(
        client=client,
        cache_dir=CACHE_DIR,
        objective="duration",
        solver_mode="both",
        enable_preprocessing=True,
        enable_corridor_constraints=True,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    GEOJSON_DIR.mkdir(parents=True, exist_ok=True)

    constructed_routes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    total_stops_constructed = 0
    total_stops_dropped = 0

    for idx, route_name in enumerate(all_route_names):
        print(f"[{idx+1:2d}/{len(all_route_names)}] {route_name}...", end=" ", flush=True)

        current_row = next((r for r in sequences_data["routes"] if r["route"] == route_name), None)
        reference_row = reference_by_route.get(route_name)
        if current_row is None or reference_row is None:
            print("SKIP (missing data)")
            errors.append({"route": route_name, "error": "missing_data"})
            continue

        try:
            t0 = time.monotonic()
            route_input = build_input(current_row, reference_row)
            topology = canonical_topology(route_input.route_type)

            # RUN CONSTRUCTION
            output = pipeline.run(route_input)

            elapsed = time.monotonic() - t0

            # Compare vs reference
            reference_ids = [s["stop_id"] for s in reference_row["ordered_stops"]]
            constructed_ids = [s.stop_id for s in output.final_stops]
            vs_ref = compare_stop_orders(reference_ids, constructed_ids)

            # Classification
            classification = classify_confidence(output.confidence.score, topology)
            auto_accept = classification in ("strong", "acceptable")

            # Preprocessing stats
            preprocess_diag = output.diagnostics.get("preprocessing", {})
            preprocess_stats = preprocess_diag.get("stats", {})

            # Build ordered stops for output
            ordered_stops_out = []
            for seq_idx, stop in enumerate(output.final_stops):
                ordered_stops_out.append({
                    "seq": seq_idx + 1,
                    "stop_id": stop.stop_id,
                    "stop_name": stop.stop_name,
                    "lat": stop.lat,
                    "lon": stop.lon,
                    "is_fixed_start": stop.is_fixed_start,
                    "is_fixed_end": stop.is_fixed_end,
                    "is_known_anchor": stop.is_known_anchor,
                    "weak_candidate": stop.weak_candidate,
                    "representative_score": stop.representative_score,
                })

            # Dropped stops
            dropped_stops = []
            dropped_ids = set(output.dropped_stop_ids)
            for stop in output.normalized_stops:
                if stop.stop_id in dropped_ids:
                    dropped_stops.append({
                        "stop_id": stop.stop_id,
                        "stop_name": stop.stop_name,
                        "lat": stop.lat,
                        "lon": stop.lon,
                        "reason": "skipped_by_solver",
                    })

            # Geometry metrics
            distance_m = output.metrics.get("geometry_distance_m", 0.0)
            duration_s = output.metrics.get("geometry_duration_s", 0.0)

            route_result = {
                "route": route_name,
                "cooperative": route_input.cooperative,
                "route_type": route_input.route_type,
                "topology": topology,
                "classification": classification,
                "auto_accept": auto_accept,
                "confidence": {
                    "label": output.confidence.label,
                    "score": output.confidence.score,
                    "auto_accept": output.confidence.auto_accept,
                    "reasons": output.confidence.reasons,
                },
                "solver_used": output.selected_method,
                "stops_input": len(route_input.stops),
                "stops_constructed": len(output.final_stops),
                "stops_dropped": len(output.dropped_stop_ids),
                "ordered_stops": ordered_stops_out,
                "dropped_stops": dropped_stops,
                "geometry_distance_m": distance_m,
                "geometry_distance_km": round(distance_m / 1000.0, 2),
                "geometry_duration_s": duration_s,
                "geometry_duration_min": round(duration_s / 60.0, 1),
                "geometry_geojson": output.geometry_geojson,
                "vs_reference": vs_ref,
                "preprocessing": {
                    "flagged_reasons": preprocess_diag.get("flagged_reasons", {}),
                    "stats": preprocess_stats,
                },
                "construction_time_s": round(elapsed, 2),
                "validation": {
                    name: {
                        "status": getattr(v, "status", "unknown"),
                        "score": getattr(v, "score", 0.0),
                        "issues": getattr(v, "issues", []),
                    }
                    for name, v in output.validation.items()
                },
            }
            constructed_routes.append(route_result)
            total_stops_constructed += len(output.final_stops)
            total_stops_dropped += len(output.dropped_stop_ids)

            # Save individual GeoJSON
            geojson_fc = route_geojson_feature_collection(route_name, output)
            geojson_path = GEOJSON_DIR / f"{slug(route_name)}.geojson"
            geojson_path.write_text(json.dumps(geojson_fc, indent=2))

            status_icon = "✓" if auto_accept else ("~" if classification == "ambiguous" else "✗")
            print(
                f"{status_icon} {classification:20s} | conf={output.confidence.score:5.1f} "
                f"agree={vs_ref['order_agreement']:.3f} "
                f"stops={len(output.final_stops):2d}/{len(route_input.stops):2d} "
                f"drop={len(output.dropped_stop_ids)} "
                f"dist={distance_m/1000:.1f}km "
                f"[{elapsed:.1f}s] {output.selected_method}"
            )

        except Exception as exc:
            print(f"ERROR: {exc}")
            traceback.print_exc()
            errors.append({"route": route_name, "error": str(exc)})

    # --- Build master artifact ---
    strong = [r for r in constructed_routes if r["classification"] == "strong"]
    acceptable = [r for r in constructed_routes if r["classification"] == "acceptable"]
    ambiguous = [r for r in constructed_routes if r["classification"] == "ambiguous"]
    manual = [r for r in constructed_routes if r["classification"] == "needs_manual_review"]
    special = [r for r in constructed_routes if r["classification"] == "special_case"]

    auto_accepted = [r for r in constructed_routes if r["auto_accept"]]

    avg_agreement = (
        sum(r["vs_reference"]["order_agreement"] for r in constructed_routes) / len(constructed_routes)
        if constructed_routes else 0.0
    )
    avg_confidence = (
        sum(r["confidence"]["score"] for r in constructed_routes) / len(constructed_routes)
        if constructed_routes else 0.0
    )

    master = {
        "artifact_version": "valle_v2_CONSTRUCTED_39",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "constructor_version": "constructor_v2.v2_cpsat",
        "valhalla_url": VALHALLA_URL,
        "solver_mode": "both (best-of OR-Tools + CP-SAT)",
        "preprocessing": "side_of_road (direction + duplicate + outlier, OUTLIER_FACTOR=4.0)",
        "reference_artifact": str(REFERENCE_FILE.name),
        "summary": {
            "total_routes": len(all_route_names),
            "routes_constructed": len(constructed_routes),
            "errors": len(errors),
            "total_stops_constructed": total_stops_constructed,
            "total_stops_dropped": total_stops_dropped,
            "auto_accepted": len(auto_accepted),
            "needs_manual_review": len(manual),
            "avg_reference_agreement": round(avg_agreement, 4),
            "avg_confidence": round(avg_confidence, 1),
            "classification_breakdown": {
                "strong": len(strong),
                "acceptable": len(acceptable),
                "ambiguous": len(ambiguous),
                "needs_manual_review": len(manual),
                "special_case": len(special),
            },
            "solver_usage": _solver_usage(constructed_routes),
        },
        "classification_lists": {
            "strong": [r["route"] for r in strong],
            "acceptable": [r["route"] for r in acceptable],
            "ambiguous": [r["route"] for r in ambiguous],
            "needs_manual_review": [r["route"] for r in manual],
            "special_case": [r["route"] for r in special],
        },
        "routes": constructed_routes,
        "errors": errors,
    }

    MASTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    MASTER_FILE.write_text(json.dumps(master, indent=2, default=str))

    # --- Comparison report ---
    comparison_md = render_comparison(master, constructed_routes)
    comparison_path = OUTPUT_DIR / "comparison_vs_reference.md"
    comparison_path.write_text(comparison_md)

    print(f"\n{'=' * 70}")
    print(f"CONSTRUCTION COMPLETE")
    print(f"{'=' * 70}")
    print(f"Master artifact: {MASTER_FILE}")
    print(f"GeoJSON files:   {GEOJSON_DIR}/ ({len(list(GEOJSON_DIR.glob('*.geojson')))} files)")
    print(f"Comparison:      {comparison_path}")
    print()
    print(f"--- SUMMARY ---")
    print(f"Routes constructed:  {len(constructed_routes)} / {len(all_route_names)}")
    print(f"Auto-accepted:       {len(auto_accepted)} ({len(strong)} strong + {len(acceptable)} acceptable)")
    print(f"Ambiguous:           {len(ambiguous)}")
    print(f"Manual review:       {len(manual)}")
    print(f"Special case:        {len(special)}")
    print(f"Errors:              {len(errors)}")
    print(f"Total stops built:   {total_stops_constructed}")
    print(f"Total stops dropped: {total_stops_dropped}")
    print(f"Avg agreement:       {avg_agreement:.3f}")
    print(f"Avg confidence:      {avg_confidence:.1f}")
    print(f"Solver usage:        {_solver_usage(constructed_routes)}")


def _solver_usage(routes: list[dict]) -> dict:
    usage: dict[str, int] = {}
    for r in routes:
        method = r.get("solver_used", "unknown")
        usage[method] = usage.get(method, 0) + 1
    return usage


def render_comparison(master: dict, routes: list[dict]) -> str:
    s = master["summary"]
    lines = [
        "# Constructor V2 — Constructed Sequences vs COHERENT_v17 Reference",
        "",
        f"Generated: {master['generated_at']}",
        f"Reference: {master['reference_artifact']}",
        "",
        "## Summary",
        "",
        f"- Routes constructed: {s['routes_constructed']} / {s['total_routes']}",
        f"- Auto-accepted: {s['auto_accepted']} ({s['classification_breakdown']['strong']} strong + {s['classification_breakdown']['acceptable']} acceptable)",
        f"- Ambiguous: {s['classification_breakdown']['ambiguous']}",
        f"- Manual review: {s['classification_breakdown']['needs_manual_review']}",
        f"- Special case: {s['classification_breakdown']['special_case']}",
        f"- Avg reference agreement: {s['avg_reference_agreement']:.3f}",
        f"- Avg confidence: {s['avg_confidence']:.1f}",
        f"- Total stops constructed: {s['total_stops_constructed']}",
        f"- Total stops dropped: {s['total_stops_dropped']}",
        "",
        "## Per-Route Comparison",
        "",
        "| # | Route | Type | Class | Conf | Agreement | Stops | Drop | Dist(km) | Solver |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    for idx, r in enumerate(routes, 1):
        lines.append(
            f"| {idx} | {r['route']} | {r['topology']} | {r['classification']} | "
            f"{r['confidence']['score']:.0f} | {r['vs_reference']['order_agreement']:.3f} | "
            f"{r['stops_constructed']} | {r['stops_dropped']} | "
            f"{r['geometry_distance_km']:.1f} | {r['solver_used']} |"
        )

    # Deltas vs reference
    lines.extend([
        "",
        "## Agreement Deltas vs Reference",
        "",
        "Routes with order agreement < 0.90 (potential ordering issues):",
        "",
    ])
    low_agreement = [r for r in routes if r["vs_reference"]["order_agreement"] < 0.90]
    if low_agreement:
        for r in sorted(low_agreement, key=lambda x: x["vs_reference"]["order_agreement"]):
            lines.append(
                f"- **{r['route']}**: agreement={r['vs_reference']['order_agreement']:.3f} "
                f"conf={r['confidence']['score']:.0f} "
                f"common={r['vs_reference']['common_count']}/{r['vs_reference']['reference_count']} "
                f"extra={r['vs_reference']['extra_candidate_count']} "
                f"missing={r['vs_reference']['missing_reference_count']}"
            )
    else:
        lines.append("None — all routes have agreement >= 0.90")

    # Coverage gaps
    lines.extend([
        "",
        "## Coverage Gaps",
        "",
        "Routes missing reference stops:",
        "",
    ])
    missing = [r for r in routes if r["vs_reference"]["missing_reference_count"] > 0]
    if missing:
        for r in sorted(missing, key=lambda x: -x["vs_reference"]["missing_reference_count"]):
            lines.append(
                f"- **{r['route']}**: missing {r['vs_reference']['missing_reference_count']} "
                f"reference stops (have {r['vs_reference']['common_count']}/{r['vs_reference']['reference_count']})"
            )
    else:
        lines.append("None — all reference stops are present")

    # Routes ready for production
    lines.extend([
        "",
        "## Production-Ready Routes (auto-accepted)",
        "",
    ])
    auto = [r for r in routes if r["auto_accept"]]
    for r in auto:
        lines.append(
            f"- {r['route']} — conf={r['confidence']['score']:.0f} "
            f"agree={r['vs_reference']['order_agreement']:.3f} "
            f"stops={r['stops_constructed']} dist={r['geometry_distance_km']:.1f}km"
        )

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    run_construction()
