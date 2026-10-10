from __future__ import annotations

from typing import Any

import streamlit as st

from ._gtfs_step_preview import render_gtfs_step_output_preview
from datamind_console.i18n import t


def render_step05_stop_times_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown(t("ui.p5.step05_stop_times.step_05_build_stop_times"))
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    route_id = (st.session_state.get("phase5.route_id") or "").strip()
    direction_id = int(st.session_state.get("phase5.direction_id") or 0)
    if not run_id:
        st.info(t("ui.p5.step05_stop_times.create_select_gtfs_id_first_internal"))
        return
    st.caption(
        t("ui.p5.step05_stop_times.context_export_run_id_run_id", run_id=(run_id), route_id=(route_id or '-'), direction_id=(direction_id))
    )
    st.caption(t("ui.p5.step05_stop_times.strict_mode_no_uniform_fallback_every"))

    mode = st.radio(
        t("ui.p5.step05_stop_times.run_scope"),
        options=["Selected route + direction", "Whole export_run_id"],
        format_func=lambda v: t("p5.scope.whole_export") if v == "Whole export_run_id" else t("p5.scope.current_route"),
        horizontal=True,
        key="p5.step05.scope_mode",
    )

    if st.button(t("ui.p5.step05_stop_times.run_step_05"), type="primary", use_container_width=True, key="p5.step05.run"):
        with st.spinner(t("ui.p5.step05_stop_times.building_stop_times")):
            try:
                if mode == "Selected route + direction":
                    if not route_id:
                        raise RuntimeError("Select a route first (Route scope selector) or choose 'Whole export_run_id'.")
                    out = client.run_step_05_stop_times(run_id, route_id=route_id, direction_id=direction_id)
                else:
                    out = client.run_step_05_stop_times(run_id)
            except Exception as e:
                st.error(str(e))
                return
        st.success(t("ui.p5.step05_stop_times.step_05_completed"))
        st.json(out)

    with st.expander(t("ui.p5.step05_stop_times.deletion_log_phase_5_revisions"), expanded=False):
        try:
            logs = client.list_revision_events(export_run_id=run_id, event_type="step_rebuild_delete", limit=200)
        except Exception as e:
            st.error(t("ui.p5.step05_stop_times.failed_load_deletion_log_e", e=(e)))
            logs = []
        if logs:
            st.dataframe(logs, use_container_width=True, hide_index=True, height=220)
        else:
            st.info(t("ui.p5.step05_stop_times.no_deletion_log_entries_yet"))

    render_gtfs_step_output_preview(
        client=client,
        export_run_id=run_id,
        key="p5.step05.preview",
        route_id=(st.session_state.get("phase5.route_id") or "").strip() or None,
        title=t("p5.step05.preview_title"),
        default_tables=["gtfs_stop_times", "gtfs_trips", "gtfs_stops"],
    )
