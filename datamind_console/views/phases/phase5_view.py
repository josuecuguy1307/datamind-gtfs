from __future__ import annotations

from typing import Any, Callable, Optional
import inspect
import importlib
import os

import streamlit as st

from datamind_console.phases.phase5_gtfs.client import _get_phase5_client


def _call(render_fn: Optional[Callable[..., Any]], *, ctx: Any, **deps: Any) -> Any:
    if render_fn is None:
        st.info("Widget/Tab not implemented yet.")
        return None
    try:
        sig = inspect.signature(render_fn)
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return render_fn(ctx=ctx, **deps)
        safe = {k: v for k, v in deps.items() if k in params}
        if "ctx" in params:
            return render_fn(ctx=ctx, **safe)
        return render_fn(**safe)
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
        st.subheader("Phase 5 — Schedules + GTFS Publishing")
        st.info(
            "Phase 5 data is unavailable: configure LOCAL_DB_DSN for the local-only session. "
            "Remote database fallbacks are disabled."
        )
        return

    client = _get_phase5_client("v3")

    st.subheader("Phase 5 — Schedules + GTFS Publishing")
    st.radio(
        "Phase 5 screen",
        options=["Pipeline", "Runtime Lab"],
        horizontal=True,
        key="phase5.screen",
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
    st.markdown("### Widgets")

    st.markdown("#### GTFS Build Context")
    g1, g2, g3 = st.columns([1.1, 1.3, 1.0])
    with g1:
        new_gtfs_id = st.text_input(
            "Create gtfs_id (optional custom)",
            value="",
            key="phase5.gtfs_build.new_id",
            placeholder="e.g. gtfs-valle-2026-02",
        )
    with g2:
        new_gtfs_name = st.text_input(
            "Build name (optional)",
            value="",
            key="phase5.gtfs_build.new_name",
            placeholder="Valle Feb 2026",
        )
    with g3:
        if st.button("Create gtfs_id", use_container_width=True, key="phase5.gtfs_build.create"):
            try:
                row = client.create_gtfs_build(
                    gtfs_id=(new_gtfs_id or "").strip() or None,
                    build_name=(new_gtfs_name or "").strip() or None,
                )
            except Exception as e:
                st.error(f"Create gtfs_id failed: {e}")
            else:
                ss["phase5.gtfs_id"] = str(row.get("gtfs_id") or "")
                linked = str(row.get("export_run_id") or "")
                if linked:
                    ss["phase5.export_run_id"] = linked
                st.success(f"Created gtfs_id: {ss.get('phase5.gtfs_id')}")

    try:
        builds = client.list_gtfs_builds(limit=300)
    except Exception as e:
        st.error(f"Failed to load gtfs_id list: {e}")
        builds = []
    try:
        runs = client.list_export_runs(limit=300)
    except Exception:
        runs = []

    if builds:
        build_ids = [str(r.get("gtfs_id") or "") for r in builds if str(r.get("gtfs_id") or "").strip()]
        current_gid = str(ss.get("phase5.gtfs_id") or "")
        idx = build_ids.index(current_gid) if current_gid in build_ids else 0
        sel_gid = st.selectbox("Select gtfs_id", build_ids, index=idx, key="phase5.gtfs_build.selected")
        ss["phase5.gtfs_id"] = str(sel_gid or "")
        selected_row = next((r for r in builds if str(r.get("gtfs_id") or "") == str(sel_gid)), {})
        linked_run = str(selected_row.get("export_run_id") or "")
        if str(sel_gid or "").strip():
            try:
                linked_run = str(client.ensure_export_run_for_gtfs_id(str(sel_gid).strip()) or "")
            except Exception as e:
                st.error(f"Failed to prepare internal build context for gtfs_id `{sel_gid}`: {e}")
        if linked_run:
            ss["phase5.export_run_id"] = linked_run
        st.caption(
            f"gtfs_id: `{sel_gid}` | internal_build_export_run_id: `{linked_run or '-'}` | "
            f"status: `{selected_row.get('status') or 'draft'}`"
        )
        st.caption("Uploaded GTFS source export_run_id below is only for reading/importing source data.")

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
                "Uploaded GTFS source export_run_id (read-only source for loading)",
                run_options,
                index=run_options.index(current_src),
                key="phase5.gtfs_build.source_run_pick",
            )
            ss["phase5.source_export_run_id"] = str(pick_run or "").strip()
            st.caption(
                "This source export is used only to preview/load uploaded GTFS content into the selected "
                "`gtfs_id`. It is not linked or assigned to the GTFS build."
            )
        else:
            ss["phase5.source_export_run_id"] = ""
            st.info("No uploaded GTFS source exports found (`source=upload_zip`). Upload a GTFS ZIP first.")
    else:
        st.info("No gtfs_id yet. Create one to start this workflow.")

    st.divider()

    st.markdown("### Steps + Workspace")
    if builds:
        step_gtfs_ids = [str(r.get("gtfs_id") or "") for r in builds if str(r.get("gtfs_id") or "").strip()]
        if step_gtfs_ids:
            current_step_gid = str(ss.get("phase5.gtfs_id") or "")
            step_idx = step_gtfs_ids.index(current_step_gid) if current_step_gid in step_gtfs_ids else 0
            picked_step_gid = st.selectbox(
                "GTFS context for all steps",
                step_gtfs_ids,
                index=step_idx,
                key="phase5.steps.gtfs_id",
                help="All Phase 5 steps run using this gtfs_id internal build context.",
            )
            if str(picked_step_gid or "").strip() and str(picked_step_gid) != str(ss.get("phase5.gtfs_id") or ""):
                ss["phase5.gtfs_id"] = str(picked_step_gid)
            try:
                run_id = str(client.ensure_export_run_for_gtfs_id(str(ss.get("phase5.gtfs_id") or "").strip()) or "")
                if run_id:
                    ss["phase5.export_run_id"] = run_id
            except Exception as e:
                st.error(f"Failed to prepare step context for gtfs_id `{ss.get('phase5.gtfs_id')}`: {e}")

    st.markdown("#### Run mode")
    st.radio(
        "Step execution mode",
        options=["One by one", "Bulk (multi-route)"],
        horizontal=True,
        key="phase5.run_mode",
        help="Bulk mode makes route-scoped previews default to whole-export and exposes bulk actions where available.",
    )

    left, right = st.columns([1.0, 1.25], gap="large")

    with left:
        st.markdown("#### Route scope selector")
        if str(ss.get("phase5.run_mode") or "") == "Bulk (multi-route)":
            st.caption("Bulk mode: route selector is optional and used only for per-route tools.")
        else:
            st.caption("Service-route scope is used in Steps 01-03. Direction is required in Step 04/05.")
        w_inputs = _load("views.phases.phase5.widgets.route_inputs_table", "render_route_inputs_table")
        picked = _call(w_inputs, ctx=ctx, **deps)
        if isinstance(picked, str) and picked.strip():
            ss["phase5.route_id"] = picked.strip()

        st.divider()
        tab_00, tab_01s, tab_02r, tab_02bsh, tab_03c, tab_04t, tab_06, tab_07 = st.tabs(
            ["00 Agencies", "01 Stops", "02 Routes", "02b Shapes", "03 Calendar", "04 Trips+StopTimes", "06 Validate", "07 Package"]
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
