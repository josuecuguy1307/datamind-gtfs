#!/usr/bin/env python3
"""Promote usable Daule/Nobol seed-built routes to route_prod + catalog.

Reuses promote_duran_artifacts.promote_one() with a Daule/Nobol
jurisdiction map. Reads the _index.json from the daule_nobol artifact
directory to determine usable routes.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[4]
PHASE3_ROOT = _HERE.parents[1]
for p in (str(PHASE3_ROOT), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import psycopg2  # noqa: E402
from src.constructor_v2.clients.valhalla_client import ValhallaClient  # noqa: E402
from src.constructor_v2.constants import DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL  # noqa: E402
from src.constructor_v2.pipeline import ConstructorV2Pipeline  # noqa: E402

from scripts.build_from_seeds import load_catalog_pairs  # noqa: E402
from scripts.promote_duran_artifacts import promote_one  # noqa: E402


# Daule/Nobol cooperative → jurisdiction mapping.
# Per research notes:
#   - All intercantonal / interprovincial routes → ANT
#   - La Aurora internal circuits (R13/R14 LojasTrans) → ATM_DAULE
#   - ATM_GYE for routes R15/R16 (Rutas 63/64 La Aurora-Centro GYE)
COOP_JUR = {
    "coop_senor_milagros": "ANT",
    "coop_nobol_express": "ANT",
    "coop_rutas_balzarenas": "ANT",
    "coop_santa_lucia": "ANT",
    "coop_los_daulis": "ANT",
    "coop_narcisa_jesus_sa": "ANT",
    "coop_assad_bucaram": "ANT",
    "coop_santa_clara": "ANT",
    "coop_santa_rosa_colimes": "ANT",
    "coop_rutas_empenmenas": "ANT",
    "coop_fifa": "ANT",
    "coop_rutas_vincenas": "ANT",
    "coop_eloy_alfaro": "ATM_GYE",
    "coop_16_octubre": "ATM_GYE",
    "coop_jose_joaquin_olmedo": "ATM_GYE",
    "coop_lojastrans": "ATM_DAULE",
}

OVERRIDE = {
    "R13": "ATM_DAULE",
    "R14": "ATM_DAULE",
    "R15": "ATM_GYE",
    "R16": "ATM_GYE",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--province", default="sample_region_b")
    parser.add_argument("--catalog-dir", required=True)
    parser.add_argument("--index", required=True, help="Path to _index.json")
    parser.add_argument("--valhalla-url", default=DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL)
    parser.add_argument("--db-dsn", required=True)
    parser.add_argument("--source-tag", default="deep_research_daule_nobol_seeds_promote")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    catalog_dir = Path(args.catalog_dir).resolve()
    pairs = load_catalog_pairs(catalog_dir)

    idx = json.load(open(args.index))
    usable = {r["route_code"] for r in idx.get("results", []) if r.get("bucket") == "usable"}
    print(f"  usable ({len(usable)}): {sorted(usable)}")

    client = ValhallaClient(base_url=args.valhalla_url)
    try:
        client.refresh_capabilities(force=True)
    except Exception as exc:
        print(f"  WARN valhalla: {exc}")

    pipeline = ConstructorV2Pipeline(client=client)
    conn = psycopg2.connect(args.db_dsn)
    conn.autocommit = False

    # Patch promote_one's jurisdiction resolution by monkey-patching
    # its module-level dicts.
    import scripts.promote_duran_artifacts as _pmd
    _pmd.COOPERATIVE_JURISDICTION = COOP_JUR
    _pmd.CROSS_JURISDICTIONAL_OVERRIDES = OVERRIDE

    results = []
    for seed, geo, seed_path in pairs:
        code = seed.get("route_code", seed_path.stem)
        if code not in usable:
            print(f"  SKIP {code} (not usable)")
            continue
        print(f"  PROMOTE {code} ...", end=" ", flush=True)
        res = promote_one(conn, seed, geo, pipeline, args.province, args.source_tag, args.dry_run)
        print(f"{res['status']} bucket={res.get('bucket','-')} conf={res.get('confidence_label','-')}({res.get('confidence_score',0):.1f})")
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
    else:
        conn.rollback()
        print("\n  DRY RUN — rolled back")
    conn.close()

    from collections import Counter
    by_status = Counter(r["status"] for r in results)
    print("\nSummary:", dict(by_status))

    report = {
        "province": args.province,
        "canton": "daule_nobol",
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "source_tag": args.source_tag,
        "results": results,
        "by_status": dict(by_status),
    }
    report_path = Path(args.catalog_dir).parent / "constructor_artifacts" / "_promotion_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nReport: {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
