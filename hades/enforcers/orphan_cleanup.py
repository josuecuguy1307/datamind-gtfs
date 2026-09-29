"""Pre-Ship Orphan Cleanup service (v2 — 3-category design).

Applies stop-polyline alignment classification (see
``stop_polyline_alignment``) to a route's proposed stops and produces
a cleaned stop list along with a structured cleanup report. Stops in
the snap band are moved to the polyline foot; orphans are dropped;
aligned stops are kept unchanged.

Design principle: the polyline is canonical and immutable. Every
preserved stop is literally on the polyline after cleanup — there are
no kept-as-is "near" stops floating off the route.

Read-only and stateless. The caller (typically the reclassifier's
pre-ship phase) decides whether to persist the cleaned stops based on
a non-destructiveness check — see ``validate_cleanup_not_destructive``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from hades.enforcers.stop_polyline_alignment import analyze_route_alignment


CLEANUP_VERSION = 2

DEFAULT_THRESHOLDS: dict[str, float] = {
    "aligned_threshold_m": 10.0,
    "snap_threshold_m": 60.0,
    "max_removal_pct": 0.20,
}


@dataclass
class OrphanCleanupReport:
    total_stops_before: int
    total_stops_after: int
    stops_aligned: int
    stops_snapped: int
    stops_removed: int
    snapped_stops: list[dict] = field(default_factory=list)
    orphans_removed: list[dict] = field(default_factory=list)
    thresholds_used: dict = field(default_factory=dict)
    cleanup_version: int = CLEANUP_VERSION


def cleanup_orphan_stops(
    stops: Sequence[dict],
    polyline: Sequence[tuple[float, float]],
    *,
    thresholds: Optional[dict] = None,
) -> tuple[list[dict], OrphanCleanupReport]:
    """Apply alignment-based cleanup to a route's stops.

    Returns ``(cleaned_stops, report)``:

      * aligned stops keep their original coordinates
      * snap stops are returned with updated lat/lon (the foot of the
        perpendicular onto the polyline) and carry a
        ``snapped_to_polyline`` flag
      * orphan stops are removed from the list

    Stop dicts must contain ``lat`` and ``lon``; ``stop_id`` and any
    other fields are preserved as-is on kept stops. Sequence order is
    preserved (no reindexing — the caller can renumber if needed).
    """
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    alignments = analyze_route_alignment(
        stops,
        polyline,
        aligned_threshold_m=t["aligned_threshold_m"],
        snap_threshold_m=t["snap_threshold_m"],
    )

    cleaned: list[dict] = []
    snapped_stops: list[dict] = []
    orphans_removed: list[dict] = []
    counts = {"aligned": 0, "snap": 0, "orphan": 0}

    for stop, align in zip(stops, alignments):
        counts[align.alignment_class] += 1

        if align.alignment_class == "orphan":
            orphans_removed.append({
                "stop_id": align.stop_id,
                "original_lat": align.original_coord[0],
                "original_lon": align.original_coord[1],
                "distance_to_polyline_m": align.distance_to_polyline_m,
                "reason": (
                    "polyline_empty"
                    if not polyline
                    else f"distance_{align.distance_to_polyline_m:.1f}m_exceeds_snap_threshold"
                ),
            })
            continue

        if (
            align.alignment_class == "snap"
            and align.projected_coord is not None
        ):
            new_lat, new_lon = align.projected_coord
            snapped_stops.append({
                "stop_id": align.stop_id,
                "original_lat": align.original_coord[0],
                "original_lon": align.original_coord[1],
                "snapped_lat": new_lat,
                "snapped_lon": new_lon,
                "distance_to_polyline_m": align.distance_to_polyline_m,
                "nearest_polyline_index": align.nearest_polyline_index,
            })
            cleaned.append({
                **stop,
                "lat": new_lat,
                "lon": new_lon,
                "snapped_to_polyline": True,
            })
        else:
            cleaned.append(dict(stop))

    report = OrphanCleanupReport(
        total_stops_before=len(stops),
        total_stops_after=len(cleaned),
        stops_aligned=counts["aligned"],
        stops_snapped=counts["snap"],
        stops_removed=counts["orphan"],
        snapped_stops=snapped_stops,
        orphans_removed=orphans_removed,
        thresholds_used=dict(t),
    )
    return cleaned, report


def validate_cleanup_not_destructive(
    original_stops: Sequence[dict],
    cleaned_stops: Sequence[dict],
    *,
    max_removal_pct: float = 0.20,
) -> tuple[bool, str]:
    """Safety gate: reject cleanups that drop too large a fraction of stops.

    Prevents catastrophic over-cleanup on routes whose stops are
    legitimately dispersed (interprovincial routes spanning multiple
    municipalities, atypical corridor mismatches, etc.).

    Returns ``(ok, reason)``. ``ok=True`` means the cleanup is safe to
    apply; ``reason`` is a short informational tag.
    """
    n_before = len(original_stops)
    if n_before == 0:
        return True, "ok_empty_input"

    removed = n_before - len(cleaned_stops)
    if removed <= 0:
        return True, "ok_no_removals"

    removal_pct = removed / n_before
    if removal_pct > max_removal_pct:
        return False, (
            f"cleanup_would_remove_{removal_pct:.0%}_of_stops_threshold_{max_removal_pct:.0%}"
        )

    return True, "ok"
