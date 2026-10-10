from __future__ import annotations

import streamlit as st
from datamind_console.phases.phase4_naming.client import _get_phase4_client
from datamind_console.i18n import t, status_label


def render_route_semantics_detail_card(route_id: str, *, key: str = "p4_detail") -> None:
    c = _get_phase4_client()
    try:
        d = c.get_route(route_id)
    except Exception as e:
        st.error(t("ui.p124.route_semantics_detail_card.get_route_route_id_failed_e", route_id=(route_id), e=(e)))
        return

    st.subheader(t("ui.p124.route_semantics_detail_card.route_detail_route_id", route_id=(route_id)))

    route = d.raw.get("route", {})
    sem = d.raw.get("semantics", {})

    promoted = bool(route.get("human_verified")) and bool(sem.get("human_verified")) and (
        (route.get("route_name") or "") == (sem.get("route_name") or "")
    )
    if not sem:
        status = "pending"
    elif sem.get("human_verified") and not promoted:
        status = "reviewed_not_promoted"
    elif promoted:
        status = "promoted"
    else:
        status = "pending"

    st.caption(t("ui.p124.route_semantics_detail_card.pipeline_status_status", status=status_label("naming_pipeline", status)))

    with st.expander(t("ui.p124.route_semantics_detail_card.route_route_prod_routes"), expanded=True):
        st.json(route)

    with st.expander(t("ui.p124.route_semantics_detail_card.semantics_route_prod_route_semantics"), expanded=True):
        if sem:
            st.json(sem)
        else:
            st.info(t("ui.p124.route_semantics_detail_card.no_semantics_row_yet_this_route"))
