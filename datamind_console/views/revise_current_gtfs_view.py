from __future__ import annotations

from typing import Any, Dict, List, Optional
from pathlib import Path
import uuid

import pandas as pd
import pydeck as pdk
import streamlit as st

from datamind_console.phases.phase5_gtfs.client import _get_phase5_client
from datamind_console.services.gtfs_exports_service import get_latest_artifact_for_zip
from datamind_console.ui import navigation
from datamind_console.i18n import t as _t, option_label

try:
    import folium
    from streamlit_folium import st_folium
except Exception:  # pragma: no cover
    folium = None
    st_folium = None


@st.cache_data(ttl=90, show_spinner=False)
def _cached_export_runs(*, _client: Any, limit: int) -> List[Dict[str, Any]]:
    return _client.list_export_runs(limit=int(limit))


@st.cache_data(ttl=120, show_spinner=False)
def _cached_route_ids_for_export(*, _client: Any, export_run_id: str) -> List[str]:
    return _client.list_route_ids_for_export(str(export_run_id))


@st.cache_data(ttl=60, show_spinner=False)
def _cached_table_counts(*, _client: Any, export_run_id: str) -> Dict[str, int]:
    return _client.get_gtfs_table_counts(str(export_run_id))


@st.cache_data(ttl=90, show_spinner=False)
def _cached_export_map_payload(
    *,
    _client: Any,
    export_run_id: str,
    route_id: str,
    direction_id: int,
    include_all_shapes: bool,
) -> Dict[str, Any]:
    return _client.get_export_map_payload(
        str(export_run_id),
        route_id=str(route_id),
        direction_id=int(direction_id),
        include_all_shapes=bool(include_all_shapes),
    )


@st.cache_data(ttl=120, show_spinner=False)
def _cached_known_routes(*, _client: Any, export_run_id: str) -> List[Dict[str, Any]]:
    return _client.list_gtfs_rows("gtfs_routes", str(export_run_id), limit=2000)


@st.cache_data(ttl=120, show_spinner=False)
def _cached_stops_for_export(
    *,
    _client: Any,
    export_run_id: str,
    limit: int = 20000,
) -> List[Dict[str, Any]]:
    return _client.list_stops_for_export(str(export_run_id), limit=int(limit))


@st.cache_data(ttl=120, show_spinner=False)
def _cached_preview_rows(
    *,
    _client: Any,
    table_name: str,
    export_run_id: str,
    limit: int,
    route_id: str,
) -> List[Dict[str, Any]]:
    rid = route_id.strip() or None
    return _client.list_gtfs_rows(str(table_name), str(export_run_id), limit=int(limit), route_id=rid)


def _render_map(
    payload: Dict[str, Any],
    *,
    show_lines: bool = True,
    show_points: bool = True,
    show_point_labels: bool = False,
    height: int = 620,
) -> None:
    shapes = payload.get("shapes") or []
    stops = payload.get("stops") or []
    if not shapes and not stops:
        st.info(_t("ui.revise.no_map_data_this_export_run"))
        return

    lines = []
    for s in shapes:
        path = s.get("path") or []
        if len(path) >= 2:
            lines.append({"shape_id": str(s.get("shape_id") or ""), "path": path})
    line_df = pd.DataFrame(lines)

    pts = []
    for s in stops:
        pts.append(
            {
                "stop_id": str(s.get("stop_id") or ""),
                "stop_name": str(s.get("stop_name") or ""),
                "label": str(s.get("label") or s.get("stop_name") or ""),
                "lon": float(s.get("stop_lon") or 0.0),
                "lat": float(s.get("stop_lat") or 0.0),
            }
        )
    pt_df = pd.DataFrame(pts)

    if show_points and not pt_df.empty:
        c_lat = float(pt_df["lat"].median())
        c_lon = float(pt_df["lon"].median())
    elif show_lines and not line_df.empty and len(line_df.iloc[0]["path"]) > 0:
        c_lon = float(line_df.iloc[0]["path"][0][0])
        c_lat = float(line_df.iloc[0]["path"][0][1])
    else:
        c_lat, c_lon = -0.20, -78.50

    layers: List[pdk.Layer] = []
    if show_lines and not line_df.empty:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=line_df,
                get_path="path",
                get_width=4,
                width_min_pixels=2,
                get_color=[41, 98, 255, 190],
                pickable=True,
            )
        )
    if show_points and not pt_df.empty:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=pt_df,
                get_position="[lon, lat]",
                get_radius=20,
                radius_min_pixels=3,
                get_fill_color=[255, 99, 71, 200],
                pickable=True,
            )
        )
        if show_point_labels:
            layers.append(
                pdk.Layer(
                    "TextLayer",
                    data=pt_df,
                    get_position="[lon, lat]",
                    get_text="label",
                    get_size=12,
                    get_color=[32, 64, 128, 220],
                    get_text_anchor="'start'",
                    get_alignment_baseline="'center'",
                    get_pixel_offset=[8, 0],
                    pickable=False,
                )
            )

    deck = pdk.Deck(
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        initial_view_state=pdk.ViewState(latitude=c_lat, longitude=c_lon, zoom=12, pitch=0),
        layers=layers,
        tooltip={"html": "<b>{stop_name}</b><br/>{stop_id}", "style": {"fontSize": "12px"}},
    )
    st.pydeck_chart(deck, use_container_width=True, height=int(height))


def _extract_map_position(event_payload: Dict[str, Any]) -> Dict[str, float] | None:
    if not event_payload:
        return None
    for k in ("last_object_clicked", "last_clicked"):
        v = event_payload.get(k)
        if not isinstance(v, dict):
            continue
        lat = v.get("lat", v.get("latitude"))
        lon = v.get("lng", v.get("longitude"))
        if lat is None or lon is None:
            continue
        try:
            return {"lat": float(lat), "lon": float(lon)}
        except Exception:
            continue
    return None


def _render_drag_stop_map(
    *,
    points: List[Dict[str, Any]],
    selected_stop_id: str,
    draft_lat: float,
    draft_lon: float,
    key: str,
    zoom_start: int = 15,
) -> Dict[str, float] | None:
    if folium is None or st_folium is None:
        st.info(_t("ui.revise.install_streamlit_folium_enable_drag_drop"))
        return None

    fmap = folium.Map(location=[float(draft_lat), float(draft_lon)], zoom_start=int(zoom_start), tiles="CartoDB positron")
    for p in points:
        sid = str(p.get("stop_id") or "")
        try:
            lat = float(p.get("stop_lat"))
            lon = float(p.get("stop_lon"))
        except Exception:
            continue
        is_selected = sid == selected_stop_id
        folium.CircleMarker(
            location=[lat, lon],
            radius=5 if is_selected else 3,
            color="#2173ff" if is_selected else "#7c8595",
            fill=True,
            fill_opacity=0.95,
            weight=2 if is_selected else 1,
            tooltip=f"{sid}",
        ).add_to(fmap)

    folium.Marker(
        location=[float(draft_lat), float(draft_lon)],
        draggable=True,
        tooltip=_t("revise.map.drag_tooltip"),
        icon=folium.Icon(color="blue", icon="move", prefix="fa"),
    ).add_to(fmap)

    evt = st_folium(
        fmap,
        use_container_width=True,
        height=430,
        returned_objects=["last_object_clicked", "last_clicked"],
        key=f"{key}.drag_stop_map",
    )
    return _extract_map_position(evt if isinstance(evt, dict) else {})



def render_revise_current_gtfs_view(*, analytics: Any = None, audit: Any = None, **_) -> None:
    ss = st.session_state
    client = _get_phase5_client("v9")
    required_methods = (
        "get_representative_trip_id",
        "reorder_trip_stop_sequence",
        "delete_trip_stop_point",
        "delete_shape_point",
    )
    if any(not hasattr(client, m) for m in required_methods):
        st.cache_resource.clear()
        client = _get_phase5_client("v9.refresh")
    st.subheader(_t("ui.revise.revise_current_gtfs"))
    st.caption(_t("ui.revise.upload_gtfs_inspect_map_tables_edit"))
    _, top_refresh_col = st.columns([6, 1])
    with top_refresh_col:
        if st.button(_t("ui.revise.refresh_screen"), use_container_width=True, key="revise_gtfs.top_refresh"):
            st.rerun()

    ss.setdefault("revise_gtfs.export_run_id", "")
    ss.setdefault("revise_gtfs.route_id", "")
    ss.setdefault("revise_gtfs.last_update", {})
    ss.setdefault("revise_gtfs.gtfs_valhalla_result", {})
    ss.setdefault("revise_gtfs.route_ctx_key", "")
    ss.setdefault("revise_gtfs.section", "map")
    ss.setdefault("revise_gtfs.preview_file", "stops.txt")
    ss.setdefault("revise_gtfs.valhalla_workspace_mode", True)

    with st.expander(_t("ui.revise.upload_gtfs_zip"), expanded=True):
        up = st.file_uploader(_t("ui.revise.gtfs_zip"), type=["zip"], key="revise_gtfs.upload")
        c1, c2 = st.columns([1, 2])
        with c1:
            if st.button(_t("ui.revise.import_uploaded_gtfs"), type="primary", use_container_width=True, key="revise_gtfs.import"):
                if up is None:
                    st.warning(_t("ui.revise.choose_gtfs_zip_first"))
                else:
                    try:
                        out = client.import_gtfs_zip(file_bytes=up.getvalue(), filename=up.name)
                        st.cache_data.clear()
                        ss["revise_gtfs.export_run_id"] = str(out.get("export_run_id") or "")
                        st.success(_t("ui.revise.imported_gtfs_into_export_run_id", export_run_id=(out.get('export_run_id'))))
                        st.json(out.get("counts") or {})
                        st.rerun()
                    except Exception as e:
                        st.error(_t("ui.revise.import_failed_e", e=(e)))
        with c2:
            st.caption(_t("ui.revise.after_import_use_editors_below_run"))

    runs = _cached_export_runs(_client=client, limit=300)
    run_ids = [str(r.get("export_run_id")) for r in runs if r.get("export_run_id")]
    if not run_ids:
        st.info(_t("ui.revise.no_export_runs_yet_upload_gtfs"))
        return

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        cur_run = (ss.get("revise_gtfs.export_run_id") or "").strip()
        if cur_run not in run_ids:
            cur_run = run_ids[0]
        pick_run = st.selectbox("export_run_id", options=run_ids, index=run_ids.index(cur_run), key="revise_gtfs.run_pick")
        ss["revise_gtfs.export_run_id"] = pick_run
    with c2:
        route_ids = _cached_route_ids_for_export(_client=client, export_run_id=pick_run)
        route_opts = ["(all)"] + route_ids
        cur_route = (ss.get("revise_gtfs.route_id") or "").strip()
        if cur_route and cur_route not in route_ids:
            cur_route = ""
        route_pick = st.selectbox(
            _t("ui.revise.route_id_filter"),
            options=route_opts,
            index=(route_opts.index(cur_route) if cur_route in route_opts else 0),
            key="revise_gtfs.route_pick",
            format_func=option_label(),
        )
        ss["revise_gtfs.route_id"] = "" if route_pick == "(all)" else route_pick
    with c3:
        st.write("")
        if st.button(_t("ui.revise.refresh"), use_container_width=True, key="revise_gtfs.refresh"):
            st.rerun()

    run_id = (ss.get("revise_gtfs.export_run_id") or "").strip()
    route_id: Optional[str] = (ss.get("revise_gtfs.route_id") or "").strip() or None
    route_ctx_key = f"{run_id}|{route_id or '(all)'}"
    if ss.get("revise_gtfs.route_ctx_key") != route_ctx_key:
        ss["revise_gtfs.route_ctx_key"] = route_ctx_key
        ss["revise_gtfs.gtfs_valhalla_result"] = {}

    picked_direction: Optional[int] = None
    direction_shape_ids: List[str] = []
    st.markdown(_t("ui.revise.route_direction_helper"))
    if route_id:
        try:
            summary = client.get_route_direction_summary(run_id, route_id)
            dirs = [int(x["direction_id"]) for x in (summary.get("directions") or [])]
            shape_by_dir = {
                int(r.get("direction_id")): [str(x) for x in (r.get("shape_ids") or []) if x]
                for r in (summary.get("shape_ids_by_direction") or [])
            }
            if not dirs:
                st.info(_t("ui.revise.no_trips_directions_found_selected_route"))
            else:
                picked_direction = st.selectbox(
                    _t("ui.revise.direction_id_0_1"),
                    options=dirs,
                    index=0,
                    key="revise_gtfs.direction_id_filter",
                    help=_t("ui.revise.map_editor_route_view_restricted_this"),
                )
                if 0 in dirs and 1 in dirs:
                    st.caption(_t("ui.revise.direction_pair_detected_0_1"))
                elif 0 in dirs:
                    st.caption(_t("ui.revise.only_direction_0_exists_this_route"))
                elif 1 in dirs:
                    st.caption(_t("ui.revise.only_direction_1_exists_this_route"))
                direction_shape_ids = shape_by_dir.get(int(picked_direction), [])
        except Exception as e:
            st.error(_t("ui.revise.direction_helper_failed_e", e=(e)))
    else:
        st.info(_t("ui.revise.select_route_id_first_then_choose"))

    st.markdown(_t("ui.revise.send_phase_3_phase_1"))
    st.caption(
        _t("ui.revise.bridge_route_context_into_phase_3")
    )

    ss.setdefault("revise_gtfs.phase3_job_map", {})
    ss.setdefault("revise_gtfs.bridge_ctx", "")
    ss.setdefault("revise_gtfs.bridge.route_job_id", "")
    ss.setdefault("revise_gtfs.bridge.route_job_id_pending", "")
    ss.setdefault("revise_gtfs.bridge.stop_send_mode", "all")
    ss.setdefault("revise_gtfs.bridge.stop_single_key", "")
    ss.setdefault("revise_gtfs.bridge.stop_multi_keys", [])
    ss.setdefault("revise_gtfs.bridge.stop_select_ctx", "")

    bridge_ctx = f"{run_id}|{route_id or '(all)'}|{picked_direction if picked_direction is not None else '(all)'}"
    if ss.get("revise_gtfs.bridge_ctx") != bridge_ctx:
        ss["revise_gtfs.bridge_ctx"] = bridge_ctx
        # Always clear manual Phase 3 route_id when route/direction context changes.
        # This avoids accidentally reusing a previous job id on a different route.
        ss["revise_gtfs.bridge.route_job_id"] = ""
        ss["revise_gtfs.bridge.route_job_id_pending"] = ""

    # Apply pending route_job_id BEFORE widget creation (safe for Streamlit state rules).
    pending_job_id = str(ss.get("revise_gtfs.bridge.route_job_id_pending") or "").strip()
    if pending_job_id:
        ss["revise_gtfs.bridge.route_job_id"] = pending_job_id
        ss["revise_gtfs.bridge.route_job_id_pending"] = ""

    # Stop selection for Phase 1 send is export-run scoped (not route scoped).
    stop_select_ctx = str(run_id or "")
    if ss.get("revise_gtfs.bridge.stop_select_ctx") != stop_select_ctx:
        ss["revise_gtfs.bridge.stop_select_ctx"] = stop_select_ctx
        ss["revise_gtfs.bridge.stop_send_mode"] = "all"
        ss["revise_gtfs.bridge.stop_single_key"] = ""
        ss["revise_gtfs.bridge.stop_multi_keys"] = []

    bridge_stop_rows: List[Dict[str, Any]] = []
    bridge_rep_trip_id: Optional[str] = None
    if route_id and picked_direction is not None:
        try:
            bridge_rep_trip_id = client.get_representative_trip_id(run_id, str(route_id), int(picked_direction))
            if bridge_rep_trip_id:
                bridge_stop_rows = client.list_trip_stops_with_sequence(run_id, str(bridge_rep_trip_id)) or []
        except Exception as e:
            st.caption(_t("ui.revise.stop_preview_unavailable_bridge_e", e=(e)))

    export_stop_rows: List[Dict[str, Any]] = []
    try:
        export_stop_rows = _cached_stops_for_export(
            _client=client,
            export_run_id=run_id,
            limit=20000,
        )
    except Exception as e:
        st.caption(_t("ui.revise.gtfs_stops_list_unavailable_e", e=(e)))

    c_b1, c_b2, c_b3 = st.columns([2, 1, 1])
    with c_b1:
        st.text_input(
            _t("ui.revise.phase_3_route_id_optional_existing"),
            value=str(ss.get("revise_gtfs.bridge.route_job_id") or ""),
            key="revise_gtfs.bridge.route_job_id",
            help=_t("ui.revise.leave_empty_auto_create_new_phase"),
        )
    with c_b2:
        st.write("")
        st.caption(_t("ui.revise.stops_current_route_preview_n_bridge", n_bridge_stop_rows=(len(bridge_stop_rows))))
    with c_b3:
        st.write("")
        st.caption(_t("ui.revise.gtfs_stops_export_n_export_stop", n_export_stop_rows=(len(export_stop_rows))))

    ordered_export_rows = sorted(
        export_stop_rows,
        key=lambda r: (
            str(r.get("stop_name") or "").lower(),
            str(r.get("stop_id") or ""),
        ),
    )
    stop_choice_labels: List[str] = []
    stop_choice_map: Dict[str, Dict[str, Any]] = {}
    for r in ordered_export_rows:
        sid = str(r.get("stop_id") or "")
        name = str(r.get("stop_name") or "")
        lbl = f"{name} | {sid}"
        stop_choice_labels.append(lbl)
        stop_choice_map[lbl] = r

    selected_stop_rows_for_phase1: List[Dict[str, Any]] = list(ordered_export_rows)
    if stop_choice_labels:
        stop_mode_options = ["all", "one", "multiple"]
        cur_mode = str(ss.get("revise_gtfs.bridge.stop_send_mode") or "all")
        if cur_mode not in stop_mode_options:
            cur_mode = "all"
            ss["revise_gtfs.bridge.stop_send_mode"] = "all"
        send_mode = st.radio(
            _t("ui.revise.stops_send_phase_1"),
            options=stop_mode_options,
            index=stop_mode_options.index(cur_mode),
            format_func=lambda v: _t(f"revise.stop_mode.{v}"),
            horizontal=True,
            key="revise_gtfs.bridge.stop_send_mode",
        )
        if send_mode == "one":
            current_single = str(ss.get("revise_gtfs.bridge.stop_single_key") or "")
            single_index = stop_choice_labels.index(current_single) if current_single in stop_choice_labels else 0
            one_pick = st.selectbox(
                _t("ui.revise.pick_one_stop"),
                options=stop_choice_labels,
                index=single_index,
                key="revise_gtfs.bridge.stop_single_key",
            )
            selected_stop_rows_for_phase1 = [stop_choice_map[one_pick]]
        elif send_mode == "multiple":
            current_multi = [x for x in (ss.get("revise_gtfs.bridge.stop_multi_keys") or []) if x in stop_choice_labels]
            multi_pick = st.multiselect(
                _t("ui.revise.pick_multiple_stops"),
                options=stop_choice_labels,
                default=current_multi,
                key="revise_gtfs.bridge.stop_multi_keys",
            )
            selected_stop_rows_for_phase1 = [stop_choice_map[x] for x in multi_pick if x in stop_choice_map]
        else:
            selected_stop_rows_for_phase1 = list(ordered_export_rows)
        st.caption(_t("ui.revise.stops_selected_phase_1_send_n", n_selected_stop_rows_for_phase1=(len(selected_stop_rows_for_phase1))))

    def _ensure_phase3_job_id() -> str:
        route_job_id = (ss.get("revise_gtfs.bridge.route_job_id") or "").strip()
        if route_job_id:
            try:
                uuid.UUID(route_job_id)
            except Exception as e:
                raise RuntimeError(f"Invalid Phase 3 route_id: {route_job_id}") from e
            return route_job_id

        from datamind_console.phases.phase3_routes.client import _get_phase3_client

        p3 = _get_phase3_client()
        auth_user = ss.get("auth.user") or {}
        created_by = (
            str(auth_user.get("email") or "").strip()
            or str(auth_user.get("display_name") or "").strip()
            or str(auth_user.get("user_id") or "").strip()
            or "console"
        )
        notes = (
            f"GTFS bridge | export_run_id={run_id} | route_id={route_id or '(all)'} "
            f"| direction_id={picked_direction if picked_direction is not None else '(all)'}"
        )
        new_job_id = str(p3.create_route_job(created_by=created_by, notes=notes))
        bridge_map = dict(ss.get("revise_gtfs.phase3_job_map") or {})
        bridge_map[bridge_ctx] = new_job_id
        ss["revise_gtfs.phase3_job_map"] = bridge_map
        # Defer widget value update to next rerun-safe pre-widget assignment.
        ss["revise_gtfs.bridge.route_job_id_pending"] = new_job_id
        return new_job_id

    st.caption(_t("ui.revise.phase_3_bridge_single_action_send"))
    bridge_nav1, bridge_nav2 = st.columns(2)
    with bridge_nav1:
        open_phase3_after_send = st.checkbox(
            _t("ui.revise.open_phase_3_after_send"),
            value=True,
            key="revise_gtfs.bridge.open_phase3",
        )
    with bridge_nav2:
        open_phase1_after_send = st.checkbox(
            _t("ui.revise.open_phase_1_new_nodes_after"),
            value=False,
            key="revise_gtfs.bridge.open_phase1",
        )

    b_send1, b_send3 = st.columns(2)
    with b_send1:
        if st.button(
            _t("ui.revise.send_route_seed_stop_prior_phase"),
            use_container_width=True,
            key="revise_gtfs.bridge.send_route",
        ):
            if not route_id:
                st.warning(_t("ui.revise.select_route_id_first"))
            else:
                try:
                    from datamind_console.phases.phase3_routes.client import _get_phase3_client

                    p3 = _get_phase3_client()
                    p3_route_id = _ensure_phase3_job_id()
                    ss["p3.route_id"] = str(p3_route_id)
                    ss["phase3.route_id"] = str(p3_route_id)

                    seeded_n = 0
                    if bridge_stop_rows:
                        ordered = sorted(bridge_stop_rows, key=lambda r: int(r.get("stop_sequence") or 0))
                        prior_rows: List[Dict[str, Any]] = []
                        for i, r in enumerate(ordered, start=1):
                            prior_rows.append(
                                {
                                    "seq": int(i),
                                    "lat": float(r.get("stop_lat") or 0.0),
                                    "lon": float(r.get("stop_lon") or 0.0),
                                    "role": "gtfs_stop",
                                    "osm_node_id": None,
                                    "matched_stop_node_id": None,
                                    "match_dist_m": None,
                                }
                            )
                        p3.replace_relation_stop_prior(uuid.UUID(str(p3_route_id)), prior_rows)
                        seeded_n = len(prior_rows)

                    st.success(
                        _t("revise.p3_job_ready", route_id=p3_route_id)
                        + (" | " + _t("revise.stop_prior_seeded", n=seeded_n) if seeded_n > 0 else "")
                    )
                    if open_phase3_after_send:
                        ss["phases.current_phase"] = 3
                        navigation.go("phases", phase=3)
                except Exception as e:
                    st.error(_t("ui.revise.send_route_failed_e", e=(e)))

    with b_send3:
        if st.button(_t("ui.revise.send_selected_gtfs_stops_phase_1"), use_container_width=True, key="revise_gtfs.bridge.send_p1"):
            if not export_stop_rows:
                st.warning(_t("ui.revise.no_gtfs_stops_available_this_export"))
            elif not selected_stop_rows_for_phase1:
                st.warning(_t("ui.revise.select_at_least_one_stop_send"))
            else:
                try:
                    import importlib
                    import datamind_console.phases.phase1_nodes.client as phase1_client_mod

                    phase1_client_mod = importlib.reload(phase1_client_mod)
                    p1 = phase1_client_mod.Phase1Client()
                    route_job_id_txt = (ss.get("revise_gtfs.bridge.route_job_id") or "").strip()
                    if route_job_id_txt:
                        try:
                            uuid.UUID(route_job_id_txt)
                        except Exception as e:
                            raise RuntimeError(f"Invalid Phase 3 route_id: {route_job_id_txt}") from e
                    auth_user = ss.get("auth.user") or {}
                    requested_by = (
                        str(auth_user.get("email") or "").strip()
                        or str(auth_user.get("display_name") or "").strip()
                        or str(auth_user.get("user_id") or "").strip()
                        or None
                    )

                    existing = p1.list_node_review_requests(source="phase3_route", status=None, limit=20000) or []
                    existing_keys: set[tuple[str, str]] = set()
                    for r in existing:
                        tags = r.get("tags") or {}
                        if isinstance(tags, str):
                            try:
                                import json as _json

                                tags = _json.loads(tags)
                            except Exception:
                                tags = {} 
                        if not isinstance(tags, dict):
                            tags = {}
                        ex_run = str(tags.get("export_run_id") or "").strip()
                        ex_sid = str(tags.get("gtfs_stop_id") or "").strip()
                        if ex_run and ex_sid:
                            existing_keys.add((ex_run, ex_sid))

                    created = 0
                    skipped = 0
                    invalid = 0
                    selected_ordered = sorted(
                        selected_stop_rows_for_phase1,
                        key=lambda r: (
                            str(r.get("stop_name") or "").lower(),
                            str(r.get("stop_id") or ""),
                        ),
                    )
                    for r in selected_ordered:
                        stop_id = str(r.get("stop_id") or "").strip()
                        if not stop_id:
                            invalid += 1
                            continue
                        key = (str(run_id), stop_id)
                        if key in existing_keys:
                            skipped += 1
                            continue
                        p1.create_node_review_request(
                            source="phase3_route",
                            route_id=(route_job_id_txt or None),
                            seq=None,
                            lat=float(r.get("stop_lat") or 0.0),
                            lon=float(r.get("stop_lon") or 0.0),
                            node_type="STOP",
                            name=str(r.get("stop_name") or "") or None,
                            ref=stop_id,
                            operator=None,
                            tags={
                                "origin": "revise_gtfs_export_stops",
                                "export_run_id": str(run_id),
                                "gtfs_route_id": str(route_id or ""),
                                "direction_id": (int(picked_direction) if picked_direction is not None else None),
                                "trip_id": str(bridge_rep_trip_id or ""),
                                "gtfs_stop_id": stop_id,
                            },
                            requested_by=requested_by,
                            notes="Requested from Revise GTFS export stops list.",
                        )
                        created += 1

                    st.success(
                        _t("ui.revise.phase_1_requests_ready_selected_n", n_selected_ordered=(len(selected_ordered)), created=(created), skipped=(skipped), invalid=(invalid), route_job_id_txt=(route_job_id_txt or '-'))
                    )
                    if open_phase1_after_send:
                        # Phase 1 no longer has a "New Nodes" section (it is a
                        # monitoring view), so the old p1.section jump landed
                        # nowhere in particular; open Phase 1 explicitly.
                        ss["phases.current_phase"] = 1
                        navigation.go("phases", phase=1)
                except Exception as e:
                    st.error(_t("ui.revise.send_phase_1_failed_e", e=(e)))

    st.markdown(_t("ui.revise.open_section"))
    section_options = ["map", "stoptimes", "shapepoints", "stops", "valhalla"]
    current_section = str(ss.get("revise_gtfs.section") or "map")
    if current_section not in section_options:
        current_section = "map"
    if ss.get("revise_gtfs.section_picker") not in section_options:
        ss.pop("revise_gtfs.section_picker", None)
    active_section = st.radio(
        _t("ui.revise.section"),
        options=section_options,
        index=section_options.index(current_section),
        format_func=lambda v: _t(f"revise.section.{v}"),
        horizontal=True,
        label_visibility="collapsed",
        key="revise_gtfs.section_picker",
    )
    ss["revise_gtfs.section"] = active_section
    st.divider()
    try:
        counts = _cached_table_counts(_client=client, export_run_id=run_id)
        st.dataframe(
            [{"table": t, "rows": int(n)} for t, n in counts.items()],
            hide_index=True,
            use_container_width=True,
        )
    except Exception:
        pass

    if active_section == "map":
        st.markdown(_t("ui.revise.map_preview_selected_route_selected_direction"))
        try:
            if not route_id:
                st.info(_t("ui.revise.select_route_id_first"))
            elif picked_direction is None:
                st.info(_t("ui.revise.select_direction_id_first"))
            else:
                show_all_shapes = st.checkbox(
                    _t("ui.revise.show_all_shapes_debug"),
                    value=False,
                    key="revise_gtfs.map_show_all_shapes",
                    help=_t("ui.revise.off_by_default_avoid_accumulated_spaghetti"),
                )
                payload = _cached_export_map_payload(
                    _client=client,
                    export_run_id=run_id,
                    route_id=str(route_id),
                    direction_id=int(picked_direction),
                    include_all_shapes=bool(show_all_shapes),
                )
                rep_trip = payload.get("representative_trip_id")
                rep_shape = payload.get("representative_shape_id")
                if not show_all_shapes:
                    st.caption(_t("ui.revise.displaying_representative_trip_shape_trip_id", rep_trip=(rep_trip or '-'), rep_shape=(rep_shape or '-')))
                _render_map(payload)
        except Exception as e:
            st.error(_t("ui.revise.map_load_failed_e", e=(e)))

    if active_section == "stoptimes":
        st.markdown(_t("ui.revise.edit_stoptimes"))
        trip_ids = client.list_trip_ids_for_export(run_id, route_id=route_id, direction_id=picked_direction)
        if not trip_ids:
            st.info(_t("ui.revise.no_trips_found_this_export"))
        else:
            trip_id = st.selectbox("trip_id", options=trip_ids, key="revise_gtfs.trip_id")
            rows = client.list_stop_times_by_trip(run_id, trip_id, limit=4000)
            if rows:
                seqs = [int(r["stop_sequence"]) for r in rows]
                seq_pick = st.selectbox("stop_sequence", options=seqs, key="revise_gtfs.st_seq")
                row = next((r for r in rows if int(r["stop_sequence"]) == int(seq_pick)), rows[0])
                c1, c2, c3 = st.columns(3)
                with c1:
                    arr = st.text_input("arrival_time", value=str(row.get("arrival_time") or ""), key="revise_gtfs.arr")
                with c2:
                    dep = st.text_input("departure_time", value=str(row.get("departure_time") or ""), key="revise_gtfs.dep")
                with c3:
                    sid = st.text_input("stop_id", value=str(row.get("stop_id") or ""), key="revise_gtfs.st_sid")
                confirm_st = st.checkbox(_t("ui.revise.confirm_stop_time_change"), value=False, key="revise_gtfs.confirm_stop_time")
                if st.button(_t("ui.revise.confirm_update_stop_time"), use_container_width=True, key="revise_gtfs.save_stop_time"):
                    if not confirm_st:
                        st.warning(_t("ui.revise.enable_confirmation_checkbox_first"))
                    else:
                        try:
                            out = client.update_gtfs_row(
                                "gtfs_stop_times",
                                run_id,
                                {
                                    "trip_id": trip_id,
                                    "stop_sequence": int(seq_pick),
                                    "arrival_time": arr,
                                    "departure_time": dep,
                                    "stop_id": sid,
                                },
                            )
                            ss["revise_gtfs.last_update"] = {
                                "kind": "stop_time",
                                "message": _t("revise.msg.stop_time_updated"),
                                "payload": out,
                            }
                            st.cache_data.clear()
                            st.success(_t("ui.revise.stop_time_updated"))
                        except Exception as e:
                            st.error(_t("ui.revise.update_failed_e", e=(e)))
                st.dataframe(rows, use_container_width=True, hide_index=True, height=220)

    if active_section == "shapepoints":
        st.markdown(_t("ui.revise.edit_shape_points"))
        st.caption(_t("ui.revise.explicit_editor_choose_shape_id_choose"))
        shape_ids = direction_shape_ids or client.list_shape_ids_for_export(
            run_id,
            route_id=route_id,
            direction_id=picked_direction,
        )
        if not shape_ids:
            st.info(_t("ui.revise.no_shape_id_found_this_export"))
        else:
            shape_id = st.selectbox("shape_id", options=shape_ids, key="revise_gtfs.shape_id")
            srows = client.list_shape_points(run_id, shape_id, limit=12000)
            if srows:
                def _seq_key(row: Dict[str, Any]) -> tuple:
                    raw = row.get("shape_pt_sequence")
                    try:
                        return (0, float(raw))
                    except Exception:
                        return (1, str(raw or ""))

                try:
                    srows = sorted(
                        srows,
                        key=_seq_key,
                    )
                except Exception:
                    pass
                shape_path: List[List[float]] = []
                for r in srows:
                    try:
                        lon = float(r.get("shape_pt_lon") or 0.0)
                        lat = float(r.get("shape_pt_lat") or 0.0)
                        shape_path.append([lon, lat])
                    except Exception:
                        continue
                shape_payload = {
                    "shapes": [{"shape_id": str(shape_id), "path": shape_path}] if len(shape_path) >= 2 else [],
                    "stops": [],
                }
                st.caption(_t("ui.revise.polyline_follows_selected_shape_points_order"))
                _render_map(shape_payload, show_lines=True, show_points=False)

                seq_options = [str(r.get("shape_pt_sequence")) for r in srows]
                sseq_pick = st.selectbox("shape_pt_sequence", options=seq_options, key="revise_gtfs.shape_seq")
                srow = next((r for r in srows if str(r.get("shape_pt_sequence")) == str(sseq_pick)), srows[0])
                c1, c2, c3 = st.columns(3)
                with c1:
                    slat = st.number_input("shape_pt_lat", value=float(srow.get("shape_pt_lat") or 0.0), format="%.8f", key="revise_gtfs.shape_lat")
                with c2:
                    slon = st.number_input("shape_pt_lon", value=float(srow.get("shape_pt_lon") or 0.0), format="%.8f", key="revise_gtfs.shape_lon")
                with c3:
                    sdist = st.number_input(
                        "shape_dist_traveled",
                        value=float(srow.get("shape_dist_traveled") or 0.0),
                        format="%.6f",
                        key="revise_gtfs.shape_dist",
                    )
                confirm_shape = st.checkbox(_t("ui.revise.confirm_shape_point_change"), value=False, key="revise_gtfs.confirm_shape")
                if st.button(_t("ui.revise.confirm_update_shape_point"), use_container_width=True, key="revise_gtfs.save_shape"):
                    if not confirm_shape:
                        st.warning(_t("ui.revise.enable_confirmation_checkbox_first"))
                    else:
                        try:
                            out = client.update_gtfs_row(
                                "gtfs_shapes",
                                run_id,
                                {
                                    "shape_id": shape_id,
                                    "shape_pt_sequence": srow.get("shape_pt_sequence"),
                                    "shape_pt_lat": float(slat),
                                    "shape_pt_lon": float(slon),
                                    "shape_dist_traveled": float(sdist),
                                },
                            )
                            ss["revise_gtfs.last_update"] = {
                                "kind": "shape_point",
                                "message": _t("revise.msg.shape_point_updated"),
                                "payload": out,
                            }
                            st.cache_data.clear()
                            st.success(_t("ui.revise.shape_point_updated"))
                        except Exception as e:
                            st.error(_t("ui.revise.update_failed_e", e=(e)))
                confirm_shape_delete = st.checkbox(
                    _t("ui.revise.confirm_delete_selected_shape_point"),
                    value=False,
                    key="revise_gtfs.confirm_shape_delete",
                )
                if st.button(_t("ui.revise.delete_selected_shape_point"), use_container_width=True, key="revise_gtfs.delete_shape_point"):
                    if not confirm_shape_delete:
                        st.warning(_t("ui.revise.enable_delete_confirmation_checkbox_first"))
                    else:
                        try:
                            out = client.delete_shape_point(
                                export_run_id=run_id,
                                shape_id=str(shape_id),
                                shape_pt_sequence=srow.get("shape_pt_sequence"),
                            )
                            ss["revise_gtfs.last_update"] = {
                                "kind": "shape_delete",
                                "message": _t("revise.msg.shape_point_deleted"),
                                "payload": out,
                            }
                            st.cache_data.clear()
                            st.success(_t("ui.revise.shape_point_deleted"))
                            st.rerun()
                        except Exception as e:
                            st.error(_t("ui.revise.delete_failed_e", e=(e)))
                st.dataframe(srows, use_container_width=True, hide_index=True, height=220)

    if active_section == "stops":
        st.markdown(_t("ui.revise.edit_stops_name_lat_lon"))
        stop_workspace_points: List[Dict[str, Any]] = []
        if route_id and picked_direction is not None:
            rep_trip_id = client.get_representative_trip_id(run_id, route_id, int(picked_direction))
            if rep_trip_id:
                st.markdown(_t("ui.revise.stop_order_workspace"))
                st.caption(_t("ui.revise.reorder_stops_representative_trip_selected_route"))
                seq_rows = client.list_trip_stops_with_sequence(run_id, rep_trip_id)
                stop_workspace_points = list(seq_rows or [])
                if seq_rows:
                    path = []
                    points = []
                    for r in seq_rows:
                        try:
                            lon = float(r.get("stop_lon") or 0.0)
                            lat = float(r.get("stop_lat") or 0.0)
                            seq = int(r.get("stop_sequence") or 0)
                            name = str(r.get("stop_name") or "")
                            sid = str(r.get("stop_id") or "")
                            path.append([lon, lat])
                            points.append(
                                {
                                    "stop_id": sid,
                                    "stop_name": name,
                                    "stop_lon": lon,
                                    "stop_lat": lat,
                                    "label": f"{seq}",
                                }
                            )
                        except Exception:
                            continue
                    seq_payload = {
                        "shapes": [{"shape_id": f"trip:{rep_trip_id}", "path": path}] if len(path) >= 2 else [],
                        "stops": points,
                    }
                    _render_map(seq_payload, show_lines=True, show_points=True, show_point_labels=True)

                    options = [
                        f"{int(r.get('stop_sequence') or 0):03d} | {str(r.get('stop_name') or '')} | {str(r.get('stop_id') or '')}"
                        for r in seq_rows
                    ]
                    pick = st.selectbox(_t("ui.revise.stop_reorder"), options=options, key="revise_gtfs.stop_reorder_pick")
                    row_idx = options.index(pick)
                    picked_row = seq_rows[row_idx]
                    cur_seq = int(picked_row.get("stop_sequence") or 1)

                    c_re1, c_re2 = st.columns(2)
                    with c_re1:
                        new_seq = int(
                            st.number_input(
                                _t("ui.revise.new_sequence"),
                                min_value=1,
                                max_value=max(1, len(seq_rows)),
                                value=cur_seq,
                                step=1,
                                key="revise_gtfs.stop_reorder_new_seq",
                            )
                        )
                    with c_re2:
                        mode_label = st.selectbox(
                            _t("ui.revise.reorder_mode"),
                            options=["shift", "swap"],
                            index=0,
                            format_func=lambda v: _t(f"revise.reorder_mode.{v}"),
                            key="revise_gtfs.stop_reorder_mode",
                        )
                    mode = mode_label if mode_label in ("shift", "swap") else "shift"
                    confirm_reorder = st.checkbox(
                        _t("ui.revise.confirm_stop_order_change"),
                        value=False,
                        key="revise_gtfs.confirm_stop_reorder",
                    )
                    if st.button(_t("ui.revise.confirm_reorder_stop_sequence"), use_container_width=True, key="revise_gtfs.apply_stop_reorder"):
                        if not confirm_reorder:
                            st.warning(_t("ui.revise.enable_confirmation_checkbox_first"))
                        else:
                            try:
                                out = client.reorder_trip_stop_sequence(
                                    export_run_id=run_id,
                                    trip_id=str(rep_trip_id),
                                    current_sequence=int(cur_seq),
                                    new_sequence=int(new_seq),
                                    mode=str(mode),
                                )
                                ss["revise_gtfs.last_update"] = {
                                    "kind": "stop_reorder",
                                    "message": _t("revise.msg.stop_order_updated"),
                                    "payload": out,
                                }
                                st.cache_data.clear()
                                st.success(_t("ui.revise.stop_order_updated"))
                                st.rerun()
                            except Exception as e:
                                st.error(_t("ui.revise.reorder_failed_e", e=(e)))
                    confirm_delete_stop = st.checkbox(
                        _t("ui.revise.confirm_delete_selected_stop_point"),
                        value=False,
                        key="revise_gtfs.confirm_stop_delete",
                    )
                    if st.button(_t("ui.revise.delete_selected_stop_point"), use_container_width=True, key="revise_gtfs.delete_stop_point"):
                        if not confirm_delete_stop:
                            st.warning(_t("ui.revise.enable_delete_confirmation_checkbox_first"))
                        else:
                            try:
                                out = client.delete_trip_stop_point(
                                    export_run_id=run_id,
                                    trip_id=str(rep_trip_id),
                                    stop_sequence=int(cur_seq),
                                )
                                ss["revise_gtfs.last_update"] = {
                                    "kind": "stop_delete",
                                    "message": _t("revise.msg.stop_deleted"),
                                    "payload": out,
                                }
                                st.cache_data.clear()
                                st.success(_t("ui.revise.stop_point_deleted"))
                                st.rerun()
                            except Exception as e:
                                st.error(_t("ui.revise.delete_failed_e", e=(e)))
                    st.dataframe(seq_rows, use_container_width=True, hide_index=True, height=220)

        try:
            if route_id and picked_direction is not None:
                stop_map_payload = _cached_export_map_payload(
                    _client=client,
                    export_run_id=run_id,
                    route_id=str(route_id),
                    direction_id=int(picked_direction),
                    include_all_shapes=False,
                )
                _render_map(stop_map_payload, show_lines=False, show_points=True)
        except Exception as e:
            st.warning(_t("ui.revise.stops_map_preview_failed_e", e=(e)))
        stops = client.list_stops_for_export(run_id, limit=7000)
        if not stops:
            st.info(_t("ui.revise.no_stops_found_this_export"))
        else:
            labels = [f"{s.get('stop_name') or _t('common.unnamed')} | {s.get('stop_id')}" for s in stops]
            label_pick = st.selectbox(_t("col.stop"), options=labels, key="revise_gtfs.stop_pick")
            idx = labels.index(label_pick)
            srow = stops[idx]
            stop_id = str(srow.get("stop_id") or "")
            prev_stop_id = str(ss.get("revise_gtfs.stop_edit_stop_id") or "")
            if prev_stop_id != stop_id:
                ss["revise_gtfs.stop_edit_stop_id"] = stop_id
                ss["revise_gtfs.stop_name"] = str(srow.get("stop_name") or "")
                ss["revise_gtfs.stop_lat"] = float(srow.get("stop_lat") or 0.0)
                ss["revise_gtfs.stop_lon"] = float(srow.get("stop_lon") or 0.0)

            move_mode = st.checkbox(_t("ui.revise.move_selected_stop_map"), value=False, key="revise_gtfs.stop_move_mode")
            if move_mode:
                map_points = stop_workspace_points if stop_workspace_points else stops
                pos = _render_drag_stop_map(
                    points=map_points,
                    selected_stop_id=stop_id,
                    draft_lat=float(ss.get("revise_gtfs.stop_lat", srow.get("stop_lat") or 0.0)),
                    draft_lon=float(ss.get("revise_gtfs.stop_lon", srow.get("stop_lon") or 0.0)),
                    key="revise_gtfs",
                    zoom_start=16,
                )
                if pos:
                    ss["revise_gtfs.stop_lat"] = float(pos["lat"])
                    ss["revise_gtfs.stop_lon"] = float(pos["lon"])

            c1, c2, c3 = st.columns(3)
            with c1:
                stop_name = st.text_input("stop_name", key="revise_gtfs.stop_name")
            with c2:
                stop_lat = st.number_input("stop_lat", format="%.8f", key="revise_gtfs.stop_lat")
            with c3:
                stop_lon = st.number_input("stop_lon", format="%.8f", key="revise_gtfs.stop_lon")
            sync_node = st.checkbox(_t("ui.revise.also_sync_node_prod_nodes_when"), value=False, key="revise_gtfs.sync_node")
            confirm_stop = st.checkbox(_t("ui.revise.confirm_stop_change"), value=False, key="revise_gtfs.confirm_stop")
            if st.button(_t("ui.revise.confirm_update_stop"), use_container_width=True, key="revise_gtfs.save_stop"):
                if not confirm_stop:
                    st.warning(_t("ui.revise.enable_confirmation_checkbox_first"))
                else:
                    try:
                        out = client.update_stop_and_optionally_sync_node(
                            export_run_id=run_id,
                            stop_id=stop_id,
                            stop_name=stop_name,
                            stop_lat=float(stop_lat),
                            stop_lon=float(stop_lon),
                            sync_node_prod=bool(sync_node),
                        )
                        ss["revise_gtfs.last_update"] = {
                            "kind": "stop",
                            "message": _t("revise.msg.stop_updated"),
                            "payload": out,
                        }
                        st.cache_data.clear()
                        st.success(_t("ui.revise.stop_updated"))
                    except Exception as e:
                        st.error(_t("ui.revise.update_failed_e", e=(e)))

    if active_section == "valhalla":
        st.markdown(_t("ui.revise.run_valhalla_gtfs_route_direction_only"))
        if not route_id:
            st.info(_t("ui.revise.select_route_id_first"))
        elif picked_direction is None:
            st.info(_t("ui.revise.select_direction_id_first"))
        else:
            presets = client.list_gtfs_valhalla_presets()
            preset_names = [str(p.get("name")) for p in presets if p.get("name")]
            pick_presets = st.multiselect(
                _t("ui.revise.valhalla_presets"),
                options=preset_names,
                default=(preset_names[:2] if len(preset_names) > 1 else preset_names),
                key="revise_gtfs.gtfs_valhalla_presets",
            )
            timeout_s = int(st.number_input(_t("ui.revise.timeout_seconds"), min_value=10, max_value=180, value=60, step=5, key="revise_gtfs.gtfs_valhalla_timeout"))

            if st.button(_t("ui.revise.run_valhalla"), type="primary", use_container_width=True, key="revise_gtfs.run_gtfs_valhalla"):
                try:
                    res = client.build_gtfs_valhalla_candidates(
                        export_run_id=run_id,
                        route_id=str(route_id),
                        direction_id=int(picked_direction),
                        preset_names=list(pick_presets or preset_names[:1]),
                        timeout_s=int(timeout_s),
                    )
                    ss["revise_gtfs.gtfs_valhalla_result"] = res
                    st.success(_t("ui.revise.valhalla_candidates_built_n_candidates", n_candidates=(len(res.get('candidates') or []))))
                except Exception as e:
                    st.error(_t("ui.revise.valhalla_run_failed_e", e=(e)))

            res = ss.get("revise_gtfs.gtfs_valhalla_result") or {}
            cands = res.get("candidates") or []
            if cands:
                workspace_mode = st.toggle(
                    _t("ui.revise.workspace_mode_focus_map"),
                    value=bool(ss.get("revise_gtfs.valhalla_workspace_mode", True)),
                    key="revise_gtfs.valhalla_workspace_mode",
                )
                cand_rows = [
                    {
                        "candidate_id": str(c.get("candidate_id") or ""),
                        "preset": c.get("preset"),
                        "score": c.get("score"),
                        "avg_stop_dist_m": c.get("avg_stop_dist_m"),
                        "max_stop_dist_m": c.get("max_stop_dist_m"),
                        "length_m": c.get("length_m"),
                        "n_points": c.get("n_points"),
                    }
                    for c in cands
                ]

                cand_ids = [str(c.get("candidate_id")) for c in cands if c.get("candidate_id")]
                pick_cid = st.selectbox(_t("ui.revise.candidate"), options=cand_ids, key="revise_gtfs.gtfs_valhalla_candidate_pick")
                picked = next((c for c in cands if str(c.get("candidate_id")) == pick_cid), cands[0])

                preview_payload = {
                    "shapes": [{"shape_id": f"candidate:{pick_cid}", "path": list(picked.get("shape_points") or [])}],
                    "stops": [
                        {"stop_id": f"s{i+1}", "stop_name": "", "stop_lon": p[0], "stop_lat": p[1]}
                        for i, p in enumerate((res.get("stop_points") or []))
                    ],
                }
                if workspace_mode:
                    closet_col, map_col = st.columns([1.2, 3.8])
                    with closet_col:
                        with st.expander(_t("ui.revise.candidates_closet"), expanded=True):
                            st.dataframe(cand_rows, use_container_width=True, hide_index=True, height=420)
                    with map_col:
                        _render_map(preview_payload, show_lines=True, show_points=True, height=760)
                else:
                    st.dataframe(cand_rows, use_container_width=True, hide_index=True)
                    _render_map(preview_payload, show_lines=True, show_points=True)

                shape_hint = str(res.get("shape_id") or "").strip()
                shape_target_opts = direction_shape_ids or client.list_shape_ids_for_export(
                    run_id, route_id=route_id, direction_id=picked_direction
                )
                default_idx = 0
                if shape_hint and shape_hint in shape_target_opts:
                    default_idx = shape_target_opts.index(shape_hint)
                shape_target = st.selectbox(
                    _t("ui.revise.target_shape_id_overwrite"),
                    options=shape_target_opts if shape_target_opts else [shape_hint or "(missing)"],
                    index=default_idx if shape_target_opts else 0,
                    key="revise_gtfs.gtfs_valhalla_target_shape",
                    format_func=option_label(),
                )
                confirm_apply = st.checkbox(_t("ui.revise.confirm_replace_shape_points"), value=False, key="revise_gtfs.gtfs_valhalla_confirm")
                if st.button(_t("ui.revise.confirm_update_shape_points"), use_container_width=True, key="revise_gtfs.apply_gtfs_valhalla"):
                    if not confirm_apply:
                        st.warning(_t("ui.revise.enable_confirmation_checkbox_first"))
                    elif not shape_target or shape_target == "(missing)":
                        st.warning(_t("ui.revise.no_target_shape_id_available"))
                    else:
                        try:
                            out = client.apply_gtfs_valhalla_candidate(
                                export_run_id=run_id,
                                shape_id=str(shape_target),
                                shape_points=list(picked.get("shape_points") or []),
                            )
                            ss["revise_gtfs.last_update"] = {
                                "kind": "shape_points",
                                "message": _t("revise.msg.shape_from_valhalla"),
                                "payload": out,
                            }
                            st.cache_data.clear()
                            st.success(_t("ui.revise.shape_points_updated"))
                        except Exception as e:
                            st.error(_t("ui.revise.apply_failed_e", e=(e)))

    last = ss.get("revise_gtfs.last_update") or {}
    if last:
        st.markdown(_t("ui.revise.update_confirmation"))
        st.success(str(last.get("message") or _t("revise.msg.update_applied")))
        st.json(last.get("payload") or {})

    st.divider()
    st.markdown(_t("ui.revise.final_gtfs_otp_single_zip"))
    z1, z2 = st.columns([1, 2])
    with z1:
        if st.button(_t("ui.revise.build_refresh_final_zip"), type="primary", use_container_width=True, key="revise_gtfs.package_zip"):
            try:
                out = client.run_step_07_package(run_id)
                ss["revise_gtfs.last_update"] = {
                    "kind": "package_zip",
                    "message": _t("revise.msg.zip_packaged"),
                    "payload": out,
                }
                st.cache_data.clear()
                st.success(_t("ui.revise.final_zip_generated"))
                st.rerun()
            except Exception as e:
                st.error(_t("ui.revise.package_failed_e", e=(e)))
    with z2:
        info = client.get_packaged_zip_info(run_id)
        if not info.get("zip_path"):
            st.info(_t("ui.revise.no_packaged_zip_yet_this_export"))
        elif not info.get("exists"):
            st.warning(_t("ui.revise.zip_path_recorded_but_file_not", zip_path=(info.get('zip_path'))))
        else:
            zip_path = str(info.get("zip_path"))
            st.caption(_t("ui.revise.path_zip_path", zip_path=(zip_path)))
            st.caption(_t("ui.revise.size_size_bytes_bytes", size_bytes=format((int(info.get('size_bytes') or 0)), ',')))
            artifact = None
            try:
                artifact = get_latest_artifact_for_zip(zip_path)
            except Exception as e:
                st.warning(_t("ui.revise.could_not_read_artifact_approval_state", e=(e)))
            status = str((artifact or {}).get("status") or "").strip().lower()
            if not artifact:
                st.warning(_t("ui.revise.no_artifact_approval_record_found_this"))
            elif status not in {"approved", "downloaded"}:
                st.warning(
                    _t("ui.revise.download_blocked_until_approval_current_artifact", status=(status))
                )
                st.caption(_t("ui.revise.artifact_artifact_id_token_approval_token", artifact_id=(artifact.get('artifact_id')), approval_token=(artifact.get('approval_token'))))
            else:
                try:
                    p = Path(zip_path)
                    data = p.read_bytes()
                    st.download_button(
                        _t("ui.revise.download_gtfs_zip"),
                        data=data,
                        file_name=p.name,
                        mime="application/zip",
                        use_container_width=True,
                        key="revise_gtfs.download_zip",
                    )
                except Exception as e:
                    st.warning(_t("ui.revise.cannot_open_zip_download_e", e=(e)))

            members = info.get("members") or []
            if members:
                st.caption(_t("ui.revise.zip_members"))
                st.dataframe([{"file": m} for m in members], hide_index=True, use_container_width=True, height=180)

    st.divider()
    st.markdown(_t("ui.revise.gtfs_files_visualization"))
    file_to_table = {
        "agency.txt": "gtfs_agency",
        "stops.txt": "gtfs_stops",
        "routes.txt": "gtfs_routes",
        "trips.txt": "gtfs_trips",
        "stop_times.txt": "gtfs_stop_times",
        "shapes.txt": "gtfs_shapes",
        "calendar.txt": "gtfs_calendar",
        "calendar_dates.txt": "gtfs_calendar_dates",
        "frequencies.txt": "gtfs_frequencies",
    }
    pf1, pf2 = st.columns([2, 1])
    with pf1:
        pick_file = st.selectbox(
            _t("ui.revise.preview_gtfs_file"),
            options=list(file_to_table.keys()),
            index=list(file_to_table.keys()).index(str(ss.get("revise_gtfs.preview_file") or "stops.txt")),
            key="revise_gtfs.preview_file_pick",
        )
        ss["revise_gtfs.preview_file"] = pick_file
    with pf2:
        limit = int(st.number_input(_t("ui.revise.rows"), min_value=10, max_value=10000, value=200, step=50, key="revise_gtfs.preview_limit"))
    af1, af2 = st.columns([1, 1])
    with af1:
        do_preview_load = st.button(_t("ui.revise.load_refresh_preview_data"), use_container_width=True, key="revise_gtfs.preview_load")
    with af2:
        if st.button(_t("ui.revise.clear_preview_cache"), use_container_width=True, key="revise_gtfs.preview_clear_cache"):
            st.cache_data.clear()
            st.session_state.pop("revise_gtfs.preview_rows", None)
            st.rerun()

    table_name = file_to_table.get(str(ss.get("revise_gtfs.preview_file")))
    if table_name:
        try:
            # Always show a route_id lookup list here, so users don't need to guess IDs.
            known_routes = _cached_known_routes(_client=client, export_run_id=run_id)
            if known_routes:
                route_labels = []
                route_map: Dict[str, str] = {}
                for r in known_routes:
                    rid = str(r.get("route_id") or "").strip()
                    if not rid:
                        continue
                    short = str(r.get("route_short_name") or "").strip()
                    longn = str(r.get("route_long_name") or "").strip()
                    label_parts = [rid]
                    if short:
                        label_parts.append(short)
                    if longn:
                        label_parts.append(longn)
                    lbl = " | ".join(label_parts)
                    route_labels.append(lbl)
                    route_map[lbl] = rid
                if route_labels:
                    k1, k2 = st.columns([4, 1])
                    with k1:
                        pick_known_route = st.selectbox(
                            _t("ui.revise.known_route_id_list"),
                            options=sorted(route_labels),
                            key="revise_gtfs.known_route_pick",
                            help=_t("ui.revise.pick_from_existing_gtfs_routes_route"),
                        )
                    with k2:
                        st.write("")
                        if st.button(_t("ui.revise.use_workspace"), use_container_width=True, key="revise_gtfs.use_known_route"):
                            ss["revise_gtfs.route_id"] = route_map.get(pick_known_route, "")
                            st.rerun()

            apply_route_ctx = st.checkbox(
                _t("ui.revise.apply_selected_route_id_context_this"),
                value=bool(route_id),
                key="revise_gtfs.preview_apply_route_ctx",
            )
            route_ctx = (str(ss.get("revise_gtfs.route_id") or "").strip() if apply_route_ctx else "")
            if route_ctx:
                st.caption(_t("ui.revise.route_context_active_route_ctx", route_ctx=(route_ctx)))
            preview_sig = f"{run_id}|{table_name}|{limit}|{route_ctx}"
            if do_preview_load:
                rows = _cached_preview_rows(
                    _client=client,
                    table_name=str(table_name),
                    export_run_id=run_id,
                    limit=int(limit),
                    route_id=route_ctx,
                )
                st.session_state["revise_gtfs.preview_rows"] = rows
                st.session_state["revise_gtfs.preview_sig"] = preview_sig
            else:
                rows = st.session_state.get("revise_gtfs.preview_rows")
                if st.session_state.get("revise_gtfs.preview_sig") != preview_sig:
                    rows = None
            if rows is None:
                st.info(_t("ui.revise.click_load_refresh_preview_data_query"))
                return
            if not rows:
                st.info(_t("ui.revise.no_rows_available_this_file_selected"))
            else:
                f1, f2, f3 = st.columns([2, 1, 2])
                with f1:
                    text_q = st.text_input(
                        _t("ui.revise.search_text"),
                        value="",
                        placeholder=_t("ui.revise.route_id_trip_id_stop_id"),
                        key="revise_gtfs.preview_search_text",
                    ).strip().lower()
                with f2:
                    col_opts = ["(none)"] + sorted(list(rows[0].keys()))
                    col_pick = st.selectbox(_t("ui.revise.filter_column"), options=col_opts, key="revise_gtfs.preview_filter_col", format_func=option_label())
                with f3:
                    val_pick = ""
                    if col_pick != "(none)":
                        values = sorted({str(r.get(col_pick) or "") for r in rows})
                        val_pick = st.selectbox(
                            _t("ui.revise.filter_value"),
                            options=["(all)"] + values,
                            key="revise_gtfs.preview_filter_val",
                            format_func=option_label(),
                        )
                    else:
                        st.text_input(_t("ui.revise.filter_value"), value="", disabled=True, key="revise_gtfs.preview_filter_val_disabled")

                # quick selectors for common GTFS ids if present
                q1, q2, q3, q4 = st.columns(4)
                with q1:
                    route_q = "(all)"
                    if "route_id" in rows[0]:
                        route_vals = sorted({str(r.get("route_id") or "") for r in rows})
                        route_q = st.selectbox("route_id", options=["(all)"] + route_vals, key="revise_gtfs.preview_route_q", format_func=option_label())
                with q2:
                    trip_q = "(all)"
                    if "trip_id" in rows[0]:
                        trip_vals = sorted({str(r.get("trip_id") or "") for r in rows})
                        trip_q = st.selectbox("trip_id", options=["(all)"] + trip_vals, key="revise_gtfs.preview_trip_q", format_func=option_label())
                with q3:
                    shape_q = "(all)"
                    if "shape_id" in rows[0]:
                        shape_vals = sorted({str(r.get("shape_id") or "") for r in rows})
                        shape_q = st.selectbox("shape_id", options=["(all)"] + shape_vals, key="revise_gtfs.preview_shape_q", format_func=option_label())
                with q4:
                    stop_q = "(all)"
                    stop_key = "stop_id" if "stop_id" in rows[0] else None
                    if stop_key:
                        stop_vals = sorted({str(r.get(stop_key) or "") for r in rows})
                        stop_q = st.selectbox("stop_id", options=["(all)"] + stop_vals, key="revise_gtfs.preview_stop_q", format_func=option_label())

                filtered = list(rows)
                if text_q:
                    filtered = [
                        r
                        for r in filtered
                        if any(text_q in str(v).lower() for v in r.values())
                    ]
                if col_pick != "(none)" and val_pick != "(all)":
                    filtered = [r for r in filtered if str(r.get(col_pick) or "") == str(val_pick)]
                if "route_id" in rows[0] and route_q != "(all)":
                    filtered = [r for r in filtered if str(r.get("route_id") or "") == route_q]
                if "trip_id" in rows[0] and trip_q != "(all)":
                    filtered = [r for r in filtered if str(r.get("trip_id") or "") == trip_q]
                if "shape_id" in rows[0] and shape_q != "(all)":
                    filtered = [r for r in filtered if str(r.get("shape_id") or "") == shape_q]
                if stop_key and stop_q != "(all)":
                    filtered = [r for r in filtered if str(r.get(stop_key) or "") == stop_q]

                st.caption(_t("ui.revise.showing_n_filtered_n_rows_rows", n_filtered=(len(filtered)), n_rows=(len(rows))))
                st.dataframe(filtered, hide_index=True, use_container_width=True, height=320)
        except Exception as e:
            st.error(_t("ui.revise.preview_failed_e", e=(e)))
