"""
Sequence discovery pipeline — typed seed intake through geometry candidate derivation.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    ConstructorRunSummary,
    CorridorIntersectionResult,
    CorridorResult,
    GeometryCandidate,
    HintCandidateSet,
    RouteSeed,
    SequenceSkeleton,
    StopGroundingResult,
    TypedRouteSeed,
)
from datamind_console.phases.phase3_routes.stop_grounding.corridor_builder import (
    build_typed_corridor,
)
from datamind_console.phases.phase3_routes.stop_grounding.corridor_stop_intersector import (
    intersect_corridor_with_stops,
)
from datamind_console.phases.phase3_routes.stop_grounding.dual_catalog_loader import (
    DualCatalogContext,
    RouteContext,
)
from datamind_console.phases.phase3_routes.stop_grounding.geographic_validator import (
    validate_corridor_geography,
)
from datamind_console.phases.phase3_routes.stop_grounding.geometry_candidate_builder import (
    derive_geometry_candidate,
)
from datamind_console.phases.phase3_routes.stop_grounding.geography_guardrails import (
    derive_expected_geographic_envelope,
    normalize_seed_hints,
)
from datamind_console.phases.phase3_routes.stop_grounding.hint_set_coherence import (
    resolve_coherent_hint_set,
    serialize_hint_sets,
)
from datamind_console.phases.phase3_routes.stop_grounding.on_route_classifier import (
    infer_terminus_type,
    is_terminus_protected,
    score_candidates,
)
from datamind_console.phases.phase3_routes.stop_grounding.sequence_skeleton_builder import (
    assemble_sequence_skeleton,
)
from datamind_console.phases.phase3_routes.stop_grounding.sequence_validator import (
    validate_sequence,
)
from datamind_console.phases.phase3_routes.stop_grounding.typed_token_dispatch import (
    build_legacy_grounding,
    ground_typed_seed,
    intake_typed_seed,
    ordered_groundable_tokens,
    token_dispatch_log,
    typed_seed_to_route_seed,
)
from datamind_console.phases.phase3_routes.stop_grounding.terminus_lock import (
    apply_terminus_lock,
)
from datamind_console.phases.phase3_routes.stop_grounding.tail_relaxation import (
    apply_tail_relaxation,
    inject_synthetic_terminus_stops,
)
from datamind_console.phases.phase3_routes.stop_grounding.confidence_architecture import (
    compute_all_confidences,
    derive_route_status,
)
from datamind_console.phases.phase3_routes.stop_grounding.catalogs import get_config_section
from datamind_console.phases.phase3_routes.stop_grounding.gap_analyzer import analyze_gaps
from datamind_console.phases.phase3_routes.stop_grounding.a2_synthesis_bridge import (
    A2RunInputs,
    run_a2_for_unresolved_anchors,
)

_LOG = logging.getLogger(__name__)

_corridor_cfg = get_config_section("corridor")
MAX_CORRIDOR_ATTEMPTS = _corridor_cfg.get("max_attempts", 3)
CORRIDOR_QUALITY_THRESHOLD = _corridor_cfg.get("quality_threshold", 0.60)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_dump(obj: Any, path: Path) -> None:
    def default(value: Any) -> Any:
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if hasattr(value, "to_dict"):
            return value.to_dict()
        return str(value)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=default), encoding="utf-8")


def _artifact_dir(seed: TypedRouteSeed, base_dir: Optional[str] = None) -> Path:
    base = Path(base_dir) if base_dir else Path("constructor_artifacts")
    safe_name = (seed.route_name or "unknown").replace("/", "_").replace(" ", "_")[:80]
    coop = (seed.cooperative or "noop").replace("/", "_").replace(" ", "_")[:40]
    return base / f"{safe_name}__{coop}"


def _route_status_from_metrics(metrics: Dict[str, Any]) -> str:
    """Derive route status using BUG-014 Two-Confidence Architecture."""
    return derive_route_status(
        geometry_confidence=float(metrics.get("geometry_confidence") or 0.0),
        sequence_confidence=float(metrics.get("sequence_confidence") or 0.0),
        stop_coverage_score=float(metrics.get("stop_coverage_score") or 0.0),
        discovered_stops=int(metrics.get("total_discovered_stops") or 0),
        rejected_for_geography=bool(metrics.get("rejected_for_geographic_implausibility")),
        geo_validation=metrics.get("geo_validation"),
        geography_plausibility_score=float(metrics.get("geography_plausibility_score") or 0.0),
    )


def _build_geography_audit(
    *,
    seed: RouteSeed,
    expected_envelope: Dict[str, Any],
    metrics: Dict[str, Any],
    skeleton: SequenceSkeleton,
    geometry: GeometryCandidate,
) -> Dict[str, Any]:
    return {
        "route_name": seed.route_name,
        "expected_geographic_envelope": expected_envelope,
        "route_status": metrics.get("route_status"),
        "route_length_km": metrics.get("route_length_km", 0.0),
        "straight_line_km": metrics.get("straight_line_km", 0.0),
        "corridor_inflation_ratio": metrics.get("corridor_inflation_ratio", 0.0),
        "geography_plausibility_score": metrics.get("geography_plausibility_score", 0.0),
        "rejected_for_geographic_implausibility": metrics.get("rejected_for_geographic_implausibility", False),
        "revised_sequence_candidate": {
            "stop_count": len(skeleton.ordered_stops),
            "ordered_stop_ids": skeleton.stop_ids(),
            "ordered_stop_names": [stop.stop_name for stop in skeleton.ordered_stops],
        },
        "revised_geometry_candidate": geometry.to_dict(),
    }


def _ensure_typed_seed(seed: Union[RouteSeed, TypedRouteSeed]) -> TypedRouteSeed:
    if isinstance(seed, TypedRouteSeed):
        return seed
    sequence_seed = list(seed.sequence_seed_fragments or [])
    if not sequence_seed:
        if seed.anchor_a_hint:
            sequence_seed.append(seed.anchor_a_hint)
        sequence_seed.extend(list(seed.intermediate_hints or []))
        if seed.anchor_b_hint:
            sequence_seed.append(seed.anchor_b_hint)
    entry = {
        "route_name": seed.route_name,
        "cooperative": seed.cooperative_name or seed.operator_name,
        "sequence_seed": sequence_seed,
        "localities": list(seed.locality_hints or []),
        "sequence_confidence": "medium",
        "source_notes": list(seed.source_notes or []) if isinstance(seed.source_notes, list) else [seed.source_notes] if seed.source_notes else [],
    }
    # Camino D PIEZA 7: carry seed.province through the legacy→typed fallback.
    return intake_typed_seed(entry, province=getattr(seed, "province", None))


def _normalize_route_seed(
    seed: RouteSeed,
    *,
    catalog_ctx: Optional["DualCatalogContext"] = None,
) -> RouteSeed:
    normalization = normalize_seed_hints(
        route_name=seed.route_name,
        anchor_a_hint=seed.anchor_a_hint,
        anchor_b_hint=seed.anchor_b_hint,
        intermediate_hints=seed.intermediate_hints,
        sequence_seed_fragments=seed.sequence_seed_fragments,
    )
    seed.anchor_a_hint = str(normalization.get("anchor_a_hint") or seed.anchor_a_hint)
    seed.anchor_b_hint = str(normalization.get("anchor_b_hint") or seed.anchor_b_hint)
    seed.raw_anchor_a_hint = str(normalization.get("raw_anchor_a_hint") or seed.raw_anchor_a_hint or "")
    seed.raw_anchor_b_hint = str(normalization.get("raw_anchor_b_hint") or seed.raw_anchor_b_hint or "")
    seed.seed_normalization_notes = list(normalization.get("normalization_notes") or [])
    province = catalog_ctx.province if catalog_ctx is not None else None
    unit_name = catalog_ctx.unit_name if catalog_ctx is not None else None
    # Camino D PIEZA 7: stamp province onto the seed so downstream call sites
    # (stop_grounding_service, typed_token_dispatch, corridor_stop_intersector,
    # territorial_resolver, arterial_waypoints) can propagate it to the
    # province-aware helpers without needing catalog_ctx plumbed through.
    seed.province = province
    seed.expected_geographic_envelope = derive_expected_geographic_envelope(
        route_name=seed.route_name,
        operator_name=seed.operator_name,
        corridor_description=seed.corridor_description,
        anchor_a_hint=seed.anchor_a_hint,
        anchor_b_hint=seed.anchor_b_hint,
        intermediate_hints=seed.intermediate_hints,
        locality_hints=seed.locality_hints,
        sequence_seed_fragments=seed.sequence_seed_fragments,
        source_notes=seed.source_notes,
        sector_key=seed.sector_key,
        province=province,
        unit_name=unit_name,
    )
    return seed


def _seed_summary_dict(typed_seed: TypedRouteSeed, legacy_seed: RouteSeed) -> Dict[str, Any]:
    return {
        "route_name": typed_seed.route_name,
        "cooperative_name": typed_seed.cooperative,
        "anchor_a_hint": legacy_seed.anchor_a_hint,
        "anchor_b_hint": legacy_seed.anchor_b_hint,
        "intermediate_hints": legacy_seed.intermediate_hints,
        "corridor_description": legacy_seed.corridor_description,
        "locality_hints": legacy_seed.locality_hints,
        "sequence_seed_fragments": legacy_seed.sequence_seed_fragments,
        "sequence_tokens": [token.to_dict() for token in typed_seed.sequence_tokens],
        "corridor_constraints": [constraint.to_dict() for constraint in typed_seed.corridor_constraints],
        "expected_geographic_envelope": legacy_seed.expected_geographic_envelope,
    }


def _selected_candidates_map(seed: TypedRouteSeed, candidate_set: HintCandidateSet) -> Dict[str, Any]:
    tokens = ordered_groundable_tokens(seed)
    if not tokens:
        return {}
    mapping: Dict[str, Any] = {tokens[0].label: candidate_set.anchor_a, tokens[-1].label: candidate_set.anchor_b}
    mapping.update(candidate_set.intermediates)
    return mapping


def _build_corridor_with_feedback(
    hint_sets: List[HintCandidateSet],
    seed: TypedRouteSeed,
    corridor_constraints,
    envelope: Optional[Dict[str, Any]],
    *,
    timeout_s: int,
    route_context: Optional[RouteContext] = None,
) -> Tuple[Optional[HintCandidateSet], CorridorResult, List[Dict[str, Any]], Dict[str, Any]]:
    attempts: List[Tuple[HintCandidateSet, CorridorResult, Dict[str, Any]]] = []
    feedback_log: List[Dict[str, Any]] = []

    for index, hint_set in enumerate(hint_sets[:MAX_CORRIDOR_ATTEMPTS], start=1):
        corridor, build_meta = build_typed_corridor(
            hint_set,
            seed,
            list(corridor_constraints or []),
            expected_envelope=envelope,
            timeout_s=timeout_s,
            route_context=route_context,
        )
        attempts.append((hint_set, corridor, build_meta))

        # Geography catalog validation
        geo_validation_dict = {}
        if route_context and route_context.geography and corridor.corridor_geojson:
            geo_validation = validate_corridor_geography(
                corridor.corridor_geojson,
                route_context,
                corridor_length_km=corridor.total_length_km,
            )
            geo_validation_dict = geo_validation.to_dict()

        feedback_log.append(
            {
                "attempt": index,
                "coherence_score": round(hint_set.set_coherence_score, 4),
                "corridor_length_km": round(corridor.total_length_km, 4),
                "inflation_ratio": round(corridor.corridor_inflation_ratio, 4),
                "geo_score": round(corridor.geography_plausibility_score, 4),
                "rejected": corridor.rejected_for_geographic_implausibility,
                "constraint_log": build_meta.get("constraint_log"),
                "geo_validation": geo_validation_dict,
            }
        )
        if corridor.geography_plausibility_score >= CORRIDOR_QUALITY_THRESHOLD and corridor.corridor_inflation_ratio < 3.0:
            return hint_set, corridor, feedback_log, build_meta

    if not attempts:
        return None, CorridorResult(corridor_notes="No hint sets available"), feedback_log, {}

    attempts.sort(
        key=lambda item: (
            not item[1].rejected_for_geographic_implausibility,
            item[1].geography_plausibility_score,
            -item[1].corridor_inflation_ratio,
        ),
        reverse=True,
    )
    best_hint_set, best_corridor, best_meta = attempts[0]
    return best_hint_set, best_corridor, feedback_log, best_meta


def run_discovery_pipeline(
    seed: Union[RouteSeed, TypedRouteSeed],
    *,
    artifact_dir: Optional[str] = None,
    refine_geometry: bool = True,
    run_llm_advisory: bool = False,
    llm_mode: str = "mock",
    valhalla_timeout_s: int = 60,
    buffer_passes: Optional[List[int]] = None,
    scoring_mode: str = "ensemble",
    conn=None,
    catalog_ctx: Optional[DualCatalogContext] = None,
    enable_a2: bool = False,
    a2_inputs: Optional[A2RunInputs] = None,
) -> ConstructorRunSummary:
    typed_seed = _ensure_typed_seed(seed)
    legacy_seed = _normalize_route_seed(
        typed_seed_to_route_seed(typed_seed),
        catalog_ctx=catalog_ctx,
    )
    out_dir = _artifact_dir(typed_seed, artifact_dir)
    summary = ConstructorRunSummary(started_at=_utc_iso(), status="running")
    summary.route_seed = _seed_summary_dict(typed_seed, legacy_seed)
    summary.expected_geographic_envelope = legacy_seed.expected_geographic_envelope
    _safe_json_dump(summary.route_seed, out_dir / "route_seed_input.json")

    # Load route context from geography catalog if available
    route_context: Optional[RouteContext] = None
    if catalog_ctx:
        route_context = catalog_ctx.get_route_context(typed_seed.route_name)
        if route_context and route_context.geography:
            _safe_json_dump(route_context.to_dict(), out_dir / "route_geography_context.json")

    # Handle pilot_no_stops routes: geometry only, skip stop grounding
    if typed_seed.route_status == "pilot_no_stops":
        _LOG.info("Route '%s' is pilot_no_stops — geometry-only mode", typed_seed.route_name)
        try:
            typed_grounding = ground_typed_seed(
                typed_seed,
                legacy_seed.expected_geographic_envelope,
                conn=conn,
                catalog_ctx=catalog_ctx,
            )
            best_set, ranked_sets = resolve_coherent_hint_set(
                typed_grounding,
                typed_seed,
                legacy_seed.expected_geographic_envelope,
                route_context=route_context,
            )
            if best_set is not None:
                _, corridor, feedback_log, build_meta = _build_corridor_with_feedback(
                    ranked_sets or [best_set],
                    typed_seed,
                    typed_grounding.corridor_constraints,
                    legacy_seed.expected_geographic_envelope,
                    timeout_s=valhalla_timeout_s,
                    route_context=route_context,
                )
                summary.corridor = corridor.to_dict() if corridor.corridor_geojson else None
                summary.corridor_feedback_log = feedback_log
                _safe_json_dump(feedback_log, out_dir / "corridor_feedback_log.json")
                if corridor.corridor_geojson:
                    _safe_json_dump(summary.corridor, out_dir / "corridor_result.json")
                    summary.corridor_length_km = corridor.total_length_km or 0.0

            # BUG-013 Rule 3: Pilot routes — corridor IS the geometry
            if corridor.corridor_geojson:
                summary.geometry = {
                    "geometry_geojson": corridor.corridor_geojson,
                    "derived_from": "corridor_passthrough",
                    "geometry_confidence": 0.5,
                    "sequence_to_geometry_consistency": 0.0,
                    "total_length_km": corridor.total_length_km or 0.0,
                    "waypoint_count": 0,
                    "notes": f"pilot_no_stops: corridor passthrough {corridor.total_length_km or 0:.1f}km",
                    "straight_line_km": corridor.straight_line_km or 0.0,
                    "corridor_inflation_ratio": corridor.corridor_inflation_ratio or 0.0,
                    "in_bounds_fraction": corridor.in_bounds_fraction or 0.0,
                    "geography_plausibility_score": corridor.geography_plausibility_score or 0.0,
                    "rejected_for_geographic_implausibility": corridor.rejected_for_geographic_implausibility,
                }
                _safe_json_dump(summary.geometry, out_dir / "geometry_candidate.json")
                _LOG.info(
                    "[PILOT PASSTHROUGH] route=%s, corridor=%.1fkm → geometry=%.1fkm",
                    typed_seed.route_name, corridor.total_length_km or 0, corridor.total_length_km or 0,
                )

            summary.status = "completed"
            summary.completed_at = _utc_iso()
            summary.metrics = {
                "route_status": "geometry_only",
                "geometry_only": True,
                "corridor_length_km": summary.corridor_length_km or 0.0,
                "geometry_confidence": 0.5 if corridor.corridor_geojson else 0.0,
                "skeleton_stop_count": 0,
            }
            _safe_json_dump(summary.metrics, out_dir / "constructor_metrics.json")
            _safe_json_dump(summary.to_dict(), out_dir / "constructor_run_summary.json")
            return summary
        except Exception as exc:
            _LOG.error("pilot_no_stops geometry failed for '%s': %s", typed_seed.route_name, exc)
            summary.status = "completed"
            summary.completed_at = _utc_iso()
            summary.metrics = {"route_status": "geometry_only", "geometry_only": True, "error": str(exc)}
            _safe_json_dump(summary.to_dict(), out_dir / "constructor_run_summary.json")
            return summary

    # Extract zone from catalog entry for zone-aware parameter tuning (v5.3)
    _source_entry = typed_seed.source_entry or {}
    _route_chars = _source_entry.get("route_characteristics") or {}
    _route_zone = str(_route_chars.get("zone") or "").strip()

    # v5.3: Zone-aware envelope overrides — relax inflation/length limits for rural routes
    if _route_zone and legacy_seed.expected_geographic_envelope:
        env = legacy_seed.expected_geographic_envelope
        zone_lower = _route_zone.lower()
        if zone_lower in ("rural", "rural_periurban", "rural_to_urban"):
            env["max_inflation_ratio"] = max(float(env.get("max_inflation_ratio") or 3.5), 5.0)
            env["absolute_max_corridor_km"] = max(float(env.get("absolute_max_corridor_km") or 30.0), 50.0)
        elif zone_lower in ("periurban", "mixed"):
            env["max_inflation_ratio"] = max(float(env.get("max_inflation_ratio") or 3.5), 4.5)
            env["absolute_max_corridor_km"] = max(float(env.get("absolute_max_corridor_km") or 30.0), 45.0)

    try:
        typed_grounding = ground_typed_seed(
            typed_seed,
            legacy_seed.expected_geographic_envelope,
            conn=conn,
            catalog_ctx=catalog_ctx,
        )
        dispatch_log = token_dispatch_log(typed_seed, typed_grounding)
        _safe_json_dump(typed_grounding.to_dict(), out_dir / "typed_grounding_result.json")
        _safe_json_dump(dispatch_log, out_dir / "token_dispatch_log.json")

        # === Stage A2 — path-aware synthesis for unresolved anchor tokens ===
        # Runs only when the caller opts in and supplies a2_inputs. The
        # synthesizer commits its own nodes to node_prod, so downstream
        # corridor intersection can pick them up without further plumbing.
        a2_reports: List[Dict[str, Any]] = []
        if enable_a2 and conn is not None and a2_inputs is not None:
            if not a2_inputs.grounded_stop_coords:
                a2_inputs.grounded_stop_coords = tuple(
                    (float(g.best_candidate.lat), float(g.best_candidate.lon))
                    for g in (typed_grounding.token_groundings or {}).values()
                    if g.best_candidate is not None
                )
            if not a2_inputs.termini and len(a2_inputs.grounded_stop_coords) >= 2:
                a2_inputs.termini = (
                    a2_inputs.grounded_stop_coords[0],
                    a2_inputs.grounded_stop_coords[-1],
                )
            try:
                decisions = run_a2_for_unresolved_anchors(
                    typed_grounding=typed_grounding,
                    typed_seed=typed_seed,
                    inputs=a2_inputs,
                    conn=conn,
                )
                a2_reports = [d.to_dict() for d in decisions]
                _safe_json_dump(a2_reports, out_dir / "a2_reports.json")
            except Exception as a2_exc:
                _LOG.error(
                    "A2 stage raised for route=%r: %s",
                    typed_seed.route_name, a2_exc, exc_info=True,
                )
                a2_reports = [{"outcome": "error", "error": str(a2_exc)}]

        best_set, ranked_sets = resolve_coherent_hint_set(
            typed_grounding,
            typed_seed,
            legacy_seed.expected_geographic_envelope,
            route_context=route_context,
        )
        if best_set is None:
            summary.status = "failed"
            summary.error = "No coherent hint set could be resolved"
            summary.completed_at = _utc_iso()
            _safe_json_dump(summary.to_dict(), out_dir / "constructor_run_summary.json")
            return summary

        summary.hint_coherence = {
            "best_set": best_set.to_dict(),
            "all_sets_ranked": serialize_hint_sets(ranked_sets),
        }
        summary.hint_coherence_score = best_set.set_coherence_score
        _safe_json_dump(summary.hint_coherence, out_dir / "hint_coherence.json")

        selected_set, corridor, feedback_log, build_meta = _build_corridor_with_feedback(
            ranked_sets or [best_set],
            typed_seed,
            typed_grounding.corridor_constraints,
            legacy_seed.expected_geographic_envelope,
            timeout_s=valhalla_timeout_s,
            route_context=route_context,
        )
        summary.corridor_attempts = len(feedback_log)
        summary.corridor_feedback_log = feedback_log
        summary.arterial_injection_log = build_meta.get("constraint_log")
        summary.arterial_waypoints_injected = len((build_meta.get("constraint_log") or {}).get("arterials", []))
        _safe_json_dump(feedback_log, out_dir / "corridor_feedback_log.json")

        if selected_set is None or not corridor.corridor_geojson:
            summary.status = "failed"
            summary.error = corridor.corridor_notes or "Corridor construction failed"
            summary.completed_at = _utc_iso()
            _safe_json_dump(summary.to_dict(), out_dir / "constructor_run_summary.json")
            return summary

        selected_candidates = _selected_candidates_map(typed_seed, selected_set)
        effective_grounding = build_legacy_grounding(
            typed_seed,
            typed_grounding,
            selected_candidates=selected_candidates,
        )
        summary.grounding = effective_grounding.to_dict()

        # === BUG-010: Terminus Lock — clip corridor to terminus bounds ===
        corridor_pre_lock = corridor
        corridor = apply_terminus_lock(corridor, typed_seed)
        if corridor is not corridor_pre_lock:
            _safe_json_dump(
                {"original_km": corridor_pre_lock.total_length_km, "locked_km": corridor.total_length_km},
                out_dir / "terminus_lock_log.json",
            )

        summary.corridor = corridor.to_dict()
        _safe_json_dump(summary.grounding, out_dir / "stop_grounding_result.json")
        _safe_json_dump(summary.corridor, out_dir / "corridor_result.json")

        known_anchor_ids: Set[str] = set()
        known_intermediate_ids: Set[str] = set()
        if effective_grounding.best_anchor_a():
            known_anchor_ids.add(effective_grounding.best_anchor_a().stop_id)
        if effective_grounding.best_anchor_b():
            known_anchor_ids.add(effective_grounding.best_anchor_b().stop_id)
        for matches in effective_grounding.matched_intermediate_candidates.values():
            if matches:
                known_intermediate_ids.add(matches[0].stop_id)

        # Camino D PIEZA 7: propagate province from the seed.
        intersection = intersect_corridor_with_stops(
            corridor.corridor_geojson,
            operator_name=legacy_seed.operator_name,
            cooperative_name=legacy_seed.cooperative_name,
            locality_hints=legacy_seed.locality_hints,
            expected_envelope=legacy_seed.expected_geographic_envelope,
            known_anchor_ids=known_anchor_ids,
            known_intermediate_ids=known_intermediate_ids,
            buffer_passes=buffer_passes,
            corridor_length_km=corridor.total_length_km or 0.0,
            jurisdiction=typed_seed.jurisdiction,
            conn=conn,
            province=getattr(legacy_seed, "province", None),
        )
        summary.intersection = intersection.to_dict()
        _safe_json_dump(summary.intersection, out_dir / "corridor_intersection.json")

        # Annotate candidates with geography catalog fields before scoring
        if route_context and (route_context.required_areas or route_context.forbidden_areas):
            from datamind_console.phases.phase3_routes.stop_grounding.geographic_validator import (
                validate_stops_geography,
            )
            geo_result = validate_stops_geography(
                [{"lat": c.lat, "lon": c.lon, "stop_id": c.stop_id} for c in intersection.ordered_candidates],
                route_context,
            )
            if geo_result.get("has_constraints"):
                stop_flags = geo_result.get("stop_flags", {})
                # Collect terminus-protected stop IDs so we can exempt them from forbidden_area
                _terminus_stop_ids: Set[str] = set()
                for token in typed_seed.sequence_tokens:
                    if is_terminus_protected(token):
                        for label, grounding in (typed_grounding.token_groundings or {}).items():
                            if label == token.label and grounding.best_candidate:
                                _terminus_stop_ids.add(grounding.best_candidate.stop_id)

                for candidate in intersection.ordered_candidates:
                    flags = stop_flags.get(candidate.stop_id, {})
                    candidate.in_required_area = flags.get("in_required_area", False)
                    # BUG-007: Terminus stops are exempt from in_forbidden_area
                    is_terminus = (
                        candidate.path_fraction < 0.01
                        or candidate.path_fraction > 0.99
                        or candidate.stop_id in _terminus_stop_ids
                    )
                    if is_terminus:
                        candidate.in_forbidden_area = False
                    else:
                        candidate.in_forbidden_area = flags.get("in_forbidden_area", False)
                    candidate.distance_to_nearest_required_area_m = flags.get(
                        "distance_to_nearest_required_area_m", 99999.0
                    )

        # Build terminus-protected stop IDs from grounded endpoint tokens
        terminus_protected_ids: Set[str] = set()
        for token in typed_seed.sequence_tokens:
            if is_terminus_protected(token):
                # Infer terminus_type if not set
                token.terminus_type = infer_terminus_type(token)
                # Find matching grounded stop IDs
                for label, grounding in (typed_grounding.token_groundings or {}).items():
                    if label == token.label and grounding.best_candidate:
                        terminus_protected_ids.add(grounding.best_candidate.stop_id)

        probable, marginal, rejected = score_candidates(
            intersection.ordered_candidates,
            corridor_geojson=corridor.corridor_geojson,
            scoring_mode=scoring_mode,
            route_length_km=corridor.total_length_km or 0.0,
            route_n_stops=len(ordered_groundable_tokens(typed_seed)),
            terminus_protected_ids=terminus_protected_ids,
        )

        # === BUG-012: Tail Relaxation — rescue marginal stops near termini ===
        probable, marginal, rejected, tail_relax_log = apply_tail_relaxation(
            probable, marginal, rejected,
            corridor=corridor,
            seed=typed_seed,
            terminus_protected_ids=terminus_protected_ids,
        )
        _safe_json_dump(tail_relax_log, out_dir / "tail_relaxation_log.json")

        # === Synthetic Terminus Stops — inject when no real stop near terminus ===
        probable, synthetic_log = inject_synthetic_terminus_stops(
            probable,
            corridor=corridor,
            seed=typed_seed,
        )
        _safe_json_dump(synthetic_log, out_dir / "synthetic_terminus_log.json")

        _safe_json_dump(
            {
                "probable_count": len(probable),
                "marginal_count": len(marginal),
                "rejected_count": len(rejected),
                "scores": [candidate.to_dict() for candidate in intersection.ordered_candidates],
                "tail_relaxation": tail_relax_log,
                "synthetic_terminus": synthetic_log,
            },
            out_dir / "stop_scores.json",
        )

        skeleton = assemble_sequence_skeleton(
            probable,
            marginal,
            rejected,
            corridor_length_km=corridor.total_length_km or 0.0,
        )
        validation = validate_sequence(
            skeleton,
            effective_grounding,
            corridor_length_km=corridor.total_length_km or 0.0,
        )
        summary.sequence_validation = validation
        summary.stops_removed_by_validation = int(validation.get("stops_removed_by_validation") or 0)
        if validation.get("repaired_stops") is not None:
            skeleton.ordered_stops = list(validation["repaired_stops"])
            skeleton.total_stops = len(skeleton.ordered_stops)
        # BUG-014: Compute sequence_confidence with new architecture (ordering only)
        new_confidences = compute_all_confidences(
            corridor,
            skeleton,
            zone=_route_zone,
        )
        skeleton.sequence_confidence = new_confidences["sequence_confidence"]
        # Apply validation delta on top
        skeleton.sequence_confidence = max(
            0.0,
            min(1.0, skeleton.sequence_confidence + float(validation.get("validation_confidence_delta") or 0.0)),
        )
        summary.skeleton = skeleton.to_dict()
        _safe_json_dump(summary.skeleton, out_dir / "sequence_skeleton.json")
        _safe_json_dump(validation, out_dir / "sequence_validation.json")
        _safe_json_dump(new_confidences, out_dir / "confidence_architecture.json")

        # === Missing-node backfill gap analysis ===
        _route_id = getattr(legacy_seed, "route_job_id", None) or typed_seed.route_name
        _canton = (legacy_seed.locality_hints[0] if legacy_seed.locality_hints else "unknown")
        _province = getattr(legacy_seed, "province", None) or "sample_region"
        backfill_candidates = analyze_gaps(
            skeleton,
            synthetic_log,
            probable,
            route_id=_route_id,
            canton=_canton,
            province=_province,
            corridor_length_km=corridor.total_length_km or 0.0,
        )
        if backfill_candidates:
            _safe_json_dump(
                [c.to_dict() for c in backfill_candidates],
                out_dir / "backfill_candidates.json",
            )
        summary.backfill_candidates = [c.to_dict() for c in backfill_candidates]

        geometry = derive_geometry_candidate(
            corridor,
            skeleton,
            expected_envelope=legacy_seed.expected_geographic_envelope,
            refine_with_valhalla=refine_geometry,
            timeout_s=valhalla_timeout_s,
        )
        summary.geometry = geometry.to_dict()
        _safe_json_dump(summary.geometry, out_dir / "geometry_candidate.json")

        if run_llm_advisory:
            llm_payload = _build_llm_evidence_payload(
                legacy_seed,
                effective_grounding,
                corridor,
                intersection,
                skeleton,
                geometry,
                typed_seed=typed_seed,
                dispatch_log=dispatch_log,
            )
            _safe_json_dump(llm_payload, out_dir / "llm_evidence_payload.json")
            summary.llm_refinement = _run_llm_advisory(llm_payload, mode=llm_mode)
            _safe_json_dump(summary.llm_refinement, out_dir / "llm_refinement_result.json")

        summary.status = "completed"
        summary.completed_at = _utc_iso()
        summary.corridor_length_km = corridor.total_length_km or 0.0
        summary.stops_removed_by_spacing = getattr(skeleton, "stops_removed_by_spacing", 0)
        summary.stops_removed_by_density = getattr(skeleton, "stops_removed_by_density", 0)
        summary.marginals_promoted = getattr(skeleton, "marginals_promoted", 0)
        summary.endpoint_resolution_method = effective_grounding.best_anchor_a().match_source if effective_grounding.best_anchor_a() else ""

        summary.metrics = _compute_metrics(
            legacy_seed,
            effective_grounding,
            corridor,
            intersection,
            skeleton,
            geometry,
        )

        # BUG-014: Override with Two-Confidence Architecture values
        summary.metrics["geometry_confidence"] = new_confidences["geometry_confidence"]
        summary.metrics["sequence_confidence"] = skeleton.sequence_confidence
        summary.metrics["stop_coverage_score"] = new_confidences["stop_coverage_score"]

        # Geography catalog validation for final metrics
        if route_context and route_context.geography and corridor.corridor_geojson:
            final_geo_validation = validate_corridor_geography(
                corridor.corridor_geojson,
                route_context,
                corridor_length_km=corridor.total_length_km,
            )
            summary.metrics["geo_validation"] = final_geo_validation.to_dict()
        summary.metrics["route_status"] = _route_status_from_metrics(summary.metrics)
        summary.metrics["hint_coherence_score"] = summary.hint_coherence_score
        summary.metrics["corridor_attempts"] = summary.corridor_attempts
        summary.metrics["arterial_waypoints_injected"] = summary.arterial_waypoints_injected
        summary.metrics["sequence_validation_pass_rate"] = validation.get("pass_rate", 1.0)
        summary.metrics["typed_dispatch_summary"] = dispatch_log
        summary.metrics["tail_relaxation"] = tail_relax_log
        summary.metrics["synthetic_terminus"] = synthetic_log
        summary.metrics["backfill_candidates_count"] = len(backfill_candidates)
        summary.metrics["a2_reports"] = a2_reports
        summary.metrics["a2_synthesized_count"] = sum(
            1 for r in a2_reports if r.get("outcome") == "synthesized"
        )
        summary.metrics["a2_dropped_count"] = sum(
            1 for r in a2_reports
            if str(r.get("outcome", "")).startswith("dropped_")
        )
        _safe_json_dump(summary.metrics, out_dir / "constructor_metrics.json")

        summary.geography_audit = _build_geography_audit(
            seed=legacy_seed,
            expected_envelope=legacy_seed.expected_geographic_envelope or {},
            metrics=summary.metrics,
            skeleton=skeleton,
            geometry=geometry,
        )
        _safe_json_dump(summary.geography_audit, out_dir / "geography_plausibility_audit.json")
        _safe_json_dump(summary.to_dict(), out_dir / "constructor_run_summary.json")
        return summary
    except Exception as exc:
        _LOG.error("Pipeline failed for '%s': %s", typed_seed.route_name, exc, exc_info=True)
        summary.status = "failed"
        summary.error = str(exc)
        summary.completed_at = _utc_iso()
        _safe_json_dump(summary.to_dict(), out_dir / "constructor_run_summary.json")
        return summary


def _build_llm_evidence_payload(
    seed: RouteSeed,
    grounding: StopGroundingResult,
    corridor: CorridorResult,
    intersection: CorridorIntersectionResult,
    skeleton: SequenceSkeleton,
    geometry: GeometryCandidate,
    *,
    typed_seed: Optional[TypedRouteSeed] = None,
    dispatch_log: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    sequence_preview = [
        {"seq": idx + 1, "name": stop.stop_name, "score": round(stop.on_route_score, 4)}
        for idx, stop in enumerate(skeleton.ordered_stops[:10])
    ]

    return {
        "route_context": {
            "route_name": seed.route_name,
            "operator": seed.operator_name,
            "cooperative": seed.cooperative_name,
            "corridor_description": seed.corridor_description,
            "expected_geographic_envelope": seed.expected_geographic_envelope,
            "typed_sequence_tokens": [token.to_dict() for token in typed_seed.sequence_tokens] if typed_seed else [],
        },
        "grounded_evidence": {
            "anchor_a_matches": [match.to_dict() for match in grounding.matched_anchor_a_candidates[:3]],
            "anchor_b_matches": [match.to_dict() for match in grounding.matched_anchor_b_candidates[:3]],
            "unmatched_hints": grounding.unmatched_hints,
            "grounding_confidence": grounding.overall_grounding_confidence,
            "corridor_summary": {
                "total_length_km": corridor.total_length_km,
                "straight_line_km": corridor.straight_line_km,
                "corridor_inflation_ratio": corridor.corridor_inflation_ratio,
                "geography_plausibility_score": corridor.geography_plausibility_score,
                "rejected_for_geographic_implausibility": corridor.rejected_for_geographic_implausibility,
                "buffer_used_m": intersection.buffer_used_m,
                "corridor_confidence": corridor.corridor_confidence,
            },
            "candidate_sequence_preview": sequence_preview,
            "sequence_stats": {
                "total_stops": skeleton.total_stops,
                "total_length_km": skeleton.total_length_km,
                "avg_spacing_m": skeleton.avg_stop_spacing_m,
                "gap_count": len(skeleton.gaps),
            },
            "gaps": skeleton.gaps[:5],
            "geometry_confidence": geometry.geometry_confidence,
            "token_types": {row["label"]: row for row in list(dispatch_log or [])},
        },
    }


def _run_llm_advisory(payload: Dict[str, Any], *, mode: str = "mock") -> Dict[str, Any]:
    if mode == "mock":
        corridor_summary = payload.get("grounded_evidence", {}).get("corridor_summary", {})
        rejected = bool(corridor_summary.get("rejected_for_geographic_implausibility"))
        return {
            "mode": "mock",
            "sequence_assessment": "Geographically implausible corridor." if rejected else "Sequence appears plausible.",
            "express_skip_analysis": "Review skipped pending geography." if rejected else "No express pattern detected.",
            "gap_analysis": [],
            "branch_variant_risk": "low",
            "geographically_implausible_corridor": rejected,
            "failure_reasons": ["geographically_implausible_corridor"] if rejected else [],
            "confidence": 0.12 if rejected else 0.70,
            "recommended_action": "block_geography" if rejected else "review",
            "recommended_operator_actions": ["Review typed token resolutions", "Check corridor gaps"],
            "reasoning_summary": "Mock advisory output.",
        }

    from datamind_console.phases.phase3_routes.constructor_llm_advisor import ConstructorLLMAdvisor

    return ConstructorLLMAdvisor(mode=mode).advise_sequence_discovery(payload)


def _compute_metrics(
    seed: RouteSeed,
    grounding: StopGroundingResult,
    corridor: CorridorResult,
    intersection: CorridorIntersectionResult,
    skeleton: SequenceSkeleton,
    geometry: GeometryCandidate,
) -> Dict[str, Any]:
    best_scores = []
    if grounding.matched_anchor_a_candidates:
        best_scores.append(grounding.matched_anchor_a_candidates[0].composite_score)
    if grounding.matched_anchor_b_candidates:
        best_scores.append(grounding.matched_anchor_b_candidates[0].composite_score)
    for matches in grounding.matched_intermediate_candidates.values():
        if matches:
            best_scores.append(matches[0].composite_score)

    within_50m = sum(1 for stop in skeleton.ordered_stops if stop.distance_to_corridor_m <= 50.0)
    spacing_values = []
    max_gap = 0.0
    for idx in range(1, len(skeleton.ordered_stops)):
        previous = skeleton.ordered_stops[idx - 1]
        current = skeleton.ordered_stops[idx]
        gap_m = ((previous.lon - current.lon) ** 2 + (previous.lat - current.lat) ** 2) ** 0.5 * 111000.0
        spacing_values.append(gap_m)
        max_gap = max(max_gap, gap_m)

    return {
        "anchor_match_rate": (
            (1 if grounding.matched_anchor_a_candidates else 0)
            + (1 if grounding.matched_anchor_b_candidates else 0)
        ) / 2.0,
        "intermediate_match_rate": len(grounding.matched_intermediate_candidates) / max(1, len(seed.intermediate_hints)),
        "stop_match_confidence_avg": sum(best_scores) / len(best_scores) if best_scores else 0.0,
        "corridor_stop_coverage_50m": within_50m / max(1, skeleton.total_stops),
        "unmatched_hint_rate": len(grounding.unmatched_hints) / max(1, 2 + len(seed.intermediate_hints)),
        "route_length_km": corridor.total_length_km,
        "straight_line_km": corridor.straight_line_km,
        "corridor_inflation_ratio": corridor.corridor_inflation_ratio,
        "geography_plausibility_score": corridor.geography_plausibility_score,
        "rejected_for_geographic_implausibility": corridor.rejected_for_geographic_implausibility,
        "stops_per_km": intersection.coverage_density,
        "avg_sequence_gap_m": sum(spacing_values) / len(spacing_values) if spacing_values else 0.0,
        "max_sequence_gap_m": max_gap,
        "sequence_confidence": skeleton.sequence_confidence,
        "geometry_confidence": geometry.geometry_confidence,
        "total_discovered_stops": skeleton.total_stops,
        "total_gaps": len(skeleton.gaps),
        "route_locality_consistency_notes": list(corridor.route_locality_consistency_notes or []),
    }
