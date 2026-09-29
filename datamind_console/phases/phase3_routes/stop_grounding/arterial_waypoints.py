"""
Layer 4 — Corridor constraint and arterial waypoint injection.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    CorridorConstraint,
    RouteSeed,
    StopGroundingResult,
    StopMatch,
)
from datamind_console.phases.phase3_routes.stop_grounding.geography_guardrails import (
    PLACE_BBOXES,
    infer_locality_keys,
)

KNOWN_ARTERIAL_CORRIDORS = {
    "autopista_ruminahui": {
        "triggers": [
            "quitumbe",
            "marin",
            "san roque",
            "estacion san francisco",
            "san francisco",
            "trebol",
            "quito",
            "autopista general ruminahui",
            "autopista general rumiñahui",
        ],
        "waypoints": [
            {"lat": -0.2980, "lon": -78.4890, "name": "Autopista Rumiñahui - Valle"},
            {"lat": -0.2350, "lon": -78.5100, "name": "El Trébol"},
        ],
    },
    "simon_bolivar": {
        "triggers": ["simon bolivar", "simón bolívar", "puengasi", "puengasí"],
        "waypoints": [
            {"lat": -0.2670, "lon": -78.4810, "name": "Av. Simón Bolívar - Conocoto"},
        ],
    },
}


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * radius_m * math.asin(math.sqrt(a))


def _constraint_labels(constraints: Sequence[CorridorConstraint]) -> List[str]:
    return [str(constraint.label or "").strip().lower() for constraint in constraints if str(constraint.label or "").strip()]


def _route_text_bundle(
    route_name: str,
    constraints: Sequence[CorridorConstraint],
    anchor_a: Optional[StopMatch] = None,
    anchor_b: Optional[StopMatch] = None,
) -> str:
    texts = [route_name.lower(), *_constraint_labels(constraints)]
    if anchor_a:
        texts.append(anchor_a.stop_name.lower())
        texts.append(anchor_a.locality.lower())
    if anchor_b:
        texts.append(anchor_b.stop_name.lower())
        texts.append(anchor_b.locality.lower())
    return " ".join(texts)


def _between_anchors(
    lat: float,
    lon: float,
    anchor_a: Optional[StopMatch],
    anchor_b: Optional[StopMatch],
) -> bool:
    if not anchor_a or not anchor_b:
        return True
    direct = _haversine_m(anchor_a.lon, anchor_a.lat, anchor_b.lon, anchor_b.lat)
    d1 = _haversine_m(anchor_a.lon, anchor_a.lat, lon, lat)
    d2 = _haversine_m(anchor_b.lon, anchor_b.lat, lon, lat)
    return d1 <= direct * 1.45 and d2 <= direct * 1.45


def _center_for_label(
    label: str,
    *,
    province: Optional[str] = None,
) -> Optional[Tuple[float, float]]:
    # Camino D PIEZA 7: province-aware lookup. None == sample_region (legacy).
    keys = infer_locality_keys([label], province=province)
    if province is None or str(province).strip().lower() in ("", "sample_region"):
        _prov_bboxes = PLACE_BBOXES
    else:
        from datamind_console.phases.phase3_routes.stop_grounding.place_geography_loader import (
            get_place_bboxes,
        )
        _prov_bboxes = get_place_bboxes(province)
    for key in keys:
        if key in _prov_bboxes:
            south, west, north, east = _prov_bboxes[key]
            return ((south + north) / 2.0, (west + east) / 2.0)
    return None


def _constraint_center_waypoints(
    constraints: Sequence[CorridorConstraint],
    *,
    anchor_a: Optional[StopMatch],
    anchor_b: Optional[StopMatch],
    province: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    waypoints: List[Dict[str, Any]] = []
    log: List[Dict[str, Any]] = []
    seen = set()
    for constraint in constraints:
        if constraint.kind not in {"urban_corridor", "road_corridor", "avenue", "mixed_area_corridor"}:
            continue
        center = _center_for_label(constraint.label, province=province)
        if center is None:
            continue
        lat, lon = center
        key = (round(lat, 5), round(lon, 5))
        if key in seen or not _between_anchors(lat, lon, anchor_a, anchor_b):
            continue
        seen.add(key)
        waypoints.append(
            {
                "lat": lat,
                "lon": lon,
                "label": f"constraint:{constraint.label}",
                "type": "through",
                "source": "corridor_constraint",
            }
        )
        log.append(
            {
                "label": constraint.label,
                "kind": constraint.kind,
                "resolution": "center_waypoint",
                "lat": lat,
                "lon": lon,
            }
        )
    return waypoints, log


def resolve_constraint_waypoints(
    route_name: str,
    constraints: Sequence[CorridorConstraint],
    *,
    anchor_a: Optional[StopMatch] = None,
    anchor_b: Optional[StopMatch] = None,
    province: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    text_bundle = _route_text_bundle(route_name, constraints, anchor_a, anchor_b)
    waypoints: List[Dict[str, Any]] = []
    log: Dict[str, Any] = {"arterials": [], "constraint_waypoints": []}

    for corridor_name, corridor in KNOWN_ARTERIAL_CORRIDORS.items():
        if not any(trigger in text_bundle for trigger in corridor["triggers"]):
            continue
        for waypoint in corridor["waypoints"]:
            if not _between_anchors(waypoint["lat"], waypoint["lon"], anchor_a, anchor_b):
                continue
            waypoints.append(
                {
                    "lat": waypoint["lat"],
                    "lon": waypoint["lon"],
                    "label": waypoint["name"],
                    "type": "through",
                    "source": corridor_name,
                }
            )
            log["arterials"].append(
                {
                    "corridor": corridor_name,
                    "label": waypoint["name"],
                    "lat": waypoint["lat"],
                    "lon": waypoint["lon"],
                }
            )

    center_waypoints, center_log = _constraint_center_waypoints(
        constraints,
        anchor_a=anchor_a,
        anchor_b=anchor_b,
        province=province,
    )
    waypoints.extend(center_waypoints)
    log["constraint_waypoints"].extend(center_log)
    log["total_waypoints"] = len(waypoints)
    return waypoints, log


def should_inject_arterial_waypoints(
    seed: RouteSeed,
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
) -> bool:
    route_name = str(seed.route_name or "")
    family = str((expected_envelope or seed.expected_geographic_envelope or {}).get("route_family_type") or "")
    if "connector" in family or "historic" in family:
        return True
    text = route_name.lower()
    return any(trigger in text for trigger in KNOWN_ARTERIAL_CORRIDORS["autopista_ruminahui"]["triggers"])


def get_arterial_waypoints(
    seed: RouteSeed,
    grounding: StopGroundingResult,
    *,
    expected_envelope: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    if not should_inject_arterial_waypoints(seed, expected_envelope=expected_envelope):
        return []
    # Camino D PIEZA 7: propagate seed.province.
    waypoints, _ = resolve_constraint_waypoints(
        seed.route_name,
        [],
        anchor_a=grounding.best_anchor_a(),
        anchor_b=grounding.best_anchor_b(),
        province=getattr(seed, "province", None),
    )
    return waypoints
