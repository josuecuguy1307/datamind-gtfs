"""
core — the public ``path_aware_synthesis`` entry point.

Responsibilities (from hades-path-aware-synthesis §5 and
IMPLEMENTATION_STATUS.md §2.2):

1. Ensure the route's inferred-path polyline is available.
2. Check per-route cap BEFORE running cap-consuming stages (3d / 4).
3. Walk the stage ladder in order (3a → 3b → 3c → 3d → 4), stopping on
   the first success.
4. On success:
   - allocate a synthetic osm_id (negative sequence),
   - insert the node row with BOTH ``source_type`` enum and ``source``
     audit string,
   - call ``synthesis_events.log_synthesis_event`` in the same DB txn,
   - call ``review_queue_writer.write_synthetic_review`` (filesystem-only,
     after commit).
5. On full failure or cap-trip, log a rejection event and (for 4_pure)
   enqueue a 06c_rerun via ``research_queue.write_prompt``.

This module does NOT wire into ``discovery_pipeline.py``. That is Phase 3.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from datamind_console.common import (
    research_queue as rq,
    review_queue_writer as rw,
    synthesis_events as se,
)
from datamind_console.phases.phase3_routes.synthesis import (
    path_inference as pi,
    poi_matcher as pm,
    stages as st,
)

log = logging.getLogger(__name__)

SKILL_ID = "hades-path-aware-synthesis"

# Distance threshold for semantic-spatial conflict (§5 "Semantic-spatial
# conflict detection").  If the 3a-chosen coord diverges from research
# coords by more than 80 m we mark the review for the conflicts folder.
SEMANTIC_CONFLICT_DISTANCE_M = 80.0

# Per-unit-week calibration ceiling (§8). Above this we stop consuming cap
# for the rest of the week — research queue is the release valve.
UNIT_WEEKLY_CAP = 20

# Research coords must carry at least this many decimal digits of precision
# (≈ 11 m resolution). Fewer digits means the researcher only had block-level
# precision, which makes stage 3d's snap-distance bands meaningless.
RESEARCH_COORDS_MIN_DECIMALS = 4


# ---------------------------------------------------------------------------
# inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    """One anchor from the Deep Research response."""
    name: str
    research_coords: Optional[tuple[float, float]] = None
    road_tokens: tuple[str, ...] = ()  # 0, 1, or 2+ extracted road names
    research_output_file: Optional[str] = None  # basename only


@dataclass(frozen=True)
class Route:
    """Route context handed to synthesis."""
    route_id: str  # uuid
    route_code: str
    unit: str
    province: str
    termini: tuple[tuple[float, float], ...]
    osm_relation_id: Optional[int] = None
    grounded_stop_coords: tuple[tuple[float, float], ...] = ()
    must_pass_through: tuple[tuple[float, float], ...] = ()


@dataclass
class SynthesisResult:
    success: bool
    stage: Optional[str] = None
    node_id: Optional[str] = None
    osm_id: Optional[int] = None
    final_coords: Optional[tuple[float, float]] = None
    source: Optional[str] = None
    source_type: Optional[str] = None
    synthetic_confidence: Optional[str] = None
    rejected_reason: Optional[str] = None
    semantic_spatial_conflict: bool = False
    review_path: Optional[Path] = None
    stage_results: list[st.StageResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _allocate_synthetic_osm_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT nextval('node_prod.synthetic_osm_id_seq')")
        row = cur.fetchone()
    if not row:
        raise RuntimeError("synthetic_osm_id_seq returned no row")
    val = row[0] if isinstance(row, (tuple, list)) else row.get("nextval")
    return int(val)


def _insert_node(
    conn,
    *,
    node_id: str,                # accepted but ignored — treater allocates a fresh UUID
    osm_id: int,
    lat: float,
    lon: float,
    source: str,
    source_type: str,
    synthetic_confidence: str,
    route_id: str,               # not a node_prod.nodes column; tracked via synthesis_events FK
    anchor_name: str,            # not a node_prod.nodes column; used as proposed_name
    poi_osm_id: Optional[int] = None,
    poi_to_path_distance_m: Optional[float] = None,
    path_projection_distance_m: Optional[float] = None,
    research_to_projection_distance_m: Optional[float] = None,
) -> str:
    """Insert one synthesized node row via the universal treater.

    Pre-treater this function carried five SQL bugs (referenced columns
    ``lat``, ``lon``, ``route_id``, ``anchor_name``, ``poi_osm_id`` that
    do not exist on ``node_prod.nodes``). 0 rows ever made it into
    ``node_prod.synthesis_events`` from this code path. The migration
    fixes those by routing through ``treat_stop`` which owns the correct
    INSERT shape (and adds normalize/forbidden/place-mapping/audit).

    Returns the actually-allocated node_id (the treater assigns its own
    UUID; the ``node_id`` argument is preserved in the signature for
    backward compat but ignored).
    """
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput,
        treat_stop,
    )

    extras = {
        "osm_id": osm_id,
        "source": source,
        "source_type": source_type,
        "synthetic_confidence": synthetic_confidence,
        "poi_anchor_osm_id": poi_osm_id,
        "poi_to_path_distance_m": poi_to_path_distance_m,
        "path_projection_distance_m": path_projection_distance_m,
        "research_to_projection_distance_m": research_to_projection_distance_m,
    }
    extras = {k: v for k, v in extras.items() if v is not None}

    result = treat_stop(
        StopTreatmentInput(
            operation="synthetic_insert",
            caller=f"synthesis.core:{source_type}",
            proposed_name=anchor_name or "",
            proposed_lat=lat,
            proposed_lon=lon,
            extras=extras,
        ),
        conn,
    )
    if not result.success:
        raise RuntimeError(f"synthesis treatment failed: {result.error}")
    return str(result.node_id)


def _enqueue_06c_rerun(
    *,
    research_queue_root: Path,
    route: Route,
    anchor: Anchor,
    reason: str,
    priority_bump: bool = False,
) -> Optional[Path]:
    extras = {"priority_bump": True} if priority_bump else None
    try:
        return rq.write_prompt(
            queue_root=research_queue_root,
            prompt_type="stop_grounding_detail",
            route_code=route.route_code,
            unit=route.unit,
            province=route.province,
            trigger_condition=(
                f"path_aware_synthesis rerun: {reason}"
            ),
            priority=0 if priority_bump else 1,
            estimated_research_budget="standard",
            depends_on=[],
            dedup_key=(
                f"stop_grounding_detail:{route.unit}:{route.route_code}:"
                f"rerun:{st.slugify_anchor(anchor.name)}"
            ),
            prompt_markdown_content=(
                f"# 06c rerun — {anchor.name}\n\n"
                f"Reason: {reason}\n"
                f"Route: {route.route_code} / {route.unit}\n"
            ),
            generated_by_skill=SKILL_ID,
            extra_frontmatter=extras,
        )
    except Exception as e:  # pragma: no cover — best-effort
        log.warning("06c rerun enqueue failed: %s", e)
        return None


def _research_coords_precision_ok(coords: tuple[float, float]) -> bool:
    """True iff both lat and lon carry at least
    :data:`RESEARCH_COORDS_MIN_DECIMALS` decimal digits in their float
    representation. Guards against block-resolution coords feeding stage 3d.
    """
    def _ok(v: float) -> bool:
        s = repr(float(v))
        if "." not in s:
            return False
        frac = s.split(".", 1)[1].split("e", 1)[0].rstrip("0")
        return len(frac) >= RESEARCH_COORDS_MIN_DECIMALS
    return _ok(coords[0]) and _ok(coords[1])


def _ensure_path(route: Route, conn, force_recompute: bool = False) -> pi.RoutePath:
    return pi.compute_route_path(
        route_id=route.route_id,
        termini=route.termini,
        grounded_stops=route.grounded_stop_coords,
        must_pass_through=route.must_pass_through,
        osm_relation_id=route.osm_relation_id,
        conn=conn,
        force_recompute=force_recompute,
    )


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


def path_aware_synthesis(
    anchor: Anchor,
    route: Route,
    *,
    conn,
    review_root: Path,
    research_queue_root: Optional[Path] = None,
    now: Optional[datetime] = None,
    http_post: Optional[Callable] = None,
    overpass_fetcher: Optional[Callable] = None,
    force_recompute_path: bool = False,
) -> SynthesisResult:
    """Run the full synthesis ladder for one anchor on one route.

    Returns a :class:`SynthesisResult` describing what happened. On
    success, the node row and audit event are committed to ``conn`` and
    the review file is written to ``review_root``.
    """
    now = now or datetime.now(timezone.utc)
    review_root = Path(review_root)

    # Precondition: research coords must carry enough precision for stage 3d
    # to be meaningful. Fail fast without any DB or filesystem writes.
    if (
        anchor.research_coords is not None
        and not _research_coords_precision_ok(anchor.research_coords)
    ):
        return SynthesisResult(
            success=False,
            rejected_reason="research_coords_low_precision",
        )

    route_path = _ensure_path(route, conn, force_recompute=force_recompute_path)

    ctx = st.StageContext(
        route_id=route.route_id,
        route_code=route.route_code,
        unit=route.unit,
        province=route.province,
        polyline=route_path.polyline,
        grounded_stop_coords=list(route.grounded_stop_coords),
        http_post=http_post,
        overpass_fetcher=overpass_fetcher,
    )

    collected: list[st.StageResult] = []

    # ---- 3a POI-on-path ---------------------------------------------------
    r3a = st.stage_3a_poi_on_path(ctx, anchor.name)
    collected.append(r3a)
    if r3a.success:
        return _persist_winner(
            conn=conn, review_root=review_root, route=route, anchor=anchor,
            winner=r3a, route_path=route_path, now=now, all_results=collected,
        )

    # ---- 3b / 3c — need road tokens --------------------------------------
    if len(anchor.road_tokens) >= 2:
        r3c = st.stage_3c_path_intersection(
            ctx, anchor.name,
            road_token_a=anchor.road_tokens[0],
            road_token_b=anchor.road_tokens[1],
        )
        collected.append(r3c)
        if r3c.success:
            return _persist_winner(
                conn=conn, review_root=review_root, route=route, anchor=anchor,
                winner=r3c, route_path=route_path, now=now, all_results=collected,
            )
    if anchor.road_tokens:
        r3b = st.stage_3b_path_corridor(
            ctx, anchor.name, road_token=anchor.road_tokens[0],
        )
        collected.append(r3b)
        if r3b.success:
            return _persist_winner(
                conn=conn, review_root=review_root, route=route, anchor=anchor,
                winner=r3b, route_path=route_path, now=now, all_results=collected,
            )

    # ---- 3d — need research coords ---------------------------------------
    if anchor.research_coords is not None:
        # cap-gate before 3d (weight 0.5): route cap OR unit-weekly cap
        cap_type = _which_cap_hit(conn, route, now=now)
        if cap_type is not None:
            return _reject_cap_hit(
                conn=conn, review_root=review_root, route=route, anchor=anchor,
                attempted_stage=st.STAGE_3D, research_queue_root=research_queue_root,
                all_results=collected, cap_type=cap_type,
            )
        r3d = st.stage_3d_research_coords_snapped(
            ctx, anchor.name, research_coords=anchor.research_coords,
        )
        collected.append(r3d)
        if r3d.success:
            return _persist_winner(
                conn=conn, review_root=review_root, route=route, anchor=anchor,
                winner=r3d, route_path=route_path, now=now, all_results=collected,
            )

    # ---- 4 pure synthesis — cap-gated ------------------------------------
    cap_type = _which_cap_hit(conn, route, now=now)
    if cap_type is not None:
        return _reject_cap_hit(
            conn=conn, review_root=review_root, route=route, anchor=anchor,
            attempted_stage=st.STAGE_4, research_queue_root=research_queue_root,
            all_results=collected, cap_type=cap_type,
        )
    r4 = st.stage_4_pure_synthesis(ctx, anchor.name)
    collected.append(r4)
    if r4.success:
        result = _persist_winner(
            conn=conn, review_root=review_root, route=route, anchor=anchor,
            winner=r4, route_path=route_path, now=now, all_results=collected,
        )
        # §5.4 — stage 4 always enqueues a 06c rerun
        if research_queue_root is not None:
            _enqueue_06c_rerun(
                research_queue_root=Path(research_queue_root),
                route=route, anchor=anchor,
                reason="stage_4_pure_synthesis_fired",
            )
        return result

    # ---- all stages failed -----------------------------------------------
    se.log_synthesis_event(
        conn,
        node_id=None,
        route_id=route.route_id,
        unit=route.unit,
        province=route.province,
        stage=st.STAGE_4,  # arbitrary — rejection row
        anchor_name=anchor.name,
        research_coords=anchor.research_coords,
        rejected_reason="all_stages_failed",
        triggered_by_skill=SKILL_ID,
        research_output_file=anchor.research_output_file,
    )
    return SynthesisResult(
        success=False,
        rejected_reason="all_stages_failed",
        stage_results=collected,
    )


# ---------------------------------------------------------------------------
# persistence helpers
# ---------------------------------------------------------------------------


def _cap_hit(conn, route: Route) -> bool:
    consumed = se.compute_route_cap_consumed(conn, route_id=route.route_id)
    return se.is_route_at_cap(consumed)


def _which_cap_hit(
    conn, route: Route, *, now: Optional[datetime] = None
) -> Optional[str]:
    """Return the kind of cap that is currently tripped, or None.

    Checks in priority order: route cap (3.0 weighted units) first, then
    unit-weekly cap (20 events in the last 7 days).
    """
    if _cap_hit(conn, route):
        return "route_synthesis_cap_hit"
    try:
        weekly = se.count_synthesis_events_for_unit_week(
            conn, unit=route.unit, reference=now
        )
    except Exception as e:  # pragma: no cover — defensive
        log.warning("unit-weekly cap probe failed: %s", e)
        return None
    if weekly >= UNIT_WEEKLY_CAP:
        return "unit_weekly_cap_hit"
    return None


def _reject_cap_hit(
    *,
    conn,
    review_root: Path,
    route: Route,
    anchor: Anchor,
    attempted_stage: str,
    research_queue_root: Optional[Path],
    all_results: list[st.StageResult],
    cap_type: str = "route_synthesis_cap_hit",
) -> SynthesisResult:
    se.log_synthesis_event(
        conn,
        node_id=None,
        route_id=route.route_id,
        unit=route.unit,
        province=route.province,
        stage=attempted_stage,
        anchor_name=anchor.name,
        research_coords=anchor.research_coords,
        rejected_reason=cap_type,
        triggered_by_skill=SKILL_ID,
        research_output_file=anchor.research_output_file,
    )
    if hasattr(conn, "commit"):
        conn.commit()
    if research_queue_root is not None:
        _enqueue_06c_rerun(
            research_queue_root=Path(research_queue_root),
            route=route, anchor=anchor,
            reason=f"{cap_type}_at_{attempted_stage}",
            priority_bump=True,
        )
    return SynthesisResult(
        success=False,
        rejected_reason=cap_type,
        stage=attempted_stage,
        stage_results=all_results,
    )


def _persist_winner(
    *,
    conn,
    review_root: Path,
    route: Route,
    anchor: Anchor,
    winner: st.StageResult,
    route_path: pi.RoutePath,
    now: datetime,
    all_results: list[st.StageResult],
) -> SynthesisResult:
    # semantic-spatial conflict detection
    conflict = _detect_semantic_conflict(winner, anchor)

    osm_id = _allocate_synthetic_osm_id(conn)
    lat, lon = winner.final_coords  # type: ignore[misc]

    # Node insert and the audit event must land atomically: if the event
    # insert raises (CHECK violation, FK mismatch, connection drop), roll the
    # node insert back so we never have a node row without its audit trail.
    try:
        node_id = _insert_node(
            conn,
            node_id="",                 # ignored — treater allocates
            osm_id=osm_id,
            lat=lat, lon=lon,
            source=winner.source or "",
            source_type=winner.source_type or "",
            synthetic_confidence=winner.synthetic_confidence or "low",
            route_id=route.route_id,
            anchor_name=anchor.name,
            poi_osm_id=(winner.poi_match.osm_id if winner.poi_match else None),
            poi_to_path_distance_m=winner.poi_to_path_distance_m,
            path_projection_distance_m=winner.path_projection_distance_m,
            research_to_projection_distance_m=winner.research_to_projection_distance_m,
        )

        se.log_synthesis_event(
            conn,
            node_id=node_id,
            route_id=route.route_id,
            unit=route.unit,
            province=route.province,
            stage=winner.stage,
            anchor_name=anchor.name,
            research_coords=anchor.research_coords,
            final_coords=winner.final_coords,
            match_score=(winner.poi_match.name_similarity if winner.poi_match else None),
            triggered_by_skill=SKILL_ID,
            research_output_file=anchor.research_output_file,
        )
    except Exception as e:
        log.error("synthesis persist failed node=%s: %s", node_id, e)
        if hasattr(conn, "rollback"):
            try:
                conn.rollback()
            except Exception as r:  # pragma: no cover — defensive
                log.warning("rollback after persist failure itself failed: %s", r)
        raise

    # commit the DB txn before writing the review file (filesystem is not
    # transactional; atomic-write + idempotent (node_id, stage) make re-run
    # safe).
    if hasattr(conn, "commit"):
        conn.commit()

    try:
        review_path = rw.write_synthetic_review(
            review_root=review_root,
            node_id=node_id,
            osm_id=osm_id,
            stage=winner.stage,
            source_type=winner.source_type,
            source=winner.source,
            unit=route.unit,
            province=route.province,
            route_code=route.route_code,
            route_id=route.route_id,
            anchor_name=anchor.name,
            synthetic_confidence=winner.synthetic_confidence or "low",
            final_coords=winner.final_coords,
            research_coords=anchor.research_coords,
            poi_to_path_distance_m=winner.poi_to_path_distance_m,
            path_projection_distance_m=winner.path_projection_distance_m,
            research_to_projection_distance_m=winner.research_to_projection_distance_m,
            polyline_source=route_path.source,
            semantic_spatial_conflict=conflict,
            cap_weight_consumed=se.stage_weight(winner.stage),
            triggered_by_skill=SKILL_ID,
            research_output_file=anchor.research_output_file,
            evidence_markdown=_format_evidence(winner),
            why_synthesis_fired=_format_why(winner, all_results),
            now=now,
        )
    except Exception as e:
        log.error("review file write failed node=%s: %s", node_id, e)
        review_path = None

    return SynthesisResult(
        success=True,
        stage=winner.stage,
        node_id=node_id,
        osm_id=osm_id,
        final_coords=winner.final_coords,
        source=winner.source,
        source_type=winner.source_type,
        synthetic_confidence=winner.synthetic_confidence,
        semantic_spatial_conflict=conflict,
        review_path=review_path,
        stage_results=all_results,
    )


def _detect_semantic_conflict(winner: st.StageResult, anchor: Anchor) -> bool:
    """§5 semantic-spatial conflict: if 3a won but research coords exist and
    are > 80 m from the chosen coord, flag it."""
    if winner.stage != st.STAGE_3A:
        return False
    if anchor.research_coords is None or winner.final_coords is None:
        return False
    d = pi.haversine_m(anchor.research_coords, winner.final_coords)
    return d > SEMANTIC_CONFLICT_DISTANCE_M


def _format_evidence(winner: st.StageResult) -> str:
    lines = [f"- {e}" for e in winner.evidence]
    if not lines:
        lines = ["- (no structured evidence)"]
    return "\n".join(lines) + "\n"


def _format_why(winner: st.StageResult, all_results: Sequence[st.StageResult]) -> str:
    parts = [f"Stage {winner.stage} succeeded for anchor {winner.anchor_name!r}."]
    for r in all_results:
        if r is winner:
            continue
        if r.rejected_reason:
            parts.append(f"- {r.stage}: rejected ({r.rejected_reason})")
    return "\n".join(parts) + "\n"
