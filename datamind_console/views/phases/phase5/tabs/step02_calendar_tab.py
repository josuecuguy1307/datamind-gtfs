from __future__ import annotations

from typing import Any

import streamlit as st

from ._gtfs_step_preview import render_gtfs_step_output_preview
from datamind_console.i18n import t


def render_step02_calendar_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown(t("ui.p5.step02_calendar.step_03_build_calendar"))
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    route_id = (st.session_state.get("phase5.route_id") or "").strip()
    direction_id = int(st.session_state.get("phase5.direction_id") or 0)
    run_mode = str(st.session_state.get("phase5.run_mode") or "One by one")
    if not run_id:
        st.info(t("ui.p5.step02_calendar.create_select_gtfs_id_first_internal"))
        return

    st.markdown(t("ui.p5.step02_calendar.service_days_preset"))
    st.caption(t("ui.p5.step02_calendar.apply_standard_day_pattern_service_windows"))
    presets = {
        "Mon-Fri": dict(monday=True, tuesday=True, wednesday=True, thursday=True, friday=True, saturday=False, sunday=False),
        "Mon-Sat": dict(monday=True, tuesday=True, wednesday=True, thursday=True, friday=True, saturday=True, sunday=False),
        "Mon-Sun": dict(monday=True, tuesday=True, wednesday=True, thursday=True, friday=True, saturday=True, sunday=True),
        "Mon-Wed": dict(monday=True, tuesday=True, wednesday=True, thursday=False, friday=False, saturday=False, sunday=False),
        "Thu-Sat": dict(monday=False, tuesday=False, wednesday=False, thursday=True, friday=True, saturday=True, sunday=False),
        "Weekend": dict(monday=False, tuesday=False, wednesday=False, thursday=False, friday=False, saturday=True, sunday=True),
    }
    preset_name = st.selectbox(
        t("ui.p5.step02_calendar.preset"), list(presets.keys()), index=0, key="p5.step02.preset",
        format_func=lambda v: t(f"p5.preset.{v.lower().replace('-', '_')}"),
    )
    scope = st.radio(
        t("ui.p5.step02_calendar.apply"),
        options=["Current route + direction", "All routes (bulk)"],
        format_func=lambda v: t("p5.scope.all_routes") if v == "All routes (bulk)" else t("p5.scope.current_route"),
        horizontal=True,
        key="p5.step02.preset.scope",
    )
    if scope == "All routes (bulk)" and run_mode != "Bulk (multi-route)":
        st.info(t("ui.p5.step02_calendar.switch_bulk_multi_route_if_you"))
    if st.button(t("ui.p5.step02_calendar.apply_preset_service_windows"), use_container_width=True, key="p5.step02.preset.apply"):
        try:
            if scope == "Current route + direction":
                if not route_id:
                    raise RuntimeError("Select a route first (Route scope selector).")
                profiles = [
                    p for p in (client.list_profiles(route_id=route_id, limit=200) or [])
                    if int(p.get("direction_id") or 0) == int(direction_id)
                ]
            else:
                profiles = client.list_profiles(route_id=None, limit=2000) or []
            profile_ids = [str(p.get("profile_id") or "") for p in profiles if str(p.get("profile_id") or "").strip()]
            if not profile_ids:
                raise RuntimeError("No service windows found for this scope.")
            out = client.update_service_windows_days(profile_ids=profile_ids, **presets[preset_name])
        except Exception as e:
            st.error(str(e))
        else:
            st.success(t("ui.p5.step02_calendar.preset_applied_updated_window_rows", updated=(out.get('updated'))))

    if st.button(t("ui.p5.step02_calendar.run_step_02"), type="primary", use_container_width=True, key="p5.step02.run"):
        with st.spinner(t("ui.p5.step02_calendar.building_calendar")):
            try:
                out = client.run_step_02_calendar(run_id)
            except Exception as e:
                st.error(str(e))
                return
        st.success(t("ui.p5.step02_calendar.step_02_completed"))
        st.json(out)

    with st.expander(t("ui.p5.step02_calendar.deletion_log_phase_5_revisions"), expanded=False):
        try:
            logs = client.list_revision_events(export_run_id=run_id, event_type="step_rebuild_delete", limit=200)
        except Exception as e:
            st.error(t("ui.p5.step02_calendar.failed_load_deletion_log_e", e=(e)))
            logs = []
        if logs:
            st.dataframe(logs, use_container_width=True, hide_index=True, height=220)
        else:
            st.info(t("ui.p5.step02_calendar.no_deletion_log_entries_yet"))

    render_gtfs_step_output_preview(
        client=client,
        export_run_id=run_id,
        key="p5.step02.preview",
        route_id=(st.session_state.get("phase5.route_id") or "").strip() or None,
        title=t("p5.step02.calendar_preview_title"),
        default_tables=["gtfs_calendar", "gtfs_calendar_dates", "gtfs_trips"],
        note=t("p5.step02.calendar_preview_note"),
    )
