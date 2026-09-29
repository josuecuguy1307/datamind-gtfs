"""On-demand DR prompt generator.

Produces a parser-compatible Claude.ai prompt for a specific tier-4
unresolved gap (or a small bundle of them). The output is identical in
shape to the existing multi-route ``workspace/dr_stop_coverage/queries/
batch_*.md`` files so the existing
:func:`hades.enforcers.dr_stop_coverage_importer.import_batch_file` can
parse the response without modification.

Two output paths:

* ``generate_dr_prompt_for_gap(route_id, gap_idx)`` — single-gap
  on-demand prompt. Used when the worker hits a route whose tier-4
  gap fell outside every existing batch bbox (the
  ``routes_needing_on_demand_prompts`` list returned by
  :func:`hades.enforcers.reclassifier.populate_dr_dependencies`).

* ``generate_dr_prompt_for_unit_cohort(unit, max_gaps_per_prompt)`` —
  bundles every still-unresolved tier-4 gap in a unit into chunked
  prompts. Used when the operator wants to front-load DR work on a unit.

Both write to ``workspace/dr_stop_coverage/pending_requests/`` (a new
directory created on first call). On-demand request IDs follow the
``<unit_prefix>_<NNN>`` pattern (``qc_007``, ``vc_012``, …) and coexist
with the legacy ``batch_NN_locality`` IDs from the existing files. Both
are valid ``dr_batch_id`` values for ``route_prod.dr_batch_dependencies``.

CLI:
    python -m hades.enforcers.dr_prompt_generator generate \\
        --route-id <uuid> --gap-idx <N>
    python -m hades.enforcers.dr_prompt_generator unit \\
        --unit <unit_name> [--max-per-prompt 10]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import psycopg2
import psycopg2.extras
from datamind_core.dsn import need_dsn

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DSN = ""
PENDING_REQUESTS_DIR = ROOT / "workspace" / "dr_stop_coverage" / "pending_requests"
ASSIGNMENT_MAPS_DIR = ROOT / "workspace" / "unit_logs" / "_assignment_maps"

# Maps unit name → request-id prefix. New units get a 2-3 letter prefix
# here. Anything missing falls back to "unc" (unclassified).
UNIT_PREFIXES: dict[str, str] = {
    "quito_centro":  "qc",
    "quito_norte":   "qn",
    "quito_sur":     "qs",
    "valle_chillos": "vc",
    "cayambe":       "cay",
    "mejia":         "mej",
    "pedro_moncayo": "pm",
    "unclassified":  "unc",
}


def _dsn() -> str:
    return os.environ.get("DB_DSN", DEFAULT_DSN)


# ---------------------------------------------------------------------------
# Templates — kept in sync with the existing batch_*.md format.
# ---------------------------------------------------------------------------

_PROMPT_HEADER = """\
# DR On-demand Request — {request_id}

**Type:** Stop Coverage Gap Filling (DR Type 2)
**Generated:** {generated_at}
**Total queries:** {n_queries}
**Unique routes:** {n_routes}

## Geographic scope

- Unit: {unit}
- Bounding box: [{bbox_lat_min:.4f}, {bbox_lon_min:.4f}, {bbox_lat_max:.4f}, {bbox_lon_max:.4f}]

## Instructions for responder (Claude with web search)

You are helping fill in stop coverage gaps for bus routes in Ecuador. You have web search enabled. Your job: find real paradas (formal or informal) where buses stop in each specific tramo described below.

Before answering individual queries, build context with 2-3 web searches:
1. Facebook pages of cooperativas listed
2. Blogs about transporte público in the primary cantons
3. Google Maps reviews mentioning "parada" + the named corridors
4. Ecuadorian news (El Comercio, Últimas Noticias, El Universo) about public transport in this sector

Then process each query.

## Response format (parser-strict — do not deviate)

For each query, respond with EXACTLY ONE of these blocks:

### LANDMARK_FOUND
    QUERY_ID: Q001
    ROUTE_CODE: <echo>
    GAP_NUMBER: <echo>
    LANDMARKS:
      - name: "<primary landmark>"
        local_reference: "<local nickname>"
        approx_lat: <decimal>
        approx_lng: <decimal>
        confidence: high | medium | low
        source_description: "<brief: where this came from>"

### LANDMARK_NOT_FOUND
    QUERY_ID: Q001
    ROUTE_CODE: <echo>
    GAP_NUMBER: <echo>
    FOUND: false
    reason: "<brief: why web search did not yield results>"
    fallback: use_synthetic

No prose outside these blocks. Up to 5 landmarks per query. Coords WGS84 decimal.

## Queries

"""

_QUERY_BLOCK = """\
### Q{qnum:03d}
- Route code: {route_id}
- Route name: {route_name}
- Operator/cooperativa: {operator}
- Canton: {canton}, {province}
- Gap number in this route: {gap_number}
- Gap length: {gap_m:.0f}m
- Stop before gap: "{prev_name}" at {prev_lat:.4f}, {prev_lon:.4f}
- Stop after gap: "{next_name}" at {next_lat:.4f}, {next_lon:.4f}
- Named streets in gap segment: (not aggregated)
- Gap midpoint: {mid_lat:.4f}, {mid_lon:.4f}
- Zone type: {zone}

Find 1-3 real landmarks where buses actually stop in this specific tramo.

---

"""

_INSTRUCTIONS_TEMPLATE = """\
# Operator instructions — {request_id}

This is an **on-demand DR request** generated for {n_queries} gap(s) on
{n_routes} route(s) that did not match any existing batch bbox.

## Steps

1. Open the prompt file: `{prompt_relpath}`
2. Open Claude.ai, enable web search.
3. Paste the entire contents of the prompt file into the chat.
4. Wait for the response. It must follow the parser-strict format
   described in the prompt header (LANDMARK_FOUND / LANDMARK_NOT_FOUND
   blocks, one per query).
5. Save the response to: `{response_relpath}`
6. Run the handler:
       python -m hades.enforcers.dr_response_handler {response_relpath}

The handler will:
   - parse the response into `workspace/dr_stop_coverage/parsed/{request_id}.json`
   - validate landmarks via Overpass + Nominatim into
     `workspace/dr_stop_coverage/validated/{request_id}.json`
   - update `route_prod.dr_batch_dependencies` rows to landmark_found /
     landmark_not_found
   - re-run the v2 classifier on every affected approval_queue row and
     report which ones upgraded.
"""


# ---------------------------------------------------------------------------
# Unit lookup (from assignment maps).
# ---------------------------------------------------------------------------

def _load_route_to_unit() -> dict[str, str]:
    """Reverse-index: route_id (text) → unit name."""
    out: dict[str, str] = {}
    if not ASSIGNMENT_MAPS_DIR.exists():
        return out
    for p in sorted(ASSIGNMENT_MAPS_DIR.glob("*.json")):
        unit = p.stem
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        for rid in (data.get("all_route_ids") or []):
            out[str(rid)] = unit
    return out


def _unit_prefix(unit: str) -> str:
    return UNIT_PREFIXES.get(unit, UNIT_PREFIXES["unclassified"])


def _unit_subdir(unit: str) -> Path:
    """Per-unit subfolder under ``pending_requests/``.

    Convention (skill 17, 2026-04-22): every on-demand DR prompt lives
    inside ``pending_requests/<unit_name>/`` — never in the flat root.
    Keeps prefixes from mixing across units (qc_, qn_, vc_, …) when many
    units are active in parallel.
    """
    d = PENDING_REQUESTS_DIR / unit
    d.mkdir(parents=True, exist_ok=True)
    return d


def _next_request_number(prefix: str, unit: str) -> int:
    """Scan ``pending_requests/<unit>/`` for ``<prefix>_NNN.md`` and return next NNN.

    Also peeks at the legacy flat root + the processed/ archive so the
    counter monotonically advances even after files are archived or
    moved out of the unit subfolder.
    """
    pat = re.compile(rf"^{re.escape(prefix)}_(\d+)\.md$")
    nums: list[int] = []
    scan_dirs = [
        _unit_subdir(unit),
        _unit_subdir(unit) / "processed",
        PENDING_REQUESTS_DIR,                # legacy flat layout (pre-2026-04-22)
        PENDING_REQUESTS_DIR / "processed",
    ]
    for d in scan_dirs:
        if not d.exists():
            continue
        for p in d.glob(f"{prefix}_*.md"):
            m = pat.match(p.name)
            if m:
                nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


# ---------------------------------------------------------------------------
# Per-gap data fetcher.
# ---------------------------------------------------------------------------

def _fetch_gap_context(
    cur,
    route_id: str,
    gap_idx: int,
) -> Optional[dict[str, Any]]:
    """Pull the route record + the named gap from approval_queue.

    Returns a flat dict ready for the QUERY_BLOCK template.
    """
    cur.execute(
        """
        SELECT r.route_id::text       AS route_id,
               r.route_name           AS route_name,
               r.province             AS province,
               r.source               AS operator,
               aq.stop_coverage_report AS sc,
               aq.proposed_stops       AS proposed_stops
          FROM route_prod.approval_queue aq
          JOIN route_prod.routes r ON r.route_id::text = aq.route_code
         WHERE aq.status = 'pending'
           AND aq.route_code = %s
         LIMIT 1
        """,
        (route_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    sc = row["sc"] or {}
    gaps = sc.get("gaps") or []
    gap = next((g for g in gaps if int(g.get("idx", -1)) == gap_idx), None)
    if not gap:
        return None
    mc = gap.get("midpoint_coord") or [0.0, 0.0]
    stops = row["proposed_stops"] or []

    def _stop_label(idx: int) -> tuple[str, float, float]:
        if idx < 0 or idx >= len(stops):
            return ("(route end)", 0.0, 0.0)
        s = stops[idx]
        return (
            str(s.get("name") or s.get("stop_id") or "(unnamed)"),
            float(s.get("lat") or 0.0),
            float(s.get("lon") or 0.0),
        )

    prev_name, prev_lat, prev_lon = _stop_label(int(gap.get("prev_stop_idx", -1)))
    next_name, next_lat, next_lon = _stop_label(int(gap.get("next_stop_idx", -1)))

    return {
        "route_id": row["route_id"],
        "route_name": (row["route_name"] or "(unnamed)").strip(),
        "operator": (row["operator"] or "(unknown)").strip()[:80],
        "canton": "(unknown)",
        "province": (row["province"] or "sample_region").strip(),
        "gap_number": gap_idx + 1,
        "gap_m": float(gap.get("gap_m") or 0.0),
        "prev_name": prev_name,
        "prev_lat": prev_lat,
        "prev_lon": prev_lon,
        "next_name": next_name,
        "next_lat": next_lat,
        "next_lon": next_lon,
        "mid_lat": float(mc[0]),
        "mid_lon": float(mc[1]),
        "zone": str(sc.get("zone") or "urban_peripheral"),
    }


# ---------------------------------------------------------------------------
# Prompt rendering + persistence.
# ---------------------------------------------------------------------------

def _render_prompt(
    request_id: str,
    unit: str,
    gap_contexts: list[dict[str, Any]],
) -> str:
    if not gap_contexts:
        raise ValueError("Cannot render an empty prompt.")
    queries_text = []
    for i, ctx in enumerate(gap_contexts, start=1):
        ctx_with_qnum = dict(ctx, qnum=i)
        queries_text.append(_QUERY_BLOCK.format(**ctx_with_qnum))
    lats = [c["mid_lat"] for c in gap_contexts]
    lons = [c["mid_lon"] for c in gap_contexts]
    header = _PROMPT_HEADER.format(
        request_id=request_id,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        n_queries=len(gap_contexts),
        n_routes=len({c["route_id"] for c in gap_contexts}),
        unit=unit,
        bbox_lat_min=min(lats), bbox_lon_min=min(lons),
        bbox_lat_max=max(lats), bbox_lon_max=max(lons),
    )
    return header + "".join(queries_text)


def _register_dependencies(
    cur,
    request_id: str,
    gap_contexts: list[dict[str, Any]],
) -> int:
    """INSERT a dr_batch_dependencies row per (route, gap) for this request."""
    n = 0
    for ctx in gap_contexts:
        cur.execute(
            """
            INSERT INTO route_prod.dr_batch_dependencies
                (dr_batch_id, route_id, gap_idx, gap_coords, status)
            VALUES (%s, %s::uuid, %s, %s::jsonb, 'waiting')
            ON CONFLICT (dr_batch_id, route_id, gap_idx) DO NOTHING
            """,
            (
                request_id,
                ctx["route_id"],
                int(ctx["gap_number"]) - 1,
                json.dumps({"lat": ctx["mid_lat"], "lon": ctx["mid_lon"]}),
            ),
        )
        n += cur.rowcount
        # Also append to approval_queue.pending_dr_batches.
        cur.execute(
            """
            UPDATE route_prod.approval_queue
               SET pending_dr_batches = (
                   SELECT ARRAY(
                       SELECT DISTINCT unnest(
                           pending_dr_batches || ARRAY[%s]::text[]
                       )
                   )
               )
             WHERE route_code = %s
               AND status     = 'pending'
            """,
            (request_id, ctx["route_id"]),
        )
    return n


def _write_prompt(
    request_id: str,
    unit: str,
    prompt_text: str,
    n_queries: int,
    n_routes: int,
) -> tuple[Path, Path]:
    """Write the prompt + INSTRUCTIONS into the per-unit subfolder.

    Output paths:
        pending_requests/<unit>/<request_id>.md
        pending_requests/<unit>/<request_id>.INSTRUCTIONS.md

    The INSTRUCTIONS template points the operator at the matching
    ``responses/<unit>/<request_id>.md`` location so unit-organized
    storage stays consistent across the request → response → archive
    lifecycle.
    """
    out_dir = _unit_subdir(unit)
    prompt_path = out_dir / f"{request_id}.md"
    instr_path  = out_dir / f"{request_id}.INSTRUCTIONS.md"
    response_relpath = (
        ROOT / "workspace" / "dr_stop_coverage" / "responses" / unit / f"{request_id}.md"
    ).relative_to(ROOT)
    prompt_relpath = prompt_path.relative_to(ROOT)
    prompt_path.write_text(prompt_text)
    instr_path.write_text(
        _INSTRUCTIONS_TEMPLATE.format(
            request_id=request_id,
            n_queries=n_queries,
            n_routes=n_routes,
            prompt_relpath=str(prompt_relpath),
            response_relpath=str(response_relpath),
        )
    )
    return prompt_path, instr_path


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def generate_dr_prompt_for_gap(
    route_id: str,
    gap_idx: int,
    *,
    dsn: Optional[str] = None,
    unit: Optional[str] = None,
) -> dict[str, Any]:
    """Build a single-gap prompt and register the dependency.

    Returns a dict with ``request_id``, ``prompt_path``, ``instructions_path``.
    """
    dsn = dsn or _dsn()
    if unit is None:
        unit = _load_route_to_unit().get(route_id, "unclassified")
    prefix = _unit_prefix(unit)
    request_id = f"{prefix}_{_next_request_number(prefix, unit):03d}"

    with psycopg2.connect(need_dsn(dsn)) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ctx = _fetch_gap_context(cur, route_id, gap_idx)
        if ctx is None:
            raise ValueError(
                f"Could not locate gap_idx={gap_idx} for route_id={route_id} "
                f"in pending approval_queue."
            )
        prompt_text = _render_prompt(request_id, unit, [ctx])
        with conn.cursor() as cur:
            _register_dependencies(cur, request_id, [ctx])
    prompt_path, instr_path = _write_prompt(
        request_id, unit, prompt_text, n_queries=1, n_routes=1,
    )
    return {
        "request_id": request_id,
        "unit": unit,
        "route_id": route_id,
        "gap_idx": gap_idx,
        "prompt_path": str(prompt_path.relative_to(ROOT)),
        "instructions_path": str(instr_path.relative_to(ROOT)),
    }


def generate_dr_prompt_for_unit_cohort(
    unit: str,
    *,
    max_gaps_per_prompt: int = 10,
    dsn: Optional[str] = None,
    skip_routes_with_legacy_batches: bool = False,
    skip_gaps_with_validated_landmarks: bool = True,
) -> list[dict[str, Any]]:
    """Bundle every still-unresolved tier-4 gap in ``unit`` into prompts.

    Per skill 17 (canonical 2026-04-22), the default behaviour is to
    generate on-demand prompts for **every** unresolved tier-4 gap in
    the unit, regardless of whether the route is already linked to a
    legacy ``batch_*`` id in ``pending_dr_batches``. Legacy linkage is
    historic and biased; the on-demand generator is the canonical path
    for new DR work.

    ``skip_routes_with_legacy_batches=True`` opts back into the old
    behaviour (skip any gap on a route whose ``pending_dr_batches``
    already contains a ``batch_*`` entry) — useful only if you
    deliberately want to defer to the legacy batch corpus for that
    route.

    ``skip_gaps_with_validated_landmarks=True`` (default) skips any
    (route, gap_idx) that already has a validated landmark in
    ``workspace/dr_stop_coverage/validated/*.json``. This is the
    canonical "enrich more" mode — only un-landmarked gaps are sent for
    a fresh DR round. The persisted ``stop_coverage_report`` still
    marks these gaps as ``tier=4_prepared`` (the worker hasn't re-run
    yet), but the reclassifier already knows they're covered, so we
    don't waste a Claude.ai pass on them.
    """
    dsn = dsn or _dsn()
    rid_path = ASSIGNMENT_MAPS_DIR / f"{unit}.json"
    if not rid_path.exists():
        raise FileNotFoundError(rid_path)
    rids = json.loads(rid_path.read_text()).get("all_route_ids") or []
    prefix = _unit_prefix(unit)

    # Build the (route, gap_idx) → landmark index once if we need to filter.
    landmark_keys: set[tuple[str, int]] = set()
    if skip_gaps_with_validated_landmarks:
        from hades.enforcers.reclassifier import _load_validated_landmarks
        landmark_keys = set(_load_validated_landmarks().keys())

    gap_contexts: list[dict[str, Any]] = []
    with psycopg2.connect(need_dsn(dsn)) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT route_code, stop_coverage_report, pending_dr_batches
                  FROM route_prod.approval_queue
                 WHERE status = 'pending'
                   AND route_code = ANY(%s)
                """,
                (rids,),
            )
            rows = cur.fetchall()
        for r in rows:
            already = set(r["pending_dr_batches"] or [])
            sc = r["stop_coverage_report"] or {}
            for g in (sc.get("gaps") or []):
                res = g.get("resolution") or {}
                if int(res.get("tier") or 0) != 4 or res.get("resolved"):
                    continue
                if (skip_routes_with_legacy_batches
                        and any(b.startswith("batch_") for b in already)):
                    continue
                gap_idx = int(g.get("idx") or 0)
                if (skip_gaps_with_validated_landmarks
                        and (r["route_code"], gap_idx) in landmark_keys):
                    continue
                with psycopg2.connect(need_dsn(dsn)) as inner_conn:
                    with inner_conn.cursor(
                        cursor_factory=psycopg2.extras.RealDictCursor,
                    ) as cur2:
                        ctx = _fetch_gap_context(
                            cur2, r["route_code"], int(g.get("idx") or 0),
                        )
                if ctx is not None:
                    gap_contexts.append(ctx)

    if not gap_contexts:
        return []

    # Chunk into prompts of <= max_gaps_per_prompt.
    chunks = [
        gap_contexts[i:i + max_gaps_per_prompt]
        for i in range(0, len(gap_contexts), max_gaps_per_prompt)
    ]
    out: list[dict[str, Any]] = []
    with psycopg2.connect(need_dsn(dsn)) as conn:
        for chunk in chunks:
            request_id = f"{prefix}_{_next_request_number(prefix, unit):03d}"
            prompt_text = _render_prompt(request_id, unit, chunk)
            with conn.cursor() as cur:
                _register_dependencies(cur, request_id, chunk)
            prompt_path, instr_path = _write_prompt(
                request_id, unit, prompt_text,
                n_queries=len(chunk),
                n_routes=len({c["route_id"] for c in chunk}),
            )
            out.append({
                "request_id": request_id,
                "n_queries": len(chunk),
                "n_routes": len({c["route_id"] for c in chunk}),
                "prompt_path": str(prompt_path.relative_to(ROOT)),
                "instructions_path": str(instr_path.relative_to(ROOT)),
            })
    return out


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dr_prompt_generator",
        description="On-demand DR prompt generator for tier-4 unresolved gaps.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_g = sub.add_parser(
        "generate", help="Single-gap prompt for one route + gap_idx.",
    )
    p_g.add_argument("--route-id", required=True)
    p_g.add_argument("--gap-idx", type=int, required=True)
    p_g.add_argument(
        "--unit",
        help="Override unit lookup (defaults to assignment-map reverse index).",
    )

    p_u = sub.add_parser(
        "unit",
        help="Bundle every still-unresolved tier-4 gap in a unit into prompts.",
    )
    p_u.add_argument("--unit", required=True)
    p_u.add_argument("--max-per-prompt", type=int, default=10)
    p_u.add_argument(
        "--skip-legacy-covered",
        action="store_true",
        help=("Opt-in to legacy behaviour: skip gaps whose route has a "
              "legacy batch_* id in pending_dr_batches. Default OFF — "
              "skill 17 says generate on-demand regardless."),
    )
    p_u.add_argument(
        "--include-gaps-with-landmarks",
        action="store_true",
        help=("Opt out of the default 'enrich more' filter and ALSO emit "
              "prompts for gaps that already have a validated landmark. "
              "Useful only for replay / re-validation runs."),
    )

    args = parser.parse_args(argv)

    if args.cmd == "generate":
        result = generate_dr_prompt_for_gap(
            args.route_id, args.gap_idx, unit=args.unit,
        )
        print(json.dumps(result, indent=2))
    elif args.cmd == "unit":
        results = generate_dr_prompt_for_unit_cohort(
            args.unit,
            max_gaps_per_prompt=args.max_per_prompt,
            skip_routes_with_legacy_batches=args.skip_legacy_covered,
            skip_gaps_with_validated_landmarks=not args.include_gaps_with_landmarks,
        )
        print(json.dumps({"prompts_generated": len(results), "files": results},
                         indent=2))
    else:  # pragma: no cover
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
