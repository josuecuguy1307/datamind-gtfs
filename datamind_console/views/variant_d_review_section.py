"""
TEMP quality-review section for the 10 Variant-D quito_sur routes built on
2026-04-21. Reads the driver's run_summary.json to identify the exact route
set, pulls live data from route_prod/node_prod, and renders a map + detail
table inside the main Dashboard.

Remove this file (and its import from dashboard_view.py) once the 71-shard
backfill is reviewed and closed out.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pydeck as pdk
import streamlit as st

from datamind_console.db import db_conn, fetch_all
from datamind_console.ui.components.maps import _deck_theme, _resolve_theme


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SUMMARY = (
    _PROJECT_ROOT
    / "constructor_artifacts"
    / "variant_d_quito_sur"
    / "run_summary.json"
)


def _map_theme_from_style() -> str:
    style = str(st.session_state.get("ui.style") or "Graphite")
    if style == "Graphite":
        return "dark"
    return "light"


@st.cache_data(ttl=60, show_spinner=False)
def _load_summary(path_str: str) -> List[Dict[str, Any]]:
    p = Path(path_str)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


@st.cache_data(ttl=60, show_spinner="Loading Variant-D routes…")
def _fetch_routes(route_ids: tuple[str, ...]) -> List[Dict[str, Any]]:
    if not route_ids:
        return []
    sql = """
    SELECT
      r.route_id::text AS route_id,
      COALESCE(r.route_name, r.route_id::text) AS route_name,
      r.source,
      COALESCE(array_length(r.stop_node_ids, 1), 0) AS stop_count,
      (ST_Length(r.geom::geography) / 1000.0)::numeric(8,2) AS km,
      r.naming_confidence,
      r.direction_semantics,
      r.created_at,
      ST_AsGeoJSON(r.geom)::json -> 'coordinates' AS coords,
      ST_GeometryType(r.geom) AS gtype
    FROM route_prod.routes r
    WHERE r.route_id::text = ANY(%(ids)s)
    ORDER BY r.created_at
    """
    try:
        with db_conn(readonly=True) as conn:
            return fetch_all(conn, sql, {"ids": list(route_ids)}) or []
    except Exception:
        return []


@st.cache_data(ttl=60, show_spinner="Loading stops…")
def _fetch_stops(route_ids: tuple[str, ...]) -> List[Dict[str, Any]]:
    if not route_ids:
        return []
    sql = """
    SELECT
      r.route_id::text AS route_id,
      r.route_name,
      n.node_id::text AS stop_id,
      COALESCE(NULLIF(n.name, ''), 'Stop ' || LEFT(n.node_id::text, 8)) AS stop_name,
      ST_Y(n.geom) AS lat,
      ST_X(n.geom) AS lon,
      pos.ord AS ord
    FROM route_prod.routes r
    JOIN LATERAL unnest(r.stop_node_ids) WITH ORDINALITY AS pos(node_id, ord) ON true
    JOIN node_prod.nodes n ON n.node_id = pos.node_id
    WHERE r.route_id::text = ANY(%(ids)s)
    ORDER BY r.route_id, pos.ord
    """
    try:
        with db_conn(readonly=True) as conn:
            return fetch_all(conn, sql, {"ids": list(route_ids)}) or []
    except Exception:
        return []


def _coords_to_path(coords, gtype: str):
    if not coords:
        return []
    if gtype and "Multi" in gtype:
        merged = []
        for seg in coords:
            if isinstance(seg, list):
                merged.extend(seg)
        return merged
    return coords


_PALETTE = [
    [231, 76, 60, 220],   # red
    [52, 152, 219, 220],  # blue
    [46, 204, 113, 220],  # green
    [241, 196, 15, 220],  # yellow
    [155, 89, 182, 220],  # purple
    [26, 188, 156, 220],  # teal
    [230, 126, 34, 220],  # orange
    [236, 64, 122, 220],  # pink
    [127, 140, 141, 220], # grey
    [149, 165, 166, 220], # slate
]


def render_variant_d_review_section(summary_path: str | Path = _DEFAULT_SUMMARY) -> None:
    st.divider()
    st.markdown("### TEMP: Variant-D quality review — quito_sur (10 routes)")
    st.caption(
        "Smoke batch built 2026-04-21 from workspace/catalogs/seed/*.json "
        "(Variant-D shards). Remove this section once the 71-shard backfill is signed off."
    )

    summary_rows = _load_summary(str(summary_path))
    if not summary_rows:
        st.warning(f"No run_summary.json at `{summary_path}`. Run the driver first.")
        return

    built = [r for r in summary_rows if r.get("status") == "built"]
    route_ids = tuple(r["route_id"] for r in built if r.get("route_id"))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Shards processed", len(summary_rows))
    c2.metric("Built", len(built))
    c3.metric(
        "Blocked / failed",
        len(summary_rows) - len(built),
    )
    c4.metric(
        "Synth events",
        sum(int(r.get("synthesis_events") or 0) for r in summary_rows),
    )

    if not route_ids:
        st.info("No successfully built rows to review.")
        return

    routes = _fetch_routes(route_ids)
    if not routes:
        st.error(
            "Route IDs found in summary but not in route_prod.routes. "
            "DB mode may be on server — switch to local."
        )
        return

    stops = _fetch_stops(route_ids)

    # Detail table ---------------------------------------------------
    rows_df = []
    for r in routes:
        semantics = r.get("direction_semantics") or {}
        if isinstance(semantics, str):
            try:
                semantics = json.loads(semantics)
            except Exception:
                semantics = {}
        cooperative = semantics.get("cooperative") or ""
        conf = r.get("naming_confidence")
        flags: list[str] = []
        if conf is not None and float(conf) < 0.7:
            flags.append("low_conf")
        if (r.get("km") or 0) and float(r["km"]) > 50:
            flags.append("long")
        if (r.get("stop_count") or 0) > 25:
            flags.append("many_stops")
        rows_df.append(
            {
                "route_name": r.get("route_name"),
                "cooperative": cooperative,
                "stops": int(r.get("stop_count") or 0),
                "km": float(r.get("km") or 0),
                "naming_conf": (float(conf) if conf is not None else None),
                "flags": ", ".join(flags) or "-",
                "route_id": r.get("route_id"),
            }
        )
    df = pd.DataFrame(rows_df)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "naming_conf": st.column_config.NumberColumn(format="%.3f"),
            "km": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    # Route picker + map --------------------------------------------
    name_options = ["All 10 routes"] + [r["route_name"] for r in rows_df]
    pick = st.selectbox(
        "Focus route", name_options, index=0, key="variant_d_review.pick"
    )

    route_color = {}
    for i, r in enumerate(routes):
        route_color[r["route_id"]] = _PALETTE[i % len(_PALETTE)]

    if pick == "All 10 routes":
        selected_routes = routes
        selected_stops = stops
    else:
        selected_routes = [r for r in routes if r["route_name"] == pick]
        sel_ids = {r["route_id"] for r in selected_routes}
        selected_stops = [s for s in stops if s["route_id"] in sel_ids]

    path_data = []
    for r in selected_routes:
        path = _coords_to_path(r.get("coords"), r.get("gtype") or "")
        if len(path) < 2:
            continue
        path_data.append(
            {
                "path": path,
                "name": r.get("route_name"),
                "stops": int(r.get("stop_count") or 0),
                "km": float(r.get("km") or 0),
                "color": route_color.get(r["route_id"], [200, 200, 200, 220]),
            }
        )

    if not path_data and not selected_stops:
        st.info("Nothing to map for this selection.")
        return

    all_lats: list[float] = []
    all_lons: list[float] = []
    for p in path_data:
        for c in p["path"]:
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                all_lons.append(float(c[0]))
                all_lats.append(float(c[1]))
    for s in selected_stops:
        if s.get("lat") is not None and s.get("lon") is not None:
            all_lats.append(float(s["lat"]))
            all_lons.append(float(s["lon"]))

    if all_lats and all_lons:
        center_lat = (min(all_lats) + max(all_lats)) / 2
        center_lon = (min(all_lons) + max(all_lons)) / 2
        span = max(
            max(all_lats) - min(all_lats),
            max(all_lons) - min(all_lons),
            1e-6,
        )
        zoom = (
            10.0 if span > 0.6
            else 11.0 if span > 0.25
            else 12.0 if span > 0.1
            else 13.0 if span > 0.05
            else 13.8
        )
    else:
        center_lat, center_lon, zoom = -0.26, -78.52, 12

    resolved = _resolve_theme(_map_theme_from_style())
    dark_visual = resolved in ("dark", "datamind")

    layers = [
        pdk.Layer(
            "PathLayer",
            data=pd.DataFrame(path_data) if path_data else [],
            get_path="path",
            get_width=5,
            get_color="color",
            pickable=True,
            auto_highlight=True,
            width_min_pixels=2,
        )
    ]

    if selected_stops:
        stop_df = pd.DataFrame(
            [
                {
                    "lat": float(s["lat"]),
                    "lon": float(s["lon"]),
                    "stop_name": s.get("stop_name", ""),
                    "route_name": s.get("route_name", ""),
                    "ord": int(s.get("ord") or 0),
                }
                for s in selected_stops
                if s.get("lat") is not None and s.get("lon") is not None
            ]
        )
        if not stop_df.empty:
            layers.append(
                pdk.Layer(
                    "ScatterplotLayer",
                    data=stop_df,
                    get_position="[lon, lat]",
                    get_radius=42,
                    get_fill_color=(
                        [255, 210, 60, 230] if dark_visual else [220, 120, 20, 220]
                    ),
                    get_line_color=(
                        [14, 20, 42, 240] if dark_visual else [255, 255, 255, 230]
                    ),
                    line_width_min_pixels=1,
                    stroked=True,
                    pickable=True,
                    auto_highlight=True,
                )
            )

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(
            latitude=center_lat, longitude=center_lon, zoom=zoom, pitch=0
        ),
        tooltip={
            "text": "{name}{stop_name}\n{route_name} | ord {ord} | {stops} stops | {km} km"
        },
        map_style=_deck_theme(resolved),
    )
    st.pydeck_chart(deck, use_container_width=True, height=560)

    # Artifact hint --------------------------------------------------
    st.caption(
        f"Artifacts: `constructor_artifacts/variant_d_quito_sur/<ROUTE_CODE>/` · "
        f"Summary: `{summary_path}`"
    )
