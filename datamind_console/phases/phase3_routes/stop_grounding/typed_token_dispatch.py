"""
Stage B — Typed token intake and dispatch.

Supports both the prompt's v5-style typed catalog and the existing
`valle_chillos_catalog_typed_rules_v3.json` structure on disk.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

_LOG = logging.getLogger(__name__)

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    CorridorConstraint,
    RouteSeed,
    StopGroundingResult,
    StopMatch,
    TypedGroundingResult,
    TypedRouteSeed,
    TypedSeedToken,
    TypedTokenGrounding,
)
from datamind_console.phases.phase3_routes.stop_grounding.geography_guardrails import (
    infer_locality_keys,
)
from datamind_console.phases.phase3_routes.stop_grounding.dual_catalog_loader import (
    DualCatalogContext,
)
from datamind_console.phases.phase3_routes.stop_grounding.stop_grounding_service import (
    _ground_as_landmark,
    _ground_as_stop,
    _ground_as_terminal,
    _search_sector_for_best_stop,
)

DEFAULT_SEQUENCE_RULES: Dict[str, Dict[str, str]] = {
    "confirmed_stop": {"default_policy": "resolve_to_best_matching_stop_node"},
    "stop_candidate": {"default_policy": "resolve_to_best_matching_stop_node"},
    "terminal_confirmed": {"default_policy": "resolve_to_best_matching_terminal_node"},
    "terminal_candidate": {"default_policy": "resolve_to_best_matching_terminal_node"},
    "sector": {"default_policy": "search_for_terminal_or_strong_stop_within_area"},
    "neighborhood": {"default_policy": "search_for_terminal_or_strong_stop_within_area"},
    "district_anchor": {"default_policy": "search_for_strong_stop_within_area"},
    "landmark": {"default_policy": "treat_as_landmark_anchor"},
    "station_anchor": {"default_policy": "treat_as_landmark_anchor"},
    "urban_corridor": {"default_policy": "use_as_corridor_constraint_only"},
    "road_corridor": {"default_policy": "use_as_corridor_constraint_only"},
    "avenue": {"default_policy": "use_as_corridor_constraint_only"},
    "mixed_anchor": {"default_policy": "resolve_mixed_anchor_with_bias_to_stop_or_terminal"},
    "mixed_area_corridor": {"default_policy": "resolve_mixed_anchor_with_area_bias"},
    "generic_token": {"default_policy": "deprioritize"},
}

_ROLE_MAP = {
    "origin_hint": "origin_stop_candidate",
    "origin_area": "origin_area",
    "destination_hint": "destination_stop_candidate",
    "destination_area": "destination_area",
    "intermediate_hint": "intermediate_anchor",
    "connector_hint": "connector",
    "corridor_hint": "approach_corridor",
}


def _default_policy(kind: str, rules: Optional[Dict[str, Any]] = None) -> str:
    rule_map = rules or {}
    if kind in rule_map:
        policy = str((rule_map.get(kind) or {}).get("default_policy") or "").strip()
        if policy:
            return policy
    return str(DEFAULT_SEQUENCE_RULES.get(kind, {}).get("default_policy") or "resolve_to_best_matching_stop_node")


def _normalize_role(role: Optional[str], *, index: int, total: int) -> str:
    raw = str(role or "").strip()
    if raw:
        return _ROLE_MAP.get(raw, raw)
    if index == 0:
        return "origin_stop_candidate"
    if index == total - 1:
        return "destination_stop_candidate"
    return "intermediate_anchor"


def _confidence_from_entry(route_entry: Dict[str, Any], token: Dict[str, Any]) -> str:
    for value in (
        token.get("confidence"),
        token.get("sequence_confidence"),
        route_entry.get("sequence_confidence"),
    ):
        if str(value or "").strip():
            return str(value)
    return "medium"


def _route_label(route_entry: Dict[str, Any]) -> str:
    return str(route_entry.get("route_name") or route_entry.get("route") or "").strip()


def _note_bundle(route_entry: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    if str(route_entry.get("why") or "").strip():
        notes.append(str(route_entry["why"]))
    for item in route_entry.get("source_notes") or []:
        if str(item or "").strip():
            notes.append(str(item))
    return notes


def _localities_for_entry(
    route_entry: Dict[str, Any],
    *,
    province: Optional[str] = None,
) -> List[str]:
    explicit = list(route_entry.get("localities") or [])
    if explicit:
        return explicit
    texts: List[str] = [
        _route_label(route_entry),
        str(route_entry.get("cooperative") or ""),
    ]
    for token in route_entry.get("sequence_seed_typed") or []:
        texts.append(str(token.get("label") or ""))
    for label in route_entry.get("sequence_seed") or []:
        if isinstance(label, str):
            texts.append(label)
    for label in route_entry.get("explicit_anchors") or []:
        if isinstance(label, dict):
            texts.append(str(label.get("name") or ""))
        else:
            texts.append(str(label))
    for label in route_entry.get("researched_anchors") or []:
        if isinstance(label, dict):
            texts.append(str(label.get("name") or ""))
        else:
            texts.append(str(label))
    texts.extend(_note_bundle(route_entry))
    # Camino D PIEZA 7: province-aware locality inference (None == sample_region).
    return infer_locality_keys(texts, province=province)


def _normalize_constraint(
    route_entry: Dict[str, Any],
    raw: Dict[str, Any],
    rules: Optional[Dict[str, Any]] = None,
) -> CorridorConstraint:
    kind = str(raw.get("kind") or "road_corridor")
    return CorridorConstraint(
        label=str(raw.get("label") or ""),
        kind=kind,
        resolution_policy=str(
            raw.get("resolution_policy")
            or _default_policy(kind, rules)
        ),
        confidence=str(raw.get("confidence") or route_entry.get("sequence_confidence") or "medium"),
        usage=raw.get("usage"),
    )


def _build_anchor_terminus_map(
    route_entry: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Build label→anchor_info map from researched_anchors (v3 catalog format).

    In v3, researched_anchors can be dicts with ``name``, ``terminus_type``,
    and optional ``lat``/``lon``.  Returns a mapping keyed by normalized name
    (and common variants like stripping " terminus" suffix) so
    _upgrade_legacy_tokens can attach terminus_type and coordinates to
    matching sequence_seed tokens.
    """
    result: Dict[str, Dict[str, Any]] = {}
    for anchor in route_entry.get("researched_anchors") or []:
        if isinstance(anchor, dict):
            name = str(anchor.get("name") or "").strip()
            ttype = str(anchor.get("terminus_type") or "").strip()
            if not name:
                continue
            info: Dict[str, Any] = {}
            if ttype:
                info["terminus_type"] = ttype
            if anchor.get("lat") is not None and anchor.get("lon") is not None:
                info["lat"] = anchor["lat"]
                info["lon"] = anchor["lon"]
            # v4 catalog: pick up anchor role
            if anchor.get("role"):
                info["anchor_role"] = str(anchor["role"]).strip()
            if not info:
                continue
            key = name.lower()
            result[key] = info
            # Also register without " terminus" suffix for fuzzy matching
            for suffix in (" terminus", " terminal", " parada"):
                if key.endswith(suffix):
                    result[key[: -len(suffix)]] = info
    # Also process explicit_anchors (v4 catalog dict-style)
    for anchor in route_entry.get("explicit_anchors") or []:
        if isinstance(anchor, dict):
            name = str(anchor.get("name") or "").strip()
            if not name:
                continue
            info = {}
            if anchor.get("terminus_type"):
                info["terminus_type"] = str(anchor["terminus_type"])
            if anchor.get("lat") is not None and anchor.get("lon") is not None:
                info["lat"] = anchor["lat"]
                info["lon"] = anchor["lon"]
            if anchor.get("role"):
                info["anchor_role"] = str(anchor["role"]).strip()
            if info:
                result[name.lower()] = info
    return result


def _upgrade_legacy_tokens(
    route_entry: Dict[str, Any],
    rules: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    seeds = list(route_entry.get("sequence_seed") or [])
    if not seeds or not isinstance(seeds[0], str):
        return seeds
    terminus_map = _build_anchor_terminus_map(route_entry)
    typed: List[Dict[str, Any]] = []
    total = len(seeds)
    for index, label in enumerate(seeds):
        token: Dict[str, Any] = {
            "label": label,
            "kind": "stop_candidate",
            "role": _normalize_role(None, index=index, total=total),
            "resolution_policy": _default_policy("stop_candidate", rules),
            "confidence": str(route_entry.get("sequence_confidence") or "medium"),
            "position": index + 1,
        }
        # Propagate terminus_type + coords + role from researched/explicit anchors
        anchor_info = terminus_map.get(label.lower())
        if anchor_info:
            if anchor_info.get("terminus_type"):
                token["terminus_type"] = anchor_info["terminus_type"]
            if anchor_info.get("lat") is not None:
                token["anchor_lat"] = anchor_info["lat"]
                token["anchor_lon"] = anchor_info["lon"]
            if anchor_info.get("anchor_role"):
                token["anchor_role"] = anchor_info["anchor_role"]
        typed.append(token)
    return typed


def maybe_upgrade_legacy_seed(
    route_entry: Dict[str, Any],
    rules: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    upgraded = dict(route_entry)
    if upgraded.get("sequence_seed_typed"):
        return upgraded
    upgraded["sequence_seed"] = _upgrade_legacy_tokens(upgraded, rules)
    return upgraded


def intake_typed_seed(
    route_entry: Dict[str, Any],
    *,
    rules: Optional[Dict[str, Any]] = None,
    province: Optional[str] = None,
) -> TypedRouteSeed:
    # Camino D PIEZA 7: province plumbing. If caller does not pass it explicitly,
    # fall back to a top-level ``province`` field on the route_entry itself
    # (useful when a per-route entry was produced by a province-aware loader).
    # None == sample_region (legacy retrocompat).
    if province is None:
        province = route_entry.get("province") if isinstance(route_entry, dict) else None
    entry = maybe_upgrade_legacy_seed(route_entry, rules)
    token_payload = (
        list(entry.get("sequence_seed_typed") or [])
        or list(entry.get("sequence_seed") or [])
    )

    sequence_tokens: List[TypedSeedToken] = []
    for index, raw in enumerate(token_payload):
        if isinstance(raw, str):
            raw = {
                "label": raw,
                "kind": "stop_candidate",
                "role": _normalize_role(None, index=index, total=len(token_payload)),
                "resolution_policy": _default_policy("stop_candidate", rules),
                "confidence": str(entry.get("sequence_confidence") or "medium"),
                "position": index + 1,
            }
        kind = str(raw.get("kind") or "stop_candidate")
        sequence_tokens.append(
            TypedSeedToken(
                label=str(raw.get("label") or ""),
                kind=kind,
                role=_normalize_role(raw.get("role"), index=index, total=len(token_payload)),
                resolution_policy=str(raw.get("resolution_policy") or _default_policy(kind, rules)),
                confidence=_confidence_from_entry(entry, raw),
                position=int(raw.get("position") or (index + 1)),
                note=raw.get("note"),
                terminus_type=raw.get("terminus_type"),
                anchor_lat=raw.get("anchor_lat"),
                anchor_lon=raw.get("anchor_lon"),
                anchor_role=str(raw.get("anchor_role") or "waypoint"),
            )
        )

    corridor_constraints = [
        _normalize_constraint(entry, raw, rules)
        for raw in list(entry.get("corridor_prior") or [])
        if isinstance(raw, dict) and str(raw.get("label") or "").strip()
    ]

    return TypedRouteSeed(
        route_name=_route_label(entry),
        cooperative=str(entry.get("cooperative") or "").strip() or None,
        sequence_tokens=sequence_tokens,
        corridor_constraints=corridor_constraints,
        localities=_localities_for_entry(entry, province=province),
        notes=_note_bundle(entry),
        route_status=entry.get("route_status"),
        source_entry=route_entry,
        jurisdiction=str(entry.get("jurisdiction") or "both").strip(),
        province=province,
    )


def typed_seed_to_route_seed(seed: TypedRouteSeed) -> RouteSeed:
    groundable = [
        token
        for token in seed.sequence_tokens
        if token.resolution_policy != "use_as_corridor_constraint_only"
    ]
    anchor_a = groundable[0].label if groundable else ""
    anchor_b = groundable[-1].label if groundable else ""
    intermediates = [token.label for token in groundable[1:-1]]
    notes = list(seed.notes) if isinstance(seed.notes, list) else ([str(seed.notes)] if seed.notes else [])

    return RouteSeed(
        route_name=seed.route_name,
        operator_name=seed.cooperative,
        cooperative_name=seed.cooperative,
        corridor_description=" -> ".join(token.label for token in seed.sequence_tokens),
        anchor_a_hint=anchor_a,
        anchor_b_hint=anchor_b,
        intermediate_hints=intermediates,
        locality_hints=list(seed.localities or []),
        sequence_seed_fragments=[token.label for token in seed.sequence_tokens],
        source_notes=notes,
        # Camino D PIEZA 7: carry province from the typed seed to the legacy
        # seed so downstream helpers that only see RouteSeed retain it.
        province=getattr(seed, "province", None),
    )


def _merge_candidates(*candidate_groups: Iterable[StopMatch]) -> List[StopMatch]:
    merged: Dict[str, StopMatch] = {}
    for group in candidate_groups:
        for candidate in group:
            if candidate is None:
                continue
            existing = merged.get(candidate.stop_id)
            if existing is None or candidate.composite_score > existing.composite_score:
                merged[candidate.stop_id] = candidate
    return sorted(merged.values(), key=lambda item: item.composite_score, reverse=True)


def _token_to_constraint(token: TypedSeedToken) -> CorridorConstraint:
    return CorridorConstraint(
        label=token.label,
        kind=token.kind,
        resolution_policy=token.resolution_policy,
        confidence=token.confidence,
    )


def _compute_overall_confidence(results: Dict[str, TypedTokenGrounding]) -> float:
    if not results:
        return 0.0
    scores: List[float] = []
    resolved = 0
    for grounding in results.values():
        if grounding.best_candidate:
            scores.append(grounding.best_candidate.composite_score)
        if grounding.is_resolved:
            resolved += 1
    avg_score = sum(scores) / len(scores) if scores else 0.0
    return 0.45 * (resolved / max(len(results), 1)) + 0.55 * avg_score


def ground_typed_seed(
    seed: TypedRouteSeed,
    envelope: Optional[Dict[str, Any]],
    *,
    conn=None,
    max_candidates_per_token: int = 5,
    catalog_ctx: Optional[DualCatalogContext] = None,
) -> TypedGroundingResult:
    results: Dict[str, TypedTokenGrounding] = {}
    corridor_hints: List[CorridorConstraint] = []

    # Camino D PIEZA 7 (2026-04-09): read province from the seed and propagate
    # it to every stop_grounding_service entrypoint. None == sample_region (legacy
    # retrocompat); unknown provinces fall through to the safe-fallback semantics
    # of the province-aware helpers.
    _province = getattr(seed, "province", None)

    for token in seed.sequence_tokens:
        policy = token.resolution_policy
        candidates: List[StopMatch] = []
        resolved_from_sector = False
        resolved_from_landmark = False

        if policy == "resolve_to_best_matching_stop_node":
            candidates = _ground_as_stop(
                token.label,
                expected_envelope=envelope,
                operator_name=seed.cooperative,
                locality_hints=seed.localities,
                max_results=max_candidates_per_token,
                conn=conn,
                province=_province,
            )
        elif policy == "resolve_to_best_matching_terminal_node":
            candidates = _ground_as_terminal(
                token.label,
                expected_envelope=envelope,
                operator_name=seed.cooperative,
                locality_hints=seed.localities,
                max_results=max_candidates_per_token,
                conn=conn,
                province=_province,
            )
        elif policy in (
            "search_for_terminal_or_strong_stop_within_area",
            "search_for_strong_stop_within_area",
        ):
            # Use geography catalog area_definitions for sector bbox if available
            sector_bbox = None
            if catalog_ctx:
                from datamind_console.phases.phase3_routes.stop_grounding.geographic_validator import (
                    get_area_bbox_for_sector,
                )
                sector_bbox = get_area_bbox_for_sector(token.label, catalog_ctx)
            candidates = _search_sector_for_best_stop(
                token.label,
                expected_envelope=envelope,
                operator_name=seed.cooperative,
                locality_hints=seed.localities,
                max_results=max_candidates_per_token,
                prefer_terminals=(policy == "search_for_terminal_or_strong_stop_within_area"),
                conn=conn,
                sector_bbox_override=sector_bbox,
                province=_province,
            )
            resolved_from_sector = True
        elif policy == "treat_as_landmark_anchor":
            candidates = _ground_as_landmark(
                token.label,
                expected_envelope=envelope,
                operator_name=seed.cooperative,
                locality_hints=seed.localities,
                max_results=max_candidates_per_token,
                conn=conn,
                province=_province,
            )
            resolved_from_landmark = True
        elif policy == "use_as_corridor_constraint_only":
            corridor_hints.append(_token_to_constraint(token))
            continue
        elif policy == "resolve_mixed_anchor_with_area_bias":
            candidates = _merge_candidates(
                _search_sector_for_best_stop(
                    token.label,
                    expected_envelope=envelope,
                    operator_name=seed.cooperative,
                    locality_hints=seed.localities,
                    max_results=max_candidates_per_token,
                    prefer_terminals=True,
                    conn=conn,
                    province=_province,
                ),
                _ground_as_terminal(
                    token.label,
                    expected_envelope=envelope,
                    operator_name=seed.cooperative,
                    locality_hints=seed.localities,
                    max_results=max_candidates_per_token,
                    conn=conn,
                    province=_province,
                ),
                _ground_as_stop(
                    token.label,
                    expected_envelope=envelope,
                    operator_name=seed.cooperative,
                    locality_hints=seed.localities,
                    max_results=max_candidates_per_token,
                    conn=conn,
                    province=_province,
                ),
            )[:max_candidates_per_token]
            corridor_hints.append(_token_to_constraint(token))
            resolved_from_sector = True
        elif policy == "resolve_mixed_anchor_with_bias_to_stop_or_terminal":
            candidates = _merge_candidates(
                _ground_as_terminal(
                    token.label,
                    expected_envelope=envelope,
                    operator_name=seed.cooperative,
                    locality_hints=seed.localities,
                    max_results=max_candidates_per_token,
                    conn=conn,
                    province=_province,
                ),
                _ground_as_stop(
                    token.label,
                    expected_envelope=envelope,
                    operator_name=seed.cooperative,
                    locality_hints=seed.localities,
                    max_results=max_candidates_per_token,
                    conn=conn,
                    province=_province,
                ),
            )[:max_candidates_per_token]
        elif policy in ("deprioritize", "deprioritize_until_resolved"):
            candidates = _ground_as_stop(
                token.label,
                expected_envelope=envelope,
                operator_name=seed.cooperative,
                locality_hints=seed.localities,
                max_results=max_candidates_per_token,
                conn=conn,
                province=_province,
            )
            for candidate in candidates:
                candidate.composite_score *= 0.5
        else:
            candidates = _ground_as_stop(
                token.label,
                expected_envelope=envelope,
                operator_name=seed.cooperative,
                locality_hints=seed.localities,
                max_results=max_candidates_per_token,
                conn=conn,
                province=_province,
            )

        # Camino D PIEZA 4 (2026-04-09): UNCONDITIONAL anchor-proxy injection.
        #
        # Prior to Camino D this block was guarded by `best_name_sim < 0.35`,
        # meaning that when the fuzzy matcher found *any* stop with name
        # similarity >= 0.35, the anchor_proxy was suppressed even if that
        # match was geographically nonsensical. In multi-province settings
        # with a Sample Region-only stop universe, a Sample Region B/Azuay/Imbabura anchor
        # label would pg_trgm-match an accidentally similar Sample Region stop
        # (name_sim ≥ 0.35) and win over the explicit v3-catalog anchor
        # coordinates — that was the ALAUSI-01 leak pathway.
        #
        # The v3 catalog's anchor_lat/anchor_lon is a HUMAN-AUTHORED ground
        # truth. When a token carries ground-truth coordinates, the dispatcher
        # MUST preserve them. We inject the anchor_proxy at position 0
        # unconditionally; downstream scoring is free to rank it below other
        # candidates if text alignment is strong, but it will never be
        # *missing* from the candidate list.
        if token.has_anchor_coords:
            best_name_sim = candidates[0].name_similarity if candidates else 0.0
            proxy = StopMatch(
                stop_id=f"proxy:{token.label.lower().replace(' ', '_')}",
                stop_name=token.label,
                lat=token.anchor_lat,
                lon=token.anchor_lon,
                name_similarity=0.80,
                composite_score=0.72,
                geography_score=1.0,
                in_expected_geography=True,
                match_source="anchor_proxy",
                text_alignment_score=0.80,
                metadata={"terminus_type": token.terminus_type or "anchor_point"},
            )
            candidates.insert(0, proxy)
            _LOG.info(
                "[ANCHOR PROXY] Token '%s' unconditionally anchored via v3 catalog "
                "coords (%.4f, %.4f); best fuzzy name_sim was %.2f",
                token.label, token.anchor_lat, token.anchor_lon, best_name_sim,
            )

        best = candidates[0] if candidates else None
        results[token.label] = TypedTokenGrounding(
            token=token,
            candidates=candidates,
            best_candidate=best,
            resolution_method=policy,
            is_resolved=bool(best and best.composite_score >= 0.30),
            resolved_from_sector=resolved_from_sector,
            resolved_from_landmark=resolved_from_landmark,
        )

    for constraint in seed.corridor_constraints:
        corridor_hints.append(constraint)

    return TypedGroundingResult(
        token_groundings=results,
        corridor_constraints=corridor_hints,
        overall_confidence=_compute_overall_confidence(results),
    )


def ordered_groundable_tokens(seed: TypedRouteSeed) -> List[TypedSeedToken]:
    return [
        token
        for token in seed.sequence_tokens
        if token.resolution_policy != "use_as_corridor_constraint_only"
    ]


def build_legacy_grounding(
    seed: TypedRouteSeed,
    typed_grounding: TypedGroundingResult,
    *,
    selected_candidates: Optional[Dict[str, StopMatch]] = None,
) -> StopGroundingResult:
    ordered_tokens = ordered_groundable_tokens(seed)
    if not ordered_tokens:
        return StopGroundingResult()

    selected = dict(selected_candidates or {})
    anchor_a_token = ordered_tokens[0]
    anchor_b_token = ordered_tokens[-1]

    def _ordered_candidates(token: TypedSeedToken) -> List[StopMatch]:
        grounding = typed_grounding.token_groundings.get(token.label)
        if grounding is None:
            return []
        candidates = list(grounding.candidates)
        chosen = selected.get(token.label)
        if chosen is None:
            return candidates
        out = [chosen]
        seen = {chosen.stop_id}
        for candidate in candidates:
            if candidate.stop_id not in seen:
                out.append(candidate)
                seen.add(candidate.stop_id)
        return out

    intermediate_map: Dict[str, List[StopMatch]] = {}
    unmatched: List[str] = []
    for token in ordered_tokens[1:-1]:
        candidates = _ordered_candidates(token)
        if candidates:
            intermediate_map[token.label] = candidates
        else:
            unmatched.append(token.label)

    if not _ordered_candidates(anchor_a_token):
        unmatched.append(anchor_a_token.label)
    if not _ordered_candidates(anchor_b_token):
        unmatched.append(anchor_b_token.label)

    return StopGroundingResult(
        matched_anchor_a_candidates=_ordered_candidates(anchor_a_token),
        matched_anchor_b_candidates=_ordered_candidates(anchor_b_token),
        matched_intermediate_candidates=intermediate_map,
        unmatched_hints=unmatched,
        overall_grounding_confidence=typed_grounding.overall_confidence,
        grounding_notes="typed_dispatch",
    )


def token_dispatch_log(seed: TypedRouteSeed, grounding: TypedGroundingResult) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for token in seed.sequence_tokens:
        tg = grounding.token_groundings.get(token.label)
        best = tg.best_candidate if tg else None
        rows.append(
            {
                "label": token.label,
                "kind": token.kind,
                "role": token.role,
                "resolution_policy": token.resolution_policy,
                "confidence": token.confidence,
                "resolved": bool(tg and tg.is_resolved),
                "resolved_to": best.stop_name if best else None,
                "resolved_stop_id": best.stop_id if best else None,
                "resolved_score": round(best.composite_score, 4) if best else None,
                "resolved_from_sector": bool(tg and tg.resolved_from_sector),
                "resolved_from_landmark": bool(tg and tg.resolved_from_landmark),
                "candidate_count": len(tg.candidates) if tg else 0,
            }
        )
    return rows
