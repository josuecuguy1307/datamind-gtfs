"""Route fixers — fragment merge, stop sequence clone.

All fixers are idempotent and return FixAttempt with confidence score.
"""
from __future__ import annotations

from typing import List, Optional

from ..models import EntityIssue, FixAttempt, Route


def fix_merge_route_fragment(route: Route, issue: EntityIssue,
                              candidates: Optional[List[Route]] = None) -> FixAttempt:
    """Attempt to merge a short route (<3 stops) with a parent route.

    Checks if route shares service_route_id with a longer route.
    Confidence: fragment_overlap_ratio with candidate parent route.

    phase_origin: 3
    rule_name: route_too_few_stops
    """
    if not candidates:
        return FixAttempt(
            success=False, new_value=None, confidence=0.0,
            log=f"No candidate routes to merge fragment {route.route_id[:8]} into",
        )

    best_match = None
    best_overlap = 0.0

    for candidate in candidates:
        if candidate.route_id == route.route_id:
            continue
        if candidate.service_route_id and candidate.service_route_id == route.service_route_id:
            # Check stop overlap
            my_stops = set(route.stop_node_ids)
            their_stops = set(candidate.stop_node_ids)
            if not my_stops:
                continue
            overlap = len(my_stops & their_stops) / len(my_stops)
            if overlap > best_overlap:
                best_overlap = overlap
                best_match = candidate

    if best_match and best_overlap > 0:
        return FixAttempt(
            success=True,
            new_value={"merge_into": best_match.route_id, "overlap_ratio": best_overlap},
            confidence=best_overlap,
            log=f"Merge fragment {route.route_id[:8]} into {best_match.route_id[:8]} (overlap={best_overlap:.2f})",
        )

    # Try opposite direction clone
    for candidate in candidates:
        if (candidate.service_route_id == route.service_route_id and
                candidate.direction_id != route.direction_id and
                len(candidate.stop_node_ids) >= 3):
            return FixAttempt(
                success=True,
                new_value={"clone_from": candidate.route_id, "direction": candidate.direction_id},
                confidence=0.6,
                log=f"Clone stops from opposite direction {candidate.route_id[:8]} for {route.route_id[:8]}",
            )

    return FixAttempt(
        success=False, new_value=None, confidence=0.0,
        log=f"No viable merge/clone target for fragment {route.route_id[:8]}",
    )


FIXERS = {
    "route_too_few_stops": fix_merge_route_fragment,
    "route_no_schedule": lambda route, issue: FixAttempt(
        success=False, new_value=None, confidence=0.0,
        log=f"No auto-fix for missing schedule on {route.route_id[:8]} — needs Phase 4 catalog",
    ),
}
