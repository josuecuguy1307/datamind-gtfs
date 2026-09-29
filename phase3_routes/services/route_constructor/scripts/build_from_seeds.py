#!/usr/bin/env python3
"""Build routes from seed+geography catalog pairs — province-aware, artifact-only.

NO DB WRITES. Outputs a JSON artifact per route plus a summary index.

Wraps ``ConstructorV2Pipeline.run()`` directly, bypassing both
``canton_pipeline.cycle_1`` (whose ``_build_from_research`` is a stub) and
``construct_canton_routes.py`` (whose ``run_all.py from_seed`` subcommand does
not exist). This is the minimal seed → ConstructorV2Pipeline adapter needed
to unblock any terminal that has to build non-OSM research routes.

It does not touch ``route_prod.routes`` (avoiding the DEFAULT='sample_region'
backfill bug), does not mutate ``canton_pipeline.py`` or ``run_all.py``, and
does not touch multi-province catalogs. It is strictly additive.

Usage
-----

    python phase3_routes/services/route_constructor/scripts/build_from_seeds.py \\
        --province sample_region_b \\
        --canton duran \\
        --catalog-dir workspace/provinces/sample_region_b/cantons/duran/processing/catalogs \\
        --output-dir constructor_artifacts/duran_from_seeds

Seed catalog shape expected (produced by ingest_research_routes.py):
    {
      "route_code": str,
      "display_name": str,
      "cooperative": str,
      "terminus_origin":     {name, lat, lon, type, confidence, source},
      "terminus_destination":{name, lat, lon, type, confidence, source},
      "intermediate_stops":  [ {name, lat, lon, type, confidence, source}, ... ],
      "primary_corridor":    str,
      "corridor_roads":      [str, ...]
    }

Geography catalog shape (optional, matched by filename):
    {
      "route_code":         str,
      "zone_classification": str,
      "envelope_bbox":       [west, south, east, north],
      "required_corridors":  [str, ...],
      "must_pass_through":   [...],
      "must_not_enter":      [...]
    }
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Resolve repo + phase3 roots regardless of CWD.
_HERE = Path(__file__).resolve()
# scripts/ -> route_constructor/ -> services/ -> phase3_routes/ -> REPO_ROOT
REPO_ROOT = _HERE.parents[4]
PHASE3_ROOT = _HERE.parents[1]
if str(PHASE3_ROOT) not in sys.path:
    sys.path.insert(0, str(PHASE3_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.constructor_v2.clients.valhalla_client import ValhallaClient  # noqa: E402
from src.constructor_v2.constants import DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL  # noqa: E402
from src.constructor_v2.pipeline import ConstructorV2Pipeline  # noqa: E402
from src.constructor_v2.schemas.route_input import RouteInput  # noqa: E402


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s).strip("_") or "route"


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math
    R = 6_371_000
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    dl = la2 - la1
    do = lo2 - lo1
    x = math.sin(dl / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(do / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def _infer_route_type(seed: dict, origin: dict, destination: dict) -> str:
    """Detect circular topology: identical/near-identical termini OR explicit 'circular' mention.

    Returns one of constructor_v2.topology's accepted values: 'lineal', 'possible_circular',
    'internal_loop'. Default is 'lineal'.
    """
    try:
        endpoint_dist_m = _haversine_m(
            (float(origin["lat"]), float(origin["lon"])),
            (float(destination["lat"]), float(destination["lon"])),
        )
    except Exception:
        endpoint_dist_m = float("inf")

    display = (seed.get("display_name") or "").lower()
    code = (seed.get("route_code") or "").lower()
    origin_name = (origin.get("name") or "").lower()
    dest_name = (destination.get("name") or "").lower()

    has_circular_token = any(
        "circular" in txt or "circuito" in txt or "loop" in txt
        for txt in (display, code, origin_name, dest_name)
    )
    # Within 50 m → endpoints coincide in practice given seed GPS precision.
    endpoints_coincide = endpoint_dist_m < 50.0

    if endpoints_coincide or has_circular_token:
        return "possible_circular"
    return "lineal"


def load_catalog_pairs(catalog_dir: Path) -> list[tuple[dict, dict | None, Path]]:
    """Load matching (seed, geography) pairs from ``catalog_dir/{seed,geography}/``."""
    seed_dir = catalog_dir / "seed"
    geo_dir = catalog_dir / "geography"
    if not seed_dir.is_dir():
        raise FileNotFoundError(f"seed dir not found: {seed_dir}")
    pairs: list[tuple[dict, dict | None, Path]] = []
    for seed_path in sorted(seed_dir.glob("*.json")):
        try:
            with seed_path.open() as f:
                seed = json.load(f)
        except Exception as exc:
            print(f"  WARNING: could not read {seed_path.name}: {exc}")
            continue
        geo = None
        geo_path = geo_dir / seed_path.name
        if geo_path.exists():
            try:
                with geo_path.open() as f:
                    geo = json.load(f)
            except Exception as exc:
                print(f"  WARNING: could not read geography for {seed_path.name}: {exc}")
        pairs.append((seed, geo, seed_path))
    return pairs


def seed_to_route_input(seed: dict, geo: dict | None) -> RouteInput:
    """Adapt a Deep Research seed catalog into a ``RouteInput``.

    Terminus_origin becomes seq=1 (anchor), terminus_destination becomes
    seq=N (anchor). Intermediates keep their declared order. Any stop whose
    ``type == "anchor"`` or whose name appears in
    ``geo.required_corridors`` is flagged ``is_known_anchor`` so the solver
    treats it as a fixed checkpoint.
    """
    origin = seed.get("terminus_origin")
    destination = seed.get("terminus_destination")
    if not origin or not destination:
        raise ValueError("seed missing terminus_origin or terminus_destination")
    if origin.get("lat") is None or origin.get("lon") is None:
        raise ValueError("terminus_origin has null coordinates")
    if destination.get("lat") is None or destination.get("lon") is None:
        raise ValueError("terminus_destination has null coordinates")

    intermediates = list(seed.get("intermediate_stops", []) or [])
    stops_raw = [origin] + intermediates + [destination]

    required_corridor_names = set()
    if geo:
        for name in geo.get("required_corridors", []) or []:
            if isinstance(name, str):
                required_corridor_names.add(name.strip())

    code = seed.get("route_code") or seed.get("display_name") or "UNKNOWN"
    total = len(stops_raw)
    ordered_stops: list[dict[str, Any]] = []

    for idx, raw in enumerate(stops_raw):
        if raw.get("lat") is None or raw.get("lon") is None:
            raise ValueError(f"intermediate stop {idx} ('{raw.get('name')}') has null coordinates")

        seq = idx + 1
        is_terminus = idx == 0 or idx == total - 1
        declared_type = (raw.get("type") or "").lower()
        raw_name = raw.get("name") or f"stop_{seq}"
        is_anchor = (
            is_terminus
            or declared_type == "anchor"
            or raw_name in required_corridor_names
        )

        ordered_stops.append({
            "seq": seq,
            "stop_id": f"{_slug(code)}_S{seq:03d}",
            "stop_name": raw_name,
            "lat": float(raw["lat"]),
            "lon": float(raw["lon"]),
            "path_fraction": idx / max(total - 1, 1),
            "is_known_anchor": bool(is_anchor),
            "stop_source": raw.get("source") or "deep_research",
            "metadata": {
                "declared_type": raw.get("type"),
                "terminus_confidence": raw.get("confidence"),
            },
        })

    route_type = _infer_route_type(seed, origin, destination)

    return RouteInput.from_artifact_route({
        "route": code,
        "cooperative": seed.get("cooperative") or "",
        "route_type": route_type,
        "corridor_km": None,  # pipeline will derive from matrix
        "ordered_stops": ordered_stops,
    })


def _confidence_bucket(label: str, score: float) -> str:
    """Map raw confidence into an operational bucket the state machine cares about."""
    lbl = (label or "").lower()
    if lbl in ("strong", "acceptable"):
        return "usable"
    if lbl in ("weak", "marginal", "ambiguous", "needs_manual_review", "needs_review"):
        return "needs_review"
    if lbl in ("rejected", "blocked", "failed"):
        return "blocked"
    return "unknown"


def build_one(seed: dict, geo: dict | None, pipeline: ConstructorV2Pipeline) -> dict:
    """Run ConstructorV2Pipeline for a single seed. Catches adapter and pipeline errors."""
    code = seed.get("route_code", "UNKNOWN")
    t0 = time.monotonic()

    try:
        route_input = seed_to_route_input(seed, geo)
    except Exception as exc:
        return {
            "route_code": code,
            "status": "adapter_failed",
            "bucket": "blocked",
            "error": str(exc),
            "elapsed_s": time.monotonic() - t0,
        }

    try:
        output = pipeline.run(route_input)
    except Exception as exc:
        return {
            "route_code": code,
            "status": "pipeline_failed",
            "bucket": "blocked",
            "error": str(exc),
            "traceback": traceback.format_exc(limit=6),
            "elapsed_s": time.monotonic() - t0,
        }

    metrics = getattr(output, "metrics", {}) or {}
    confidence = getattr(output, "confidence", None)
    conf_label = getattr(confidence, "label", "") if confidence else ""
    conf_score = float(getattr(confidence, "score", 0.0) or 0.0) if confidence else 0.0
    conf_reasons = list(getattr(confidence, "reasons", []) or []) if confidence else []

    # Seed-side geometric sanity so reviewers see data-quality issues without rerunning.
    stop_pts = [(float(s.lat), float(s.lon)) for s in route_input.stops]
    seq_sum_m = sum(_haversine_m(stop_pts[i], stop_pts[i + 1]) for i in range(len(stop_pts) - 1))
    end_to_end_m = _haversine_m(stop_pts[0], stop_pts[-1]) if len(stop_pts) >= 2 else 0.0
    pipeline_distance_m = metrics.get("geometry_distance_m") or 0.0
    detour_ratio = (
        pipeline_distance_m / seq_sum_m if seq_sum_m > 50 else None
    )

    return {
        "route_code": code,
        "status": "built",
        "bucket": _confidence_bucket(conf_label, conf_score),
        "confidence_label": conf_label,
        "confidence_score": conf_score,
        "confidence_reasons": conf_reasons[:8],
        "route_type": route_input.route_type,
        "input_stops": len(route_input.stops),
        "final_stops": len(getattr(output, "final_stops", []) or []),
        "dropped_stops": len(getattr(output, "dropped_stop_ids", []) or []),
        "distance_m": pipeline_distance_m,
        "duration_s": metrics.get("geometry_duration_s"),
        "seed_seq_sum_m": round(seq_sum_m, 1),
        "seed_end_to_end_m": round(end_to_end_m, 1),
        "detour_ratio": round(detour_ratio, 2) if detour_ratio is not None else None,
        "elapsed_s": time.monotonic() - t0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build routes from seed+geography catalogs. Artifact-only, no DB writes.",
    )
    parser.add_argument("--province", required=True, help="e.g. sample_region_b")
    parser.add_argument("--canton", required=True, help="e.g. duran")
    parser.add_argument(
        "--catalog-dir", required=True,
        help="Directory containing seed/ and geography/ subdirs.",
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Where to write per-route result JSONs plus _index.json.",
    )
    parser.add_argument("--valhalla-url", default=DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL)
    parser.add_argument("--max", type=int, default=None, help="Limit number of routes (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="List catalogs without running the pipeline")
    args = parser.parse_args()

    catalog_dir = Path(args.catalog_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    pairs = load_catalog_pairs(catalog_dir)
    if args.max:
        pairs = pairs[: args.max]

    print("build_from_seeds")
    print(f"  province:    {args.province}")
    print(f"  canton:      {args.canton}")
    print(f"  catalog_dir: {catalog_dir}")
    print(f"  output_dir:  {output_dir}")
    print(f"  valhalla:    {args.valhalla_url}")
    print(f"  pairs found: {len(pairs)}")
    print()

    if args.dry_run:
        for seed, _geo, seed_path in pairs:
            code = seed.get("route_code", seed_path.stem)
            n_stops = 2 + len(seed.get("intermediate_stops", []) or [])
            print(f"  DRY  {code}  ({n_stops} stops)")
        return 0

    client = ValhallaClient(base_url=args.valhalla_url)
    try:
        client.refresh_capabilities(force=True)
    except Exception as exc:
        print(f"  WARNING: valhalla refresh_capabilities failed: {exc}")

    pipeline = ConstructorV2Pipeline(client=client)

    results: list[dict[str, Any]] = []
    for seed, geo, seed_path in pairs:
        code = seed.get("route_code", seed_path.stem)
        print(f"  BUILD {code} ...", end=" ", flush=True)
        res = build_one(seed, geo, pipeline)
        label = res.get("confidence_label") or "-"
        score = res.get("confidence_score") or 0.0
        final_n = res.get("final_stops", "-")
        print(
            f"{res['status']:16s} bucket={res['bucket']:12s} "
            f"conf={label}({score:.1f}) stops={final_n} "
            f"[{res['elapsed_s']:.1f}s]"
        )
        results.append(res)

        out_path = output_dir / f"{_slug(code)}.json"
        with out_path.open("w") as f:
            json.dump({
                "province": args.province,
                "canton": args.canton,
                "seed_path": str(seed_path),
                "result": res,
            }, f, indent=2, ensure_ascii=False)

    index = {
        "province": args.province,
        "canton": args.canton,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "catalog_dir": str(catalog_dir),
        "valhalla_url": args.valhalla_url,
        "pairs": len(pairs),
        "by_status": {},
        "by_bucket": {},
        "results": results,
    }
    for r in results:
        index["by_status"][r["status"]] = index["by_status"].get(r["status"], 0) + 1
        index["by_bucket"][r["bucket"]] = index["by_bucket"].get(r["bucket"], 0) + 1

    index_path = output_dir / "_index.json"
    with index_path.open("w") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    print()
    print("Summary:")
    for k, v in sorted(index["by_status"].items()):
        print(f"  status  {k:20s} {v}")
    for k, v in sorted(index["by_bucket"].items()):
        print(f"  bucket  {k:20s} {v}")
    print(f"  index: {index_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
