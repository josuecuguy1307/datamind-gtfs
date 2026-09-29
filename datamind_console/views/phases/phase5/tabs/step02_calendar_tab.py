from __future__ import annotations

from typing import Any

import streamlit as st

from ._gtfs_step_preview import render_gtfs_step_output_preview


def render_step02_calendar_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown("### Step 03 Build calendar")
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    route_id = (st.session_state.get("phase5.route_id") or "").strip()
    direction_id = int(st.session_state.get("phase5.direction_id") or 0)
    run_mode = str(st.session_state.get("phase5.run_mode") or "One by one")
    if not run_id:
        st.info("Create/select gtfs_id first (internal export context is created automatically).")
        return

    st.markdown("#### Service days preset")
    st.caption("Apply a standard day-pattern to service windows before building calendar.")
    presets = {
        "Mon-Fri": dict(monday=True, tuesday=True, wednesday=True, thursday=True, friday=True, saturday=False, sunday=False),
        "Mon-Sat": dict(monday=True, tuesday=True, wednesday=True, thursday=True, friday=True, saturday=True, sunday=False),
        "Mon-Sun": dict(monday=True, tuesday=True, wednesday=True, thursday=True, friday=True, saturday=True, sunday=True),
        "Mon-Wed": dict(monday=True, tuesday=True, wednesday=True, thursday=False, friday=False, saturday=False, sunday=False),
        "Thu-Sat": dict(monday=False, tuesday=False, wednesday=False, thursday=True, friday=True, saturday=True, sunday=False),
        "Weekend": dict(monday=False, tuesday=False, wednesday=False, thursday=False, friday=False, saturday=True, sunday=True),
    }
    preset_name = st.selectbox("Preset", list(presets.keys()), index=0, key="p5.step02.preset")
    scope = st.radio(
        "Apply to",
        options=["Current route + direction", "All routes (bulk)"],
        horizontal=True,
        key="p5.step02.preset.scope",
    )
    if scope == "All routes (bulk)" and run_mode != "Bulk (multi-route)":
        st.info("Switch to Bulk (multi-route) if you want this to apply to all routes.")
    if st.button("Apply preset to service windows", use_container_width=True, key="p5.step02.preset.apply"):
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
            st.success(f"Preset applied to {out.get('updated')} window rows.")

    if st.button("Run Step 02", type="primary", use_container_width=True, key="p5.step02.run"):
        with st.spinner("Building calendar..."):
            try:
                out = client.run_step_02_calendar(run_id)
            except Exception as e:
                st.error(str(e))
                return
        st.success("Step 02 completed.")
        st.json(out)

    with st.expander("Deletion log (Phase 5 revisions)", expanded=False):
        try:
            logs = client.list_revision_events(export_run_id=run_id, event_type="step_rebuild_delete", limit=200)
        except Exception as e:
            st.error(f"Failed to load deletion log: {e}")
            logs = []
        if logs:
            st.dataframe(logs, use_container_width=True, hide_index=True, height=220)
        else:
            st.info("No deletion log entries yet.")

    render_gtfs_step_output_preview(
        client=client,
        export_run_id=run_id,
        key="p5.step02.preview",
        route_id=(st.session_state.get("phase5.route_id") or "").strip() or None,
        title="Step 02 GTFS output preview (calendar tables)",
        default_tables=["gtfs_calendar", "gtfs_calendar_dates", "gtfs_trips"],
        note="Shows calendar rows currently available for the selected route scope (derived via trips service_id).",
    )
