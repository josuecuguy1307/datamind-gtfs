from __future__ import annotations

import streamlit as st
from datamind_console.phases.phase4_naming.client import _get_phase4_client


def render_route_semantics_detail_card(route_id: str, *, key: str = "p4_detail") -> None:
    c = _get_phase4_client()
    try:
        d = c.get_route(route_id)
    except Exception as e:
        st.error(f"get_route({route_id}) failed: {e}")
        return

    st.subheader(f"Route detail — {route_id}")

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

    st.caption(f"Pipeline status: {status}")

    with st.expander("Route (route_prod.routes)", expanded=True):
        st.json(route)

    with st.expander("Semantics (route_prod.route_semantics)", expanded=True):
        if sem:
            st.json(sem)
        else:
            st.info("No semantics row yet for this route.")
