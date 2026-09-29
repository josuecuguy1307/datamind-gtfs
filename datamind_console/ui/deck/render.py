from __future__ import annotations
import streamlit as st
import pydeck as pdk
from typing import List, Optional, Dict, Any

def render_deck(
    *,
    layers: List[pdk.Layer],
    view_state: pdk.ViewState,
    tooltip: Optional[Dict[str, Any]] = None,
    map_style: Optional[str] = None,
    height: int = 520,
) -> None:
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        tooltip=tooltip,
        map_style=map_style,  # can be None (no basemap) or a style url
    )
    st.pydeck_chart(deck, use_container_width=True, height=height)
