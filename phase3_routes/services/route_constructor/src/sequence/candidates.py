from __future__ import annotations

import math
import uuid
from statistics import median
from typing import Dict, List, Optional, Tuple

from phase3_routes.services.route_constructor.src.db.geo_prod_repo import (
    stop_nodes_within_radius,
)
from phase3_routes.services.route_constructor.src.db.route_work_repo import (
    create_stop_sequence_set,
    insert_stop_sequence_candidate,
    replace_stop_prior,
)
from phase3_routes.services.route_constructor.src.geometry.valhalla_client import (
    valhalla_route,
)
from phase3_routes.services.route_constructor.src.settings import (
    MAX_STOP_MATCH_RADIUS_M_RELAXED,
    MAX_STOP_MATCH_RADIUS_M_STRICT,
    STEP20_VALHALLA_BLEND_WEIGHT,
    STEP20_VALHALLA_MAX_CANDIDATES,
    STEP20_VALHALLA_MIN_STRUCTURAL_SCORE,
    STEP20_VALHALLA_TIMEOUT_S,
)

Hit = Optional[Tuple[uuid.UUID, float]]
LonLat = Tuple[float, float]


def _haversine_m(a: LonLat, b: LonLat) -> float:
    lon1, lat1 = float(a[0]), float(a[1])
    lon2, lat2 = float(b[0]), float(b[1])
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.asin(math.sqrt(max(0.0, min(1.0, h))))


def _angle_deg(a: LonLat, b: LonLat, c: LonLat) -> float:
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1 = math.hypot(v1[0], v1[1])
    n2 = math.hypot(v2[0], v2[1])
    if n1 <= 0.0 or n2 <= 0.0:
        return 0.0
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    cosang = max(-1.0, min(1.0, dot / (n1 * n2)))
    return float(math.degrees(math.acos(cosang)))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _candidate_signature(rows: List[Dict]) -> str:
    seqs = [str(int(r.get("seq") or 0)) for r in rows]
    return ",".join(seqs)


def _candidate_marker(row: Dict) -> str:
    sid = str(row.get("matched_stop_node_id") or "").strip()
    if sid:
        return sid
    return f"seq:{int(row.get('seq') or 0)}"


def _candidate_orientation(rows: List[Dict]) -> str:
    seqs = [int(r.get("seq") or 0) for r in rows]
    if len(seqs) <= 1:
        return "forward"
    forward_steps = 0
    reverse_steps = 0
    for idx in range(1, len(seqs)):
        delta = seqs[idx] - seqs[idx - 1]
        if delta > 0:
            forward_steps += 1
        elif delta < 0:
            reverse_steps += 1
    if reverse_steps > forward_steps:
        return "inverse"
    if reverse_steps == forward_steps and seqs[-1] < seqs[0]:
        return "inverse"
    return "forward"


def _variant_boundary_window(n_rows: int) -> int:
    if n_rows <= 2:
        return max(1, n_rows)
    return max(2, min(4, int(math.ceil(float(n_rows) * 0.18))))


def _variant_boundary_signature(rows: List[Dict], *, from_end: bool) -> Tuple[str, List[str]]:
    if not rows:
        return "", []
    window = _variant_boundary_window(len(rows))
    subset = list(rows[-window:] if from_end else rows[:window])
    markers = [_candidate_marker(row) for row in subset]
    normalized = sorted(markers)
    return "|".join(normalized), markers


def _short_marker(marker: str) -> str:
    text = str(marker or "").strip()
    if not text:
        return "-"
    if text.startswith("seq:"):
        return text
    return text[:8]


def _variant_group_metadata(rows: List[Dict]) -> Dict[str, object]:
    orientation = _candidate_orientation(rows)
    head_signature, head_markers = _variant_boundary_signature(rows, from_end=False)
    tail_signature, tail_markers = _variant_boundary_signature(rows, from_end=True)
    marker_source = "matched_stop_node_id" if any(str(r.get("matched_stop_node_id") or "").strip() for r in rows) else "prior_seq"
    group_key = f"{orientation}:{head_signature}->{tail_signature}"
    head_label = ",".join(_short_marker(marker) for marker in head_markers[:2]) or "-"
    tail_label = ",".join(_short_marker(marker) for marker in tail_markers[:2]) or "-"
    return {
        "sequence_orientation": orientation,
        "inverse_orientation_candidate": bool(orientation == "inverse"),
        "variant_group_key": group_key,
        "variant_group_label": f"{orientation} | head {head_label} | tail {tail_label}",
        "variant_head_signature": head_signature,
        "variant_tail_signature": tail_signature,
        "variant_boundary_window": _variant_boundary_window(len(rows)),
        "variant_marker_source": marker_source,
    }


def _coords(rows: List[Dict]) -> List[LonLat]:
    out: List[LonLat] = []
    for row in rows:
        out.append((float(row["lon"]), float(row["lat"])))
    return out


def _nearest_vertex_index(point: LonLat, shape: List[LonLat]) -> int:
    best_i = 0
    best_d = float("inf")
    for idx, shp in enumerate(shape):
        d = _haversine_m(point, shp)
        if d < best_d:
            best_i = idx
            best_d = d
    return best_i


def _path_length_between_vertices(shape: List[LonLat], start_idx: int, end_idx: int) -> float:
    if end_idx <= start_idx:
        return 0.0
    total = 0.0
    for idx in range(start_idx + 1, end_idx + 1):
        total += _haversine_m(shape[idx - 1], shape[idx])
    return total


def _pin_shape_endpoints(requested_coords: List[LonLat], shape_pts: List[LonLat]) -> List[LonLat]:
    if len(shape_pts) < 2 or len(requested_coords) < 2:
        return list(shape_pts)
    pinned = list(shape_pts)
    pinned[0] = requested_coords[0]
    pinned[-1] = requested_coords[-1]
    return pinned


def _match_once(conn, prior: List[Dict], radius_m: float) -> List[Dict]:
    out = []
    for r in prior:
        rr = dict(r)
        if rr.get("matched_stop_node_id"):
            rr["match_state"] = "matched"
            rr["candidate_count"] = int(rr.get("candidate_count") or 1)
            if rr.get("match_dist_m") is None:
                rr["match_dist_m"] = 0.0
            out.append(rr)
            continue
        hits = stop_nodes_within_radius(conn, rr["lat"], rr["lon"], radius_m, limit=6)
        rr["candidate_count"] = len(hits)
        if len(hits) == 0:
            rr["matched_stop_node_id"] = None
            rr["match_dist_m"] = None
            rr["match_state"] = "unmatched"
        elif len(hits) == 1:
            rr["matched_stop_node_id"] = hits[0]["node_id"]
            rr["match_dist_m"] = hits[0]["dist_m"]
            rr["match_state"] = "matched"
        else:
            # Multiple candidates: accept nearest when it's clearly closest
            # (gap >= 5m to next, or nearest within a tight 25m halo).
            # This handles legitimate canonical duplicates created by
            # multiple extraction templates at the same physical stop.
            nearest = hits[0]
            runner_up = hits[1]
            gap = float(runner_up["dist_m"]) - float(nearest["dist_m"])
            if float(nearest["dist_m"]) <= 25.0 or gap >= 5.0:
                rr["matched_stop_node_id"] = nearest["node_id"]
                rr["match_dist_m"] = nearest["dist_m"]
                rr["match_state"] = "matched"
            else:
                rr["matched_stop_node_id"] = None
                rr["match_dist_m"] = None
                rr["match_state"] = "ambiguous"
        out.append(rr)
    return out


def _best_available_rows(conn, ordered_prior: List[Dict]) -> Tuple[List[Dict], bool]:
    fully_pre_matched = bool(ordered_prior) and all(r.get("matched_stop_node_id") for r in ordered_prior)
    if fully_pre_matched:
        strict_rows = []
        for r in ordered_prior:
            rr = dict(r)
            rr["match_state"] = "matched"
            rr["candidate_count"] = 1
            if rr.get("match_dist_m") is None:
                rr["match_dist_m"] = 0.0
            strict_rows.append(rr)
        return strict_rows, True

    strict_rows = _match_once(conn, ordered_prior, MAX_STOP_MATCH_RADIUS_M_STRICT)
    relaxed_rows = _match_once(conn, ordered_prior, MAX_STOP_MATCH_RADIUS_M_RELAXED)
    best_rows = []
    for s, r in zip(strict_rows, relaxed_rows):
        rr = dict(r)
        if s.get("matched_stop_node_id") is not None:
            rr["matched_stop_node_id"] = s["matched_stop_node_id"]
            rr["match_dist_m"] = s["match_dist_m"]
            rr["match_state"] = "matched"
        best_rows.append(rr)
    return best_rows, False


def _farthest_pair_indices(coords: List[LonLat]) -> Tuple[int, int]:
    if len(coords) < 2:
        return 0, 0
    best = (0.0, 0, 1)
    for i in range(len(coords)):
        for j in range(i + 1, len(coords)):
            d = _haversine_m(coords[i], coords[j])
            if d > best[0]:
                best = (d, i, j)
    return best[1], best[2]


def _sort_by_axis(rows: List[Dict], *, reverse: bool = False) -> List[Dict]:
    coords = _coords(rows)
    if len(coords) < 2:
        return [dict(r) for r in rows]
    i0, i1 = _farthest_pair_indices(coords)
    start_idx, end_idx = i0, i1
    origin = coords[start_idx]
    target = coords[end_idx]
    if _haversine_m(coords[0], coords[end_idx]) < _haversine_m(coords[0], coords[start_idx]):
        origin = coords[end_idx]
        target = coords[start_idx]
    axis = (target[0] - origin[0], target[1] - origin[1])
    axis_norm_sq = (axis[0] * axis[0]) + (axis[1] * axis[1]) or 1.0

    def _projection(row: Dict) -> float:
        px = float(row["lon"]) - origin[0]
        py = float(row["lat"]) - origin[1]
        return ((px * axis[0]) + (py * axis[1])) / axis_norm_sq

    ranked = sorted(
        (dict(r) for r in rows),
        key=lambda row: (_projection(row), int(row.get("seq") or 0)),
        reverse=bool(reverse),
    )
    return ranked


def _nearest_neighbor(rows: List[Dict], *, start_from_end: bool = False) -> List[Dict]:
    if len(rows) <= 2:
        return [dict(r) for r in (reversed(rows) if start_from_end else rows)]
    unused = [dict(r) for r in rows]
    current = unused.pop(-1 if start_from_end else 0)
    ordered = [current]
    while unused:
        curr_xy = (float(current["lon"]), float(current["lat"]))
        idx = min(
            range(len(unused)),
            key=lambda i: (
                _haversine_m(curr_xy, (float(unused[i]["lon"]), float(unused[i]["lat"]))),
                int(unused[i].get("seq") or 0),
            ),
        )
        current = unused.pop(idx)
        ordered.append(current)
    return ordered


def _local_backtrack_smooth(rows: List[Dict]) -> List[Dict]:
    out = [dict(r) for r in rows]
    if len(out) < 4:
        return out
    changed = True
    passes = 0
    while changed and passes < 3:
        changed = False
        passes += 1
        for i in range(1, len(out) - 1):
            a = (float(out[i - 1]["lon"]), float(out[i - 1]["lat"]))
            b = (float(out[i]["lon"]), float(out[i]["lat"]))
            c = (float(out[i + 1]["lon"]), float(out[i + 1]["lat"]))
            current_cost = _haversine_m(a, b) + _haversine_m(b, c)
            swapped_cost = _haversine_m(a, c) + _haversine_m(c, b)
            if _angle_deg(a, b, c) >= 150.0 and swapped_cost + 30.0 < current_cost:
                out[i], out[i + 1] = out[i + 1], out[i]
                changed = True
    return out


def _candidate_metrics(rows: List[Dict], original_rows: List[Dict], family: str, label: str) -> Dict:
    coords = _coords(rows)
    original_coords = _coords(original_rows)
    segs = [_haversine_m(coords[i - 1], coords[i]) for i in range(1, len(coords))]
    median_seg = float(median(segs)) if segs else 0.0
    large_jump_m = max(450.0, median_seg * 2.5)
    very_large_jump_m = max(900.0, median_seg * 4.0)
    long_jump_count = sum(1 for d in segs if d >= large_jump_m)
    very_long_jump_count = sum(1 for d in segs if d >= very_large_jump_m)

    backtracks = 0
    min_backtrack_segment = max(45.0, median_seg * 0.35)
    for i in range(1, len(coords) - 1):
        d1 = _haversine_m(coords[i - 1], coords[i])
        d2 = _haversine_m(coords[i], coords[i + 1])
        if d1 < min_backtrack_segment or d2 < min_backtrack_segment:
            continue
        if _angle_deg(coords[i - 1], coords[i], coords[i + 1]) >= 145.0:
            backtracks += 1

    seen: set[str] = set()
    duplicate_stop_count = 0
    for row in rows:
        sid = str(row.get("matched_stop_node_id") or "").strip()
        if not sid:
            continue
        if sid in seen:
            duplicate_stop_count += 1
        else:
            seen.add(sid)

    direct_origin = original_coords[0] if original_coords else coords[0]
    direct_target = original_coords[-1] if original_coords else coords[-1]
    baseline_terminal_span = max(_haversine_m(direct_origin, direct_target), 150.0)
    forward_shift = _haversine_m(coords[0], direct_origin) + _haversine_m(coords[-1], direct_target)
    reverse_shift = _haversine_m(coords[0], direct_target) + _haversine_m(coords[-1], direct_origin)
    direction_consistency = max(0.0, 1.0 - (forward_shift / (baseline_terminal_span * 2.0)))
    terminal_consistency = max(0.0, 1.0 - (min(forward_shift, reverse_shift) / (baseline_terminal_span * 2.0)))

    candidate_span = max(_haversine_m(coords[0], coords[-1]), 1.0)
    axis = (coords[-1][0] - coords[0][0], coords[-1][1] - coords[0][1])
    axis_norm_sq = (axis[0] * axis[0]) + (axis[1] * axis[1]) or 1.0
    projections: List[float] = []
    origin = coords[0]
    for lon, lat in coords:
        px = lon - origin[0]
        py = lat - origin[1]
        projections.append(((px * axis[0]) + (py * axis[1])) / axis_norm_sq)
    non_negative_projection_steps = 0
    for i in range(1, len(projections)):
        if projections[i] >= (projections[i - 1] - 0.02):
            non_negative_projection_steps += 1
    monotonic_spatial_progression = (
        float(non_negative_projection_steps) / float(max(len(projections) - 1, 1))
    )

    duplicate_penalty = float(duplicate_stop_count) * 15.0
    long_jump_penalty = (float(long_jump_count) * 10.0) + (float(very_long_jump_count) * 12.0)
    backtrack_penalty = float(backtracks) * 10.0
    endpoint_penalty = max(0.0, 1.0 - terminal_consistency) * 10.0
    direction_penalty = max(0.0, 1.0 - direction_consistency) * 8.0
    score = (
        100.0
        - duplicate_penalty
        - long_jump_penalty
        - backtrack_penalty
        - endpoint_penalty
        - direction_penalty
        + (monotonic_spatial_progression * 6.0)
        + (terminal_consistency * 6.0)
        + (direction_consistency * 4.0)
    )
    score = _clamp(score, 0.0, 100.0)

    risk_indicators: List[str] = []
    if duplicate_stop_count > 0:
        risk_indicators.append("duplicate_stop_pressure")
    if long_jump_count > 0:
        risk_indicators.append("long_jump_pressure")
    if very_long_jump_count > 0:
        risk_indicators.append("very_long_jump_pressure")
    if backtracks > 0:
        risk_indicators.append("backtrack_pressure")
    if terminal_consistency < 0.55:
        risk_indicators.append("terminal_inconsistency")
    if monotonic_spatial_progression < 0.70:
        risk_indicators.append("spatial_progression_noise")

    dists = [float(r.get("match_dist_m") or 0.0) for r in rows if r.get("match_dist_m") is not None]
    matched_stops = sum(1 for r in rows if r.get("matched_stop_node_id"))
    confidence_label = "high" if score >= 86.0 else ("medium" if score >= 70.0 else "low")
    return {
        "family": family,
        "label": label,
        "mode": family,
        "candidate_generation_version": "sequence_resolution_v3_valhalla",
        "matched_stops": matched_stops,
        "total_prior_stops": len(rows),
        "avg_match_dist_m": (sum(dists) / len(dists)) if dists else 0.0,
        "max_match_dist_m": max(dists) if dists else 0.0,
        "structural_sequence_score": round(score, 3),
        "terminal_consistency": round(terminal_consistency, 4),
        "direction_consistency": round(direction_consistency, 4),
        "monotonic_spatial_progression": round(monotonic_spatial_progression, 4),
        "duplicate_stop_count": int(duplicate_stop_count),
        "duplicate_stop_penalty": round(duplicate_penalty, 3),
        "long_jump_count": int(long_jump_count),
        "very_long_jump_count": int(very_long_jump_count),
        "long_jump_penalty": round(long_jump_penalty, 3),
        "backtrack_count": int(backtracks),
        "backtrack_penalty": round(backtrack_penalty, 3),
        "terminal_span_m": round(candidate_span, 3),
        "sequence_score": round(score, 3),
        "confidence_label": confidence_label,
        "sequence_risk_indicators": risk_indicators,
        "evidence_summary": [
            f"family={family}",
            f"structural_sequence_score={score:.2f}",
            f"matched_stops={matched_stops}/{len(rows)}",
        ],
    }


def _select_valhalla_candidates(ranked: List[Dict]) -> List[int]:
    budget = max(0, int(STEP20_VALHALLA_MAX_CANDIDATES))
    if budget <= 0 or not ranked:
        return []
    threshold = float(STEP20_VALHALLA_MIN_STRUCTURAL_SCORE)
    dominant_orientation = str(
        dict((ranked[0].get("metrics") or {})).get("sequence_orientation") or "forward"
    ).strip().lower() or "forward"
    selected: List[int] = []
    seen_groups: set[str] = set()

    def _eligible(record: Dict, idx: int) -> bool:
        metrics = dict(record.get("metrics") or {})
        score = float(metrics.get("structural_sequence_score") or metrics.get("sequence_score") or 0.0)
        return idx == 0 or score >= threshold

    for idx, record in enumerate(ranked):
        if len(selected) >= budget or not _eligible(record, idx):
            continue
        metrics = dict(record.get("metrics") or {})
        orientation = str(metrics.get("sequence_orientation") or "forward").strip().lower() or "forward"
        if orientation != dominant_orientation:
            continue
        group_key = str(metrics.get("variant_group_key") or "").strip() or f"{orientation}:{idx}"
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)
        selected.append(idx)

    for idx, record in enumerate(ranked):
        if len(selected) >= budget or idx in selected or not _eligible(record, idx):
            continue
        selected.append(idx)
    return selected


def _build_valhalla_evidence(rows: List[Dict]) -> Dict[str, object]:
    coords = _coords(rows)
    segment_count = max(0, len(coords) - 1)
    base = {
        "valhalla_evidence_status": "unavailable",
        "valhalla_traversability_score": None,
        "valhalla_segment_count": int(segment_count),
        "valhalla_successful_segment_count": 0,
        "valhalla_failed_segment_count": int(segment_count),
        "valhalla_segment_success_rate": None,
        "valhalla_extreme_detour_count": 0,
        "valhalla_backtrack_segment_count": 0,
        "valhalla_backtrack_penalty": 0.0,
        "valhalla_detour_penalty": 0.0,
        "valhalla_failed_segment_penalty": 0.0,
        "valhalla_route_continuity_quality": None,
        "valhalla_path_length_m": None,
        "valhalla_geodesic_length_m": None,
        "valhalla_path_vs_geodesic_ratio": None,
        "valhalla_avg_stop_anchor_dist_m": None,
        "valhalla_max_stop_anchor_dist_m": None,
        "network_risk_indicators": ["valhalla_evidence_unavailable"] if segment_count > 0 else [],
        "valhalla_evidence_summary": [
            "Valhalla evidence unavailable; structural sequence score retained."
        ],
    }
    if len(coords) < 2:
        base["valhalla_evidence_status"] = "skipped_short_sequence"
        base["network_risk_indicators"] = []
        base["valhalla_evidence_summary"] = ["Candidate has fewer than 2 stops; Valhalla evidence skipped."]
        return base

    try:
        shape_pts = _pin_shape_endpoints(
            coords,
            list(valhalla_route(coords, costing_options=None, timeout_s=STEP20_VALHALLA_TIMEOUT_S)),
        )
    except Exception as exc:
        base["valhalla_error"] = str(exc)[:240]
        return base

    nearest_idx = [_nearest_vertex_index(point, shape_pts) for point in coords]
    anchor_dists = [_haversine_m(point, shape_pts[idx]) for point, idx in zip(coords, nearest_idx)]
    successful_segments = 0
    failed_segments = 0
    backtrack_segments = 0
    extreme_detours = 0
    detour_penalty = 0.0
    total_path_length = 0.0
    total_geodesic_length = 0.0

    for idx in range(1, len(coords)):
        geodesic = _haversine_m(coords[idx - 1], coords[idx])
        total_geodesic_length += geodesic
        start_idx = nearest_idx[idx - 1]
        end_idx = nearest_idx[idx]
        if end_idx <= start_idx:
            failed_segments += 1
            backtrack_segments += 1
            continue
        path_length = _path_length_between_vertices(shape_pts, start_idx, end_idx)
        if path_length <= 0.0:
            failed_segments += 1
            continue
        successful_segments += 1
        total_path_length += path_length
        ratio_floor = max(geodesic, 35.0)
        ratio = path_length / ratio_floor
        if ratio >= (5.0 if geodesic < 60.0 else 3.2):
            extreme_detours += 1
        if ratio > 1.35:
            detour_penalty += min(18.0, (ratio - 1.35) * 12.0)

    segment_success_rate = (
        float(successful_segments) / float(segment_count) if segment_count > 0 else 1.0
    )
    route_continuity_quality = (
        float(successful_segments - backtrack_segments) / float(max(segment_count, 1))
    )
    path_vs_geodesic_ratio = (
        float(total_path_length) / float(max(total_geodesic_length, 1.0))
        if total_geodesic_length > 0.0
        else None
    )
    backtrack_penalty = float(backtrack_segments) * 14.0
    failed_segment_penalty = float(failed_segments) * 22.0
    anchor_penalty = min(
        12.0,
        (float(sum(anchor_dists)) / float(max(len(anchor_dists), 1)) / 10.0)
        + (float(max(anchor_dists) if anchor_dists else 0.0) / 28.0),
    )
    traversability_score = _clamp(
        100.0 - detour_penalty - backtrack_penalty - failed_segment_penalty - anchor_penalty,
        0.0,
        100.0,
    )
    if segment_success_rate >= 0.95 and (path_vs_geodesic_ratio is None or path_vs_geodesic_ratio <= 2.1):
        traversability_score = _clamp(traversability_score + 4.0, 0.0, 100.0)

    network_risks: List[str] = []
    if failed_segments > 0:
        network_risks.append("failed_segment_pressure")
    if backtrack_segments > 0:
        network_risks.append("network_backtrack_pressure")
    if extreme_detours > 0:
        network_risks.append("extreme_detour_pressure")
    if path_vs_geodesic_ratio is not None and path_vs_geodesic_ratio >= 2.6:
        network_risks.append("network_ratio_heavy")
    if anchor_dists and (sum(anchor_dists) / float(len(anchor_dists))) >= 35.0:
        network_risks.append("anchor_snap_noise")

    evidence_summary = [
        f"valhalla_traversability_score={traversability_score:.2f}",
        f"segment_success_rate={segment_success_rate:.2f}",
        f"failed_segments={failed_segments}/{segment_count}",
    ]
    if path_vs_geodesic_ratio is not None:
        evidence_summary.append(f"path_vs_geodesic_ratio={path_vs_geodesic_ratio:.2f}")

    return {
        "valhalla_evidence_status": "available",
        "valhalla_traversability_score": round(traversability_score, 3),
        "valhalla_segment_count": int(segment_count),
        "valhalla_successful_segment_count": int(successful_segments),
        "valhalla_failed_segment_count": int(failed_segments),
        "valhalla_segment_success_rate": round(segment_success_rate, 4),
        "valhalla_extreme_detour_count": int(extreme_detours),
        "valhalla_backtrack_segment_count": int(backtrack_segments),
        "valhalla_backtrack_penalty": round(backtrack_penalty, 3),
        "valhalla_detour_penalty": round(detour_penalty, 3),
        "valhalla_failed_segment_penalty": round(failed_segment_penalty, 3),
        "valhalla_route_continuity_quality": round(_clamp(route_continuity_quality, 0.0, 1.0), 4),
        "valhalla_path_length_m": round(total_path_length, 3),
        "valhalla_geodesic_length_m": round(total_geodesic_length, 3),
        "valhalla_path_vs_geodesic_ratio": (
            round(path_vs_geodesic_ratio, 4) if path_vs_geodesic_ratio is not None else None
        ),
        "valhalla_avg_stop_anchor_dist_m": (
            round(float(sum(anchor_dists)) / float(max(len(anchor_dists), 1)), 3) if anchor_dists else None
        ),
        "valhalla_max_stop_anchor_dist_m": round(float(max(anchor_dists) if anchor_dists else 0.0), 3),
        "network_risk_indicators": network_risks,
        "valhalla_evidence_summary": evidence_summary,
    }


def _enrich_ranked_candidates_with_valhalla(ranked: List[Dict]) -> List[Dict]:
    selected_indices = set(_select_valhalla_candidates(ranked))
    blend_weight = _clamp(STEP20_VALHALLA_BLEND_WEIGHT, 0.0, 0.45)
    for idx, record in enumerate(ranked):
        metrics = dict(record.get("metrics") or {})
        structural_score = float(metrics.get("structural_sequence_score") or metrics.get("sequence_score") or 0.0)
        metrics["structural_sequence_score"] = round(structural_score, 3)
        if idx in selected_indices:
            valhalla_metrics = _build_valhalla_evidence(list(record.get("rows") or []))
            metrics.update(valhalla_metrics)
            valhalla_score = valhalla_metrics.get("valhalla_traversability_score")
            if valhalla_score is not None:
                combined_score = (
                    (structural_score * (1.0 - blend_weight))
                    + (float(valhalla_score) * blend_weight)
                )
                metrics["valhalla_score_blend_weight"] = round(blend_weight, 3)
            else:
                combined_score = structural_score
                metrics["valhalla_score_blend_weight"] = 0.0
        else:
            metrics.update(
                {
                    "valhalla_evidence_status": "skipped_budget",
                    "valhalla_traversability_score": None,
                    "valhalla_segment_count": max(0, len(list(record.get("rows") or [])) - 1),
                    "valhalla_successful_segment_count": None,
                    "valhalla_failed_segment_count": None,
                    "valhalla_segment_success_rate": None,
                    "valhalla_extreme_detour_count": None,
                    "valhalla_backtrack_segment_count": None,
                    "valhalla_backtrack_penalty": None,
                    "valhalla_detour_penalty": None,
                    "valhalla_failed_segment_penalty": None,
                    "valhalla_route_continuity_quality": None,
                    "valhalla_path_length_m": None,
                    "valhalla_geodesic_length_m": None,
                    "valhalla_path_vs_geodesic_ratio": None,
                    "valhalla_avg_stop_anchor_dist_m": None,
                    "valhalla_max_stop_anchor_dist_m": None,
                    "network_risk_indicators": [],
                    "valhalla_evidence_summary": [
                        "Valhalla evidence skipped for this candidate due to Step 20 budget limits."
                    ],
                    "valhalla_score_blend_weight": 0.0,
                }
            )
            combined_score = structural_score

        combined_risks = list(
            dict.fromkeys(
                [
                    str(x)
                    for x in (
                        list(metrics.get("sequence_risk_indicators") or [])
                        + list(metrics.get("network_risk_indicators") or [])
                    )
                    if str(x).strip()
                ]
            )
        )
        metrics["sequence_risk_indicators"] = combined_risks
        metrics["combined_sequence_score"] = round(combined_score, 3)
        metrics["sequence_score"] = round(combined_score, 3)
        evidence_summary = list(metrics.get("evidence_summary") or [])
        evidence_summary.extend(list(metrics.get("valhalla_evidence_summary") or [])[:3])
        metrics["evidence_summary"] = list(dict.fromkeys(str(x) for x in evidence_summary if str(x).strip()))
        record["metrics"] = metrics
    return ranked


def _ordered_stop_node_ids(rows: List[Dict]) -> List[uuid.UUID]:
    out: List[uuid.UUID] = []
    for row in rows:
        raw = row.get("matched_stop_node_id")
        if raw is None:
            return []
        out.append(uuid.UUID(str(raw)))
    return out


def build_sequence_candidates(conn, route_id: uuid.UUID, prior_rows: List[Dict]) -> uuid.UUID:
    ordered_prior = sorted((dict(r) for r in (prior_rows or [])), key=lambda r: int(r.get("seq") or 0))
    if not ordered_prior:
        raise ValueError("No prior rows available for sequence candidate generation")

    working_rows, fully_pre_matched = _best_available_rows(conn, ordered_prior)
    replace_stop_prior(conn, route_id, working_rows)

    family_specs = [
        ("current_relation_order", "Current relation order", [dict(r) for r in working_rows]),
        ("cleaned_relation_order", "Local backtrack smoothing", _local_backtrack_smooth(working_rows)),
        ("terminal_forward_order", "Terminal-forward axis order", _sort_by_axis(working_rows, reverse=False)),
        ("terminal_inverse_order", "Terminal-inverse axis order", _sort_by_axis(working_rows, reverse=True)),
        ("spatial_smooth_forward", "Spatially smoothed forward progression", _nearest_neighbor(working_rows, start_from_end=False)),
        ("spatial_smooth_inverse", "Spatially smoothed inverse progression", _nearest_neighbor(working_rows, start_from_end=True)),
        ("reverse_relation_order", "Reverse relation order", [dict(r) for r in reversed(working_rows)]),
    ]
    if fully_pre_matched:
        family_specs.append(
            ("manual_prior_informed_order", "Manual prior informed order", [dict(r) for r in working_rows])
        )

    deduped: Dict[str, Dict] = {}
    for family, label, family_rows in family_specs:
        rows = [dict(r) for r in family_rows]
        metrics = _candidate_metrics(rows, working_rows, family, label)
        metrics.update(_variant_group_metadata(rows))
        signature = _candidate_signature(rows)
        record = {
            "rows": rows,
            "signature": signature,
            "metrics": metrics,
        }
        existing = deduped.get(signature)
        if existing is None:
            metrics["source_families"] = [family]
            deduped[signature] = record
            continue
        existing_score = float((existing.get("metrics") or {}).get("sequence_score") or 0.0)
        new_score = float(metrics.get("sequence_score") or 0.0)
        source_families = list((existing.get("metrics") or {}).get("source_families") or [])
        if family not in source_families:
            source_families.append(family)
        if new_score > existing_score:
            metrics["source_families"] = source_families
            deduped[signature] = record
        else:
            (existing.get("metrics") or {})["source_families"] = source_families

    structural_ranked = sorted(
        deduped.values(),
        key=lambda row: (
            float((row.get("metrics") or {}).get("sequence_score") or 0.0),
            float((row.get("metrics") or {}).get("terminal_consistency") or 0.0),
            float((row.get("metrics") or {}).get("direction_consistency") or 0.0),
        ),
        reverse=True,
    )
    ranked = sorted(
        _enrich_ranked_candidates_with_valhalla(list(structural_ranked)),
        key=lambda row: (
            float((row.get("metrics") or {}).get("sequence_score") or 0.0),
            float((row.get("metrics") or {}).get("valhalla_traversability_score") or -1.0),
            float((row.get("metrics") or {}).get("structural_sequence_score") or 0.0),
            float((row.get("metrics") or {}).get("terminal_consistency") or 0.0),
            float((row.get("metrics") or {}).get("direction_consistency") or 0.0),
        ),
        reverse=True,
    )

    notes = (
        "manual(pre-matched) sequence resolution from relation_stop_prior"
        if fully_pre_matched
        else "auto(sequence_resolution_v1) from relation_stop_prior"
    )
    set_id = create_stop_sequence_set(conn, route_id, notes=notes)

    for rank_idx, record in enumerate(ranked, start=1):
        rows = list(record.get("rows") or [])
        metrics = dict(record.get("metrics") or {})
        stop_prior_seqs = [int(r["seq"]) for r in rows]
        stop_node_ids = _ordered_stop_node_ids(rows)
        metrics["canonical_stop_node_ids_complete"] = bool(stop_node_ids)
        insert_stop_sequence_candidate(
            conn,
            set_id=set_id,
            rank=rank_idx,
            stop_node_ids=stop_node_ids,
            stop_prior_seqs=stop_prior_seqs,
            metrics=metrics,
        )

    return set_id
