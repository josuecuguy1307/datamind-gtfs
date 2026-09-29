"""Shape quality rules — gaps C7, C14.

Each rule is a pure function: (Shape) -> list[EntityIssue]. No DB writes.
"""
from __future__ import annotations

from typing import Callable, List

from ..config import SHAPE_GAP_MAX_KM, haversine_m
from ..models import EntityIssue, Shape, Severity


def rule_shape_gap(shape: Shape) -> List[EntityIssue]:
    """C7: Consecutive shape points separated by >5km.

    phase_origin: 3 (Valhalla routing or Phase 5 shape building)
    rule_name: shape_gap_too_large
    """
    issues: List[EntityIssue] = []
    pts = sorted(shape.points, key=lambda p: p.sequence)

    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        dist_m = haversine_m(a.lat, a.lon, b.lat, b.lon)
        dist_km = dist_m / 1000.0
        if dist_km > SHAPE_GAP_MAX_KM:
            issues.append(EntityIssue(
                entity_type="shape", entity_id=shape.shape_id,
                rule_name="shape_gap_too_large", severity=Severity.ERROR,
                description=(
                    f"Gap of {dist_km:.2f}km between shape points "
                    f"seq {a.sequence} and {b.sequence}"
                ),
                original_value={
                    "from_seq": a.sequence, "to_seq": b.sequence,
                    "gap_km": round(dist_km, 2),
                    "from_coord": (a.lat, a.lon), "to_coord": (b.lat, b.lon),
                },
                phase_origin=3,
            ))
    return issues


def rule_shape_self_intersection(shape: Shape) -> List[EntityIssue]:
    """C14: Route geometry self-intersects (figure-8 that isn't a real loop).

    Uses a sweep-line approach on consecutive segments. Only flags crossings
    between non-adjacent segments (gap >= 2 in sequence).

    phase_origin: 3
    rule_name: shape_self_intersection
    """
    pts = sorted(shape.points, key=lambda p: p.sequence)
    if len(pts) < 4:
        return []

    segments = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    crossings = 0

    for i in range(len(segments)):
        for j in range(i + 2, len(segments)):
            if _segments_cross(segments[i], segments[j]):
                crossings += 1
                if crossings >= 3:
                    break
        if crossings >= 3:
            break

    if crossings > 0:
        return [EntityIssue(
            entity_type="shape", entity_id=shape.shape_id,
            rule_name="shape_self_intersection", severity=Severity.WARNING,
            description=f"Shape has {crossings} self-intersection(s)",
            original_value=crossings, phase_origin=3,
        )]
    return []


def _ccw(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _segments_cross(seg1, seg2) -> bool:
    """Test if two segments cross using the CCW orientation test."""
    a, b = seg1
    c, d = seg2
    d1 = _ccw(a.lon, a.lat, b.lon, b.lat, c.lon, c.lat)
    d2 = _ccw(a.lon, a.lat, b.lon, b.lat, d.lon, d.lat)
    d3 = _ccw(c.lon, c.lat, d.lon, d.lat, a.lon, a.lat)
    d4 = _ccw(c.lon, c.lat, d.lon, d.lat, b.lon, b.lat)

    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return False


RULES: List[Callable[[Shape], List[EntityIssue]]] = [
    rule_shape_gap,
    rule_shape_self_intersection,
]
