from __future__ import annotations
import pydeck as pdk
from typing import List, Dict, Any

def build_phase2_layers(
    *,
    candidates: List[Dict[str, Any]],  # [{lon, lat, score, ...}]
    accepted: List[Dict[str, Any]],    # [{lon, lat, ...}]
    show_compare: bool = True,
) -> List[pdk.Layer]:
    layers: List[pdk.Layer] = []

    # Regular bins: nice for spotting over-generation zones
    layers.append(
        pdk.Layer(
            "GridLayer",
            data=candidates,
            get_position="[lon, lat]",
            cell_size=60,
            elevation_scale=8,
            pickable=True,
            extruded=True,
        )
    )

    # Top candidates
    if candidates:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=candidates,
                get_position="[lon, lat]",
                get_radius=14,
                pickable=True,
            )
        )

    # Compare “accepted” overlay (optional)
    if show_compare and accepted:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=accepted,
                get_position="[lon, lat]",
                get_radius=22,
                pickable=True,
            )
        )

    return layers
