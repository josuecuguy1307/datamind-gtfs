from __future__ import annotations

from typing import Any

import streamlit as st

from datamind_console.i18n import t
from datamind_console.ui.components.states import empty_state, error_state

from ._gtfs_step_preview import render_gtfs_step_output_preview


def render_step06_validate_tab(*, ctx: Any, client: Any, **_) -> None:
    st.subheader(t("phase5.step06.title"), anchor=False)
    # This is DataMind's own database check, not the MobilityData validator;
    # say so, so a pass here is never read as "fully validated".
    st.caption(t("phase5.step06.scope"))
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    if not run_id:
        empty_state("phase5.select_gtfs_id")
        return

    if st.button(t("phase5.step06.run"), type="primary", use_container_width=True, key="p5.step06.run"):
        with st.spinner(t("phase5.step06.running")):
            try:
                out = client.run_step_06_validate(run_id)
            except Exception as e:  # noqa: BLE001 - rendered and logged
                error_state(e, context_key="phase5.step06.failed", retry=False)
                return
        if (out or {}).get("ok"):
            st.success(t("phase5.step06.passed"))
        else:
            st.error(t("phase5.step06.failed_checks", n=len((out or {}).get("errors") or [])))
        st.json(out)

    render_gtfs_step_output_preview(
        client=client,
        export_run_id=run_id,
        key="p5.step06.preview",
        route_id=(st.session_state.get("phase5.route_id") or "").strip() or None,
        title=t("p5.step06.preview_title"),
        default_tables=["gtfs_routes", "gtfs_trips", "gtfs_stop_times", "gtfs_frequencies"],
        note=t("p5.step06.preview_note"),
    )
