from __future__ import annotations

import streamlit as st
from datamind_console.phases.phase4_naming.client import _get_phase4_client
from datamind_console.i18n import t


def render_semantics_stats_cards(*, key: str = "p4_stats") -> None:
    c = _get_phase4_client()
    try:
        s = c.stats_overview()
    except Exception as e:
        st.error(t("ui.p124.semantics_stats_cards.stats_overview_failed_e", e=(e)))
        return

    col1, col2, col3, col4 = st.columns(4)
    col1.metric(t("ui.p124.semantics_stats_cards.pending_routes"), int(s.get("pending_routes", 0) or 0))
    col2.metric(t("ui.p124.semantics_stats_cards.reviewed_not_promoted"), int(s.get("reviewed_not_promoted", 0) or 0))
    col3.metric(t("ui.p124.semantics_stats_cards.promoted_routes"), int(s.get("promoted_routes", 0) or 0))
    avg = s.get("avg_confidence")
    col4.metric(t("ui.p124.semantics_stats_cards.avg_confidence"), f"{float(avg):.3f}" if avg is not None else "—")

    st.caption(t("ui.p124.semantics_stats_cards.coverage"))
    d1, d2, d3 = st.columns(3)
    d1.metric(t("ui.p124.semantics_stats_cards.total_routes"), int(s.get("total_routes", 0) or 0))
    d2.metric(t("ui.p124.semantics_stats_cards.semantics_rows"), int(s.get("semantics_rows", 0) or 0))
    d3.metric(t("ui.p124.semantics_stats_cards.verified_rows"), int(s.get("verified_routes", 0) or 0))
