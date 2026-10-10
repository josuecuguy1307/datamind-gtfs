# views/phases/phase3_view.py
"""Phase 3 — Route Constructor (Monitoring)"""
from __future__ import annotations

from typing import Any, Optional
import uuid

import streamlit as st
import pandas as pd
import pydeck as pdk

from datamind_console.phases.phase3_routes.client import _get_phase3_client
from datamind_console.db import console_repo
from datamind_console.views.phases.panels import call_panel as _call, load_panel as _load
from datamind_console.i18n import t
from datamind_console.ui.components.page import section
from datamind_console.ui.components.states import empty_state, error_state
from datamind_console.ui.formatting import format_date

_MAP_STYLES = {
    "Colorful (Voyager)": "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
    "Light (Positron)": "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    "Dark (Matter)": "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
}


def _current_map_style() -> str:
    name = st.session_state.get("p3.map_style_name", "Colorful (Voyager)")
    return _MAP_STYLES.get(name, _MAP_STYLES["Colorful (Voyager)"])


@st.cache_data(ttl=1, show_spinner=False)
def _cached_service_route_gate(service_route_id: str) -> dict:
    c = _get_phase3_client()
    try:
        return c.get_service_route_gate(service_route_id=service_route_id) or {}
    except Exception:
        return {}


@st.cache_data(ttl=1, show_spinner=False)
def _cached_list_relation_raw_ids(route_id: str) -> list:
    c = _get_phase3_client()
    try:
        return c.list_relation_raw_ids(uuid.UUID(route_id)) or []
    except Exception:
        return []


@st.cache_data(ttl=1, show_spinner=False)
def _cached_get_relation_geometry(route_id: str, osm_relation_id: int) -> dict:
    c = _get_phase3_client()
    try:
        return c.get_relation_geometry(uuid.UUID(route_id), osm_relation_id=int(osm_relation_id)) or {}
    except Exception:
        return {}


@st.cache_data(ttl=1, show_spinner=False)
def _cached_list_geometry_sets(route_id: str) -> list:
    c = _get_phase3_client()
    try:
        return c.list_geometry_sets(uuid.UUID(route_id)) or []
    except Exception:
        return []


@st.cache_data(ttl=1, show_spinner=False)
def _cached_get_geometry_candidates(route_id: str, geometry_set_id: str) -> dict:
    c = _get_phase3_client()
    try:
        return c.get_geometry_candidates(uuid.UUID(route_id), geometry_set_id=uuid.UUID(geometry_set_id)) or {}
    except Exception:
        return {}


# ------------------------------------------------------------
# Safe caller (keyword-only ctx, filtered deps)
# ------------------------------------------------------------
def _short_id(v: Any) -> str:
    s = str(v or "").strip()
    if not s:
        return "-"
    return s if len(s) <= 12 else f"{s[:8]}..{s[-4:]}"


def _coords_from_feature_geojson(payload: Optional[dict]) -> list[tuple[float, float]]:
    if not payload:
        return []
    geom = payload.get("geometry") if isinstance(payload, dict) else None
    if not geom:
        return []
    gtype = str(geom.get("type") or "").lower()
    out: list[tuple[float, float]] = []
    if gtype == "linestring":
        items = geom.get("coordinates") or []
        for xy in items:
            if not isinstance(xy, (list, tuple)) or len(xy) < 2:
                continue
            try:
                out.append((float(xy[0]), float(xy[1])))
            except Exception:
                continue
    elif gtype == "multilinestring":
        lines = geom.get("coordinates") or []
        for line in lines:
            if not isinstance(line, (list, tuple)):
                continue
            for xy in line:
                if not isinstance(xy, (list, tuple)) or len(xy) < 2:
                    continue
                try:
                    out.append((float(xy[0]), float(xy[1])))
                except Exception:
                    continue
    return out


def _render_workspace_polyline(title: str, coords: list[tuple[float, float]]) -> None:
    st.caption(title)
    if len(coords) < 2:
        st.info(t("ui.p3.phase3.no_line_geometry_available_this_selection"))
        return

    mid = coords[len(coords) // 2]
    path_data = [{"name": title, "path": [[lon, lat] for lon, lat in coords]}]
    sampled = coords[:: max(1, len(coords) // 60)]
    points_df = pd.DataFrame([{"lon": lon, "lat": lat} for lon, lat in sampled])

    st.pydeck_chart(
        pdk.Deck(
            map_style=_current_map_style(),
            initial_view_state=pdk.ViewState(
                latitude=float(mid[1]),
                longitude=float(mid[0]),
                zoom=12,
                pitch=0,
            ),
            layers=[
                pdk.Layer(
                    "PathLayer",
                    data=path_data,
                    get_path="path",
                    get_width=5,
                    width_min_pixels=2,
                    get_color=[255, 90, 95],
                ),
                pdk.Layer(
                    "ScatterplotLayer",
                    data=points_df,
                    get_position="[lon, lat]",
                    get_radius=24,
                    radius_min_pixels=2,
                    get_fill_color=[255, 180, 0, 170],
                ),
            ],
            tooltip={"text": "{name}"},
        ),
        use_container_width=True,
    )


# ------------------------------------------------------------
# Phase 3 session state
# ------------------------------------------------------------
def _init_phase3_state() -> None:
    ss = st.session_state
    ss.setdefault("p3.service_route_id", None)
    ss.setdefault("p3.direction_id", 0)
    ss.setdefault("p3.route_id", None)
    ss.setdefault("p3.osm_relation_id", None)
    ss.setdefault("p3.active_geometry_set_id", None)
    ss.setdefault("p3.active_geometry_candidate_id", None)
    ss.setdefault("p3.geometry_candidate_id", None)
    ss.setdefault("p3.map_style_name", "Colorful (Voyager)")
    ss.setdefault("p3._ctx_route_id", None)


# ------------------------------------------------------------
# Phase 3 view
# ------------------------------------------------------------
# Phase 3 workspaces: (id used in ?tab= deep links, module, render function).
PHASE3_TABS = (
    ("coverage", "views.phases.phase3.tabs.coverage_review_tab", "render_coverage_review_tab"),
    ("grounding_catalog", "views.phases.phase3.tabs.grounding_catalog_tab", "render_grounding_catalog_tab"),
    ("stop_grounding_perf", "views.phases.phase3.tabs.stop_grounding_perf_tab", "render_stop_grounding_perf_tab"),
    ("sequences_review", "views.phases.phase3.tabs.sequences_review_tab", "render_sequences_review_tab"),
    ("re_entry", "views.phases.phase3.tabs.re_entry_review_tab", "render_re_entry_review_tab"),
    ("coverage_improvement", "views.phases.phase3.tabs.coverage_improvement_tab", "render_coverage_improvement_tab"),
    ("greek_validated_catalog", "views.phases.phase3.tabs.greek_validated_catalog_tab", "render_greek_validated_catalog_tab"),
    # Was registered but missing from the selector options, so it was unreachable.
    ("greek_ship_overview", "views.phases.phase3.tabs.greek_ship_overview_tab", "render_greek_ship_overview_tab"),
)
PHASE3_TAB_IDS = tuple(tab_id for tab_id, _, _ in PHASE3_TABS)
_MAP_MODES = ("after_step_30", "before_step_30")


def _current_tab_id() -> str:
    ss = st.session_state
    requested = st.query_params.get("tab")
    if requested in PHASE3_TAB_IDS:
        ss["p3.active_tab_id"] = requested
    if ss.get("p3.active_tab_id") not in PHASE3_TAB_IDS:
        ss["p3.active_tab_id"] = "coverage"
    return str(ss["p3.active_tab_id"])


def _workspace_map(route_id: str, *, ctx: Any, deps: dict[str, Any]) -> None:
    ss = st.session_state
    with st.expander(t("phase3.map.title"), icon=":material/map:", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            st.selectbox(t("phase3.map.basemap"), options=list(_MAP_STYLES.keys()), key="p3.map_style_name")
        with c2:
            map_mode = st.selectbox(
                t("phase3.map.mode"),
                options=list(_MAP_MODES),
                format_func=lambda m: t(f"phase3.map.mode.{m}"),
                key="p3.workspace_map_mode_id",
            )

        if map_mode == "before_step_30":
            rel_ids = _cached_list_relation_raw_ids(str(route_id))
            if not rel_ids:
                empty_state("phase3.map.no_relation")
                return
            cur_rel = ss.get("p3.osm_relation_id")
            if cur_rel not in rel_ids:
                cur_rel = rel_ids[0]
            selected_rel = st.selectbox(
                t("phase3.map.osm_relation"),
                options=rel_ids,
                index=rel_ids.index(cur_rel) if cur_rel in rel_ids else 0,
                key=f"p3.workspace_rel_pick.{route_id}",
            )
            ss["p3.osm_relation_id"] = int(selected_rel)
            rel_geo = _cached_get_relation_geometry(str(route_id), int(selected_rel))
            rel_coords = _coords_from_feature_geojson(rel_geo.get("geojson"))
            _render_workspace_polyline(t("phase3.map.mode.before_step_30"), rel_coords)
            return

        set_rows = _cached_list_geometry_sets(str(route_id))
        set_rows = [r for r in set_rows if int(r.get("n_candidates") or 0) > 0]
        if not set_rows:
            empty_state("phase3.map.no_geometry")
            return
        set_ids = [str(r.get("set_id")) for r in set_rows if r.get("set_id")]
        current_sid = str(ss.get("p3.active_geometry_set_id") or "")
        if current_sid not in set_ids:
            current_sid = set_ids[0]
        label_map = {
            str(r.get("set_id")): t(
                "phase3.map.geometry_set_label",
                short=_short_id(r.get("set_id")),
                n=int(r.get("n_candidates") or 0),
                created=format_date(r.get("created_at"), with_time=True),
            )
            for r in set_rows
            if r.get("set_id")
        }
        g1, g2 = st.columns(2)
        with g1:
            selected_sid = st.selectbox(
                t("phase3.map.geometry_set"),
                options=set_ids,
                index=set_ids.index(current_sid) if current_sid in set_ids else 0,
                format_func=lambda x: label_map.get(x, x),
                key=f"p3.workspace_set_pick.{route_id}",
            )
        ss["p3.active_geometry_set_id"] = selected_sid

        c_payload = _cached_get_geometry_candidates(str(route_id), str(selected_sid))
        cands = c_payload.get("candidates", []) if isinstance(c_payload, dict) else []
        cand_ids = [str(c.get("geometry_candidate_id")) for c in cands if c.get("geometry_candidate_id")]
        if not cand_ids:
            empty_state("phase3.map.empty_set")
            return
        cur_cid = str(ss.get("p3.active_geometry_candidate_id") or ss.get("p3.geometry_candidate_id") or "")
        if cur_cid not in cand_ids:
            cur_cid = cand_ids[0]
        with g2:
            selected_cid = st.selectbox(
                t("phase3.map.geometry_candidate"),
                options=cand_ids,
                index=cand_ids.index(cur_cid) if cur_cid in cand_ids else 0,
                format_func=_short_id,
                key=f"p3.workspace_geom_pick.{route_id}.{selected_sid}",
            )
        ss["p3.active_geometry_candidate_id"] = selected_cid
        ss["p3.geometry_candidate_id"] = selected_cid

        w_geom_map = _load("views.phases.phase3.widgets.route_geometry_map", "render_route_geometry_map")
        _call(w_geom_map, ctx=ctx, route_id=route_id, geometry_candidate_id=selected_cid, **deps)


def render_phase3_view(*, ctx: Any, analytics: Any = None, audit: Any = None, **_) -> None:
    _init_phase3_state()

    try:
        client = _get_phase3_client()
    except RuntimeError as e:
        error_state(e, context_key="error.page.phase_render", phase=3, retry=False)
        return
    console = console_repo
    ss = st.session_state

    # Route context guard
    current_route_ctx = str(ss.get("p3.route_id") or "").strip() or None
    if current_route_ctx != (ss.get("p3._ctx_route_id") or None):
        ss["p3._ctx_route_id"] = current_route_ctx
        ss["p3.active_geometry_set_id"] = None
        ss["p3.active_geometry_candidate_id"] = None
        ss["p3.geometry_candidate_id"] = None

    deps = {
        "analytics": analytics,
        "audit": audit,
        "client": client,
        "console": console,
    }

    # --- Route queue (logical routes and directions) ---
    section(t("phase3.queue.title"), t("phase3.queue.caption"))
    w_queue = _load("views.phases.phase3.widgets.route_queue_table", "render_route_queue_table")
    picked_route = _call(w_queue, ctx=ctx, **deps)
    if isinstance(picked_route, str) and picked_route.strip():
        ss["p3.route_id"] = picked_route.strip()
        if ss.get("p3.geometry_candidate_id"):
            ss["p3.geometry_candidate_id"] = None

    route_id = ss.get("p3.route_id")
    if ss.get("p3.service_route_id") is not None:
        st.caption(
            t(
                "phase3.route_context",
                service_route=_short_id(ss.get("p3.service_route_id")),
                direction=int(ss.get("p3.direction_id") or 0),
            )
        )
    if route_id:
        _workspace_map(str(route_id), ctx=ctx, deps=deps)

    # --- Workspaces (do not depend on the selected route) ---
    section(t("phase3.tabs.title"), t("phase3.tabs.caption"), divider=True)
    current_tab = _current_tab_id()
    picked_tab = st.segmented_control(
        t("phase3.tabs.title"),
        options=list(PHASE3_TAB_IDS),
        default=current_tab,
        format_func=lambda tab_id: t(f"phase3.tab.{tab_id}"),
        key="p3.tab_picker",
        label_visibility="collapsed",
    )
    if picked_tab and picked_tab != current_tab:
        ss["p3.active_tab_id"] = picked_tab
        st.query_params["tab"] = picked_tab
        st.rerun()
    st.caption(t(f"phase3.tab.{current_tab}.description"))
    module, fn_name = next((m, f) for tab_id, m, f in PHASE3_TABS if tab_id == current_tab)
    _call(_load(module, fn_name), ctx=ctx, **deps)
