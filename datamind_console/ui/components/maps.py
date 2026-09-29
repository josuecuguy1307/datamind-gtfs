from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union, Literal

import streamlit as st
import pandas as pd
import pydeck as pdk


MapTheme = Literal["light", "dark", "datamind"]


@dataclass(frozen=True)
class MapPoint:
    lat: float
    lon: float
    label: Optional[str] = None
    value: Optional[float] = None


@dataclass(frozen=True)
class MapPath:
    path: List[Tuple[float, float]]  # [(lat, lon), ...]
    name: Optional[str] = None
    score: Optional[float] = None


def _bounds_from_points(points: Sequence[MapPoint]) -> Optional[Tuple[float, float, float, float]]:
    if not points:
        return None
    lats = [p.lat for p in points]
    lons = [p.lon for p in points]
    return min(lats), min(lons), max(lats), max(lons)


def _bounds_from_paths(paths: Sequence[MapPath]) -> Optional[Tuple[float, float, float, float]]:
    if not paths:
        return None
    lats: List[float] = []
    lons: List[float] = []
    for p in paths:
        for lat, lon in p.path:
            lats.append(lat)
            lons.append(lon)
    if not lats or not lons:
        return None
    return min(lats), min(lons), max(lats), max(lons)


def _merge_bounds(
    a: Optional[Tuple[float, float, float, float]],
    b: Optional[Tuple[float, float, float, float]],
) -> Optional[Tuple[float, float, float, float]]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _viewstate_from_bounds(
    bounds: Tuple[float, float, float, float],
    padding_factor: float = 1.10,
) -> pdk.ViewState:
    min_lat, min_lon, max_lat, max_lon = bounds
    center_lat = (min_lat + max_lat) / 2.0
    center_lon = (min_lon + max_lon) / 2.0

    lat_span = max(1e-6, (max_lat - min_lat) * padding_factor)
    lon_span = max(1e-6, (max_lon - min_lon) * padding_factor)
    span = max(lat_span, lon_span)

    zoom = 12.5
    if span > 0.60:
        zoom = 9.5
    elif span > 0.25:
        zoom = 10.5
    elif span > 0.10:
        zoom = 11.3
    elif span > 0.05:
        zoom = 12.2
    elif span > 0.02:
        zoom = 13.0
    else:
        zoom = 13.8

    return pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=zoom, pitch=0)


def _resolve_theme(theme: MapTheme) -> MapTheme:
    if theme in ("dark", "datamind"):
        return theme
    ui_style = str(st.session_state.get("ui.style") or "")
    if ui_style == "Graphite":
        return "dark"
    return "light"


def _deck_theme(theme: MapTheme) -> str:
    t = _resolve_theme(theme)
    if t == "datamind":
        return "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"
    return "dark" if t == "dark" else "light"


def render_points_map(
    title: str,
    points: Sequence[MapPoint],
    theme: MapTheme = "light",
    radius_m: int = 55,
    height: int = 520,
) -> None:
    st.markdown(f"#### {title}")

    if not points:
        st.info("No points to display.")
        return

    df = pd.DataFrame(
        [
            {
                "lat": p.lat,
                "lon": p.lon,
                "label": p.label or "",
                "value": p.value if p.value is not None else 0.0,
            }
            for p in points
        ]
    )

    bounds = _bounds_from_points(points)
    view_state = _viewstate_from_bounds(bounds) if bounds else pdk.ViewState(latitude=0.0, longitude=0.0, zoom=2)

    resolved = _resolve_theme(theme)
    dark_visual = resolved in ("dark", "datamind")
    layer = pdk.Layer(
        "ScatterplotLayer",
        data=df,
        get_position="[lon, lat]",
        get_radius=radius_m,
        get_fill_color=([82, 204, 255, 230] if dark_visual else [44, 100, 225, 200]),
        get_line_color=([14, 20, 42, 240] if dark_visual else [255, 255, 255, 230]),
        line_width_min_pixels=1,
        stroked=True,
        pickable=True,
        auto_highlight=True,
    )

    tooltip = {"text": "{label}\nvalue: {value}"}

    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=view_state,
        tooltip=tooltip,
        map_style=_deck_theme(resolved),
    )
    st.pydeck_chart(deck, use_container_width=True, height=height)


def render_paths_map(
    title: str,
    paths: Sequence[MapPath],
    theme: MapTheme = "light",
    width_scale: int = 5,
    height: int = 560,
) -> None:
    st.markdown(f"#### {title}")

    if not paths:
        st.info("No paths to display.")
        return

    df = pd.DataFrame(
        [
            {
                "path": [[lon, lat] for lat, lon in p.path],
                "name": p.name or "",
                "score": p.score if p.score is not None else 0.0,
            }
            for p in paths
        ]
    )

    bounds = _bounds_from_paths(paths)
    view_state = _viewstate_from_bounds(bounds) if bounds else pdk.ViewState(latitude=0.0, longitude=0.0, zoom=2)

    resolved = _resolve_theme(theme)
    dark_visual = resolved in ("dark", "datamind")
    layer = pdk.Layer(
        "PathLayer",
        data=df,
        get_path="path",
        get_width=width_scale,
        get_color=([114, 224, 255, 235] if dark_visual else [250, 84, 84, 210]),
        pickable=True,
        auto_highlight=True,
    )

    tooltip = {"text": "{name}\nscore: {score}"}

    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=view_state,
        tooltip=tooltip,
        map_style=_deck_theme(resolved),
    )
    st.pydeck_chart(deck, use_container_width=True, height=height)


def render_points_and_paths(
    title: str,
    points: Sequence[MapPoint],
    paths: Sequence[MapPath],
    theme: MapTheme = "light",
    point_radius_m: int = 55,
    path_width_scale: int = 5,
    height: int = 580,
) -> None:
    st.markdown(f"#### {title}")

    if not points and not paths:
        st.info("No map data to display.")
        return

    point_df = pd.DataFrame(
        [
            {
                "lat": p.lat,
                "lon": p.lon,
                "label": p.label or "",
                "value": p.value if p.value is not None else 0.0,
            }
            for p in points
        ]
    )

    path_df = pd.DataFrame(
        [
            {
                "path": [[lon, lat] for lat, lon in p.path],
                "name": p.name or "",
                "score": p.score if p.score is not None else 0.0,
            }
            for p in paths
        ]
    )

    b1 = _bounds_from_points(points) if points else None
    b2 = _bounds_from_paths(paths) if paths else None
    bounds = _merge_bounds(b1, b2)
    view_state = _viewstate_from_bounds(bounds) if bounds else pdk.ViewState(latitude=0.0, longitude=0.0, zoom=2)

    resolved = _resolve_theme(theme)
    dark_visual = resolved in ("dark", "datamind")
    layers: List[pdk.Layer] = []

    if not path_df.empty:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=path_df,
                get_path="path",
                get_width=path_width_scale,
                get_color=([114, 224, 255, 235] if dark_visual else [250, 84, 84, 210]),
                pickable=True,
                auto_highlight=True,
            )
        )

    if not point_df.empty:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=point_df,
                get_position="[lon, lat]",
                get_radius=point_radius_m,
                get_fill_color=([82, 204, 255, 230] if dark_visual else [44, 100, 225, 200]),
                get_line_color=([14, 20, 42, 240] if dark_visual else [255, 255, 255, 230]),
                line_width_min_pixels=1,
                stroked=True,
                pickable=True,
                auto_highlight=True,
            )
        )

    tooltip = {"text": "{label}{name}\nvalue: {value}\nscore: {score}"}

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        tooltip=tooltip,
        map_style=_deck_theme(resolved),
    )
    st.pydeck_chart(deck, use_container_width=True, height=height)


def render_geojson_map(
    title: str,
    geojson: Dict[str, Any],
    theme: MapTheme = "light",
    opacity: float = 0.25,
    height: int = 560,
) -> None:
    st.markdown(f"#### {title}")

    if not geojson or "features" not in geojson:
        st.info("No GeoJSON to display.")
        return

    resolved = _resolve_theme(theme)
    dark_visual = resolved in ("dark", "datamind")
    layer = pdk.Layer(
        "GeoJsonLayer",
        data=geojson,
        pickable=True,
        auto_highlight=True,
        opacity=opacity,
        get_fill_color=([110, 172, 255, 95] if dark_visual else [80, 140, 245, 85]),
        get_line_color=([164, 224, 255, 230] if dark_visual else [235, 78, 78, 220]),
        line_width_min_pixels=2,
    )

    view_state = pdk.ViewState(latitude=0.0, longitude=0.0, zoom=11)

    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=view_state,
        map_style=_deck_theme(resolved),
    )
    st.pydeck_chart(deck, use_container_width=True, height=height)
