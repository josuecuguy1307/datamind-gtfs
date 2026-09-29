from __future__ import annotations

from typing import Any

import streamlit as st

from ._gtfs_step_preview import render_gtfs_step_output_preview


def render_step06_validate_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown("### Step 06 Validate GTFS")
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    if not run_id:
        st.info("Create/select gtfs_id first (internal export context is created automatically).")
        return

    if st.button("Run Step 06", type="primary", use_container_width=True, key="p5.step06.run"):
        with st.spinner("Validating..."):
            try:
                out = client.run_step_06_validate(run_id)
            except Exception as e:
                st.error(str(e))
                return
        st.success("Step 06 completed.")
        st.json(out)

    render_gtfs_step_output_preview(
        client=client,
        export_run_id=run_id,
        key="p5.step06.preview",
        route_id=(st.session_state.get("phase5.route_id") or "").strip() or None,
        title="GTFS rows preview before/after validation (inspect columns)",
        default_tables=["gtfs_routes", "gtfs_trips", "gtfs_stop_times", "gtfs_frequencies"],
        note="Validation does not usually insert GTFS rows, but this panel lets you inspect the exact rows/columns currently in the selected GTFS context.",
    )
