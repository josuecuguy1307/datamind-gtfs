# views/widgets/node_set_cards.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import streamlit as st

try:
    import pydeck as pdk
except Exception:
    pdk = None

from phases.phase1_nodes.client import _get_phase1_client

LatLon = Tuple[float, float]


# ---------------------------
# Helpers (safe keys)
# ---------------------------

def _safe_token(x: Any) -> str:
    s = str(x)
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in s)

def _k(prefix: str, node_set_id: str, name: str) -> str:
    return f"{prefix}.{_safe_token(node_set_id)}.{name}"


# ---------------------------
# Summary model
# ---------------------------

@dataclass(frozen=True)
class NodeSetSummary:
    node_set_id: str
    title: str

    context_key: Optional[str] = None
    created_at: Optional[str] = None

    n_nodes: Optional[int] = None
    n_stop_candidates: Optional[int] = None
    n_poi_candidates: Optional[int] = None
    n_clusters: Optional[int] = None

    bbox: Optional[Tuple[float, float, float, float]] = None  # (min_lat, min_lon, max_lat, max_lon)
    sample_points: Optional[List[LatLon]] = None              # [(lat, lon), ...]


# ---------------------------
# Client adapters (NO HTTP)
# ---------------------------

def _call_first(obj: Any, names: Sequence[str], *args, **kwargs):
    """
    Try obj.<name>(*args, **kwargs) for the first name that exists.
    """
    for n in names:
        fn = getattr(obj, n, None)
        if callable(fn):
            return fn(*args, **kwargs)
    return None


def _summary_from_dict(d: Dict[str, Any]) -> NodeSetSummary:
    node_set_id = str(d.get("node_set_id") or d.get("id") or d.get("nodeSetId") or "")
    title = str(d.get("name") or d.get("title") or f"Node set {node_set_id}")

    # sample points can come as [{lat,lon}] or [(lat,lon)]
    sp = d.get("sample_points") or d.get("points") or d.get("samplePoints")
    sample_points: Optional[List[LatLon]] = None
    if isinstance(sp, list) and sp:
        if isinstance(sp[0], dict) and "lat" in sp[0] and "lon" in sp[0]:
            sample_points = [(float(p["lat"]), float(p["lon"])) for p in sp]
        elif isinstance(sp[0], (tuple, list)) and len(sp[0]) == 2:
            sample_points = [(float(a), float(b)) for (a, b) in sp]

    bbox = d.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        bbox = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    else:
        bbox = None

    return NodeSetSummary(
        node_set_id=node_set_id,
        title=title,
        context_key=d.get("context_key") or d.get("contextKey"),
        created_at=str(d.get("created_at") or d.get("createdAt")) if (d.get("created_at") or d.get("createdAt")) else None,
        n_nodes=d.get("n_nodes") or d.get("nodes_count") or d.get("count_nodes"),
        n_stop_candidates=d.get("n_stop_candidates") or d.get("stop_candidates"),
        n_poi_candidates=d.get("n_poi_candidates") or d.get("poi_candidates"),
        n_clusters=d.get("n_clusters") or d.get("clusters"),
        bbox=bbox,
        sample_points=sample_points,
    )


def fetch_node_set_summary(node_set_id: str) -> NodeSetSummary:
    """
    Direct Phase1Client wiring.
    """
    phase1 = _get_phase1_client()

    d = phase1.get_node_set_details(node_set_id)

    # sample points → small preview
    pts = phase1.get_resolved_points(node_set_id, limit=120)

    sample_points: Optional[List[LatLon]] = None
    if pts:
        sample_points = [(float(p["lat"]), float(p["lon"])) for p in pts]

    return NodeSetSummary(
        node_set_id=str(d.get("node_set_id")),
        title=f"Node set {str(d.get('node_set_id'))[:8]}",
        context_key=d.get("context_key"),
        created_at=str(d.get("created_at")) if d.get("created_at") else None,
        n_nodes=d.get("n_resolved"),
        n_stop_candidates=d.get("n_stop_like"),
        n_poi_candidates=d.get("n_poi_like"),
        n_clusters=d.get("n_candidates"),
        bbox=None,  # not exposed by your view yet
        sample_points=sample_points,
    )

# ---------------------------
# Mini map preview (no Mapbox)
# ---------------------------

def _center_zoom_from_points(points: Sequence[LatLon]) -> Tuple[LatLon, float]:
    if not points:
        return (0.0, 0.0), 10
    lat = sum(p[0] for p in points) / len(points)
    lon = sum(p[1] for p in points) / len(points)
    # simple fixed zoom for previews
    return (lat, lon), 11

def render_points_preview(points: Sequence[LatLon], *, height: int, key: str) -> None:
    if pdk is None or not points:
        st.caption("Map preview unavailable.")
        return

    center, zoom = _center_zoom_from_points(points)

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=[{"lat": lat, "lon": lon} for (lat, lon) in points],
        get_position=["lon", "lat"],
        get_radius=18,
        radius_min_pixels=4,
        pickable=False,
    )

    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=pdk.ViewState(
            latitude=float(center[0]),
            longitude=float(center[1]),
            zoom=float(zoom),
            pitch=0,
        ),
        map_style=None,  # no Mapbox dependency
        tooltip=None,
    )

    st.pydeck_chart(deck, height=height, use_container_width=True, key=key)


# ---------------------------
# Main widget: Summary Card
# ---------------------------
def render_node_set_summary_card(
    *,
    node_set_id: str,
    key_prefix: str = "p1.summary",   # DEFAULT
    show_map: bool = True,
    map_height: int = 160,
    primary_label: str = "Use this node set",
    secondary_label: Optional[str] = "Inspect",
) -> Optional[Tuple[str, str]]:

    """
    Renders a card for a node_set_id.
    Returns: ("primary"|"secondary", node_set_id) if clicked.
    """

    s = fetch_node_set_summary(node_set_id)


    with st.container(border=True):
        left, mid, right = st.columns([3, 5, 2], vertical_alignment="top")

        with left:
            st.markdown(f"**{s.title}**")
            st.caption(s.node_set_id)

            if s.context_key:
                st.write(f"**context:** `{s.context_key}`")
            if s.created_at:
                st.write(f"**created:** {s.created_at}")

            # Metrics (only show if present)
            if s.n_nodes is not None:
                st.write(f"**nodes:** {s.n_nodes}")
            if s.n_stop_candidates is not None:
                st.write(f"**STOP cand.:** {s.n_stop_candidates}")
            if s.n_poi_candidates is not None:
                st.write(f"**POI cand.:** {s.n_poi_candidates}")
            if s.n_clusters is not None:
                st.write(f"**clusters:** {s.n_clusters}")

        with mid:
            if show_map:
                pts = s.sample_points or []
                if pts:
                    render_points_preview(
                        pts,
                        height=map_height,
                        key=_k(key_prefix, s.node_set_id, "map"),
                    )
                else:
                    st.caption("No sample points available for preview.")

        with right:
            evt: Optional[Tuple[str, str]] = None

            if st.button(primary_label, key=_k(key_prefix, s.node_set_id, "btn.primary"), use_container_width=True):
                evt = ("primary", s.node_set_id)

            if secondary_label:
                if st.button(secondary_label, key=_k(key_prefix, s.node_set_id, "btn.secondary"), use_container_width=True):
                    evt = ("secondary", s.node_set_id)

            return evt

    return None
