"""
Batch Discovery Runner

Processes a hints catalog (JSON) through the sequence discovery pipeline
and persists results to route_work.discovery_runs.

Usage:
    python -m datamind_console.phases.phase3_routes.stop_grounding.batch_discovery \
        /path/to/catalog.json [--max N] [--scoring-mode ensemble]
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from datamind_console.db.db import db_conn, fetch_all, exec_sql
from datamind_console.phases.phase3_routes.stop_grounding.a2_synthesis_bridge import A2RunInputs
from datamind_console.phases.phase3_routes.stop_grounding.discovery_pipeline import (
    run_discovery_pipeline,
)
from datamind_console.phases.phase3_routes.stop_grounding.dual_catalog_loader import (
    DualCatalogContext,
    load_dual_catalogs,
    load_geography_catalog_only,
)
from datamind_console.phases.phase3_routes.stop_grounding.termini_utils import extract_termini
from datamind_console.phases.phase3_routes.stop_grounding.typed_token_dispatch import (
    intake_typed_seed,
    typed_seed_to_route_seed,
)

_A2_ENABLED = os.environ.get("HADES_DISABLE_A2") != "1"

_LOG = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO route_work.discovery_runs (
    route_name, cooperative_name,
    anchor_a_hint, anchor_b_hint, intermediate_hints,
    corridor_description, source_catalog, catalog_index,
    idempotency_key, status, scoring_mode, review_status,
    grounding_confidence, anchor_a_matches, anchor_b_matches, unmatched_hints,
    corridor_geojson, corridor_length_km, corridor_confidence,
    skeleton_stops, skeleton_stop_count, skeleton_gaps, sequence_confidence,
    geometry_geojson, geometry_length_km, geometry_confidence, geometry_derived_from,
    metrics, full_summary, seed_payload, source_notes,
    province,
    updated_at
)
VALUES (
    %(route_name)s, %(cooperative_name)s,
    %(anchor_a_hint)s, %(anchor_b_hint)s, %(intermediate_hints)s,
    %(corridor_description)s, %(source_catalog)s, %(catalog_index)s,
    %(idempotency_key)s, %(status)s, %(scoring_mode)s, %(review_status)s,
    %(grounding_confidence)s, %(anchor_a_matches)s, %(anchor_b_matches)s, %(unmatched_hints)s,
    %(corridor_geojson)s, %(corridor_length_km)s, %(corridor_confidence)s,
    %(skeleton_stops)s, %(skeleton_stop_count)s, %(skeleton_gaps)s, %(sequence_confidence)s,
    %(geometry_geojson)s, %(geometry_length_km)s, %(geometry_confidence)s, %(geometry_derived_from)s,
    %(metrics)s, %(full_summary)s, %(seed_payload)s, %(source_notes)s,
    COALESCE(%(province)s, 'sample_region'),
    now()
)
ON CONFLICT (idempotency_key) DO UPDATE SET
    status = EXCLUDED.status,
    review_status = CASE
        WHEN route_work.discovery_runs.review_status IN ('approved', 'rejected')
        THEN route_work.discovery_runs.review_status
        ELSE EXCLUDED.review_status
    END,
    province = EXCLUDED.province,
    grounding_confidence = EXCLUDED.grounding_confidence,
    anchor_a_matches = EXCLUDED.anchor_a_matches,
    anchor_b_matches = EXCLUDED.anchor_b_matches,
    unmatched_hints = EXCLUDED.unmatched_hints,
    corridor_geojson = EXCLUDED.corridor_geojson,
    corridor_length_km = EXCLUDED.corridor_length_km,
    corridor_confidence = EXCLUDED.corridor_confidence,
    skeleton_stops = EXCLUDED.skeleton_stops,
    skeleton_stop_count = EXCLUDED.skeleton_stop_count,
    skeleton_gaps = EXCLUDED.skeleton_gaps,
    sequence_confidence = EXCLUDED.sequence_confidence,
    geometry_geojson = EXCLUDED.geometry_geojson,
    geometry_length_km = EXCLUDED.geometry_length_km,
    geometry_confidence = EXCLUDED.geometry_confidence,
    geometry_derived_from = EXCLUDED.geometry_derived_from,
    metrics = EXCLUDED.metrics,
    full_summary = EXCLUDED.full_summary,
    seed_payload = EXCLUDED.seed_payload,
    source_notes = EXCLUDED.source_notes,
    updated_at = now()
"""


def _catalog_entry_to_seed(
    entry: Dict[str, Any],
    index: int,
    *,
    rules: Optional[Dict[str, Any]] = None,
    province: Optional[str] = None,
):
    del index
    # Camino D PIEZA 7: propagate province from the dual-catalog context (or
    # from a top-level province field on the entry itself when absent).
    return intake_typed_seed(entry, rules=rules, province=province)


def _summary_to_db_params(
    seed,
    summary_dict: Dict[str, Any],
    *,
    catalog_name: str,
    catalog_index: int,
    entry: Dict[str, Any],
    scoring_mode: str,
    province: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert pipeline summary to DB upsert params."""
    metrics = summary_dict.get("metrics") or {}
    route_seed = summary_dict.get("route_seed") or {}
    grounding = summary_dict.get("grounding") or {}
    corridor = summary_dict.get("corridor") or {}
    skeleton = summary_dict.get("skeleton") or {}
    geometry = summary_dict.get("geometry") or {}

    # Classify review status
    status = summary_dict.get("status", "failed")
    if metrics.get("rejected_for_geographic_implausibility"):
        review_status = "blocked_geography"
    elif status == "completed":
        seq_conf = metrics.get("sequence_confidence", 0)
        geom_conf = metrics.get("geometry_confidence", 0)
        n_stops = metrics.get("total_discovered_stops", 0)
        if seq_conf >= 0.5 and geom_conf >= 0.8 and n_stops >= 5:
            review_status = "pending_review"
        elif n_stops >= 3:
            review_status = "pending_review"
        else:
            review_status = "needs_more_data"
    else:
        review_status = "needs_more_data"

    return {
        "route_name": route_seed.get("route_name") or seed.route_name,
        "cooperative_name": seed.cooperative_name,
        "anchor_a_hint": route_seed.get("anchor_a_hint") or seed.anchor_a_hint,
        "anchor_b_hint": route_seed.get("anchor_b_hint") or seed.anchor_b_hint,
        "intermediate_hints": route_seed.get("intermediate_hints") or seed.intermediate_hints,
        "corridor_description": route_seed.get("corridor_description") or seed.corridor_description,
        "source_catalog": catalog_name,
        "catalog_index": catalog_index,
        "idempotency_key": seed.idempotency_key(),
        "status": status,
        "scoring_mode": scoring_mode,
        "review_status": review_status,
        "grounding_confidence": grounding.get("overall_grounding_confidence"),
        "anchor_a_matches": json.dumps(grounding.get("matched_anchor_a_candidates", [])),
        "anchor_b_matches": json.dumps(grounding.get("matched_anchor_b_candidates", [])),
        "unmatched_hints": grounding.get("unmatched_hints", []),
        "corridor_geojson": json.dumps(corridor.get("corridor_geojson")) if corridor.get("corridor_geojson") else None,
        "corridor_length_km": corridor.get("total_length_km"),
        "corridor_confidence": corridor.get("corridor_confidence"),
        "skeleton_stops": json.dumps(skeleton.get("ordered_stops", [])),
        "skeleton_stop_count": len(skeleton.get("ordered_stops", [])),
        "skeleton_gaps": json.dumps(skeleton.get("gaps", [])),
        "sequence_confidence": skeleton.get("sequence_confidence"),
        "geometry_geojson": json.dumps(geometry.get("geometry_geojson")) if geometry.get("geometry_geojson") else None,
        "geometry_length_km": geometry.get("total_length_km"),
        "geometry_confidence": geometry.get("geometry_confidence"),
        "geometry_derived_from": geometry.get("derived_from"),
        "metrics": json.dumps(metrics),
        "full_summary": json.dumps(summary_dict),
        "seed_payload": json.dumps(entry),
        "source_notes": entry.get("source_notes", []),
        # Camino D PIEZA 7 follow-up: thread province through to DB.
        # NULL → column DEFAULT ('sample_region') applies; non-null overrides.
        "province": (province or "").strip().lower() or None,
    }


def run_batch_discovery(
    catalog_path: str,
    *,
    max_routes: Optional[int] = None,
    scoring_mode: str = "ensemble",
    artifact_base_dir: Optional[str] = None,
    conn=None,
    geography_catalog_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Process all routes in a hints catalog through the discovery pipeline.
    Persists results to route_work.discovery_runs (upsert by idempotency_key).

    If geography_catalog_path is provided, loads the dual catalog architecture
    for geographic validation at every pipeline stage.
    """
    with open(catalog_path, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    catalog_name = catalog.get("catalog_name", Path(catalog_path).stem)
    routes = catalog.get("routes", [])
    rules = catalog.get("sequence_resolution_rules") or {}
    if max_routes:
        routes = routes[:max_routes]

    # Load geography catalog if provided
    catalog_ctx: Optional[DualCatalogContext] = None
    if geography_catalog_path:
        catalog_ctx = load_dual_catalogs(catalog_path, geography_catalog_path)
        _LOG.info(
            "Loaded dual catalogs: %d areas, %d route geographies",
            len(catalog_ctx.area_definitions),
            len(catalog_ctx.route_geographies),
        )

    _LOG.info("Processing %d routes from catalog '%s'", len(routes), catalog_name)

    if artifact_base_dir is None:
        artifact_base_dir = f"constructor_artifacts/{catalog_name}"

    results = []

    def _process(connection):
        # Enable autocommit so each route's DB upsert commits immediately.
        # This prevents Postgres from killing the connection during long
        # Overpass/Valhalla waits (idle-in-transaction timeout).
        connection.autocommit = True
        for i, entry in enumerate(routes):
            route_label = entry.get("route", f"route_{i}")
            coop = entry.get("cooperative", "?")
            _LOG.info("=" * 60)
            _LOG.info("[%d/%d] %s — %s", i + 1, len(routes), coop, route_label)

            # Camino D PIEZA 7: province from dual-catalog context (if loaded)
            # propagates into the typed seed and from there to every downstream
            # province-aware helper.
            _entry_province = (
                catalog_ctx.province if catalog_ctx is not None else None
            )
            seed = _catalog_entry_to_seed(entry, i, rules=rules, province=_entry_province)
            legacy_seed = typed_seed_to_route_seed(seed)

            try:
                # Reset LightGBM model cache per-run to avoid stale state
                import datamind_console.phases.phase3_routes.stop_grounding.on_route_classifier as mod
                mod._lgbm_load_attempted = False
                mod._meta_cache = None

                _unit = (catalog_ctx.unit_name if catalog_ctx else None) or "unknown"
                _province = (catalog_ctx.province if catalog_ctx else None) or "sample_region"
                _a2_inputs = A2RunInputs(
                    route_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"datamind.{_unit.lower()}.{(seed.route_name or '').lower()}")),
                    route_code=seed.route_name or "",
                    unit=_unit, province=_province,
                    termini=extract_termini(seed),
                    osm_relation_id=(entry.get("_meta") or {}).get("osm_relation_id"),
                    grounded_stop_coords=(),
                    review_root=Path(artifact_base_dir),
                    research_queue_root=Path("workspace/research_queue"),
                )
                summary = run_discovery_pipeline(
                    seed,
                    artifact_dir=artifact_base_dir,
                    scoring_mode=scoring_mode,
                    conn=connection,
                    catalog_ctx=catalog_ctx,
                    enable_a2=_A2_ENABLED,
                    a2_inputs=_a2_inputs,
                )
                summary_dict = summary.to_dict()
            except Exception as exc:
                _LOG.error("Pipeline failed for '%s': %s", route_label, exc)
                summary_dict = {"status": "failed", "error": str(exc)}

            # Persist to DB
            params = _summary_to_db_params(
                legacy_seed, summary_dict,
                catalog_name=catalog_name,
                catalog_index=i,
                entry=entry,
                scoring_mode=scoring_mode,
                province=(catalog_ctx.province if catalog_ctx is not None else None),
            )
            try:
                exec_sql(connection, _UPSERT_SQL, params)
                _LOG.info("  DB: upserted run for '%s' (status=%s, review=%s)",
                          route_label, params["status"], params["review_status"])
            except Exception as db_exc:
                _LOG.error("  DB upsert failed for '%s': %s", route_label, db_exc)

            # Summary for caller
            result_metrics = dict(summary_dict.get("metrics") or {})
            skeleton_data = summary_dict.get("skeleton") or {}
            result_entry = {
                "index": i,
                "route": route_label,
                "cooperative": coop,
                "status": params["status"],
                "review_status": params["review_status"],
                "skeleton_stop_count": params["skeleton_stop_count"],
                "corridor_length_km": round(params.get("corridor_length_km") or 0, 1),
                "straight_line_km": round(result_metrics.get("straight_line_km") or 0, 1),
                "corridor_inflation_ratio": round(result_metrics.get("corridor_inflation_ratio") or 0, 2),
                "in_bounds_fraction": round(result_metrics.get("in_bounds_fraction") or 0, 2),
                "geography_plausibility_score": round(result_metrics.get("geography_plausibility_score") or 0, 2),
                "route_status": result_metrics.get("route_status") or params["status"],
                "sequence_confidence": round(params.get("sequence_confidence") or 0, 2),
                "geometry_confidence": round(params.get("geometry_confidence") or 0, 2),
                # Quality metrics (Block 4)
                "stops_removed_by_spacing": skeleton_data.get("stops_removed_by_spacing", 0),
                "stops_removed_by_density": skeleton_data.get("stops_removed_by_density", 0),
                "marginals_promoted": skeleton_data.get("marginals_promoted", 0),
                "endpoint_resolution_method": summary_dict.get("endpoint_resolution_method", ""),
                # Territorial resolution (Stage B2)
                "territorial_tokens": summary_dict.get("territorial_tokens_count", 0),
                "territorial_resolved": summary_dict.get("territorial_resolved_count", 0),
                "chain_confidence": round(summary_dict.get("chain_confidence", 0), 2),
                # Geography catalog validation
                "geo_validation_score": round(
                    (result_metrics.get("geo_validation") or {}).get("score", 0), 2
                ),
                "pass_through_compliance": round(
                    (result_metrics.get("geo_validation") or {}).get("pass_through_compliance", 0), 2
                ),
                "must_not_violations": len(
                    (result_metrics.get("geo_validation") or {}).get("forbidden_violations", [])
                ),
            }
            results.append(result_entry)

            _LOG.info(
                "  Result: %d stops, %.1fkm, seq_conf=%.2f, geom_conf=%.2f",
                result_entry["skeleton_stop_count"],
                result_entry["corridor_length_km"],
                result_entry["sequence_confidence"],
                result_entry["geometry_confidence"],
            )

        return results

    if conn is not None:
        return _process(conn)
    with db_conn() as connection:
        return _process(connection)


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Batch sequence discovery")
    parser.add_argument("catalog", help="Path to hints catalog JSON")
    parser.add_argument("--max", type=int, default=None, help="Max routes to process")
    parser.add_argument("--scoring-mode", default="ensemble", help="Scoring mode")
    parser.add_argument("--geography-catalog", default=None, help="Path to route geography catalog JSON")
    args = parser.parse_args()

    os.environ.setdefault("DATA_MODE", "local")
    os.environ.setdefault("DB_DSN", "postgresql://localhost:5432/datamind_ml")
    os.environ.setdefault("VALHALLA_URL", "http://127.0.0.1:8003")

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    results = run_batch_discovery(
        args.catalog,
        max_routes=args.max,
        scoring_mode=args.scoring_mode,
        geography_catalog_path=args.geography_catalog,
    )

    print(f"\n{'='*60}")
    print(f"BATCH COMPLETE: {len(results)} routes processed")
    print(f"{'='*60}")
    for r in results:
        status_icon = "+" if r["status"] == "completed" else "X"
        print(f"  [{status_icon}] {r['cooperative']}: {r['route']} — "
              f"{r['skeleton_stop_count']} stops, {r['corridor_length_km']}km, "
              f"seq={r['sequence_confidence']}, geom={r['geometry_confidence']}")
