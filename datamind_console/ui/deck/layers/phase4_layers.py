from __future__ import annotations
import pydeck as pdk
from typing import List, Dict, Any, Optional

def build_phase4_layers(
    *,
    approved_route: List[Dict[str, Any]],   # [{path: [[lon,lat],...]}]
    evidence_points: List[Dict[str, Any]],  # [{lon, lat, source, ...}]
    use_icons: bool = False,
) -> List[pdk.Layer]:
    layers: List[pdk.Layer] = []

    if approved_route:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=approved_route,
                get_path="path",
                get_width=6,
                pickable=True,
            )
        )

    if evidence_points:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=evidence_points,
                get_position="[lon, lat]",
                get_radius=18,
                pickable=True,
            )
        )

    # IconLayer is optional because it needs atlas/mapping.
    # Add later when you’re ready to ship icons.
    if use_icons and evidence_points:
        layers.append(
            pdk.Layer(
                "IconLayer",
                data=evidence_points,
                get_position="[lon, lat]",
                get_icon="icon",      # expects dict per row: {url/atlas coords}
                get_size=4,
                pickable=True,
            )
        )

    return layers
