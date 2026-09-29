# phases/phase1/widgets/resolved_points_kpis.py
from __future__ import annotations

import streamlit as st
from typing import Any, Dict

from phases.phase1_nodes.client import _get_phase1_client


def render_resolved_points_kpis(*, node_set_id: str) -> None:
    """
    Phase 1 — Resolved Points KPIs

    Wired directly to Phase1Client.get_resolved_points_kpis(...)
    """

    st.subheader("Resolved Points — Quality")

    if not node_set_id:
        st.info("Select a node set to inspect resolved nodes.")
        return

    phase1 = _get_phase1_client()

    try:
        kpis: Dict[str, Any] = phase1.get_resolved_points_kpis(node_set_id)
    except Exception as e:
        st.error(f"Failed to load KPIs: {e}")
        return

    if not kpis or kpis.get("n_total", 0) == 0:
        st.warning("No resolved nodes found for this node set.")
        return

    # ------------------------------
    # Top KPIs
    # ------------------------------
    c1, c2, c3 = st.columns(3)

    c1.metric("Total nodes", kpis["n_total"])
    c2.metric(
        "Avg confidence",
        f"{kpis['avg_confidence']:.2f}" if kpis["avg_confidence"] is not None else "—",
    )
    c3.metric(
        "Median confidence",
        f"{kpis['median_confidence']:.2f}" if kpis["median_confidence"] is not None else "—",
    )

    st.divider()

    # ------------------------------
    # Confidence distribution
    # ------------------------------
    st.markdown("### Confidence distribution")

    data = {
        "High (≥ 0.75)": kpis["n_high"],
        "Medium (0.40–0.75)": kpis["n_medium"],
        "Low (< 0.40)": kpis["n_low"],
    }

    st.bar_chart(data)

    # ------------------------------
    # Interpretation helper
    # ------------------------------
    high_ratio = kpis["n_high"] / max(kpis["n_total"], 1)

    if high_ratio >= 0.7:
        st.success("High confidence overall — node set looks healthy.")
    elif high_ratio >= 0.4:
        st.warning("Mixed confidence — review recommended.")
    else:
        st.error("Low confidence — node set likely needs reprocessing.")
