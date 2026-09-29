from __future__ import annotations

import streamlit as st
from datamind_console.phases.phase4_naming.client import _get_phase4_client


def render_semantics_stats_cards(*, key: str = "p4_stats") -> None:
    c = _get_phase4_client()
    try:
        s = c.stats_overview()
    except Exception as e:
        st.error(f"stats_overview() failed: {e}")
        return

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Pending routes", int(s.get("pending_routes", 0) or 0))
    col2.metric("Reviewed not promoted", int(s.get("reviewed_not_promoted", 0) or 0))
    col3.metric("Promoted routes", int(s.get("promoted_routes", 0) or 0))
    avg = s.get("avg_confidence")
    col4.metric("Avg confidence", f"{float(avg):.3f}" if avg is not None else "—")

    st.caption("Coverage")
    d1, d2, d3 = st.columns(3)
    d1.metric("Total routes", int(s.get("total_routes", 0) or 0))
    d2.metric("Semantics rows", int(s.get("semantics_rows", 0) or 0))
    d3.metric("Verified rows", int(s.get("verified_routes", 0) or 0))
