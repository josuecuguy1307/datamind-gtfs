from __future__ import annotations

import math
import os
import uuid
from typing import Any, Optional, List, Tuple
from phase3_routes.services.route_constructor.src.db.conn import db_cursor
from phase3_routes.services.route_constructor.src.geometry.valhalla_client import (
    valhalla_route,
    valhalla_route_with_meta,
)
from phase3_routes.services.route_constructor.src.geometry.scoring import score_geometry
from phase3_routes.services.route_constructor.src.db.route_work_repo import (
    create_geometry_set,
    insert_geometry_candidate,
)
from phase3_routes.services.route_constructor.src.settings import VALHALLA_COSTING

ANCHOR_TIGHTNESS_M = 60.0
RETURN_LOOP_TOLERANCE_M = 35.0


# ----------------------------
# UUID[] parsing (robust)
# ----------------------------

def _parse_uuid_array(v: Any) -> list[uuid.UUID]:
    if v is None:
        return []

    if isinstance(v, (list, tuple)):
        return [x if isinstance(x, uuid.UUID) else uuid.UUID(str(x)) for x in v]

    if isinstance(v, str):
        s = v.strip()
        if s.startswith("{") and s.endswith("}"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            parts = [p.strip().strip('"') for p in inner.split(",")]
            return [uuid.UUID(p) for p in parts if p]
        return [uuid.UUID(s)]

    return [uuid.UUID(str(v))]


def _parse_int_array(v: Any) -> list[int]:
    """
    Handles:
      - python list/tuple of ints/str
      - Postgres int[] text like "{1,2,3}"
      - single int/str
    """
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [int(x) for x in v]
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("{") and s.endswith("}"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            parts = [p.strip().strip('"') for p in inner.split(",")]
            return [int(p) for p in parts if p]
        return [int(s)]
    return [int(v)]


# ----------------------------
# Fetch stop coords
# ----------------------------

def _fetch_stop_points_from_canonical(conn, stop_node_ids: list[uuid.UUID]) -> list[tuple[float, float]]:
    """
    Returns [(lat, lon), ...] in SAME order as stop_node_ids.
    """
    if not stop_node_ids:
        return []

    ids = [str(x) for x in stop_node_ids]

    with db_cursor(conn) as cur:
        cur.execute(
            """
            SELECT node_id, ST_Y(geom) AS lat, ST_X(geom) AS lon
            FROM node_prod.nodes
            WHERE node_type='STOP'
              AND node_id = ANY(%s::uuid[])
            """,
            (ids,),
        )
        rows = cur.fetchall()

    by_id = {uuid.UUID(str(r["node_id"])): (float(r["lon"]), float(r["lat"])) for r in rows}

    missing = [x for x in stop_node_ids if x not in by_id]
    if missing:
        raise ValueError(f"Missing {len(missing)} stop(s) in node_prod.nodes: {missing[:5]}")

    return [by_id[x] for x in stop_node_ids]


def _fetch_stop_points_from_prior(
    conn,
    route_id: uuid.UUID,
    stop_prior_seqs: list[int],
) -> list[tuple[float, float]]:
    """
    Returns [(lat, lon), ...] in SAME order as stop_prior_seqs.
    Pulls from route_work.relation_stop_prior (lat/lon from Overpass extraction).
    """
    if not stop_prior_seqs:
        return []

    with db_cursor(conn) as cur:
        cur.execute(
            """
            SELECT seq, lat, lon
            FROM route_work.relation_stop_prior
            WHERE route_id=%s
              AND seq = ANY(%s::int[])
            """,
            (str(route_id), stop_prior_seqs),
        )
        rows = cur.fetchall()

    by_seq = {int(r["seq"]): (float(r["lon"]), float(r["lat"])) for r in rows}

    missing = [s for s in stop_prior_seqs if s not in by_seq]
    if missing:
        raise ValueError(f"Missing {len(missing)} seq(s) in relation_stop_prior: {missing[:10]}")

    return [by_seq[s] for s in stop_prior_seqs]


# ----------------------------
# WKT helpers (your existing)
# ----------------------------

def _looks_like_latlon(p0: tuple[float, float], p1: tuple[float, float]) -> bool:
    a0, b0 = p0
    a1, b1 = p1
    a_is_lat = -90 <= a0 <= 90 and -90 <= a1 <= 90
    b_is_lon = -180 <= b0 <= 180 and -180 <= b1 <= 180
    a_is_lon = -180 <= a0 <= 180 and -180 <= a1 <= 180
    b_is_lat = -90 <= b0 <= 90 and -90 <= b1 <= 90
    return (a_is_lat and b_is_lon) and not (a_is_lon and b_is_lat)


def _dedup_consecutive(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    last = None
    for c in coords:
        if last is None or c != last:
            out.append(c)
            last = c
    return out


def _to_linestring_wkt_from_valhalla_points(points: list[tuple[float, float]]) -> str:
    if not points:
        raise ValueError("Empty Valhalla shape")

    pts = [(round(float(a), 6), round(float(b), 6)) for (a, b) in points]
    pts = _dedup_consecutive(pts)

    if len(pts) < 2 or len(set(pts)) < 2:
        raise ValueError("Valhalla returned <2 distinct points")

    is_latlon = _looks_like_latlon(pts[0], pts[1])
    lonlat = [(lon, lat) for (lat, lon) in pts] if is_latlon else pts

    if len(lonlat) < 2 or len(set(lonlat)) < 2:
        raise ValueError("LineString needs at least 2 distinct points")

    coord_str = ", ".join(f"{lon} {lat}" for (lon, lat) in lonlat)
    return f"LINESTRING({coord_str})"


def _haversine_m(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    lon1, lat1 = p1
    lon2, lat2 = p2
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _truncate_at_first_repeat_markers(
    markers: list[str],
    coords: list[tuple[float, float]],
) -> tuple[list[str], list[tuple[float, float]], bool]:
    seen: set[str] = set()
    for i, m in enumerate(markers):
        if m in seen and i >= 2:
            return markers[:i], coords[:i], True
        seen.add(m)
    return markers, coords, False


def _truncate_at_first_coordinate_return(
    markers: list[str],
    coords: list[tuple[float, float]],
    tolerance_m: float = RETURN_LOOP_TOLERANCE_M,
) -> tuple[list[str], list[tuple[float, float]], bool]:
    for i in range(2, len(coords)):
        p = coords[i]
        for j in range(0, i - 1):
            if _haversine_m(p, coords[j]) <= tolerance_m:
                return markers[:i], coords[:i], True
    return markers, coords, False


def _nearest_vertex_index(point: tuple[float, float], shape: list[tuple[float, float]]) -> int:
    best_i = 0
    best_d = float("inf")
    for i, sp in enumerate(shape):
        d = _haversine_m(point, sp)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def _directional_tightness(
    requested_coords: list[tuple[float, float]],
    shape_pts: list[tuple[float, float]],
) -> dict[str, Any]:
    start_anchor_dist_m = _haversine_m(requested_coords[0], shape_pts[0])
    end_anchor_dist_m = _haversine_m(requested_coords[-1], shape_pts[-1])
    nearest_idx = [_nearest_vertex_index(p, shape_pts) for p in requested_coords]
    monotonic_violations = 0
    last = -1
    for idx in nearest_idx:
        if idx < last:
            monotonic_violations += 1
        last = max(last, idx)
    avg_stop_to_shape_m = 0.0
    if requested_coords:
        avg_stop_to_shape_m = sum(_haversine_m(p, shape_pts[_nearest_vertex_index(p, shape_pts)]) for p in requested_coords) / len(requested_coords)

    is_tight = (
        start_anchor_dist_m <= ANCHOR_TIGHTNESS_M
        and end_anchor_dist_m <= ANCHOR_TIGHTNESS_M
        and monotonic_violations == 0
    )
    return {
        "start_anchor_dist_m": float(start_anchor_dist_m),
        "end_anchor_dist_m": float(end_anchor_dist_m),
        "avg_stop_to_shape_m": float(avg_stop_to_shape_m),
        "monotonic_violations": int(monotonic_violations),
        "is_direction_tight": bool(is_tight),
    }


def _pin_shape_endpoints(
    requested_coords: list[tuple[float, float]],
    shape_pts: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """
    Force geometry to anchor exactly to first/last requested stops.
    This preserves Valhalla interior while guaranteeing route direction endpoints.
    """
    if len(shape_pts) < 2:
        return shape_pts
    pinned = list(shape_pts)
    pinned[0] = requested_coords[0]
    pinned[-1] = requested_coords[-1]
    return pinned


# ----------------------------
# Main builder (UPDATED)
# ----------------------------

def build_geometry_candidates_for_sequence(
    conn,
    route_id: uuid.UUID,
    stop_sequence_candidate_id: uuid.UUID,
) -> uuid.UUID:

    # Fetch candidate row: now supports both stop_node_ids and stop_prior_seqs
    with db_cursor(conn) as cur:
        cur.execute(
            """
            SELECT
              ssc.stop_node_ids,
              ssc.stop_prior_seqs,
              ssc.metrics,
              ssc.set_id,
              scs.route_id,
              sa.chosen_stop_sequence_candidate_id AS approved_stop_sequence_candidate_id,
              sa.approval_status AS sequence_approval_status
            FROM route_work.stop_sequence_candidates ssc
            JOIN route_work.stop_sequence_candidate_sets scs
              ON scs.set_id = ssc.set_id
            LEFT JOIN route_work.sequence_approvals sa
              ON sa.route_id = scs.route_id
            WHERE ssc.candidate_id=%s
            """,
            (str(stop_sequence_candidate_id),),
        )
        row = cur.fetchone()

    if not row:
        raise ValueError("stop_sequence_candidate_id not found")
    if str(row.get("route_id")) != str(route_id):
        raise ValueError("stop_sequence_candidate_id does not belong to route_id")
    approved_seq_id = str(row.get("approved_stop_sequence_candidate_id") or "").strip()
    approval_status = str(row.get("sequence_approval_status") or "").strip().lower()
    if not approved_seq_id or approval_status != "approved":
        raise RuntimeError("Canonical stop sequence is not approved for this route")
    if approved_seq_id != str(stop_sequence_candidate_id):
        raise RuntimeError("Geometry build is only allowed for the approved canonical stop sequence")

    stop_node_ids = _parse_uuid_array(row.get("stop_node_ids"))
    stop_prior_seqs = _parse_int_array(row.get("stop_prior_seqs"))
    metrics = row.get("metrics") or {}

    # Decide coordinates source:
    # - Prefer canonical if present
    # - Else fallback to raw prior seqs
    source = None
    if stop_node_ids:
        locs_latlon = _fetch_stop_points_from_canonical(conn, stop_node_ids)
        markers = [str(x) for x in stop_node_ids]
        source = "canonical_stop_node_ids"
    elif stop_prior_seqs:
        locs_latlon = _fetch_stop_points_from_prior(conn, route_id, stop_prior_seqs)
        markers = [f"seq:{x}" for x in stop_prior_seqs]
        source = "relation_stop_prior_seqs"
    else:
        raise ValueError("Candidate has neither stop_node_ids nor stop_prior_seqs")

    markers, locs_latlon, truncated_by_marker_repeat = _truncate_at_first_repeat_markers(markers, locs_latlon)
    markers, locs_latlon, truncated_by_coord_loop = _truncate_at_first_coordinate_return(markers, locs_latlon)

    if len(locs_latlon) < 2:
        raise ValueError("Need at least 2 stops to build geometry")

    set_id = create_geometry_set(
        conn,
        route_id,
        notes=f"valhalla variants for seq {stop_sequence_candidate_id} (source={source}, mode={metrics.get('mode')})",
        stop_sequence_set_id=(
            uuid.UUID(str(row.get("set_id")))
            if row.get("set_id") is not None
            else None
        ),
    )

    variants = [
        {VALHALLA_COSTING: {"use_highways": 0.20, "use_tolls": 0.0}},
        {VALHALLA_COSTING: {"use_highways": 0.05, "use_tolls": 0.0}},
        {VALHALLA_COSTING: {"use_highways": 0.35, "use_tolls": 0.0}},
    ]

    inserted_n = 0
    valhalla_inserted_n = 0
    for idx, costing_opts in enumerate(variants, start=1):
        try:

            shape_pts, _valhalla_meta = valhalla_route_with_meta(
                locs_latlon, costing_options=costing_opts
            )
            shape_pts = _pin_shape_endpoints(locs_latlon, shape_pts)
            tight = _directional_tightness(locs_latlon, shape_pts)
            wkt = _to_linestring_wkt_from_valhalla_points(shape_pts)

            # Scoring:
            # - If canonical ids exist, score against those stops (best)
            # - If raw-first, score using only geometry heuristics OR extend scorer later
            if stop_node_ids:
                score, length_m, avg_d, max_d, m = score_geometry(conn, wkt, stop_node_ids)
            else:
                # Minimal fallback scoring when no canonical stop ids:
                # You can improve later (distance from prior points, self-intersections, etc.)
                score, length_m, avg_d, max_d, m = 0.0, 0.0, 0.0, 0.0, {"note": "no canonical scoring (raw-first)"}
            if not bool(tight.get("is_direction_tight")):
                # Keep candidate but penalize score so tight candidates rank first.
                score = float(score) - 0.35

            insert_geometry_candidate(
                conn,
                set_id=set_id,
                stop_sequence_candidate_id=stop_sequence_candidate_id,  # keep link to candidate
                engine="valhalla_route",
                params={
                    "costing": VALHALLA_COSTING,
                    "costing_options": costing_opts,
                    "variant_rank": idx,
                    "coords_source": source,
                    "direction_from": markers[0],
                    "direction_to": markers[-1],
                    "direction_label": f"{markers[0]} -> {markers[-1]}",
                    "truncated_by_marker_repeat": truncated_by_marker_repeat,
                    "truncated_by_coord_loop": truncated_by_coord_loop,
                },
                linestring_wkt=wkt,
                score=score,
                length_m=length_m,
                avg_stop_dist_m=avg_d,
                max_stop_dist_m=max_d,
                metrics={
                    **(m or {}),
                    "direction_tightness": tight,
                },
                valhalla_request=_valhalla_meta,
                valhalla_response_hash=(_valhalla_meta or {}).get("response_hash"),
            )
            inserted_n += 1
            valhalla_inserted_n += 1

        except Exception as e:
            print(f"[geometry] variant {idx} failed: {e}")

    # Optional fallback for debug/manual mode only.
    # Default behavior is strict: Step 30 must produce at least one real Valhalla candidate.
    allow_fallback = str(os.getenv("P3_ALLOW_GEOM_FALLBACK", "0")).strip() in {"1", "true", "TRUE", "yes", "YES"}
    if valhalla_inserted_n == 0 and allow_fallback:
        try:
            fallback_wkt = _to_linestring_wkt_from_valhalla_points(locs_latlon)
            if stop_node_ids:
                score, length_m, avg_d, max_d, m = score_geometry(conn, fallback_wkt, stop_node_ids)
            else:
                score, length_m, avg_d, max_d, m = 0.0, 0.0, 0.0, 0.0, {"note": "raw fallback polyline"}

            insert_geometry_candidate(
                conn,
                set_id=set_id,
                stop_sequence_candidate_id=stop_sequence_candidate_id,
                engine="fallback_stop_polyline",
                params={
                    "costing": "fallback",
                    "variant_rank": 999,
                    "coords_source": source,
                    "direction_from": markers[0],
                    "direction_to": markers[-1],
                    "direction_label": f"{markers[0]} -> {markers[-1]}",
                    "fallback_reason": "all_valhalla_variants_failed",
                    "truncated_by_marker_repeat": truncated_by_marker_repeat,
                    "truncated_by_coord_loop": truncated_by_coord_loop,
                },
                linestring_wkt=fallback_wkt,
                score=float(score) - 0.5,  # keep below proper valhalla candidates
                length_m=length_m,
                avg_stop_dist_m=avg_d,
                max_stop_dist_m=max_d,
                metrics={
                    **(m or {}),
                    "direction_tightness": {"is_direction_tight": False, "fallback": True},
                },
            )
            inserted_n += 1
            print("[geometry] inserted fallback_stop_polyline candidate")
        except Exception as e:
            print(f"[geometry] fallback candidate failed: {e}")

    if valhalla_inserted_n == 0:
        raise RuntimeError(
            "Valhalla not turned on (or unavailable): Step 30 produced zero Valhalla geometry candidates. "
            "All Valhalla variants failed."
        )

    return set_id
