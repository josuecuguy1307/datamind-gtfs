"""[LEGACY — 2026-04-22] Exporter for DR Type 2 batches.

Pairs with ``dr_stop_coverage_clusterer``: produced the historical
``workspace/dr_stop_coverage/queries/batch_NN_<zone>.md`` corpus.
Preserved for backward compatibility with that corpus.

NEW DR WORK SHOULD USE: ``hades.enforcers.dr_prompt_generator`` (skill 17).

Do not call this module for new route processing. See
``workspace/skills/17_DEPRECATION_AUDIT.md``.

---

Reads the clustering plan + the diagnostic JSONL + route_prod metadata,
then writes one parser-strict Markdown batch file per cluster into
``workspace/dr_stop_coverage/queries/`` plus a curated ``batch_00_pilot.md``
with 1-2 queries drawn from every production cluster.

This writes **DR Type 2** only. DR Type 1 (``workspace/research_queue/``)
is never touched. Read-only DB access; no writes to ``route_prod.*``.
See ``workspace/skills/14_DR_STOP_COVERAGE_GAP_FILLING.md`` for the
contract this implements.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from datamind_core.dsn import need_dsn


ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = ROOT / "workspace" / "dr_stop_coverage" / "_clustering_plan.json"
DIAG_PATH = ROOT / "workspace" / "diagnostics" / "stop_coverage_diagnostic_full.jsonl"
OUT_DIR = ROOT / "workspace" / "dr_stop_coverage" / "queries"

DSN = os.environ.get("DB_DSN", "")

PILOT_PER_CLUSTER = 2
PILOT_TARGET_TOTAL = 15


@dataclass
class GapDetail:
    route_code: str
    gap_idx: int
    gap_m: float
    zone: str
    midpoint_lat: float
    midpoint_lon: float
    prev_stop_idx: int
    next_stop_idx: int


@dataclass
class RouteMeta:
    route_code: str
    route_name: str
    source_type: str
    province: str
    stop_node_ids: list[str] = field(default_factory=list)
    stop_names: list[Optional[str]] = field(default_factory=list)
    stop_lat: list[Optional[float]] = field(default_factory=list)
    stop_lon: list[Optional[float]] = field(default_factory=list)
    canton: Optional[str] = None
    cooperativa: Optional[str] = None


# ----------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------

def _load_plan() -> dict[str, Any]:
    if not PLAN_PATH.exists():
        raise SystemExit(
            f"Clustering plan not found at {PLAN_PATH}. Run "
            "`python -m hades.enforcers.dr_stop_coverage_clusterer` first."
        )
    with PLAN_PATH.open() as fh:
        plan = json.load(fh)
    if not plan.get("size_bounds_satisfied", False):
        print(
            f"[exporter] WARNING: clustering plan has "
            f"size_bounds_satisfied=False (method={plan['method']}). "
            "Proceeding anyway; review batches for undersized clusters."
        )
    return plan


def _load_diagnostic() -> dict[str, dict[str, Any]]:
    """Index diagnostic JSONL by route_code."""
    if not DIAG_PATH.exists():
        raise SystemExit(f"Diagnostic JSONL not found at {DIAG_PATH}.")
    idx: dict[str, dict[str, Any]] = {}
    with DIAG_PATH.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            idx[rec["route_code"]] = rec
    print(f"[exporter] loaded {len(idx)} diagnostic records")
    return idx


def _fetch_route_meta(
    route_codes: list[str],
    diag_idx: dict[str, dict[str, Any]],
) -> dict[str, RouteMeta]:
    """Hit route_prod.routes once for all routes needed, then resolve
    every referenced stop_node_id against node_prod.nodes in one query.
    """
    metas: dict[str, RouteMeta] = {}
    with psycopg2.connect(need_dsn(DSN)) as conn:
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    route_id::text AS route_id,
                    route_name,
                    source_type,
                    province,
                    stop_node_ids::text[] AS stop_node_ids,
                    direction_semantics
                FROM route_prod.routes
                WHERE route_id::text = ANY(%s)
                """,
                (route_codes,),
            )
            row_by_rc: dict[str, dict[str, Any]] = {}
            all_stop_ids: set[str] = set()
            for row in cur.fetchall():
                row_by_rc[row["route_id"]] = row
                for sid in row["stop_node_ids"] or []:
                    all_stop_ids.add(sid)

            node_map: dict[str, dict[str, Any]] = {}
            if all_stop_ids:
                cur.execute(
                    """
                    SELECT
                        node_id::text AS node_id,
                        name,
                        ST_Y(geom) AS lat,
                        ST_X(geom) AS lon
                    FROM node_prod.nodes
                    WHERE node_id::text = ANY(%s)
                    """,
                    (list(all_stop_ids),),
                )
                for row in cur.fetchall():
                    node_map[row["node_id"]] = row

    for rc in route_codes:
        diag = diag_idx.get(rc, {})
        row = row_by_rc.get(rc)
        if row is None:
            metas[rc] = RouteMeta(
                route_code=rc,
                route_name=diag.get("route_name", "(unknown)"),
                source_type=diag.get("source_type", "unknown"),
                province=diag.get("province", "unknown"),
            )
            continue

        ds = row.get("direction_semantics") or {}
        canton = ds.get("canton") or ds.get("canton_primary")
        operator = (
            ds.get("chosen_rel_operator")
            or ds.get("service_route_operator")
            or ds.get("cooperative_hint")
            or ds.get("operator")
        )

        stop_names: list[Optional[str]] = []
        stop_lat: list[Optional[float]] = []
        stop_lon: list[Optional[float]] = []
        for sid in row["stop_node_ids"] or []:
            n = node_map.get(sid)
            if n is not None:
                stop_names.append(n.get("name"))
                stop_lat.append(n.get("lat"))
                stop_lon.append(n.get("lon"))
            else:
                stop_names.append(None)
                stop_lat.append(None)
                stop_lon.append(None)

        metas[rc] = RouteMeta(
            route_code=rc,
            route_name=row.get("route_name") or diag.get("route_name", "(unknown)"),
            source_type=row.get("source_type") or diag.get("source_type", "unknown"),
            province=row.get("province") or diag.get("province", "unknown"),
            stop_node_ids=list(row["stop_node_ids"] or []),
            stop_names=stop_names,
            stop_lat=stop_lat,
            stop_lon=stop_lon,
            canton=canton,
            cooperativa=operator,
        )
    return metas


# ----------------------------------------------------------------------
# Query expansion
# ----------------------------------------------------------------------

def _collect_gap_detail(
    route_code: str,
    gap_idx: int,
    diag_idx: dict[str, dict[str, Any]],
) -> Optional[GapDetail]:
    rec = diag_idx.get(route_code)
    if rec is None:
        return None
    for gap in rec.get("gaps", []):
        if gap.get("idx") == gap_idx:
            mc = gap.get("midpoint_coord") or [None, None]
            return GapDetail(
                route_code=route_code,
                gap_idx=gap_idx,
                gap_m=float(gap.get("gap_m") or 0.0),
                zone=rec.get("zone", "unknown"),
                midpoint_lat=float(mc[0]) if mc[0] is not None else 0.0,
                midpoint_lon=float(mc[1]) if mc[1] is not None else 0.0,
                prev_stop_idx=int(gap.get("prev_stop_idx", -1)),
                next_stop_idx=int(gap.get("next_stop_idx", -1)),
            )
    return None


def _fmt_stop(
    meta: RouteMeta,
    stop_idx: int,
) -> tuple[str, Optional[float], Optional[float]]:
    if stop_idx < 0 or stop_idx >= len(meta.stop_node_ids):
        return ("(no prior stop)", None, None)
    name = meta.stop_names[stop_idx] or f"stop_{stop_idx}"
    return (name, meta.stop_lat[stop_idx], meta.stop_lon[stop_idx])


# ----------------------------------------------------------------------
# Markdown rendering
# ----------------------------------------------------------------------

HEADER_INSTRUCTIONS = (
    "You are helping fill in stop coverage gaps for bus routes in Ecuador. "
    "You have web search enabled. Your job: find real paradas (formal or "
    "informal) where buses stop in each specific tramo described below.\n\n"
    "Before answering individual queries, build context with 2-3 web searches:\n"
    "1. Facebook pages of cooperativas listed above\n"
    "2. Blogs about transporte público in the primary cantons\n"
    "3. Google Maps reviews mentioning \"parada\" + the named corridors\n"
    "4. Ecuadorian news (El Comercio, Últimas Noticias, El Universo) about "
    "public transport in this sector\n\n"
    "Then process each query. If a landmark from Q_X also applies to Q_Y "
    "(same corridor, overlapping area), you may reference \"same as Q_X\"."
)

RESPONSE_FORMAT_BLOCK = """## Response format (parser-strict — do not deviate)

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

No prose outside these blocks. Up to 5 landmarks per query. Coords WGS84 decimal."""


def _zone_distribution(queries: list[dict[str, Any]]) -> dict[str, int]:
    dist = {"urban_dense": 0, "urban_peripheral": 0, "rural": 0, "interprovincial": 0}
    for q in queries:
        z = q.get("zone") or "rural"
        if z in dist:
            dist[z] += 1
        else:
            dist.setdefault(z, 0)
            dist[z] += 1
    return dist


def _render_batch(
    *,
    batch_number: int | str,
    batch_title: str,
    cluster: dict[str, Any],
    queries_enriched: list[dict[str, Any]],
    generated_iso: str,
    is_pilot: bool = False,
) -> str:
    zone_dist = _zone_distribution(queries_enriched)
    gap_values = [q["gap_m"] for q in queries_enriched if q.get("gap_m")]
    gmin = int(min(gap_values)) if gap_values else 0
    gmax = int(max(gap_values)) if gap_values else 0
    unique_routes = len({q["route_code"] for q in queries_enriched})

    bbox = cluster.get("bbox") or [0.0, 0.0, 0.0, 0.0]
    cantons = cluster.get("cantons") or []
    cooperativas = cluster.get("cooperativas") or []

    corridor_set: set[str] = set()
    for q in queries_enriched:
        for street in q.get("named_streets") or []:
            if street:
                corridor_set.add(street)
    corridors = sorted(corridor_set)[:12] or ["(not aggregated)"]

    header = f"# DR Batch {batch_number:02d}" if isinstance(batch_number, int) else f"# DR Batch {batch_number}"
    if is_pilot:
        header = "# DR Batch 00 — pilot"
    else:
        header += f" — {batch_title}"

    zone_line = ", ".join(f"{k}={v}" for k, v in zone_dist.items())
    parts: list[str] = []
    parts.append(header)
    parts.append("")
    parts.append("**Type:** Stop Coverage Gap Filling (DR Type 2)")
    parts.append(f"**Generated:** {generated_iso}")
    parts.append(f"**Total queries:** {len(queries_enriched)}")
    parts.append(f"**Unique routes:** {unique_routes}")
    parts.append(f"**Zone distribution:** {zone_line}")
    parts.append(f"**Gap length range:** {gmin}m — {gmax}m")
    parts.append("")
    parts.append("## Geographic scope")
    parts.append("")
    cantons_str = ", ".join(cantons) if cantons else "(mixed / unknown)"
    parts.append(f"- Primary canton(s): {cantons_str}")
    parts.append(
        f"- Bounding box: [{bbox[0]:.4f}, {bbox[1]:.4f}, {bbox[2]:.4f}, {bbox[3]:.4f}]"
    )
    parts.append(f"- Main corridors: {', '.join(corridors)}")
    coops_str = ", ".join(cooperativas) if cooperativas else "(unknown)"
    parts.append(f"- Cooperativas: {coops_str}")
    parts.append("")
    parts.append("## Instructions for responder (Claude with web search)")
    parts.append("")
    parts.append(HEADER_INSTRUCTIONS)
    parts.append("")
    parts.append(RESPONSE_FORMAT_BLOCK)
    parts.append("")
    parts.append("## Queries")
    parts.append("")

    for q in queries_enriched:
        parts.append(f"### {q['qid']}")
        parts.append(f"- Route code: {q['route_code']}")
        parts.append(f"- Route name: {q['route_name']}")
        parts.append(f"- Operator/cooperativa: {q.get('cooperativa') or '(unknown)'}")
        canton_label = q.get("canton") or "(unknown)"
        province = q.get("province") or "unknown"
        parts.append(f"- Canton: {canton_label}, {province}")
        parts.append(f"- Gap number in this route: {q['gap_number']}")
        parts.append(f"- Gap length: {int(q['gap_m'])}m")
        prev_name = q.get("prev_name", "(no prior stop)")
        prev_lat, prev_lon = q.get("prev_lat"), q.get("prev_lon")
        next_name = q.get("next_name", "(no next stop)")
        next_lat, next_lon = q.get("next_lat"), q.get("next_lon")
        if prev_lat is not None and prev_lon is not None:
            parts.append(f"- Stop before gap: \"{prev_name}\" at {prev_lat:.4f}, {prev_lon:.4f}")
        else:
            parts.append(f"- Stop before gap: \"{prev_name}\"")
        if next_lat is not None and next_lon is not None:
            parts.append(f"- Stop after gap: \"{next_name}\" at {next_lat:.4f}, {next_lon:.4f}")
        else:
            parts.append(f"- Stop after gap: \"{next_name}\"")
        streets = q.get("named_streets") or []
        streets_str = ", ".join(streets) if streets else "(not aggregated)"
        parts.append(f"- Named streets in gap segment: {streets_str}")
        parts.append(f"- Gap midpoint: {q['midpoint_lat']:.4f}, {q['midpoint_lon']:.4f}")
        parts.append(f"- Zone type: {q.get('zone') or 'unknown'}")
        parts.append("")
        parts.append("Find 1-3 real landmarks where buses actually stop in this specific tramo.")
        parts.append("")
        parts.append("---")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def _enrich_queries(
    cluster: dict[str, Any],
    diag_idx: dict[str, dict[str, Any]],
    route_metas: dict[str, RouteMeta],
) -> list[dict[str, Any]]:
    """Build the full per-query payload ready for rendering."""
    enriched: list[dict[str, Any]] = []
    pointers = cluster["query_pointers"]
    pointers_sorted = sorted(pointers, key=lambda p: (p["route_code"], p["gap_idx"]))

    for ptr in pointers_sorted:
        rc = ptr["route_code"]
        gidx = int(ptr["gap_idx"])
        meta = route_metas.get(rc)
        detail = _collect_gap_detail(rc, gidx, diag_idx)
        if meta is None or detail is None:
            continue
        prev_name, prev_lat, prev_lon = _fmt_stop(meta, detail.prev_stop_idx)
        next_name, next_lat, next_lon = _fmt_stop(meta, detail.next_stop_idx)
        enriched.append(
            {
                "qid": ptr["qid"],
                "route_code": rc,
                "route_name": meta.route_name,
                "cooperativa": meta.cooperativa,
                "canton": meta.canton,
                "province": meta.province,
                "gap_number": gidx + 1,
                "gap_m": detail.gap_m,
                "zone": detail.zone,
                "midpoint_lat": detail.midpoint_lat,
                "midpoint_lon": detail.midpoint_lon,
                "prev_name": prev_name,
                "prev_lat": prev_lat,
                "prev_lon": prev_lon,
                "next_name": next_name,
                "next_lat": next_lat,
                "next_lon": next_lon,
                "named_streets": [],
            }
        )
    return enriched


def _renumber_pilot(enriched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, q in enumerate(enriched, 1):
        q2 = dict(q)
        q2["qid"] = f"Q{i:03d}"
        out.append(q2)
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    plan = _load_plan()
    diag_idx = _load_diagnostic()

    route_codes: set[str] = set()
    for cluster in plan["clusters"]:
        for ptr in cluster["query_pointers"]:
            route_codes.add(ptr["route_code"])
    print(f"[exporter] resolving metadata for {len(route_codes)} distinct routes")
    route_metas = _fetch_route_meta(sorted(route_codes), diag_idx)

    generated_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary: list[dict[str, Any]] = []
    all_enriched_by_cluster: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    for cluster in plan["clusters"]:
        enriched = _enrich_queries(cluster, diag_idx, route_metas)
        all_enriched_by_cluster.append((cluster, enriched))

    # ------------------------------------------------------------------
    # Production batches — number sequentially 01..NN in plan order
    # ------------------------------------------------------------------
    for prod_idx, (cluster, enriched) in enumerate(all_enriched_by_cluster, 1):
        if not enriched:
            print(f"[exporter] cluster {cluster.get('batch_name')}: empty after enrichment, skipped")
            continue
        matched_seed = cluster.get("matched_seed")
        original_title = cluster.get("batch_name", "").split("_", 2)
        title = matched_seed or (original_title[-1] if len(original_title) >= 3 else f"cluster_{cluster['cluster_id']}")
        md = _render_batch(
            batch_number=prod_idx,
            batch_title=title,
            cluster=cluster,
            queries_enriched=enriched,
            generated_iso=generated_iso,
            is_pilot=False,
        )
        fname = f"batch_{prod_idx:02d}_{title}.md"
        (OUT_DIR / fname).write_text(md)
        summary.append(
            {
                "batch_number": prod_idx,
                "file": fname,
                "cluster_id": cluster["cluster_id"],
                "matched_seed": matched_seed,
                "query_count": len(enriched),
            }
        )
        print(f"[exporter] wrote {fname} ({len(enriched)} queries)")

    # ------------------------------------------------------------------
    # Pilot batch — 1-2 queries sampled from every production batch,
    # capped near PILOT_TARGET_TOTAL.
    # ------------------------------------------------------------------
    pilot_enriched: list[dict[str, Any]] = []
    nonempty = [(c, e) for c, e in all_enriched_by_cluster if e]
    if nonempty:
        quota = max(1, min(PILOT_PER_CLUSTER, PILOT_TARGET_TOTAL // max(1, len(nonempty))))
        for _, enriched in nonempty:
            step = max(1, len(enriched) // max(1, quota + 1))
            picks: list[dict[str, Any]] = []
            for i in range(quota):
                idx = min(len(enriched) - 1, (i + 1) * step - 1)
                picks.append(enriched[idx])
            pilot_enriched.extend(picks)
        pilot_enriched = pilot_enriched[:PILOT_TARGET_TOTAL]
        pilot_enriched = _renumber_pilot(pilot_enriched)
        pilot_cluster = {
            "bbox": [
                min(q["midpoint_lat"] for q in pilot_enriched),
                min(q["midpoint_lon"] for q in pilot_enriched),
                max(q["midpoint_lat"] for q in pilot_enriched),
                max(q["midpoint_lon"] for q in pilot_enriched),
            ],
            "cantons": sorted({q["canton"] for q in pilot_enriched if q.get("canton")})[:8],
            "cooperativas": sorted({q["cooperativa"] for q in pilot_enriched if q.get("cooperativa")})[:10],
        }
        md = _render_batch(
            batch_number=0,
            batch_title="pilot",
            cluster=pilot_cluster,
            queries_enriched=pilot_enriched,
            generated_iso=generated_iso,
            is_pilot=True,
        )
        (OUT_DIR / "batch_00_pilot.md").write_text(md)
        print(f"[exporter] wrote batch_00_pilot.md ({len(pilot_enriched)} curated queries)")

    # ------------------------------------------------------------------
    # Summary manifest — useful for progress regen later.
    # ------------------------------------------------------------------
    manifest = {
        "generated": generated_iso,
        "plan_method": plan.get("method"),
        "plan_k": plan.get("k"),
        "total_queries": sum(s["query_count"] for s in summary),
        "pilot_query_count": len(pilot_enriched),
        "batches": summary,
    }
    (OUT_DIR.parent / "_export_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(
        f"[exporter] DONE — {len(summary)} production batches + 1 pilot "
        f"({manifest['total_queries']} production queries total)"
    )


if __name__ == "__main__":
    main()
