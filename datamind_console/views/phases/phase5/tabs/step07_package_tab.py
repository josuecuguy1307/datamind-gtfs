from __future__ import annotations

from typing import Any

import streamlit as st

from ._gtfs_step_preview import render_gtfs_step_output_preview


def render_step07_package_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown("### Step 07 Package GTFS")
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    if not run_id:
        st.info("Create/select gtfs_id first (internal export context is created automatically).")
        return

    if st.button("Run Step 07", type="primary", use_container_width=True, key="p5.step07.run"):
        with st.spinner("Packaging zip..."):
            try:
                out = client.run_step_07_package(run_id)
            except Exception as e:
                st.error(str(e))
                return
        st.success("Step 07 completed.")
        st.json(out)

    render_gtfs_step_output_preview(
        client=client,
        export_run_id=run_id,
        key="p5.step07.preview",
        route_id=(st.session_state.get("phase5.route_id") or "").strip() or None,
        title="GTFS rows preview packaged in this context",
        default_tables=["gtfs_agency", "gtfs_routes", "gtfs_trips", "gtfs_stop_times"],
        note="Package step exports the rows already present in the selected gtfs_id/export context. Use this to inspect columns before generating CSV/ZIP.",
    )
