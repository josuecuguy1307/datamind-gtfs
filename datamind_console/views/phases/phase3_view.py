# views/phases/phase3_view.py
"""Phase 3 — Route Constructor (Monitoring)"""
from __future__ import annotations

from typing import Any, Callable, Optional
import inspect
import importlib
import uuid

import streamlit as st
import pandas as pd
import pydeck as pdk

from datamind_console.phases.phase3_routes.client import _get_phase3_client
from datamind_console.db import console_repo

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
def _call(
    render_fn: Optional[Callable[..., Any]],
    *,
    ctx: Any,
    **deps: Any,
) -> Any:
    if render_fn is None:
        return None
    try:
        sig = inspect.signature(render_fn)
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return render_fn(ctx=ctx, **deps)
        safe_deps = {k: v for k, v in deps.items() if k in params}
        if "ctx" in params:
            return render_fn(ctx=ctx, **safe_deps)
        return render_fn(**safe_deps)
    except Exception as e:
        st.exception(e)
        return None


def _load(module_path: str, fn_name: str) -> Optional[Callable[..., Any]]:
    try:
        mod = importlib.import_module(module_path)
        fn = getattr(mod, fn_name, None)
        return fn if callable(fn) else None
    except Exception:
        return None


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
        st.info("No line geometry available for this selection.")
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
def render_phase3_view(*, ctx: Any, analytics: Any = None, audit: Any = None, **_) -> None:
    _init_phase3_state()

    try:
        client = _get_phase3_client()
    except RuntimeError as e:
        st.subheader("Phase 3 — Route Constructor (Monitoring)")
        st.info(str(e))
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

    st.subheader("Phase 3 — Route Constructor (Monitoring)")

    deps = {
        "analytics": analytics,
        "audit": audit,
        "client": client,
        "console": console,
    }

    # --- Route Queue ---
    w_queue = _load("views.phases.phase3.widgets.route_queue_table", "render_route_queue_table")
    picked_route = _call(w_queue, ctx=ctx, **deps)
    if isinstance(picked_route, str) and picked_route.strip():
        ss["p3.route_id"] = picked_route.strip()
        if ss.get("p3.geometry_candidate_id"):
            ss["p3.geometry_candidate_id"] = None

    route_id = ss.get("p3.route_id")
    if ss.get("p3.service_route_id") is not None:
        st.caption(
            f"service_route_id: `{ss.get('p3.service_route_id')}` | "
            f"direction_id: `{int(ss.get('p3.direction_id') or 0)}`"
        )

    st.divider()

    # --- Workspace Map ---
    if route_id:
        left, right = st.columns([1.0, 2.0], gap="large")

        with right:
            st.markdown("#### Workspace Map")
            st.selectbox(
                "Basemap style",
                options=list(_MAP_STYLES.keys()),
                key="p3.map_style_name",
            )
            map_mode = st.selectbox(
                "Display mode",
                options=[
                    "After Step 30 (geometry polyline)",
                    "Before Step 30 (OSM relation raw)",
                ],
                key="p3.workspace_map_mode",
            )

            if map_mode == "Before Step 30 (OSM relation raw)":
                rel_ids = _cached_list_relation_raw_ids(str(route_id))
                if not rel_ids:
                    st.info("No fetched OSM relation raw found.")
                else:
                    cur_rel = ss.get("p3.osm_relation_id")
                    if cur_rel not in rel_ids:
                        cur_rel = rel_ids[0]
                    selected_rel = st.selectbox(
                        "osm_relation_id",
                        options=rel_ids,
                        index=rel_ids.index(cur_rel) if cur_rel in rel_ids else 0,
                        key=f"p3.workspace_rel_pick.{route_id}",
                    )
                    ss["p3.osm_relation_id"] = int(selected_rel)
                    rel_geo = _cached_get_relation_geometry(str(route_id), int(selected_rel))
                    rel_coords = _coords_from_feature_geojson(rel_geo.get("geojson"))
                    _render_workspace_polyline("Before Step 30 (OSM relation raw)", rel_coords)
            else:
                set_rows = _cached_list_geometry_sets(str(route_id))
                set_rows = [r for r in set_rows if int(r.get("n_candidates") or 0) > 0]
                if not set_rows:
                    st.info("No geometry candidates available yet.")
                else:
                    set_ids = [str(r.get("set_id")) for r in set_rows if r.get("set_id")]
                    current_sid = str(ss.get("p3.active_geometry_set_id") or "")
                    if current_sid not in set_ids:
                        current_sid = set_ids[0]
                    label_map = {
                        str(r.get("set_id")): f"{r.get('set_id')} | candidates={int(r.get('n_candidates') or 0)} | created_at={r.get('created_at')}"
                        for r in set_rows
                        if r.get("set_id")
                    }
                    selected_sid = st.selectbox(
                        "geometry_set_id",
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
                        st.info("Selected geometry set has no candidates.")
                    else:
                        cur_cid = str(ss.get("p3.active_geometry_candidate_id") or ss.get("p3.geometry_candidate_id") or "")
                        if cur_cid not in cand_ids:
                            cur_cid = cand_ids[0]
                        selected_cid = st.selectbox(
                            "geometry_candidate_id",
                            options=cand_ids,
                            index=cand_ids.index(cur_cid) if cur_cid in cand_ids else 0,
                            key=f"p3.workspace_geom_pick.{route_id}.{selected_sid}",
                        )
                        ss["p3.active_geometry_candidate_id"] = selected_cid
                        ss["p3.geometry_candidate_id"] = selected_cid

                        w_geom_map = _load("views.phases.phase3.widgets.route_geometry_map", "render_route_geometry_map")
                        _call(w_geom_map, ctx=ctx, route_id=route_id, geometry_candidate_id=selected_cid, **deps)

        with left:
            st.markdown("#### Monitoring Tabs")
            tab_label = st.radio(
                "Tab",
                options=["Coverage", "Grounding Catalog", "Stop Grounding Perf", "Sequences Review", "Re-Entry Review", "Coverage Improvement", "GREEK-Validated Catalog"],
                horizontal=True,
                label_visibility="collapsed",
                key="p3.active_tab",
            )

            tab_renderers = {
                "Coverage": _load("views.phases.phase3.tabs.coverage_review_tab", "render_coverage_review_tab"),
                "Grounding Catalog": _load("views.phases.phase3.tabs.grounding_catalog_tab", "render_grounding_catalog_tab"),
                "Stop Grounding Perf": _load("views.phases.phase3.tabs.stop_grounding_perf_tab", "render_stop_grounding_perf_tab"),
                "Sequences Review": _load("views.phases.phase3.tabs.sequences_review_tab", "render_sequences_review_tab"),
                "Re-Entry Review": _load("views.phases.phase3.tabs.re_entry_review_tab", "render_re_entry_review_tab"),
                "Coverage Improvement": _load("views.phases.phase3.tabs.coverage_improvement_tab", "render_coverage_improvement_tab"),
                "GREEK-Validated Catalog": _load("views.phases.phase3.tabs.greek_validated_catalog_tab", "render_greek_validated_catalog_tab"),
                "GREEK Ship Overview": _load("views.phases.phase3.tabs.greek_ship_overview_tab", "render_greek_ship_overview_tab"),
            }
            _call(tab_renderers.get(tab_label), ctx=ctx, **deps)

    else:
        st.info("Select a route from the queue to see details.")
