from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple


# Common route member roles used for stops/platforms in PTv2 relations
STOP_ROLE_EXACT = {
    "stop", "stop_entry_only", "stop_exit_only",
    "platform", "platform_entry_only", "platform_exit_only",
    "stop_position",
    "hail_and_ride",
}

# Some mappers use variations
STOP_ROLE_SUBSTRINGS = ("stop", "platform", "stop_position")


def _is_stop_role(role: str) -> bool:
    r = (role or "").strip().lower()
    if not r:
        return False
    if r in STOP_ROLE_EXACT:
        return True
    return any(s in r for s in STOP_ROLE_SUBSTRINGS)


def _is_stop_tags(tags: Dict[str, Any]) -> bool:
    # PTv2 + common OSM tagging patterns
    pt = (tags.get("public_transport") or "").strip().lower()
    hw = (tags.get("highway") or "").strip().lower()
    rw = (tags.get("railway") or "").strip().lower()

    if hw == "bus_stop":
        return True
    if pt in {"platform", "stop_position", "stop_area"}:
        return True
    if rw == "platform":
        return True

    # Optional: treat stations as “stops” if you want (usually too coarse)
    # if tags.get("amenity") == "bus_station":
    #     return True

    return False


def _way_point(way: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """
    Return representative (lat, lon) for a way:
    - prefer 'center'
    - else if 'geometry' exists (list of points), use average
    """
    center = way.get("center")
    if isinstance(center, dict) and "lat" in center and "lon" in center:
        return float(center["lat"]), float(center["lon"])

    geom = way.get("geometry")
    if isinstance(geom, list) and geom:
        lats = [float(p["lat"]) for p in geom if "lat" in p and "lon" in p]
        lons = [float(p["lon"]) for p in geom if "lat" in p and "lon" in p]
        if lats and lons and len(lats) == len(lons):
            return (sum(lats) / len(lats), sum(lons) / len(lons))

    return None


def _node_point(node: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    if "lat" in node and "lon" in node:
        return float(node["lat"]), float(node["lon"])
    return None


def _relation_score(rel: Dict[str, Any]) -> float:
    """
    Pick the “best” route relation from overpass_json:
    prefer type=route, route=bus/trolleybus/etc, has members, has ref/name.
    """
    tags = rel.get("tags") or {}
    t = (tags.get("type") or "").lower()
    route = (tags.get("route") or "").lower()
    members = rel.get("members") or []

    score = 0.0
    if t == "route":
        score += 100.0
    elif t == "route_master":
        score += 10.0

    if route in {"bus", "trolleybus", "tram", "share_taxi", "subway"}:
        score += 20.0

    if tags.get("ref"):
        score += 5.0
    if tags.get("name"):
        score += 2.0

    score += min(len(members), 200) / 10.0  # member count signal
    return score


def extract_stop_prior(overpass_json: dict) -> List[dict]:
    """
    Returns ordered stop prior extracted from a route relation.

    Output rows:
      {
        "seq": int,
        "osm_ref": int,           # node/way id
        "member_type": "node"|"way",
        "role": str,
        "lat": float,
        "lon": float,
        "source": "route_member"|"stop_area_member"
      }

    Notes:
    - Handles route_master trap by scoring + selecting best relation.
    - Expands relation members that look like stop_area relations into their node/way platforms.
    """
    elements = overpass_json.get("elements", [])
    if not isinstance(elements, list) or not elements:
        return []

    # Index all elements
    nodes_by_id: Dict[int, Dict[str, Any]] = {}
    ways_by_id: Dict[int, Dict[str, Any]] = {}
    rels_by_id: Dict[int, Dict[str, Any]] = {}

    for e in elements:
        et = e.get("type")
        eid = e.get("id")
        if et == "node" and isinstance(eid, int):
            nodes_by_id[eid] = e
        elif et == "way" and isinstance(eid, int):
            ways_by_id[eid] = e
        elif et == "relation" and isinstance(eid, int):
            rels_by_id[eid] = e

    rels = list(rels_by_id.values())
    if not rels:
        return []

    # Pick best relation (route > route_master)
    rel = max(rels, key=_relation_score)
    members = rel.get("members") or []
    if not members:
        return []

    # Helper to append stop while keeping order + de-duping
    seen: set[tuple[str, int]] = set()
    prior: List[dict] = []
    seq = 0

    def add_stop(*, member_type: str, osm_ref: int, role: str, lat: float, lon: float, source: str) -> None:
        nonlocal seq
        key = (member_type, int(osm_ref))
        if key in seen:
            return
        seen.add(key)
        prior.append(
            {
                "seq": seq,
                "osm_ref": int(osm_ref),
                "member_type": member_type,
                "role": role,
                "lat": float(lat),
                "lon": float(lon),
                "source": source,
            }
        )
        seq += 1

    def expand_stop_area_relation(rel_id: int, inherited_role: str) -> None:
        """
        If a route relation contains a stop_area relation as member,
        expand its members to platforms/stop_positions (node/way).
        """
        sr = rels_by_id.get(rel_id)
        if not sr:
            return
        tags = sr.get("tags") or {}
        pt = (tags.get("public_transport") or "").lower()
        t = (tags.get("type") or "").lower()

        # Only expand if it looks like a stop_area container
        if not (pt == "stop_area" or t == "stop_area"):
            return

        for sm in (sr.get("members") or []):
            sm_type = sm.get("type")
            sm_ref = sm.get("ref")
            sm_role = (sm.get("role") or "").strip().lower()
            role = inherited_role or sm_role or "stop_area"

            if sm_type == "node" and isinstance(sm_ref, int):
                node = nodes_by_id.get(sm_ref)
                if not node:
                    continue
                tags2 = node.get("tags") or {}
                if not (_is_stop_role(sm_role) or _is_stop_tags(tags2)):
                    continue
                pt_xy = _node_point(node)
                if not pt_xy:
                    continue
                add_stop(member_type="node", osm_ref=sm_ref, role=role, lat=pt_xy[0], lon=pt_xy[1], source="stop_area_member")

            elif sm_type == "way" and isinstance(sm_ref, int):
                way = ways_by_id.get(sm_ref)
                if not way:
                    continue
                tags2 = way.get("tags") or {}
                if not (_is_stop_role(sm_role) or _is_stop_tags(tags2)):
                    continue
                pt_xy = _way_point(way)
                if not pt_xy:
                    continue
                add_stop(member_type="way", osm_ref=sm_ref, role=role, lat=pt_xy[0], lon=pt_xy[1], source="stop_area_member")

    # Walk route members in order
    for m in members:
        m_type = m.get("type")
        m_ref = m.get("ref")
        role = (m.get("role") or "").strip().lower()

        if m_type == "node" and isinstance(m_ref, int):
            node = nodes_by_id.get(m_ref)
            if not node:
                continue
            tags = node.get("tags") or {}

            if not (_is_stop_role(role) or _is_stop_tags(tags)):
                continue

            pt_xy = _node_point(node)
            if not pt_xy:
                continue

            add_stop(
                member_type="node",
                osm_ref=m_ref,
                role=role or (tags.get("public_transport") or tags.get("highway") or "stop"),
                lat=pt_xy[0],
                lon=pt_xy[1],
                source="route_member",
            )

        elif m_type == "way" and isinstance(m_ref, int):
            way = ways_by_id.get(m_ref)
            if not way:
                continue
            tags = way.get("tags") or {}

            if not (_is_stop_role(role) or _is_stop_tags(tags)):
                continue

            pt_xy = _way_point(way)
            if not pt_xy:
                continue

            add_stop(
                member_type="way",
                osm_ref=m_ref,
                role=role or (tags.get("public_transport") or tags.get("highway") or "platform"),
                lat=pt_xy[0],
                lon=pt_xy[1],
                source="route_member",
            )

        elif m_type == "relation" and isinstance(m_ref, int):
            # Expand stop_area relations (common in PTv2)
            expand_stop_area_relation(m_ref, inherited_role=role)

        else:
            continue

    return prior
