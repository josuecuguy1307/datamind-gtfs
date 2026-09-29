"""
Stage E — On-route stop classifier.

Scores corridor-intersection candidates with a heuristic baseline and an
optional LightGBM model trained on the same feature vector.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    CorridorStopCandidate,
    TypedSeedToken,
)
from datamind_console.phases.phase3_routes.stop_grounding.catalogs import get_config_section

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Terminus protection — prevents aggressive pruning of endpoint waypoints
# ---------------------------------------------------------------------------

UNPRUNEABLE_POLICIES: Set[str] = {
    "resolve_to_best_matching_terminal_node",
    "resolve_to_best_matching_stop_node",
}

UNPRUNEABLE_ROLES: Set[str] = {
    "start", "end", "terminal", "anchor_primary",
    "origin_stop_candidate", "destination_stop_candidate",
    "origin_area", "destination_area",
}

# Terminus type → geography tolerance multiplier (applied to bbox)
_terminus_cfg = get_config_section("terminus")
_tolerance_by_type = _terminus_cfg.get("tolerance_by_type", {})
TERMINUS_TOLERANCE: Dict[str, float] = {
    "formal_terminal": _tolerance_by_type.get("formal_terminal", 1.0),
    "street_terminus": _tolerance_by_type.get("street_terminus", 1.40),
    "neighborhood_endpoint": _tolerance_by_type.get("neighborhood_endpoint", 1.25),
}

_NEIGHBORHOOD_KEYWORDS = {
    "barrio", "sector", "ciudadela",
    "urbanizacion", "urbanización", "lotización", "lotizacion",
    "cdla", "recinto", "comuna", "caserío", "caserio",
}


def infer_terminus_type(
    token: TypedSeedToken,
    resolved_node: object = None,
) -> str:
    """Infer terminus_type from token attributes if not explicitly set.

    Args:
        token: The seed token.
        resolved_node: Optional resolved OSM node object. If it has a ``tags``
            dict with ``public_transport=stop_area`` or ``amenity=bus_station``,
            the result is ``formal_terminal``.
    """
    if token.terminus_type:
        return token.terminus_type

    # Check resolved OSM node tags
    if resolved_node is not None:
        tags = getattr(resolved_node, "tags", None) or {}
        if isinstance(tags, dict):
            if tags.get("public_transport") == "stop_area" or tags.get("amenity") == "bus_station":
                return "formal_terminal"

    # formal_terminal: resolved against terminal/station nodes
    if token.kind in ("terminal_confirmed", "station_anchor"):
        return "formal_terminal"
    if token.role in ("terminal",):
        return "formal_terminal"
    if token.resolution_policy == "resolve_to_best_matching_terminal_node":
        return "formal_terminal"

    # neighborhood_endpoint: label contains neighborhood keywords
    label_lower = token.label.lower()
    if any(kw in label_lower for kw in _NEIGHBORHOOD_KEYWORDS):
        return "neighborhood_endpoint"

    # street_terminus: default for endpoints that don't match above
    if token.role in ("origin_stop_candidate", "destination_stop_candidate", "start", "end"):
        return "street_terminus"

    return "formal_terminal"


def is_terminus_protected(token: TypedSeedToken) -> bool:
    """Return True if this token should be protected from geography penalties.

    A token is protected if EITHER its resolution_policy OR its role indicates
    it is an endpoint / terminus waypoint.
    """
    if token.resolution_policy in UNPRUNEABLE_POLICIES:
        return True
    if token.role in UNPRUNEABLE_ROLES:
        return True
    return False

_MODEL_DIR = Path(__file__).parent / "models"
_DEFAULT_MODEL_PATH = str(_MODEL_DIR / "on_route_lgbm.txt")
_DEFAULT_META_PATH = str(_MODEL_DIR / "on_route_lgbm_meta.json")

FEATURE_COLUMNS = [
    "distance_to_corridor_m",
    "path_fraction",
    "discovery_buffer_m",
    "operator_match",
    "cooperative_match",
    "locality_match",
    "locality_consistency_score",
    "bearing_alignment_deg",
    "is_known_anchor",
    "is_known_intermediate",
    "stop_usage_frequency",
    "gap_to_previous_m",
    "gap_to_next_m",
    "local_stop_density",
    "distance_to_envelope_m",
    "heuristic_score",
    "in_required_area",
    "in_forbidden_area",
    "distance_to_nearest_required_area_m",
]

_scoring_cfg = get_config_section("scoring")
DEFAULT_ON_ROUTE_THRESHOLD = _scoring_cfg.get("on_route_threshold", 0.50)
DEFAULT_MARGINAL_THRESHOLD = _scoring_cfg.get("marginal_threshold", 0.32)

DEDUP_RADIUS_M = _scoring_cfg.get("dedup_radius_m", 40.0)

# ---------------------------------------------------------------------------
# BUG-008: Generic stop name penalty
# ---------------------------------------------------------------------------

GENERIC_STOP_NAMES = {
    "parada", "sin nombre", "(sin nombre)", "parada sin nombre",
    "unnamed", "bus stop", "paradero", "s/n", "sin nombre 10",
}

GENERIC_PREFIX_PATTERNS = [
    "parada ",  # "Parada 8", "Parada el Tingo", etc.
]


def generic_name_penalty(stop_name: str) -> float:
    """Return penalty multiplier 0.0–1.0 for generic/uninformative stop names.

    1.0 = no penalty (informative name).
    """
    name = (stop_name or "").strip().lower()
    if not name:
        return 0.55  # unnamed → same as generic
    if name in GENERIC_STOP_NAMES:
        return 0.55
    for prefix in GENERIC_PREFIX_PATTERNS:
        if name.startswith(prefix):
            return 0.75
    return 1.0


_lgbm_model = None
_lgbm_load_attempted = False
_meta_cache: Optional[Dict[str, Any]] = None


def _bearing_between(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_m = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * radius_m * math.asin(math.sqrt(a))


def _load_meta(model_meta_path: Optional[str] = None) -> Dict[str, Any]:
    global _meta_cache
    if _meta_cache is not None:
        return _meta_cache
    path = model_meta_path or os.getenv("STOP_GROUNDING_MODEL_META_PATH") or _DEFAULT_META_PATH
    try:
        _meta_cache = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        _meta_cache = {}
    return _meta_cache


def _blend_weights(meta: Dict[str, Any]) -> Tuple[float, float]:
    weights = meta.get("ensemble_weights") or {}
    if weights:
        return (
            float(weights.get("heuristic") or 0.35),
            float(weights.get("lgbm") or 0.65),
        )

    trainable_routes = int(meta.get("trainable_routes") or 0)
    if trainable_routes < 5:
        return 0.65, 0.35
    if trainable_routes <= 10:
        return 0.50, 0.50
    return 0.35, 0.65


def _thresholds(meta: Dict[str, Any]) -> Tuple[float, float]:
    on_route = float(meta.get("on_route_threshold") or DEFAULT_ON_ROUTE_THRESHOLD)
    marginal = float(meta.get("marginal_threshold") or max(DEFAULT_MARGINAL_THRESHOLD, on_route * 0.62))
    return on_route, marginal


_hw = _scoring_cfg.get("heuristic_weights", {})
_HW_DISTANCE = _hw.get("distance_score", 0.34)
_HW_BUFFER = _hw.get("buffer_score", 0.08)
_HW_LOCALITY_CONSISTENCY = _hw.get("locality_consistency", 0.08)
_HW_USAGE_FREQ = _hw.get("stop_usage_frequency", 0.06)
_HW_OPERATOR = _hw.get("operator_match", 0.10)
_HW_COOPERATIVE = _hw.get("cooperative_match", 0.05)
_HW_LOCALITY = _hw.get("locality_match", 0.07)
_HW_BEARING = _hw.get("bearing_alignment", 0.10)
_HW_KNOWN = _hw.get("known_anchor_or_intermediate", 0.12)
_DIST_DECAY_DIVISOR = _scoring_cfg.get("distance_decay_divisor", 180.0)
_DIST_DECAY_EXP = _scoring_cfg.get("distance_decay_exponent", 1.15)
_BUFFER_BASE = _scoring_cfg.get("buffer_base", 50.0)
_BUFFER_RANGE = _scoring_cfg.get("buffer_range", 350.0)
_USAGE_FREQ_CAP = _scoring_cfg.get("usage_frequency_cap", 6.0)
_gp = _scoring_cfg.get("geography_penalties", {})
_GP_OUTSIDE_NORMAL = _gp.get("outside_expected_normal", 0.18)
_GP_OUTSIDE_TERMINUS = _gp.get("outside_expected_terminus_protected", 0.70)
_GP_FAR_ENVELOPE = _gp.get("far_from_envelope_multiplier", 0.72)
_GP_FAR_ENVELOPE_DIST = _gp.get("far_from_envelope_distance_m", 250.0)
_GP_FORBIDDEN_NORMAL = _gp.get("in_forbidden_area_normal", 0.05)
_GP_FORBIDDEN_TERMINUS = _gp.get("in_forbidden_area_terminus_protected", 0.50)
_GP_REQUIRED_BOOST = _gp.get("in_required_area_boost", 1.10)


def heuristic_on_route_score(
    candidate: CorridorStopCandidate,
    *,
    terminus_protected: bool = False,
) -> float:
    score = 0.0

    distance_score = max(0.0, 1.0 - (candidate.distance_to_corridor_m / _DIST_DECAY_DIVISOR) ** _DIST_DECAY_EXP)
    score += _HW_DISTANCE * distance_score

    buffer_score = max(0.0, 1.0 - (candidate.discovery_buffer_m - _BUFFER_BASE) / _BUFFER_RANGE)
    score += _HW_BUFFER * buffer_score

    score += _HW_LOCALITY_CONSISTENCY * min(1.0, candidate.locality_consistency_score)
    score += _HW_USAGE_FREQ * min(1.0, candidate.stop_usage_frequency / _USAGE_FREQ_CAP)
    score += _HW_OPERATOR * (1.0 if candidate.operator_match else 0.0)
    score += _HW_COOPERATIVE * (1.0 if candidate.cooperative_match else 0.0)
    score += _HW_LOCALITY * (1.0 if candidate.locality_match else 0.0)
    score += _HW_BEARING * max(0.0, candidate.bearing_alignment_deg / 90.0)
    score += _HW_KNOWN * (1.0 if candidate.is_known_anchor or candidate.is_known_intermediate else 0.0)

    # Geography penalties — skip for terminus-protected stops
    if not terminus_protected:
        if not candidate.in_expected_geography:
            score *= _GP_OUTSIDE_NORMAL
        elif candidate.distance_to_envelope_m > _GP_FAR_ENVELOPE_DIST:
            score *= _GP_FAR_ENVELOPE

        # Geography catalog modifiers
        if candidate.in_forbidden_area:
            score *= _GP_FORBIDDEN_NORMAL
    else:
        # Protected terminus: mild penalty instead of aggressive pruning
        if not candidate.in_expected_geography:
            score *= _GP_OUTSIDE_TERMINUS
        if candidate.in_forbidden_area:
            score *= _GP_FORBIDDEN_TERMINUS

    if candidate.in_required_area:
        score *= _GP_REQUIRED_BOOST

    return max(0.0, min(1.0, score))


def _load_lgbm_model(model_path: Optional[str] = None):
    global _lgbm_model, _lgbm_load_attempted
    if _lgbm_load_attempted:
        return _lgbm_model
    _lgbm_load_attempted = True

    path = model_path or os.getenv("STOP_GROUNDING_MODEL_PATH") or _DEFAULT_MODEL_PATH
    if not os.path.exists(path):
        _LOG.info("LightGBM model not found at %s; using heuristic scoring", path)
        return None

    try:
        import lightgbm as lgb

        _lgbm_model = lgb.Booster(model_file=path)
        _LOG.info("Loaded LightGBM model from %s", path)
        return _lgbm_model
    except Exception as exc:
        _LOG.warning("Failed to load LightGBM model: %s", exc)
        return None


def _apply_bearing_alignment(
    candidates: List[CorridorStopCandidate],
    corridor_geojson: Optional[Dict[str, Any]],
) -> None:
    coords = list((corridor_geojson or {}).get("coordinates") or [])
    if len(coords) < 2:
        for candidate in candidates:
            candidate.bearing_alignment_deg = 0.0
        return

    for candidate in candidates:
        est_idx = max(0, min(int(candidate.path_fraction * (len(coords) - 1)), len(coords) - 2))
        search_lo = max(0, est_idx - 3)
        search_hi = min(len(coords) - 1, est_idx + 4)
        best_seg = est_idx
        best_dist = float("inf")
        for seg_idx in range(search_lo, search_hi):
            dist = _haversine_m(coords[seg_idx][0], coords[seg_idx][1], candidate.lon, candidate.lat)
            if dist < best_dist:
                best_dist = dist
                best_seg = seg_idx
        seg_idx = min(best_seg, len(coords) - 2)
        corridor_bearing = _bearing_between(
            coords[seg_idx][0],
            coords[seg_idx][1],
            coords[seg_idx + 1][0],
            coords[seg_idx + 1][1],
        )
        approach_bearing = _bearing_between(candidate.lon, candidate.lat, coords[seg_idx][0], coords[seg_idx][1])
        delta = abs(corridor_bearing - approach_bearing) % 360.0
        if delta > 180.0:
            delta = 360.0 - delta
        candidate.bearing_alignment_deg = 90.0 - abs(delta - 90.0)


def _context_features(
    candidates: List[CorridorStopCandidate],
    idx: int,
) -> Tuple[float, float, float]:
    current = candidates[idx]
    gap_to_previous_m = 0.0
    gap_to_next_m = 0.0
    if idx > 0:
        prev = candidates[idx - 1]
        gap_to_previous_m = _haversine_m(prev.lon, prev.lat, current.lon, current.lat)
    if idx < len(candidates) - 1:
        nxt = candidates[idx + 1]
        gap_to_next_m = _haversine_m(current.lon, current.lat, nxt.lon, nxt.lat)

    density = 0.0
    for j in range(max(0, idx - 20), min(len(candidates), idx + 21)):
        if j == idx:
            continue
        if _haversine_m(candidates[j].lon, candidates[j].lat, current.lon, current.lat) <= 200.0:
            density += 1.0
    return gap_to_previous_m, gap_to_next_m, density


def _lgbm_features_for_candidate(
    candidate: CorridorStopCandidate,
    idx: int,
    candidates: List[CorridorStopCandidate],
) -> List[float]:
    gap_to_previous_m, gap_to_next_m, local_density = _context_features(candidates, idx)
    return [
        candidate.distance_to_corridor_m,
        candidate.path_fraction,
        float(candidate.discovery_buffer_m),
        float(int(candidate.operator_match)),
        float(int(candidate.cooperative_match)),
        float(int(candidate.locality_match)),
        candidate.locality_consistency_score,
        candidate.bearing_alignment_deg,
        float(int(candidate.is_known_anchor)),
        float(int(candidate.is_known_intermediate)),
        float(candidate.stop_usage_frequency),
        gap_to_previous_m,
        gap_to_next_m,
        local_density,
        candidate.distance_to_envelope_m,
        heuristic_on_route_score(candidate),
        float(int(candidate.in_required_area)),
        float(int(candidate.in_forbidden_area)),
        candidate.distance_to_nearest_required_area_m,
    ]


def lgbm_on_route_score(
    candidates: List[CorridorStopCandidate],
    *,
    model_path: Optional[str] = None,
) -> Optional[List[float]]:
    if not candidates:
        return []
    model = _load_lgbm_model(model_path)
    if model is None:
        return None

    import numpy as np

    features = np.array(
        [_lgbm_features_for_candidate(candidate, idx, candidates) for idx, candidate in enumerate(candidates)]
    )
    if features.ndim < 2:
        return None
    return model.predict(features).tolist()


def score_candidates(
    candidates: List[CorridorStopCandidate],
    *,
    corridor_geojson: Optional[Dict[str, Any]] = None,
    scoring_mode: str = "ensemble",
    route_length_km: float = 0.0,
    route_n_stops: int = 0,
    terminus_protected_ids: Optional[Set[str]] = None,
) -> Tuple[List[CorridorStopCandidate], List[CorridorStopCandidate], List[CorridorStopCandidate]]:
    del route_length_km
    del route_n_stops

    if not candidates:
        return [], [], []

    protected_ids = terminus_protected_ids or set()

    meta = _load_meta()
    heuristic_weight, lgbm_weight = _blend_weights(meta)
    on_route_threshold, marginal_threshold = _thresholds(meta)

    _apply_bearing_alignment(candidates, corridor_geojson)

    for candidate in candidates:
        candidate.on_route_score = heuristic_on_route_score(
            candidate,
            terminus_protected=(candidate.stop_id in protected_ids),
        )

    lgbm_scores = None
    if scoring_mode in ("lgbm", "ensemble"):
        lgbm_scores = lgbm_on_route_score(candidates)

    if lgbm_scores is not None:
        if scoring_mode == "lgbm":
            for candidate, score in zip(candidates, lgbm_scores):
                candidate.lgbm_score = score
                candidate.on_route_score = score
        elif scoring_mode == "ensemble":
            for candidate, score in zip(candidates, lgbm_scores):
                candidate.lgbm_score = score
                candidate.on_route_score = heuristic_weight * candidate.on_route_score + lgbm_weight * score
        _LOG.info(
            "Scoring mode=%s using heuristic/lgbm weights %.2f/%.2f and threshold %.2f",
            scoring_mode,
            heuristic_weight,
            lgbm_weight,
            on_route_threshold,
        )
    elif scoring_mode != "heuristic":
        _LOG.info("LightGBM unavailable; falling back to heuristic")

    # BUG-008: Apply generic name penalty (skip for terminus-protected stops)
    for candidate in candidates:
        if candidate.stop_id not in protected_ids:
            penalty = generic_name_penalty(candidate.stop_name)
            if penalty < 1.0:
                candidate.on_route_score *= penalty
                _LOG.debug(
                    "[GENERIC PENALTY] stop=%r, penalty=%.2f, final_score=%.4f",
                    candidate.stop_name, penalty, candidate.on_route_score,
                )

    deduped = _spatial_dedup(candidates, radius_m=DEDUP_RADIUS_M)

    probable: List[CorridorStopCandidate] = []
    marginal: List[CorridorStopCandidate] = []
    rejected: List[CorridorStopCandidate] = []

    for candidate in deduped:
        is_protected = candidate.stop_id in protected_ids

        # Terminus-protected stops bypass geography rejection
        if is_protected:
            probable.append(candidate)
        elif not candidate.in_expected_geography and not candidate.is_known_anchor and not candidate.is_known_intermediate:
            candidate.rejection_reasons = list(
                dict.fromkeys(list(candidate.rejection_reasons or []) + ["outside_expected_geography"])
            )
            rejected.append(candidate)
        elif candidate.is_known_anchor or candidate.is_known_intermediate:
            probable.append(candidate)
        elif candidate.on_route_score >= on_route_threshold:
            probable.append(candidate)
        elif candidate.on_route_score >= marginal_threshold:
            marginal.append(candidate)
        else:
            rejected.append(candidate)

    probable.sort(key=lambda item: item.path_fraction)
    marginal.sort(key=lambda item: item.path_fraction)
    rejected.sort(key=lambda item: item.path_fraction)

    _LOG.info(
        "Scored %d candidates (deduped from %d): %d probable, %d marginal, %d rejected (terminus_protected=%d)",
        len(deduped),
        len(candidates),
        len(probable),
        len(marginal),
        len(rejected),
        len(protected_ids),
    )

    return probable, marginal, rejected


def _spatial_dedup(
    candidates: List[CorridorStopCandidate],
    *,
    radius_m: float = DEDUP_RADIUS_M,
) -> List[CorridorStopCandidate]:
    if not candidates:
        return []

    sorted_candidates = sorted(candidates, key=lambda item: item.path_fraction)
    kept: List[CorridorStopCandidate] = [sorted_candidates[0]]
    for candidate in sorted_candidates[1:]:
        last = kept[-1]
        if _haversine_m(last.lon, last.lat, candidate.lon, candidate.lat) < radius_m:
            if candidate.on_route_score > last.on_route_score:
                kept[-1] = candidate
        else:
            kept.append(candidate)
    return kept
