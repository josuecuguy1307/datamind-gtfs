"""Stop enrichment from confirmed GTFS routes.

Injects missing confirmed GTFS stops into a V2 route's stop set.
These stops are ground-truth — verified coordinates and ordering.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from src.constructor_v2.common import haversine_m as _haversine_m

logger = logging.getLogger(__name__)

REFERENCES_DIR = Path(__file__).parent
MATCH_RADIUS_M = 80.0  # max distance to consider a V2 stop "already present"


@dataclass(slots=True)
class EnrichedStop:
    """A stop to inject into the route's stop set."""
    stop_id: str
    stop_name: str
    lat: float
    lon: float
    gtfs_position: int          # position in confirmed GTFS sequence (0-indexed)
    source: str = "confirmed_gtfs"
    confidence: str = "high"
    matched_v2_stop_id: str | None = None  # if this GTFS stop was already covered


@dataclass(slots=True)
class EnrichmentResult:
    """Result of enriching a route's stop set."""
    route_name: str
    gtfs_code: str
    direction_id: int
    reversed: bool
    original_stop_count: int
    gtfs_stop_count: int
    already_matched: int
    stops_added: int
    enriched_stops: list[dict[str, Any]]   # full enriched stop list (original + injected) as dicts
    added_stop_details: list[EnrichedStop]  # only the newly added stops
    diagnostics: dict[str, Any] = field(default_factory=dict)


class StopEnrichmentLoader:
    """Loads confirmed GTFS routes and enriches V2 stop sets."""

    def __init__(
        self,
        confirmed_routes_path: Path | None = None,
        confirmed_corridors_path: Path | None = None,
    ) -> None:
        self._routes_path = confirmed_routes_path or REFERENCES_DIR / "confirmed_routes.json"
        self._corridors_path = confirmed_corridors_path or REFERENCES_DIR / "confirmed_corridors.json"
        self._routes: list[dict] | None = None
        self._benchmark: dict[str, Any] | None = None

    def _load(self) -> None:
        if self._routes is not None:
            return
        with open(self._routes_path) as f:
            rdata = json.load(f)
        self._routes = rdata.get("routes", [])
        with open(self._corridors_path) as f:
            cdata = json.load(f)
        self._benchmark = cdata.get("benchmark_mapping", {})

    def get_confirmed_for_route(self, route_name: str) -> dict | None:
        """Get the benchmark mapping entry for a route name."""
        self._load()
        return self._benchmark.get(route_name)

    def _find_directions(self, route_code: str) -> list[dict]:
        """Find all direction entries for a route code."""
        self._load()
        return [r for r in self._routes if r["route_code"] == route_code]

    def _best_direction(
        self,
        v2_stops: list[dict],
        candidates: list[dict],
    ) -> tuple[dict | None, bool]:
        """Pick the GTFS direction + orientation that best matches V2 stops.
        Returns (best_candidate, was_reversed)."""
        best_dir = None
        best_score = -1
        best_reversed = False

        for cand in candidates:
            gtfs_stops = cand["ordered_stops"]
            for reversed_v2 in (False, True):
                effective = list(reversed(v2_stops)) if reversed_v2 else v2_stops
                matched = self._count_proximity_matches(effective, gtfs_stops)
                # compute simple order agreement for the matched pairs
                agree = self._quick_order_agreement(effective, gtfs_stops)
                score = agree * 10 + matched / max(1, len(gtfs_stops))
                if score > best_score:
                    best_dir = cand
                    best_score = score
                    best_reversed = reversed_v2

        return best_dir, best_reversed

    def _count_proximity_matches(
        self, v2_stops: list[dict], gtfs_stops: list[dict]
    ) -> int:
        used = set()
        count = 0
        for vs in v2_stops:
            for gi, gs in enumerate(gtfs_stops):
                if gi in used:
                    continue
                d = _haversine_m(vs["lat"], vs["lon"], gs["lat"], gs["lon"])
                if d <= MATCH_RADIUS_M:
                    used.add(gi)
                    count += 1
                    break
        return count

    def _quick_order_agreement(
        self, v2_stops: list[dict], gtfs_stops: list[dict]
    ) -> float:
        """Quick pairwise order agreement between matched stops."""
        from itertools import combinations

        # Match by proximity
        pairs = []  # (v2_idx, gtfs_idx)
        used_gtfs = set()
        candidates = []
        for vi, vs in enumerate(v2_stops):
            for gi, gs in enumerate(gtfs_stops):
                d = _haversine_m(vs["lat"], vs["lon"], gs["lat"], gs["lon"])
                if d <= MATCH_RADIUS_M:
                    candidates.append((d, vi, gi))
        candidates.sort()
        used_v2 = set()
        for _, vi, gi in candidates:
            if vi not in used_v2 and gi not in used_gtfs:
                pairs.append((vi, gi))
                used_v2.add(vi)
                used_gtfs.add(gi)

        if len(pairs) < 2:
            return 1.0 if pairs else 0.0

        concordant = 0
        total = 0
        for (v1, g1), (v2, g2) in combinations(pairs, 2):
            total += 1
            if (v1 < v2) == (g1 < g2):
                concordant += 1
        return concordant / total if total > 0 else 1.0

    def enrich(
        self,
        route_name: str,
        v2_stops: list[dict],
        *,
        match_radius_m: float = MATCH_RADIUS_M,
    ) -> EnrichmentResult | None:
        """Enrich a V2 stop set with missing confirmed GTFS stops.

        Args:
            route_name: V2 route name (must be in benchmark_mapping)
            v2_stops: list of dicts with at least {stop_id, stop_name, lat, lon}
            match_radius_m: proximity threshold for considering a stop "already present"

        Returns EnrichmentResult or None if no confirmed route exists.
        """
        self._load()
        mapping = self._benchmark.get(route_name)
        if not mapping:
            return None
        code = mapping.get("confirmed_route")
        if not code:
            return None

        candidates = self._find_directions(code)
        if not candidates:
            return None

        best_dir, was_reversed = self._best_direction(v2_stops, candidates)
        if best_dir is None:
            return None

        gtfs_stops = best_dir["ordered_stops"]
        effective_v2 = list(reversed(v2_stops)) if was_reversed else list(v2_stops)

        # Find which GTFS stops are already covered by V2
        matched_gtfs_indices: set[int] = set()
        matched_details: list[tuple[int, str]] = []  # (gtfs_idx, v2_stop_id)

        # Greedy proximity matching (closest first)
        match_candidates = []
        for gi, gs in enumerate(gtfs_stops):
            for vs in effective_v2:
                d = _haversine_m(vs["lat"], vs["lon"], gs["lat"], gs["lon"])
                if d <= match_radius_m:
                    match_candidates.append((d, gi, vs.get("stop_id", "")))
        match_candidates.sort()
        used_v2_ids: set[str] = set()
        for _, gi, v2_id in match_candidates:
            if gi not in matched_gtfs_indices and v2_id not in used_v2_ids:
                matched_gtfs_indices.add(gi)
                matched_details.append((gi, v2_id))
                used_v2_ids.add(v2_id)

        # Identify missing GTFS stops
        missing_indices = [i for i in range(len(gtfs_stops)) if i not in matched_gtfs_indices]
        added_stops: list[EnrichedStop] = []
        for gi in missing_indices:
            gs = gtfs_stops[gi]
            added_stops.append(EnrichedStop(
                stop_id=f"gtfs_{code}_{best_dir['direction_id']}_{gi}",
                stop_name=gs.get("stop_name", f"GTFS Stop {gi}"),
                lat=gs["lat"],
                lon=gs["lon"],
                gtfs_position=gi,
                source="confirmed_gtfs",
                confidence="high",
            ))

        # Build enriched stop list: interleave V2 stops + GTFS stops by position
        # Strategy: use GTFS order as the master ordering, keeping V2 stops
        # at their matched GTFS positions and inserting missing ones.
        enriched = self._merge_stops(effective_v2, gtfs_stops, matched_details, added_stops)

        return EnrichmentResult(
            route_name=route_name,
            gtfs_code=code,
            direction_id=best_dir["direction_id"],
            reversed=was_reversed,
            original_stop_count=len(v2_stops),
            gtfs_stop_count=len(gtfs_stops),
            already_matched=len(matched_gtfs_indices),
            stops_added=len(added_stops),
            enriched_stops=enriched,
            added_stop_details=added_stops,
            diagnostics={
                "matched_gtfs_indices": sorted(matched_gtfs_indices),
                "missing_gtfs_indices": missing_indices,
                "was_reversed": was_reversed,
                "match_radius_m": match_radius_m,
            },
        )

    def _merge_stops(
        self,
        v2_stops: list[dict],
        gtfs_stops: list[dict],
        matched_details: list[tuple[int, str]],  # (gtfs_idx, v2_stop_id)
        added_stops: list[EnrichedStop],
    ) -> list[dict[str, Any]]:
        """Merge V2 and GTFS stops using GTFS ordering as backbone.

        For matched positions, keep the V2 stop (it has richer metadata).
        For unmatched positions, inject the GTFS stop.
        For V2 stops NOT matched to any GTFS stop, insert them at
        their best interpolated position.
        """
        # Map gtfs_idx -> v2_stop
        gtfs_to_v2: dict[int, dict] = {}
        matched_v2_ids: set[str] = set()
        for gi, v2_id in matched_details:
            for vs in v2_stops:
                if vs.get("stop_id") == v2_id:
                    gtfs_to_v2[gi] = vs
                    matched_v2_ids.add(v2_id)
                    break

        # Unmatched V2 stops (not paired with any GTFS stop)
        unmatched_v2 = [vs for vs in v2_stops if vs.get("stop_id") not in matched_v2_ids]

        # Build GTFS-ordered backbone
        result: list[dict[str, Any]] = []
        added_set = {s.gtfs_position for s in added_stops}
        added_by_pos = {s.gtfs_position: s for s in added_stops}

        for gi, gs in enumerate(gtfs_stops):
            if gi in gtfs_to_v2:
                # Use V2 stop (richer metadata), update seq
                stop = dict(gtfs_to_v2[gi])
                stop["seq"] = len(result) + 1
                result.append(stop)
            elif gi in added_set:
                # Inject GTFS stop
                es = added_by_pos[gi]
                result.append({
                    "seq": len(result) + 1,
                    "stop_id": es.stop_id,
                    "stop_name": es.stop_name,
                    "lat": es.lat,
                    "lon": es.lon,
                    "stop_source": "confirmed_gtfs",
                    "is_known_anchor": True,  # confirmed GTFS stops are high-confidence
                    "on_route_score": 0.95,
                    "metadata": {"gtfs_position": gi, "confidence": "high"},
                })

        # Insert unmatched V2 stops at best positions
        for vs in unmatched_v2:
            best_pos = self._find_best_insert_position(vs, result)
            stop = dict(vs)
            stop["seq"] = best_pos + 1
            result.insert(best_pos, stop)

        # Re-sequence
        for i, s in enumerate(result):
            s["seq"] = i + 1

        return result

    def _find_best_insert_position(
        self, stop: dict, ordered: list[dict]
    ) -> int:
        """Find the best position to insert a stop into an ordered list.
        Minimizes total detour distance."""
        if not ordered:
            return 0
        if len(ordered) == 1:
            return 1

        best_pos = len(ordered)
        best_detour = float("inf")

        for i in range(len(ordered) + 1):
            detour = 0.0
            if i > 0:
                detour += _haversine_m(
                    ordered[i - 1]["lat"], ordered[i - 1]["lon"],
                    stop["lat"], stop["lon"],
                )
            if i < len(ordered):
                detour += _haversine_m(
                    stop["lat"], stop["lon"],
                    ordered[i]["lat"], ordered[i]["lon"],
                )
            if 0 < i < len(ordered):
                # Subtract direct distance being broken
                detour -= _haversine_m(
                    ordered[i - 1]["lat"], ordered[i - 1]["lon"],
                    ordered[i]["lat"], ordered[i]["lon"],
                )
            if detour < best_detour:
                best_detour = detour
                best_pos = i

        return best_pos

    def enrich_from_corridors(
        self,
        route_name: str,
        v2_stops: list[dict],
        *,
        match_radius_m: float = MATCH_RADIUS_M,
    ) -> EnrichmentResult | None:
        """Enrich a V2 stop set using corridor stops (for routes without direct GTFS match).

        Finds which corridors this route uses, then injects corridor stops
        that fall within the route's geographic buffer.
        """
        self._load()
        mapping = self._benchmark.get(route_name)
        if not mapping:
            return None

        # Only for routes WITHOUT a confirmed_route (those use enrich() directly)
        if mapping.get("confirmed_route"):
            return None

        corridor_names = mapping.get("corridors", [])
        if not corridor_names:
            return None

        with open(self._corridors_path) as f:
            cdata = json.load(f)
        corridors = cdata.get("corridors", {})

        # Collect corridor stops that are within the route's bounding box + buffer
        v2_lats = [s["lat"] for s in v2_stops]
        v2_lons = [s["lon"] for s in v2_stops]
        if not v2_lats:
            return None
        lat_min, lat_max = min(v2_lats) - 0.01, max(v2_lats) + 0.01
        lon_min, lon_max = min(v2_lons) - 0.01, max(v2_lons) + 0.01

        candidate_stops: list[EnrichedStop] = []
        for cname in corridor_names:
            corridor = corridors.get(cname)
            if not corridor:
                continue
            for ci, cs in enumerate(corridor["ordered_stops"]):
                clat, clon = cs["lat"], cs["lon"]
                # Check within bounding box
                if not (lat_min <= clat <= lat_max and lon_min <= clon <= lon_max):
                    continue
                # Check not already matched by a V2 stop
                already_present = any(
                    _haversine_m(vs["lat"], vs["lon"], clat, clon) <= match_radius_m
                    for vs in v2_stops
                )
                if already_present:
                    continue
                candidate_stops.append(EnrichedStop(
                    stop_id=f"corridor_{cname}_{ci}",
                    stop_name=cs.get("stop_name", f"Corridor {cname} #{ci}"),
                    lat=clat,
                    lon=clon,
                    gtfs_position=ci,
                    source=f"corridor_{cname}",
                    confidence="medium",
                ))

        if not candidate_stops:
            return None

        # Build enriched list: original + injected at best positions
        enriched = [dict(s) for s in v2_stops]
        for cs in candidate_stops:
            stop_dict = {
                "stop_id": cs.stop_id,
                "stop_name": cs.stop_name,
                "lat": cs.lat,
                "lon": cs.lon,
                "stop_source": cs.source,
                "is_known_anchor": False,
                "on_route_score": 0.80,
                "metadata": {"corridor_position": cs.gtfs_position, "confidence": "medium"},
            }
            pos = self._find_best_insert_position(stop_dict, enriched)
            enriched.insert(pos, stop_dict)

        # Re-sequence
        for i, s in enumerate(enriched):
            s["seq"] = i + 1

        return EnrichmentResult(
            route_name=route_name,
            gtfs_code="",
            direction_id=-1,
            reversed=False,
            original_stop_count=len(v2_stops),
            gtfs_stop_count=0,
            already_matched=0,
            stops_added=len(candidate_stops),
            enriched_stops=enriched,
            added_stop_details=candidate_stops,
            diagnostics={
                "corridors_used": corridor_names,
                "candidates_found": len(candidate_stops),
                "source": "corridor_enrichment",
            },
        )
