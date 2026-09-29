from __future__ import annotations

from typing import Any

import streamlit as st

from datamind_console.views.gtfs_exports_view import render_gtfs_exports_section
from datamind_console.widgets.gtfs_ops_widget import render_gtfs_ops_widget


def render_gtfs_ops_view(*, analytics: Any = None, audit: Any = None, **_) -> None:
    st.subheader("GTFS Operations")
    st.caption("Validate and publish a GTFS package, then review the generated export.")
    start_col, phase_col, revise_col = st.columns(3)
    with start_col:
        st.markdown("**1. Import / validate**")
        st.caption("Use the GTFS ZIP controls below.")
    with phase_col:
        if st.button("2. Build in Phase 5", key="gtfs_ops.go_phase5", use_container_width=True):
            st.session_state["ui.page"] = "Phases"
            st.session_state["phases.current_phase"] = 5
            st.rerun()
    with revise_col:
        if st.button("3. Revise current GTFS", key="gtfs_ops.go_revise", use_container_width=True):
            st.session_state["ui.page"] = "Revise Current GTFS"
            st.rerun()

    tab_ops, tab_exports = st.tabs(["1. Import & publish", "2. Review exports"])
    with tab_ops:
        render_gtfs_ops_widget()
    with tab_exports:
        render_gtfs_exports_section()
