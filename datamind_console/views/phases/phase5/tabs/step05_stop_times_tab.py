from __future__ import annotations

from typing import Any

import streamlit as st

from ._gtfs_step_preview import render_gtfs_step_output_preview


def render_step05_stop_times_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown("### Step 05 Build stop_times")
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    route_id = (st.session_state.get("phase5.route_id") or "").strip()
    direction_id = int(st.session_state.get("phase5.direction_id") or 0)
    if not run_id:
        st.info("Create/select gtfs_id first (internal export context is created automatically).")
        return
    st.caption(
        f"Context: export_run_id=`{run_id}` | route_id=`{route_id or '-'}` | direction_id=`{direction_id}`"
    )
    st.caption("Strict mode: no uniform fallback. Every route+direction trip must have a bound Runtime Lab leg estimate.")

    mode = st.radio(
        "Run scope",
        options=["Selected route + direction", "Whole export_run_id"],
        horizontal=True,
        key="p5.step05.scope_mode",
    )

    if st.button("Run Step 05", type="primary", use_container_width=True, key="p5.step05.run"):
        with st.spinner("Building stop_times..."):
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
        st.success("Step 05 completed.")
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
        key="p5.step05.preview",
        route_id=(st.session_state.get("phase5.route_id") or "").strip() or None,
        title="Step 05 GTFS output preview (stop_times)",
        default_tables=["gtfs_stop_times", "gtfs_trips", "gtfs_stops"],
    )
