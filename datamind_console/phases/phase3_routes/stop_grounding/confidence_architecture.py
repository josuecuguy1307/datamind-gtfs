"""
BUG-014 — Two-Confidence Architecture

Separates the monolithic confidence score into three independent metrics:

1. geometry_confidence — corridor quality only (Valhalla + geography plausibility)
2. sequence_confidence — stop ordering quality only (monotonicity, spacing)
3. stop_coverage_score — fraction of corridor covered by discovered stops

Also provides the new route status derivation logic including `geometry_confirmed`.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    CorridorResult,
    CorridorStopCandidate,
    GeometryCandidate,
    SequenceSkeleton,
)

_LOG = logging.getLogger(__name__)


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2.0 * r * math.asin(math.sqrt(a))


def _zone_inflation_thresholds(zone: str) -> Tuple[float, float]:
    """Return (soft_threshold, hard_cap) for inflation ratio based on route zone.

    Rural and periurban routes legitimately have higher inflation ratios
    because road networks are sparser and more winding.
    """
    zone_lower = (zone or "").lower().strip()
    if zone_lower in ("rural", "rural_periurban", "rural_to_urban"):
        return 4.0, 6.0   # soft at 4.0, hard cap at 6.0
    elif zone_lower in ("periurban", "mixed"):
        return 3.5, 5.0   # soft at 3.5, hard cap at 5.0
    else:  # urban or unknown
        return 2.5, 4.0   # original thresholds


def compute_geometry_confidence(
    corridor: CorridorResult,
    *,
    terminus_reached_start: bool = True,
    terminus_reached_end: bool = True,
    zone: str = "",
) -> float:
    """
    Compute geometry confidence from corridor quality alone.

    Factors:
    - corridor_confidence (Valhalla routing quality)
    - geography_plausibility_score (envelope adherence)
    - corridor_inflation_ratio (penalize extreme detours, zone-aware)
    - terminus_reached (both ends reachable)

    Returns 0.0–1.0.
    """
    base = corridor.corridor_confidence
    geo_score = corridor.geography_plausibility_score

    # Zone-aware inflation ratio penalty
    inflation = corridor.corridor_inflation_ratio
    soft_threshold, hard_cap = _zone_inflation_thresholds(zone)
    if inflation > hard_cap:
        inflation_factor = 0.50
    elif inflation > soft_threshold:
        inflation_factor = 1.0 - (inflation - soft_threshold) / (hard_cap - soft_threshold + 1.0)
    else:
        inflation_factor = 1.0

    # Terminus penalty
    terminus_factor = 1.0
    if not terminus_reached_start:
        terminus_factor *= 0.85
    if not terminus_reached_end:
        terminus_factor *= 0.85

    confidence = base * geo_score * inflation_factor * terminus_factor

    if corridor.rejected_for_geographic_implausibility:
        confidence *= 0.10

    return max(0.0, min(1.0, confidence))


def compute_sequence_confidence(
    skeleton: SequenceSkeleton,
    corridor_length_km: float,
) -> float:
    """
    Compute sequence confidence from stop ordering quality only.

    Factors:
    - Monotonicity of path_fractions (should be strictly increasing)
    - Spacing regularity (std dev of inter-stop distances)
    - Anchor preservation (known anchors/intermediates present)
    - Total stop count relative to route length

    Does NOT penalize for gaps — that's stop_coverage_score's job.

    Returns 0.0–1.0.
    """
    stops = skeleton.ordered_stops
    if len(stops) < 2:
        return 0.0 if len(stops) == 0 else 0.30

    # Monotonicity: fraction of stop pairs that are strictly increasing in pf
    monotonic_pairs = 0
    total_pairs = len(stops) - 1
    for i in range(total_pairs):
        if stops[i + 1].path_fraction > stops[i].path_fraction:
            monotonic_pairs += 1
    monotonicity = monotonic_pairs / total_pairs

    # Spacing regularity: coefficient of variation of inter-stop distances
    spacings = []
    for i in range(len(stops) - 1):
        d = _haversine_m(stops[i].lon, stops[i].lat, stops[i + 1].lon, stops[i + 1].lat)
        spacings.append(d)

    mean_spacing = sum(spacings) / len(spacings) if spacings else 0.0
    if mean_spacing > 0:
        variance = sum((s - mean_spacing) ** 2 for s in spacings) / len(spacings)
        cv = math.sqrt(variance) / mean_spacing
        # CV < 0.5 = very regular, CV > 2.0 = very irregular
        regularity = max(0.0, min(1.0, 1.0 - (cv - 0.5) / 2.0))
    else:
        regularity = 0.5

    # Anchor preservation: boost if known anchors/intermediates present
    anchor_count = sum(1 for s in stops if s.is_known_anchor or s.is_known_intermediate)
    anchor_bonus = min(0.15, anchor_count * 0.05)

    # Stop density: expected ~2-4 stops/km for urban transit
    if corridor_length_km > 0:
        stops_per_km = len(stops) / corridor_length_km
        if stops_per_km < 0.5:
            density_factor = 0.60
        elif stops_per_km < 1.0:
            density_factor = 0.80
        elif stops_per_km > 8.0:
            density_factor = 0.75  # over-dense, possibly wrong
        else:
            density_factor = 1.0
    else:
        density_factor = 0.70

    confidence = (
        0.50 * monotonicity
        + 0.25 * regularity
        + 0.25 * density_factor
        + anchor_bonus
    )

    return max(0.0, min(1.0, confidence))


def compute_stop_coverage_score(
    skeleton: SequenceSkeleton,
    corridor_length_km: float,
) -> float:
    """
    Compute stop coverage score: how much of the corridor is covered by stops.

    Measures:
    - path_fraction extent (first_pf to last_pf)
    - Gap density (what fraction of corridor has gaps > adaptive threshold)
    - Absolute stop count vs route length

    Returns 0.0–1.0.
    """
    stops = skeleton.ordered_stops
    if not stops:
        return 0.0

    first_pf = stops[0].path_fraction
    last_pf = stops[-1].path_fraction
    extent = last_pf - first_pf  # 0.0–1.0

    # Penalize if stops don't reach route extremes
    head_coverage = max(0.0, 1.0 - first_pf * 5.0)  # pf=0.0→1.0, pf=0.2→0.0
    tail_coverage = max(0.0, 1.0 - (1.0 - last_pf) * 5.0)
    endpoint_coverage = (head_coverage + tail_coverage) / 2.0

    # Gap penalty from skeleton
    n_gaps = len(skeleton.gaps)
    n_weak = len(skeleton.weak_segments)
    gap_penalty = min(1.0, n_gaps * 0.08 + n_weak * 0.12)
    gap_coverage = max(0.0, 1.0 - gap_penalty)

    # Absolute minimum stops
    if len(stops) < 3:
        min_stop_factor = 0.30
    elif len(stops) < 5:
        min_stop_factor = 0.60
    else:
        min_stop_factor = 1.0

    coverage = (
        0.35 * extent
        + 0.25 * endpoint_coverage
        + 0.25 * gap_coverage
        + 0.15 * min_stop_factor
    )

    return max(0.0, min(1.0, coverage))


def derive_route_status(
    geometry_confidence: float,
    sequence_confidence: float,
    stop_coverage_score: float,
    *,
    discovered_stops: int = 0,
    rejected_for_geography: bool = False,
    geo_validation: Optional[Dict[str, Any]] = None,
    geography_plausibility_score: float = 0.0,
    terminus_reached_start: bool = True,
    terminus_reached_end: bool = True,
) -> str:
    """
    Derive route status from the three independent confidence metrics.

    Statuses:
    - geographically_implausible: corridor fails geography check
    - blocked: fundamental failure (<3 stops or very low scores)
    - geometry_confirmed: good corridor but sparse stop coverage
    - usable: all metrics above thresholds
    - weak: marginal quality
    """
    if rejected_for_geography:
        return "geographically_implausible"

    geo_valid = geo_validation or {}
    geo_valid_score = float(geo_valid.get("score") or 0.0)
    pass_through = float(geo_valid.get("pass_through_compliance") or 0.0)

    # BLOCKED: fundamental failure
    if discovered_stops < 3:
        # But if corridor is solid, it might be geometry_confirmed
        if geometry_confidence >= 0.40 and geography_plausibility_score >= 0.60:
            return "geometry_confirmed"
        return "blocked"

    if geo_valid_score > 0 and geo_valid_score < 0.30:
        return "blocked"

    # GEOMETRY_CONFIRMED: good corridor, sparse stops
    if (geometry_confidence >= 0.40
            and stop_coverage_score < 0.40
            and geography_plausibility_score >= 0.55):
        return "geometry_confirmed"

    # USABLE: all three metrics adequate
    if geo_valid_score > 0:
        # With geography catalog
        if (geo_valid_score >= 0.55
                and sequence_confidence >= 0.35
                and discovered_stops >= 5
                and pass_through >= 0.60
                and geometry_confidence >= 0.20):
            return "usable"
    else:
        # Without geography catalog
        if (geography_plausibility_score >= 0.72
                and sequence_confidence >= 0.40
                and geometry_confidence >= 0.20
                and discovered_stops >= 4):
            return "usable"

    # Also usable with relaxed thresholds
    if (geography_plausibility_score >= 0.60
            and discovered_stops >= 5
            and sequence_confidence >= 0.35
            and geometry_confidence >= 0.15):
        return "usable"

    # WEAK: marginal quality
    if geography_plausibility_score >= 0.48 and discovered_stops >= 3:
        return "weak"

    return "blocked"


def compute_all_confidences(
    corridor: CorridorResult,
    skeleton: SequenceSkeleton,
    *,
    terminus_reached_start: bool = True,
    terminus_reached_end: bool = True,
    zone: str = "",
) -> Dict[str, float]:
    """
    Compute all three confidence metrics at once.

    Returns dict with geometry_confidence, sequence_confidence, stop_coverage_score.
    """
    geo_conf = compute_geometry_confidence(
        corridor,
        terminus_reached_start=terminus_reached_start,
        terminus_reached_end=terminus_reached_end,
        zone=zone,
    )
    seq_conf = compute_sequence_confidence(
        skeleton,
        corridor_length_km=corridor.total_length_km or 0.0,
    )
    coverage = compute_stop_coverage_score(
        skeleton,
        corridor_length_km=corridor.total_length_km or 0.0,
    )

    _LOG.info(
        "[CONFIDENCE] geometry=%.3f, sequence=%.3f, coverage=%.3f",
        geo_conf, seq_conf, coverage,
    )

    return {
        "geometry_confidence": geo_conf,
        "sequence_confidence": seq_conf,
        "stop_coverage_score": coverage,
    }
