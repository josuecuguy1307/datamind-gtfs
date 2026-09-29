from __future__ import annotations

import requests
from typing import Any, Dict, List, Optional, Tuple

from src.settings import OVERPASS_URL

BBox = Tuple[float, float, float, float]  # (south, west, north, east)


def normalize_query_strategy(raw: Any) -> str:
    txt = str(raw or "").strip().lower()
    if txt in {"strict", "filtered", "metadata_filtered", "narrow"}:
        return "metadata_filtered"
    return "bbox_first_broad"


def search_route_relations(
    bbox: BBox,
    *,
    refs: Optional[List[str]] = None,
    operator_contains: Optional[str] = None,
    name_contains: Optional[str] = None,
    ref_mode: str = "contains",  # "contains" | "exact"
    query_strategy: str = "bbox_first_broad",
    timeout_s: int = 60,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Find candidate relations of type route / route_master inside bbox.

    This function is intentionally GENERIC:
    - supports regex-based ref matching
    - returns both route and route_master
    - does NOT expand or score

    Returns minimal objects:
      [{"id": <int>, "tags": {...}}]
    """
    s, w, n, e = bbox

    route_filters: List[str] = []
    route_master_filters: List[str] = []
    strategy = normalize_query_strategy(query_strategy)

    # Transport modes (kept broad on purpose)
    route_filters.append('["route"~"bus|minibus|trolleybus|share_taxi",i]')
    route_master_filters.append('["route_master"~"bus|minibus|trolleybus|share_taxi",i]')

    use_metadata_filters = strategy == "metadata_filtered"

    if use_metadata_filters and operator_contains:
        route_filters.append(f'["operator"~"{operator_contains}",i]')
        route_master_filters.append(f'["operator"~"{operator_contains}",i]')

    if use_metadata_filters and name_contains:
        route_filters.append(f'["name"~"{name_contains}",i]')
        route_master_filters.append(f'["name"~"{name_contains}",i]')

    if use_metadata_filters and refs:
        clean_refs = [r.replace('"', "").strip() for r in refs if r.strip()]
        if clean_refs:
            pattern = "|".join(clean_refs)
            if ref_mode == "exact":
                route_filters.append(f'["ref"~"^({pattern})$",i]')
                route_master_filters.append(f'["ref"~"^({pattern})$",i]')
            else:
                route_filters.append(f'["ref"~"({pattern})",i]')
                route_master_filters.append(f'["ref"~"({pattern})",i]')

    route_filt = "".join(route_filters)
    route_master_filt = "".join(route_master_filters)

    query = f"""
    [out:json][timeout:{timeout_s}];
    (
      relation["type"="route"]{route_filt}({s},{w},{n},{e});
      relation["type"="route_master"]{route_master_filt}({s},{w},{n},{e});
    );
    out tags qt {limit};
    """

    resp = requests.post(
        OVERPASS_URL,
        data=query.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
        timeout=timeout_s,
    )
    resp.raise_for_status()

    try:
        data = resp.json()
    except Exception as e:
        snippet = (resp.text or "")[:300].replace("\n", " ")
        raise RuntimeError(f"Overpass returned non-JSON response. HTTP {resp.status_code}. Body: {snippet}") from e

    out: List[Dict[str, Any]] = []
    for el in data.get("elements", []):
        if el.get("type") != "relation":
            continue
        out.append(
            {
                "id": int(el["id"]),
                "tags": el.get("tags") or {},
            }
        )

    return out


def fetch_relation_overpass_json(
    osm_relation_id: int,
    *,
    timeout_s: int = 90,
) -> Dict[str, Any]:
    """
    Fetch full Overpass JSON for a relation, including all members.

    This is used by:
      - Step 05 (discover) for inspection / expansion
      - Step 10 (fetch) for materialization into route_raw

    DO NOT add logic here.
    """
    query = f"""
    [out:json][timeout:{timeout_s}];
    relation({int(osm_relation_id)});
    (._;>;);
    out body center qt;
    """

    resp = requests.post(
        OVERPASS_URL,
        data=query.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
        timeout=timeout_s,
    )
    resp.raise_for_status()

    try:
        return resp.json()
    except Exception as e:
        snippet = (resp.text or "")[:300].replace("\n", " ")
        raise RuntimeError(f"Overpass returned non-JSON response. HTTP {resp.status_code}. Body: {snippet}") from e
