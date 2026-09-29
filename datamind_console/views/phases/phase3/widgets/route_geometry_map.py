# datamind_console/ui/widgets/phase3/route_geometry_map.py
from __future__ import annotations

import streamlit as st
from uuid import UUID
from typing import List, Dict, Any
import pandas as pd
import pydeck as pdk

from datamind_console.phases.phase3_routes.client import _get_phase3_client
from .geom import parse_linestring_wkt, line_to_points_df

_MAP_STYLES = {
    "Colorful (Voyager)": "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
    "Light (Positron)": "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    "Dark (Matter)": "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
}


def _current_map_style() -> str:
    name = st.session_state.get("p3.map_style_name", "Colorful (Voyager)")
    return _MAP_STYLES.get(name, _MAP_STYLES["Colorful (Voyager)"])

def render_route_geometry_map(*, route_id: str, geometry_candidate_id: str, key: str="phase3_map") -> None:
    c = _get_phase3_client()
    cand = c.get_geometry_candidate(UUID(geometry_candidate_id))
    if not cand:
        st.error("Candidate not found.")
        return

    wkt = (cand.get("geom_wkt") or "").strip()
    line = parse_linestring_wkt(wkt)
    if not line:
        st.warning("No geometry WKT for this candidate.")
        return

    # stops if any
    stops = []
    ssc = cand.get("stop_sequence_candidate_id")
    if ssc:
        stops = c.get_stop_points_for_candidate(route_id=UUID(route_id), stop_sequence_candidate_id=UUID(str(ssc)))

    st.subheader("Route geometry map")
    col1, col2 = st.columns(2)
    with col1:
        st.caption("Line vertices (sample)")
        st.dataframe(line_to_points_df(line)[:200], use_container_width=True, height=200)
    with col2:
        st.caption("Stops")
        if stops:
            st.dataframe(stops, use_container_width=True, height=200)
        else:
            st.info("No stops linked (stop_sequence_candidate_id is null).")

    # Map: render real route polyline + optional stops.
    center = line[len(line) // 2]
    path_data = [{"name": "candidate", "path": [[lon, lat] for lon, lat in line]}]
    stop_df = pd.DataFrame(
        [{"lon": float(r["lon"]), "lat": float(r["lat"])} for r in stops]
    ) if stops else pd.DataFrame(columns=["lon", "lat"])

    layers: List[pdk.Layer] = [
        pdk.Layer(
            "PathLayer",
            data=path_data,
            get_path="path",
            get_width=5,
            width_min_pixels=2,
            get_color=[255, 90, 95],
        )
    ]
    if not stop_df.empty:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=stop_df,
                get_position="[lon, lat]",
                get_radius=26,
                radius_min_pixels=2,
                get_fill_color=[255, 170, 0, 170],
            )
        )

    st.pydeck_chart(
        pdk.Deck(
            map_style=_current_map_style(),
            initial_view_state=pdk.ViewState(
                latitude=float(center[1]),
                longitude=float(center[0]),
                zoom=12,
                pitch=0,
            ),
            layers=layers,
            tooltip={"text": "{name}"},
        ),
        use_container_width=True,
    )
