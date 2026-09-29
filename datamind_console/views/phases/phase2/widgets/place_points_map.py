# views/phases/phase2/widgets/place_points_map.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pydeck as pdk
import streamlit as st


def _center_from_points(points: List[Dict[str, Any]]) -> Tuple[float, float]:
    lats = [p.get("lat") for p in points if p.get("lat") is not None]
    lons = [p.get("lon") for p in points if p.get("lon") is not None]
    if not lats or not lons:
        return (-0.22, -78.52)  # Quito-ish fallback
    return (sum(lats) / len(lats), sum(lons) / len(lons))


def _to_df(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    pydeck can take list[dict] directly.
    Ensure lon/lat keys exist (pydeck wants 'longitude'/'latitude' OR custom accessors).
    We'll keep 'lon'/'lat' and use accessors.
    """
    out: List[Dict[str, Any]] = []
    for p in points:
        lat = p.get("lat")
        lon = p.get("lon")
        if lat is None or lon is None:
            continue
        out.append(p)
    return out


def _deck_scatter(points: List[Dict[str, Any]], *, tooltip_html: str, height: int = 520) -> None:
    pts = _to_df(points)
    if not pts:
        st.info("No points to render.")
        return

    c_lat, c_lon = _center_from_points(pts)

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=pts,
        get_position="[lon, lat]",
        get_radius=18,
        radius_min_pixels=2,
        radius_max_pixels=10,
        pickable=True,
        auto_highlight=True,
    )

    view_state = pdk.ViewState(latitude=c_lat, longitude=c_lon, zoom=12, pitch=0)

    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=view_state,
        tooltip={"html": tooltip_html},
        map_style=None,
    )
    st.pydeck_chart(deck, use_container_width=True, height=height)


def render_place_points_map(
    client: Any,
    *,
    mode: str,
    place_set_id: Optional[str] = None,
    place_id: Optional[str] = None,
    limit: int = 20000,
    height: int = 520,
    key_prefix: str = "p2_points_map",
) -> List[Dict[str, Any]]:
    """
    Phase 2 — Place points map

    STRICTLY aligned with your Phase2Client:

    Work (per set):
      - client.get_place_set_points(place_set_id, limit=...)
      returns rows with (from your client):
        place_set_id, place_candidate_id, proposed_canonical_name, proposed_place_type,
        node_id, confidence, mapping_source, node_type, lat, lon

    Prod:
      - client.get_prod_place_points(place_id=..., limit=...)
      returns rows with:
        place_id, canonical_name, place_type,
        node_id, confidence, mapping_source, node_type, lat, lon

    Args:
      mode: "work" or "prod"
      place_set_id: required for mode="work"
      place_id: optional filter for mode="prod"
    Returns:
      The raw list of point dicts (so the caller can also show a table, etc).
    """
    mode = (mode or "").strip().lower()
    limit = int(limit)

    if mode not in ("work", "prod"):
        st.error("place_points_map: mode must be 'work' or 'prod'.")
        return []

    if mode == "work":
        st.subheader("Place points map (work)")
        if not place_set_id:
            st.info("Select a place_set_id to render work points.")
            return []

        try:
            points = client.get_place_set_points(place_set_id, limit=limit)
        except Exception as e:
            st.error(f"Failed to load work points: {e}")
            return []

        # Tooltip ONLY using fields guaranteed by Phase2Client query
        tooltip = """
        <div style="max-width:320px">
          <div><b>{proposed_canonical_name}</b></div>
          <div>place_candidate_id: {place_candidate_id}</div>
          <div>type: {proposed_place_type}</div>
          <div>node_id: {node_id}</div>
          <div>node_type: {node_type}</div>
          <div>confidence: {confidence}</div>
          <div>source: {mapping_source}</div>
          <div>lat,lon: {lat}, {lon}</div>
        </div>
        """

        _deck_scatter(points, tooltip_html=tooltip, height=height)

        with st.expander("Points table (work)", expanded=False):
            st.dataframe(points, use_container_width=True)

        return points

    # prod
    st.subheader("Place points map (prod)")

    try:
        points = client.get_prod_place_points(place_id=place_id, limit=limit)
    except RuntimeError as e:
        st.info(str(e))
        return []
    except Exception as e:
        st.error(f"Failed to load prod points: {e}")
        return []

    tooltip = """
    <div style="max-width:320px">
      <div><b>{canonical_name}</b></div>
      <div>place_id: {place_id}</div>
      <div>type: {place_type}</div>
      <div>node_id: {node_id}</div>
      <div>node_type: {node_type}</div>
      <div>confidence: {confidence}</div>
      <div>source: {mapping_source}</div>
      <div>lat,lon: {lat}, {lon}</div>
    </div>
    """

    _deck_scatter(points, tooltip_html=tooltip, height=height)

    with st.expander("Points table (prod)", expanded=False):
        st.dataframe(points, use_container_width=True)

    return points
