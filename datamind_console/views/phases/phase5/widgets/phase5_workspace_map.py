from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st
import pydeck as pdk


def _path_from_geojson(geojson: Dict[str, Any]) -> List[List[float]]:
    gtype = str(geojson.get("type") or "")
    coords = geojson.get("coordinates") or []
    if gtype == "LineString" and isinstance(coords, list):
        return [[float(x[1]), float(x[0])] for x in coords if isinstance(x, (list, tuple)) and len(x) >= 2]
    if gtype == "MultiLineString" and isinstance(coords, list) and coords:
        merged: List[List[float]] = []
        for seg in coords:
            if isinstance(seg, list):
                merged.extend([[float(x[1]), float(x[0])] for x in seg if isinstance(x, (list, tuple)) and len(x) >= 2])
        return merged
    return []


@st.cache_data(ttl=120, show_spinner=False)
def _cached_route_map_payload(*, _client: Any, route_id: str) -> dict:
    return _client.get_route_map_payload(route_id)


def render_phase5_workspace_map(*, client: Any, key: str = "p5_workspace") -> None:
    ss = st.session_state
    st.markdown("#### Workspace")
    c1, c2 = st.columns([1, 1])
    with c1:
        st.button("Load / Refresh workspace", use_container_width=True, key=f"{key}.refresh")
    with c2:
        if st.button("Clear map cache", use_container_width=True, key=f"{key}.clear_cache"):
            st.cache_data.clear()
            st.rerun()

    service_route_id = (ss.get("phase5.service_route_id") or "").strip()
    direction_id = int(ss.get("phase5.direction_id") or 0)
    route_id = (ss.get("phase5.route_id") or "").strip()
    if not route_id:
        st.info("Select service_route_id + direction in Route inputs first.")
        return
    st.caption(
        f"service_route_id: `{service_route_id or '-'}` | direction_id: `{direction_id}` | route_id: `{route_id}`"
    )

    try:
        payload = _cached_route_map_payload(_client=client, route_id=route_id)
    except Exception as e:
        st.error(f"Failed to load route geometry: {e}")
        return

    stops = payload.get("stops") or []
    path = _path_from_geojson(payload.get("route_geojson") or {})

    if not stops and not path:
        st.info("No route geometry/stops available for this route.")
        return

    center_lat = float(stops[0].get("lat")) if stops else float(path[0][0])
    center_lon = float(stops[0].get("lon")) if stops else float(path[0][1])

    layers: List[pdk.Layer] = []
    if path:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=[{"path": [[p[1], p[0]] for p in path]}],
                get_path="path",
                get_width=5,
                get_color=[43, 110, 255, 200],
                width_min_pixels=3,
                pickable=False,
            )
        )

    if stops:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=stops,
                get_position="[lon, lat]",
                get_radius=8,
                radius_min_pixels=3,
                radius_max_pixels=7,
                get_fill_color=[12, 56, 200, 220],
                get_line_color=[255, 255, 255, 220],
                line_width_min_pixels=1,
                stroked=True,
                pickable=True,
            )
        )

    tooltip = {
        "html": (
            "<b>#{seq}</b> {name}<br/>"
            "place: {canonical_name} ({place_type})<br/>"
            "node: {node_id}<br/>"
            "lat: {lat}<br/>lon: {lon}<br/>"
            "map_conf: {map_confidence}"
        ),
        "style": {"backgroundColor": "rgba(17, 25, 40, 0.92)", "color": "white"},
    }
    st.pydeck_chart(
        pdk.Deck(
            map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
            initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=12, pitch=0),
            layers=layers,
            tooltip=tooltip,
        ),
        use_container_width=True,
        key=f"{key}.deck",
    )

    st.caption(f"Route: {payload.get('route_name') or route_id} | Stops: {len(stops)}")

    meta = payload.get("route_meta") or {}
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Geom verified (P3)", str(meta.get("geom_human_verified")))
    m2.metric("Semantics verified (P4)", str(meta.get("semantics_human_verified")))
    m3.metric("Geom confidence", f"{float(meta.get('geom_naming_confidence') or 0.0):.3f}")
    m4.metric("Name confidence", f"{float(meta.get('semantics_naming_confidence') or 0.0):.3f}")

    with st.expander("Phase 3/4 route details", expanded=False):
        st.json(meta)

    with st.expander("Route stops (final geocoder mapping)", expanded=False):
        st.dataframe(stops, use_container_width=True, hide_index=True, height=240)
