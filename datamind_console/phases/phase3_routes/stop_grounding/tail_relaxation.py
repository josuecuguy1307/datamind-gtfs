"""
BUG-012 — Tail Relaxation + Synthetic Terminus Stops

Two related features:

1. Tail Relaxation: In the last 25% of the route (path_fraction > 0.75),
   relax the on_route score threshold to MIN_SCORE_TAIL (0.32) to rescue
   stops that would otherwise be rejected. Also injects a terminus filler
   if the last stop is >1500m from the terminus.

2. Synthetic Terminus Stops: Inject a synthetic stop at each terminus
   anchor when no real stop exists within 300m. Score=0.70,
   source=synthetic_known_facility.

Both are applied AFTER scoring, BEFORE confidence computation.
"""
from __future__ import annotations

import logging
import math
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    CorridorResult,
    CorridorStopCandidate,
    TypedRouteSeed,
)
from datamind_console.phases.phase3_routes.stop_grounding.catalogs import get_config_section

_LOG = logging.getLogger(__name__)

# BUG-012 constants (loaded from centralized config catalog)
_tail_cfg = get_config_section("tail_relaxation")
MIN_SCORE_TAIL = _tail_cfg.get("min_score_tail", 0.32)
TAIL_ZONE_START = _tail_cfg.get("tail_zone_start", 0.75)
HEAD_ZONE_END = _tail_cfg.get("head_zone_end", 0.25)
TERMINUS_FILLER_DISTANCE_M = _tail_cfg.get("terminus_filler_distance_m", 1500.0)

# Synthetic terminus constants
SYNTHETIC_PROXIMITY_M = _tail_cfg.get("synthetic_proximity_m", 300.0)
SYNTHETIC_SCORE = _tail_cfg.get("synthetic_score", 0.70)

LonLat = Tuple[float, float]


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2.0 * r * math.asin(math.sqrt(a))


def _find_path_fraction_for_point(
    coords: List[LonLat], target_lon: float, target_lat: float,
) -> float:
    """Estimate the path_fraction of a point along the corridor."""
    if not coords:
        return 0.0
    best_idx = 0
    best_dist = float("inf")
    for i, (lon, lat) in enumerate(coords):
        d = _haversine_m(lon, lat, target_lon, target_lat)
        if d < best_dist:
            best_dist = d
            best_idx = i
    return best_idx / max(len(coords) - 1, 1)


def _extract_terminus_coords(
    seed: TypedRouteSeed,
) -> Tuple[Optional[LonLat], Optional[LonLat], Optional[str], Optional[str]]:
    """Extract start/end terminus coords and labels from typed seed tokens."""
    start_coord: Optional[LonLat] = None
    end_coord: Optional[LonLat] = None
    start_label: Optional[str] = None
    end_label: Optional[str] = None

    for token in seed.sequence_tokens:
        if not token.has_anchor_coords:
            continue
        role = token.role.lower() if token.role else ""
        if token.anchor_role == "terminus" or role in (
            "origin_stop_candidate", "destination_stop_candidate", "start", "end",
        ):
            if role in ("origin_stop_candidate", "start", "origin_area") and start_coord is None:
                start_coord = (token.anchor_lon, token.anchor_lat)
                start_label = token.label
            elif role in ("destination_stop_candidate", "end", "destination_area") and end_coord is None:
                end_coord = (token.anchor_lon, token.anchor_lat)
                end_label = token.label

    return start_coord, end_coord, start_label, end_label


def apply_tail_relaxation(
    probable: List[CorridorStopCandidate],
    marginal: List[CorridorStopCandidate],
    rejected: List[CorridorStopCandidate],
    *,
    corridor: CorridorResult,
    seed: TypedRouteSeed,
    terminus_protected_ids: Optional[Set[str]] = None,
) -> Tuple[List[CorridorStopCandidate], List[CorridorStopCandidate], List[CorridorStopCandidate], Dict[str, Any]]:
    """
    BUG-012: Relax score thresholds in the tail (last 25%) and head (first 25%)
    of the route to rescue marginally-scored stops near termini.

    Returns (updated_probable, updated_marginal, updated_rejected, log_dict).
    """
    log: Dict[str, Any] = {"rescued_tail": 0, "rescued_head": 0, "terminus_filler_injected": False}

    if not corridor.corridor_geojson:
        return probable, marginal, rejected, log

    coords = corridor.corridor_geojson.get("coordinates", [])
    corridor_km = corridor.total_length_km or 0.0
    _, end_coord, _, end_label = _extract_terminus_coords(seed)

    # Rescue marginal/rejected stops in tail zone
    rescued = []
    remaining_marginal = []
    remaining_rejected = []

    for stop in marginal:
        if stop.path_fraction >= TAIL_ZONE_START and stop.on_route_score >= MIN_SCORE_TAIL:
            rescued.append(stop)
            log["rescued_tail"] += 1
            _LOG.info(
                "[TAIL RELAX] Rescued marginal stop '%s' pf=%.3f score=%.3f",
                stop.stop_name, stop.path_fraction, stop.on_route_score,
            )
        elif stop.path_fraction <= HEAD_ZONE_END and stop.on_route_score >= MIN_SCORE_TAIL:
            rescued.append(stop)
            log["rescued_head"] += 1
            _LOG.info(
                "[HEAD RELAX] Rescued marginal stop '%s' pf=%.3f score=%.3f",
                stop.stop_name, stop.path_fraction, stop.on_route_score,
            )
        else:
            remaining_marginal.append(stop)

    for stop in rejected:
        if stop.path_fraction >= TAIL_ZONE_START and stop.on_route_score >= MIN_SCORE_TAIL:
            rescued.append(stop)
            log["rescued_tail"] += 1
            _LOG.info(
                "[TAIL RELAX] Rescued rejected stop '%s' pf=%.3f score=%.3f",
                stop.stop_name, stop.path_fraction, stop.on_route_score,
            )
        elif stop.path_fraction <= HEAD_ZONE_END and stop.on_route_score >= MIN_SCORE_TAIL:
            rescued.append(stop)
            log["rescued_head"] += 1
            _LOG.info(
                "[HEAD RELAX] Rescued rejected stop '%s' pf=%.3f score=%.3f",
                stop.stop_name, stop.path_fraction, stop.on_route_score,
            )
        else:
            remaining_rejected.append(stop)

    updated_probable = sorted(probable + rescued, key=lambda s: s.path_fraction)

    # Terminus filler: check if last probable stop is too far from end terminus
    if end_coord and updated_probable and corridor_km > 0:
        last_stop = updated_probable[-1]
        dist_to_terminus = _haversine_m(last_stop.lon, last_stop.lat, end_coord[0], end_coord[1])
        if dist_to_terminus > TERMINUS_FILLER_DISTANCE_M:
            _LOG.info(
                "[TAIL FILLER NEEDED] Last stop '%s' is %.0fm from terminus '%s' (>%.0fm)",
                last_stop.stop_name, dist_to_terminus, end_label or "end", TERMINUS_FILLER_DISTANCE_M,
            )
            log["terminus_filler_needed"] = True
            log["terminus_filler_distance_m"] = round(dist_to_terminus, 1)

    _LOG.info(
        "[TAIL RELAX] Total rescued: %d tail + %d head = %d stops",
        log["rescued_tail"], log["rescued_head"],
        log["rescued_tail"] + log["rescued_head"],
    )

    return updated_probable, remaining_marginal, remaining_rejected, log


def inject_synthetic_terminus_stops(
    probable: List[CorridorStopCandidate],
    *,
    corridor: CorridorResult,
    seed: TypedRouteSeed,
) -> Tuple[List[CorridorStopCandidate], Dict[str, Any]]:
    """
    Inject synthetic stops at terminus anchors when no real stop exists within
    SYNTHETIC_PROXIMITY_M. The synthetic stop gets score=0.70 and
    source=synthetic_known_facility.

    Returns (updated_probable, log_dict).
    """
    log: Dict[str, Any] = {"synthetic_start": False, "synthetic_end": False}

    if not corridor.corridor_geojson:
        return probable, log

    coords = corridor.corridor_geojson.get("coordinates", [])
    start_coord, end_coord, start_label, end_label = _extract_terminus_coords(seed)
    updated = list(probable)

    # Check start terminus
    if start_coord is not None:
        nearest_start_dist = float("inf")
        for stop in updated:
            d = _haversine_m(stop.lon, stop.lat, start_coord[0], start_coord[1])
            if d < nearest_start_dist:
                nearest_start_dist = d

        if nearest_start_dist > SYNTHETIC_PROXIMITY_M:
            pf = _find_path_fraction_for_point(coords, start_coord[0], start_coord[1])
            synthetic = CorridorStopCandidate(
                stop_id=f"synthetic:{uuid.uuid4().hex[:12]}",
                stop_name=f"[Terminus] {start_label or 'Start'}",
                lat=start_coord[1],
                lon=start_coord[0],
                distance_to_corridor_m=0.0,
                path_fraction=max(0.0, pf),
                is_known_anchor=True,
                on_route_score=SYNTHETIC_SCORE,
                in_expected_geography=True,
                stop_source="synthetic_known_facility",
            )
            updated.append(synthetic)
            log["synthetic_start"] = True
            log["synthetic_start_label"] = start_label
            log["nearest_real_stop_start_m"] = round(min(nearest_start_dist, 999999.0), 1)
            _LOG.info(
                "[SYNTHETIC TERMINUS START] Injected '%s' at pf=%.3f (nearest real stop %.0fm away)",
                synthetic.stop_name, pf, nearest_start_dist,
            )

    # Check end terminus
    if end_coord is not None:
        nearest_end_dist = float("inf")
        for stop in updated:
            d = _haversine_m(stop.lon, stop.lat, end_coord[0], end_coord[1])
            if d < nearest_end_dist:
                nearest_end_dist = d

        if nearest_end_dist > SYNTHETIC_PROXIMITY_M:
            pf = _find_path_fraction_for_point(coords, end_coord[0], end_coord[1])
            synthetic = CorridorStopCandidate(
                stop_id=f"synthetic:{uuid.uuid4().hex[:12]}",
                stop_name=f"[Terminus] {end_label or 'End'}",
                lat=end_coord[1],
                lon=end_coord[0],
                distance_to_corridor_m=0.0,
                path_fraction=min(1.0, pf),
                is_known_anchor=True,
                on_route_score=SYNTHETIC_SCORE,
                in_expected_geography=True,
                stop_source="synthetic_known_facility",
            )
            updated.append(synthetic)
            log["synthetic_end"] = True
            log["synthetic_end_label"] = end_label
            log["nearest_real_stop_end_m"] = round(min(nearest_end_dist, 999999.0), 1)
            _LOG.info(
                "[SYNTHETIC TERMINUS END] Injected '%s' at pf=%.3f (nearest real stop %.0fm away)",
                synthetic.stop_name, pf, nearest_end_dist,
            )

    updated.sort(key=lambda s: s.path_fraction)
    return updated, log
