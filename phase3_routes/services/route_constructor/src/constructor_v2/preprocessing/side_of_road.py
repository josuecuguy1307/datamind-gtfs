"""Side-of-road preprocessing: direction alignment, duplicate collapse, and outlier detection.

Cleans the stop set BEFORE ordering by flagging opposite-direction stops,
collapsing near-duplicates, and detecting spatial outliers.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Any, Sequence

from hades.geometry.canonical import OPPOSITION_DEG_THRESHOLD
from src.constructor_v2.clients.valhalla_client import ValhallaClient
from src.constructor_v2.common import angular_delta_deg, bearing_deg, haversine_m, normalize_name
from src.constructor_v2.schemas.route_input import NormalizedStop

logger = logging.getLogger(__name__)

# --- Configuration defaults ---
DIRECTION_OPPOSITION_THRESHOLD_DEG = OPPOSITION_DEG_THRESHOLD
DUPLICATE_DISTANCE_M = 30.0
DUPLICATE_NAME_SIMILARITY_THRESHOLD = 0.7
OUTLIER_FACTOR = 4.0

# Skip penalties for flagged stops
PENALTY_OPPOSITE_DIRECTION = 100
PENALTY_DUPLICATE = 200
PENALTY_OUTLIER = 300


@dataclass(slots=True)
class PreprocessingResult:
    cleaned_stops: list[NormalizedStop]
    removed_stop_ids: list[str]
    flagged_reasons: dict[str, str]
    stats: dict[str, Any]
    locate_cache: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass(slots=True)
class PreprocessorConfig:
    direction_threshold_deg: float = DIRECTION_OPPOSITION_THRESHOLD_DEG
    duplicate_distance_m: float = DUPLICATE_DISTANCE_M
    duplicate_name_threshold: float = DUPLICATE_NAME_SIMILARITY_THRESHOLD
    outlier_factor: float = OUTLIER_FACTOR
    enable_direction_filter: bool = True
    enable_duplicate_filter: bool = True
    enable_outlier_filter: bool = True


class SideOfRoadPreprocessor:
    def __init__(
        self,
        client: ValhallaClient,
        config: PreprocessorConfig | None = None,
    ) -> None:
        self.client = client
        self.config = config or PreprocessorConfig()
        self._locate_cache: dict[str, list[dict[str, Any]]] = {}

    def preprocess(
        self,
        stops: Sequence[NormalizedStop],
        *,
        corridor_bearing: float | None = None,
    ) -> PreprocessingResult:
        if len(stops) < 2:
            return PreprocessingResult(
                cleaned_stops=list(stops),
                removed_stop_ids=[],
                flagged_reasons={},
                stats={"total": len(stops), "opposite_direction": 0, "duplicates": 0, "outliers": 0},
            )

        # Compute corridor bearing from start to end if not given
        start, end = stops[0], stops[-1]
        if corridor_bearing is None:
            corridor_bearing = bearing_deg(start.lat, start.lon, end.lat, end.lon)

        flagged_reasons: dict[str, str] = {}
        opposite_count = 0
        duplicate_count = 0
        outlier_count = 0

        # Collect terminus and anchor IDs — these are NEVER flagged
        protected_ids = {
            stop.stop_id
            for stop in stops
            if stop.is_fixed_start or stop.is_fixed_end or stop.is_known_anchor
        }

        # --- 1. Direction alignment filter ---
        locate_results = self._batch_locate(stops)
        if self.config.enable_direction_filter and locate_results:
            for stop, loc_result in zip(stops, locate_results):
                if stop.stop_id in protected_ids:
                    continue
                if not loc_result or not loc_result.get("edges"):
                    continue
                edges = loc_result["edges"]
                best_edge = self._best_matching_edge(edges)
                if best_edge is None:
                    continue
                edge_heading = best_edge.get("heading")
                if edge_heading is None:
                    continue
                delta = angular_delta_deg(float(edge_heading), corridor_bearing)
                if delta > self.config.direction_threshold_deg:
                    if stop.stop_id not in flagged_reasons:
                        flagged_reasons[stop.stop_id] = (
                            f"opposite_direction: edge_heading={edge_heading:.0f} "
                            f"corridor={corridor_bearing:.0f} delta={delta:.0f}°"
                        )
                        stop.weak_candidate = True
                        stop.optional_penalty = min(
                            stop.optional_penalty or PENALTY_OPPOSITE_DIRECTION,
                            PENALTY_OPPOSITE_DIRECTION,
                        )
                        opposite_count += 1
                        logger.info(
                            "Flagged opposite-direction: %s (%s) delta=%.0f°",
                            stop.stop_id,
                            stop.stop_name,
                            delta,
                        )

        # --- 2. Duplicate collapse ---
        if self.config.enable_duplicate_filter:
            duplicate_groups = self._find_duplicate_groups(stops, locate_results)
            for group in duplicate_groups:
                if len(group) < 2:
                    continue
                # Keep the stop closer to road centerline (lower snap distance from /locate)
                keeper = self._pick_keeper(group, locate_results, stops)
                for stop in group:
                    if stop.stop_id == keeper.stop_id:
                        continue
                    if stop.stop_id in protected_ids:
                        continue
                    if stop.stop_id not in flagged_reasons:
                        flagged_reasons[stop.stop_id] = f"duplicate_of={keeper.stop_id}"
                        stop.weak_candidate = True
                        stop.optional_penalty = min(
                            stop.optional_penalty or PENALTY_DUPLICATE,
                            PENALTY_DUPLICATE,
                        )
                        stop.duplicate_group_id = stop.duplicate_group_id or keeper.stop_id[:12]
                        duplicate_count += 1
                        logger.info(
                            "Flagged duplicate: %s (%s) duplicate_of=%s (%s)",
                            stop.stop_id,
                            stop.stop_name,
                            keeper.stop_id,
                            keeper.stop_name,
                        )

        # --- 3. Outlier detection ---
        if self.config.enable_outlier_filter:
            outlier_ids = self._detect_outliers(stops)
            for stop_id in outlier_ids:
                if stop_id in protected_ids:
                    continue
                if stop_id not in flagged_reasons:
                    flagged_reasons[stop_id] = "outlier: min_distance > 3x median"
                    stop_obj = next(s for s in stops if s.stop_id == stop_id)
                    stop_obj.weak_candidate = True
                    stop_obj.optional_penalty = min(
                        stop_obj.optional_penalty or PENALTY_OUTLIER,
                        PENALTY_OUTLIER,
                    )
                    outlier_count += 1
                    logger.info("Flagged outlier: %s (%s)", stop_id, stop_obj.stop_name)

        # Build locate cache for downstream use
        locate_cache: dict[str, list[dict[str, Any]]] = {}
        if locate_results:
            for stop, loc in zip(stops, locate_results):
                locate_cache[stop.stop_id] = loc.get("edges", []) if loc else []

        return PreprocessingResult(
            cleaned_stops=list(stops),
            removed_stop_ids=[],  # We flag, not remove
            flagged_reasons=flagged_reasons,
            stats={
                "total": len(stops),
                "opposite_direction": opposite_count,
                "duplicates": duplicate_count,
                "outliers": outlier_count,
                "corridor_bearing": corridor_bearing,
                "flagged_total": len(flagged_reasons),
                "protected_ids": sorted(protected_ids),
            },
            locate_cache=locate_cache,
        )

    def _batch_locate(self, stops: Sequence[NormalizedStop]) -> list[dict[str, Any]]:
        """Call Valhalla /locate for all stops at once."""
        if not self.client.supports("/locate"):
            return []
        locations = [(stop.lon, stop.lat) for stop in stops]
        try:
            results = self.client.locate(locations)
            return results
        except Exception as exc:
            logger.warning("Valhalla /locate failed: %s", exc)
            return []

    def _best_matching_edge(self, edges: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Pick the edge with lowest distance (best snap) that has heading info."""
        candidates = [e for e in edges if e.get("heading") is not None]
        if not candidates:
            return None
        return min(candidates, key=lambda e: float(e.get("distance", 1e9)))

    def _find_duplicate_groups(
        self,
        stops: Sequence[NormalizedStop],
        locate_results: list[dict[str, Any]],
    ) -> list[list[NormalizedStop]]:
        """Find groups of stops within duplicate_distance_m with similar names."""
        used: set[int] = set()
        groups: list[list[NormalizedStop]] = []
        for i, si in enumerate(stops):
            if i in used:
                continue
            group = [si]
            for j, sj in enumerate(stops):
                if j <= i or j in used:
                    continue
                dist = haversine_m(si.lat, si.lon, sj.lat, sj.lon)
                if dist > self.config.duplicate_distance_m:
                    continue
                name_sim = _levenshtein_ratio(si.normalized_name, sj.normalized_name)
                if name_sim >= self.config.duplicate_name_threshold or si.normalized_name == sj.normalized_name:
                    group.append(sj)
                    used.add(j)
            if len(group) > 1:
                used.add(i)
                groups.append(group)
        return groups

    def _pick_keeper(
        self,
        group: list[NormalizedStop],
        locate_results: list[dict[str, Any]],
        all_stops: Sequence[NormalizedStop],
    ) -> NormalizedStop:
        """Pick the best stop in a duplicate group: prefer anchors, then closer snap distance."""
        # Anchors always win
        for stop in group:
            if stop.is_known_anchor or stop.is_fixed_start or stop.is_fixed_end:
                return stop

        # Use /locate snap distance if available
        if locate_results:
            stop_index = {s.stop_id: idx for idx, s in enumerate(all_stops)}
            best = None
            best_dist = float("inf")
            for stop in group:
                idx = stop_index.get(stop.stop_id)
                if idx is None or idx >= len(locate_results):
                    continue
                loc = locate_results[idx]
                if not loc or not loc.get("edges"):
                    continue
                edge_distances = [
                    float(e.get("distance", 1e9))
                    for e in loc["edges"]
                    if e.get("distance") is not None
                ]
                if not edge_distances:
                    continue
                snap_dist = min(edge_distances)
                if snap_dist < best_dist:
                    best_dist = snap_dist
                    best = stop
            if best is not None:
                return best

        # Fallback: highest on_route_score
        return max(group, key=lambda s: s.representative_score)

    def _detect_outliers(self, stops: Sequence[NormalizedStop]) -> list[str]:
        """Flag stops whose min distance to any other stop exceeds outlier_factor × median."""
        if len(stops) < 3:
            return []

        min_distances: list[tuple[str, float]] = []
        for i, si in enumerate(stops):
            min_d = float("inf")
            for j, sj in enumerate(stops):
                if i == j:
                    continue
                d = haversine_m(si.lat, si.lon, sj.lat, sj.lon)
                min_d = min(min_d, d)
            min_distances.append((si.stop_id, min_d))

        distances_only = [d for _, d in min_distances]
        median_d = statistics.median(distances_only)
        if median_d <= 0:
            return []

        threshold = self.config.outlier_factor * median_d
        return [sid for sid, d in min_distances if d > threshold]


def _levenshtein_ratio(s1: str, s2: str) -> float:
    """Compute Levenshtein similarity ratio between two strings."""
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0
    max_len = max(len(s1), len(s2))
    dist = _levenshtein_distance(s1, s2)
    return 1.0 - (dist / max_len)


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance."""
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if not s2:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev[j + 1] + 1
            deletions = curr[j] + 1
            substitutions = prev[j] + (0 if c1 == c2 else 1)
            curr.append(min(insertions, deletions, substitutions))
        prev = curr
    return prev[-1]
