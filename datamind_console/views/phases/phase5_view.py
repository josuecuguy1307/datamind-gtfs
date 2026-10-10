from __future__ import annotations

from typing import Any
import os

import streamlit as st

from datamind_console.phases.phase5_gtfs.client import _get_phase5_client
from datamind_console.i18n import t
from datamind_console.ui.components.states import empty_state
from datamind_console.views.phases.panels import call_panel as _call, load_panel as _load


def _init_state() -> None:
    ss = st.session_state
    ss.setdefault("phase5.route_id", "")
    ss.setdefault("phase5.service_route_id", "")
    ss.setdefault("phase5.direction_id", 0)
    ss.setdefault("phase5.export_run_id", "")
    ss.setdefault("phase5.source_export_run_id", "")
    ss.setdefault("phase5.gtfs_id", "")
    ss.setdefault("phase5.screen", "Pipeline")
    ss.setdefault("phase5.run_mode", "One by one")


def render_phase5_view(*, ctx: Any, analytics: Any = None, audit: Any = None, **_) -> None:
    _init_state()
    ss = st.session_state

    local_only = os.getenv("DATAMIND_LOCAL_ONLY_MODE", "").strip().lower() in {"1", "true", "t", "yes", "y", "on"}
    local_dsn = os.getenv("LOCAL_DB_DSN") or os.getenv("DATAMIND_LOCAL_DB_DSN") or os.getenv("DB_DSN_LOCAL")
    if local_only and not local_dsn:
        empty_state("phase.local_dsn_missing", icon=":material/cloud_off:")
        return

    client = _get_phase5_client("v3")

    st.segmented_control(
        t("phase5.screen.label"),
        options=["Pipeline", "Runtime Lab"],
        format_func=lambda v: t("phase5.screen.pipeline") if v == "Pipeline" else t("phase5.screen.runtime_lab"),
        key="phase5.screen",
        label_visibility="collapsed",
    )

    deps = {
        "client": client,
        "analytics": analytics,
        "audit": audit,
    }

    if str(ss.get("phase5.screen") or "Pipeline") == "Runtime Lab":
        w_runtime = _load("views.phases.phase5.widgets.runtime_lab_workspace", "render_runtime_lab_workspace")
        _call(w_runtime, ctx=ctx, **deps)
        return
    st.subheader(t("phase5.build_context.title"), anchor=False)
    g1, g2, g3 = st.columns([1.1, 1.3, 1.0])
    with g1:
        new_gtfs_id = st.text_input(
            t("ui.p5.phase5.create_gtfs_id_optional_custom"),
            value="",
            key="phase5.gtfs_build.new_id",
            placeholder=t("ui.p5.phase5.e_g_gtfs_valle_2026_02"),
        )
    with g2:
        new_gtfs_name = st.text_input(
            t("ui.p5.phase5.build_name_optional"),
            value="",
            key="phase5.gtfs_build.new_name",
            placeholder=t("ui.p5.phase5.valle_feb_2026"),
        )
    with g3:
        if st.button(t("ui.p5.phase5.create_gtfs_id"), use_container_width=True, key="phase5.gtfs_build.create"):
            try:
                row = client.create_gtfs_build(
                    gtfs_id=(new_gtfs_id or "").strip() or None,
                    build_name=(new_gtfs_name or "").strip() or None,
                )
            except Exception as e:
                st.error(t("ui.p5.phase5.create_gtfs_id_failed_e", e=(e)))
            else:
                ss["phase5.gtfs_id"] = str(row.get("gtfs_id") or "")
                linked = str(row.get("export_run_id") or "")
                if linked:
                    ss["phase5.export_run_id"] = linked
                st.success(t("ui.p5.phase5.created_gtfs_id_phase5_gtfs_id", phase5_gtfs_id=(ss.get('phase5.gtfs_id'))))

    try:
        builds = client.list_gtfs_builds(limit=300)
    except Exception as e:
        st.error(t("ui.p5.phase5.failed_load_gtfs_id_list_e", e=(e)))
        builds = []
    try:
        runs = client.list_export_runs(limit=300)
    except Exception:
        runs = []

    if builds:
        build_ids = [str(r.get("gtfs_id") or "") for r in builds if str(r.get("gtfs_id") or "").strip()]
        current_gid = str(ss.get("phase5.gtfs_id") or "")
        idx = build_ids.index(current_gid) if current_gid in build_ids else 0
        sel_gid = st.selectbox(t("ui.p5.phase5.select_gtfs_id"), build_ids, index=idx, key="phase5.gtfs_build.selected")
        ss["phase5.gtfs_id"] = str(sel_gid or "")
        selected_row = next((r for r in builds if str(r.get("gtfs_id") or "") == str(sel_gid)), {})
        linked_run = str(selected_row.get("export_run_id") or "")
        if str(sel_gid or "").strip():
            try:
                linked_run = str(client.ensure_export_run_for_gtfs_id(str(sel_gid).strip()) or "")
            except Exception as e:
                st.error(t("ui.p5.phase5.failed_prepare_internal_build_context_gtfs", sel_gid=(sel_gid), e=(e)))
        if linked_run:
            ss["phase5.export_run_id"] = linked_run
        st.caption(
            t("ui.p5.phase5.gtfs_id_sel_gid_internal_build", sel_gid=(sel_gid), linked_run=(linked_run or '-'), status=(selected_row.get('status') or 'draft'))
        )
        st.caption(t("ui.p5.phase5.uploaded_gtfs_source_export_run_id"))

        upload_runs = []
        for r in (runs or []):
            rid = str(r.get("export_run_id") or "").strip()
            params = r.get("params") or {}
            src = ""
            if isinstance(params, dict):
                src = str(params.get("source") or "").strip()
            if rid and src == "upload_zip":
                upload_runs.append(r)
        run_options = [str(r.get("export_run_id") or "") for r in upload_runs]
        if run_options:
            current_src = str(ss.get("phase5.source_export_run_id") or "").strip()
            if current_src not in run_options:
                current_src = run_options[0]
            pick_run = st.selectbox(
                t("ui.p5.phase5.uploaded_gtfs_source_export_run_id_2"),
                run_options,
                index=run_options.index(current_src),
                key="phase5.gtfs_build.source_run_pick",
            )
            ss["phase5.source_export_run_id"] = str(pick_run or "").strip()
            st.caption(
                t("ui.p5.phase5.this_source_export_used_only_preview")
            )
        else:
            ss["phase5.source_export_run_id"] = ""
            st.info(t("ui.p5.phase5.no_uploaded_gtfs_source_exports_found"))
    else:
        st.info(t("ui.p5.phase5.no_gtfs_id_yet_create_one"))

    st.divider()

    st.markdown(t("ui.p5.phase5.steps_workspace"))
    if builds:
        step_gtfs_ids = [str(r.get("gtfs_id") or "") for r in builds if str(r.get("gtfs_id") or "").strip()]
        if step_gtfs_ids:
            current_step_gid = str(ss.get("phase5.gtfs_id") or "")
            step_idx = step_gtfs_ids.index(current_step_gid) if current_step_gid in step_gtfs_ids else 0
            picked_step_gid = st.selectbox(
                t("ui.p5.phase5.gtfs_context_all_steps"),
                step_gtfs_ids,
                index=step_idx,
                key="phase5.steps.gtfs_id",
                help=t("ui.p5.phase5.all_phase_5_steps_run_using"),
            )
            if str(picked_step_gid or "").strip() and str(picked_step_gid) != str(ss.get("phase5.gtfs_id") or ""):
                ss["phase5.gtfs_id"] = str(picked_step_gid)
            try:
                run_id = str(client.ensure_export_run_for_gtfs_id(str(ss.get("phase5.gtfs_id") or "").strip()) or "")
                if run_id:
                    ss["phase5.export_run_id"] = run_id
            except Exception as e:
                st.error(t("ui.p5.phase5.failed_prepare_step_context_gtfs_id", phase5_gtfs_id=(ss.get('phase5.gtfs_id')), e=(e)))

    st.markdown(t("ui.p5.phase5.run_mode"))
    st.radio(
        t("ui.p5.phase5.step_execution_mode"),
        options=["One by one", "Bulk (multi-route)"],
        format_func=lambda v: t("p5.run_mode.bulk") if v == "Bulk (multi-route)" else t("p5.run_mode.one"),
        horizontal=True,
        key="phase5.run_mode",
        help=t("ui.p5.phase5.bulk_mode_makes_route_scoped_previews"),
    )

    left, right = st.columns([1.0, 1.25], gap="large")

    with left:
        st.markdown(t("ui.p5.phase5.route_scope_selector"))
        if str(ss.get("phase5.run_mode") or "") == "Bulk (multi-route)":
            st.caption(t("ui.p5.phase5.bulk_mode_route_selector_optional_used"))
        else:
            st.caption(t("ui.p5.phase5.service_route_scope_used_steps_01"))
        w_inputs = _load("views.phases.phase5.widgets.route_inputs_table", "render_route_inputs_table")
        picked = _call(w_inputs, ctx=ctx, **deps)
        if isinstance(picked, str) and picked.strip():
            ss["phase5.route_id"] = picked.strip()

        st.divider()
        tab_00, tab_01s, tab_02r, tab_02bsh, tab_03c, tab_04t, tab_06, tab_07 = st.tabs(
            [t(f"phase5.step.{k}") for k in ("00", "01", "02", "02b", "03", "04", "06", "07")]
        )

        t_00 = _load("views.phases.phase5.tabs.step01_prepare_tab", "render_step01_prepare_tab")
        t_01s = _load("views.phases.phase5.tabs.step015_stops_tab", "render_step015_stops_tab")
        t_02r = _load("views.phases.phase5.tabs.step03_routes_shapes_tab", "render_step03_routes_shapes_tab")
        t_02bsh = _load("views.phases.phase5.tabs.step03b_shapes_tab", "render_step03b_shapes_tab")
        t_03c = _load("views.phases.phase5.tabs.step02_calendar_tab", "render_step02_calendar_tab")
        t_04t = _load("views.phases.phase5.tabs.step04_trips_tab", "render_step04_trips_tab")
        t_06 = _load("views.phases.phase5.tabs.step06_validate_tab", "render_step06_validate_tab")
        t_07 = _load("views.phases.phase5.tabs.step07_package_tab", "render_step07_package_tab")

        with tab_00:
            _call(t_00, ctx=ctx, **deps)
        with tab_01s:
            _call(t_01s, ctx=ctx, **deps)
        with tab_02r:
            _call(t_02r, ctx=ctx, **deps)
        with tab_02bsh:
            _call(t_02bsh, ctx=ctx, **deps)
        with tab_03c:
            _call(t_03c, ctx=ctx, **deps)
        with tab_04t:
            _call(t_04t, ctx=ctx, **deps)
        with tab_06:
            _call(t_06, ctx=ctx, **deps)
        with tab_07:
            _call(t_07, ctx=ctx, **deps)

    with right:
        w_workspace = _load("views.phases.phase5.widgets.phase5_workspace_map", "render_phase5_workspace_map")
        _call(w_workspace, ctx=ctx, **deps)
