#!/usr/bin/env python3
"""
Ingest a lightweight 06a exhaustive_route_inventory JSON (no seed_catalog coords)
by creating route_raw.route_jobs stubs and emitting one 06c stop_grounding_detail
prompt per route into workspace/research_queue/pending/.

Input JSON schema (per route): route_code, short_name, long_name, cooperative,
origin, destination, via[], corridor_classification, operating_hours,
frequency_minutes, fleet_size, confidence, source, status.

Usage:
    python -m datamind_console.scripts.ingest_inventory_06a \\
        --input "workspace/provinces/sample_region/INVENTORY ROUTES NEW/quito_sur_inventory.json" \\
        [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from datamind_console.common.research_queue import write_prompt
from datamind_console.db.db import db_conn

QUEUE_ROOT = ROOT / "workspace" / "research_queue"


def _slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower().strip()
    out = []
    for ch in s:
        out.append(ch if ch.isalnum() else "-")
    s = "".join(out)
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


def _route_hint(route: dict) -> str:
    via = " → ".join(route.get("via") or [])
    origin = route.get("origin", "")
    dest = route.get("destination", "")
    if via:
        return f"{origin} → {via} → {dest}"
    return f"{origin} → {dest}"


def _route_notes(route: dict, unit: str, research_date: str) -> str:
    return (
        f"Deep Research 06a inventory ({research_date}): "
        f"{route.get('long_name','')} — {route.get('cooperative','?')} — "
        f"{_route_hint(route)} — "
        f"corridor={route.get('corridor_classification','?')} "
        f"hours={route.get('operating_hours','?')} "
        f"headway_min={route.get('frequency_minutes','?')} "
        f"confidence={route.get('confidence','?')} "
        f"source={route.get('source','?')}"
    )


def _upsert_route_job(cur, route: dict, unit: str, extractor_source: str, research_date: str):
    """Return (route_id, inserted_bool). Idempotent on (known_ref, area_key, extractor_source)."""
    code = route["route_code"]
    cur.execute(
        """
        SELECT route_id FROM route_raw.route_jobs
        WHERE known_ref = %s AND area_key = %s AND extractor_source = %s AND is_trashed = FALSE
        LIMIT 1
        """,
        (code, unit, extractor_source),
    )
    row = cur.fetchone()
    if row:
        rid = row["route_id"] if isinstance(row, dict) else row[0]
        return str(rid), False

    cur.execute(
        """
        INSERT INTO route_raw.route_jobs (
            created_by, status, notes, area_key, known_ref, extractor_source, province
        )
        VALUES (%s, 'new', %s, %s, %s, %s, 'sample_region')
        RETURNING route_id
        """,
        (
            "ingest_inventory_06a",
            _route_notes(route, unit, research_date),
            unit,
            code,
            extractor_source,
        ),
    )
    row = cur.fetchone()
    rid = row["route_id"] if isinstance(row, dict) else row[0]
    return str(rid), True


def _build_06c_body(route: dict, unit: str, province: str, research_date: str) -> str:
    code = route["route_code"]
    coop = route.get("cooperative", "?")
    origin = route.get("origin", "")
    dest = route.get("destination", "")
    via = route.get("via") or []
    corridor = route.get("corridor_classification", "")
    hours = route.get("operating_hours", "")
    headway = route.get("frequency_minutes", "")
    src = route.get("source", "")
    display = route.get("long_name") or f"{origin} — {dest}"

    via_md = "\n".join(f"- {v}" for v in via) if via else "- (none documented in baseline inventory)"

    body = f"""# Deep Research — Stop Grounding Detail

**Route:** {code} — {display}
**Cooperative:** {coop}
**Unit / Province:** {unit} / {province}
**Corridor classification:** {corridor or "?"}
**Operating hours (baseline):** {hours or "?"}
**Headway (min, baseline):** {headway or "?"}
**Baseline inventory source(s):** {src or "?"}
**Baseline research date:** {research_date}

## Context from 06a inventory

Route `{code}` was catalogued during the 06a exhaustive route inventory on {research_date} for
`{unit}` but does not yet carry coordinate-backed anchors. The 06a payload listed:

- **Origin:** {origin or "?"}
- **Destination:** {dest or "?"}
- **Via (documented stops/sectors):**
{via_md}

The Phase 3 stop-grounding pipeline needs a dual catalog per route
(seed catalog + geography catalog) before it can ground this route against
`node_prod.nodes`. Please return the fields below.

## Required output (JSON; single object)

```json
{{
  "prompt_type": "stop_grounding_detail",
  "route_code": "{code}",
  "unit": "{unit}",
  "canton": "{unit}",
  "province": "{province}",
  "research_date": "YYYY-MM-DD",

  "seed_catalog_completion": {{
    "terminus_origin":      {{"name": "", "lat": 0.0, "lon": 0.0, "confidence": "high|medium|low", "landmark_near": ""}},
    "terminus_destination": {{"name": "", "lat": 0.0, "lon": 0.0, "confidence": "high|medium|low", "landmark_near": ""}},
    "intermediate_anchors": [
      {{"sequence_index": 1, "name": "", "lat": 0.0, "lon": 0.0, "landmark_near": "", "confidence": "high|medium|low"}}
    ],
    "primary_corridor": "",
    "corridor_roads": []
  }},

  "geography_catalog_completion": {{
    "zone_classification": "urban|periurban|rural|interparroquial",
    "envelope_bbox": [south, west, north, east],
    "required_areas":  [{{"name": "", "centroid_lat": 0.0, "centroid_lon": 0.0, "radius_m": 0}}],
    "forbidden_areas": [{{"name": "", "centroid_lat": 0.0, "centroid_lon": 0.0, "radius_m": 0}}]
  }},

  "grounding_gaps_remaining": [],
  "evidence_sources": ["URL or document"],
  "evidence_date": "YYYY-MM-DD",
  "evidence_confidence": "high|medium|low"
}}
```

### Constraints

- **Coordinate precision:** ≥ 5 decimals for urban DMQ stops (Quitumbe, Solanda,
  La Marín, Terminal Quitumbe, etc.). ≥ 4 decimals acceptable only for
  rural/periurban fringes.
- **Intermediate anchors:** target ≥ 5. If the route is short-loop or feeder
  and genuinely has fewer, explain in `grounding_gaps_remaining[]` rather than
  padding.
- **Required/forbidden areas:** list any district the route must pass through
  (e.g., "Parroquia Quitumbe", "Av. Mariscal Sucre Sur") and any it must NOT
  enter (e.g., "Quito Centro Histórico" for a pure-Sur route).
- **Sources:** prefer primary operator pages, MyBusEc, openalfa.com,
  operator Facebook pages, cooperative directories, and municipal
  mobility documents. Do not rely on a single source.

Refuse to fabricate coordinates. If a stop location is genuinely unknown,
list it in `grounding_gaps_remaining[]` with a reason.
"""
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Path to 06a inventory JSON")
    ap.add_argument("--dry-run", action="store_true", help="Show plan, no DB writes or queue writes")
    ap.add_argument("--no-prompts", action="store_true", help="Skip queue prompt emission (DB only)")
    args = ap.parse_args()

    inp = Path(args.input)
    data = json.loads(inp.read_text())

    unit = data["unit"]
    province = (data.get("province") or "sample_region").lower()
    research_date = data.get("research_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    routes = data.get("routes") or []
    extractor_source = f"deep_research_{unit}_{research_date}"

    print(f"Input:            {inp}")
    print(f"Unit:             {unit}")
    print(f"Province:         {province}")
    print(f"Research date:    {research_date}")
    print(f"Routes in JSON:   {len(routes)}")
    print(f"extractor_source: {extractor_source}")
    print(f"Queue root:       {QUEUE_ROOT}")
    print()

    if args.dry_run:
        for i, r in enumerate(routes[:5]):
            print(f"  [{i+1}] {r['route_code']:30s} {r.get('cooperative',''):20s} "
                  f"{r.get('origin','')[:20]:20s} → {r.get('destination','')[:20]}")
        if len(routes) > 5:
            print(f"  ... + {len(routes)-5} more")
        print("\nDRY RUN — nothing written.")
        return 0

    inserted = 0
    skipped = 0
    prompts_written = 0
    prompts_skipped = 0

    with db_conn() as conn:
        for i, route in enumerate(routes, 1):
            code = route["route_code"]
            with conn.cursor() as cur:
                rid, was_inserted = _upsert_route_job(cur, route, unit, extractor_source, research_date)
            conn.commit()
            if was_inserted:
                inserted += 1
                tag = "OK"
            else:
                skipped += 1
                tag = "SKIP"
            if i <= 3 or i % 20 == 0:
                print(f"  [{i:3d}] {tag} {code:32s} rid={rid}")

            if args.no_prompts:
                continue

            dedup_key = f"stop_grounding_detail:{unit}:{code}:initial_grounding"
            body = _build_06c_body(route, unit, province, research_date)
            try:
                path = write_prompt(
                    queue_root=QUEUE_ROOT,
                    prompt_type="stop_grounding_detail",
                    route_code=code,
                    unit=unit,
                    province=province,
                    trigger_condition="06a inventory ingested; route lacks coordinate-backed seed_catalog",
                    priority=2,
                    estimated_research_budget="standard",
                    depends_on=[],
                    dedup_key=dedup_key,
                    prompt_markdown_content=body,
                    generated_by_skill="ingest_inventory_06a",
                )
                if path.parent.name == "pending":
                    prompts_written += 1
                else:
                    prompts_skipped += 1
            except Exception as exc:
                prompts_skipped += 1
                print(f"        prompt-write failed for {code}: {exc}")

    print()
    print(f"=== Done ({unit}) ===")
    print(f"  route_jobs inserted: {inserted}")
    print(f"  route_jobs skipped:  {skipped}  (already existed for same extractor_source)")
    print(f"  prompts written:     {prompts_written}  → {QUEUE_ROOT / 'pending'}")
    print(f"  prompts skipped:     {prompts_skipped}  (dedup_key already in queue)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
