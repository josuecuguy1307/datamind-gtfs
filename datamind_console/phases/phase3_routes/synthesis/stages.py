"""
stages — pure-function implementations of the 5 path-aware synthesis stages.

Each ``stage_*`` function returns a :class:`StageResult`.  No DB writes.
No HTTP side effects beyond what the injected helpers perform.  Callers
(``core.path_aware_synthesis``) decide which stages to run, persist the
winner, and handle cap accounting.

Stages map to the ``node_prod.synthesis_events.stage`` CHECK values:

- 3a_poi_on_path              — ``stage_3a_poi_on_path``
- 3b_path_corridor            — ``stage_3b_path_corridor``
- 3c_path_intersection        — ``stage_3c_path_intersection``
- 3d_research_coords_snapped  — ``stage_3d_research_coords_snapped``
- 4_pure_synthesis            — ``stage_4_pure_synthesis``
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from datamind_console.common import text_utils as tu
from datamind_console.phases.phase3_routes.synthesis import (
    path_inference as pi,
    poi_matcher as pm,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------


STAGE_3A = "3a_poi_on_path"
STAGE_3B = "3b_path_corridor"
STAGE_3C = "3c_path_intersection"
STAGE_3D = "3d_research_coords_snapped"
STAGE_4 = "4_pure_synthesis"

SOURCE_TYPE_POI = "poi_anchored_path_projected"
SOURCE_TYPE_CORRIDOR = "path_corridor_projected"
SOURCE_TYPE_INTERSECTION = "path_intersection"
SOURCE_TYPE_RESEARCH_SNAPPED = "research_coords_path_snapped"
SOURCE_TYPE_PURE = "pure_synthesis"

CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"

# Per §5.3b disambiguation modifiers
ANCHOR_MODIFIERS = ("norte", "sur", "alto", "bajo", "entrada", "salida")

# Path-corridor thresholds (§5.3b)
CORRIDOR_OVERLAP_MIN_M = 50.0
# Path-intersection ceiling (§5.3c)
INTERSECTION_TO_PATH_MAX_M = 30.0
# Stage 3d bands (§5.3d)
STAGE_3D_HIGH_CONF_MAX_M = 30.0
STAGE_3D_LOW_CONF_MAX_M = 80.0


@dataclass
class StageContext:
    """Shared state handed to each stage.

    ``http_post`` is injected for tests — stages call ``pm.query_pois_by_name``
    with ``http_post`` so the real Overpass endpoint is not touched.
    """
    route_id: str
    route_code: str
    unit: str
    province: str
    polyline: str
    grounded_stop_coords: Sequence[tuple[float, float]] = ()
    bbox: Optional[tuple[float, float, float, float]] = None
    http_post: Optional[Callable] = None
    overpass_fetcher: Optional[Callable] = None


@dataclass(frozen=True)
class StageResult:
    stage: str
    success: bool
    final_coords: Optional[tuple[float, float]] = None
    source_type: Optional[str] = None
    source: Optional[str] = None
    synthetic_confidence: Optional[str] = None

    # diagnostic — stages populate what they know
    anchor_name: Optional[str] = None
    poi_match: Optional[pm.POIMatch] = None
    access_point: Optional[pm.POIAccessPoint] = None
    poi_to_path_distance_m: Optional[float] = None
    path_projection_distance_m: Optional[float] = None
    research_to_projection_distance_m: Optional[float] = None
    research_coords: Optional[tuple[float, float]] = None
    rejected_reason: Optional[str] = None
    evidence: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def slugify_anchor(anchor_name: str) -> str:
    """Lowercase, accent-stripped, hyphen-joined anchor slug."""
    return tu.slugify(anchor_name) or "anchor"


def _source_string(source_type: str, route_code: str, anchor_name: str) -> str:
    return f"{source_type}:{route_code}:{slugify_anchor(anchor_name)}"


def _bbox_from_ctx(ctx: StageContext) -> tuple[float, float, float, float]:
    if ctx.bbox is not None:
        return ctx.bbox
    # derive from polyline if bbox not explicitly set
    pts = pi.decode_polyline(ctx.polyline)
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    pad = 0.01
    return (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)


def _path_midpoint(polyline: str) -> Optional[tuple[float, float]]:
    pts = pi.decode_polyline(polyline)
    if not pts:
        return None
    return pts[len(pts) // 2]


# ---------------------------------------------------------------------------
# stage 3a — POI-on-path
# ---------------------------------------------------------------------------


def stage_3a_poi_on_path(
    ctx: StageContext,
    anchor_name: str,
    *,
    candidates: Optional[Sequence[pm.POIMatch]] = None,
    road_projection_coords: Optional[tuple[float, float]] = None,
) -> StageResult:
    """Look up POIs by name, filter by similarity + 80 m path distance, pick
    the closest, derive the access point.

    If ``candidates`` is None, Overpass is queried via
    :func:`poi_matcher.query_pois_by_name`.
    """
    if candidates is None:
        bbox = _bbox_from_ctx(ctx)
        try:
            candidates = pm.query_pois_by_name(
                anchor_name, bbox,
                tag_filters=pm.STAGE_3A_TAG_FILTERS,
                http_post=ctx.http_post,
            )
        except pm.POIMatcherError as e:
            log.warning("stage 3a overpass failed route=%s: %s", ctx.route_id, e)
            return StageResult(
                stage=STAGE_3A, success=False, anchor_name=anchor_name,
                rejected_reason=f"overpass_unreachable:{e}",
            )

    scored = pm.filter_and_score_candidates(
        anchor_name, candidates, ctx.polyline,
    )
    if not scored:
        return StageResult(
            stage=STAGE_3A, success=False, anchor_name=anchor_name,
            rejected_reason="no_poi_within_80m_of_path",
        )

    top = scored[0]
    ap = pm.poi_to_access_point(
        top, ctx.polyline, road_projection_coords=road_projection_coords,
    )
    return StageResult(
        stage=STAGE_3A,
        success=True,
        final_coords=(ap.lat, ap.lon),
        source_type=SOURCE_TYPE_POI,
        source=_source_string(SOURCE_TYPE_POI, ctx.route_code, anchor_name),
        synthetic_confidence=CONF_HIGH,
        anchor_name=anchor_name,
        poi_match=top,
        access_point=ap,
        poi_to_path_distance_m=top.poi_to_path_distance_m,
        evidence=(
            f"name_similarity={top.name_similarity:.2f}",
            f"poi_to_path_distance_m={top.poi_to_path_distance_m:.1f}",
            f"access_type={ap.access_type}",
        ),
    )


# ---------------------------------------------------------------------------
# stage 3b — path-corridor-projected
# ---------------------------------------------------------------------------


def _apply_modifier_bias(
    segment: Sequence[tuple[float, float]], modifier: Optional[str]
) -> tuple[float, float]:
    if not segment:
        raise ValueError("empty corridor segment")
    if modifier in ("norte", "alto", "entrada"):
        return segment[0]
    if modifier in ("sur", "bajo", "salida"):
        return segment[-1]
    return segment[len(segment) // 2]


def _detect_modifier(anchor_name: str) -> Optional[str]:
    low = pm._normalise_name(anchor_name)
    for mod in ANCHOR_MODIFIERS:
        if f" {mod} " in f" {low} ":
            return mod
    return None


def _segment_length_m(segment: Sequence[tuple[float, float]]) -> float:
    if len(segment) < 2:
        return 0.0
    total = 0.0
    for i in range(len(segment) - 1):
        total += pi.haversine_m(segment[i], segment[i + 1])
    return total


def stage_3b_path_corridor(
    ctx: StageContext,
    anchor_name: str,
    road_token: str,
) -> StageResult:
    """Identify the sub-polyline where a named road overlaps, place the stop
    at the corridor midpoint (biased by norte/sur/etc.).
    """
    try:
        segment = pi.polyline_segment_by_road_name(
            ctx.polyline, road_token, ctx.route_id,
            overpass_fetcher=ctx.overpass_fetcher,
        )
    except pi.PathInferenceError as e:
        log.warning("stage 3b overpass failed route=%s: %s", ctx.route_id, e)
        return StageResult(
            stage=STAGE_3B, success=False, anchor_name=anchor_name,
            rejected_reason=f"overpass_unreachable:{e}",
        )
    if not segment:
        return StageResult(
            stage=STAGE_3B, success=False, anchor_name=anchor_name,
            rejected_reason="no_named_road_on_path",
        )

    overlap_m = _segment_length_m(segment)
    if overlap_m < CORRIDOR_OVERLAP_MIN_M:
        return StageResult(
            stage=STAGE_3B, success=False, anchor_name=anchor_name,
            rejected_reason=f"corridor_overlap_too_short:{overlap_m:.0f}m",
        )

    modifier = _detect_modifier(anchor_name)
    coord = _apply_modifier_bias(segment, modifier)

    # distance from coord to the exact polyline (should be ~0 since segment is
    # a sub-polyline of the path)
    proj = pi.project_point_to_polyline(coord[0], coord[1], ctx.polyline)
    return StageResult(
        stage=STAGE_3B,
        success=True,
        final_coords=(proj.lat, proj.lon),
        source_type=SOURCE_TYPE_CORRIDOR,
        source=_source_string(SOURCE_TYPE_CORRIDOR, ctx.route_code, anchor_name),
        synthetic_confidence=CONF_MEDIUM,
        anchor_name=anchor_name,
        path_projection_distance_m=proj.distance_m,
        evidence=(
            f"corridor_overlap_m={overlap_m:.0f}",
            f"modifier={modifier or 'none'}",
        ),
    )


# ---------------------------------------------------------------------------
# stage 3c — path-intersection
# ---------------------------------------------------------------------------


def stage_3c_path_intersection(
    ctx: StageContext,
    anchor_name: str,
    road_token_a: str,
    road_token_b: str,
) -> StageResult:
    """Find the intersection of two named roads nearest to the path."""
    try:
        inter = pi.find_intersection_along_polyline(
            ctx.polyline, road_token_a, road_token_b, ctx.route_id,
            overpass_fetcher=ctx.overpass_fetcher,
        )
    except pi.PathInferenceError as e:
        log.warning("stage 3c overpass failed route=%s: %s", ctx.route_id, e)
        return StageResult(
            stage=STAGE_3C, success=False, anchor_name=anchor_name,
            rejected_reason=f"overpass_unreachable:{e}",
        )
    if inter is None:
        return StageResult(
            stage=STAGE_3C, success=False, anchor_name=anchor_name,
            rejected_reason="no_intersection_found",
        )

    proj = pi.project_point_to_polyline(inter[0], inter[1], ctx.polyline)
    if proj.distance_m > INTERSECTION_TO_PATH_MAX_M:
        return StageResult(
            stage=STAGE_3C, success=False, anchor_name=anchor_name,
            path_projection_distance_m=proj.distance_m,
            rejected_reason=f"intersection_too_far_from_path:{proj.distance_m:.0f}m",
        )
    return StageResult(
        stage=STAGE_3C,
        success=True,
        final_coords=(proj.lat, proj.lon),
        source_type=SOURCE_TYPE_INTERSECTION,
        source=_source_string(SOURCE_TYPE_INTERSECTION, ctx.route_code, anchor_name),
        synthetic_confidence=CONF_MEDIUM,
        anchor_name=anchor_name,
        path_projection_distance_m=proj.distance_m,
        evidence=(
            f"road_a={road_token_a}", f"road_b={road_token_b}",
            f"distance_to_path_m={proj.distance_m:.0f}",
        ),
    )


# ---------------------------------------------------------------------------
# stage 3d — research-coords-path-snapped
# ---------------------------------------------------------------------------


def stage_3d_research_coords_snapped(
    ctx: StageContext,
    anchor_name: str,
    research_coords: tuple[float, float],
) -> StageResult:
    """Three-band classification per §5.3d.

    - ≤ 30 m: confidence high, snap to path.
    - 30–80 m: confidence low, snap to path (always pending review).
    - > 80 m: fail with ``rejected_reason='research_coords_too_far_from_path_80m'``.
    """
    proj = pi.project_point_to_polyline(
        research_coords[0], research_coords[1], ctx.polyline,
    )
    d = proj.distance_m

    if d > STAGE_3D_LOW_CONF_MAX_M:
        return StageResult(
            stage=STAGE_3D, success=False, anchor_name=anchor_name,
            research_coords=research_coords,
            research_to_projection_distance_m=d,
            rejected_reason="research_coords_too_far_from_path_80m",
        )

    confidence = CONF_HIGH if d <= STAGE_3D_HIGH_CONF_MAX_M else CONF_LOW
    return StageResult(
        stage=STAGE_3D,
        success=True,
        final_coords=(proj.lat, proj.lon),
        source_type=SOURCE_TYPE_RESEARCH_SNAPPED,
        source=_source_string(SOURCE_TYPE_RESEARCH_SNAPPED, ctx.route_code, anchor_name),
        synthetic_confidence=confidence,
        anchor_name=anchor_name,
        research_coords=research_coords,
        research_to_projection_distance_m=d,
        evidence=(
            f"research_to_projection_distance_m={d:.1f}",
            f"band={'high' if d <= STAGE_3D_HIGH_CONF_MAX_M else 'low'}",
        ),
    )


# ---------------------------------------------------------------------------
# stage 4 — pure synthesis
# ---------------------------------------------------------------------------


def stage_4_pure_synthesis(
    ctx: StageContext,
    anchor_name: str,
    *,
    between_grounded: Optional[tuple[tuple[float, float], tuple[float, float]]] = None,
    corridor_midpoint: Optional[tuple[float, float]] = None,
) -> StageResult:
    """Weakly-informed placement along the path. No POI, no intersection, no
    research coords. Last resort. Cap enforcement is the caller's job.

    Placement priority:
      1. midpoint of ``between_grounded`` (if given)
      2. ``corridor_midpoint`` (from a stage-3b run that fell short)
      3. path midpoint
    """
    coord: Optional[tuple[float, float]] = None
    reason = ""
    if between_grounded is not None:
        a, b = between_grounded
        coord = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        reason = "midpoint_between_grounded"
    elif corridor_midpoint is not None:
        coord = corridor_midpoint
        reason = "corridor_midpoint"
    else:
        coord = _path_midpoint(ctx.polyline)
        reason = "path_midpoint"

    if coord is None:
        return StageResult(
            stage=STAGE_4, success=False, anchor_name=anchor_name,
            rejected_reason="no_usable_placement",
        )
    proj = pi.project_point_to_polyline(coord[0], coord[1], ctx.polyline)
    return StageResult(
        stage=STAGE_4,
        success=True,
        final_coords=(proj.lat, proj.lon),
        source_type=SOURCE_TYPE_PURE,
        source=_source_string(SOURCE_TYPE_PURE, ctx.route_code, anchor_name),
        synthetic_confidence=CONF_LOW,
        anchor_name=anchor_name,
        path_projection_distance_m=proj.distance_m,
        evidence=(f"placement={reason}",),
    )
