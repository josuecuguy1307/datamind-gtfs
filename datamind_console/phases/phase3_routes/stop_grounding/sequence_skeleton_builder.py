"""
Stage F — Sequence Skeleton Assembly

From scored and filtered corridor candidates, builds the ordered stop
sequence skeleton with gap detection, minimum spacing enforcement,
density thinning, marginal promotion, and confidence scoring.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    CorridorStopCandidate,
    SequenceSkeleton,
)
from datamind_console.phases.phase3_routes.stop_grounding.catalogs import get_config_section

# ---------------------------------------------------------------------------
# Constants (loaded from centralized config catalog)
# ---------------------------------------------------------------------------

_skeleton_cfg = get_config_section("skeleton")
MIN_SPACING_M = _skeleton_cfg.get("min_spacing_m", 80.0)
SEQUENCE_DEDUP_RADIUS_M = _skeleton_cfg.get("sequence_dedup_radius_m", 80.0)
TERMINUS_CLUSTER_RADIUS_M = _skeleton_cfg.get("terminus_cluster_radius_m", 250.0)
MAX_STOPS_PER_KM = _skeleton_cfg.get("max_stops_per_km", 5)
MARGINAL_PROMOTION_THRESHOLD = _skeleton_cfg.get("marginal_promotion_threshold", 0.28)


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2.0 * r * math.asin(math.sqrt(a))


import logging as _logging

_LOG = _logging.getLogger(__name__)


def _deduplicate_sequence_by_proximity(
    sequence: list,
    min_dist_m: float = SEQUENCE_DEDUP_RADIUS_M,
) -> tuple:
    """Remove consecutive stops within proximity threshold, keeping the higher-scored one.

    BUG-009 fix: Only the *first* and *last* stop in the sequence are truly
    protected from removal.  Other stops in the terminus zone
    (path_fraction < 0.05 or > 0.95) are compared against the terminus
    anchor (first/last stop) using TERMINUS_CLUSTER_RADIUS_M — if within
    that distance, they are collapsed.

    Returns (deduped_list, removed_count).
    """
    if len(sequence) <= 1:
        return sequence, 0

    first_stop = sequence[0]
    last_stop = sequence[-1]
    terminus_cluster_removed = 0
    deduped = [first_stop]
    removed = 0

    for idx, stop in enumerate(sequence[1:], start=1):
        is_last = (idx == len(sequence) - 1)

        if is_last:
            # Last stop is always protected — never remove
            deduped.append(stop)
            continue

        # BUG-009: In terminus start zone, compare against the first stop
        if stop.path_fraction < 0.05:
            dist_to_anchor = _haversine_m(first_stop.lon, first_stop.lat, stop.lon, stop.lat)
            if dist_to_anchor < TERMINUS_CLUSTER_RADIUS_M:
                removed += 1
                terminus_cluster_removed += 1
                continue

        # BUG-009: In terminus end zone, compare against the last stop
        if stop.path_fraction > 0.95:
            dist_to_anchor = _haversine_m(last_stop.lon, last_stop.lat, stop.lon, stop.lat)
            if dist_to_anchor < TERMINUS_CLUSTER_RADIUS_M:
                removed += 1
                terminus_cluster_removed += 1
                continue

        # General proximity dedup against previous kept stop
        prev = deduped[-1]
        dist = _haversine_m(prev.lon, prev.lat, stop.lon, stop.lat)
        if dist < min_dist_m:
            if prev is first_stop:
                # First stop is protected — discard current stop
                removed += 1
            elif (stop.on_route_score + (stop.lgbm_score or 0)) > (prev.on_route_score + (prev.lgbm_score or 0)):
                deduped[-1] = stop
                removed += 1
            else:
                removed += 1
        else:
            deduped.append(stop)

    if terminus_cluster_removed:
        _LOG.info(
            "[TERMINUS CLUSTER DEDUP] collapsed %d stops near terminus into 1",
            terminus_cluster_removed,
        )

    return deduped, removed


def _adaptive_gap_thresholds(corridor_length_km: float):
    """Route-length-adaptive gap thresholds."""
    if corridor_length_km < 10:
        return 600.0, 0.08
    elif corridor_length_km < 25:
        return 1000.0, 0.10
    else:
        return 1500.0, 0.12


def _enforce_minimum_spacing(
    ordered_stops: List[CorridorStopCandidate],
    min_spacing_m: float = MIN_SPACING_M,
) -> List[CorridorStopCandidate]:
    """
    Remove stops too close together. Keep the higher-scoring one.
    Always keep anchors/intermediates.
    Returns (thinned_list, removed_count).
    """
    if len(ordered_stops) <= 1:
        return ordered_stops

    thinned: List[CorridorStopCandidate] = [ordered_stops[0]]
    for stop in ordered_stops[1:]:
        prev = thinned[-1]
        dist_m = _haversine_m(prev.lon, prev.lat, stop.lon, stop.lat)
        if dist_m < min_spacing_m:
            if prev.is_known_anchor or prev.is_known_intermediate:
                continue  # keep prev, skip stop
            if stop.is_known_anchor or stop.is_known_intermediate:
                thinned[-1] = stop
                continue
            if stop.on_route_score > prev.on_route_score:
                thinned[-1] = stop
        else:
            thinned.append(stop)

    return thinned


def _thin_dense_segments(
    ordered_stops: List[CorridorStopCandidate],
    corridor_length_km: float,
    max_per_km: float = MAX_STOPS_PER_KM,
) -> List[CorridorStopCandidate]:
    """
    If a 1km window has too many stops, thin to keep only highest-scoring.
    Always protect anchors/intermediates.
    """
    if corridor_length_km <= 0 or len(ordered_stops) <= 4:
        return ordered_stops

    result = list(ordered_stops)
    window_frac = 1.0 / max(corridor_length_km, 1.0)

    changed = True
    max_iterations = 50
    iteration = 0
    while changed and iteration < max_iterations:
        changed = False
        iteration += 1
        i = 0
        while i < len(result):
            window_start = result[i].path_fraction
            window_end = window_start + window_frac
            window_indices = []
            j = i
            while j < len(result) and result[j].path_fraction <= window_end:
                window_indices.append(j)
                j += 1

            if len(window_indices) > max_per_km:
                protected = [
                    idx for idx in window_indices
                    if result[idx].is_known_anchor or result[idx].is_known_intermediate
                ]
                unprotected = [idx for idx in window_indices if idx not in protected]
                unprotected.sort(key=lambda idx: result[idx].on_route_score, reverse=True)

                slots_left = max(int(max_per_km) - len(protected), 0)
                keep = set(protected) | set(unprotected[:slots_left])
                remove = sorted(set(window_indices) - keep, reverse=True)

                if remove:
                    for idx in remove:
                        result.pop(idx)
                    changed = True
                    break
            i += 1

    return result


def assemble_sequence_skeleton(
    probable_on_route: List[CorridorStopCandidate],
    marginal: List[CorridorStopCandidate],
    rejected: List[CorridorStopCandidate],
    *,
    gap_threshold_m: float = 1000.0,
    gap_threshold_fraction: float = 0.1,
    corridor_length_km: float = 0.0,
) -> SequenceSkeleton:
    """
    Build a sequence skeleton from scored candidates.

    - probable_on_route: high-confidence stops, already sorted by path_fraction
    - marginal: borderline stops that may fill gaps
    - rejected: low-scoring stops (kept for reference)
    - corridor_length_km: used for adaptive thresholds and density thinning
    """
    skeleton = sorted(probable_on_route, key=lambda s: s.path_fraction)

    # === Step 0: Minimum skeleton bootstrap from marginals ===
    # When there are very few probable stops but many marginals, promote the
    # best marginals to ensure we have enough stops to work with.
    MIN_SKELETON_TARGET = 5
    if len(skeleton) < MIN_SKELETON_TARGET and marginal:
        # Promote top marginals by score, up to MIN_SKELETON_TARGET total
        sorted_marginals = sorted(marginal, key=lambda m: m.on_route_score, reverse=True)
        promotion_threshold = max(MARGINAL_PROMOTION_THRESHOLD, 0.20)
        promoted_bootstrap = 0
        for m in sorted_marginals:
            if len(skeleton) >= MIN_SKELETON_TARGET:
                break
            if m.on_route_score < promotion_threshold:
                break
            # Check spacing with existing skeleton stops
            too_close = any(
                _haversine_m(s.lon, s.lat, m.lon, m.lat) < MIN_SPACING_M
                for s in skeleton
            )
            if not too_close:
                skeleton.append(m)
                promoted_bootstrap += 1
        if promoted_bootstrap:
            skeleton.sort(key=lambda s: s.path_fraction)

    # === Step 1: Enforce minimum spacing (remove clusters) ===
    count_before_spacing = len(skeleton)
    skeleton = _enforce_minimum_spacing(skeleton, MIN_SPACING_M)
    stops_removed_by_spacing = count_before_spacing - len(skeleton)

    # === Step 1b: Proximity-based deduplication (BUG-006) ===
    skeleton, dedup_removed = _deduplicate_sequence_by_proximity(skeleton, SEQUENCE_DEDUP_RADIUS_M)
    if dedup_removed:
        _LOG.info("[DEDUP] removed %d duplicate stops, kept %d", dedup_removed, len(skeleton))

    # === Step 2: Density-based segment thinning ===
    effective_length = corridor_length_km if corridor_length_km > 0 else 0.0
    if effective_length > 0:
        count_before_density = len(skeleton)
        skeleton = _thin_dense_segments(skeleton, effective_length, MAX_STOPS_PER_KM)
        stops_removed_by_density = count_before_density - len(skeleton)
    else:
        stops_removed_by_density = 0

    # === Step 3: Adaptive gap thresholds ===
    if corridor_length_km > 0:
        gap_threshold_m, gap_threshold_fraction = _adaptive_gap_thresholds(corridor_length_km)

    # === Step 4: Detect gaps and check if marginal candidates can fill them ===
    gaps: List[Dict[str, Any]] = []
    for i in range(len(skeleton) - 1):
        gap_m = _haversine_m(
            skeleton[i].lon, skeleton[i].lat,
            skeleton[i + 1].lon, skeleton[i + 1].lat,
        )
        gap_frac = skeleton[i + 1].path_fraction - skeleton[i].path_fraction

        if gap_m > gap_threshold_m or gap_frac > gap_threshold_fraction:
            # Find marginal candidates that could fill this gap
            fillers = [
                m for m in marginal
                if skeleton[i].path_fraction < m.path_fraction < skeleton[i + 1].path_fraction
            ]
            gaps.append({
                "after_stop_id": skeleton[i].stop_id,
                "after_stop_name": skeleton[i].stop_name,
                "before_stop_id": skeleton[i + 1].stop_id,
                "before_stop_name": skeleton[i + 1].stop_name,
                "gap_m": round(gap_m, 1),
                "gap_fraction": round(gap_frac, 4),
                "potential_fillers": [
                    {
                        "stop_id": f.stop_id,
                        "stop_name": f.stop_name,
                        "on_route_score": round(f.on_route_score, 4),
                        "path_fraction": round(f.path_fraction, 6),
                    }
                    for f in fillers
                ],
                "filler_count": len(fillers),
            })

    # === Step 5: Promote marginal candidates into large gaps ===
    marginals_promoted = 0
    for gap in gaps:
        if gap["gap_m"] > gap_threshold_m and gap.get("potential_fillers"):
            best_filler_dict = max(gap["potential_fillers"], key=lambda f: f["on_route_score"])
            if best_filler_dict["on_route_score"] >= MARGINAL_PROMOTION_THRESHOLD:
                # Find the actual CorridorStopCandidate object
                filler_obj = next(
                    (m for m in marginal if m.stop_id == best_filler_dict["stop_id"]),
                    None,
                )
                if filler_obj:
                    insert_idx = next(
                        (idx for idx, s in enumerate(skeleton)
                         if s.path_fraction > filler_obj.path_fraction),
                        len(skeleton),
                    )
                    skeleton.insert(insert_idx, filler_obj)
                    gap["filled_by"] = filler_obj.stop_id
                    marginals_promoted += 1

    # === Step 6: Compute metrics ===
    total_length_km = 0.0
    spacing_values = []
    if len(skeleton) >= 2:
        for i in range(len(skeleton) - 1):
            d = _haversine_m(
                skeleton[i].lon, skeleton[i].lat,
                skeleton[i + 1].lon, skeleton[i + 1].lat,
            )
            spacing_values.append(d)
            total_length_km += d / 1000.0
    avg_spacing = sum(spacing_values) / len(spacing_values) if spacing_values else 0.0

    # Weak segments (very large gaps or very low density areas)
    weak_segments = [g for g in gaps if g["gap_m"] > 2000 or g.get("filler_count", 0) == 0]

    # Express/skip detection (unusually large gap with no fillers)
    express_candidates = [
        g for g in gaps
        if g["gap_m"] > 1500 and g.get("filler_count", 0) == 0
    ]

    # Sequence confidence (NO dense_bonus — over-density must not increase confidence)
    n_stops = len(skeleton)
    n_gaps = len(gaps)
    n_weak = len(weak_segments)
    if n_stops == 0:
        confidence = 0.0
    else:
        coverage = 1.0 - (n_gaps * 0.1)
        coverage -= n_weak * 0.15
        confidence = max(0.0, min(1.0, coverage))

    notes_parts = [f"{n_stops} stops"]
    if stops_removed_by_spacing:
        notes_parts.append(f"{stops_removed_by_spacing} removed by spacing")
    if dedup_removed:
        notes_parts.append(f"{dedup_removed} removed by proximity dedup")
    if stops_removed_by_density:
        notes_parts.append(f"{stops_removed_by_density} removed by density")
    if marginals_promoted:
        notes_parts.append(f"{marginals_promoted} marginal(s) promoted")
    if n_gaps:
        notes_parts.append(f"{n_gaps} gap(s)")
    if n_weak:
        notes_parts.append(f"{n_weak} weak segment(s)")
    if express_candidates:
        notes_parts.append(f"{len(express_candidates)} possible express skip(s)")

    return SequenceSkeleton(
        ordered_stops=skeleton,
        gaps=gaps,
        marginal_stops=marginal,
        rejected_stops=rejected,
        total_stops=n_stops,
        total_length_km=total_length_km,
        avg_stop_spacing_m=avg_spacing,
        sequence_confidence=confidence,
        weak_segments=weak_segments,
        express_skip_candidates=express_candidates,
        branch_variant_risk=[],
        notes=", ".join(notes_parts),
        stops_removed_by_spacing=stops_removed_by_spacing,
        stops_removed_by_density=stops_removed_by_density,
        marginals_promoted=marginals_promoted,
    )
