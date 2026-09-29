"""
Gap Analyzer — collects detection signals and emits MissingNodeCandidate[].

Two enabled triggers:
  1. Unfilled gaps from skeleton assembly (gaps with zero potential fillers)
  2. Synthetic terminus stops injected by tail_relaxation

Called after skeleton + tail relaxation are complete in the discovery pipeline.
"""
from __future__ import annotations

import logging
import math
import uuid
from typing import Any, Dict, List, Optional

from datamind_console.phases.phase3_routes.stop_grounding.backfill_contracts import (
    MissingNodeCandidate,
    SignalType,
)
from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    CorridorStopCandidate,
    SequenceSkeleton,
)

_LOG = logging.getLogger(__name__)


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2.0 * r * math.asin(math.sqrt(a))


def _midpoint_coords(
    stop_a: CorridorStopCandidate, stop_b: CorridorStopCandidate,
) -> tuple:
    """Midpoint between two stops (simple average — accurate enough for <5km)."""
    return (stop_a.lat + stop_b.lat) / 2.0, (stop_a.lon + stop_b.lon) / 2.0


MAX_BACKFILL_ROUTE_LENGTH_KM = 50.0


def _min_gap_for_backfill(corridor_length_km: float, median_leg_m: float = 0.0) -> float:
    """Zone-aware + route-length-aware minimum gap to trigger backfill.

    Uses both a fixed floor per zone and a median-leg multiplier so that
    routes with naturally longer spacing don't trigger excessive candidates.
    """
    if corridor_length_km < 10:
        return max(600.0, median_leg_m * 2.0)
    elif corridor_length_km < 25:
        return max(1000.0, median_leg_m * 2.5)
    else:
        return max(1500.0, median_leg_m * 3.0)


def _gap_confidence(gap_m: float, avg_spacing_m: float) -> float:
    """Larger gaps relative to route average spacing = higher confidence a stop is missing."""
    if avg_spacing_m <= 0:
        return 0.50
    ratio = gap_m / avg_spacing_m
    # 2× average → 0.60, 3× → 0.66, 5× → 0.78
    return min(0.90, 0.50 + ratio * 0.06)


def analyze_gaps(
    skeleton: SequenceSkeleton,
    synthetic_log: Dict[str, Any],
    probable_stops: List[CorridorStopCandidate],
    *,
    route_id: str,
    canton: str,
    province: str,
    corridor_length_km: float = 0.0,
) -> List[MissingNodeCandidate]:
    """Collect detection signals and emit backfill candidates.

    Parameters
    ----------
    skeleton : SequenceSkeleton
        Output of assemble_sequence_skeleton (contains gaps[]).
    synthetic_log : dict
        Output of inject_synthetic_terminus_stops (contains synthetic_start/end).
    probable_stops : list
        The probable stops list (may include synthetic terminus stops).
    route_id : str
        Route identifier for provenance.
    canton, province : str
        Geographic context for the backfill executor.
    corridor_length_km : float
        Total corridor length for threshold calculation.

    Returns
    -------
    list[MissingNodeCandidate]
        Candidates to feed to the P1.3B backfill executor.
    """
    if corridor_length_km > MAX_BACKFILL_ROUTE_LENGTH_KM:
        _LOG.info(
            "[GAP ANALYZER] route=%s: skipping (%.1f km > %.0f km intercity limit)",
            route_id, corridor_length_km, MAX_BACKFILL_ROUTE_LENGTH_KM,
        )
        return []

    # Compute median leg for adaptive thresholds
    _median_leg_m = 0.0
    if len(skeleton.ordered_stops) >= 2:
        legs = []
        for i in range(len(skeleton.ordered_stops) - 1):
            a, b = skeleton.ordered_stops[i], skeleton.ordered_stops[i + 1]
            legs.append(_haversine_m(a.lon, a.lat, b.lon, b.lat))
        if legs:
            legs_sorted = sorted(legs)
            _median_leg_m = legs_sorted[len(legs_sorted) // 2]

    candidates: List[MissingNodeCandidate] = []
    min_gap = _min_gap_for_backfill(corridor_length_km, _median_leg_m)

    # ---- Trigger 1: Unfilled gaps from skeleton ----
    for gap in skeleton.gaps:
        fillers = gap.get("potential_fillers", [])
        if len(fillers) > 0:
            continue  # marginals can fill this — skip
        gap_m = gap.get("gap_m", 0.0)
        if gap_m < min_gap:
            continue  # gap too small to justify backfill

        # Find the bounding stops in the skeleton to compute midpoint
        after_id = gap.get("after_stop_id")
        before_id = gap.get("before_stop_id")
        stop_a = next((s for s in skeleton.ordered_stops if s.stop_id == after_id), None)
        stop_b = next((s for s in skeleton.ordered_stops if s.stop_id == before_id), None)

        if stop_a is None or stop_b is None:
            continue

        mid_lat, mid_lon = _midpoint_coords(stop_a, stop_b)
        conf = _gap_confidence(gap_m, skeleton.avg_stop_spacing_m)

        candidates.append(MissingNodeCandidate(
            candidate_id=str(uuid.uuid4()),
            route_id=route_id,
            canton=canton,
            province=province,
            signal_type=SignalType.UNFILLED_GAP,
            expected_lat=mid_lat,
            expected_lon=mid_lon,
            search_radius_m=min(gap_m / 2.0, 500.0),
            gap_distance_m=gap_m,
            gap_start_stop_id=after_id,
            gap_end_stop_id=before_id,
            confidence=conf,
            context={
                "gap_fraction": gap.get("gap_fraction", 0.0),
                "after_stop_name": gap.get("after_stop_name", ""),
                "before_stop_name": gap.get("before_stop_name", ""),
            },
        ))

    # ---- Trigger 2: Synthetic terminus stops ----
    for key, label_key in [
        ("synthetic_start", "synthetic_start_label"),
        ("synthetic_end", "synthetic_end_label"),
    ]:
        if not synthetic_log.get(key):
            continue
        # Find the synthetic stop in probable_stops
        synthetic = next(
            (s for s in probable_stops if s.stop_source == "synthetic_known_facility"
             and s.stop_name and key.replace("synthetic_", "[Terminus] ").lower() in s.stop_name.lower()),
            None,
        )
        # Fallback: find ANY synthetic stop near head or tail
        if synthetic is None:
            target_pf = 0.0 if "start" in key else 1.0
            synthetics = [
                s for s in probable_stops
                if s.stop_source == "synthetic_known_facility"
            ]
            if synthetics:
                synthetic = min(synthetics, key=lambda s: abs(s.path_fraction - target_pf))

        if synthetic is None:
            continue

        candidates.append(MissingNodeCandidate(
            candidate_id=str(uuid.uuid4()),
            route_id=route_id,
            canton=canton,
            province=province,
            signal_type=SignalType.SYNTHETIC_TERMINUS,
            expected_lat=synthetic.lat,
            expected_lon=synthetic.lon,
            search_radius_m=300.0,
            confidence=0.60,
            context={
                "anchor_name": synthetic_log.get(label_key, ""),
                "synthetic_score": synthetic.on_route_score,
                "nearest_real_stop_m": synthetic_log.get(
                    f"nearest_real_stop_{key.replace('synthetic_', '')}_m", 0.0
                ),
            },
        ))

    if candidates:
        _LOG.info(
            "[GAP ANALYZER] route=%s: %d backfill candidates (%d unfilled gaps, %d synthetic termini)",
            route_id,
            len(candidates),
            sum(1 for c in candidates if c.signal_type == SignalType.UNFILLED_GAP),
            sum(1 for c in candidates if c.signal_type == SignalType.SYNTHETIC_TERMINUS),
        )

    return candidates
