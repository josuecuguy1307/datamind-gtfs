from __future__ import annotations

from typing import Any, Dict, List
import pydeck as pdk


def build_phase1_layers(
    *,
    all_points: List[Dict[str, Any]],       # [{lon, lat, ...}]
    selected_points: List[Dict[str, Any]],  # subset for “approved/selected”
    show_labels: bool = False,
    top_n_labels: int = 30,
) -> List[pdk.Layer]:
    layers: List[pdk.Layer] = []

    # Density hills (best for “where are real hubs?”)
    layers.append(
        pdk.Layer(
            "HexagonLayer",
            data=all_points,
            get_position="[lon, lat]",
            radius=40,
            elevation_scale=12,
            extruded=True,
            pickable=True,
        )
    )

    # Show only selected/approved points as crisp dots
    if selected_points:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=selected_points,
                get_position="[lon, lat]",
                get_radius=18,
                pickable=True,
            )
        )

    # Optional labels (top N only)
    if show_labels and selected_points:
        labeled = selected_points[:top_n_labels]
        layers.append(
            pdk.Layer(
                "TextLayer",
                data=labeled,
                get_position="[lon, lat]",
                get_text="label",
                get_size=14,
                pickable=False,
            )
        )

    return layers
