"""
Stage B2 — Hint-set coherence for typed dispatch results.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    HintCandidateSet,
    StopMatch,
    TypedGroundingResult,
    TypedRouteSeed,
    TypedSeedToken,
)
from datamind_console.phases.phase3_routes.stop_grounding.geography_guardrails import (
    point_in_bbox,
)
from datamind_console.phases.phase3_routes.stop_grounding.dual_catalog_loader import (
    DualCatalogContext,
    RouteContext,
)

TOKEN_QUALITY_WEIGHTS = {
    "confirmed_stop": 1.0,
    "terminal_confirmed": 1.0,
    "terminal_candidate": 0.9,
    "stop_candidate": 0.8,
    "landmark": 0.6,
    "station_anchor": 0.6,
    "district_anchor": 0.5,
    "sector": 0.5,
    "neighborhood": 0.5,
    "mixed_anchor": 0.7,
    "mixed_area_corridor": 0.5,
    "generic_token": 0.2,
}


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * radius_km * math.asin(math.sqrt(a))


def _project_fraction(
    point: StopMatch,
    start: StopMatch,
    end: StopMatch,
) -> float:
    ax = end.lon - start.lon
    ay = end.lat - start.lat
    denom = ax * ax + ay * ay
    if denom <= 0:
        return 0.0
    px = point.lon - start.lon
    py = point.lat - start.lat
    return (px * ax + py * ay) / denom


def _distance_to_line_km(point: StopMatch, start: StopMatch, end: StopMatch) -> float:
    fraction = max(0.0, min(1.0, _project_fraction(point, start, end)))
    proj_lon = start.lon + (end.lon - start.lon) * fraction
    proj_lat = start.lat + (end.lat - start.lat) * fraction
    return _haversine_km(point.lon, point.lat, proj_lon, proj_lat)


def _groundable_tokens(seed: TypedRouteSeed) -> List[TypedSeedToken]:
    return [
        token
        for token in seed.sequence_tokens
        if token.resolution_policy != "use_as_corridor_constraint_only"
    ]


def _anchor_tokens(seed: TypedRouteSeed) -> Tuple[TypedSeedToken, TypedSeedToken]:
    tokens = _groundable_tokens(seed)
    if not tokens:
        raise ValueError("No groundable tokens available")

    anchor_a = next((token for token in tokens if token.role.startswith("origin")), tokens[0])
    anchor_b = next((token for token in reversed(tokens) if token.role.startswith("destination")), tokens[-1])
    return anchor_a, anchor_b


def _expected_range_km(envelope: Optional[Dict[str, Any]]) -> Tuple[float, float]:
    absolute_max = float((envelope or {}).get("absolute_max_corridor_km") or 30.0)
    return 0.5, absolute_max


def _weighted_avg_quality(
    seed: TypedRouteSeed,
    cset: HintCandidateSet,
) -> float:
    token_lookup = {token.label: token for token in _groundable_tokens(seed)}
    weights = []
    weighted_scores = []

    items = [(token_lookup.get(cset.anchor_a.stop_name), cset.anchor_a), (token_lookup.get(cset.anchor_b.stop_name), cset.anchor_b)]
    del items

    anchor_a_token, anchor_b_token = _anchor_tokens(seed)
    weighted_scores.append(cset.anchor_a.composite_score * TOKEN_QUALITY_WEIGHTS.get(anchor_a_token.kind, 0.5))
    weights.append(TOKEN_QUALITY_WEIGHTS.get(anchor_a_token.kind, 0.5))
    weighted_scores.append(cset.anchor_b.composite_score * TOKEN_QUALITY_WEIGHTS.get(anchor_b_token.kind, 0.5))
    weights.append(TOKEN_QUALITY_WEIGHTS.get(anchor_b_token.kind, 0.5))

    for token in _groundable_tokens(seed)[1:-1]:
        match = cset.intermediates.get(token.label)
        if not match:
            continue
        weight = TOKEN_QUALITY_WEIGHTS.get(token.kind, 0.5)
        weighted_scores.append(match.composite_score * weight)
        weights.append(weight)

    if not weights:
        return 0.0
    return sum(weighted_scores) / sum(weights)


def score_hint_set(
    cset: HintCandidateSet,
    seed: TypedRouteSeed,
    envelope: Optional[Dict[str, Any]],
    *,
    route_context: Optional[RouteContext] = None,
) -> float:
    score = 0.0

    # Use geography catalog distance range if available, else envelope
    min_km, max_km = _expected_range_km(envelope)
    if route_context and route_context.expected_distance:
        min_km = float(route_context.expected_distance.get("min") or min_km)
        max_km = float(route_context.expected_distance.get("max") or max_km)

    has_geo = route_context is not None and route_context.geography is not None

    # 1. Straight-line plausibility (15%)
    if min_km <= cset.straight_line_km <= max_km:
        score += 0.15
    elif cset.straight_line_km > max_km * 2.0:
        score += 0.02

    # 2. Path inflation (15%)
    if cset.path_inflation < 1.5:
        score += 0.15
    elif cset.path_inflation < 2.5:
        score += 0.10
    elif cset.path_inflation < 4.0:
        score += 0.04

    # 3. Ordering (15%)
    score += 0.15 * cset.ordering_score

    # 4. Envelope containment (10%)
    score += 0.10 * cset.envelope_containment

    # 5. Must-pass-through compliance (20%) — from geography catalog
    if has_geo and route_context.required_areas:
        waypoints = _cset_to_waypoint_dicts(cset)
        areas_hit = 0
        for area in route_context.required_areas:
            for wp in waypoints:
                if area.contains_point(wp["lat"], wp["lon"]):
                    areas_hit += 1
                    break
        area_compliance = areas_hit / len(route_context.required_areas)
        score += 0.20 * area_compliance
    else:
        score += 0.20  # no constraint

    # 6. Must-NOT-enter compliance (10%) — from geography catalog
    if has_geo and route_context.forbidden_areas:
        waypoints = _cset_to_waypoint_dicts(cset)
        violations = 0
        for area in route_context.forbidden_areas:
            for wp in waypoints:
                if area.contains_point(wp["lat"], wp["lon"]):
                    violations += 1
                    break
        if violations == 0:
            score += 0.10
        else:
            score -= 0.10 * violations
    else:
        score += 0.10

    # 7. Individual match quality weighted by token priority (15%)
    score += 0.15 * _weighted_avg_quality(seed, cset)

    return score


def _cset_to_waypoint_dicts(cset: HintCandidateSet) -> List[Dict[str, float]]:
    """Convert a HintCandidateSet to a list of waypoint dicts for area checking."""
    waypoints = [{"lat": cset.anchor_a.lat, "lon": cset.anchor_a.lon}]
    for match in cset.intermediates.values():
        waypoints.append({"lat": match.lat, "lon": match.lon})
    waypoints.append({"lat": cset.anchor_b.lat, "lon": cset.anchor_b.lon})
    return waypoints


def _ordering_score(
    anchor_a: StopMatch,
    anchor_b: StopMatch,
    intermediates: List[StopMatch],
) -> float:
    if not intermediates:
        return 1.0
    fractions = [_project_fraction(stop, anchor_a, anchor_b) for stop in intermediates]
    if not fractions:
        return 1.0
    ordered = 0
    prev = -1.0
    for fraction in fractions:
        if fraction >= prev - 0.01:
            ordered += 1
        prev = fraction
    return ordered / len(fractions)


def _envelope_containment(stops: List[StopMatch], envelope: Optional[Dict[str, Any]]) -> float:
    bbox = dict((envelope or {}).get("bbox") or {})
    if not bbox:
        return 1.0
    in_bounds = sum(1 for stop in stops if point_in_bbox(stop.lon, stop.lat, bbox))
    return in_bounds / max(len(stops), 1)


def _choose_intermediate_candidate(
    token: TypedSeedToken,
    grounding: TypedGroundingResult,
    anchor_a: StopMatch,
    anchor_b: StopMatch,
    *,
    previous_fraction: float,
) -> Tuple[Optional[StopMatch], float]:
    token_grounding = grounding.token_groundings.get(token.label)
    if token_grounding is None:
        return None, previous_fraction

    scored: List[Tuple[float, float, StopMatch]] = []
    for candidate in token_grounding.candidates[:3]:
        fraction = _project_fraction(candidate, anchor_a, anchor_b)
        if fraction < previous_fraction - 0.05:
            continue
        line_dist = _distance_to_line_km(candidate, anchor_a, anchor_b)
        scored.append((line_dist, -candidate.composite_score, candidate))

    if not scored and token_grounding.best_candidate is not None:
        return token_grounding.best_candidate, _project_fraction(token_grounding.best_candidate, anchor_a, anchor_b)

    if not scored:
        return None, previous_fraction

    scored.sort(key=lambda item: (item[0], item[1]))
    chosen = scored[0][2]
    return chosen, _project_fraction(chosen, anchor_a, anchor_b)


def _build_candidate_set(
    seed: TypedRouteSeed,
    grounding: TypedGroundingResult,
    anchor_a: StopMatch,
    anchor_b: StopMatch,
) -> HintCandidateSet:
    tokens = _groundable_tokens(seed)
    intermediates: Dict[str, StopMatch] = {}
    unresolved: List[str] = []
    ordered_intermediates: List[StopMatch] = []
    previous_fraction = 0.0

    for token in tokens[1:-1]:
        match, previous_fraction = _choose_intermediate_candidate(
            token,
            grounding,
            anchor_a,
            anchor_b,
            previous_fraction=previous_fraction,
        )
        if match is None:
            unresolved.append(token.label)
            continue
        intermediates[token.label] = match
        ordered_intermediates.append(match)

    stops = [anchor_a] + ordered_intermediates + [anchor_b]
    straight_line_km = _haversine_km(anchor_a.lon, anchor_a.lat, anchor_b.lon, anchor_b.lat)
    waypoint_path_km = 0.0
    for idx in range(1, len(stops)):
        waypoint_path_km += _haversine_km(stops[idx - 1].lon, stops[idx - 1].lat, stops[idx].lon, stops[idx].lat)

    return HintCandidateSet(
        anchor_a=anchor_a,
        anchor_b=anchor_b,
        intermediates=intermediates,
        unresolved=unresolved,
        straight_line_km=straight_line_km,
        waypoint_path_km=waypoint_path_km,
        path_inflation=waypoint_path_km / max(straight_line_km, 0.25),
        ordering_score=_ordering_score(anchor_a, anchor_b, ordered_intermediates),
        envelope_containment=_envelope_containment(stops, envelope=None),
    )


def _fallback_set(seed: TypedRouteSeed, grounding: TypedGroundingResult) -> Optional[HintCandidateSet]:
    tokens = _groundable_tokens(seed)
    if len(tokens) < 2:
        return None
    anchor_a_grounding = grounding.token_groundings.get(tokens[0].label)
    anchor_b_grounding = grounding.token_groundings.get(tokens[-1].label)
    if not anchor_a_grounding or not anchor_b_grounding or not anchor_a_grounding.best_candidate or not anchor_b_grounding.best_candidate:
        return None
    return _build_candidate_set(seed, grounding, anchor_a_grounding.best_candidate, anchor_b_grounding.best_candidate)


def _generate_candidate_sets(
    grounding: TypedGroundingResult,
    seed: TypedRouteSeed,
    envelope: Optional[Dict[str, Any]],
    *,
    route_context: Optional[RouteContext] = None,
) -> List[HintCandidateSet]:
    anchor_a_token, anchor_b_token = _anchor_tokens(seed)
    anchor_a_grounding = grounding.token_groundings.get(anchor_a_token.label)
    anchor_b_grounding = grounding.token_groundings.get(anchor_b_token.label)
    anchor_a_candidates = list(anchor_a_grounding.candidates if anchor_a_grounding else [])
    anchor_b_candidates = list(anchor_b_grounding.candidates if anchor_b_grounding else [])
    anchor_a_candidates = anchor_a_candidates[:3]
    anchor_b_candidates = anchor_b_candidates[:3]

    sets: List[HintCandidateSet] = []
    for anchor_a in anchor_a_candidates:
        for anchor_b in anchor_b_candidates:
            if anchor_a.stop_id == anchor_b.stop_id:
                continue
            cset = _build_candidate_set(seed, grounding, anchor_a, anchor_b)
            cset.envelope_containment = _envelope_containment(
                [cset.anchor_a, *cset.intermediates.values(), cset.anchor_b],
                envelope,
            )
            cset.set_coherence_score = score_hint_set(
                cset, seed, envelope, route_context=route_context,
            )
            sets.append(cset)

    sets.sort(key=lambda item: item.set_coherence_score, reverse=True)
    return sets[:5]


def resolve_coherent_hint_set(
    grounding: TypedGroundingResult,
    seed: TypedRouteSeed,
    envelope: Optional[Dict[str, Any]],
    *,
    route_context: Optional[RouteContext] = None,
) -> Tuple[Optional[HintCandidateSet], List[HintCandidateSet]]:
    sets = _generate_candidate_sets(
        grounding, seed, envelope, route_context=route_context,
    )
    if not sets:
        fallback = _fallback_set(seed, grounding)
        if fallback is None:
            return None, []
        fallback.set_coherence_score = score_hint_set(
            fallback, seed, envelope, route_context=route_context,
        )
        return fallback, [fallback]
    return sets[0], sets


def serialize_hint_sets(sets: List[HintCandidateSet]) -> List[Dict[str, Any]]:
    return [hint_set.to_dict() for hint_set in sets]
