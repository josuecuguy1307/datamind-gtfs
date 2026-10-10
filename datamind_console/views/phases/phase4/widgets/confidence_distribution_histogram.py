from __future__ import annotations

import streamlit as st
from datamind_console.phases.phase4_naming.client import _get_phase4_client
from datamind_console.i18n import t


def render_confidence_distribution_histogram(*, key: str = "p4_hist") -> None:
    c = _get_phase4_client()
    try:
        rows = c.list_semantics(limit=5000, offset=0)
    except Exception as e:
        st.error(t("ui.p124.confidence_distribution_histogram.list_semantics_failed_e", e=(e)))
        return

    vals = [r.get("naming_confidence") for r in rows if r.get("naming_confidence") is not None]
    if not vals:
        st.info(t("ui.p124.confidence_distribution_histogram.no_confidence_values_found"))
        return

    st.subheader(t("ui.p124.confidence_distribution_histogram.confidence_distribution"))
    st.bar_chart(vals)
    st.caption(t("ui.p124.confidence_distribution_histogram.quick_bar_chart_over_raw_values"))
