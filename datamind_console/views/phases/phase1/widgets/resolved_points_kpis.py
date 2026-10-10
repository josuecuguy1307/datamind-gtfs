# phases/phase1/widgets/resolved_points_kpis.py
from __future__ import annotations

import streamlit as st
from typing import Any, Dict

from phases.phase1_nodes.client import _get_phase1_client
from datamind_console.i18n import t


def render_resolved_points_kpis(*, node_set_id: str) -> None:
    """
    Phase 1 — Resolved Points KPIs

    Wired directly to Phase1Client.get_resolved_points_kpis(...)
    """

    st.subheader(t("ui.p124.resolved_points_kpis.resolved_points_quality"))

    if not node_set_id:
        st.info(t("ui.p124.resolved_points_kpis.select_node_set_inspect_resolved_nodes"))
        return

    phase1 = _get_phase1_client()

    try:
        kpis: Dict[str, Any] = phase1.get_resolved_points_kpis(node_set_id)
    except Exception as e:
        st.error(t("ui.p124.resolved_points_kpis.failed_load_kpis_e", e=(e)))
        return

    if not kpis or kpis.get("n_total", 0) == 0:
        st.warning(t("ui.p124.resolved_points_kpis.no_resolved_nodes_found_this_node"))
        return

    # ------------------------------
    # Top KPIs
    # ------------------------------
    c1, c2, c3 = st.columns(3)

    c1.metric(t("ui.p124.resolved_points_kpis.total_nodes"), kpis["n_total"])
    c2.metric(
        t("ui.p124.resolved_points_kpis.avg_confidence"),
        f"{kpis['avg_confidence']:.2f}" if kpis["avg_confidence"] is not None else "—",
    )
    c3.metric(
        t("ui.p124.resolved_points_kpis.median_confidence"),
        f"{kpis['median_confidence']:.2f}" if kpis["median_confidence"] is not None else "—",
    )

    st.divider()

    # ------------------------------
    # Confidence distribution
    # ------------------------------
    st.markdown(t("ui.p124.resolved_points_kpis.confidence_distribution"))

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
        st.success(t("ui.p124.resolved_points_kpis.high_confidence_overall_node_set_looks"))
    elif high_ratio >= 0.4:
        st.warning(t("ui.p124.resolved_points_kpis.mixed_confidence_review_recommended"))
    else:
        st.error(t("ui.p124.resolved_points_kpis.low_confidence_node_set_likely_needs"))
