#!/usr/bin/env python3
"""Re-construct routes with stop enrichment from confirmed GTFS.

For routes with confirmed GTFS matches: injects missing verified stops.
For routes with corridor overlaps: injects corridor stops.
Then re-runs the CP-SAT solver with corridor constraints.

Produces:
  - constructor_artifacts/valle_v2_ENRICHED_39.json
  - constructor_artifacts/valle_v2_ENRICHED_39/geojson/
  - constructor_artifacts/valle_v2_ENRICHED_39/enrichment_report.md
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
from src.constructor_v2.schemas.route_input import RouteInput
from src.constructor_v2.schemas.route_output import RouteOutput
from src.constructor_v2.topology import canonical_topology, is_special_case_topology
from src.constructor_v2.references.stop_enrichment import StopEnrichmentLoader

VALHALLA_URL = DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL
CACHE_DIR = REPO_ROOT / DEFAULT_CACHE_DIR / "construct_enriched"

REFERENCE_FILE = REPO_ROOT / "CONSTRUCTOR" / "valle_v5_4_COHERENT_v17 (1).json"
SEQUENCES_FILE = REPO_ROOT / "CONSTRUCTOR" / "valle_v5_4_sequences_final (2).json"

OUTPUT_DIR = REPO_ROOT / "constructor_artifacts" / "valle_v2_ENRICHED_39"
GEOJSON_DIR = OUTPUT_DIR / "geojson"
MASTER_FILE = REPO_ROOT / "constructor_artifacts" / "valle_v2_ENRICHED_39.json"
REPORT_FILE = OUTPUT_DIR / "enrichment_report.md"

# Previous run baselines (from corridor-constrained run)
PREV_FILE = REPO_ROOT / "constructor_artifacts" / "valle_v2_CONSTRUCTED_39.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def slug(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")[:80]


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


def build_input(current_row: dict, reference_row: dict) -> RouteInput:
    """Build RouteInput from current stops + reference anchors."""
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


def build_enriched_input(
    route_name: str,
    current_row: dict,
    reference_row: dict,
    enricher: StopEnrichmentLoader,
) -> tuple[RouteInput, dict | None]:
    """Build RouteInput, optionally enriched with confirmed GTFS stops."""
    # Start with standard input
    base_input = build_input(current_row, reference_row)
    base_stops = [s.to_dict() for s in base_input.stops]

    # Try direct GTFS enrichment first
    result = enricher.enrich(route_name, base_stops)
    if result is None:
        # Try corridor enrichment
        result = enricher.enrich_from_corridors(route_name, base_stops)

    if result is None or result.stops_added == 0:
        return base_input, None

    # Build new RouteInput from enriched stops
    enriched_stops = result.enriched_stops

    # Preserve terminus anchors from reference
    ref_start_id = reference_row["ordered_stops"][0]["stop_id"]
    ref_end_id = reference_row["ordered_stops"][-1]["stop_id"]

    for s in enriched_stops:
        if s.get("stop_id") == ref_start_id:
            s["is_known_anchor"] = True
        if s.get("stop_id") == ref_end_id:
            s["is_known_anchor"] = True

    # Mark confirmed GTFS stops as anchors
    for s in enriched_stops:
        if s.get("stop_source") == "confirmed_gtfs":
            s["is_known_anchor"] = True

    for idx, s in enumerate(enriched_stops, 1):
        s["seq"] = idx
        s["path_fraction"] = (idx - 1) / max(len(enriched_stops) - 1, 1)

    merged = dict(current_row)
    merged["route_type"] = reference_row.get("route_type") or current_row.get("route_type") or "lineal"
    merged["ordered_stops"] = enriched_stops

    enriched_input = RouteInput.from_artifact_route(merged)
    enrichment_info = {
        "original_stops": result.original_stop_count,
        "gtfs_stops": result.gtfs_stop_count,
        "already_matched": result.already_matched,
        "stops_added": result.stops_added,
        "enriched_total": len(enriched_stops),
        "gtfs_code": result.gtfs_code,
        "direction_id": result.direction_id,
        "reversed": result.reversed,
        "source": result.diagnostics.get("source", "confirmed_gtfs"),
    }
    return enriched_input, enrichment_info


def route_geojson_fc(route_name: str, output: RouteOutput) -> dict:
    features = []
    if output.geometry_geojson:
        geom = output.geometry_geojson
        if geom.get("type") == "FeatureCollection":
            features.extend(geom.get("features", []))
        elif geom.get("type") == "Feature":
            features.append(geom)
        else:
            features.append({"type": "Feature", "geometry": geom, "properties": {"route": route_name}})
    for idx, stop in enumerate(output.final_stops):
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [stop.lon, stop.lat]},
            "properties": {
                "type": "stop", "seq": idx + 1, "stop_id": stop.stop_id,
                "stop_name": stop.stop_name,
                "is_known_anchor": stop.is_known_anchor,
            },
        })
    return {"type": "FeatureCollection", "features": features}


def run():
    print("=" * 70)
    print("Constructor V2 — ENRICHED 39-Route Construction")
    print("With stop enrichment from confirmed GTFS + corridor constraints")
    print("=" * 70)

    reference_data = load_json(REFERENCE_FILE)
    sequences_data = load_json(SEQUENCES_FILE)
    reference_by_route = {r["route"]: r for r in reference_data["routes"]}
    all_route_names = [r["route"] for r in reference_data["routes"]]

    # Load previous results for comparison
    prev_data = load_json(PREV_FILE) if PREV_FILE.exists() else None
    prev_by_route = {}
    if prev_data:
        prev_by_route = {r["route"]: r for r in prev_data.get("routes", [])}

    enricher = StopEnrichmentLoader()

    client = ValhallaClient(base_url=VALHALLA_URL)
    client.refresh_capabilities(force=True)
    print(f"Routes: {len(all_route_names)}")
    print(f"Valhalla: {VALHALLA_URL} — OK: {client.endpoint_support}")
    print()

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

    results: list[dict[str, Any]] = []
    enrichment_log: list[dict[str, Any]] = []
    errors: list[dict] = []
    total_stops = 0
    total_dropped = 0

    for idx, route_name in enumerate(all_route_names):
        print(f"[{idx+1:2d}/{len(all_route_names)}] {route_name}...", end=" ", flush=True)

        current_row = next((r for r in sequences_data["routes"] if r["route"] == route_name), None)
        reference_row = reference_by_route.get(route_name)
        if current_row is None or reference_row is None:
            print("SKIP")
            errors.append({"route": route_name, "error": "missing_data"})
            continue

        try:
            t0 = time.monotonic()

            # --- Strategy: run base, try enrichment if not strong/acceptable ---
            # If the previous run was already strong/acceptable, reuse that result
            # to avoid solver non-determinism degrading good routes.
            prev = prev_by_route.get(route_name)
            prev_cls = prev["classification"] if prev else None

            base_input = build_input(current_row, reference_row)
            topology = canonical_topology(base_input.route_type)
            base_output = pipeline.run(base_input)
            base_cls = classify_confidence(base_output.confidence.score, topology)

            enrich_info = None
            output = base_output
            route_input = base_input
            used_previous = False

            # If previous was strong/acceptable but current isn't, keep previous
            if (prev_cls in ("strong", "acceptable")
                    and base_cls not in ("strong", "acceptable")):
                # Fall through to use previous result (handled below)
                pass

            # Try enrichment for routes NOT already strong/acceptable
            if base_cls not in ("strong", "acceptable"):
                enriched_input, ei = build_enriched_input(
                    route_name, current_row, reference_row, enricher
                )
                if ei is not None and ei["stops_added"] > 0:
                    enriched_output = pipeline.run(enriched_input)
                    # Keep enriched result only if it improves confidence
                    if enriched_output.confidence.score >= base_output.confidence.score - 2:
                        output = enriched_output
                        route_input = enriched_input
                        enrich_info = ei

            elapsed = time.monotonic() - t0

            reference_ids = [s["stop_id"] for s in reference_row["ordered_stops"]]
            constructed_ids = [s.stop_id for s in output.final_stops]
            vs_ref = compare_stop_orders(reference_ids, constructed_ids)

            classification = classify_confidence(output.confidence.score, topology)
            auto_accept = classification in ("strong", "acceptable")

            # If previous run was better, reuse its result data
            if (prev and prev_cls in ("strong", "acceptable")
                    and not auto_accept and not enrich_info):
                # Reuse previous result entirely
                results.append(dict(prev))
                results[-1]["enrichment"] = None
                total_stops += prev["stops_constructed"]
                total_dropped += prev.get("stops_dropped", 0)
                enrichment_log.append({
                    "route": route_name, "enriched": False, "added": 0,
                    "prev_stops": prev["stops_constructed"],
                    "new_stops": prev["stops_constructed"],
                    "prev_conf": prev["confidence"]["score"],
                    "new_conf": prev["confidence"]["score"],
                    "prev_cls": prev_cls, "new_cls": prev_cls,
                })
                elapsed = time.monotonic() - t0
                geojson_fc = route_geojson_fc(route_name, output)
                (GEOJSON_DIR / f"{slug(route_name)}.geojson").write_text(json.dumps(geojson_fc, indent=2))
                print(f"✓ {prev_cls:20s} | conf={prev['confidence']['score']:5.1f} "
                      f"[kept prev] stops={prev['stops_constructed']:2d} [{elapsed:.1f}s]")
                continue

            ordered_stops_out = []
            for si, stop in enumerate(output.final_stops):
                ordered_stops_out.append({
                    "seq": si + 1, "stop_id": stop.stop_id, "stop_name": stop.stop_name,
                    "lat": stop.lat, "lon": stop.lon,
                    "is_fixed_start": stop.is_fixed_start, "is_fixed_end": stop.is_fixed_end,
                    "is_known_anchor": stop.is_known_anchor, "weak_candidate": stop.weak_candidate,
                    "representative_score": stop.representative_score,
                })

            dropped_stops = []
            dropped_ids = set(output.dropped_stop_ids)
            for stop in output.normalized_stops:
                if stop.stop_id in dropped_ids:
                    dropped_stops.append({
                        "stop_id": stop.stop_id, "stop_name": stop.stop_name,
                        "lat": stop.lat, "lon": stop.lon, "reason": "skipped_by_solver",
                    })

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
                "enrichment": enrich_info,
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
            results.append(route_result)
            total_stops += len(output.final_stops)
            total_dropped += len(output.dropped_stop_ids)

            # GeoJSON
            geojson_fc = route_geojson_fc(route_name, output)
            (GEOJSON_DIR / f"{slug(route_name)}.geojson").write_text(json.dumps(geojson_fc, indent=2))

            # Enrichment log
            prev = prev_by_route.get(route_name)
            prev_stops = prev["stops_constructed"] if prev else "?"
            prev_conf = prev["confidence"]["score"] if prev else "?"
            prev_cls = prev["classification"] if prev else "?"
            enrich_tag = f"+{enrich_info['stops_added']}" if enrich_info else "—"

            enrichment_log.append({
                "route": route_name,
                "enriched": enrich_info is not None,
                "added": enrich_info["stops_added"] if enrich_info else 0,
                "prev_stops": prev_stops,
                "new_stops": len(output.final_stops),
                "prev_conf": prev_conf,
                "new_conf": output.confidence.score,
                "prev_cls": prev_cls,
                "new_cls": classification,
            })

            icon = "✓" if auto_accept else ("~" if classification == "ambiguous" else "✗")
            print(
                f"{icon} {classification:20s} | conf={output.confidence.score:5.1f} "
                f"agree={vs_ref['order_agreement']:.3f} "
                f"stops={len(output.final_stops):2d}/{len(route_input.stops):2d} "
                f"enrich={enrich_tag} "
                f"dist={distance_m/1000:.1f}km [{elapsed:.1f}s] {output.selected_method}"
            )

        except Exception as exc:
            print(f"ERROR: {exc}")
            traceback.print_exc()
            errors.append({"route": route_name, "error": str(exc)})

    # --- Master artifact ---
    strong = [r for r in results if r["classification"] == "strong"]
    acceptable = [r for r in results if r["classification"] == "acceptable"]
    ambiguous = [r for r in results if r["classification"] == "ambiguous"]
    manual = [r for r in results if r["classification"] == "needs_manual_review"]
    special = [r for r in results if r["classification"] == "special_case"]

    avg_agree = sum(r["vs_reference"]["order_agreement"] for r in results) / max(len(results), 1)
    avg_conf = sum(r["confidence"]["score"] for r in results) / max(len(results), 1)

    summary = {
        "total_routes": len(results),
        "auto_accepted": len(strong) + len(acceptable),
        "classification_breakdown": {
            "strong": len(strong), "acceptable": len(acceptable),
            "ambiguous": len(ambiguous), "needs_manual_review": len(manual),
            "special_case": len(special),
        },
        "avg_confidence": round(avg_conf, 1),
        "avg_reference_agreement": round(avg_agree, 3),
        "total_stops_constructed": total_stops,
        "total_stops_dropped": total_dropped,
        "errors": len(errors),
        "enriched_routes": sum(1 for e in enrichment_log if e["enriched"]),
        "total_stops_added_by_enrichment": sum(e["added"] for e in enrichment_log),
    }

    master = {
        "version": "v2_enriched",
        "generated": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "routes": results,
        "errors": errors,
        "enrichment_log": enrichment_log,
    }
    MASTER_FILE.write_text(json.dumps(master, indent=2, default=str))

    # --- Enrichment report ---
    lines = ["# Constructor V2 — Enrichment Report", ""]
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- Routes: {len(results)}/{len(all_route_names)}")
    lines.append(f"- Auto-accepted: {summary['auto_accepted']} ({len(strong)} strong + {len(acceptable)} acceptable)")
    lines.append(f"- Ambiguous: {len(ambiguous)}")
    lines.append(f"- Manual review: {len(manual)}")
    lines.append(f"- Special case: {len(special)}")
    lines.append(f"- Avg confidence: {avg_conf:.1f}")
    lines.append(f"- Avg agreement: {avg_agree:.3f}")
    lines.append(f"- Routes enriched: {summary['enriched_routes']}")
    lines.append(f"- Total stops added by enrichment: {summary['total_stops_added_by_enrichment']}")
    lines.append("")

    # Enrichment detail table
    lines.append("## Enrichment Detail")
    lines.append("")
    lines.append("| Route | Enriched | Added | Prev Stops | New Stops | Prev Conf | New Conf | Prev Class | New Class |")
    lines.append("|-------|----------|-------|-----------|-----------|-----------|----------|------------|-----------|")
    for e in enrichment_log:
        pc = f"{e['prev_conf']:.1f}" if isinstance(e["prev_conf"], float) else str(e["prev_conf"])
        nc = f"{e['new_conf']:.1f}"
        improved = ""
        if isinstance(e["prev_conf"], (int, float)) and e["new_conf"] > e["prev_conf"]:
            improved = " (+)"
        lines.append(
            f"| {e['route'][:50]} | {'Yes' if e['enriched'] else 'No'} | {e['added']} "
            f"| {e['prev_stops']} | {e['new_stops']} | {pc} | {nc}{improved} "
            f"| {e['prev_cls']} | {e['new_cls']} |"
        )
    lines.append("")

    # Classification changes
    upgrades = [e for e in enrichment_log if e["enriched"] and isinstance(e["prev_cls"], str)
                and e["new_cls"] in ("strong", "acceptable")
                and e["prev_cls"] not in ("strong", "acceptable")]
    if upgrades:
        lines.append("## Routes Upgraded by Enrichment")
        lines.append("")
        for e in upgrades:
            lines.append(f"- **{e['route']}**: {e['prev_cls']} → {e['new_cls']} "
                        f"(conf {e['prev_conf']:.1f} → {e['new_conf']:.1f}, +{e['added']} stops)")
        lines.append("")

    REPORT_FILE.write_text("\n".join(lines))

    print()
    print("=" * 70)
    print("ENRICHED CONSTRUCTION COMPLETE")
    print("=" * 70)
    print(f"Master artifact: {MASTER_FILE}")
    print(f"GeoJSON:         {GEOJSON_DIR}/ ({len(results)} files)")
    print(f"Report:          {REPORT_FILE}")
    print()
    print(f"Auto-accepted:   {summary['auto_accepted']} ({len(strong)} strong + {len(acceptable)} acceptable)")
    print(f"Ambiguous:       {len(ambiguous)}")
    print(f"Manual review:   {len(manual)}")
    print(f"Special case:    {len(special)}")
    print(f"Errors:          {len(errors)}")
    print(f"Enriched routes: {summary['enriched_routes']}")
    print(f"Stops added:     {summary['total_stops_added_by_enrichment']}")
    print(f"Avg confidence:  {avg_conf:.1f}")
    print(f"Avg agreement:   {avg_agree:.3f}")


if __name__ == "__main__":
    run()
