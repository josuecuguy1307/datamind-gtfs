from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import pydeck as pdk
import streamlit as st


def _center(points: List[Dict[str, Any]]) -> Tuple[float, float]:
    lat = [p.get("lat") for p in points if p.get("lat") is not None]
    lon = [p.get("lon") for p in points if p.get("lon") is not None]
    if not lat or not lon:
        return (-0.22, -78.52)
    return (sum(lat) / len(lat), sum(lon) / len(lon))


def render_evidence_coverage_map(
    client: Any,
    *,
    place_set_id: Optional[str],
    limit: int = 20000,
    key_prefix: str = "p2",
) -> None:
    st.subheader("Evidence coverage map (work)")

    if not place_set_id:
        st.info("Select a place_set_id.")
        return

    points = client.get_place_set_points(place_set_id, limit=int(limit)) or []
    if not points:
        st.info("No points for this set.")
        return

    for r in points:
        conf = r.get("confidence")
        try:
            c = float(conf) if conf is not None else 0.5
        except Exception:
            c = 0.5
        r["_radius"] = 20 + int(60 * max(0.0, min(1.0, c)))
        r["_color"] = [80, 170, 255, 160]

    lat0, lon0 = _center(points)

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=points,
        get_position=["lon", "lat"],
        get_fill_color="_color",
        get_radius="_radius",
        pickable=True,
        auto_highlight=True,
    )

    tooltip = {
        "html": """
        <b>{proposed_canonical_name}</b><br/>
        place_candidate_id: {place_candidate_id}<br/>
        node_id: {node_id}<br/>
        node_type: {node_type}<br/>
        confidence: {confidence}<br/>
        mapping_source: {mapping_source}
        """,
        "style": {"backgroundColor": "black", "color": "white"},
    }

    st.pydeck_chart(
        pdk.Deck(
            layers=[layer],
            initial_view_state=pdk.ViewState(latitude=lat0, longitude=lon0, zoom=12, pitch=0),
            tooltip=tooltip,
        ),
        use_container_width=True,
        key=f"{key_prefix}:work_map:{place_set_id}",
    )
