"""
Stage C — Valhalla Corridor Construction

Given grounded stop waypoints, builds a Valhalla road-level corridor
LineString. Handles failures gracefully with segmented fallback.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    CorridorConstraint,
    CorridorResult,
    HintCandidateSet,
    StopGroundingResult,
    StopMatch,
    TypedRouteSeed,
)
from datamind_console.phases.phase3_routes.stop_grounding.arterial_waypoints import (
    resolve_constraint_waypoints,
)
from datamind_console.phases.phase3_routes.stop_grounding.geography_guardrails import (
    evaluate_corridor_geography,
)

_LOG = logging.getLogger(__name__)

LonLat = Tuple[float, float]


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2.0 * r * math.asin(math.sqrt(a))


def _linestring_length_km(coords: List[LonLat]) -> float:
    total = 0.0
    for i in range(len(coords) - 1):
        total += _haversine_m(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
    return total / 1000.0


def _coords_to_geojson(coords: List[LonLat]) -> Dict[str, Any]:
    return {
        "type": "LineString",
        "coordinates": [[lon, lat] for lon, lat in coords],
    }


def _dedupe_waypoints(waypoints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not waypoints:
        return []

    deduped: List[Dict[str, Any]] = []
    seen_stop_ids = set()
    for waypoint in waypoints:
        stop_id = str(waypoint.get("stop_id") or "").strip()
        lon = float(waypoint.get("lon") or 0.0)
        lat = float(waypoint.get("lat") or 0.0)
        label = str(waypoint.get("label") or "")
        is_anchor_b = label == "anchor_b"

        same_seen_stop = bool(stop_id and stop_id in seen_stop_ids)
        near_existing_idx = None
        for idx, existing in enumerate(deduped):
            dist_m = _haversine_m(
                float(existing.get("lon") or 0.0),
                float(existing.get("lat") or 0.0),
                lon,
                lat,
            )
            if dist_m <= 300.0:
                near_existing_idx = idx
                break

        if same_seen_stop and not is_anchor_b:
            continue
        if near_existing_idx is not None:
            existing = deduped[near_existing_idx]
            if is_anchor_b:
                deduped[near_existing_idx] = waypoint
                if stop_id:
                    seen_stop_ids.add(stop_id)
                continue
            existing_label = str(existing.get("label") or "")
            if existing_label == "anchor_a":
                continue
            if existing_label.startswith("intermediate:"):
                continue

        deduped.append(waypoint)
        if stop_id:
            seen_stop_ids.add(stop_id)

    return deduped


def _build_corridor_from_waypoints(
    waypoints: List[Dict[str, Any]],
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    timeout_s: int = 60,
) -> CorridorResult:
    lonlat_list: List[LonLat] = [(float(wp["lon"]), float(wp["lat"])) for wp in waypoints]

    try:
        shape, valhalla_meta = _call_valhalla(lonlat_list, timeout_s=timeout_s)
        geojson = _coords_to_geojson(shape)
        length_km = _linestring_length_km(shape)
        geography = evaluate_corridor_geography(
            corridor_geojson=geojson,
            waypoints_used=waypoints,
            expected_envelope=expected_envelope,
        )
        return CorridorResult(
            corridor_geojson=geojson,
            total_length_km=length_km,
            segment_count=1,
            failed_segments=[],
            waypoints_used=waypoints,
            corridor_confidence=max(0.0, min(1.0, float(geography.get("geography_plausibility_score") or 1.0))),
            corridor_notes=f"Full corridor: {length_km:.1f}km, {len(shape)} points",
            expected_geographic_envelope=expected_envelope,
            straight_line_km=float(geography.get("straight_line_km") or 0.0),
            corridor_inflation_ratio=float(geography.get("corridor_inflation_ratio") or 0.0),
            in_bounds_fraction=float(geography.get("in_bounds_fraction") or 0.0),
            out_of_bounds_reason=str(geography.get("out_of_bounds_reason") or ""),
            geography_plausibility_score=float(geography.get("geography_plausibility_score") or 0.0),
            rejected_for_geographic_implausibility=bool(
                geography.get("rejected_for_geographic_implausibility", False)
            ),
            route_locality_consistency_notes=list(
                geography.get("route_locality_consistency_notes") or []
            ),
            valhalla_meta=valhalla_meta,
        )
    except Exception as exc:
        _LOG.warning("Full Valhalla route failed: %s — trying segmented", exc)

    all_coords: List[LonLat] = []
    failed_segments: List[str] = []
    success_count = 0
    segment_metas: List[Dict[str, Any]] = []

    for i in range(len(lonlat_list) - 1):
        segment_label = f"{waypoints[i]['label']}→{waypoints[i + 1]['label']}"
        try:
            segment_shape, segment_meta = _call_valhalla(
                [lonlat_list[i], lonlat_list[i + 1]],
                timeout_s=timeout_s,
            )
            if all_coords and segment_shape and segment_shape[0] == all_coords[-1]:
                segment_shape = segment_shape[1:]
            all_coords.extend(segment_shape)
            success_count += 1
            segment_meta["segment_label"] = segment_label
            segment_metas.append(segment_meta)
        except Exception as seg_exc:
            _LOG.warning("Segment %s failed: %s", segment_label, seg_exc)
            failed_segments.append(segment_label)
            if all_coords and all_coords[-1] != lonlat_list[i]:
                all_coords.append(lonlat_list[i])
            all_coords.append(lonlat_list[i + 1])

    total_segments = len(lonlat_list) - 1
    if len(all_coords) < 2:
        return CorridorResult(
            failed_segments=failed_segments,
            waypoints_used=waypoints,
            corridor_notes="All segments failed",
        )

    geojson = _coords_to_geojson(all_coords)
    length_km = _linestring_length_km(all_coords)
    confidence = success_count / total_segments if total_segments > 0 else 0.0
    geography = evaluate_corridor_geography(
        corridor_geojson=geojson,
        waypoints_used=waypoints,
        expected_envelope=expected_envelope,
    )

    aggregate_meta: Dict[str, Any] = {
        "chunked": True,
        "n_locations": len(lonlat_list),
        "n_chunks": len(segment_metas),
        "chunks": segment_metas,
        "failed_segments": failed_segments,
        "success_count": success_count,
        "total_segments": total_segments,
        "capture_status": "segmented_aggregate",
        "source": "corridor_builder._build_corridor_from_waypoints.segmented",
    }

    return CorridorResult(
        corridor_geojson=geojson,
        total_length_km=length_km,
        segment_count=total_segments,
        failed_segments=failed_segments,
        waypoints_used=waypoints,
        corridor_confidence=min(confidence, float(geography.get("geography_plausibility_score") or confidence)),
        corridor_notes=(
            f"Segmented corridor: {length_km:.1f}km, "
            f"{success_count}/{total_segments} segments OK, "
            f"{len(failed_segments)} failed"
        ),
        expected_geographic_envelope=expected_envelope,
        straight_line_km=float(geography.get("straight_line_km") or 0.0),
        corridor_inflation_ratio=float(geography.get("corridor_inflation_ratio") or 0.0),
        in_bounds_fraction=float(geography.get("in_bounds_fraction") or 0.0),
        out_of_bounds_reason=str(geography.get("out_of_bounds_reason") or ""),
        geography_plausibility_score=float(geography.get("geography_plausibility_score") or 0.0),
        rejected_for_geographic_implausibility=bool(
            geography.get("rejected_for_geographic_implausibility", False)
        ),
        route_locality_consistency_notes=list(geography.get("route_locality_consistency_notes") or []),
        valhalla_meta=aggregate_meta,
    )


def _prune_waypoints_for_geography(
    waypoints: List[Dict[str, Any]],
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    timeout_s: int = 60,
) -> CorridorResult:
    best = _build_corridor_from_waypoints(
        waypoints,
        expected_envelope=expected_envelope,
        timeout_s=timeout_s,
    )
    if not best.rejected_for_geographic_implausibility or len(waypoints) <= 3:
        return best

    current_waypoints = list(waypoints)
    removed_labels: List[str] = []

    while len(current_waypoints) > 3 and best.rejected_for_geographic_implausibility:
        current_score = float(best.geography_plausibility_score or 0.0)
        current_length = float(best.total_length_km or 0.0)
        trial_results: List[Tuple[float, float, int, CorridorResult, List[Dict[str, Any]]]] = []

        for idx in range(1, len(current_waypoints) - 1):
            trial_waypoints = current_waypoints[:idx] + current_waypoints[idx + 1 :]
            trial = _build_corridor_from_waypoints(
                trial_waypoints,
                expected_envelope=expected_envelope,
                timeout_s=timeout_s,
            )
            trial_rank = (
                2.0 if not trial.rejected_for_geographic_implausibility else 0.0,
                float(trial.geography_plausibility_score or 0.0),
                -float(trial.total_length_km or 0.0),
            )
            trial_results.append((trial_rank[0], trial_rank[1], idx, trial, trial_waypoints))

        if not trial_results:
            break

        trial_results.sort(
            key=lambda item: (item[0], item[1], -float(item[3].total_length_km or 0.0)),
            reverse=True,
        )
        _, trial_score, drop_idx, trial_best, trial_waypoints = trial_results[0]
        improved = (
            (best.rejected_for_geographic_implausibility and not trial_best.rejected_for_geographic_implausibility)
            or trial_score >= current_score + 0.10
            or (
                trial_score >= current_score
                and float(trial_best.total_length_km or 0.0) <= max(0.0, current_length - 6.0)
            )
        )
        if not improved:
            break

        removed_labels.append(str(current_waypoints[drop_idx].get("label") or f"waypoint_{drop_idx}"))
        current_waypoints = trial_waypoints
        best = trial_best

    if removed_labels:
        best.corridor_notes = f"{best.corridor_notes}; pruned_waypoints={removed_labels}"
        notes = list(best.route_locality_consistency_notes or [])
        notes.append(f"Pruned geography-conflicting waypoints: {', '.join(removed_labels)}")
        best.route_locality_consistency_notes = notes

    return best


def _get_valhalla_url() -> str:
    """Resolve Valhalla URL from env (same priority as .env.example)."""
    import os
    return (
        os.getenv("VALHALLA_URL")
        or "http://127.0.0.1:8003"
    ).rstrip("/")


def _get_valhalla_costing() -> str:
    import os
    return os.getenv("VALHALLA_COSTING", "bus")


def _call_valhalla(
    waypoints: List[LonLat], *, timeout_s: int = 60
) -> Tuple[List[LonLat], Dict[str, Any]]:
    """
    Call Valhalla route API. Returns ``(coords, meta)``.

    ``meta`` matches the schema produced by
    ``valhalla_route_with_meta`` in the phase3 client (see
    ``src/geometry/valhalla_client.py``), so it can be persisted directly
    into ``route_work.geometry_candidates.valhalla_request`` and replayed
    deterministically.

    Preferred path: delegate to ``valhalla_route_with_meta`` (full capture).
    Fallback path: direct POST with a synthesized meta dict that still
    carries enough to replay (``request_body``, ``endpoint_url``,
    ``response_hash``, ``valhalla_version``, ``capture_status``).
    """
    try:
        from phase3_routes.services.route_constructor.src.geometry.valhalla_client import (
            valhalla_route_with_meta,
        )
        return valhalla_route_with_meta(waypoints, timeout_s=timeout_s)
    except ImportError:
        pass

    # Direct Valhalla call if the phase3 service module isn't available.
    # Build a meta dict equivalent in spirit to valhalla_route_with_meta —
    # the capture is less rich, but the route remains re-traceable from
    # the stored request_body + endpoint.
    import hashlib
    import os
    from datetime import datetime, timezone
    import requests as _requests

    url = f"{_get_valhalla_url()}/route"
    costing = _get_valhalla_costing()
    shape_format = os.getenv("VALHALLA_SHAPE_FORMAT", "polyline6")

    payload = {
        "locations": [
            {"lat": lat, "lon": lon, "type": "break"}
            for lon, lat in waypoints
        ],
        "costing": costing,
        "shape_format": shape_format,
        "shape_match": "map_snap",
    }

    requested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    resp = _requests.post(url, json=payload, timeout=timeout_s)
    resp.raise_for_status()
    data = resp.json()

    request_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    response_hash = hashlib.sha256(resp.content).hexdigest()
    valhalla_version = resp.headers.get("X-Valhalla-Version", "unknown")

    legs = (data.get("trip") or {}).get("legs") or []
    if not legs:
        raise ValueError("Valhalla response missing trip.legs")

    out: List[LonLat] = []
    for leg in legs:
        shape = leg.get("shape")
        if shape is None:
            raise ValueError("Valhalla leg missing shape")

        if isinstance(shape, str):
            # polyline6 decoding
            pts = _decode_polyline6(shape)
        elif isinstance(shape, list):
            pts = [(float(pt[0]), float(pt[1])) for pt in shape]
        else:
            raise ValueError(f"Unknown Valhalla shape type: {type(shape)}")

        if out and pts and pts[0] == out[-1]:
            pts = pts[1:]
        out.extend(pts)

    if len(out) < 2:
        raise ValueError("Valhalla returned <2 shape points")

    chunk_meta = {
        "endpoint_url": url,
        "http_method": "POST",
        "request_payload": payload,
        "requested_at": requested_at,
        "valhalla_version": valhalla_version,
        "request_hash": request_hash,
        "response_hash": response_hash,
        "n_locations": len(waypoints),
    }
    meta: Dict[str, Any] = {
        "chunked": False,
        "n_locations": len(waypoints),
        "n_chunks": 1,
        "endpoint_url": url,
        "valhalla_version": valhalla_version,
        "requested_at": requested_at,
        "max_locations_per_call": len(waypoints),
        "costing": costing,
        "shape_format": shape_format,
        "costing_options": {},
        "chunks": [chunk_meta],
        "request_hash": request_hash,
        "response_hash": response_hash,
        "capture_status": "fallback_capture",
        "source": "corridor_builder._call_valhalla.direct_post",
    }
    return out, meta


def _decode_polyline6(encoded: str) -> List[LonLat]:
    """Decode a polyline6-encoded string to list of (lon, lat) tuples."""
    inv = 1.0 / 1e6
    decoded: List[LonLat] = []
    lat = 0
    lon = 0
    i = 0
    while i < len(encoded):
        # latitude
        shift = 0
        result = 0
        while True:
            b = ord(encoded[i]) - 63
            i += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        lat += (~(result >> 1) if (result & 1) else (result >> 1))

        # longitude
        shift = 0
        result = 0
        while True:
            b = ord(encoded[i]) - 63
            i += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        lon += (~(result >> 1) if (result & 1) else (result >> 1))

        decoded.append((lon * inv, lat * inv))  # (lon, lat) order
    return decoded


def build_corridor(
    grounding: StopGroundingResult,
    intermediate_hints_order: List[str],
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    timeout_s: int = 60,
    arterial_waypoints: Optional[List[Dict[str, Any]]] = None,
) -> CorridorResult:
    """
    Build a Valhalla corridor from grounded stop waypoints.

    Waypoint order: anchor_a → [arterial] → intermediate_1 → ... → intermediate_n → [arterial] → anchor_b

    If the full route fails, tries segmented routing between consecutive pairs.
    arterial_waypoints: optional list from Layer 4 to inject known highway waypoints.
    """
    anchor_a = grounding.best_anchor_a()
    anchor_b = grounding.best_anchor_b()

    if not anchor_a or not anchor_b:
        missing = []
        if not anchor_a:
            missing.append("anchor_a")
        if not anchor_b:
            missing.append("anchor_b")
        return CorridorResult(
            corridor_notes=f"Cannot build corridor: missing {', '.join(missing)}",
        )

    # Build ordered waypoints
    waypoints: List[Dict[str, Any]] = []
    waypoints.append({
        "lon": anchor_a.lon, "lat": anchor_a.lat,
        "stop_id": anchor_a.stop_id, "label": "anchor_a",
        "type": "break",
    })

    # Inject arterial waypoints (Layer 4) before intermediates
    if arterial_waypoints:
        for art_wp in arterial_waypoints:
            waypoints.append({
                "lon": float(art_wp["lon"]),
                "lat": float(art_wp["lat"]),
                "stop_id": str(art_wp.get("stop_id") or ""),
                "label": str(art_wp.get("label") or "arterial"),
                "type": "through",
            })

    best_intermediates = grounding.best_intermediates_ordered(intermediate_hints_order)
    for match in best_intermediates:
        waypoints.append({
            "lon": match.lon, "lat": match.lat,
            "stop_id": match.stop_id, "label": f"intermediate:{match.stop_name}",
            "type": "through",
        })

    waypoints.append({
        "lon": anchor_b.lon, "lat": anchor_b.lat,
        "stop_id": anchor_b.stop_id, "label": "anchor_b",
        "type": "break",
    })
    waypoints = _dedupe_waypoints(waypoints)

    return _prune_waypoints_for_geography(
        waypoints,
        expected_envelope=expected_envelope,
        timeout_s=timeout_s,
    )


def rebuild_corridor_with_sequence(
    sequence_stops: List[Dict[str, Any]],
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    timeout_s: int = 60,
) -> CorridorResult:
    """
    Re-run Valhalla with the full discovered sequence as waypoints.
    Used in Stage G to refine geometry after stop discovery.
    """
    if len(sequence_stops) < 2:
        return CorridorResult(corridor_notes="Need at least 2 stops to build corridor")

    waypoints = [
        {"lon": s["lon"], "lat": s["lat"], "stop_id": s.get("stop_id", ""), "label": f"seq_{i}"}
        for i, s in enumerate(sequence_stops)
    ]
    waypoints = _dedupe_waypoints(waypoints)
    lonlat_list = [(float(wp["lon"]), float(wp["lat"])) for wp in waypoints]

    try:
        shape, valhalla_meta = _call_valhalla(lonlat_list, timeout_s=timeout_s)
        geojson = _coords_to_geojson(shape)
        length_km = _linestring_length_km(shape)
        geography = evaluate_corridor_geography(
            corridor_geojson=geojson,
            waypoints_used=waypoints,
            expected_envelope=expected_envelope,
            discovered_stop_count=len(sequence_stops),
        )
        return CorridorResult(
            corridor_geojson=geojson,
            total_length_km=length_km,
            segment_count=1,
            waypoints_used=waypoints,
            corridor_confidence=max(0.0, min(1.0, float(geography.get("geography_plausibility_score") or 1.0))),
            corridor_notes=f"Refined corridor: {length_km:.1f}km, {len(shape)} points, {len(sequence_stops)} waypoints",
            expected_geographic_envelope=expected_envelope,
            straight_line_km=float(geography.get("straight_line_km") or 0.0),
            corridor_inflation_ratio=float(geography.get("corridor_inflation_ratio") or 0.0),
            in_bounds_fraction=float(geography.get("in_bounds_fraction") or 0.0),
            out_of_bounds_reason=str(geography.get("out_of_bounds_reason") or ""),
            geography_plausibility_score=float(geography.get("geography_plausibility_score") or 0.0),
            rejected_for_geographic_implausibility=bool(
                geography.get("rejected_for_geographic_implausibility", False)
            ),
            route_locality_consistency_notes=list(geography.get("route_locality_consistency_notes") or []),
            valhalla_meta=valhalla_meta,
        )
    except Exception as exc:
        _LOG.warning("Refined corridor construction failed: %s", exc)
        return CorridorResult(
            waypoints_used=waypoints,
            corridor_notes=f"Refined corridor failed: {exc}",
        )


def _hint_set_to_waypoints(
    candidate_set: HintCandidateSet,
    seed: TypedRouteSeed,
) -> List[Dict[str, Any]]:
    ordered: List[Dict[str, Any]] = [
        {
            "lon": candidate_set.anchor_a.lon,
            "lat": candidate_set.anchor_a.lat,
            "stop_id": candidate_set.anchor_a.stop_id,
            "label": "anchor_a",
            "type": "break",
            "stop_name": candidate_set.anchor_a.stop_name,
        }
    ]

    for token in seed.sequence_tokens[1:-1]:
        if token.resolution_policy == "use_as_corridor_constraint_only":
            continue
        match = candidate_set.intermediates.get(token.label)
        if match is None:
            continue
        ordered.append(
            {
                "lon": match.lon,
                "lat": match.lat,
                "stop_id": match.stop_id,
                "label": f"intermediate:{token.label}",
                "type": "through",
                "stop_name": match.stop_name,
            }
        )

    ordered.append(
        {
            "lon": candidate_set.anchor_b.lon,
            "lat": candidate_set.anchor_b.lat,
            "stop_id": candidate_set.anchor_b.stop_id,
            "label": "anchor_b",
            "type": "break",
            "stop_name": candidate_set.anchor_b.stop_name,
        }
    )
    return _dedupe_waypoints(ordered)


def _inject_anchor_terminus_waypoints(
    waypoints: List[Dict[str, Any]],
    seed: TypedRouteSeed,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """BUG-005: Inject explicit Valhalla waypoints for tokens with anchor coords.

    When a TypedSeedToken has has_anchor_coords=True and anchor_role in
    ("terminus", "waypoint"), inject those coords so Valhalla routes through
    the actual terminus location instead of stopping short.

    Returns (updated_waypoints, injection_log).
    """
    tokens = seed.sequence_tokens
    if not tokens:
        return waypoints, []

    injection_log: List[Dict[str, Any]] = []
    start_injections: List[Dict[str, Any]] = []
    end_injections: List[Dict[str, Any]] = []
    mid_injections: List[Tuple[int, Dict[str, Any]]] = []

    for token in tokens:
        if not token.has_anchor_coords:
            continue
        if token.anchor_role not in ("terminus", "waypoint"):
            continue

        wp = {
            "lat": token.anchor_lat,
            "lon": token.anchor_lon,
            "stop_id": f"anchor:{token.label.lower().replace(' ', '_')}",
            "label": f"anchor_terminus:{token.label}",
            "type": "break" if token.anchor_role == "terminus" else "through",
            "stop_name": token.label,
        }

        # BUG-011: Check proximity to existing waypoints
        # Terminus anchors are ALWAYS injected — never skipped
        # Coord waypoints (from explicit_anchors with lat/lon) are NEVER skipped (v5.3)
        # Non-terminus textual anchors skip only if within 150m
        ANCHOR_WAYPOINT_SKIP_RADIUS_M = 150.0
        dist_to_nearest = float("inf")
        for existing in waypoints:
            dist = _haversine_m(
                float(existing.get("lon") or 0), float(existing.get("lat") or 0),
                token.anchor_lon, token.anchor_lat,
            )
            if dist < dist_to_nearest:
                dist_to_nearest = dist

        if token.anchor_role == "terminus":
            # Terminus anchors are NEVER skipped regardless of proximity
            _LOG.info(
                "[VALHALLA TERMINUS ANCHOR] route=%s, token=%s, dist_to_nearest=%.0fm (always injected)",
                seed.route_name, token.label, dist_to_nearest,
            )
        elif token.has_anchor_coords:
            # v5.3: Coord waypoints from explicit_anchors are NEVER skipped
            _LOG.info(
                "[VALHALLA COORD WAYPOINT] route=%s, token=%s, dist_to_nearest=%.0fm (coord bypass, always injected)",
                seed.route_name, token.label, dist_to_nearest,
            )
        elif dist_to_nearest < ANCHOR_WAYPOINT_SKIP_RADIUS_M:
            _LOG.info(
                "[VALHALLA ANCHOR SKIP] route=%s, token=%s, dist=%.0fm < %.0fm threshold",
                seed.route_name, token.label, dist_to_nearest, ANCHOR_WAYPOINT_SKIP_RADIUS_M,
            )
            continue

        if token.position == 1 or token.role in ("origin_stop_candidate", "origin_area"):
            start_injections.append(wp)
        elif token.position == len(tokens) or token.role in ("destination_stop_candidate", "destination_area"):
            end_injections.append(wp)
        else:
            mid_injections.append((token.position, wp))

        injection_log.append({
            "token": token.label,
            "lat": token.anchor_lat,
            "lon": token.anchor_lon,
            "role": token.anchor_role,
            "position": token.position,
        })
        _LOG.info(
            "[VALHALLA ANCHOR WAYPOINT] route=%s, token=%s, lat=%.4f, lon=%.4f, position=%d",
            seed.route_name, token.label, token.anchor_lat, token.anchor_lon, token.position,
        )

    if not injection_log:
        return waypoints, []

    # Rebuild waypoints: start_anchors + original[0] + mid_injections + intermediates + original[-1] + end_anchors
    result = list(start_injections)
    result.append(waypoints[0])
    # Insert mid-injections among intermediates
    intermediates = waypoints[1:-1]
    mid_injections.sort(key=lambda x: x[0])
    mid_wp_list = [wp for _, wp in mid_injections]
    result.extend(mid_wp_list)
    result.extend(intermediates)
    result.append(waypoints[-1])
    result.extend(end_injections)

    return result, injection_log


def build_typed_corridor(
    candidate_set: HintCandidateSet,
    seed: TypedRouteSeed,
    corridor_constraints: List[CorridorConstraint],
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
    timeout_s: int = 60,
    route_context=None,
) -> Tuple[CorridorResult, Dict[str, Any]]:
    waypoints = _hint_set_to_waypoints(candidate_set, seed)

    # BUG-005: Inject anchor terminus coords as explicit Valhalla waypoints
    waypoints, anchor_injection_log = _inject_anchor_terminus_waypoints(waypoints, seed)

    # Check if catalog entry disables arterial injection
    skip_arterials = bool((seed.source_entry or {}).get("skip_arterial_injection"))
    if skip_arterials:
        _LOG.info("[ARTERIAL SKIP] skip_arterial_injection=true in catalog for '%s'", seed.route_name)
        constraint_waypoints: List[Dict[str, Any]] = []
        constraint_log: Dict[str, Any] = {"arterials": [], "constraint_waypoints": [], "skipped": True}
    else:
        # Camino D PIEZA 7: propagate seed.province.
        constraint_waypoints, constraint_log = resolve_constraint_waypoints(
            seed.route_name,
            corridor_constraints,
            anchor_a=candidate_set.anchor_a,
            anchor_b=candidate_set.anchor_b,
            province=getattr(seed, "province", None),
        )

    # Inject arterials from geography catalog expected_arterials
    geo_arterial_waypoints = []
    if route_context and hasattr(route_context, 'expected_arterials'):
        for arterial_name in route_context.expected_arterials:
            from datamind_console.phases.phase3_routes.stop_grounding.arterial_waypoints import (
                KNOWN_ARTERIAL_CORRIDORS,
            )
            norm = arterial_name.lower().strip()
            for corridor_name, corridor_def in KNOWN_ARTERIAL_CORRIDORS.items():
                if any(trigger in norm for trigger in corridor_def.get("triggers", [])):
                    for wp in corridor_def["waypoints"]:
                        geo_arterial_waypoints.append({
                            "lat": wp["lat"],
                            "lon": wp["lon"],
                            "label": wp["name"],
                            "type": "through",
                            "source": f"geo_catalog:{corridor_name}",
                        })
                    break

    all_constraints = list(constraint_waypoints) + geo_arterial_waypoints
    if all_constraints:
        waypoints = _dedupe_waypoints(
            [waypoints[0], *all_constraints, *waypoints[1:-1], waypoints[-1]]
        )
    elif constraint_waypoints:
        waypoints = _dedupe_waypoints(
            [waypoints[0], *constraint_waypoints, *waypoints[1:-1], waypoints[-1]]
        )

    constraint_log["geo_catalog_arterials"] = [
        {"label": wp.get("label"), "source": wp.get("source")}
        for wp in geo_arterial_waypoints
    ]
    constraint_log["anchor_terminus_injections"] = anchor_injection_log

    corridor = _prune_waypoints_for_geography(
        waypoints,
        expected_envelope=expected_envelope,
        timeout_s=timeout_s,
    )
    return corridor, {
        "constraint_waypoints": constraint_waypoints,
        "constraint_log": constraint_log,
        "final_waypoint_count": len(waypoints),
        "geo_catalog_arterial_count": len(geo_arterial_waypoints),
        "anchor_terminus_injections": len(anchor_injection_log),
    }
