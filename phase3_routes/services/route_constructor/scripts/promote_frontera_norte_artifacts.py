#!/usr/bin/env python3
"""Promote usable frontera_norte seed-built routes to route_prod + catalog.route_semantics.

Mirrors promote_duran_artifacts.py but reads jurisdiction directly from each
seed's JSON (06a inventory records it per route) instead of a hard-coded map.
Covers both sub-cantons (pedro_carbo + el_empalme) of the frontera_norte unit.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[4]
PHASE3_ROOT = _HERE.parents[1]
if str(PHASE3_ROOT) not in sys.path:
    sys.path.insert(0, str(PHASE3_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg2  # noqa: E402
from src.constructor_v2.clients.valhalla_client import ValhallaClient  # noqa: E402
from src.constructor_v2.constants import DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL  # noqa: E402
from src.constructor_v2.pipeline import ConstructorV2Pipeline  # noqa: E402

from scripts.build_from_seeds import (  # noqa: E402
    load_catalog_pairs,
    seed_to_route_input,
    _confidence_bucket,
)

from datamind_console.persistence import write_to_route_prod  # noqa: E402

_SOURCE_TYPE = "promote_frontera_norte_artifacts"
_PIPELINE_VERSION = "promote_frontera_norte_artifacts.promote_one"
_PRESERVE_ON_CONFLICT = frozenset({"stop_node_ids", "route_aliases"})

DEFAULT_JURISDICTION = "ANT_CANTON"


def _geojson_to_wkt(geojson: dict) -> str:
    geom_type = geojson.get("type", "")
    coords = geojson.get("coordinates", [])
    if geom_type == "LineString":
        if len(coords) < 2:
            raise ValueError(f"LineString has {len(coords)} points")
        parts = ", ".join(f"{lon} {lat}" for lon, lat, *_ in coords)
        return f"LINESTRING({parts})"
    if geom_type == "MultiLineString":
        all_coords = []
        for segment in coords:
            all_coords.extend(segment)
        if len(all_coords) < 2:
            raise ValueError("MultiLineString has <2 total points")
        parts = ", ".join(f"{lon} {lat}" for lon, lat, *_ in all_coords)
        return f"LINESTRING({parts})"
    raise ValueError(f"Unsupported geometry type: {geom_type}")


def promote_one(conn, seed, geo, pipeline, province, source_tag, dry_run):
    code = seed.get("route_code", "UNKNOWN")
    coop = seed.get("cooperative", "")
    origin = seed.get("terminus_origin", {})
    dest = seed.get("terminus_destination", {})
    jurisdiction = seed.get("jurisdiction") or DEFAULT_JURISDICTION

    try:
        route_input = seed_to_route_input(seed, geo)
    except Exception as exc:
        return {"route_code": code, "status": "adapter_error", "error": str(exc)}

    try:
        output = pipeline.run(route_input)
    except Exception as exc:
        return {"route_code": code, "status": "pipeline_error", "error": str(exc)}

    confidence = getattr(output, "confidence", None)
    conf_label = getattr(confidence, "label", "") if confidence else ""
    conf_score = float(getattr(confidence, "score", 0.0) or 0.0) if confidence else 0.0
    bucket = _confidence_bucket(conf_label, conf_score)

    if bucket != "usable":
        return {
            "route_code": code,
            "status": "skipped_not_usable",
            "bucket": bucket,
            "confidence_label": conf_label,
            "confidence_score": conf_score,
        }

    geojson = getattr(output, "geometry_geojson", None) or {}
    try:
        wkt = _geojson_to_wkt(geojson)
    except Exception as exc:
        return {"route_code": code, "status": "geometry_error", "error": str(exc)}

    origin_name = origin.get("name", "")
    dest_name = dest.get("name", "")
    display_name = seed.get("display_name", f"{origin_name} - {dest_name}")
    route_short_name = code
    route_long_name = display_name or f"{origin_name} - {dest_name}"

    route_id = uuid.uuid4()
    metrics = getattr(output, "metrics", {}) or {}
    distance_m = metrics.get("geometry_distance_m", 0.0)

    result = {
        "route_code": code,
        "route_id": str(route_id),
        "status": "dry_run_would_promote" if dry_run else "promoted",
        "bucket": bucket,
        "confidence_label": conf_label,
        "confidence_score": conf_score,
        "distance_m": distance_m,
        "jurisdiction": jurisdiction,
        "cooperative": coop,
        "province": province,
    }

    if dry_run:
        print(f"    DRY: INSERT route_id={route_id} jurisdiction={jurisdiction}")
        return result

    cur = conn.cursor()
    sp = f"sp_promote_{code.replace(' ', '_').replace('-', '_')}"
    try:
        cur.execute(f"SAVEPOINT {sp}")
        cur.execute(
            """
            INSERT INTO route_raw.route_jobs (
                route_id, status, notes, known_ref,
                extractor_source, province
            )
            VALUES (%s, 'promoted', %s, %s, %s, %s)
            ON CONFLICT (route_id) DO NOTHING
            """,
            (
                str(route_id),
                f"Seed-built via promote_frontera_norte_artifacts.py. Cooperative: {coop}.",
                code,
                source_tag,
                province,
            ),
        )
        _wr_result = write_to_route_prod(
            route_code=str(route_id),
            route_data={
                "route_id": route_id,
                "province": province,
                "source": source_tag,
                "route_name": route_long_name,
                "route_aliases": [],
            },
            stops=[],
            shape={"wkt": wkt},
            source_type=_SOURCE_TYPE,
            pipeline_version=_PIPELINE_VERSION,
            conn=conn,
            legacy_grandfathered=True,
            on_conflict_preserve=_PRESERVE_ON_CONFLICT,
        )
        if not _wr_result.success:
            raise RuntimeError(f"route_prod write failed: {_wr_result.error}")
        cur.execute(
            """
            INSERT INTO catalog.route_semantics (
                route_id, operator, route_short_name, route_long_name,
                route_type, public_origin, public_destination,
                jurisdiction, evidence_source, confidence, notes
            )
            VALUES (%s, %s, %s, %s, 3, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (route_id) DO UPDATE SET
                operator = EXCLUDED.operator,
                route_short_name = EXCLUDED.route_short_name,
                route_long_name = EXCLUDED.route_long_name,
                public_origin = EXCLUDED.public_origin,
                public_destination = EXCLUDED.public_destination,
                jurisdiction = EXCLUDED.jurisdiction,
                evidence_source = EXCLUDED.evidence_source,
                confidence = EXCLUDED.confidence,
                notes = EXCLUDED.notes,
                updated_at = now()
            """,
            (
                str(route_id),
                coop,
                route_short_name,
                route_long_name,
                origin_name,
                dest_name,
                jurisdiction,
                f"deep_research_seed + constructor_v2 (build_from_seeds.py, conf={conf_label}/{conf_score:.1f})",
                round(conf_score / 100.0, 2),
                f"Promoted {datetime.now(timezone.utc).isoformat()}. Bucket={bucket}. Source=seed catalog via promote_frontera_norte_artifacts.py.",
            ),
        )
        cur.execute(f"RELEASE SAVEPOINT {sp}")
        result["status"] = "promoted"
    except Exception as exc:
        cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
        result["status"] = "db_error"
        result["error"] = str(exc)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--province", required=True)
    parser.add_argument("--canton", required=True)
    parser.add_argument("--catalog-dir", required=True)
    parser.add_argument("--artifacts-dir", required=True, help="Dir with _index.json from build_from_seeds")
    parser.add_argument("--valhalla-url", default=DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL)
    parser.add_argument("--db-dsn", required=True)
    parser.add_argument("--source-tag", default="deep_research_frontera_norte_seeds_promote")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()

    catalog_dir = Path(args.catalog_dir).resolve()
    artifacts_dir = Path(args.artifacts_dir).resolve()
    pairs = load_catalog_pairs(catalog_dir)

    print("promote_frontera_norte_artifacts")
    print(f"  province:     {args.province}")
    print(f"  canton:       {args.canton}")
    print(f"  catalog_dir:  {catalog_dir}")
    print(f"  artifacts:    {artifacts_dir}")
    print(f"  valhalla:     {args.valhalla_url}")
    print(f"  dry_run:      {args.dry_run}")
    print(f"  pairs found:  {len(pairs)}")

    index_path = artifacts_dir / "_index.json"
    usable_codes: set[str] = set()
    if index_path.exists():
        idx = json.load(open(index_path))
        for r in idx.get("results", []):
            if r.get("bucket") == "usable":
                usable_codes.add(r["route_code"])
        print(f"  usable from index: {sorted(usable_codes)}")
    else:
        print(f"  WARNING: {index_path} not found")

    allowed = set(args.only) if args.only else (usable_codes if usable_codes else None)

    client = ValhallaClient(base_url=args.valhalla_url)
    try:
        client.refresh_capabilities(force=True)
    except Exception as exc:
        print(f"  WARNING: valhalla refresh_capabilities: {exc}")

    pipeline = ConstructorV2Pipeline(client=client)
    conn = psycopg2.connect(args.db_dsn)
    conn.autocommit = False

    results: list[dict] = []
    for seed, geo, seed_path in pairs:
        code = seed.get("route_code", seed_path.stem)
        if allowed and code not in allowed:
            print(f"  SKIP {code} (not usable)")
            continue
        print(f"  PROMOTE {code} ...", end=" ", flush=True)
        res = promote_one(conn, seed, geo, pipeline, args.province, args.source_tag, args.dry_run)
        print(f"{res['status']}  bucket={res.get('bucket','-')}  conf={res.get('confidence_label','-')}({res.get('confidence_score',0):.1f})")
        if res.get("error"):
            print(f"    ERROR: {res['error']}")
        results.append(res)

    if not args.dry_run:
        try:
            conn.commit()
            print("\n  COMMIT OK")
        except Exception as exc:
            print(f"\n  COMMIT FAILED: {exc}")
            conn.rollback()
            for r in results:
                if r["status"] == "promoted":
                    r["status"] = "commit_failed"
    else:
        conn.rollback()
        print("\n  DRY RUN — rolled back")
    conn.close()

    from collections import Counter
    by_status = Counter(r["status"] for r in results)
    print("\nSummary:")
    for k, v in sorted(by_status.items()):
        print(f"  {k}: {v}")

    report = {
        "province": args.province,
        "canton": args.canton,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "source_tag": args.source_tag,
        "results": results,
        "by_status": dict(by_status),
    }
    report_path = artifacts_dir / "_promotion_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nReport: {report_path}")

    return 0 if all(r["status"] in ("promoted", "skipped_not_usable", "dry_run_would_promote") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
