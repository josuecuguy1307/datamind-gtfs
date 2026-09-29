"""Stop refill — find candidate stops to add to a route from production routes.

Inverse complement to orphan cleanup. While Pre-Ship Cleanup REMOVES stops
that drift from the polyline, Stop Refill ADDS stops that the polyline
visits but ``proposed_stops`` omits — sourcing them from routes already
shipped to ``route_prod.routes`` (production, ground truth).

This module is GREEK pipeline phase γ: read-only, geometric, and stateless.
It surfaces candidates with confidence scores; the operator decides which
ones to accept. No bulk acceptance, no automatic addition.

Algorithm
---------

For a target route A with polyline ``polyline_a`` and existing stops
``existing_stops_a``:

1. Iterate each shipped production route B (caller supplies the pool).
2. Find shared segments between A and B (``shared_segments.find_shared_segments``).
3. For each stop on B that sits inside a shared segment AND projects within
   25 m of A's polyline, propose it as a refill candidate for A.
4. Aggregate candidates by ``stop_id`` — the same stop appearing in multiple
   production routes raises its confidence (popularity factor).
5. Score every aggregated candidate; filter below ``MIN_SCORE_SURFACE``;
   exclude stops already present in A; cap the result at
   ``MAX_CANDIDATES_PER_ROUTE``.

Scoring (deliberately untuned per spec)::

    score = 0.50 * distance_factor   max(0, 1 - distance / DISTANCE_CAP_M)
          + 0.25 * popularity_factor min(1, routes_count / POPULARITY_CAP_ROUTES)
          + 0.25 * segment_factor    min(1, max_shared_segment_m / SEGMENT_CAP_M)

Threshold defaults are exposed as module constants so the operator UI and
audit modules can reuse them without re-declaring.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Sequence

from hades.enforcers.shared_segments import (
    DEFAULT_BEARING_TOLERANCE_DEG,
    DEFAULT_MIN_SEGMENT_LENGTH_M,
    DEFAULT_PROXIMITY_THRESHOLD_M,
    DEFAULT_SAMPLE_DISTANCE_M,
    SharedSegment,
    find_shared_segments,
)
from hades.enforcers.stop_coverage_enforcer import (
    cumulative_m,
    project_point_to_polyline,
)


# How close a candidate stop must be to A's polyline to be eligible at all.
STOP_PROXIMITY_THRESHOLD_M = 25.0

# User-confirmed spec (2026-04-27): a stop is only eligible to be refilled
# onto another route if it was COMPLETELY ALIGNED in its origin route. Same
# threshold the α cleanup uses for the "aligned" category. Stops that were
# snap-required (10–60 m off polyline) in their origin must NOT propagate
# across routes — quality has to start where it lands.
ORIGIN_ALIGNED_THRESHOLD_M = 10.0

# Scoring weights — must sum to 1.0.
WEIGHT_DISTANCE = 0.50
WEIGHT_POPULARITY = 0.25
WEIGHT_SEGMENT = 0.25

# Saturation caps for the three score components.
DISTANCE_CAP_M = 30.0
POPULARITY_CAP_ROUTES = 5
SEGMENT_CAP_M = 5_000.0

# Score thresholds and result cap.
MIN_SCORE_SURFACE = 0.50
HIGH_CONFIDENCE_THRESHOLD = 0.75
MAX_CANDIDATES_PER_ROUTE = 15


@dataclass(frozen=True)
class ProductionRoute:
    """A shipped production route used as a refill source.

    ``polyline`` follows the GeoJSON ``(lon, lat)`` convention. ``stops`` is
    a list of dicts shaped like ``{"stop_id": str, "lat": float, "lon": float}``
    (the same shape as ``approval_queue.proposed_stops``). ``route_id`` is
    used only for traceability in the audit metadata.
    """
    route_id: str
    polyline: Sequence[tuple[float, float]]
    stops: Sequence[dict]


@dataclass
class RefillCandidate:
    """A candidate stop to refill onto route A.

    ``score`` is computed by :func:`compute_geometric_score`. ``source_route_ids``
    lists every production route B that contributed this candidate (a stop
    appearing in 3 routes that all overlap with A would have 3 entries).
    ``max_shared_segment_m`` is the length of the longest shared segment
    across all source routes — this is what feeds the segment factor of the
    score.

    ``origin_distance_m`` is the *minimum* perpendicular distance to the
    polyline of any source route the stop appears in. Per user spec, only
    stops with ``origin_distance_m <= ORIGIN_ALIGNED_THRESHOLD_M`` are
    surfaced — guarantees we never propagate a snap-required stop from
    one route to another. Exposed so the operator UI can show the metric
    alongside the proximity-to-A distance.
    """
    stop_id: str
    lat: float
    lon: float
    distance_to_polyline_m: float
    origin_distance_m: float
    routes_count: int
    max_shared_segment_m: float
    score: float
    source_route_ids: list[str] = field(default_factory=list)


def compute_geometric_score(
    distance_m: float,
    routes_count: int,
    max_shared_segment_m: float,
) -> float:
    """Compute the 0.0–1.0 confidence score for a refill candidate.

    Formula (per design spec)::

        distance_factor   = max(0, 1 - distance / DISTANCE_CAP_M)
        popularity_factor = min(1, routes_count / POPULARITY_CAP_ROUTES)
        segment_factor    = min(1, max_shared_segment / SEGMENT_CAP_M)

        score = 0.50 * distance_factor
              + 0.25 * popularity_factor
              + 0.25 * segment_factor

    All inputs are clipped to non-negative values; the output is clipped
    to ``[0.0, 1.0]``.
    """
    d = max(0.0, distance_m)
    n = max(0, routes_count)
    s = max(0.0, max_shared_segment_m)

    if DISTANCE_CAP_M <= 0.0:
        distance_factor = 0.0
    else:
        distance_factor = max(0.0, 1.0 - d / DISTANCE_CAP_M)

    if POPULARITY_CAP_ROUTES <= 0:
        popularity_factor = 0.0
    else:
        popularity_factor = min(1.0, n / POPULARITY_CAP_ROUTES)

    if SEGMENT_CAP_M <= 0.0:
        segment_factor = 0.0
    else:
        segment_factor = min(1.0, s / SEGMENT_CAP_M)

    score = (
        WEIGHT_DISTANCE * distance_factor
        + WEIGHT_POPULARITY * popularity_factor
        + WEIGHT_SEGMENT * segment_factor
    )
    return max(0.0, min(1.0, score))


def _existing_stop_ids(existing_stops: Sequence[dict]) -> set[str]:
    out: set[str] = set()
    for s in existing_stops or []:
        sid = str(s.get("stop_id") or "")
        if sid:
            out.add(sid)
    return out


def find_refill_candidates(
    route_a_id: str,
    polyline_a: Sequence[tuple[float, float]],
    existing_stops_a: Sequence[dict],
    production_routes: Sequence[ProductionRoute],
    *,
    sample_distance_m: float = DEFAULT_SAMPLE_DISTANCE_M,
    proximity_threshold_m: float = DEFAULT_PROXIMITY_THRESHOLD_M,
    bearing_tolerance_deg: float = DEFAULT_BEARING_TOLERANCE_DEG,
    min_segment_length_m: float = DEFAULT_MIN_SEGMENT_LENGTH_M,
    stop_proximity_threshold_m: float = STOP_PROXIMITY_THRESHOLD_M,
    origin_aligned_threshold_m: float = ORIGIN_ALIGNED_THRESHOLD_M,
    min_score: float = MIN_SCORE_SURFACE,
    max_candidates: int = MAX_CANDIDATES_PER_ROUTE,
) -> list[RefillCandidate]:
    """Find refill candidates for route A.

    Iterates every production route B, runs ``find_shared_segments`` against
    A, then for each stop on B that:

    * was COMPLETELY ALIGNED in its origin route B
      (``perp_b <= origin_aligned_threshold_m``, default 10 m — same as
      α cleanup's "aligned" band),
    * sits inside a shared corridor between A and B,
    * projects within ``stop_proximity_threshold_m`` of A's polyline,

    records a candidate. Aggregates by ``stop_id`` and scores. Filters out:

    * stops already present in ``existing_stops_a``,
    * route A itself (matched by ``route_a_id``),
    * stops not aligned in their origin (per user spec),
    * candidates with ``score < min_score``.

    Returns up to ``max_candidates`` candidates sorted by score descending.
    """
    if not polyline_a or len(polyline_a) < 2 or not production_routes:
        return []

    cum_a = cumulative_m(polyline_a)
    existing_ids = _existing_stop_ids(existing_stops_a)

    by_stop: dict[str, list[dict]] = defaultdict(list)

    for prod in production_routes:
        if prod.route_id == route_a_id:
            continue
        if not prod.polyline or len(prod.polyline) < 2 or not prod.stops:
            continue

        segments = find_shared_segments(
            polyline_a,
            prod.polyline,
            sample_distance_m=sample_distance_m,
            proximity_threshold_m=proximity_threshold_m,
            bearing_tolerance_deg=bearing_tolerance_deg,
            min_segment_length_m=min_segment_length_m,
        )
        if not segments:
            continue

        max_segment_m = max(seg.length_m for seg in segments)
        cum_b = cumulative_m(prod.polyline)

        for stop in prod.stops:
            sid = str(stop.get("stop_id") or "")
            if not sid or sid in existing_ids:
                continue
            try:
                lat = float(stop["lat"])
                lon = float(stop["lon"])
            except (KeyError, TypeError, ValueError):
                continue

            # Origin-alignment gate: must be ≤10 m of B's own polyline,
            # else the stop was never properly grounded on its source
            # route and must NOT propagate.
            _, perp_b = project_point_to_polyline(
                lat, lon, prod.polyline, cum_b
            )
            if perp_b > origin_aligned_threshold_m:
                continue

            cum_a_stop, perp_a = project_point_to_polyline(
                lat, lon, polyline_a, cum_a
            )
            if perp_a > stop_proximity_threshold_m:
                continue
            if not _is_inside_any_segment_on_a(cum_a_stop, segments):
                continue

            by_stop[sid].append(
                {
                    "lat": lat,
                    "lon": lon,
                    "distance_m": perp_a,
                    "origin_distance_m": perp_b,
                    "source_route_id": prod.route_id,
                    "max_segment_m": max_segment_m,
                }
            )

    candidates = _aggregate_and_score(by_stop)
    candidates = [c for c in candidates if c.score >= min_score]
    candidates.sort(key=lambda c: c.score, reverse=True)
    if max_candidates > 0:
        candidates = candidates[:max_candidates]
    return candidates


def _is_inside_any_segment_on_a(
    cum_a_stop: float,
    segments: Sequence[SharedSegment],
) -> bool:
    for seg in segments:
        if seg.start_cum_m - 1e-6 <= cum_a_stop <= seg.end_cum_m + 1e-6:
            return True
    return False


def _aggregate_and_score(
    by_stop: dict[str, list[dict]],
) -> list[RefillCandidate]:
    """Collapse multi-route observations into one scored candidate per stop.

    When the same ``stop_id`` is contributed by multiple production routes
    we use the *minimum* distance to A's polyline (best evidence), the
    *minimum* origin distance across sources (best origin-alignment
    evidence — though by construction every observation already passed
    the origin-aligned gate), and the *maximum* shared-segment length
    (longest corroborating corridor). Popularity = number of distinct
    contributing routes.
    """
    out: list[RefillCandidate] = []
    for sid, hits in by_stop.items():
        if not hits:
            continue
        distances = [h["distance_m"] for h in hits]
        origin_distances = [h["origin_distance_m"] for h in hits]
        seg_lengths = [h["max_segment_m"] for h in hits]
        source_routes = sorted({h["source_route_id"] for h in hits})

        best_distance = min(distances)
        # Pick the lat/lon from the hit with the best distance — that is the
        # observation closest to A's polyline.
        best_hit = min(hits, key=lambda h: h["distance_m"])

        score = compute_geometric_score(
            distance_m=best_distance,
            routes_count=len(source_routes),
            max_shared_segment_m=max(seg_lengths),
        )

        out.append(
            RefillCandidate(
                stop_id=sid,
                lat=best_hit["lat"],
                lon=best_hit["lon"],
                distance_to_polyline_m=best_distance,
                origin_distance_m=min(origin_distances),
                routes_count=len(source_routes),
                max_shared_segment_m=max(seg_lengths),
                score=score,
                source_route_ids=source_routes,
            )
        )
    return out
