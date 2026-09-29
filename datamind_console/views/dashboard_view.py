from __future__ import annotations

from typing import Any
import streamlit as st

from datamind_console.ops.config import load_ops_config
from datamind_console.ops.local_api_status import run_all_local_server_api_checks
from datamind_console.ui.layout import render_app_header
from datamind_console.ui.components.maps import render_points_map, _resolve_theme, _deck_theme
from datamind_console.services.workspace_context_service import (
    SAMPLE_REGION_RESOURCE,
    active_work,
    create_work,
    ensure_workspace_context,
    select_work,
    start_new_session,
    transient_form_is_dirty,
)
from datamind_core.province_config import (
    DEFAULT_PROVINCE,
    get_canton_color,
    get_canton_colors,
    list_active_provinces,
)

import pandas as pd
import pydeck as pdk


def _map_theme_from_style() -> str:
    style = str(st.session_state.get("ui.style") or "Graphite")
    if style == "Graphite":
        return "dark"
    return "light"


def _init_dashboard_state() -> None:
    ss = st.session_state
    ss.setdefault("dashboard.local_api_status", {})
    ss.setdefault("dashboard.local_api_checked_at", "")


@st.cache_data(ttl=120, show_spinner=False)
def _cached_dashboard_kpis(_analytics: Any) -> dict:
    return {
        "routes": _analytics.count_routes(),
        "phase1_stop_nodes": _analytics.count_stops(),
        "phase2_places": _analytics.count_places(),
        "phase2_stop_places": _analytics.count_places(place_type="STOP"),
        "phase2_node_mappings": _analytics.count_node_place_mappings(),
        "pending": _analytics.count_pending_reviews(),
        "models": _analytics.count_models(),
    }


@st.cache_data(ttl=120, show_spinner=False)
def _cached_phase2_stop_points(_analytics: Any) -> Any:
    return _analytics.get_phase2_stop_points()


_DEFAULT_CANTON_COLOR = [114, 224, 255, 200]


def _known_canton_keys(province: str | None = None) -> list[str]:
    """Return canton keys for ``province`` (or all active provinces if None).

    Longest-first so substring matches (e.g. ``quito_norte`` before ``quito``)
    prefer the most specific canton.
    """
    if province:
        keys = list(get_canton_colors(province).keys())
    else:
        keys = []
        for p in list_active_provinces() or [DEFAULT_PROVINCE]:
            keys.extend(get_canton_colors(p).keys())
    # Dedup preserving order, then sort longest-first for substring matching.
    seen: set[str] = set()
    unique: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    unique.sort(key=len, reverse=True)
    return unique


def _canton_color(canton: str, province: str | None = None) -> list[int]:
    if not canton or canton == "other":
        return list(_DEFAULT_CANTON_COLOR)
    return get_canton_color(province or DEFAULT_PROVINCE, canton)


def _canton_from_source(source: str, province: str | None = None) -> str:
    s = (source or "").lower()
    for canton in _known_canton_keys(province):
        if canton in s:
            return canton
    return "other"


def _coords_to_path(coords, gtype: str):
    """Convert PostGIS coordinates JSON to [[lon, lat], ...] for pydeck."""
    if not coords:
        return []
    if gtype and "Multi" in gtype:
        merged = []
        for seg in coords:
            if isinstance(seg, list):
                merged.extend(seg)
        return merged
    return coords


@st.cache_data(ttl=180, show_spinner="Loading route geometries...")
def _cached_route_geometries(_analytics: Any, source_filter: str) -> list:
    return _analytics.get_all_route_geometries(source_filter=source_filter or None)


@st.cache_data(ttl=180, show_spinner="Loading stops...")
def _cached_route_stops(_analytics: Any, source_filter: str, route_name: str) -> list:
    return _analytics.get_route_stops_for_map(
        source_filter=source_filter or None,
        route_name=route_name or None,
    )


def _render_route_geometry_overview(analytics: Any) -> None:
    st.markdown("#### Route Geometries Overview")

    # Load all routes once (cached)
    all_rows = _cached_route_geometries(analytics, "")

    if not all_rows:
        st.info("No route geometries found.")
        return

    # Build path_data with canton labels
    all_path_data = []
    for r in all_rows:
        coords = r.get("coords")
        gtype = r.get("gtype") or ""
        path = _coords_to_path(coords, gtype)
        if not path or len(path) < 2:
            continue
        canton = _canton_from_source(r.get("source", ""))
        color = _canton_color(canton)
        all_path_data.append({
            "path": path,
            "name": r.get("route_name", ""),
            "route_id": r.get("route_id", ""),
            "canton": canton,
            "stops": int(r.get("stop_count") or 0),
            "km": float(r.get("km") or 0),
            "color": color,
        })

    if not all_path_data:
        st.info("No renderable geometries.")
        return

    # Province filter — dynamically populated from supported_provinces.json
    active_provinces = list_active_provinces() or [DEFAULT_PROVINCE]
    province_options = ["All"] + [p for p in active_provinces]

    col_province, col_canton, col_route = st.columns([0.25, 0.30, 0.45])

    with col_province:
        chosen_province = st.selectbox(
            "Province", province_options, index=0, key="dashboard.route_geom_province"
        )

    # Filter path data by province (using canton keys known for that province).
    if chosen_province == "All":
        province_data = all_path_data
    else:
        province_canton_keys = set(get_canton_colors(chosen_province).keys())
        province_data = [p for p in all_path_data if p["canton"] in province_canton_keys]

    # Canton filter scoped to the chosen province
    canton_set = sorted({p["canton"] for p in province_data})
    cantons = ["All"] + canton_set

    with col_canton:
        chosen_canton = st.selectbox("Canton", cantons, index=0, key="dashboard.route_geom_canton")

    # Filter by canton (within the chosen province scope)
    if chosen_canton == "All":
        canton_data = province_data
    else:
        canton_data = [p for p in province_data if p["canton"] == chosen_canton]

    # Route name filter
    route_names = sorted({p["name"] for p in canton_data if p["name"]})
    route_options = ["All routes"] + route_names

    with col_route:
        chosen_route = st.selectbox("Route", route_options, index=0, key="dashboard.route_geom_route")

    if chosen_route != "All routes":
        path_data = [p for p in canton_data if p["name"] == chosen_route]
    else:
        path_data = canton_data

    if not path_data:
        st.info("No routes match the selection.")
        return

    st.caption(f"{len(path_data)} route(s)")

    # Fetch stops for the current selection
    src_filter = "" if chosen_canton == "All" else chosen_canton
    rname_filter = "" if chosen_route == "All routes" else chosen_route
    stop_rows = _cached_route_stops(analytics, src_filter, rname_filter)

    df = pd.DataFrame(path_data)

    # Compute bounds from routes + stops
    all_lats = []
    all_lons = []
    for p in path_data:
        for coord in p["path"]:
            if isinstance(coord, (list, tuple)) and len(coord) >= 2:
                all_lons.append(float(coord[0]))
                all_lats.append(float(coord[1]))
    for s in stop_rows:
        if s.get("lat") is not None and s.get("lon") is not None:
            all_lats.append(float(s["lat"]))
            all_lons.append(float(s["lon"]))

    if all_lats and all_lons:
        center_lat = (min(all_lats) + max(all_lats)) / 2
        center_lon = (min(all_lons) + max(all_lons)) / 2
        lat_span = max(all_lats) - min(all_lats)
        lon_span = max(all_lons) - min(all_lons)
        span = max(lat_span, lon_span, 1e-6)
        zoom = 9.5 if span > 0.6 else 10.5 if span > 0.25 else 11.3 if span > 0.1 else 12.2 if span > 0.05 else 13.0
    else:
        center_lat, center_lon, zoom = -0.18, -78.48, 11

    view = pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=zoom, pitch=0)

    theme = _map_theme_from_style()
    resolved = _resolve_theme(theme)
    dark_visual = resolved in ("dark", "datamind")

    layers = []

    # Route lines
    layers.append(pdk.Layer(
        "PathLayer",
        data=df,
        get_path="path",
        get_width=4,
        get_color="color",
        pickable=True,
        auto_highlight=True,
        width_min_pixels=2,
    ))

    # Stop points
    if stop_rows:
        stop_df = pd.DataFrame([
            {
                "lat": float(s["lat"]),
                "lon": float(s["lon"]),
                "stop_name": s.get("stop_name", ""),
                "route_name": s.get("route_name", ""),
            }
            for s in stop_rows
            if s.get("lat") is not None and s.get("lon") is not None
        ])
        if not stop_df.empty:
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                data=stop_df,
                get_position="[lon, lat]",
                get_radius=45,
                get_fill_color=([255, 200, 50, 230] if dark_visual else [220, 120, 20, 210]),
                get_line_color=([14, 20, 42, 240] if dark_visual else [255, 255, 255, 230]),
                line_width_min_pixels=1,
                stroked=True,
                pickable=True,
                auto_highlight=True,
            ))
            st.caption(f"{len(stop_df)} stops")

    tooltip = {"text": "{name}{stop_name}\n{canton}{route_name} | {stops} stops | {km} km"}

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view,
        tooltip=tooltip,
        map_style=_deck_theme(resolved),
    )
    st.pydeck_chart(deck, use_container_width=True, height=620)

    # Canton breakdown table
    canton_counts = {}
    for p in path_data:
        c = p["canton"]
        canton_counts[c] = canton_counts.get(c, 0) + 1
    breakdown = pd.DataFrame(
        [{"Canton": k, "Routes": v} for k, v in sorted(canton_counts.items(), key=lambda x: -x[1])]
    )
    st.dataframe(breakdown, use_container_width=True, hide_index=True)


def render_dashboard_view(*, analytics: Any, audit: Any) -> None:
    """
    Dashboard CONTENT ONLY.

    IMPORTANT:
    - app.py handles: set_page_config, auth, sidebar routing, and service creation.
    - This view only renders the dashboard UI.
    """
    user = st.session_state.get("auth.user") or {}
    _init_dashboard_state()
    ensure_workspace_context(st.session_state)

    # Header + top-right refresh
    h1, h2 = st.columns([0.88, 0.12])
    with h1:
        render_app_header(user)
    with h2:
        st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)
        if st.button("Refresh", use_container_width=True, key="dashboard.refresh"):
            st.rerun()

    st.markdown(
        """
        <div style="
          border:1px solid #4b535b;
          border-radius:14px;
          padding:14px 16px;
          background:linear-gradient(120deg, #252a2f 0%, #171a1d 100%);
          color:#f5f6f7;
        ">
          <div style="font-size:18px; font-weight:700;">ML DATAMIND GTFS</div>
          <div style="opacity:0.82; margin-top:3px; font-size:13px;">
            Workspace for GTFS, route, stop, and quality operations.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("#### Start here")
    st.caption("Start with a GTFS package, a phase workspace, or an explicitly selected work. This session has no region, map query, GTFS build, or historic result selected by default.")
    start_gtfs, continue_phase, select_existing, ai_help = st.columns(4)
    with start_gtfs:
        if st.button("Import or validate GTFS", type="primary", use_container_width=True, key="dashboard.start.gtfs"):
            st.session_state["ui.page"] = "GTFS Ops"
            st.rerun()
    with continue_phase:
        if st.button("Continue phase workspace", use_container_width=True, key="dashboard.start.phases"):
            st.session_state["ui.page"] = "Phases"
            st.rerun()
    with select_existing:
        if st.button("Select existing work", use_container_width=True, key="dashboard.start.work"):
            st.session_state["workspace.show_work_setup"] = True
            st.rerun()
    with ai_help:
        if st.button("Open AI assistance", use_container_width=True, key="dashboard.start.ai"):
            st.session_state["ui.page"] = "AI Assistance"
            st.rerun()
    st.markdown("#### Work context")
    work = active_work(st.session_state)
    if st.session_state.get("workspace.show_work_setup") or work:
        with st.expander("Create or select a work", expanded=not bool(work)):
            works = st.session_state.get("workspace.works") or {}
            if works:
                labels = {wid: str(item.get("name") or wid) for wid, item in works.items()}
                chosen = st.selectbox("Existing work", options=[""] + list(labels), format_func=lambda wid: "Choose a work" if not wid else labels[wid], key="workspace.work_picker")
                if chosen and chosen != st.session_state.get("workspace.active_work_id"):
                    select_work(st.session_state, chosen)
                    st.rerun()
            else:
                st.caption("No work is active in this browser session. Historic data is intentionally not loaded here.")

            with st.form("workspace.create_work", clear_on_submit=True):
                name = st.text_input("Work name", placeholder="Example: GTFS validation — new operator")
                input_description = st.text_input("Input (optional)", placeholder="GTFS ZIP, route set, or source description")
                results_destination = st.text_input("Results destination (optional)", placeholder="Local folder or artifact reference")
                region = st.text_input("Region / coverage (optional)", placeholder="No profile is required for GTFS validation")
                resource = st.selectbox(
                    "Regional resource (optional)",
                    options=["", SAMPLE_REGION_RESOURCE["id"]],
                    format_func=lambda value: "No regional resource" if not value else SAMPLE_REGION_RESOURCE["name"],
                )
                submitted = st.form_submit_button("Create and select work", type="primary")
            if submitted:
                create_work(st.session_state, name=name, input_description=input_description, results_destination=results_destination, region=region, resource_id=resource)
                st.session_state["workspace.show_work_setup"] = False
                st.rerun()

    work = active_work(st.session_state)
    if work:
        st.success(f"Active work: {work['name']}")
        st.caption(" · ".join([
            f"Input: {work.get('input') or 'not selected'}",
            f"Results: {work.get('results_destination') or 'not selected'}",
            f"Region: {work.get('region') or 'not selected'}",
        ]))
        if work.get("resource_id") == SAMPLE_REGION_RESOURCE["id"]:
            st.info(SAMPLE_REGION_RESOURCE["name"])
            st.caption(f"Coverage: {SAMPLE_REGION_RESOURCE['coverage']} Operation: {SAMPLE_REGION_RESOURCE['operations']} Path: `{SAMPLE_REGION_RESOURCE['path']}`")
    else:
        st.info("Neutral session: select or create work only when an operation needs it. GTFS ZIP validation can begin without a regional profile.")

    dirty = transient_form_is_dirty(st.session_state)
    if dirty:
        st.warning("A transient form or file selection may be discarded. Running tasks and terminals will not be stopped.")
    confirm = st.checkbox("I understand that New session clears only active selections and transient form/file references.", key="workspace.new_session.confirm")
    def _request_new_session() -> None:
        # Streamlit executes widget callbacks before its next page render, so
        # no instantiated-widget key is changed during rendering.
        start_new_session(st.session_state)

    st.button(
        "New session",
        key="workspace.new_session",
        disabled=not confirm,
        on_click=_request_new_session,
    )

    st.caption("Maps, production counts, and legacy Variant-D review are not queried from the neutral dashboard. Open a specific operation only after selecting its work and scope.")
