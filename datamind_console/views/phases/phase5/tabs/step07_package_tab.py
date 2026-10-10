from __future__ import annotations

from typing import Any

import streamlit as st

from ._gtfs_step_preview import render_gtfs_step_output_preview
from datamind_console.i18n import t


def render_step07_package_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown(t("ui.p5.step07_package.step_07_package_gtfs"))
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    if not run_id:
        st.info(t("ui.p5.step07_package.create_select_gtfs_id_first_internal"))
        return

    if st.button(t("ui.p5.step07_package.run_step_07"), type="primary", use_container_width=True, key="p5.step07.run"):
        with st.spinner(t("ui.p5.step07_package.packaging_zip")):
            try:
                out = client.run_step_07_package(run_id)
            except Exception as e:
                st.error(str(e))
                return
        st.success(t("ui.p5.step07_package.step_07_completed"))
        st.json(out)

    render_gtfs_step_output_preview(
        client=client,
        export_run_id=run_id,
        key="p5.step07.preview",
        route_id=(st.session_state.get("phase5.route_id") or "").strip() or None,
        title=t("p5.step07.preview_title"),
        default_tables=["gtfs_agency", "gtfs_routes", "gtfs_trips", "gtfs_stop_times"],
        note=t("p5.step07.preview_note"),
    )
