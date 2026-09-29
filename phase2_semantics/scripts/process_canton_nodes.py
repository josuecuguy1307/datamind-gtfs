"""
Process all new nodes from a specific canton through the Phase 2 pipeline.
Called by the automation pipeline after Phase 1 extraction.

This is a canton-scoped orchestrator that runs the full Phase 2 pipeline
(evidence -> geo-context -> candidates -> names -> contextual names -> approve -> embeddings)
for nodes promoted after a given date within a canton's bounding box.

Usage:
    cd phase2_semantics
    python -m scripts.process_canton_nodes --canton cayambe --from-date 2026-03-20
    python -m scripts.process_canton_nodes --canton cayambe --from-date 2026-03-20 --dry-run
    python -m scripts.process_canton_nodes --bbox "-78.20,-0.10,-78.00,0.10" --from-date 2026-03-20
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional

from src.db.conn import db_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("phase2.process_canton")

PHASE2_ROOT = Path(__file__).resolve().parents[1]

# Known canton bounding boxes (west, south, east, north)
CANTON_BBOXES: Dict[str, str] = {
    "quito": "-78.65,-0.50,-78.30,0.10",
    "ruminahui": "-78.55,-0.42,-78.40,-0.28",
    "mejia": "-78.70,-0.65,-78.40,-0.35",
    "cayambe": "-78.32,-0.14,-77.82,0.18",
    "pedro_moncayo": "-78.30,0.00,-78.05,0.18",
    "san_miguel_de_los_bancos": "-79.30,-0.15,-78.80,0.20",
    "pedro_vicente_maldonado": "-79.20,0.00,-79.00,0.18",
    "puerto_quito": "-79.40,0.05,-79.10,0.28",
}


@dataclass
class CantonReport:
    canton: str
    bbox: str
    from_date: str
    nodes_found: int = 0
    evidence_rows: int = 0
    geo_context_backfilled: int = 0
    candidates_created: int = 0
    names_generated: int = 0
    contextual_names_applied: int = 0
    places_approved: int = 0
    embeddings_created: int = 0
    errors: List[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []

    def summary(self) -> str:
        lines = [
            f"\n{'='*60}",
            f"  Phase 2 Canton Processing Report",
            f"{'='*60}",
            f"  Canton:     {self.canton}",
            f"  BBox:       {self.bbox}",
            f"  From date:  {self.from_date}",
            f"  Nodes:      {self.nodes_found}",
            f"  Evidence:   {self.evidence_rows} rows",
            f"  Geo-ctx:    {self.geo_context_backfilled} backfilled",
            f"  Candidates: {self.candidates_created}",
            f"  Names:      {self.names_generated}",
            f"  Contextual: {self.contextual_names_applied}",
            f"  Approved:   {self.places_approved}",
            f"  Embeddings: {self.embeddings_created}",
        ]
        if self.errors:
            lines.append(f"  ERRORS:     {len(self.errors)}")
            for e in self.errors:
                lines.append(f"    - {e}")
        lines.append(f"{'='*60}\n")
        return "\n".join(lines)


def _fetch_new_nodes(conn, bbox: str, from_date: str) -> List[Dict[str, Any]]:
    """Fetch nodes from node_prod promoted after from_date within bbox."""
    west, south, east, north = [float(x.strip()) for x in bbox.split(",")]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT node_id, node_type, geom, chosen_tags, confidence
            FROM node_prod.nodes
            WHERE geom IS NOT NULL
              AND ST_Within(geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
              AND approved_at >= %s::timestamptz
            ORDER BY approved_at
            """,
            (west, south, east, north, from_date),
        )
        return [dict(row) for row in cur.fetchall()]


def _step_10_extract_evidence(conn, nodes: List[Dict], report: CantonReport) -> str:
    """Extract semantic evidence for canton nodes. Returns extract_run_id."""
    from src.pipeline.extract.create_extract_run import create_extract_run
    from src.pipeline.extract.evidence_rules import collect_evidence
    from src.pipeline.extract.extract_name_evidence import extract_name_evidence
    from src.db.geo_raw_repo import insert_name_evidence_batch
    from src.settings import GEO_CONTEXT_KEY

    run = create_extract_run(
        context_key=f"canton_{report.canton}",
        source_node_set_id=None,
    )
    extract_run_id = run["extract_run_id"]

    batch = []
    for node in nodes:
        tags = node.get("chosen_tags") or {}
        tags_str = {str(k): str(v) for k, v in tags.items() if v is not None}

        for ev in collect_evidence(tags_str):
            batch.append({
                "extract_run_id": extract_run_id,
                "node_id": str(node["node_id"]),
                "source": ev.source,
                "raw_text": f"tag_signal:{ev.source}",
                "lang": None,
                "weight_hint": float(ev.weight),
                "tags_snapshot": tags,
            })

        name = tags_str.get("name")
        if name:
            for ev in extract_name_evidence(name):
                batch.append({
                    "extract_run_id": extract_run_id,
                    "node_id": str(node["node_id"]),
                    "source": ev.source,
                    "raw_text": name,
                    "lang": None,
                    "weight_hint": float(ev.weight),
                    "tags_snapshot": tags,
                })

    if batch:
        insert_name_evidence_batch(conn, batch)
        conn.commit()

    report.evidence_rows = len(batch)
    logger.info("Step 10: %d evidence rows for %d nodes", len(batch), len(nodes))
    return extract_run_id


def _step_15_geo_context(
    conn,
    nodes: List[Dict],
    report: CantonReport,
    extract_run_id: Optional[str],
    context_key: str,
) -> None:
    """Build geo-context for new nodes that don't already have it.

    Mirrors the canonical phase2_semantics/scripts/15_build_geo_context.py:
    - PK on geo_work.node_geo_context is (extract_run_id, node_id), so both
      columns are required AND must appear in the ON CONFLICT clause.
    - context_key namespaces the geo context per province (e.g. sample_v1,
      guayas_v1) and should be set explicitly per Skill 11.
    """
    if not nodes:
        return
    if not extract_run_id:
        logger.warning("Step 15: skipped — no extract_run_id from Step 10")
        return

    node_ids = [str(n["node_id"]) for n in nodes]

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO geo_work.node_geo_context
                (extract_run_id, context_key, node_id,
                 lat, lon, geohash7,
                 transit_density_300m, poi_density_300m,
                 tag_stop_weight, tag_poi_weight, features)
            SELECT
                %s::uuid,
                %s,
                n.node_id,
                ST_Y(n.geom)::float8,
                ST_X(n.geom)::float8,
                ST_GeoHash(n.geom, 7),
                (SELECT COUNT(*)::int FROM node_prod.nodes n2
                  WHERE n2.node_type = 'STOP' AND n2.node_id != n.node_id
                    AND ST_DWithin(n.geom::geography, n2.geom::geography, 300)
                ) AS transit_density_300m,
                (SELECT COUNT(*)::int FROM node_prod.nodes n2
                  WHERE n2.node_type = 'POI' AND n2.node_id != n.node_id
                    AND ST_DWithin(n.geom::geography, n2.geom::geography, 300)
                ) AS poi_density_300m,
                0::float8,
                0::float8,
                jsonb_build_object(
                    'context_key', %s,
                    'extract_run_id', %s
                )
            FROM node_prod.nodes n
            WHERE n.node_id = ANY(%s::uuid[])
              AND n.geom IS NOT NULL
            ON CONFLICT (extract_run_id, node_id) DO NOTHING
            """,
            (extract_run_id, context_key, context_key, extract_run_id, node_ids),
        )
        inserted = cur.rowcount

    conn.commit()
    report.geo_context_backfilled = inserted
    logger.info(
        "Step 15: backfilled %d / %d nodes (extract_run=%s, context=%s)",
        inserted, len(node_ids), extract_run_id, context_key,
    )


def _run_subprocess_step(script_name: str, extra_args: Optional[List[str]] = None) -> None:
    """Run a pipeline step as a subprocess."""
    cmd = [sys.executable, str(PHASE2_ROOT / "scripts" / script_name)]
    if extra_args:
        cmd.extend(extra_args)

    logger.info("Running: %s", " ".join(cmd))
    subprocess.run(
        cmd,
        cwd=str(PHASE2_ROOT),
        env={**__import__("os").environ, "PYTHONPATH": str(PHASE2_ROOT)},
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description="Process canton nodes through Phase 2")
    parser.add_argument("--canton", type=str, help="Canton name (e.g., cayambe)")
    parser.add_argument("--bbox", type=str, help="Bounding box: west,south,east,north")
    parser.add_argument("--from-date", type=str, required=True, help="Process nodes created after this date (ISO)")
    parser.add_argument("--dry-run", action="store_true", help="Only count nodes, don't process")
    parser.add_argument("--skip-embeddings", action="store_true", help="Skip embedding generation")
    args = parser.parse_args()

    if not args.canton and not args.bbox:
        parser.error("Either --canton or --bbox is required")

    canton = args.canton or "custom"
    bbox = args.bbox or CANTON_BBOXES.get(args.canton)
    if not bbox:
        available = ", ".join(sorted(CANTON_BBOXES.keys()))
        parser.error(f"Unknown canton '{args.canton}'. Available: {available}. Or use --bbox.")

    report = CantonReport(canton=canton, bbox=bbox, from_date=args.from_date)
    t0 = perf_counter()

    logger.info("Processing canton=%s bbox=%s from_date=%s", canton, bbox, args.from_date)

    with db_conn() as conn:
        # 0. Discover nodes
        nodes = _fetch_new_nodes(conn, bbox, args.from_date)
        report.nodes_found = len(nodes)
        logger.info("Found %d nodes in canton %s after %s", len(nodes), canton, args.from_date)

        if not nodes:
            print(f"\nNo new nodes found in canton {canton} after {args.from_date}.")
            return

        if args.dry_run:
            print(f"\n[DRY RUN] Would process {len(nodes)} nodes in canton {canton}")
            print(f"  Node types: { {t: sum(1 for n in nodes if n.get('node_type') == t) for t in set(n.get('node_type') for n in nodes)} }")
            return

        # 1. Step 10: Extract evidence
        try:
            extract_run_id = _step_10_extract_evidence(conn, nodes, report)
        except Exception as e:
            report.errors.append(f"Step 10 failed: {e}")
            logger.error("Step 10 failed: %s", e)
            extract_run_id = None

        # 2. Step 15: Geo-context
        try:
            from src.settings import GEO_CONTEXT_KEY  # noqa: WPS433 — runtime import keeps top of file untouched
            _step_15_geo_context(conn, nodes, report, extract_run_id, GEO_CONTEXT_KEY)
        except Exception as e:
            report.errors.append(f"Step 15 failed: {e}")
            logger.error("Step 15 failed: %s", e)

    # Steps 20-40 run as subprocesses (they manage their own DB connections)
    # Step 20: Build candidates
    try:
        _run_subprocess_step("20_build_candidates.py")
        report.candidates_created = report.nodes_found  # approximate
    except Exception as e:
        report.errors.append(f"Step 20 failed: {e}")
        logger.error("Step 20 failed: %s", e)

    # Step 25: Name candidates
    try:
        _run_subprocess_step("25_build_name_candidates.py")
        report.names_generated = report.nodes_found  # approximate
    except Exception as e:
        report.errors.append(f"Step 25 failed: {e}")
        logger.error("Step 25 failed: %s", e)

    # Step 26: Contextual names
    try:
        _run_subprocess_step("26_contextual_names.py", ["--apply", "--threshold", "50"])
    except Exception as e:
        report.errors.append(f"Step 26 failed: {e}")
        logger.error("Step 26 failed: %s", e)

    # Step 30: Approve
    try:
        _run_subprocess_step("30_approve.py")
        report.places_approved = report.nodes_found  # approximate
    except Exception as e:
        report.errors.append(f"Step 30 failed: {e}")
        logger.error("Step 30 failed: %s", e)

    # Step 40: Embeddings
    if not args.skip_embeddings:
        try:
            _run_subprocess_step("40_build_embeddings.py")
            report.embeddings_created = report.nodes_found  # approximate
        except Exception as e:
            report.errors.append(f"Step 40 failed: {e}")
            logger.error("Step 40 failed: %s", e)

    elapsed = perf_counter() - t0
    print(report.summary())
    print(f"Total elapsed: {elapsed:.1f}s")

    if report.errors:
        logger.warning("%d errors during processing", len(report.errors))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
