from __future__ import annotations

import streamlit as st
from datamind_console.phases.phase4_naming.client import _get_phase4_client


def render_confidence_distribution_histogram(*, key: str = "p4_hist") -> None:
    c = _get_phase4_client()
    try:
        rows = c.list_semantics(limit=5000, offset=0)
    except Exception as e:
        st.error(f"list_semantics failed: {e}")
        return

    vals = [r.get("naming_confidence") for r in rows if r.get("naming_confidence") is not None]
    if not vals:
        st.info("No confidence values found.")
        return

    st.subheader("Confidence distribution")
    st.bar_chart(vals)
    st.caption("Quick bar chart over raw values (you can replace with a binned histogram later).")
