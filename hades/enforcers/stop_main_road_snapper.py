"""Stop-to-Main-Road Snapper (Fixer Phase 0.5).

⚠ **DEPRECATED / EXPERIMENTAL — DO NOT USE IN PRODUCTION (2026-04-22).**
Three eval iterations on 112 QC routes (no gates / way_id continuity /
way_NAME continuity) all delivered ≤ 1 class upgrade. The few "wins" were
shape-length deltas of -1.8 to +1.2 km; the bulk of attempts either got
rejected by the length gate (≤ 15 % drift) or didn't move classification.
Diagnosis: snapping stops to nearby main roads makes Valhalla follow those
main roads instead of the bus's actual service-road corridor — net result
is wild detours, not cleaner geometry. Module kept for reference; not
wired into ``re_entry_worker``.


Diagnostic on QC 27-route sample (2026-04-22) found 26 % of stops sit on
OSM `highway=service` ways while a primary/secondary/tertiary road sits
within 50 m — forcing Valhalla to detour into parking lots and driveways.
Those detours are the micro-zigzags that are below the Geometry Enforcer's
threshold but visible on the map.

This module snaps stops off service-class roads onto nearby main roads.
Conservative criteria (per operator spec 2026-04-22):

  (a) stop currently sits on a road of class ``service`` (or below —
      conservative variant only fires on ``service``; see ``_RELAXED_``)
  (b) a road of class ``tertiary`` or better exists within ``max_distance_m``
  (c) that road is within ``max_distance_m`` of the stop

Any stop that is already on primary/secondary/tertiary is LEFT ALONE.
Caller-supplied Overpass fetcher so tests can stub without network.

No DB writes. Snap audit is surfaced in the returned ``SnapResult``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence


# Road hierarchy (same as scripts/stop_snap_diagnosis.py). Higher = more main.
ROAD_RANK: dict[str, int] = {
    "motorway": 6, "motorway_link": 6,
    "trunk": 5, "trunk_link": 5,
    "primary": 4, "primary_link": 4,
    "secondary": 3, "secondary_link": 3,
    "tertiary": 2, "tertiary_link": 2,
    "unclassified": 1,
    "residential": 0, "living_street": 0,
    "service": -1,
    "track": -2, "pedestrian": -3, "footway": -3, "path": -3,
    "cycleway": -3, "construction": -4,
}

# Minimum target class for the "best nearby" road. Conservative = 2 (tertiary).
_MIN_TARGET_CLASS = 2

# Current-class upper bound — only snap if stop is currently on this OR lower.
# Conservative = service only (-1). Relaxed variant could go up to 0 (residential).
_MAX_CURRENT_CLASS = -1

# Roads we NEVER snap onto (buses don't stop on motorway/trunk).
_FORBIDDEN_TARGETS = {"motorway", "motorway_link", "trunk", "trunk_link"}


# ---------------------------------------------------------------------------
# Overpass fetcher protocol.
# ---------------------------------------------------------------------------

# Callable(lat, lon, radius_m) -> list of dicts:
#   [{"way_id": int, "highway": str, "name": str|None, "oneway": str|None,
#     "geometry": [(lat, lon), ...], "distance_m": float}, ...]
OverpassFn = Callable[[float, float, float], list[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Geometry helpers — re-export canonical primitives under legacy names.
# ---------------------------------------------------------------------------

from hades.geometry.canonical import haversine_m as _haversine_m  # noqa: E402


def _project_to_segment(
    p: tuple[float, float],   # (lat, lon)
    a: tuple[float, float],
    b: tuple[float, float],
) -> tuple[tuple[float, float], float]:
    """Return (foot_point_latlon, distance_m) where ``foot_point`` is the
    closest point on segment a→b to ``p``. Short-segment planar
    approximation — accurate enough for ≤200 m ways at these latitudes.
    """
    lat_p, lon_p = p
    lat_a, lon_a = a
    lat_b, lon_b = b
    # Approximate equirectangular projection around point p
    cos_phi = math.cos(math.radians(lat_p))
    # Convert to metres on the tangent plane
    ax = (lon_a - lon_p) * cos_phi * 111_320.0
    ay = (lat_a - lat_p) * 111_132.0
    bx = (lon_b - lon_p) * cos_phi * 111_320.0
    by = (lat_b - lat_p) * 111_132.0
    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-6:
        # degenerate — the two endpoints coincide
        return a, _haversine_m(lat_p, lon_p, lat_a, lon_a)
    # t-parameter of the foot of perpendicular (unclamped)
    t = -(ax * dx + ay * dy) / seg_len_sq
    t_clamped = max(0.0, min(1.0, t))
    foot_lat = lat_a + t_clamped * (lat_b - lat_a)
    foot_lon = lon_a + t_clamped * (lon_b - lon_a)
    d = _haversine_m(lat_p, lon_p, foot_lat, foot_lon)
    return (foot_lat, foot_lon), d


def _project_to_way(
    p: tuple[float, float],
    way_geom: list[tuple[float, float]],   # [(lat, lon), ...]
) -> tuple[tuple[float, float], float]:
    """Closest point on a polyline (best over all segments)."""
    if not way_geom:
        return p, float("inf")
    if len(way_geom) == 1:
        d = _haversine_m(p[0], p[1], way_geom[0][0], way_geom[0][1])
        return way_geom[0], d
    best = way_geom[0]
    best_d = float("inf")
    for i in range(len(way_geom) - 1):
        foot, d = _project_to_segment(p, way_geom[i], way_geom[i + 1])
        if d < best_d:
            best_d = d
            best = foot
    return best, best_d


# ---------------------------------------------------------------------------
# Result schema.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class StopSnap:
    idx: int
    original: tuple[float, float]       # (lat, lon)
    snapped: tuple[float, float]        # (lat, lon) — equal to original if untouched
    moved: bool
    move_distance_m: float = 0.0
    current_class: Optional[int] = None
    current_highway: Optional[str] = None
    best_class: Optional[int] = None
    best_highway: Optional[str] = None
    best_way_id: Optional[int] = None
    # v2 gate decision codes (only populated when a gate rejected the candidate):
    #   gate_continuity_failed_neither_side  — target name not at either neighbour
    #   gate_continuity_failed_one_side_only — target name at only one neighbour
    #   gate_named_road_failed               — target name not a neighbour's main road
    #   target_has_no_name                   — candidate has no `name` tag (can't verify)
    # reason values: "kept" | "snapped" | "no_roads_nearby" | "no_upgrade"
    # | "forbidden_target" | "target_too_far" | "projection_too_far"
    # | "no_way_geometry" | "kept_current_is_main" | "target_has_no_name"
    # | "gate_continuity_failed_neither_side"
    # | "gate_continuity_failed_one_side_only"
    # | "gate_named_road_failed"
    reason: str = "kept"
    rejected_target: Optional[str] = None  # highway tag of the candidate that was rejected (if any)


@dataclass(slots=True)
class SnapResult:
    route_id: str
    stops_before: list[tuple[float, float]]
    stops_after: list[tuple[float, float]]
    snaps: list[StopSnap] = field(default_factory=list)
    n_moved: int = 0
    total_move_distance_m: float = 0.0
    notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "n_stops": len(self.stops_before),
            "n_moved": self.n_moved,
            "total_move_distance_m": round(self.total_move_distance_m, 2),
            "avg_move_distance_m": (
                round(self.total_move_distance_m / self.n_moved, 2)
                if self.n_moved > 0 else 0.0
            ),
            "snaps": [
                {
                    "idx": s.idx,
                    "moved": s.moved,
                    "reason": s.reason,
                    "move_distance_m": round(s.move_distance_m, 2),
                    "current_highway": s.current_highway,
                    "best_highway": s.best_highway,
                    "original": list(s.original),
                    "snapped": list(s.snapped),
                }
                for s in self.snaps
            ],
            "notes": dict(self.notes),
        }


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def snap_stops_to_main_road(
    *,
    route_id: str,
    stops: Sequence[tuple[float, float]],         # (lat, lon)
    overpass_fn: OverpassFn,
    max_distance_m: float = 50.0,
    min_target_class: int = _MIN_TARGET_CLASS,
    max_current_class: int = _MAX_CURRENT_CLASS,
    require_continuity: bool = False,
    require_named_match: bool = False,
    secondary_rank: int = 3,     # used by named-road gate
) -> SnapResult:
    """Snap each stop OFF a service road onto the nearest main road.

    Conservative by default: fires only on stops currently sitting on
    ``highway=service`` when a ``tertiary``-or-better road is within
    ``max_distance_m``. Primary/secondary stops are never moved.

    ``overpass_fn(lat, lon, radius_m)`` must return ways including
    ``geometry`` (list of (lat, lon)).

    **v2 gates (opt-in)** — both default off for backward compatibility:

    - ``require_continuity``: target road's ``way_id`` must appear in the
      50 m-radius neighbour sets of BOTH the previous and next stops. If
      either neighbour can't reach the target, skip this candidate.
    - ``require_named_match``: the candidate target road's ``name`` tag
      must match (case-insensitive) at least one primary/secondary road
      name already near a neighbour stop.

    When both gates are on, stops only snap onto roads already proven to
    be the bus's actual corridor — not arbitrary avenues that happen to
    be close.
    """
    stops_before = [(float(a), float(b)) for (a, b) in stops]
    stops_after = list(stops_before)
    snaps: list[StopSnap] = []
    total_d = 0.0
    n_moved = 0

    # Pre-fetch neighbour road lists once when v2 gates are on so we can
    # run continuity / named-road checks without re-querying Overpass.
    neighbour_roads: dict[int, list[dict[str, Any]]] = {}
    if require_continuity or require_named_match:
        for j, (jlat, jlon) in enumerate(stops_before):
            neighbour_roads[j] = overpass_fn(jlat, jlon, max_distance_m) or []

    for i, (lat, lon) in enumerate(stops_before):
        if i in neighbour_roads:
            roads = neighbour_roads[i]
        else:
            roads = overpass_fn(lat, lon, max_distance_m) or []
        if not roads:
            snaps.append(StopSnap(
                idx=i, original=(lat, lon), snapped=(lat, lon),
                moved=False, reason="no_roads_nearby",
            ))
            continue
        # Current = nearest road by distance
        current = min(roads, key=lambda r: r.get("distance_m", 9999))
        cur_hw = current.get("highway")
        cur_cls = ROAD_RANK.get(cur_hw, -5)
        # Eligible = roads NOT in the forbidden target set. Buses never stop
        # on motorway/trunk — selecting one as "best" then rejecting the whole
        # snap would hide the fact that a valid lower-class alternative exists.
        eligible = [r for r in roads if r.get("highway") not in _FORBIDDEN_TARGETS]
        if not eligible:
            snaps.append(StopSnap(
                idx=i, original=(lat, lon), snapped=(lat, lon), moved=False,
                current_class=cur_cls, current_highway=cur_hw,
                reason="forbidden_target",
            ))
            continue
        # Rank-ordered candidate list (best-first) so gates can skip to the
        # next candidate instead of giving up outright.
        ranked_candidates = sorted(
            eligible,
            key=lambda r: (-ROAD_RANK.get(r.get("highway"), -5), r.get("distance_m", 9999)),
        )
        best = ranked_candidates[0]
        best_hw = best.get("highway")
        best_cls = ROAD_RANK.get(best_hw, -5)
        best_d = float(best.get("distance_m", 9999))

        stop_snap = StopSnap(
            idx=i, original=(lat, lon), snapped=(lat, lon),
            moved=False,
            current_class=cur_cls, current_highway=cur_hw,
            best_class=best_cls, best_highway=best_hw,
            best_way_id=best.get("way_id"),
        )

        # Gate (a): current must be "service" or below
        if cur_cls > max_current_class:
            stop_snap.reason = "kept_current_is_main"
            snaps.append(stop_snap)
            continue
        # Gate (b): target must be tertiary or better (class-level check)
        if best_cls < min_target_class:
            stop_snap.reason = "no_upgrade"
            snaps.append(stop_snap)
            continue

        # v2 gates loop — iterate ranked candidates; first to pass all
        # applicable gates wins. If none passes, report the reason the
        # strongest candidate failed.
        prev_idx = i - 1 if i > 0 else None
        next_idx = i + 1 if i < len(stops_before) - 1 else None
        prev_roads = neighbour_roads.get(prev_idx, []) if prev_idx is not None else []
        next_roads = neighbour_roads.get(next_idx, []) if next_idx is not None else []

        chosen: Optional[dict[str, Any]] = None
        last_fail_reason: Optional[str] = None
        had_class_candidate = False
        for cand in ranked_candidates:
            cand_cls = ROAD_RANK.get(cand.get("highway"), -5)
            cand_d = float(cand.get("distance_m", 9999))
            if cand_cls < min_target_class:
                continue
            # Candidate is high-class; track so we can report target_too_far
            # specifically if every such candidate is out of range.
            had_class_candidate = True
            if cand_d > max_distance_m:
                if last_fail_reason != "target_too_far":
                    last_fail_reason = "target_too_far"
                    stop_snap.rejected_target = cand.get("highway")
                continue
            # Named-road gate: compute neighbour MAIN road names EXCLUDING
            # the candidate itself (self-reference doesn't count as a match).
            cand_way_id = cand.get("way_id")
            neighbour_main_names: set[str] = set()
            if require_named_match:
                for r in (prev_roads + next_roads):
                    if r.get("way_id") == cand_way_id:
                        continue
                    if ROAD_RANK.get(r.get("highway"), -5) >= secondary_rank:
                        nm = r.get("name")
                        if nm:
                            neighbour_main_names.add(str(nm).lower())
            # Gate 1: way-NAME continuity — the candidate's road name must
            # appear as a tertiary-or-better road near BOTH neighbour stops.
            # Matching on name (not way_id) accommodates OSM fragmentation
            # where a single avenue is tagged as many short ways sharing the
            # same `name` tag. A stop at a corner will have neighbours
            # touching different way_ids of the same conceptual avenue;
            # way_id matching rejected those legitimate cases (Option B).
            if require_continuity and prev_idx is not None and next_idx is not None:
                target_name = (cand.get("name") or "").strip().lower()
                if not target_name:
                    # Can't verify corridor membership without a name tag.
                    last_fail_reason = "target_has_no_name"
                    stop_snap.rejected_target = cand.get("highway")
                    continue

                def _has_named_main(roads: list[dict[str, Any]]) -> bool:
                    return any(
                        (r.get("name") or "").strip().lower() == target_name
                        and ROAD_RANK.get(r.get("highway"), -5) >= min_target_class
                        for r in roads
                    )

                prev_ok = _has_named_main(prev_roads)
                next_ok = _has_named_main(next_roads)
                if prev_ok and next_ok:
                    pass  # continuity_ok_by_name
                elif prev_ok or next_ok:
                    last_fail_reason = "gate_continuity_failed_one_side_only"
                    stop_snap.rejected_target = cand.get("highway")
                    continue
                else:
                    last_fail_reason = "gate_continuity_failed_neither_side"
                    stop_snap.rejected_target = cand.get("highway")
                    continue
            # Gate 2: named-road continuity
            if require_named_match and neighbour_main_names:
                cand_name = (cand.get("name") or "").lower()
                if cand_name and cand_name not in neighbour_main_names:
                    last_fail_reason = "gate_named_road_failed"
                    stop_snap.rejected_target = cand.get("highway")
                    continue
                # If candidate has no name at all, we can't prove corridor
                # membership — reject under the named-road gate.
                if require_named_match and not cand_name:
                    last_fail_reason = "gate_named_road_failed"
                    stop_snap.rejected_target = cand.get("highway")
                    continue
            chosen = cand
            break

        if chosen is None:
            stop_snap.reason = last_fail_reason or (
                "no_upgrade" if not had_class_candidate else "no_upgrade"
            )
            snaps.append(stop_snap)
            continue

        # Project stop onto the chosen road's geometry
        way_geom = chosen.get("geometry") or []
        if not way_geom:
            stop_snap.reason = "no_way_geometry"
            snaps.append(stop_snap)
            continue
        foot, foot_d = _project_to_way((lat, lon), way_geom)
        if foot_d > max_distance_m:
            stop_snap.reason = "projection_too_far"
            snaps.append(stop_snap)
            continue
        stops_after[i] = foot
        stop_snap.snapped = foot
        stop_snap.moved = True
        stop_snap.move_distance_m = foot_d
        stop_snap.best_highway = chosen.get("highway")
        stop_snap.best_class = ROAD_RANK.get(chosen.get("highway"), -5)
        stop_snap.best_way_id = chosen.get("way_id")
        stop_snap.reason = "snapped"
        total_d += foot_d
        n_moved += 1
        snaps.append(stop_snap)

    return SnapResult(
        route_id=route_id,
        stops_before=stops_before,
        stops_after=stops_after,
        snaps=snaps,
        n_moved=n_moved,
        total_move_distance_m=total_d,
        notes={
            "max_distance_m": max_distance_m,
            "min_target_class": min_target_class,
            "max_current_class": max_current_class,
        },
    )


__all__ = ["snap_stops_to_main_road", "SnapResult", "StopSnap", "ROAD_RANK"]
